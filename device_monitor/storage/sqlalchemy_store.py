"""Хранилище на SQLAlchemy (SQLite-движок).

Реализует тот же контракт Storage, что и прежний ручной SqliteStorage, поэтому
опрос, вендоры и веб от смены не зависят. ORM даёт декларативную схему, единый
источник правды по структуре таблиц и переносимость на другой движок сменой
одной строки URL.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import (
    ForeignKey, Index, Integer, String, Text, create_engine, delete, func, select,
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, Session, mapped_column, relationship,
)

from device_monitor.domain.errors import ConflictError, ValidationError
from device_monitor.domain.models import Category, Device, Site
from device_monitor.domain.text import plural_ru, same_name


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class SiteRow(Base):
    __tablename__ = "sites"
    name: Mapped[str] = mapped_column(String(120), primary_key=True)
    note: Mapped[str] = mapped_column(Text, default="")
    proxy: Mapped[str] = mapped_column(String(200), default="")


class CategoryRow(Base):
    __tablename__ = "categories"
    site: Mapped[str] = mapped_column(String(120), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    note: Mapped[str] = mapped_column(Text, default="")


class DeviceRow(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    site: Mapped[str] = mapped_column(String(120), index=True)
    category: Mapped[str] = mapped_column(String(80), default="")
    name: Mapped[str] = mapped_column(String(120))
    host: Mapped[str] = mapped_column(String(255))
    ports: Mapped[str] = mapped_column(Text)  # JSON-массив портов
    scheme: Mapped[str] = mapped_column(String(8), default="http")
    web_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    path: Mapped[str] = mapped_column(String(300), default="/")
    username: Mapped[str] = mapped_column(String(120), default="")
    password_enc: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")


class StatusEventRow(Base):
    __tablename__ = "status_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(8))
    at: Mapped[str] = mapped_column(String(40))


Index("idx_events_device_at", StatusEventRow.device_id, StatusEventRow.at)


class SqlAlchemyStorage:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite + пул с одним соединением под общим замком: нагрузка тут —
        # десятки запросов в минуту, а несколько потоков (опрос + веб) держать
        # проще, чем разбираться с потокобезопасностью пула SQLite.
        self._engine = create_engine(
            f"sqlite:///{path}",
            connect_args={"check_same_thread": False},
        )
        self._lock = threading.RLock()
        Base.metadata.create_all(self._engine)
        try:
            path.chmod(0o600)
        except OSError:
            pass

    # -- конвертация -----------------------------------------------------------

    @staticmethod
    def _to_device(row: DeviceRow) -> Device:
        import json
        return Device(
            id=row.id, site=row.site, category=row.category, name=row.name,
            host=row.host, ports=tuple(json.loads(row.ports)), scheme=row.scheme,
            web_port=row.web_port, path=row.path, username=row.username,
            password_enc=row.password_enc, note=row.note,
        )

    # -- точки -----------------------------------------------------------------

    def sites(self) -> list[Site]:
        with self._lock, Session(self._engine) as s:
            rows = s.scalars(select(SiteRow)).all()
            return sorted(
                (Site(r.name, r.note, r.proxy) for r in rows),
                key=lambda x: x.name.casefold(),
            )

    def add_site(self, name: str, note: str, proxy: str = "") -> Site:
        with self._lock, Session(self._engine) as s:
            if any(same_name(r.name, name) for r in s.scalars(select(SiteRow))):
                raise ConflictError(f"Точка «{name}» уже есть")
            s.add(SiteRow(name=name, note=note, proxy=proxy))
            s.commit()
        return Site(name, note, proxy)

    def update_site(self, old_name: str, name: str, note: str, proxy: str = "") -> Site:
        with self._lock, Session(self._engine) as s:
            row = s.get(SiteRow, old_name)
            if row is None:
                raise ValidationError("Точка не найдена")
            if not same_name(name, old_name) and any(
                same_name(r.name, name) for r in s.scalars(select(SiteRow))
            ):
                raise ConflictError(f"Точка «{name}» уже есть")
            # Переименование каскадом в одной транзакции: устройства и категории
            # ссылаются на точку по имени.
            if name != old_name:
                for cat in s.scalars(select(CategoryRow).where(CategoryRow.site == old_name)):
                    s.add(CategoryRow(site=name, name=cat.name, note=cat.note))
                    s.delete(cat)
                for dev in s.scalars(select(DeviceRow).where(DeviceRow.site == old_name)):
                    dev.site = name
                s.add(SiteRow(name=name, note=note, proxy=proxy))
                s.delete(row)
            else:
                row.note, row.proxy = note, proxy
            s.commit()
        return Site(name, note, proxy)

    def delete_site(self, name: str) -> None:
        with self._lock, Session(self._engine) as s:
            row = s.get(SiteRow, name)
            if row is None:
                raise ValidationError("Точка не найдена")
            count = s.scalar(select(func.count()).select_from(DeviceRow).where(DeviceRow.site == name))
            if count:
                raise ConflictError(
                    f"В точке ещё {plural_ru(count, 'устройство', 'устройства', 'устройств')}"
                    " — сначала удалите или перенесите их"
                )
            s.execute(delete(CategoryRow).where(CategoryRow.site == name))
            s.delete(row)
            s.commit()

    # -- категории -------------------------------------------------------------

    def categories(self) -> list[Category]:
        with self._lock, Session(self._engine) as s:
            rows = s.scalars(select(CategoryRow)).all()
            return sorted(
                (Category(r.site, r.name, r.note) for r in rows),
                key=lambda c: (c.site.casefold(), c.name.casefold()),
            )

    def _site_categories(self, s: Session, site: str) -> list[CategoryRow]:
        return list(s.scalars(select(CategoryRow).where(CategoryRow.site == site)))

    def add_category(self, site: str, name: str, note: str) -> Category:
        with self._lock, Session(self._engine) as s:
            if s.get(SiteRow, site) is None:
                raise ValidationError("Точка не найдена")
            if any(same_name(c.name, name) for c in self._site_categories(s, site)):
                raise ConflictError(f"Категория «{name}» в этой точке уже есть")
            s.add(CategoryRow(site=site, name=name, note=note))
            s.commit()
        return Category(site, name, note)

    def update_category(self, site: str, old: str, name: str, note: str) -> Category:
        with self._lock, Session(self._engine) as s:
            row = s.get(CategoryRow, (site, old))
            if row is None:
                raise ValidationError("Категория не найдена")
            if not same_name(name, old) and any(
                same_name(c.name, name) for c in self._site_categories(s, site)
            ):
                raise ConflictError(f"Категория «{name}» в этой точке уже есть")
            if name != old:
                for dev in s.scalars(
                    select(DeviceRow).where(DeviceRow.site == site, DeviceRow.category == old)
                ):
                    dev.category = name
                s.add(CategoryRow(site=site, name=name, note=note))
                s.delete(row)
            else:
                row.note = note
            s.commit()
        return Category(site, name, note)

    def delete_category(self, site: str, name: str) -> None:
        with self._lock, Session(self._engine) as s:
            row = s.get(CategoryRow, (site, name))
            if row is None:
                raise ValidationError("Категория не найдена")
            count = s.scalar(
                select(func.count()).select_from(DeviceRow)
                .where(DeviceRow.site == site, DeviceRow.category == name)
            )
            if count:
                raise ConflictError(
                    f"В категории ещё {plural_ru(count, 'устройство', 'устройства', 'устройств')}"
                    " — сначала удалите или перенесите их"
                )
            s.delete(row)
            s.commit()

    # -- устройства ------------------------------------------------------------

    def devices(self) -> list[Device]:
        with self._lock, Session(self._engine) as s:
            return [self._to_device(r) for r in s.scalars(select(DeviceRow))]

    def device(self, device_id: str) -> Device | None:
        with self._lock, Session(self._engine) as s:
            row = s.get(DeviceRow, device_id)
            return self._to_device(row) if row else None

    def _ensure_refs(self, s: Session, device: Device) -> None:
        if s.get(SiteRow, device.site) is None:
            s.add(SiteRow(name=device.site, note="", proxy=""))
        if device.category and s.get(CategoryRow, (device.site, device.category)) is None:
            s.add(CategoryRow(site=device.site, name=device.category, note=""))

    def add_device(self, device: Device) -> Device:
        import json
        from device_monitor.config import MAX_DEVICES
        with self._lock, Session(self._engine) as s:
            count = s.scalar(select(func.count()).select_from(DeviceRow))
            if count >= MAX_DEVICES:
                raise ConflictError(f"Больше {MAX_DEVICES} устройств не поддерживается")
            self._ensure_refs(s, device)
            s.add(DeviceRow(
                id=device.id, site=device.site, category=device.category, name=device.name,
                host=device.host, ports=json.dumps(list(device.ports)), scheme=device.scheme,
                web_port=device.web_port, path=device.path, username=device.username,
                password_enc=device.password_enc, note=device.note,
            ))
            s.commit()
        return device

    def update_device(self, device: Device) -> Device:
        import json
        with self._lock, Session(self._engine) as s:
            row = s.get(DeviceRow, device.id)
            if row is None:
                raise ValidationError("Устройство не найдено")
            self._ensure_refs(s, device)
            row.site, row.category, row.name = device.site, device.category, device.name
            row.host, row.ports = device.host, json.dumps(list(device.ports))
            row.scheme, row.web_port, row.path = device.scheme, device.web_port, device.path
            row.username, row.password_enc, row.note = device.username, device.password_enc, device.note
            s.commit()
        return device

    def delete_device(self, device_id: str) -> bool:
        with self._lock, Session(self._engine) as s:
            row = s.get(DeviceRow, device_id)
            if row is None:
                return False
            s.delete(row)
            s.execute(delete(StatusEventRow).where(StatusEventRow.device_id == device_id))
            s.commit()
            return True

    # -- история ---------------------------------------------------------------

    def record_status(self, device_id: str, status: str, at: str) -> None:
        with self._lock, Session(self._engine) as s:
            s.add(StatusEventRow(device_id=device_id, status=status, at=at))
            s.commit()

    def last_status(self, device_id: str) -> str | None:
        with self._lock, Session(self._engine) as s:
            return s.scalar(
                select(StatusEventRow.status).where(StatusEventRow.device_id == device_id)
                .order_by(StatusEventRow.id.desc()).limit(1)
            )

    def history(self, device_id: str, limit: int = 100) -> list[dict]:
        with self._lock, Session(self._engine) as s:
            rows = s.execute(
                select(StatusEventRow.status, StatusEventRow.at)
                .where(StatusEventRow.device_id == device_id)
                .order_by(StatusEventRow.id.desc()).limit(max(1, min(limit, 1000)))
            ).all()
            return [{"status": r.status, "at": r.at} for r in rows]

    def uptime(self, device_id: str, days: int) -> dict:
        since = _now() - timedelta(days=days)
        since_iso = since.isoformat(timespec="seconds")
        with self._lock, Session(self._engine) as s:
            before = s.scalar(
                select(StatusEventRow.status)
                .where(StatusEventRow.device_id == device_id, StatusEventRow.at < since_iso)
                .order_by(StatusEventRow.id.desc()).limit(1)
            )
            events = s.execute(
                select(StatusEventRow.status, StatusEventRow.at)
                .where(StatusEventRow.device_id == device_id, StatusEventRow.at >= since_iso)
                .order_by(StatusEventRow.id)
            ).all()
        if before is None and not events:
            return {"days": days, "uptime": None, "outages": 0, "events": 0}

        current = before if before is not None else events[0].status
        cursor = since
        up_seconds = 0.0
        outages = 0
        for ev in events:
            moment = datetime.fromisoformat(ev.at)
            if current == "up":
                up_seconds += (moment - cursor).total_seconds()
            if ev.status == "down":
                outages += 1
            current, cursor = ev.status, moment
        now = _now()
        if current == "up":
            up_seconds += (now - cursor).total_seconds()
        total = (now - since).total_seconds()
        return {
            "days": days,
            "uptime": round(up_seconds / total * 100, 2) if total > 0 else None,
            "outages": outages,
            "events": len(events),
        }

    def prune_history(self, days: int) -> int:
        cutoff = (_now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._lock, Session(self._engine) as s:
            result = s.execute(delete(StatusEventRow).where(StatusEventRow.at < cutoff))
            s.commit()
            return result.rowcount or 0
