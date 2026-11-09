"""Фоновые задачи скана: прогресс для полосы в интерфейсе и сверка с базой.

Скан подсети через туннель идёт минуты (Балыкчы /24 - 2 мин 45 с), поэтому
HTTP-запрос его только запускает, а интерфейс опрашивает прогресс. Одна задача
на точку одновременно: два параллельных скана одной сети через один SSH-туннель
только мешали бы друг другу и давали ложные «закрыто».
"""

from __future__ import annotations

import asyncio
import ipaddress
import threading
import time
import uuid
from collections import Counter

from device_monitor.discovery.scan import DEFAULT_PORTS, ScanError, hosts_of, scan
from device_monitor.domain.errors import ValidationError

SUBNETS_KEY = "scan_subnets"
KEEP_SECONDS = 3600


def suggest_subnet(hosts: list[str]) -> str:
    """Подсеть /24, в которой больше всего уже заведённых устройств точки."""
    nets = Counter()
    for host in hosts:
        try:
            nets[str(ipaddress.ip_network(f"{host}/24", strict=False))] += 1
        except ValueError:
            continue
    return nets.most_common(1)[0][0] if nets else ""


class ScanJobs:
    def __init__(self, storage):
        self.storage = storage
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}

    def subnet_for(self, site: str) -> str:
        remembered = self.storage.get_setting(SUBNETS_KEY).get(site)
        if remembered:
            return remembered
        return suggest_subnet([d.host for d in self.storage.devices() if d.site == site])

    def start(self, site_name: str, subnet: str) -> dict:
        site = self.storage.site(site_name)
        if site is None:
            raise ValidationError("Точка не найдена")
        try:
            hosts = hosts_of(subnet)
        except ScanError as exc:
            raise ValidationError(str(exc)) from None
        with self._lock:
            self._forget_old()
            running = [j for j in self._jobs.values()
                       if j["site"] == site_name and j["status"] == "running"]
            if running:
                return self._public(running[0])
            job = {"id": uuid.uuid4().hex, "site": site_name, "subnet": subnet.strip(),
                   "status": "running", "done": 0, "total": len(hosts), "results": [],
                   "missing": [], "error": "", "started": time.time()}
            self._jobs[job["id"]] = job
            remembered = self.storage.get_setting(SUBNETS_KEY)
            remembered[site_name] = subnet.strip()
            self.storage.set_setting(SUBNETS_KEY, remembered)
        threading.Thread(target=self._run, args=(job, hosts, site.proxy),
                         name=f"scan-{site_name}", daemon=True).start()
        return self._public(job)

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return self._public(job) if job else None

    # -- работа -----------------------------------------------------------------

    def _run(self, job: dict, hosts: list[str], proxy: str) -> None:
        # Всё под try: исключение вне него оставило бы задачу «running»
        # навсегда, и новый скан этой точки до рестарта был бы невозможен.
        try:
            self._scan_and_compare(job, hosts, proxy)
        except ScanError as exc:
            job.update(status="error", error=str(exc))
        except Exception as exc:   # скан не должен ронять сервис
            job.update(status="error", error=f"скан упал: {exc}")

    def _scan_and_compare(self, job: dict, hosts: list[str], proxy: str) -> None:
        def progress(done: int, total: int) -> None:
            job["done"] = done

        network = ipaddress.ip_network(job["subnet"], strict=False)
        devices = [d for d in self.storage.devices() if d.site == job["site"]]
        # Порты уже заведённых устройств подсети - тоже в скан: иначе живое
        # устройство на нестандартном порту (8443, 5000) попало бы в «не ответили».
        extra = {p for d in devices if _in(d.host, network)
                 for p in (*d.ports, d.web_port) if p}
        ports = tuple(dict.fromkeys((*DEFAULT_PORTS, *sorted(extra))))
        found = asyncio.run(scan(hosts, proxy=proxy, ports=ports, progress=progress))

        # Сверка с базой - по адресу. Совпадение модели на другом адресе
        # «переездом» не считаем (урок 30.09): это решает человек. На одном
        # адресе может быть заведено несколько записей - показываем все.
        by_host: dict[str, list] = {}
        for d in devices:
            by_host.setdefault(d.host, []).append(d)
        for item in found:
            same = by_host.get(item["host"], [])
            item["existing"] = ({"id": same[0].id, "name": ", ".join(d.name for d in same)}
                                if same else None)
        found_hosts = {item["host"] for item in found}
        job["missing"] = [
            {"id": d.id, "name": d.name, "host": d.host}
            for d in sorted(devices, key=lambda d: d.host)
            if d.host not in found_hosts and _in(d.host, network)
        ]
        job.update(results=found, status="done", done=job["total"])

    def _forget_old(self) -> None:
        cutoff = time.time() - KEEP_SECONDS
        for job_id in [k for k, j in self._jobs.items()
                       if j["status"] != "running" and j["started"] < cutoff]:
            del self._jobs[job_id]

    @staticmethod
    def _public(job: dict) -> dict:
        return {k: v for k, v in job.items() if k != "started"}


def _in(host: str, network) -> bool:
    try:
        return ipaddress.ip_address(host) in network
    except ValueError:
        return False
