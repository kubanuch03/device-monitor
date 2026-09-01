"""Сборка приложения: хранилище + фоновый монитор, плюс перенос старых данных."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from device_monitor import config
from device_monitor.domain.models import Device
from device_monitor.probing.monitor import Monitor
from device_monitor.storage.sqlite import SqliteStorage


class App:
    def __init__(self, storage=None):
        self.storage = storage or SqliteStorage(config.DB_PATH)
        self.monitor = Monitor(self.storage)

    def start(self) -> None:
        threading.Thread(target=self.monitor.loop, name="monitor", daemon=True).start()

    def stop(self) -> None:
        self.monitor.stop()


def migrate_json(storage, path: Path) -> int:
    """Переносит devices.json в базу и помечает файл перенесённым.

    Файл не удаляется, а переименовывается: если миграция что-то поняла не так,
    исходные данные должны остаться под рукой, а не исчезнуть безвозвратно.
    """
    if not path.exists():
        return 0
    if storage.devices() or storage.sites():
        return 0

    try:
        raw = json.loads(path.read_text("utf-8") or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[migrate] не смог прочитать {path}: {exc}", flush=True)
        return 0

    for site in raw.get("sites") or []:
        if isinstance(site, dict) and site.get("name"):
            try:
                storage.add_site(str(site["name"]), str(site.get("note") or ""))
            except Exception:
                pass
    for category in raw.get("categories") or []:
        if isinstance(category, dict) and category.get("site") and category.get("name"):
            try:
                storage.add_category(
                    str(category["site"]), str(category["name"]), str(category.get("note") or "")
                )
            except Exception:
                pass

    moved = 0
    for item in raw.get("devices") or []:
        if not isinstance(item, dict) or not item.get("host"):
            continue
        try:
            storage.add_device(
                Device(
                    id=str(item.get("id") or ""),
                    site=str(item.get("site") or "Без объекта"),
                    category=str(item.get("category") or ""),
                    name=str(item.get("name") or item["host"]),
                    host=str(item["host"]),
                    ports=tuple(item.get("ports") or (80,)),
                    scheme=str(item.get("scheme") or "http"),
                    web_port=item.get("web_port"),
                    path=str(item.get("path") or "/"),
                    username=str(item.get("username") or ""),
                    password_enc=str(item.get("password_enc") or ""),
                    note=str(item.get("note") or ""),
                )
            )
            moved += 1
        except Exception as exc:
            print(f"[migrate] пропущено устройство {item.get('host')}: {exc}", flush=True)

    path.rename(path.with_suffix(path.suffix + ".migrated"))
    print(f"[migrate] перенесено устройств: {moved}, файл сохранён как {path.name}.migrated", flush=True)
    return moved
