"""Встроенный релей: байты проходят через SOCKS точки, назначение - только из
базы, порт закреплён за устройством, занятый порт обходится."""

import socket
import socketserver
import struct
import threading

import pytest

from device_monitor.access.relay import RelayManager, SETTINGS_KEY
from device_monitor.domain.models import Device
from device_monitor.storage.sqlalchemy_store import SqlAlchemyStorage

RANGE = range(29400, 29420)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while data := self.request.recv(4096):
            self.request.sendall(b"echo:" + data)


class _FakeSocks(socketserver.BaseRequestHandler):
    """SOCKS5 без аутентификации; любой CONNECT ведёт на эхо-сервер, а
    запрошенный адрес запоминается - по нему тест видит, куда просил релей."""
    requested: list = []
    echo_port = 0

    def handle(self):
        sock = self.request
        sock.recv(3)
        sock.sendall(b"\x05\x00")
        head = sock.recv(4)
        assert head[:2] == b"\x05\x01" and head[3] == 3
        name = sock.recv(sock.recv(1)[0]).decode()
        port = struct.unpack(">H", sock.recv(2))[0]
        _FakeSocks.requested.append((name, port))
        upstream = socket.create_connection(("127.0.0.1", _FakeSocks.echo_port))
        sock.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)

        def pump(src, dst):
            try:
                while data := src.recv(4096):
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
        t = threading.Thread(target=pump, args=(upstream, sock), daemon=True)
        t.start()
        pump(sock, upstream)
        t.join(timeout=2)


@pytest.fixture
def servers():
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    echo = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Echo)
    socks = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _FakeSocks)
    for server in (echo, socks):
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
    _FakeSocks.echo_port = echo.server_address[1]
    _FakeSocks.requested = []
    yield f"socks5://127.0.0.1:{socks.server_address[1]}"
    for server in (echo, socks):
        server.shutdown()
        server.server_close()


@pytest.fixture
def relay(tmp_path):
    manager = RelayManager(SqlAlchemyStorage(tmp_path / "m.db"), port_range=RANGE)
    manager.start()
    yield manager
    manager.stop()


def cam(i=5, site="ККия", **kw):
    return Device(id=f"{i:032x}", site=site, name=f"Камера {i}", host=f"192.168.158.{i}",
                  ports=(80,), **kw)


