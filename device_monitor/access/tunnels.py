"""SSH-туннели до объектов, которые держит сам сервис - без sudo и юнитов.

Раньше объект «за SSH-сервером» (Балыкчы) подключался так: sudo
deploy/setup-tunnel.sh → systemd-юнит autossh -D → порт SOCKS вписывался в
точку руками. Теперь в точке достаточно указать «доступ через SSH: user@host»:
сервис сам запускает `ssh -N -D 127.0.0.1:<порт>`, следит за ним и
перезапускает при обрыве.

Прокси такой точки НЕ записывается в базу, а вычисляется при каждом
использовании (proxy_for): записанный порт пережил бы смену доступа и достался
бы туннелю другого объекта - а подсети у объектов совпадают, и проверки точки A
тихо шли бы в сеть объекта B. Пока туннель не поднят, прокси указывает на
закрытый порт - точка честно показывает «нет связи с туннелем».

Вход - только по собственному ключу сервиса (access/ssh_setup.py), без
~/.ssh/config пользователя: его ProxyCommand/ControlMaster не должны влиять на
туннели сервиса. Работает в установке из .deb; в Docker-контейнере нет ни ssh,
ни NetworkManager - там туннель держит systemd-юнит на хосте.
"""

from __future__ import annotations

import collections
import shutil
import socket
import subprocess
import threading
import time

from device_monitor.access import ssh_setup

ACCESS_KEY = "site_access"
PORT_RANGE = range(1090, 1190)
BACKOFF = (5, 10, 30, 60)
DEAD_PROXY = "socks5://127.0.0.1:9"   # закрытый порт: «туннеля нет» = ProxyError


def ssh_available() -> bool:
    return shutil.which("ssh") is not None


