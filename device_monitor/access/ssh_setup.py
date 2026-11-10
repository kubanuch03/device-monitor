"""Вход по ключу на SSH-сервер объекта: свой ключ сервиса и разовая установка.

Туннель (tunnels.py) работает только по ключу - пароль сервис не хранит. Чтобы
сотруднику не нужен был терминал, мастер подключения ставит ключ сам: пароль
сервера вводится один раз, уходит в ssh через SSH_ASKPASS (не в аргументы
процесса - их видно в ps) и нигде не сохраняется.

Ключ - собственный ключ сервиса в каталоге данных (data/ssh/), а не личный
~/.ssh/id_ed25519 пользователя: личный ключ может быть с паролем (тогда туннель
без человека не поднимется) и не должен разноситься по серверам объектов.
По той же причине ~/.ssh/config пользователя не читается (-F /dev/null) и
known_hosts свой.

Риски, принятые осознанно:
  - ключ без пароля: украл файл - попал на серверы объектов. Защита - права
    700/600 на каталог данных, как у базы с паролями камер;
  - первый вход принимает ключ хоста без подтверждения (accept-new): подмена
    сервера при ПЕРВОМ подключении перехватила бы пароль.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from device_monitor import config
from device_monitor.domain.errors import ValidationError

# user@host[:port]; ни user, ни host не начинаются с «-» - иначе это опция ssh
# (-J, -F, -E ...), а не адрес.
TARGET_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9.-]*(:\d{1,5})?$")
TIMEOUT = 30


def _dir() -> Path:
    path = config.DATA_DIR / "ssh"
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def key_path() -> Path:
    return _dir() / "id_ed25519"


def ssh_options() -> list[str]:
    """Общие опции: свой ключ, свой known_hosts, без конфига пользователя."""
    return ["-F", "/dev/null", "-i", str(key_path()), "-o", "IdentitiesOnly=yes",
            "-o", f"UserKnownHostsFile={_dir() / 'known_hosts'}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


def child_env(extra: dict | None = None) -> dict:
    """Окружение для ssh/nmcli/ip. В сборке PyInstaller (.deb) загрузчик
    подставляет свои libssl/libcrypto через LD_LIBRARY_PATH - системные
    программы с ними могут упасть. Возвращаем исходное значение."""
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        original = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if original:
            env["LD_LIBRARY_PATH"] = original
        else:
            env.pop("LD_LIBRARY_PATH", None)
    env.update(extra or {})
    return env


def clean_target(target: str) -> tuple[str, list[str]]:
    """user@host[:port] -> (user@host, аргументы порта для ssh)."""
    target = (target or "").strip()
    if not TARGET_RE.fullmatch(target):
        raise ValidationError("Адрес SSH-сервера - в виде user@host или user@host:порт")
    if ":" in target:
        target, port = target.rsplit(":", 1)
        if not 1 <= int(port) <= 65535:
            raise ValidationError("Порт SSH - от 1 до 65535")
        return target, ["-p", port]
    return target, []


def public_key() -> str:
    """Публичный ключ сервиса; нет - создаём (без пароля: туннель
    поднимается без человека)."""
    key = key_path()
    if not key.exists():
        try:
            subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-C",
                            "device-monitor", "-f", str(key)],
                           check=True, timeout=TIMEOUT, env=child_env(),
                           capture_output=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValidationError(f"Не удалось создать ключ SSH: {type(exc).__name__}") from None
    try:
        return key.with_suffix(".pub").read_text("utf-8").strip()
    except OSError:
        raise ValidationError(f"Нет открытой части ключа {key}.pub - удалите {key}, "
                              "и ключ создастся заново") from None


def test_login(target: str) -> dict:
    """Входит ли сервис на сервер по своему ключу (без пароля)."""
    host, port_args = clean_target(target)
    public_key()
    try:
        result = subprocess.run(["ssh", *ssh_options(), *port_args, "--", host, "true"],
                                capture_output=True, text=True, timeout=TIMEOUT, env=child_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValidationError(f"ssh не запустился: {type(exc).__name__}") from None
    if result.returncode == 0:
        return {"ok": True, "detail": "вход по ключу работает"}
    error = (result.stderr.strip().splitlines() or [""])[-1][:200]
    hint = ""
    if "Permission denied" in error:
        hint = "ключ не принят - установите его паролем сервера"
    elif "Could not resolve" in error or "No route" in error or "timed out" in error:
        hint = "сервер недоступен - проверьте VPN/адрес"
    return {"ok": False, "detail": error, "hint": hint}


def install_key(target: str, password: str) -> dict:
    """Ставит ключ сервиса на сервер паролем, введённым один раз."""
    if not password:
        raise ValidationError("Нужен пароль SSH-сервера (один раз, не сохраняется)")
    host, port_args = clean_target(target)
    public_key()
    # Каталог для askpass - рядом с ключом, а не в /tmp: /tmp бывает
    # смонтирован с noexec, и скрипт пароля просто не запустился бы.
    with tempfile.TemporaryDirectory(dir=_dir()) as tmp:
        askpass = Path(tmp) / "askpass"
        # Пароль читается askpass'ом из окружения процесса ssh, а не из файла и
        # не из аргументов: так он не попадает ни на диск, ни в ps.
        askpass.write_text('#!/bin/sh\nprintf "%s\\n" "$DM_SSH_PASSWORD"\n', "utf-8")
        askpass.chmod(stat.S_IRWXU)
        env = child_env({"SSH_ASKPASS": str(askpass), "SSH_ASKPASS_REQUIRE": "force",
                         "DM_SSH_PASSWORD": password,
                         "DISPLAY": os.environ.get("DISPLAY", ":0")})
        # Одна попытка пароля: три неверных подряд - бан fail2ban на сервере.
        command = ["ssh-copy-id", "-i", str(key_path().with_suffix(".pub")), *port_args,
                   "-F", "/dev/null",
                   "-o", f"UserKnownHostsFile={_dir() / 'known_hosts'}",
                   "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
                   "-o", "NumberOfPasswordPrompts=1", "-o", "PubkeyAuthentication=no",
                   "--", host]
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, env=env, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValidationError(f"ssh-copy-id не запустился: {type(exc).__name__}") from None
    if result.returncode != 0:
        lines = (result.stderr or result.stdout).strip().splitlines()
        raise ValidationError(f"Ключ не установлен: {(lines or ['ошибка'])[-1][:200]}")
    return test_login(target)
