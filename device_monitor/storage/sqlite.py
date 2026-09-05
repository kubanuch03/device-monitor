"""Хранилище на SQLite.

SQLite взят потому, что даёт транзакции, запросы и историю, оставаясь при этом
частью стандартной библиотеки: ни новой зависимости, ни второго контейнера.
Когда писать начнут несколько процессов сразу, на смену придёт Postgres - но
уже как ещё одна реализация Storage, а не как переделка всего приложения.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from device_monitor.domain.errors import ConflictError, ValidationError
from device_monitor.domain.models import Category, Device, Site
from device_monitor.domain.text import plural_ru, same_name

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    name TEXT PRIMARY KEY,
    note TEXT NOT NULL DEFAULT '',
    proxy TEXT NOT NULL DEFAULT '',
    stream_base TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    password_enc TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS categories (
    site TEXT NOT NULL,
    name TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (site, name)
);

CREATE TABLE IF NOT EXISTS devices (
    id           TEXT PRIMARY KEY,
    site         TEXT NOT NULL,
    category     TEXT NOT NULL DEFAULT '',
    name         TEXT NOT NULL,
    host         TEXT NOT NULL,
    ports        TEXT NOT NULL,
    scheme       TEXT NOT NULL DEFAULT 'http',
    web_port     INTEGER,
    path         TEXT NOT NULL DEFAULT '/',
    username     TEXT NOT NULL DEFAULT '',
    password_enc TEXT NOT NULL DEFAULT '',
    note         TEXT NOT NULL DEFAULT '',
    open_url     TEXT NOT NULL DEFAULT '',
    stream_name  TEXT NOT NULL DEFAULT '',
    use_site_creds INTEGER NOT NULL DEFAULT 0
);

-- В таблицу пишутся только СМЕНЫ состояния, а не каждый круг опроса. Иначе
-- одиннадцать устройств с интервалом двадцать секунд дали бы полтора миллиона
-- строк в месяц, из которых 99% - повтор предыдущей.
CREATE TABLE IF NOT EXISTS status_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    status    TEXT NOT NULL,
    at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_device_at ON status_events(device_id, at);
CREATE INDEX IF NOT EXISTS idx_devices_site ON devices(site, category);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SqliteStorage:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Одно соединение под общим замком вместо пула: нагрузка тут - десятки
        # запросов в минуту, а разделяемое состояние между потоками опроса и
        # HTTP так остаётся ровно одно и его легко удержать в голове.
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")
            # Колонка proxy добавлена позже — на старых базах её нет.
            cols = {r["name"] for r in self._db.execute("PRAGMA table_info(sites)")}
            if "proxy" not in cols:
                self._db.execute("ALTER TABLE sites ADD COLUMN proxy TEXT NOT NULL DEFAULT ''")
            # open_url добавлен позже — на старых базах его нет.
            dcols = {r["name"] for r in self._db.execute("PRAGMA table_info(devices)")}
            if "open_url" not in dcols:
                self._db.execute("ALTER TABLE devices ADD COLUMN open_url TEXT NOT NULL DEFAULT ''")
            if "stream_name" not in dcols:
                self._db.execute("ALTER TABLE devices ADD COLUMN stream_name TEXT NOT NULL DEFAULT ''")
            if "stream_base" not in cols:
                self._db.execute("ALTER TABLE sites ADD COLUMN stream_base TEXT NOT NULL DEFAULT ''")
            # Учётка точки и признак наследования добавлены позже. Умолчания
            # выбраны так, что старая база ведёт себя ровно как раньше: учётки
            # у точек нет, ни одно устройство её не наследует.
            if "username" not in cols:
                self._db.execute("ALTER TABLE sites ADD COLUMN username TEXT NOT NULL DEFAULT ''")
            if "password_enc" not in cols:
                self._db.execute("ALTER TABLE sites ADD COLUMN password_enc TEXT NOT NULL DEFAULT ''")
            if "use_site_creds" not in dcols:
                self._db.execute(
                    "ALTER TABLE devices ADD COLUMN use_site_creds INTEGER NOT NULL DEFAULT 0")
            self._db.commit()
        self._lock_down()

    def _lock_down(self) -> None:
        """Закрыть от посторонних не только базу, но и её спутников.

        Права 0600 на monitor.db - половина защиты и раньше она была фиктивной:
        SQLite в режиме WAL держит ещё monitor.db-wal и -shm, создаёт их по
        umask (у нас вышло 0644), и свежезаписанный пароль устройства какое-то
        время лежит именно в WAL. Плюс файлы пересоздаются при каждом открытии
        базы, поэтому одного chmod при миграции мало - каталог обязан быть
        закрыт сам по себе: без права входа в него содержимое недоступно
        независимо от того, с какими правами SQLite создаст файл в следующий раз.
        """
        for target, mode in (
            (self.path.parent, 0o700),
            (self.path, 0o600),
            (self.path.with_name(self.path.name + "-wal"), 0o600),
            (self.path.with_name(self.path.name + "-shm"), 0o600),
        ):
            try:
                target.chmod(mode)
            except OSError:
                pass

    # -- вспомогательное ------------------------------------------------------

    def _rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute(sql, args))

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._db.execute(sql, args)
            self._db.commit()
            return cursor

    @staticmethod
    def _device(row: sqlite3.Row) -> Device:
        return Device(
            id=row["id"],
            site=row["site"],
            category=row["category"],
            name=row["name"],
            host=row["host"],
            ports=tuple(json.loads(row["ports"])),
            scheme=row["scheme"],
            web_port=row["web_port"],
            path=row["path"],
            username=row["username"],
            password_enc=row["password_enc"],
            note=row["note"],
            open_url=row["open_url"],
            stream_name=row["stream_name"],
            use_site_creds=bool(row["use_site_creds"]),
        )

    # -- точки ----------------------------------------------------------------

    def sites(self) -> list[Site]:
        rows = self._rows(
            "SELECT name, note, proxy, stream_base, username, password_enc FROM sites")
        return sorted(
            (self._site(r) for r in rows),
            key=lambda s: s.name.casefold(),
        )

    def site(self, name: str) -> Site | None:
        rows = self._rows(
            "SELECT name, note, proxy, stream_base, username, password_enc FROM sites"
            " WHERE name = ?", (name,))
        return self._site(rows[0]) if rows else None

    @staticmethod
    def _site(row: sqlite3.Row) -> Site:
        return Site(
            name=row["name"],
            note=row["note"],
            proxy=row["proxy"],
            stream_base=row["stream_base"],
            username=row["username"],
            password_enc=row["password_enc"],
        )

    def add_site(self, site: Site) -> Site:
        # Точка передаётся объектом, а не россыпью аргументов: полей у неё уже
        # шесть, и каждое новое иначе означало бы правку всех вызовов подряд.
        with self._lock:
            if any(same_name(s.name, site.name) for s in self.sites()):
                raise ConflictError(f"Точка «{site.name}» уже есть")
            self._exec(
                "INSERT INTO sites(name, note, proxy, stream_base, username, password_enc)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (site.name, site.note, site.proxy, site.stream_base,
                 site.username, site.password_enc))
        return site

    def update_site(self, old_name: str, site: Site) -> Site:
        with self._lock:
            if not self._rows("SELECT 1 FROM sites WHERE name = ?", (old_name,)):
                raise ValidationError("Точка не найдена")
            if not same_name(site.name, old_name) and any(
                    same_name(s.name, site.name) for s in self.sites()):
                raise ConflictError(f"Точка «{site.name}» уже есть")
            # Переименование одной транзакцией: устройства и категории
            # ссылаются на точку по имени, и если обновить не всё сразу, часть
            # из них останется в точке, которой больше нет.
            self._db.execute(
                "UPDATE sites SET name = ?, note = ?, proxy = ?, stream_base = ?,"
                " username = ?, password_enc = ? WHERE name = ?",
                (site.name, site.note, site.proxy, site.stream_base,
                 site.username, site.password_enc, old_name))
            self._db.execute("UPDATE categories SET site = ? WHERE site = ?", (site.name, old_name))
            self._db.execute("UPDATE devices SET site = ? WHERE site = ?", (site.name, old_name))
            self._db.commit()
        return site

    def delete_site(self, name: str) -> None:
        with self._lock:
            if not self._rows("SELECT 1 FROM sites WHERE name = ?", (name,)):
                raise ValidationError("Точка не найдена")
            count = self._rows("SELECT COUNT(*) c FROM devices WHERE site = ?", (name,))[0]["c"]
            if count:
                raise ConflictError(
                    f"В точке ещё {plural_ru(count, 'устройство', 'устройства', 'устройств')}"
                    " — сначала удалите или перенесите их"
                )
            self._db.execute("DELETE FROM categories WHERE site = ?", (name,))
            self._db.execute("DELETE FROM sites WHERE name = ?", (name,))
            self._db.commit()

    # -- категории -------------------------------------------------------------

    def categories(self) -> list[Category]:
        rows = self._rows("SELECT site, name, note FROM categories")
        return sorted(
            (Category(r["site"], r["name"], r["note"]) for r in rows),
            key=lambda c: (c.site.casefold(), c.name.casefold()),
        )

    def add_category(self, site: str, name: str, note: str) -> Category:
        with self._lock:
            if not self._rows("SELECT 1 FROM sites WHERE name = ?", (site,)):
                raise ValidationError("Точка не найдена")
            if any(same_name(c.name, name) for c in self.categories() if c.site == site):
                raise ConflictError(f"Категория «{name}» в этой точке уже есть")
            self._exec("INSERT INTO categories(site, name, note) VALUES (?, ?, ?)", (site, name, note))
        return Category(site, name, note)

    def update_category(self, site: str, old: str, name: str, note: str) -> Category:
        with self._lock:
            if not self._rows("SELECT 1 FROM categories WHERE site = ? AND name = ?", (site, old)):
                raise ValidationError("Категория не найдена")
            if not same_name(name, old) and any(
                same_name(c.name, name) for c in self.categories() if c.site == site
            ):
                raise ConflictError(f"Категория «{name}» в этой точке уже есть")
            self._db.execute(
                "UPDATE categories SET name = ?, note = ? WHERE site = ? AND name = ?",
                (name, note, site, old),
            )
            self._db.execute(
                "UPDATE devices SET category = ? WHERE site = ? AND category = ?", (name, site, old)
            )
            self._db.commit()
        return Category(site, name, note)

    def delete_category(self, site: str, name: str) -> None:
        with self._lock:
            if not self._rows("SELECT 1 FROM categories WHERE site = ? AND name = ?", (site, name)):
                raise ValidationError("Категория не найдена")
            count = self._rows(
                "SELECT COUNT(*) c FROM devices WHERE site = ? AND category = ?", (site, name)
            )[0]["c"]
            if count:
                raise ConflictError(
                    f"В категории ещё {plural_ru(count, 'устройство', 'устройства', 'устройств')}"
                    " — сначала удалите или перенесите их"
                )
            self._exec("DELETE FROM categories WHERE site = ? AND name = ?", (site, name))

    # -- устройства -------------------------------------------------------------

    def devices(self) -> list[Device]:
        return [self._device(r) for r in self._rows("SELECT * FROM devices")]

    def device(self, device_id: str) -> Device | None:
        rows = self._rows("SELECT * FROM devices WHERE id = ?", (device_id,))
        return self._device(rows[0]) if rows else None

    def _ensure_refs(self, device: Device) -> None:
        """Точка и категория устройства обязаны существовать записями.

        Устройство можно завести, вписав новое название точки прямо в форме, -
        и если не создать саму запись, оно окажется в точке, которой нет в
        списке, а её экран будет собираться из воздуха при каждом чтении.
        """
        if not self._rows("SELECT 1 FROM sites WHERE name = ?", (device.site,)):
            self._db.execute("INSERT INTO sites(name, note) VALUES (?, '')", (device.site,))
        if device.category and not self._rows(
            "SELECT 1 FROM categories WHERE site = ? AND name = ?", (device.site, device.category)
        ):
            self._db.execute(
                "INSERT INTO categories(site, name, note) VALUES (?, ?, '')",
                (device.site, device.category),
            )

    def _write(self, device: Device, insert: bool) -> Device:
        values = (
            device.site, device.category, device.name, device.host,
            json.dumps(list(device.ports)), device.scheme, device.web_port,
            device.path, device.username, device.password_enc, device.note,
            device.open_url, device.stream_name, int(device.use_site_creds),
        )
        with self._lock:
            self._ensure_refs(device)
            if insert:
                self._db.execute(
                    "INSERT INTO devices(site, category, name, host, ports, scheme, web_port,"
                    " path, username, password_enc, note, open_url, stream_name,"
                    " use_site_creds, id)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values + (device.id,),
                )
            else:
                self._db.execute(
                    "UPDATE devices SET site=?, category=?, name=?, host=?, ports=?, scheme=?,"
                    " web_port=?, path=?, username=?, password_enc=?, note=?, open_url=?,"
                    " stream_name=?, use_site_creds=? WHERE id=?",
                    values + (device.id,),
                )
            self._db.commit()
        return device

    def add_device(self, device: Device) -> Device:
        with self._lock:
            count = self._rows("SELECT COUNT(*) c FROM devices")[0]["c"]
            from device_monitor.config import MAX_DEVICES

            if count >= MAX_DEVICES:
                raise ConflictError(f"Больше {MAX_DEVICES} устройств не поддерживается")
        return self._write(device, insert=True)

    def update_device(self, device: Device) -> Device:
        return self._write(device, insert=False)

    def delete_device(self, device_id: str) -> bool:
        with self._lock:
            cursor = self._db.execute("DELETE FROM devices WHERE id = ?", (device_id,))
            self._db.execute("DELETE FROM status_events WHERE device_id = ?", (device_id,))
            self._db.commit()
            return cursor.rowcount > 0

    # -- история ----------------------------------------------------------------

    def record_status(self, device_id: str, status: str, at: str) -> None:
        self._exec(
            "INSERT INTO status_events(device_id, status, at) VALUES (?, ?, ?)",
            (device_id, status, at),
        )

    def last_status(self, device_id: str) -> str | None:
        rows = self._rows(
            "SELECT status FROM status_events WHERE device_id = ? ORDER BY id DESC LIMIT 1",
            (device_id,),
        )
        return rows[0]["status"] if rows else None

    def history(self, device_id: str, limit: int = 100) -> list[dict]:
        rows = self._rows(
            "SELECT status, at FROM status_events WHERE device_id = ? ORDER BY id DESC LIMIT ?",
            (device_id, max(1, min(limit, 1000))),
        )
        return [{"status": r["status"], "at": r["at"]} for r in rows]

    def uptime(self, device_id: str, days: int) -> dict:
        """Доля времени «на связи» за окно и число падений.

        Считается по сменам состояния, а не по числу проверок: событий мало,
        и точность от этого только выше - интервал между двумя записями и есть
        время, проведённое в том состоянии.
        """
        since = datetime.now(timezone.utc) - timedelta(days=days)
        since_iso = since.isoformat(timespec="seconds")

        before = self._rows(
            "SELECT status FROM status_events WHERE device_id = ? AND at < ?"
            " ORDER BY id DESC LIMIT 1",
            (device_id, since_iso),
        )
        events = self._rows(
            "SELECT status, at FROM status_events WHERE device_id = ? AND at >= ? ORDER BY id",
            (device_id, since_iso),
        )
        if not before and not events:
            return {"days": days, "uptime": None, "outages": 0, "events": 0}

        current = before[0]["status"] if before else events[0]["status"]
        cursor_time = since
        up_seconds = 0.0
        outages = 0

        for event in events:
            moment = datetime.fromisoformat(event["at"])
            if current == "up":
                up_seconds += (moment - cursor_time).total_seconds()
            if event["status"] == "down":
                outages += 1
            current = event["status"]
            cursor_time = moment

        now = datetime.now(timezone.utc)
        if current == "up":
            up_seconds += (now - cursor_time).total_seconds()

        total = (now - since).total_seconds()
        return {
            "days": days,
            "uptime": round(up_seconds / total * 100, 2) if total > 0 else None,
            "outages": outages,
            "events": len(events),
        }

    def prune_history(self, days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        cursor = self._exec("DELETE FROM status_events WHERE at < ?", (cutoff,))
        return cursor.rowcount
