"""Проверка TCP-порта через SOCKS5-прокси, без внешних зависимостей.

Нужна для объектов, до которых нет прямого маршрута (нет VPN на этой машине):
центральный дашборд ходит к ним через постоянный SSH-туннель `ssh -D`. SOCKS5
CONNECT — это короткий бинарный хендшейк поверх обычного сокета, поэтому
PySocks не требуется: успех CONNECT и означает, что порт на устройстве открыт
с точки зрения выходного узла туннеля (то есть из сети объекта).
"""

from __future__ import annotations

import socket
import struct
import time
from urllib.parse import urlparse


class ProxyError(Exception):
    """Туннель недоступен или ответил не по протоколу — это НЕ «порт закрыт».

    Отличать обязательно: если SOCKS-прокси лёг, все устройства объекта разом
    станут недостижимы, и это надо показать как «нет связи с туннелем», а не
    как «объект целиком не отвечает».
    """


def parse_proxy(value: str) -> tuple[str, int]:
    """`socks5://127.0.0.1:1080` → (host, port). Схему допускаем только socks5."""
    parsed = urlparse(value if "://" in value else "socks5://" + value)
    if parsed.scheme not in ("socks5", "socks5h"):
        raise ProxyError(f"Поддерживается только socks5, а не {parsed.scheme!r}")
    if not parsed.hostname or not parsed.port:
        raise ProxyError("Прокси должен быть в виде socks5://host:port")
    return parsed.hostname, parsed.port


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    chunks = []
    got = 0
    while got < count:
        chunk = sock.recv(count - got)
        if not chunk:
            raise ProxyError("Прокси закрыл соединение раньше времени")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def check_port_via_socks(proxy: str, host: str, port: int, timeout: float) -> tuple[bool, float]:
    """(порт_открыт, миллисекунды). ProxyError — если недоступен сам прокси.

    Хост передаётся прокси как ДОМЕННОЕ имя (адресный тип 0x03), даже если это
    IP-строка: DNS/маршрутизацию должен делать выходной узел туннеля, изнутри
    сети объекта, а не наша машина. Для `ssh -D` это и есть штатный режим.
    """
    proxy_host, proxy_port = parse_proxy(proxy)
    started = time.perf_counter()
    sock = None
    try:
        sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
        sock.settimeout(timeout)

        # Приветствие: версия 5, один метод — без аутентификации (0x00).
        sock.sendall(b"\x05\x01\x00")
        ver, method = _recv_exactly(sock, 2)
        if ver != 0x05 or method != 0x00:
            raise ProxyError("Прокси требует аутентификацию или не SOCKS5")

        # Приветствие прошло — туннель точно жив. Всё, что случится дальше
        # (обрыв, unreachable, refused), относится уже к ЦЕЛЕВОМУ устройству,
        # а не к туннелю. OpenSSH `ssh -D` для недостижимого хоста не шлёт
        # SOCKS-ответ с кодом ошибки, а просто закрывает канал — и это надо
        # читать как «порт закрыт», а не как отказ прокси.
        target = host.encode("idna") if any(ord(c) > 127 for c in host) else host.encode("ascii")
        if len(target) > 255:
            raise ProxyError("Слишком длинное имя хоста")
        request = b"\x05\x01\x00\x03" + bytes([len(target)]) + target + struct.pack(">H", port)
        try:
            sock.sendall(request)
            ver, rep, _rsv, atyp = _recv_exactly(sock, 4)
            if ver != 0x05:
                return False, (time.perf_counter() - started) * 1000
            if atyp == 0x01:
                _recv_exactly(sock, 4 + 2)
            elif atyp == 0x03:
                _recv_exactly(sock, _recv_exactly(sock, 1)[0] + 2)
            elif atyp == 0x04:
                _recv_exactly(sock, 16 + 2)
            elapsed = (time.perf_counter() - started) * 1000
            # rep 0x00 — порт открыт; любой другой код или обрыв — закрыт.
            return (rep == 0x00), elapsed
        except (OSError, struct.error, ProxyError):
            return False, (time.perf_counter() - started) * 1000
    except ProxyError:
        raise
    except (OSError, struct.error) as exc:
        # Сюда попадают только ошибки на подключении к прокси и приветствии —
        # то есть реальный отказ туннеля.
        raise ProxyError(str(exc)) from exc
    finally:
        if sock is not None:
            sock.close()
