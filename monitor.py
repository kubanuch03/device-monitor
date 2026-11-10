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
    # Объект, а не строка "модуль:app": так импорт виден статически, и
    # упаковка в один исполняемый файл (PyInstaller, .deb) его не теряет.
    from device_monitor.web.fastapi_app import app

    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        log_level="warning",
        # X-Forwarded-For не доверяем. По умолчанию uvicorn верит ему от
        # 127.0.0.1, а панель слушает именно 127.0.0.1 - значит, любой
        # клиент подставлял бы свой адрес и обходил ограничение перебора
        # пароля. Настоящего прокси перед панелью нет; появится - тогда
        # включать вместе с явным forwarded_allow_ips.
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
