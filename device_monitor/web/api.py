"""Операции API как обычные функции.

Ни одна из них не знает про HTTP: принимают данные, возвращают (код, тело).
Именно поэтому переезд на FastAPI, если он однажды понадобится, будет стоить
нового транспортного файла, а не переписывания логики.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from http import HTTPStatus
from urllib.request import urlopen

from device_monitor.config import HISTORY_DAYS, POLL_INTERVAL
from device_monitor.domain.errors import ValidationError
from device_monitor.domain import transfer
from device_monitor.domain.credentials import effective
from device_monitor.domain.validation import clean_category, clean_device, clean_site
from device_monitor.security import deobfuscate


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state(app) -> tuple[int, dict]:
    live, updated_at = app.monitor.snapshot()
    facts = app.monitor.facts()
    tunnels = app.monitor.tunnels()

    known_sites = {s.name: s for s in app.storage.sites()}
    mutes = app.alerts.mutes_public()

    devices = []
    for device in app.storage.devices():
        entry = device.public()
        current = live.get(device.id, {})
        # Логин и признак пароля показываем ЭФФЕКТИВНЫЕ - те, под которыми
        # реально пойдём на устройство. Иначе карточка камеры, наследующей
        # учётку точки, выглядела бы как «пароля нет», и кнопка «Открыть» не
        # стала бы его подставлять, хотя пароль есть.
        username, password_enc = effective(device, known_sites.get(device.site))
        entry.update(
            {
                "username": username,
                "has_password": bool(password_enc),
                "status": current.get("status", "unknown"),
                "ports_state": current.get("ports", {}),
                "latency_ms": current.get("latency_ms"),
                "since": current.get("since"),
                "checked_at": current.get("checked_at"),
                "facts": facts.get(device.id, {}),
                "muted_until": mutes.get(f"device:{device.id}"),
            }
        )
        devices.append(entry)
    devices.sort(key=lambda d: (d["site"].casefold(), d["name"].casefold()))

    def summary(items):
        return {
            "total": len(items),
            "up": sum(1 for d in items if d["status"] == "up"),
            "down": sum(1 for d in items if d["status"] == "down"),
        }

    sites = []
    for s in app.storage.sites():
        row = {
            **s.public(),
            **summary([d for d in devices if d["site"] == s.name]),
            "muted_until": mutes.get(f"site:{s.name}"),
        }
        if s.proxy:
            # Состояние туннеля отдаём отдельно от статусов устройств: когда он
            # лёг, все устройства точки показываются down, и UI должен сказать
            # «нет связи с туннелем объекта», а не «12 устройств не отвечают».
            row["tunnel"] = tunnels.get(s.name, {}).get("status", "unknown")
            row["tunnel_detail"] = tunnels.get(s.name, {}).get("detail")
        sites.append(row)
    categories = [
        {
            "site": c.site,
            "name": c.name,
            "note": c.note,
            **summary([d for d in devices if d["site"] == c.site and d["category"] == c.name]),
        }
        for c in app.storage.categories()
    ]

    return HTTPStatus.OK, {
        "devices": devices,
        "sites": sites,
        "categories": categories,
        "updated_at": updated_at,
        "poll_interval": POLL_INTERVAL,
        "history_days": HISTORY_DAYS,
        "alerts_configured": app.alerts.configured(),
    }


# -- оповещения ---------------------------------------------------------------

def alert_settings(app) -> tuple[int, dict]:
    return HTTPStatus.OK, app.alerts.public_settings()


def update_alert_settings(app, body: dict) -> tuple[int, dict]:
    return HTTPStatus.OK, app.alerts.update_settings(body)


def alert_test(app) -> tuple[int, dict]:
    app.alerts.send_test()
    return HTTPStatus.OK, {"ok": True}


def alert_chats(app) -> tuple[int, dict]:
    return HTTPStatus.OK, {"chats": app.alerts.find_chats()}


def set_mute(app, body: dict) -> tuple[int, dict]:
    kind, target = body.get("kind"), str(body.get("target") or "")
    if kind == "device" and app.storage.device(target) is None:
        return HTTPStatus.NOT_FOUND, {"detail": "Устройство не найдено"}
    if kind == "site" and app.storage.site(target) is None:
        return HTTPStatus.NOT_FOUND, {"detail": "Точка не найдена"}
    return HTTPStatus.OK, {"mutes": app.alerts.set_mute(kind, target, body.get("minutes"))}


def device_history(app, device_id: str) -> tuple[int, dict]:
    if app.storage.device(device_id) is None:
        return HTTPStatus.NOT_FOUND, {"detail": "Устройство не найдено"}
    return HTTPStatus.OK, {
        "history": app.storage.history(device_id, 200),
        "uptime": app.storage.uptime(device_id, HISTORY_DAYS),
        "uptime_24h": app.storage.uptime(device_id, 1),
    }


def device_secret(app, device_id: str) -> tuple[int, dict]:
    """Отдаёт логин и пароль устройства - единственная точка, где пароль
    покидает сервис. Намеренно отдельный эндпоинт, а не поле в /api/state:
    общий стейт рефрешится каждые несколько секунд и кэшируется, а секрет
    должен доставаться только по явному запросу залогиненного пользователя.
    Доступ уже отфильтрован авторизацией на уровне транспорта.
    """
    device = app.storage.device(device_id)
    if device is None:
        return HTTPStatus.NOT_FOUND, {"detail": "Устройство не найдено"}
    username, password_enc = effective(device, app.storage.site(device.site))
    return HTTPStatus.OK, {
        "username": username,
        "password": deobfuscate(password_enc) if password_enc else "",
        "has_password": bool(password_enc),
    }


def create_device(app, body: dict) -> tuple[int, dict]:
    device = app.storage.add_device(clean_device(body))
    app.monitor.request_inspect(device.id)
    app.monitor.request_run()
    return HTTPStatus.CREATED, {"device": device.public()}


def update_device(app, device_id: str, body: dict) -> tuple[int, dict]:
    existing = app.storage.device(device_id)
    if existing is None:
        return HTTPStatus.NOT_FOUND, {"detail": "Устройство не найдено"}
    device = app.storage.update_device(clean_device(body, existing=existing))
    app.monitor.request_inspect(device.id)
    app.monitor.request_run()
    return HTTPStatus.OK, {"device": device.public()}


def delete_device(app, device_id: str) -> tuple[int, dict]:
    if not app.storage.delete_device(device_id):
        return HTTPStatus.NOT_FOUND, {"detail": "Устройство не найдено"}
    return HTTPStatus.OK, {"ok": True}


def create_site(app, body: dict) -> tuple[int, dict]:
    return HTTPStatus.CREATED, {"site": app.storage.add_site(clean_site(body)).public()}


def update_site(app, old: str, body: dict) -> tuple[int, dict]:
    # existing нужен, чтобы пустое поле пароля означало «оставить как было»:
    # наружу пароль не отдаётся, и форма его не знает.
    site = clean_site(body, existing=app.storage.site(old))
    updated = app.storage.update_site(old, site)
    # Учётка точки могла смениться - устройства, которые её наследуют, надо
    # опросить заново, иначе сведения останутся собранными под старым паролем.
    app.monitor.request_run()
    return HTTPStatus.OK, {"site": updated.public()}


def delete_site(app, name: str) -> tuple[int, dict]:
    app.storage.delete_site(name)
    return HTTPStatus.OK, {"ok": True}


def create_category(app, body: dict) -> tuple[int, dict]:
    site = str(body.get("site") or "").strip()
    name, note = clean_category(body)
    return HTTPStatus.CREATED, {"category": app.storage.add_category(site, name, note).__dict__}


def update_category(app, site: str, old: str, body: dict) -> tuple[int, dict]:
    name, note = clean_category(body)
    return HTTPStatus.OK, {"category": app.storage.update_category(site, old, name, note).__dict__}


def delete_category(app, site: str, name: str) -> tuple[int, dict]:
    app.storage.delete_category(site, name)
    return HTTPStatus.OK, {"ok": True}


def export_data(app, include_secrets: bool) -> tuple[int, dict]:
    return HTTPStatus.OK, transfer.export_data(app.storage, include_secrets)


def import_data(app, payload: dict) -> tuple[int, dict]:
    report = transfer.import_data(app.storage, payload)
    # Импорт меняет устройства и учётки - пересчитываем состояние сразу,
    # не дожидаясь очередного круга опроса.
    app.monitor.request_run()
    return HTTPStatus.OK, report


def stream_names(app, site_name: str) -> tuple[int, dict]:
    """Какие источники есть в go2rtc точки.

    Странице видео это нужно для выбора качества: SD - это источник
    `<имя>_sd`, и он есть не у каждой камеры. Спросить go2rtc сам браузер не
    может - у go2rtc нет CORS, а на чужом сервере объекта его не включить.
    """
    site = app.storage.site(site_name)
    if site is None:
        return HTTPStatus.NOT_FOUND, {"detail": "Точка не найдена"}
    if not site.stream_base:
        return HTTPStatus.OK, {"names": []}
    try:
        with urlopen(f"{site.stream_base}/api/streams", timeout=4) as response:
            names = sorted(json.load(response))
    except (OSError, ValueError) as exc:
        return HTTPStatus.BAD_GATEWAY, {"detail": f"go2rtc точки не отвечает: {exc}"}
    return HTTPStatus.OK, {"names": names}


def recheck(app) -> tuple[int, dict]:
    app.monitor.request_run()
    return HTTPStatus.OK, {"ok": True}


def inspect(app, device_id: str) -> tuple[int, dict]:
    if app.storage.device(device_id) is None:
        return HTTPStatus.NOT_FOUND, {"detail": "Устройство не найдено"}
    app.monitor.request_inspect(device_id)
    app.monitor.request_run()
    return HTTPStatus.OK, {"ok": True}
