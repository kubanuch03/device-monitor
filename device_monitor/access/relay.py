"""Встроенный TCP-релей: локальный порт на устройство за туннелем точки.

Зачем. До камер объектов за SSH-туннелем или изолированным VPN браузер сам не
дойдёт - их адреса видны только через SOCKS-прокси точки. Раньше на каждую
камеру руками дописывалась строка проброса в systemd-юнит (-L у autossh,
fwd у socat) и выполнялся sudo, а порт придумывался в голове и вписывался в
open_url. Теперь сервис сам слушает 127.0.0.1:<порт> на каждое такое
устройство и гонит байты через SOCKS точки. Новая камера открывается сразу
после добавления в панель - без sudo и без правки юнитов.

Почему сырой TCP, а не HTTP-прокси. Веб-интерфейсы Dahua/Hikvision живут
на абсолютных путях, digest-авторизации, cookies и websocket; переписывание
путей под префикс на них ломается. Байтовый релей ничего не трогает - для
браузера это та же камера, просто на локальном порту. Ограничение: cookies
привязаны к хосту, а не к порту (RFC 6265), поэтому у всех камер на
127.0.0.1 общая банка cookies - сессии двух камер, открытых одновременно,
могут перетирать друг друга. Так же было и со старыми пробросами.

Безопасность. Слушаем только 127.0.0.1. Назначение берётся только из базы
(адрес и веб-порт устройства), клиент его не выбирает - открытым прокси в
сеть объекта релей не становится. Порт есть у каждого устройства точки с
прокси (включая роутеры и серверы) и без входа в панель - как и сам SOCKS
точки на 127.0.0.1, который даёт ещё больше; это осознанно: машина
дашборда считается доверенной.

Порты закреплены за устройствами и хранятся в настройках (не в устройстве):
это свойство конкретной установки, у сотрудника на другой машине свободные
порты свои, и в экспорт они попадать не должны.
"""

from __future__ import annotations

import asyncio
import socket
import struct
import threading

from device_monitor.domain.models import Device
from device_monitor.probing.socks import ProxyError, parse_proxy

SETTINGS_KEY = "relay_ports"
PORT_RANGE = range(18200, 19000)
CONNECT_TIMEOUT = 10
# Соединение, по которому ни в одну сторону ничего не шло столько секунд,
# закрывается: иначе обрыв туннеля без FIN (WireGuard, пропавший объект)
# оставлял бы сокеты висеть до перезапуска сервиса.
IDLE_TIMEOUT = 300
BUFFER = 64 * 1024


def target_port(device: Device) -> int:
    """Порт веб-интерфейса устройства - его и пробрасываем."""
    return device.web_port or (443 if device.scheme == "https" else 80)


