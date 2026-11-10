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
                # Собственная учётка устройства - для формы редактирования.
                # Эффективная (выше) могла прийти от точки, и форма, заполненная
                # ею, сохранила бы пароль точки как собственный пароль устройства.
                "own_username": device.username,
                "own_has_password": bool(device.password_enc),
                "status": current.get("status", "unknown"),
                "ports_state": current.get("ports", {}),
                "latency_ms": current.get("latency_ms"),
                "since": current.get("since"),
                "checked_at": current.get("checked_at"),
                "facts": facts.get(device.id, {}),
                "muted_until": mutes.get(f"device:{device.id}"),
                # Адрес встроенного релея для устройства за туннелем точки:
                # его открывает кнопка «Открыть» (см. access/relay.py).
                "relay_url": app.relay.url_for(device),
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
            "access": app.tunnels.access(s.name).get("kind", "direct"),
        }
        tunnel = app.tunnels.status(s.name)
        if tunnel:
            row["ssh_tunnel"] = tunnel
        # proxy - то, что записано (его правит форма точки); effective_proxy -
        # через что ходим сейчас. Для SSH-точки это порт живого туннеля, и
        # отдавать его в поле proxy нельзя: форма сохранила бы его в базу, и
        # старый порт потом достался бы туннелю другого объекта.
        row["effective_proxy"] = app.tunnels.proxy_for(s.name, s.proxy)
        if row["effective_proxy"]:
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
        "assistant_configured": app.assistant.configured(),
    }


# -- поиск оборудования и ИИ-помощник -------------------------------------------

def scan_hint(app, site: str) -> tuple[int, dict]:
    if app.storage.site(site) is None:
        return HTTPStatus.NOT_FOUND, {"detail": "Точка не найдена"}
    return HTTPStatus.OK, {"subnet": app.scans.subnet_for(site)}


def start_scan(app, site: str, body: dict) -> tuple[int, dict]:
    return HTTPStatus.OK, {"job": app.scans.start(site, str(body.get("subnet") or ""))}


def scan_status(app, job_id: str) -> tuple[int, dict]:
    job = app.scans.get(job_id)
    if job is None:
        return HTTPStatus.NOT_FOUND, {"detail": "Скан не найден (сервис перезапускался?)"}
    return HTTPStatus.OK, {"job": job}


def assistant_settings(app) -> tuple[int, dict]:
    return HTTPStatus.OK, app.assistant.public()


def update_assistant(app, body: dict) -> tuple[int, dict]:
    return HTTPStatus.OK, app.assistant.update(body)


def assistant_test(app) -> tuple[int, dict]:
    return HTTPStatus.OK, app.assistant.test()


def assistant_names(app, body: dict) -> tuple[int, dict]:
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return HTTPStatus.BAD_REQUEST, {"detail": "Нет устройств для подсказки"}
    return HTTPStatus.OK, {"suggestions": app.assistant.suggest_names(candidates[:300])}


# -- значок в трее ------------------------------------------------------------

