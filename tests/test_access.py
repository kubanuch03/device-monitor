"""Подключение объектов без терминала: SSH-туннель сервиса, импорт WireGuard."""

import os
import stat
import time

import pytest

from device_monitor.access import ssh_setup, tunnels, vpn
from device_monitor.domain.errors import ValidationError
from device_monitor.storage.sqlalchemy_store import SqlAlchemyStorage
from device_monitor.domain.models import Site


# -- адрес SSH ------------------------------------------------------------------

def test_clean_target():
    assert ssh_setup.clean_target("parking@10.20.4.2") == ("parking@10.20.4.2", [])
    assert ssh_setup.clean_target("jaks@10.24.24.23:2222") == ("jaks@10.24.24.23", ["-p", "2222"])
    for bad in ("10.20.4.2", "user@", "user@host; rm -rf /", "-oProxyCommand=x@h",
                "-Jroot@evil.com", "-E.ssh@x", "-f@h", "user@-host", "u@h:0", "u@h:99999"):
        with pytest.raises(ValidationError):
            ssh_setup.clean_target(bad)


# -- WireGuard: проверки перед импортом ----------------------------------------------

CONF = """[Interface]
PrivateKey = x
Address = 10.50.0.2/30

[Peer]
PublicKey = y
Endpoint = 1.2.3.4:51820
AllowedIPs = {allowed}
"""


def test_wireguard_refuses_full_tunnel(monkeypatch):
    monkeypatch.setattr(vpn, "current_routes", lambda: [])
    with pytest.raises(ValidationError, match="весь интернет"):
        vpn.check_conf(CONF.format(allowed="0.0.0.0/0"), "obj1")


def test_wireguard_refuses_overlap_with_existing_vpn(monkeypatch):
    import ipaddress
    monkeypatch.setattr(vpn, "current_routes",
                        lambda: [(ipaddress.ip_network("192.168.158.0/24"), "wg0")])
    with pytest.raises(ValidationError, match="изолированный"):
        vpn.check_conf(CONF.format(allowed="192.168.158.0/24, 172.30.11.0/24"), "kk2")


def test_wireguard_accepts_free_networks(monkeypatch):
    import ipaddress
    monkeypatch.setattr(vpn, "current_routes",
                        lambda: [(ipaddress.ip_network("10.20.0.0/16"), "wg0")])
    nets = vpn.check_conf(CONF.format(allowed="10.24.24.0/24"), "obj1")
    assert [str(n) for n in nets] == ["10.24.24.0/24"]


def test_wireguard_rejects_garbage():
    with pytest.raises(ValidationError, match="WireGuard"):
        vpn.check_conf("не конфиг", "obj1")


# -- туннель сервиса: поддельный ssh -------------------------------------------------

FAKE_SSH = """#!/usr/bin/env python3
# Поддельный ssh: слушает порт из -D, как настоящий ssh -D, и ждёт.
import socket, sys, time
port = int(sys.argv[sys.argv.index("-D") + 1].split(":")[1])
if "bad@host" in sys.argv:
    sys.stderr.write("bad@host: Permission denied (publickey).\\n"); sys.exit(255)
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port)); s.listen()
if "flood@host" in sys.argv:
    # Как ssh -D на каждое неудачное соединение: строки в stderr. Без
    # вычитывания пайп (64 КБ) заполнился бы и ssh встал бы на записи.
    for i in range(5000):
        sys.stderr.write(f"channel {i}: open failed: connect failed: No route to host\\n")
    sys.stderr.flush()
while True:
    time.sleep(1)
"""


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    path = tmp_path / "bin" / "ssh"
    path.parent.mkdir()
    path.write_text(FAKE_SSH, "utf-8")
    path.chmod(stat.S_IRWXU)
    monkeypatch.setenv("PATH", f"{path.parent}:{os.environ['PATH']}")
    monkeypatch.setattr(tunnels, "PORT_RANGE", range(29700, 29710))
    return path


def _wait(predicate, seconds=8):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_tunnel_starts_and_publishes_site_proxy(fake_ssh, tmp_path):
    storage = SqlAlchemyStorage(tmp_path / "m.db")
    storage.add_site(Site(name="Балыкчы"))
    manager = tunnels.TunnelManager(storage)
    try:
        manager.set_access("Балыкчы", {"kind": "ssh", "target": "parking@10.20.4.2"})
        assert _wait(lambda: (manager.sync() or True) and
                     (manager.status("Балыкчы") or {}).get("status") == "up")
        port = manager.status("Балыкчы")["port"]
        assert port in range(29700, 29710)
        # Прокси вычисляется из живого туннеля, в базу не пишется.
        assert manager.proxy_for("Балыкчы", "") == f"socks5://127.0.0.1:{port}"
        assert storage.site("Балыкчы").proxy == ""
    finally:
        manager.stop()


