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

from device_monitor import config
from device_monitor.app import App, migrate_json
from device_monitor.web.server import serve


def main() -> None:
    app = App()
    migrate_json(app.storage, config.LEGACY_JSON)
    app.start()
    serve(app)


if __name__ == "__main__":
    main()
