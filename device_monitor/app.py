"""Сборка приложения: хранилище + фоновый монитор, плюс перенос старых данных."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from device_monitor import config
from device_monitor.access.relay import RelayManager
from device_monitor.alerts.service import AlertService
from device_monitor.domain.models import Device, Site
from device_monitor.probing.monitor import Monitor
from device_monitor.security import hash_password
from device_monitor.storage.sqlalchemy_store import SqlAlchemyStorage


class App:
    def __init__(self, storage=None):
        self.storage = storage or SqlAlchemyStorage(config.DB_PATH)
        self.alerts = AlertService(self.storage, run_sender=config.MONITOR_ENABLED)
        self.relay = RelayManager(self.storage)
        self.monitor = Monitor(self.storage, self.alerts, self.relay)
        self._ensure_default_user()

    def _ensure_default_user(self) -> None:
        """При первом запуске создаёт учётку входа.

        Пароль берётся из DM_DEFAULT_PASSWORD, а без него генерируется
        случайный. Сгенерированный пароль надо как-то показать человеку, который
        только что поставил сервис, - он кладётся в data/first-login.txt (600),
        а в лог пишется только путь к файлу. Файл удаляется при первой смене пароля
        через «Аккаунт».
        """
        if self.storage.get_user() is not None:
            return
        password = config.DEFAULT_PASSWORD
        if not password:
            password = secrets.token_urlsafe(9)
            write_private(config.FIRST_LOGIN_FILE,
                          f"логин: {config.DEFAULT_USERNAME}\nпароль: {password}\n"
                          "Смените пароль в «Аккаунт» - этот файл тогда удалится.\n")
            # В лог - только путь: docker logs и journald читает вся группа
            # docker и хранят их долго, а у файла права 600.
            print(f"[auth] первый вход: логин и пароль в {config.FIRST_LOGIN_FILE}", flush=True)
        self.storage.set_user(config.DEFAULT_USERNAME, hash_password(password))

    @staticmethod
    def tray_token() -> str:
        """Ключ для значка в трее; создаётся при первом обращении."""
        path = config.TRAY_TOKEN_FILE
        try:
            token = path.read_text("utf-8").strip()
        except OSError:
            token = ""
        if len(token) < 32:
            token = secrets.token_urlsafe(32)
            try:
                write_private(path, token + "\n")
            except FileExistsError:
                # Параллельный запрос успел создать файл - берём его ключ,
                # иначе выданный здесь не совпал бы с записанным.
                token = path.read_text("utf-8").strip()
        return token

    def start(self) -> None:
        if config.MONITOR_ENABLED:
            self.tray_token()
            self.alerts.start()
            self.relay.start()
            self.monitor.start()

    def stop(self) -> None:
        self.monitor.stop()
        self.alerts.stop()
        self.relay.stop()


def write_private(path: Path, text: str) -> None:
    """Пишет файл, который с момента создания читается только владельцем."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Старый файл удаляется, а не перезаписывается: у оставшегося от прошлой
    # установки могли быть права 644 (O_TRUNC их сохранил бы) или он мог
    # оказаться симлинком. O_EXCL|O_NOFOLLOW создаёт строго новый файл.
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


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
                storage.add_site(Site(
                    name=str(site["name"]),
                    note=str(site.get("note") or ""),
                    proxy=str(site.get("proxy") or ""),
                    stream_base=str(site.get("stream_base") or ""),
                    username=str(site.get("username") or ""),
                ))
            except Exception as exc:
                # Раньше здесь стоял голый `pass`, и перенос точки мог провалиться
                # молча: устройство всё равно создавало точку с тем же именем через
                # _ensure_refs, но уже пустую - без заметки, прокси и адреса go2rtc.
                # Данные терялись так, что заметить это было невозможно.
                print(f"[migrate] точка {site['name']!r}: {exc}", flush=True)
    for category in raw.get("categories") or []:
        if isinstance(category, dict) and category.get("site") and category.get("name"):
            try:
                storage.add_category(
                    str(category["site"]), str(category["name"]), str(category.get("note") or "")
                )
            except Exception as exc:
                print(f"[migrate] категория {category['name']!r}: {exc}", flush=True)

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
                    open_url=str(item.get("open_url") or ""),
                    stream_name=str(item.get("stream_name") or ""),
                    use_site_creds=bool(item.get("use_site_creds") or False),
                )
            )
            moved += 1
        except Exception as exc:
            print(f"[migrate] пропущено устройство {item.get('host')}: {exc}", flush=True)

    path.rename(path.with_suffix(path.suffix + ".migrated"))
    print(f"[migrate] перенесено устройств: {moved}, файл сохранён как {path.name}.migrated", flush=True)
    return moved
