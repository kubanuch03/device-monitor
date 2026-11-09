"""Поиск оборудования в сети объекта: кто отвечает и что это за устройство.

Скан идёт так же, как обычная проверка: напрямую (VPN) или через SOCKS точки.
Параллельно не больше `concurrency` соединений - урок 26.09: скан в 400
потоков через VPN давал ложные «закрыто» на живых портах. По той же причине
хосты, где что-то нашлось, проверяются второй раз по всем портам.

Опознание - кодом, по проверенным признакам (как в vendors/): realm
авторизации служебного адреса Dahua («Login to <хеш>») и Hikvision (realm =
модель), заголовок Server, <title>. Языковая модель здесь не нужна - она
только предлагает имена поверх (см. assist/), и ей нельзя доверять поля:
проверка на Qwen 09.11 показала расхождения типа и категории.

Ничего не добавляется само: результат - список кандидатов, решение за
человеком (в сети объекта бывают чужие устройства, а одинаковая модель на
новом адресе не доказывает «переезд» - урок 30.09).
"""

from __future__ import annotations

import asyncio
import ipaddress
import re

from device_monitor.access.relay import open_via_socks
from device_monitor.probing.socks import ProxyError, parse_proxy

DEFAULT_PORTS = (80, 443, 554, 8000, 8080, 37777, 22, 23, 10050)
WEB_PORTS = (80, 8080, 8000)
MAX_HOSTS = 1024
CONNECT_TIMEOUT = 2.5
HTTP_TIMEOUT = 4
DAHUA_PROBE = "/cgi-bin/magicBox.cgi?action=getDeviceType"
HIKVISION_PROBE = "/ISAPI/System/deviceInfo"

TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)
# Hikvision кладёт в realm модель (DS-…, iDS-…) или «IP Camera». Любой другой
# 401 по пути /ISAPI (роутер, nginx с basic-auth) - НЕ признак Hikvision.
HIKVISION_REALM_RE = re.compile(r"^(i?DS-|IP ?Camera|Hik)", re.I)
READ_LIMIT = 16384
REALM_RE = re.compile(r'realm="([^"]*)"', re.I)


class ScanError(Exception):
    pass


def hosts_of(subnet: str) -> list[str]:
    """Адреса подсети без сети и broadcast. Слишком большую не берём: /22 и
    шире через туннель сканировались бы десятки минут."""
    try:
        network = ipaddress.ip_network(subnet.strip(), strict=False)
    except ValueError:
        raise ScanError(f"«{subnet}» - не подсеть (пример: 192.168.158.0/24)") from None
    if network.version != 4:
        raise ScanError("Поддерживается только IPv4")
    if network.num_addresses > MAX_HOSTS + 2:
        raise ScanError(f"Подсеть больше /22 ({network.num_addresses} адресов) - сузьте")
    return [str(ip) for ip in (network.hosts() if network.num_addresses > 2 else network)]


async def _connect(host: str, port: int, proxy: str):
    if proxy:
        return await asyncio.wait_for(open_via_socks(proxy, host, port), CONNECT_TIMEOUT)
    return await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIMEOUT)


async def _port_open(host: str, port: int, proxy: str) -> bool:
    try:
        _, writer = await _connect(host, port, proxy)
    except (OSError, ProxyError, asyncio.TimeoutError, asyncio.IncompleteReadError, UnicodeError):
        return False
    writer.close()
    return True


async def _http_get(host: str, port: int, proxy: str, path: str) -> dict:
    """Один HTTP/1.0 GET без учётных данных: код, Server, realm, title."""
    try:
        reader, writer = await _connect(host, port, proxy)
    except (OSError, ProxyError, asyncio.TimeoutError, asyncio.IncompleteReadError, UnicodeError):
        return {}
    try:
        writer.write(f"GET {path} HTTP/1.0\r\nHost: {host}\r\nUser-Agent: device-monitor\r\n"
                     "Connection: close\r\n\r\n".encode())
        await writer.drain()

        async def read_all() -> bytes:
            # Читаем до EOF (HTTP/1.0 + Connection: close) или лимита: через
            # туннель ответ приходит кусками, и первый кусок часто без
            # WWW-Authenticate и <title> - опознание срывалось бы случайно.
            chunks, size = [], 0
            while size < READ_LIMIT and (chunk := await reader.read(READ_LIMIT - size)):
                chunks.append(chunk)
                size += len(chunk)
            return b"".join(chunks)

        raw = await asyncio.wait_for(read_all(), HTTP_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return {}
    finally:
        writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1", "replace").split("\r\n")
    if not lines or not lines[0].startswith("HTTP/"):
        return {}
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError):
        status = 0
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    realm = REALM_RE.search(headers.get("www-authenticate", ""))
    title = TITLE_RE.search(body)
    return {
        "status": status,
        "server": headers.get("server", "")[:80],
        "realm": realm.group(1)[:120] if realm else "",
        "title": title.group(1).decode("utf-8", "replace").strip()[:120] if title else "",
    }


