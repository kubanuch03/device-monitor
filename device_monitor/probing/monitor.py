"""Фоновый цикл: опрос доступности, запись истории, сбор сведений.

Три задачи с разной ценой намеренно разведены по частоте:
  - доступность    - один TCP-коннект, каждые POLL_INTERVAL секунд;
  - история        - только при СМЕНЕ состояния, иначе таблица растёт впустую;
  - сведения       - несколько HTTP-запросов с авторизацией, раз в INSPECT_TTL.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from device_monitor.config import HISTORY_DAYS, INSPECT_TTL, MAX_WORKERS, POLL_INTERVAL
from device_monitor.domain.credentials import effective
from device_monitor.domain.models import Device
from device_monitor.probing.socks import ProxyError
from device_monitor.probing.tcp import probe
from device_monitor.probing.webauth import probe_web_auth
from device_monitor.security import deobfuscate
from device_monitor.vendors import identify


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Monitor:
    def __init__(self, storage):
        self.storage = storage
        self._lock = threading.Lock()
        self._state: dict[str, dict] = {}
        self._facts: dict[str, dict] = {}
        self._forced: set[str] = set()
        self._tunnels: dict[str, dict] = {}
        self._updated_at: str | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._last_prune = 0.0

    # -- состояние -------------------------------------------------------------

    def snapshot(self) -> tuple[dict[str, dict], str | None]:
        with self._lock:
            return {k: dict(v) for k, v in self._state.items()}, self._updated_at

    def tunnels(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._tunnels.items()}

    def facts(self) -> dict[str, dict]:
        with self._lock:
            return {
                k: {n: v for n, v in f.items() if not n.startswith("_")}
                for k, f in self._facts.items()
            }

    def request_run(self) -> None:
        self._wake.set()

    def request_inspect(self, device_id: str) -> None:
        with self._lock:
            self._forced.add(device_id)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # -- круг опроса ------------------------------------------------------------

    def run_once(self) -> None:
        devices = self.storage.devices()
        proxies = {s.name: s.proxy for s in self.storage.sites()}
        if not devices:
            with self._lock:
                self._state = {}
                self._updated_at = now_iso()
            return

        # Отказ туннеля точки помечается один раз на точку, а не считается
        # «падением» каждого её устройства: иначе один упавший ssh -D выглядел
        # бы как одновременная смерть десятка камер.
        tunnels: dict[str, dict] = {}

        def run(device):
            proxy = proxies.get(device.site, "")
            try:
                return device, probe(device, proxy), None
            except ProxyError as exc:
                return device, None, str(exc)

        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(devices))) as pool:
            probed = list(pool.map(run, devices))

        checked_at = now_iso()
        results = []
        for device, result, proxy_error in probed:
            if proxy_error is not None:
                tunnels.setdefault(device.site, {"status": "down", "detail": proxy_error})
                from device_monitor.domain.models import ProbeResult
                result = ProbeResult(status="down", ports={p: False for p in device.ports})
            elif proxies.get(device.site):
                tunnels.setdefault(device.site, {"status": "up", "detail": None})
            results.append(result)
        transitions: list[tuple[str, str]] = []

        with self._lock:
            fresh: dict[str, dict] = {}
            for device, result in zip(devices, results):  # порядок сохранён
                previous = self._state.get(device.id)
                entry = {
                    "status": result.status,
                    "ports": {str(p): v for p, v in result.ports.items()},
                    "latency_ms": result.latency_ms,
                    "checked_at": checked_at,
                }
                # since сбрасывается только при смене статуса - иначе «лежит
                # сорок минут» превращалось бы в «лежит двадцать секунд» после
                # каждого круга и теряло весь смысл.
                if previous and previous["status"] == result.status:
                    entry["since"] = previous["since"]
                else:
                    entry["since"] = checked_at
                    transitions.append((device.id, result.status))
                fresh[device.id] = entry
            self._state = fresh
            self._tunnels = tunnels
            self._updated_at = checked_at

        for device_id, status in transitions:
            # При первом круге после запуска состояние в памяти пустое, и всё
            # выглядит как смена. Чтобы не плодить фантомные события, сверяемся
            # с последним, что записано в базе.
            if self.storage.last_status(device_id) != status:
                self.storage.record_status(device_id, status, checked_at)

        self._refresh_facts(devices)
        self._prune()

    def _prune(self) -> None:
        if time.time() - self._last_prune < 6 * 3600:
            return
        self._last_prune = time.time()
        self.storage.prune_history(HISTORY_DAYS)

    # -- сведения ---------------------------------------------------------------

    def _refresh_facts(self, devices: list[Device]) -> None:
        now = time.time()
        # Сбор сведений идёт по HTTP через urllib, который SOCKS не умеет,
        # поэтому проксированные точки пока пропускаем (доступность у них
        # работает, а сведения от железа - следующий этап).
        proxied = {s.name for s in self.storage.sites() if s.proxy}
        with self._lock:
            forced = set(self._forced)
            self._forced.clear()
            stale = [
                d for d in devices
                if d.id in forced or now - self._facts.get(d.id, {}).get("_at", 0) > INSPECT_TTL
            ]
        # Вендорный опрос идёт по настоящему адресу устройства через urllib, а
        # он SOCKS не умеет - для точек за туннелем его по-прежнему пропускаем.
        # А вот схему авторизации спрашивать можно и там, где есть проброшенный
        # порт: он локальный, и именно он откроется кнопкой.
        # Учётку разрешаем здесь, а не в _inspect: правило «своя или точки»
        # должно быть одно на весь сервис, и живёт оно в domain/credentials.py.
        sites = {s.name: s for s in self.storage.sites()}
        jobs = [(d, d.site not in proxied, *effective(d, sites.get(d.site)))
                for d in stale if d.site not in proxied or d.open_url]
        if not jobs:
            return

        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(jobs))) as pool:
            for device_id, facts in pool.map(self._inspect, jobs):
                with self._lock:
                    self._facts[device_id] = facts

    @staticmethod
    def _inspect(job: tuple[Device, bool, str, str]) -> tuple[str, dict]:
        device, vendor_allowed, username, password_enc = job
        facts: dict = {"checked_at": now_iso(), "_at": time.time()}
        try:
            # Спрашиваем это до вендорного опроса и независимо от учётки:
            # ответ нужен даже для устройств, пароль от которых не заведён —
            # именно по нему видно, чем кнопка «Открыть» сможет помочь.
            facts.update(probe_web_auth(device))
            if not vendor_allowed:
                return device.id, {k: v for k, v in facts.items() if v}
            driver, detected = identify(device)
            facts.update(detected)
            if driver is None:
                facts["detail"] = "производитель не определён"
            elif not username or not password_enc:
                facts["detail"] = "нет учётных данных"
            else:
                # Драйвер берёт логин из самого устройства, поэтому при
                # наследовании подменяем его на учётку точки - иначе пошли бы
                # с чужим логином и своим паролем, то есть ни с чем.
                facts.update(driver.collect(device.with_fields(username=username),
                                            deobfuscate(password_enc)))
        except Exception as exc:
            facts["detail"] = f"не удалось опросить: {exc}"
        return device.id, {k: v for k, v in facts.items() if v}

    # -- цикл --------------------------------------------------------------------

    def loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                print(f"[monitor] круг опроса упал: {exc}", flush=True)
            self._wake.wait(POLL_INTERVAL)
            self._wake.clear()
