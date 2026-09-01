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

# Пароль панели. Пока он не задан, панель открыта - это осознанно: для списка
# одних лишь адресов вход был бы лишней преградой. Но как только в устройствах
# появляются учётки от железа, отсутствие пароля превращает панель в открытое
# хранилище доступов ко всему объекту, поэтому сохранение учёток без пароля
# запрещено на уровне кода, а не только в документации.
PASSWORD = os.environ.get("DM_PASSWORD", "").strip()
AUTH_ENABLED = bool(PASSWORD)
SESSION_COOKIE = "dm_session"
SESSION_TTL = max(300, int(os.environ.get("DM_SESSION_TTL", str(12 * 3600))))

MAX_BODY_BYTES = 256 * 1024
MAX_DEVICES = 2000
