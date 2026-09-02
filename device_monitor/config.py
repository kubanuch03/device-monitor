"""Настройки сервиса. Единственное место, которое читает окружение.

Остальной код получает готовые значения отсюда и не знает про os.environ -
иначе переменные окружения расползаются по модулям и понять, чем сервис
настраивается, можно только грепом по всему проекту.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

DATA_DIR = Path(os.environ.get("DM_DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.environ.get("DM_DB_FILE", DATA_DIR / "monitor.db"))
LEGACY_JSON = Path(os.environ.get("DM_DATA_FILE", DATA_DIR / "devices.json"))

HOST = os.environ.get("DM_HOST", "127.0.0.1")
PORT = int(os.environ.get("DM_PORT", "8890"))

POLL_INTERVAL = max(5, int(os.environ.get("DM_POLL_INTERVAL", "20")))
PROBE_TIMEOUT = max(1.0, float(os.environ.get("DM_PROBE_TIMEOUT", "3")))
MAX_WORKERS = max(4, int(os.environ.get("DM_WORKERS", "32")))

INSPECT_TIMEOUT = max(1.0, float(os.environ.get("DM_INSPECT_TIMEOUT", "6")))
INSPECT_TTL = max(60, int(os.environ.get("DM_INSPECT_TTL", "900")))

HISTORY_DAYS = max(1, int(os.environ.get("DM_HISTORY_DAYS", "90")))

# Фоновый опрос можно отключить (тесты): тогда сервис поднимается без сетевых
# проверок. В проде всегда включён.
MONITOR_ENABLED = os.environ.get("DM_DISABLE_MONITOR", "") != "1"

# Учётная запись входа хранится в БД (хешем). При первом запуске, когда
# пользователей ещё нет, создаётся дефолтная — логин/пароль ниже. Их МОЖНО и
# НУЖНО переопределить через env (особенно пароль: репозиторий публичный, и
# зашитый дефолт секретом не является — сменить после первого входа).
DEFAULT_USERNAME = os.environ.get("DM_DEFAULT_USERNAME", "admin").strip() or "admin"
DEFAULT_PASSWORD = os.environ.get("DM_DEFAULT_PASSWORD", "S347367j")

# Вход в панель обязателен всегда — она хранит доступы к оборудованию.
AUTH_ENABLED = True
SESSION_COOKIE = "dm_session"
SESSION_TTL = max(300, int(os.environ.get("DM_SESSION_TTL", str(12 * 3600))))

MAX_BODY_BYTES = 256 * 1024
MAX_DEVICES = 2000
