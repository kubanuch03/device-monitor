#!/usr/bin/env python3
"""Точка входа Device Monitor.

Сервис следит за доступностью сетевых устройств по нескольким точкам и даёт
открыть их веб-интерфейс в один клик. Вся начинка - в пакете device_monitor,
здесь только сборка и запуск.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn

from device_monitor import config


def main() -> None:
    # Приложение (хранилище + фоновый опрос) поднимается в lifespan FastAPI —
    # см. device_monitor/web/fastapi_app.py. Здесь только запуск ASGI-сервера.
    uvicorn.run(
        "device_monitor.web.fastapi_app:app",
        host=config.HOST,
        port=config.PORT,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
