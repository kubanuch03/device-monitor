"""Экспорт и импорт всех данных панели: точки, категории, устройства.

Формат - обычный JSON с версией, чтобы файл можно было открыть, перенести на
другую машину и прочитать глазами. Пароли по умолчанию в файл НЕ попадают:
бэкап не должен быть ключом ко всем камерам объекта. Выгрузка с паролями -
явный выбор, и тогда пароли лежат в открытом виде, поэтому такой файл надо
считать секретом.

Импорт идёт как upsert: совпадающие по имени точки и категории и по id
устройства обновляются, остальные создаются. Ошибки одного элемента не
останавливают остальные - они попадают в отчёт, чтобы было видно, что именно
не перенеслось.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from device_monitor.domain.errors import ValidationError
from device_monitor.domain.validation import clean_category, clean_device, clean_site
from device_monitor.security import deobfuscate

FORMAT = "device-monitor/1"
ID_RE = re.compile(r"[0-9a-f]{32}")  # формат id, который выдаёт uuid4().hex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def export_data(storage, include_secrets: bool) -> dict:
    sites = []
    for s in storage.sites():
        item = {
            "name": s.name,
            "note": s.note,
            "proxy": s.proxy,
            "stream_base": s.stream_base,
            "username": s.username,
        }
        if include_secrets:
            item["password"] = deobfuscate(s.password_enc) if s.password_enc else ""
        sites.append(item)

    categories = [
        {"site": c.site, "name": c.name, "note": c.note} for c in storage.categories()
    ]

    devices = []
    for d in storage.devices():
        item = {
            "id": d.id,
            "site": d.site,
            "category": d.category,
            "name": d.name,
            "host": d.host,
            "ports": list(d.ports),
            "scheme": d.scheme,
            "web_port": d.web_port,
            "path": d.path,
            "username": d.username,
            "note": d.note,
            "open_url": d.open_url,
            "stream_name": d.stream_name,
            "use_site_creds": d.use_site_creds,
        }
        if include_secrets:
            item["password"] = deobfuscate(d.password_enc) if d.password_enc else ""
        devices.append(item)

    return {
        "format": FORMAT,
        "exported_at": _now(),
        "includes_secrets": include_secrets,
        "sites": sites,
        "categories": categories,
        "devices": devices,
    }


def _items(payload: dict, key: str) -> list:
    value = payload.get(key) or []
    if not isinstance(value, list):
        raise ValidationError(f"Раздел «{key}» должен быть списком")
    return value


def _text(item, key: str) -> str:
    """Строка из элемента файла; всё, что не объект, даёт пустую строку."""
    return str(item.get(key) or "").strip() if isinstance(item, dict) else ""


def import_data(storage, payload: dict) -> dict:
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        raise ValidationError(f"Это не файл Device Monitor (ожидался формат {FORMAT})")

    report = {"sites": 0, "categories": 0, "devices": 0, "errors": []}

    # Порядок важен: точки и категории раньше устройств, на которые они ссылаются.
    for item in _items(payload, "sites"):
        name = _text(item, "name")
        try:
            site = clean_site(item, existing=storage.site(name))
            if storage.site(site.name):
                storage.update_site(site.name, site)
            else:
                storage.add_site(site)
            report["sites"] += 1
        except ValidationError as exc:
            report["errors"].append(f"точка «{name}»: {exc}")

    existing_categories = {(c.site, c.name) for c in storage.categories()}
    for item in _items(payload, "categories"):
        site = _text(item, "site")
        try:
            name, note = clean_category(item)
            if (site, name) in existing_categories:
                storage.update_category(site, name, name, note)
            else:
                storage.add_category(site, name, note)
            report["categories"] += 1
        except ValidationError as exc:
            report["errors"].append(f"категория «{site} / {_text(item, 'name')}»: {exc}")

    for item in _items(payload, "devices"):
        label = _text(item, "name") or _text(item, "host") or "?"
        try:
            device_id = _text(item, "id")
            existing = storage.device(device_id) if device_id else None
            device = clean_device(item, existing=existing)
            if existing:
                storage.update_device(device)
            else:
                # Восстановление бэкапа должно сохранить идентичность устройства:
                # история и ссылки по id иначе потерялись бы. Свой id берём только
                # если он нужного формата - иначе генерируем новый.
                if ID_RE.fullmatch(device_id):
                    device = device.with_fields(id=device_id)
                storage.add_device(device)
            report["devices"] += 1
        except ValidationError as exc:
            report["errors"].append(f"устройство «{label}»: {exc}")

    return report