def _roundtrip(port: int, payload: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(payload)
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while data := s.recv(4096):
            chunks.append(data)
        return b"".join(chunks)


def test_bytes_flow_through_site_proxy(servers, relay):
    device = cam(5)
    relay.sync([device], {"ККия": servers})
    port = relay.ports()[device.id]
    assert port in RANGE
    assert _roundtrip(port, b"GET / HTTP/1.0\r\n\r\n") == b"echo:GET / HTTP/1.0\r\n\r\n"
    # Прокси попросили соединить ровно с адресом устройства из базы.
    assert _FakeSocks.requested == [("192.168.158.5", 80)]
    assert relay.url_for(device) == f"http://127.0.0.1:{port}/"


def test_https_and_custom_web_port_are_forwarded(servers, relay):
    secure = cam(6, scheme="https")
    custom = cam(7, web_port=8080)
    relay.sync([secure, custom], {"ККия": servers})
    for device in (secure, custom):
        _roundtrip(relay.ports()[device.id], b"x")
    assert sorted(_FakeSocks.requested) == [("192.168.158.6", 443), ("192.168.158.7", 8080)]


def test_devices_without_site_proxy_get_no_relay(servers, relay):
    relay.sync([cam(5, site="Торугарт")], {"Торугарт": ""})
    assert relay.ports() == {}


def test_port_is_stable_across_restart(servers, tmp_path):
    storage = SqlAlchemyStorage(tmp_path / "m.db")
    first = RelayManager(storage, port_range=RANGE)
    first.start()
    first.sync([cam(5), cam(6)], {"ККия": servers})
    ports = first.ports()
    first.stop()

    second = RelayManager(storage, port_range=RANGE)
    second.start()
    second.sync([cam(6), cam(5)], {"ККия": servers})   # порядок другой - порты те же
    assert second.ports() == ports
    second.stop()


def test_busy_port_is_skipped(servers, tmp_path):
    busy = _free_port()
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", busy))
        squatter.listen()
        manager = RelayManager(SqlAlchemyStorage(tmp_path / "m.db"),
                               port_range=range(busy, busy + 3))
        manager.start()
        manager.sync([cam(5)], {"ККия": servers})
        assert manager.ports()[cam(5).id] != busy
        manager.stop()


def test_stored_port_taken_by_other_process_is_reassigned(servers, tmp_path):
    """Порт из базы занял чужой процесс (например, старый юнит проброса) -
    устройство переезжает на свободный порт, а не остаётся без доступа."""
    storage = SqlAlchemyStorage(tmp_path / "m.db")
    busy = _free_port()
    storage.set_setting(SETTINGS_KEY, {cam(5).id: busy})
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", busy))
        squatter.listen()
        manager = RelayManager(storage, port_range=range(busy, busy + 3))
        manager.start()
        manager.sync([cam(5)], {"ККия": servers})   # круг 1: занять не вышло
        manager.sync([cam(5)], {"ККия": servers})   # круг 2: новый порт
        port = manager.ports()[cam(5).id]
        assert port != busy
        assert _roundtrip(port, b"ok") == b"echo:ok"
        manager.stop()


def test_removed_device_port_is_closed(servers, relay):
    relay.sync([cam(5)], {"ККия": servers})
    port = relay.ports()[cam(5).id]
    relay.sync([], {"ККия": servers})
    assert relay.ports() == {}
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


def test_dead_proxy_closes_client_cleanly(relay):
    dead = f"socks5://127.0.0.1:{_free_port()}"
    relay.sync([cam(5)], {"ККия": dead})
    assert _roundtrip(relay.ports()[cam(5).id], b"x") == b""


def test_state_exposes_relay_url(auth):
    """В /state у устройства точки за прокси есть relay_url (в тестах опрос
    выключен, релей не запущен - поле есть и пустое)."""
    auth.post("/api/v1/sites", json={"name": "ККия", "proxy": "socks5://127.0.0.1:1082"})
    auth.post("/api/v1/devices", json={"site": "ККия", "name": "К", "host": "192.168.158.5",
                                       "ports": [80]})
    device = auth.get("/api/v1/state").json()["devices"][0]
    assert "relay_url" in device and device["relay_url"] is None



class _SilentSocks(socketserver.BaseRequestHandler):
    """SOCKS, который соединяет и замолкает навсегда - как туннель, пропавший
    без FIN (WireGuard)."""
    def handle(self):
        sock = self.request
        sock.recv(3)
        sock.sendall(b"\x05\x00")
        head = sock.recv(4)
        sock.recv(sock.recv(1)[0] + 2)
        sock.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
        while sock.recv(4096):
            pass


@pytest.fixture
def silent_socks():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SilentSocks)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"socks5://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_idle_connection_is_closed(silent_socks, relay, monkeypatch):
    import time
    from device_monitor.access import relay as relay_mod
    monkeypatch.setattr(relay_mod, "IDLE_TIMEOUT", 1)
    relay.sync([cam(5)], {"ККия": silent_socks})
    started = time.monotonic()
    assert _roundtrip(relay.ports()[cam(5).id], b"GET / HTTP/1.0\r\n\r\n") == b""
    assert time.monotonic() - started < 5
    deadline = time.monotonic() + 3
    while relay._conns and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not relay._conns


def test_stop_closes_live_connections_and_is_idempotent(silent_socks, tmp_path):
    manager = RelayManager(SqlAlchemyStorage(tmp_path / "m.db"), port_range=RANGE)
    manager.start()
    manager.sync([cam(5)], {"ККия": silent_socks})
    client = socket.create_connection(("127.0.0.1", manager.ports()[cam(5).id]), timeout=5)
    client.sendall(b"hello")
    manager.stop()
    # Закрыто сервисом: либо обычный EOF, либо RST (сокет закрыт с
    # непрочитанными данными) - но не «висит».
    try:
        assert client.recv(10) == b""
    except ConnectionResetError:
        pass
    client.close()
    manager.stop()                        # второй stop - без зависания и ошибок


def test_stop_without_start_is_noop(tmp_path):
    RelayManager(SqlAlchemyStorage(tmp_path / "m.db")).stop()



def test_stop_race_leaves_no_open_sockets(silent_socks, tmp_path):
    """Регрессия: соединение, принятое прямо перед stop(), оставалось
    ESTABLISHED (asyncio молча ронял создание транспорта после server.close).
    До исправления воспроизводилось примерно раз на 20 остановок."""
    hung = []
    for i in range(25):
        manager = RelayManager(SqlAlchemyStorage(tmp_path / f"m{i}.db"), port_range=RANGE)
        manager.start()
        manager.sync([cam(5)], {"ККия": silent_socks})
        client = socket.create_connection(("127.0.0.1", manager.ports()[cam(5).id]), timeout=3)
        client.sendall(b"hello")
        manager.stop()
        try:
            if client.recv(10) != b"":
                hung.append(i)
        except ConnectionResetError:
            pass
        except TimeoutError:
            hung.append(i)
        client.close()
    assert hung == []