def classify(ip: str, open_ports: list[int], web: dict) -> dict:
    """Что это за устройство - по портам и HTTP-признакам. Возвращает vendor,
    model, kind, category, name (предложение) и confidence."""
    ports = set(open_ports)
    root, dahua, hik = web.get("root", {}), web.get("dahua", {}), web.get("hikvision", {})
    server = (root.get("server") or "").lower()
    title = root.get("title") or ""
    text = f"{server} {title}".lower()
    last = ip.rsplit(".", 1)[-1]
    out = {"vendor": "", "model": "", "kind": "unknown", "category": "Прочее", "confidence": 0.3}

    if dahua.get("realm", "").startswith("Login to "):
        out.update(vendor="Dahua", fingerprint=dahua["realm"][len("Login to "):], confidence=0.9)
        if 554 in ports or 37777 in ports:
            out.update(kind="camera", category="Камеры")
    elif HIKVISION_REALM_RE.match(hik.get("realm", "")):
        model = hik["realm"]
        out.update(vendor="Hikvision", model=model, confidence=0.9)
        upper = model.upper()
        if upper.startswith("DS-3E"):
            out.update(kind="switch", category="Коммутаторы")
        elif upper.startswith("DS-K"):
            out.update(kind="intercom", category="Домофоны")
        elif upper.startswith(("DS-7", "DS-8", "DS-9")):
            out.update(kind="nvr", category="Регистраторы")
        else:
            out.update(kind="camera", category="Камеры")
    elif "app-webs" in server or "/doc/page/login.asp" in text:
        out.update(vendor="Hikvision", kind="camera", category="Камеры", confidence=0.6)

    if out["kind"] == "unknown":
        if "mikrotik" in text or "routeros" in text:
            out.update(vendor="MikroTik", kind="router", category="Роутеры", confidence=0.8)
        elif "switch" in text:
            out.update(kind="switch", category="Коммутаторы", confidence=0.6)
        elif 554 in ports or 37777 in ports:
            out.update(kind="camera", category="Камеры", confidence=0.5)
        elif 22 in ports or 10050 in ports:
            out.update(kind="server", category="Серверы", confidence=0.5)

    labels = {"camera": "Камера", "switch": "Коммутатор", "intercom": "Домофон",
              "nvr": "Регистратор", "router": "Роутер", "server": "Сервер", "unknown": "Устройство"}
    brand = f" {out['vendor']}" if out["vendor"] else ""
    out["name"] = f"{labels[out['kind']]}{brand} .{last}"
    out["title"] = title
    out["server"] = root.get("server", "")
    return out


async def scan(hosts: list[str], proxy: str = "", ports=DEFAULT_PORTS, concurrency: int = 24,
               progress=None, web_ports=WEB_PORTS) -> list[dict]:
    """Скан хостов. progress(done, total) вызывается по ходу (для полосы в UI)."""
    if proxy:
        # Сначала - жив ли сам туннель: иначе вся подсеть выглядела бы пустой.
        try:
            proxy_host, proxy_port = parse_proxy(proxy)
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(proxy_host, proxy_port), CONNECT_TIMEOUT)
            writer.close()
        except (OSError, asyncio.TimeoutError, ProxyError) as exc:
            raise ScanError(f"туннель точки недоступен: {exc}") from None

    gate = asyncio.Semaphore(concurrency)

    async def check(host: str, port: int) -> bool:
        async with gate:
            return await _port_open(host, port, proxy)

    async def get(host: str, port: int, path: str) -> dict:
        async with gate:
            return await _http_get(host, port, proxy, path)

    done = 0
    total = len(hosts)

    async def one(host: str) -> dict | None:
        nonlocal done
        found = [p for p, ok in zip(ports, await asyncio.gather(*(check(host, p) for p in ports)))
                 if ok]
        if found:
            # Второй проход по всем портам: под нагрузкой через туннель
            # единичный коннект иногда теряется, и порт выглядел бы закрытым.
            again = await asyncio.gather(*(check(host, p) for p in ports if p not in found))
            found += [p for p, ok in zip([p for p in ports if p not in found], again) if ok]
        done += 1
        if progress:
            progress(done, total)
        if not found:
            return None
        web = {}
        web_port = next((p for p in web_ports if p in found), None)
        if web_port:
            web["root"], web["dahua"], web["hikvision"] = await asyncio.gather(
                get(host, web_port, "/"), get(host, web_port, DAHUA_PROBE),
                get(host, web_port, HIKVISION_PROBE))
        return {"host": host, "ports": sorted(found), "web_port": web_port,
                **classify(host, found, web)}

    results = await asyncio.gather(*(one(h) for h in hosts))
    return sorted((r for r in results if r), key=lambda r: ipaddress.ip_address(r["host"]))