def _port_free(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


class _Tunnel:
    def __init__(self, target: str, port: int):
        self.target = target
        self.port = port
        self.proc: subprocess.Popen | None = None
        self.status = "starting"     # starting | up | down
        self.detail = ""
        self.failures = 0
        self.next_try = 0.0
        # Хвост stderr ssh. Читается отдельным потоком всё время работы: ssh -D
        # пишет строку на каждое неудачное соединение через туннель, и
        # непрочитанный пайп (64 КБ) остановил бы ssh - туннель «жив», но мёртв.
        self.errors: collections.deque[str] = collections.deque(maxlen=20)

    def command(self) -> list[str]:
        host, port_args = ssh_setup.clean_target(self.target)
        return ["ssh", *ssh_setup.ssh_options(), *port_args,
                "-N", "-D", f"127.0.0.1:{self.port}",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                "--", host]

    def start(self) -> None:
        self.errors.clear()
        self.proc = subprocess.Popen(self.command(), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     env=ssh_setup.child_env())
        stream, errors = self.proc.stderr, self.errors

        def drain():
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    errors.append(line[:200])
        threading.Thread(target=drain, name="ssh-stderr", daemon=True).start()
        self.status = "starting"

    def listening(self) -> bool:
        """Слушает ли порт именно наш ssh, а не чужой процесс."""
        if self.proc is None:
            return False
        try:
            out = subprocess.run(["ss", "-Hltnp", f"sport = :{self.port}"],
                                 capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return not _port_free(self.port)
        return f"pid={self.proc.pid}," in out


class TunnelManager:
    """Держит по одному ssh -D на точку с доступом «ssh». Один фоновый поток
    раз в несколько секунд сверяет желаемое (настройки) с запущенным."""

    def __init__(self, storage):
        self.storage = storage
        self._tunnels: dict[str, _Tunnel] = {}
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- настройки доступа точки -----------------------------------------------------

    def access(self, site: str) -> dict:
        return self.storage.get_setting(ACCESS_KEY).get(site) or {"kind": "direct"}

    def set_access(self, site: str, access: dict) -> None:
        settings = self.storage.get_setting(ACCESS_KEY)
        kind = access.get("kind")
        if kind == "ssh":
            settings[site] = {"kind": "ssh", "target": access["target"]}
        elif kind == "socks":
            settings[site] = {"kind": "socks"}
        else:
            settings.pop(site, None)
        self.storage.set_setting(ACCESS_KEY, settings)
        self.sync()

    def rename(self, old: str, new: str) -> None:
        settings = self.storage.get_setting(ACCESS_KEY)
        if old in settings:
            settings[new] = settings.pop(old)
            self.storage.set_setting(ACCESS_KEY, settings)
        with self._lock:
            if old in self._tunnels:
                self._tunnels[new] = self._tunnels.pop(old)

    def proxy_for(self, site: str, stored: str) -> str:
        """Прокси, через который сейчас ходить к точке. Для SSH-точки - порт её
        живого туннеля, иначе заведомо мёртвый адрес (а не старый порт, который
        мог достаться чужому туннелю). Для остальных - то, что записано."""
        if self.access(site).get("kind") != "ssh":
            return stored
        with self._lock:
            tunnel = self._tunnels.get(site)
            if tunnel is not None and tunnel.status == "up":
                return f"socks5://127.0.0.1:{tunnel.port}"
        return DEAD_PROXY

    def status(self, site: str) -> dict | None:
        with self._lock:
            tunnel = self._tunnels.get(site)
            if tunnel is None:
                return None
            return {"status": tunnel.status, "detail": tunnel.detail, "port": tunnel.port,
                    "target": tunnel.target}

    # -- жизненный цикл ---------------------------------------------------------------

    def start(self) -> None:
        if not ssh_available():
            return
        self._thread = threading.Thread(target=self._loop, name="tunnels", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # Ждём текущую сверку: иначе она могла бы запустить ssh уже после того,
        # как мы всё остановили, и процесс пережил бы сервис.
        with self._sync_lock:
            with self._lock:
                tunnels = list(self._tunnels.values())
            for tunnel in tunnels:
                self._kill(tunnel)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync()
            except Exception as exc:   # туннели не должны ронять сервис
                print(f"[tunnels] {exc}", flush=True)
            self._stop.wait(3)

    @staticmethod
    def _kill(tunnel: _Tunnel) -> None:
        if tunnel.proc and tunnel.proc.poll() is None:
            tunnel.proc.terminate()
            try:
                tunnel.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                tunnel.proc.kill()
                tunnel.proc.wait()

    # -- сверка ------------------------------------------------------------------------

    def sync(self) -> None:
        # Сверка из фонового потока и из запроса API не идут одновременно.
        with self._sync_lock:
            self._sync()

    def _sync(self) -> None:
        wanted = {site: cfg["target"] for site, cfg in self.storage.get_setting(ACCESS_KEY).items()
                  if isinstance(cfg, dict) and cfg.get("kind") == "ssh" and cfg.get("target")}
        stale = []
        with self._lock:
            for site in list(self._tunnels):
                if site not in wanted or self._tunnels[site].target != wanted[site]:
                    stale.append(self._tunnels.pop(site))
            for site, target in wanted.items():
                if site not in self._tunnels:
                    port = self._free_port()
                    if port is not None:
                        self._tunnels[site] = _Tunnel(target, port)
            current = list(self._tunnels.values())
        # Остановка и запуск процессов - вне блокировки: /state читает статус
        # под ней, и ждать секунды завершения ssh ему незачем.
        for tunnel in stale:
            self._kill(tunnel)
        for tunnel in current:
            self._tend(tunnel)

    def _free_port(self) -> int | None:
        used = {t.port for t in self._tunnels.values()}
        return next((p for p in PORT_RANGE if p not in used and _port_free(p)), None)

    def _tend(self, tunnel: _Tunnel) -> None:
        if not ssh_available():
            tunnel.status, tunnel.detail = "down", "нет программы ssh (сервис в Docker?)"
            return
        running = tunnel.proc is not None and tunnel.proc.poll() is None
        if running:
            if tunnel.status != "up" and tunnel.listening():
                tunnel.status, tunnel.detail, tunnel.failures = "up", "", 0
            return
        if tunnel.proc is not None:
            tunnel.proc.wait()
            time.sleep(0.05)   # поток чтения успевает дочитать последнюю строку
            tunnel.detail = (tunnel.errors[-1] if tunnel.errors
                             else f"ssh завершился ({tunnel.proc.returncode})")
            tunnel.status = "down"
            tunnel.failures += 1
            tunnel.next_try = time.time() + BACKOFF[min(tunnel.failures, len(BACKOFF)) - 1]
            tunnel.proc = None
            # Порт мог занять чужой процесс (ExitOnForwardFailure) - берём другой.
            if not _port_free(tunnel.port):
                with self._lock:
                    port = self._free_port()
                if port is not None:
                    tunnel.port = port
        if time.time() < tunnel.next_try or self._stop.is_set():
            return
        tunnel.start()