async def open_via_socks(proxy: str, host: str, port: int):
    """Асинхронный SOCKS5 CONNECT (без аутентификации, имя - доменом, как в
    probing/socks.py: маршрут выбирает выходной узел туннеля)."""
    proxy_host, proxy_port = parse_proxy(proxy)
    reader, writer = await asyncio.open_connection(proxy_host, proxy_port)
    try:
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        ver, method = await reader.readexactly(2)
        if ver != 5 or method != 0:
            raise ProxyError("прокси требует аутентификацию или не SOCKS5")
        name = host.encode("idna")
        writer.write(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + struct.pack(">H", port))
        await writer.drain()
        ver, rep, _rsv, atyp = await reader.readexactly(4)
        if ver != 5 or rep != 0:
            raise ProxyError(f"прокси не соединил с {host}:{port} (код {rep})")
        if atyp == 1:
            await reader.readexactly(4 + 2)
        elif atyp == 3:
            await reader.readexactly((await reader.readexactly(1))[0] + 2)
        elif atyp == 4:
            await reader.readexactly(16 + 2)
        return reader, writer
    except BaseException:
        writer.close()
        raise


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Одна сторона. Ошибку и таймаут простоя не глотает: по ним _handle
    рвёт и вторую сторону, а не оставляет её висеть."""
    while data := await asyncio.wait_for(reader.read(BUFFER), IDLE_TIMEOUT):
        writer.write(data)
        await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()   # полузакрытие: ответ с той стороны ещё дойдёт


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


class RelayManager:
    def __init__(self, storage, port_range: range = PORT_RANGE):
        self.storage = storage
        self.port_range = port_range
        self._loop: asyncio.AbstractEventLoop | None = None   # создаётся в start()
        self._thread: threading.Thread | None = None
        # Сильные ссылки на задачи соединений. Цикл событий держит задачи
        # только слабо, и задача, ждущая сокет, вместе с транспортом образует
        # недостижимый цикл - сборщик мусора её уничтожал посреди передачи
        # («Task was destroyed but it is pending»), и страница камеры рвалась.
        self._conns: set[asyncio.Task] = set()
        self._servers: dict[int, asyncio.base_events.Server] = {}
        # port -> (host, port, proxy); читается обработчиком в момент коннекта,
        # поэтому смена адреса устройства подхватывается без перезапуска порта.
        self._targets: dict[int, tuple[str, int, str]] = {}
        self._failed: set[int] = set()
        self._lock = threading.Lock()
        self._ports: dict[str, int] = {}

    # -- жизненный цикл ----------------------------------------------------------

    def start(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="relay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        loop, self._loop = self._loop, None
        if loop is None or not loop.is_running():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._close_all(), loop).result(timeout=5)
        except Exception as exc:
            print(f"[relay] остановка: {exc}", flush=True)
        loop.call_soon_threadsafe(loop.stop)
        if self._thread:
            self._thread.join(timeout=2)
        if not loop.is_running():
            loop.close()

    async def _close_all(self) -> None:
        for server in self._servers.values():
            server.close()
        self._servers.clear()
        for task in list(self._conns):
            task.cancel()
        if self._conns:
            await asyncio.wait(self._conns, timeout=2)

    # -- что пробрасывать ---------------------------------------------------------

    def ports(self) -> dict[str, int]:
        """device_id -> локальный порт (только реально слушающие)."""
        with self._lock:
            return dict(self._ports)

    def url_for(self, device: Device) -> str | None:
        port = self.ports().get(device.id)
        if not port:
            return None
        return f"{device.scheme}://127.0.0.1:{port}{device.path}"

    def sync(self, devices: list[Device], proxies: dict[str, str]) -> None:
        """Привести слушающие порты к списку устройств. Вызывается каждый круг
        опроса - дёшево: база пишется, только если назначения поменялись."""
        wanted = {d.id: d for d in devices if proxies.get(d.site)}
        stored = self.storage.get_setting(SETTINGS_KEY)
        assigned = {k: v for k, v in stored.items()
                    if k in wanted and isinstance(v, int) and v in self.port_range}
        # Порт, который не удалось занять (его держит чужой процесс), отдаём:
        # устройство получит другой на этом же круге.
        assigned = {k: v for k, v in assigned.items() if v not in self._failed}

        taken = set(assigned.values())
        for device_id in wanted:
            if device_id in assigned:
                continue
            port = next((p for p in self.port_range
                         if p not in taken and p not in self._failed and p not in self._servers
                         and _port_free(p)), None)
            if port is None:
                print("[relay] свободные порты кончились", flush=True)
                break
            assigned[device_id] = port
            taken.add(port)

        if assigned != stored:
            self.storage.set_setting(SETTINGS_KEY, assigned)

        targets = {port: (wanted[device_id].host, target_port(wanted[device_id]),
                          proxies[wanted[device_id].site])
                   for device_id, port in assigned.items()}
        if self._loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(self._apply(targets), self._loop)
        listening = future.result(timeout=10)
        with self._lock:
            self._ports = {d: p for d, p in assigned.items() if p in listening}

    async def _apply(self, targets: dict[int, tuple[str, int, str]]) -> set[int]:
        self._targets = targets
        for port in list(self._servers):
            if port not in targets:
                self._servers.pop(port).close()
        for port in targets:
            if port in self._servers:
                continue
            try:
                self._servers[port] = await asyncio.start_server(
                    lambda r, w, p=port: self._track(self._handle(p, r, w)), "127.0.0.1", port)
            except OSError as exc:
                self._failed.add(port)
                print(f"[relay] порт {port} занят другим процессом: {exc}", flush=True)
        return set(self._servers)

    # -- соединение ---------------------------------------------------------------

    def _track(self, coro) -> None:
        task = self._loop.create_task(coro)
        self._conns.add(task)
        task.add_done_callback(self._conns.discard)

    async def _handle(self, port: int, client_reader, client_writer) -> None:
        target = self._targets.get(port)
        upstream_writer = None
        pipes: list[asyncio.Task] = []
        try:
            if target is None:
                return
            host, target_port_, proxy = target
            upstream_reader, upstream_writer = await asyncio.wait_for(
                open_via_socks(proxy, host, target_port_), CONNECT_TIMEOUT)
            pipes = [asyncio.create_task(_pipe(client_reader, upstream_writer)),
                     asyncio.create_task(_pipe(upstream_reader, client_writer))]
            # Нормальный конец одной стороны (EOF) - ждём вторую: ответ ещё
            # идёт. Ошибка или простой одной стороны - рвём обе.
            await asyncio.wait(pipes, return_when=asyncio.FIRST_EXCEPTION)
        except (OSError, ProxyError, UnicodeError, asyncio.TimeoutError,
                asyncio.IncompleteReadError):
            pass   # устройство или туннель недоступны - браузер увидит обрыв
        finally:
            for pipe in pipes:
                pipe.cancel()
            for writer in (client_writer, upstream_writer):
                if writer is not None:
                    writer.close()
