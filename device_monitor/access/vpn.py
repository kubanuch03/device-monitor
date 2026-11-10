"""VPN WireGuard без терминала: импорт .conf через NetworkManager.

NetworkManager принимает импорт от пользователя без пароля (проверено 10.11:
и из сессии, и из systemd --user сервиса). Поэтому сотрудник выбирает
полученный от админа .conf в мастере - и туннель появляется в системе как
обычное VPN-подключение, поднимается при загрузке.

Защиты перед импортом (те же, что в deploy/vpn/install.sh):
  - AllowedIPs 0.0.0.0/0 - отказ: весь трафик машины ушёл бы в объект;
  - пересечение AllowedIPs с уже существующими маршрутами - отказ с
    объяснением: два объекта с одинаковыми адресами (как kk и wg0) так не
    подключить, нужен изолированный вариант (deploy/vpn/install-isolated.sh).
"""

from __future__ import annotations

import ipaddress
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from device_monitor.access.ssh_setup import child_env
from device_monitor.domain.errors import ValidationError

# Имя интерфейса Linux: до 15 символов; с буквы - не «-x» и не похоже на опцию.
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,14}$")
# Шире этого - уже не «сеть объекта», а пол-интернета (0.0.0.0/1 + 128.0.0.0/1
# - классический обход проверки на /0).
MIN_PREFIX = {4: 8, 6: 16}
TIMEOUT = 30


def nm_available() -> bool:
    return shutil.which("nmcli") is not None


def allowed_ips(conf: str) -> list:
    nets = []
    for line in conf.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() == "allowedips":
            for item in value.split(","):
                item = item.strip()
                if item:
                    try:
                        nets.append(ipaddress.ip_network(item, strict=False))
                    except ValueError:
                        raise ValidationError(f"AllowedIPs: «{item}» - не сеть") from None
    return nets


def current_routes() -> list[tuple[ipaddress.IPv4Network, str]]:
    """(сеть, интерфейс) из основной таблицы маршрутов, кроме default."""
    out = _run(["ip", "-j", "-4", "route", "show"]).stdout or "[]"
    routes = []
    for route in json.loads(out):
        dst = route.get("dst", "")
        if dst and dst != "default":
            try:
                routes.append((ipaddress.ip_network(dst, strict=False), route.get("dev", "")))
            except ValueError:
                continue
    return routes


def check_conf(conf: str, name: str) -> list[ipaddress.IPv4Network]:
    if "[Interface]" not in conf or "[Peer]" not in conf:
        raise ValidationError("Это не конфиг WireGuard: нужны секции [Interface] и [Peer]")
    nets = allowed_ips(conf)
    if not nets:
        raise ValidationError("В конфиге нет AllowedIPs - непонятно, какие сети за VPN")
    too_wide = [n for n in nets if n.prefixlen < MIN_PREFIX[n.version]]
    if too_wide:
        raise ValidationError(f"AllowedIPs {', '.join(map(str, too_wide))} - весь интернет этой "
                              "машины (или его половина) ушёл бы в объект. Попросите у админа "
                              "конфиг только с сетями объекта.")
    for net in (n for n in nets if n.version == 4):
        for route, dev in current_routes():
            if dev != name and net.overlaps(route) and route.prefixlen >= 8:
                raise ValidationError(
                    f"Сеть {net} уже занята другим подключением ({dev}: {route}). Два объекта "
                    "с одинаковыми адресами так не подключить - нужен изолированный VPN "
                    "(deploy/vpn/install-isolated.sh, см. инструкцию).")
    return nets


def _taken_names() -> set[str]:
    connections = _nmcli("-g", "NAME", "connection", "show").stdout.split("\n")
    links = json.loads(_run(["ip", "-j", "link", "show"]).stdout or "[]")
    return {c.strip() for c in connections if c.strip()} | {l.get("ifname", "") for l in links}


def _run(command: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=True, text=True,
                              timeout=TIMEOUT, env=child_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValidationError(f"{command[0]} не запустился: {type(exc).__name__}") from None


def _nmcli(*args: str) -> subprocess.CompletedProcess:
    return _run(["nmcli", *args])


def import_wireguard(conf: str, name: str) -> dict:
    """Импорт и подъём. Имя = имя интерфейса и подключения в NetworkManager."""
    if not nm_available():
        raise ValidationError("Нет NetworkManager (сервис в Docker?) - импорт недоступен")
    if not NAME_RE.fullmatch(name or ""):
        raise ValidationError("Имя VPN - с латинской буквы, латиница/цифры/-/_, до 15 символов "
                              "(пример: karakol)")
    # Занятое имя - не перезапись: второй VPN с тем же именем забрал бы
    # интерфейс у первого, и тот объект молча отвалился бы.
    if name in _taken_names():
        raise ValidationError(f"Имя «{name}» уже занято другим подключением - выберите другое")
    nets = check_conf(conf, name)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{name}.conf"   # NetworkManager берёт имя из имени файла
        path.write_text(conf, "utf-8")
        path.chmod(0o600)
        done = _nmcli("connection", "import", "type", "wireguard", "file", str(path))
    if done.returncode != 0:
        raise ValidationError(f"NetworkManager не принял конфиг: {done.stderr.strip()[:200]}")
    # DNS объекта не применяем и маршрутом по умолчанию VPN не делаем: иначе
    # админ объекта отвечал бы за имена всей машины, а без связи с объектом
    # тормозил бы весь интернет.
    modified = _nmcli("connection", "modify", "id", name,
                      "ipv4.ignore-auto-dns", "yes", "ipv6.ignore-auto-dns", "yes",
                      "ipv4.dns", "", "ipv6.dns", "",
                      "ipv4.never-default", "yes", "ipv6.never-default", "yes")
    if modified.returncode != 0:
        # Без этой настройки VPN поднялся бы с DNS объекта - не поднимаем вовсе.
        _nmcli("connection", "delete", "id", name)
        raise ValidationError(f"Не удалось отключить DNS объекта для VPN: "
                              f"{modified.stderr.strip()[:200]}")
    up = _nmcli("connection", "up", "id", name)
    if up.returncode != 0:
        # Не оставляем полуживое подключение с автозапуском - повтор мастера
        # иначе плодил бы дубли.
        _nmcli("connection", "delete", "id", name)
        raise ValidationError(f"VPN не поднялся и удалён: {(up.stderr or up.stdout).strip()[:200]}")
    return {"ok": True, "name": name, "networks": [str(n) for n in nets],
            "detail": up.stdout.strip()[:200]}


def wireguard_connections() -> list[dict]:
    if not nm_available():
        return []
    out = _nmcli("-t", "-f", "NAME,TYPE,DEVICE", "connection", "show").stdout
    result = []
    for line in out.splitlines():
        name, kind, device = (line.split(":") + ["", "", ""])[:3]
        if kind == "wireguard":
            result.append({"name": name, "active": bool(device)})
    return result
