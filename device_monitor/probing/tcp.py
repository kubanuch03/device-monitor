"""Проверка доступности TCP-коннектом.

Почему не HTTP-запросом из браузера: браузер умеет только HTTP, ничего не
помнит между открытиями страницы, не работает при закрытой вкладке и зависит
от политик безопасности. TCP-коннект свободен от всего этого и берёт любой
порт - у камеры Dahua это 80 (веб), 554 (RTSP) и 37777 (частный протокол),
и тогда ситуация «веб отвечает, а поток лежит» видна сразу.
"""

from __future__ import annotations

import socket
import time

from device_monitor.config import PROBE_TIMEOUT
from device_monitor.domain.models import Device, ProbeResult
from device_monitor.probing.socks import ProxyError, check_port_via_socks


def check_port(host: str, port: int) -> tuple[bool, float]:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT):
            pass
        return True, (time.perf_counter() - started) * 1000
    except OSError:
        return False, (time.perf_counter() - started) * 1000


def probe(device: Device, proxy: str = "") -> ProbeResult:
    """Проверка портов устройства, напрямую или через SOCKS-прокси точки.

    ProxyError (отказ туннеля) НЕ проглатывается здесь: он поднимается выше,
    в цикл опроса, чтобы отличить «туннель до объекта лёг» (тогда offline все
    устройства разом, и это надо показать отдельно) от «конкретное устройство
    не отвечает».
    """
    ports: dict[int, bool] = {}
    best: float | None = None
    for port in device.ports:
        if proxy:
            ok, elapsed = check_port_via_socks(proxy, device.host, port, PROBE_TIMEOUT)
        else:
            ok, elapsed = check_port(device.host, port)
        ports[port] = ok
        if ok and (best is None or elapsed < best):
            best = elapsed
    # Устройство живо, если открыт хотя бы один порт: закрытый RTSP при
    # работающем вебе - это неисправность службы, а не отсутствие устройства.
    return ProbeResult(
        status="up" if any(ports.values()) else "down",
        ports=ports,
        latency_ms=round(best) if best is not None else None,
    )
