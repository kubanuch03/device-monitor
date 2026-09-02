"""Общая настройка тестов.

Переменные окружения выставляются ДО импорта device_monitor (config читает их
на уровне модуля): изолированная БД во временном каталоге, известный пароль
панели, редкий опрос — чтобы фоновый монитор не мешал и не ходил в сеть.
"""

import os
import tempfile

os.environ["DM_DATA_DIR"] = tempfile.mkdtemp(prefix="dm-test-")
os.environ["DM_PASSWORD"] = "test-secret"
os.environ["DM_POLL_INTERVAL"] = "3600"
os.environ["DM_DISABLE_MONITOR"] = "1"  # тестам сетевой опрос не нужен

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    # Импорт внутри фикстуры — после того как окружение уже выставлено.
    from device_monitor.web.fastapi_app import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth(client):
    r = client.post("/api/v1/login", json={"password": "test-secret"})
    assert r.status_code == 200
    return client