def tray_summary(app) -> tuple[int, dict]:
    """Сводка для значка в трее: только кто лежит и с каких пор. Ни паролей,
    ни учёток - ключ трея лежит в файле и должен давать минимум."""
    live, updated_at = app.monitor.snapshot()
    mutes = app.alerts.mutes_public()
    settings = app.alerts.rules_summary()
    down = []
    total = up = 0
    for device in app.storage.devices():
        status = live.get(device.id, {}).get("status", "unknown")
        total += 1
        up += status == "up"
        if status == "down":
            down.append({
                "id": device.id, "name": device.name, "site": device.site, "host": device.host,
                "since": live[device.id].get("since"),
                "muted": bool(mutes.get(f"device:{device.id}") or mutes.get(f"site:{device.site}")),
            })
    down.sort(key=lambda d: (d["site"].casefold(), d["name"].casefold()))
    return HTTPStatus.OK, {
        "total": total, "up": up, "down": down, "updated_at": updated_at,
        # Сразу после запуска сервиса круга ещё не было: «никто не лежит»
        # тогда значит «не знаем», и трей не должен рапортовать «вернулись».
        "ready": updated_at is not None,
        # Через сколько секунд падение считать настоящим - то же правило, что
        # у Telegram, чтобы трей не мигал уведомлениями на каждый сбой.
        "confirm_seconds": settings["down_confirm"] * POLL_INTERVAL,
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


def device_secret(app, device_id: str, own: bool = False) -> tuple[int, dict]:
    """Отдаёт логин и пароль устройства - единственная точка, где пароль
    устройства покидает сервис. Намеренно отдельный эндпоинт, а не поле в
    /api/state: общий стейт рефрешится каждые несколько секунд, а секрет
    должен доставаться только по явному запросу залогиненного пользователя.

    own=False - учётка, под которой реально пойдём на устройство (для
    «Открыть»); own=True - собственная учётка устройства (для формы).
    """
    device = app.storage.device(device_id)
    if device is None:
        return HTTPStatus.NOT_FOUND, {"detail": "Устройство не найдено"}
    if own:
        username, password_enc = device.username, device.password_enc
    else:
        username, password_enc = effective(device, app.storage.site(device.site))
    return HTTPStatus.OK, {
        "username": username,
        "password": deobfuscate(password_enc) if password_enc else "",
        "has_password": bool(password_enc),
    }


def site_secret(app, name: str) -> tuple[int, dict]:
    """Общая учётка точки - для «глаза» в настройках точки."""
    site = app.storage.site(name)
    if site is None:
        return HTTPStatus.NOT_FOUND, {"detail": "Точка не найдена"}
    return HTTPStatus.OK, {
        "username": site.username,
        "password": deobfuscate(site.password_enc) if site.password_enc else "",
        "has_password": bool(site.password_enc),
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


def access_info(app) -> tuple[int, dict]:
    from device_monitor.access import tunnels, vpn
    return HTTPStatus.OK, {
        "ssh": tunnels.ssh_available(),
        "networkmanager": vpn.nm_available(),
        "wireguard": vpn.wireguard_connections(),
    }


def ssh_public_key(app) -> tuple[int, dict]:
    from device_monitor.access import ssh_setup
    return HTTPStatus.OK, {"public_key": ssh_setup.public_key()}


def ssh_test(app, body: dict) -> tuple[int, dict]:
    from device_monitor.access import ssh_setup
    return HTTPStatus.OK, ssh_setup.test_login(str(body.get("target") or ""))


def ssh_install_key(app, body: dict) -> tuple[int, dict]:
    from device_monitor.access import ssh_setup
    return HTTPStatus.OK, ssh_setup.install_key(str(body.get("target") or ""),
                                                str(body.get("password") or ""))


def import_wireguard(app, body: dict) -> tuple[int, dict]:
    from device_monitor.access import vpn
    return HTTPStatus.OK, vpn.import_wireguard(str(body.get("conf") or ""),
                                               str(body.get("name") or "").strip())


def set_site_access(app, name: str, body: dict) -> tuple[int, dict]:
    from device_monitor.access import ssh_setup
    if app.storage.site(name) is None:
        return HTTPStatus.NOT_FOUND, {"detail": "Точка не найдена"}
    kind = body.get("kind")
    if kind == "ssh":
        ssh_setup.clean_target(str(body.get("target") or ""))   # проверка формата
        app.tunnels.set_access(name, {"kind": "ssh", "target": str(body["target"]).strip()})
    elif kind in ("direct", "socks"):
        app.tunnels.set_access(name, {"kind": kind})
    else:
        return HTTPStatus.BAD_REQUEST, {"detail": "Доступ: direct, socks или ssh"}
    app.monitor.request_run()
    return HTTPStatus.OK, {"access": app.tunnels.access(name), "tunnel": app.tunnels.status(name)}


def update_site(app, old: str, body: dict) -> tuple[int, dict]:
    # existing нужен, чтобы пустое поле пароля означало «оставить как было»:
    # наружу пароль не отдаётся, и форма его не знает.
    site = clean_site(body, existing=app.storage.site(old))
    updated = app.storage.update_site(old, site)
    if updated.name != old:
        app.tunnels.rename(old, updated.name)
    # Учётка точки могла смениться - устройства, которые её наследуют, надо
    # опросить заново, иначе сведения останутся собранными под старым паролем.
    app.monitor.request_run()
    return HTTPStatus.OK, {"site": updated.public()}


def delete_site(app, name: str) -> tuple[int, dict]:
    app.storage.delete_site(name)
    app.tunnels.set_access(name, {"kind": "direct"})
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
