"""Общая настройка тестов.

Переменные окружения выставляются ДО импорта device_monitor (config читает их
на уровне модуля): известная дефолтная учётка, редкий опрос и его отключение.
База изолируется НА КАЖДЫЙ тест (client fixture подменяет config.DB_PATH на
свой временный файл) — иначе тест смены пароля ломал бы остальные.
"""

import os
import tempfile

os.environ["DM_DATA_DIR"] = tempfile.mkdtemp(prefix="dm-test-")
os.environ["DM_DEFAULT_USERNAME"] = "admin"
os.environ["DM_DEFAULT_PASSWORD"] = "test-secret"
os.environ["DM_POLL_INTERVAL"] = "3600"
os.environ["DM_DISABLE_MONITOR"] = "1"

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    from device_monitor import config
    config.DB_PATH = tmp_path / "monitor.db"  # своя БД на каждый тест
    from device_monitor.web.fastapi_app import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth(client):
    r = client.post("/api/v1/login", json={"username": "admin", "password": "test-secret"})
    assert r.status_code == 200
    return client


@pytest.fixture(autouse=True)
def _fresh_security_state():
    """Счётчик неудачных входов и сессии - глобальные объекты процесса;
    без сброса неудачи одного теста закрывали бы вход в следующем."""
    from device_monitor.security import SESSIONS, THROTTLE
    THROTTLE._fails.clear()
    SESSIONS._tokens.clear()
    yield