def test_tunnel_failure_reports_reason(fake_ssh, tmp_path):
    storage = SqlAlchemyStorage(tmp_path / "m.db")
    storage.add_site(Site(name="Х"))
    manager = tunnels.TunnelManager(storage)
    try:
        manager.set_access("Х", {"kind": "ssh", "target": "bad@host"})
        assert _wait(lambda: (manager.sync() or True) and
                     "Permission denied" in (manager.status("Х") or {}).get("detail", ""))
        assert manager.status("Х")["status"] == "down"
    finally:
        manager.stop()


def test_switching_to_direct_stops_tunnel(fake_ssh, tmp_path):
    storage = SqlAlchemyStorage(tmp_path / "m.db")
    storage.add_site(Site(name="Б"))
    manager = tunnels.TunnelManager(storage)
    try:
        manager.set_access("Б", {"kind": "ssh", "target": "u@h"})
        assert _wait(lambda: (manager.sync() or True) and
                     (manager.status("Б") or {}).get("status") == "up")
        manager.set_access("Б", {"kind": "direct"})
        assert manager.status("Б") is None
        # Точка без туннеля ходит по записанному прокси (здесь - пустому),
        # а не по старому порту, который может достаться чужому туннелю.
        assert manager.proxy_for("Б", "") == ""
    finally:
        manager.stop()


# -- API -------------------------------------------------------------------------------

def test_stderr_flood_does_not_freeze_tunnel(fake_ssh, tmp_path):
    storage = SqlAlchemyStorage(tmp_path / "m.db")
    storage.add_site(Site(name="Ф"))
    manager = tunnels.TunnelManager(storage)
    try:
        manager.set_access("Ф", {"kind": "ssh", "target": "flood@host"})
        assert _wait(lambda: (manager.sync() or True) and
                     (manager.status("Ф") or {}).get("status") == "up")
        time.sleep(1)
        with manager._lock:
            tunnel = manager._tunnels["Ф"]
        assert tunnel.proc.poll() is None                      # жив, не встал на записи
        assert "open failed" in tunnel.errors[-1]              # stderr вычитывается
    finally:
        manager.stop()


def test_ssh_site_without_live_tunnel_gets_dead_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(tunnels, "ssh_available", lambda: False)
    storage = SqlAlchemyStorage(tmp_path / "m.db")
    storage.add_site(Site(name="С", proxy="socks5://127.0.0.1:1095"))   # старый порт
    manager = tunnels.TunnelManager(storage)
    manager.set_access("С", {"kind": "ssh", "target": "u@h"})
    assert manager.proxy_for("С", "socks5://127.0.0.1:1095") == tunnels.DEAD_PROXY


@pytest.mark.parametrize("allowed", ["10.9.9.0/24, ::/0", "0.0.0.0/1, 128.0.0.0/1", "10.0.0.0/7"])
def test_wireguard_refuses_too_wide_including_ipv6(monkeypatch, allowed):
    monkeypatch.setattr(vpn, "current_routes", lambda: [])
    with pytest.raises(ValidationError, match="интернет"):
        vpn.check_conf(CONF.format(allowed=allowed), "obj1")


def test_wireguard_name_must_be_free(monkeypatch):
    monkeypatch.setattr(vpn, "nm_available", lambda: True)
    monkeypatch.setattr(vpn, "_taken_names", lambda: {"wg0", "object1"})
    with pytest.raises(ValidationError, match="занято"):
        vpn.import_wireguard(CONF.format(allowed="10.9.9.0/24"), "object1")
    with pytest.raises(ValidationError, match="латинской"):
        vpn.import_wireguard(CONF.format(allowed="10.9.9.0/24"), "-bad")


def test_site_access_api(auth, monkeypatch):
    # Без настоящего ssh: тест не должен ходить в сеть.
    monkeypatch.setattr(tunnels, "ssh_available", lambda: False)
    auth.post("/api/v1/sites", json={"name": "Объект"})
    r = auth.put("/api/v1/sites/Объект/access", json={"kind": "ssh", "target": "user@10.0.0.1"})
    assert r.status_code == 200 and r.json()["access"]["kind"] == "ssh"
    site = auth.get("/api/v1/state").json()["sites"][0]
    assert site["access"] == "ssh"
    # Форма правки точки берёт proxy - там записанное (пусто), а не порт
    # туннеля: иначе сохранение формы записало бы порт в базу.
    assert site["proxy"] == "" and site["effective_proxy"] == tunnels.DEAD_PROXY
    assert auth.put("/api/v1/sites/Объект/access",
                    json={"kind": "ssh", "target": "нет"}).status_code == 400
    assert auth.put("/api/v1/sites/Нет/access", json={"kind": "direct"}).status_code == 404
    info = auth.get("/api/v1/access").json()
    assert "ssh" in info and "networkmanager" in info
