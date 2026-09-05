"""Как именно веб-интерфейс устройства спрашивает пароль.

Нужно ровно для одного: решить, можно ли открыть морду устройства сразу
залогиненным. Браузер умеет это сам — но только когда устройство отвечает
HTTP-авторизацией (401 + WWW-Authenticate). Если же вход нарисован формой на
странице (так делают Dahua и часть Hikvision), подставить учётку снаружи
нельзя ничем: страница живёт в чужом origin.

Отдельный пробник, а не поле вендорного драйвера, потому что вопрос не
вендорный: одна и та же Hikvision может отвечать Digest на ISAPI и при этом
показывать форму на корне. Спрашиваем именно тот адрес, который откроется по
кнопке «Открыть», а не служебный CGI.
"""

from __future__ import annotations

import urllib.error
import urllib.request

from device_monitor.config import INSPECT_TIMEOUT
from device_monitor.domain.models import Device

# Схемы, для которых браузер умеет логиниться сам по адресу вида
# http://логин:пароль@хост/. Проверено на Chrome 152: и Basic, и Digest
# проходят при переходе верхнего уровня — 401, повтор с учёткой, 200.
BROWSER_SCHEMES = ("basic", "digest")


def probe_web_auth(device: Device) -> dict:
    """{'web_auth': 'basic'|'digest'|'form', 'web_realm': str} или {}.

    Запрос идёт БЕЗ учётных данных: 401 с заголовком — это и есть ответ.
    Пустой словарь означает «не выяснили» (устройство молчит, таймаут,
    неожиданный код) — и тогда кнопка «Открыть» ведёт себя как раньше.
    """
    # Спрашиваем ровно тот адрес, который откроется кнопкой «Открыть». Для
    # устройства за туннелем это проброшенный локальный порт: он доступен
    # обычному urllib, в отличие от настоящего адреса в сети объекта.
    request = urllib.request.Request(device.open_url or device.url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=INSPECT_TIMEOUT) as response:
            # Ответ без 401 означает, что пароль спрашивают не по HTTP, а
            # формой внутри страницы (либо не спрашивают вовсе).
            return {"web_auth": "form"} if 200 <= response.status < 400 else {}
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            return {"web_auth": "form"}
        header = (exc.headers.get("WWW-Authenticate") or "").strip()
        if not header:
            return {}
        scheme, _, rest = header.partition(" ")
        facts = {"web_auth": scheme.lower()}
        realm = _realm(rest)
        if realm:
            facts["web_realm"] = realm
        return facts
    except (urllib.error.URLError, OSError, ValueError):
        return {}


def _realm(params: str) -> str:
    for part in params.split(","):
        key, sep, value = part.strip().partition("=")
        if sep and key.strip().lower() == "realm":
            return value.strip().strip('"')[:120]
    return ""
