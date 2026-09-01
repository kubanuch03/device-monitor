"""Разбор и проверка того, что пришло от клиента.

Валидация живёт в доменном слое, а не в обработчиках HTTP: правила «порт от 1
до 65535», «путь начинается со слэша» верны независимо от того, пришли данные
из формы, из импорта файла или из будущего CLI.
"""

from __future__ import annotations

import re
import uuid
from urllib.parse import urlsplit

from device_monitor.config import AUTH_ENABLED
from device_monitor.domain.errors import ValidationError
from device_monitor.domain.models import Device
from device_monitor.security import obfuscate

HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def split_address(raw: str) -> dict:
    """Разбирает то, что вписали в поле «Адрес».

    Туда одинаково естественно вписать и голый `10.30.205.24`, и целиком
    скопированную из браузера ссылку `http://10.30.205.243:8833/admin/` -
    разбирать её на части руками пользователь не должен.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    candidate = raw if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw) else "//" + raw
    try:
        parts = urlsplit(candidate)
        host, port = parts.hostname, parts.port
    except ValueError:
        return {"host": raw}
    if not host:
        return {"host": raw}

    path = parts.path or ""
    if parts.query:
        path += "?" + parts.query
    return {
        "host": host,
        "scheme": parts.scheme or None,
        "web_port": port,
        "path": path or None,
    }


def clean_path(value: str) -> str:
    """Путь внутри устройства: `/admin/`, `/cgi-bin/…`.

    Ведущие слэши схлопываются: путь вида `//example.com` в ссылке читается
    браузером как другой хост, и кнопка «Открыть» уводила бы на чужой адрес.
    """
    value = CONTROL_RE.sub("", str(value or "").strip())[:300]
    if not value:
        return "/"
    return "/" + value.lstrip("/")


def clean_ports(raw, fallback=(80,)) -> tuple[int, ...]:
    if isinstance(raw, str):
        raw = [p for p in re.split(r"[\s,;]+", raw) if p]
    if raw is None:
        raw = list(fallback)
    ports: list[int] = []
    for item in raw:
        try:
            port = int(item)
        except (TypeError, ValueError):
            raise ValidationError(f"Порт «{item}» — не число")
        if not 1 <= port <= 65535:
            raise ValidationError(f"Порт {port} вне диапазона 1–65535")
        if port not in ports:
            ports.append(port)
    if not ports:
        raise ValidationError("Укажите хотя бы один порт")
    if len(ports) > 12:
        raise ValidationError("Не больше 12 портов на устройство")
    return tuple(ports)


def clean_name(payload: dict, limit: int, what: str) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise ValidationError("Ожидался JSON-объект")
    name = CONTROL_RE.sub("", str(payload.get("name") or "").strip())[:limit]
    if not name:
        raise ValidationError(f"Название {what} не может быть пустым")
    note = CONTROL_RE.sub("", str(payload.get("note") or "").strip())[:400]
    return name, note


def clean_site(payload: dict) -> tuple[str, str]:
    return clean_name(payload, 120, "точки")


def clean_category(payload: dict) -> tuple[str, str]:
    return clean_name(payload, 80, "категории")


def clean_device(payload: dict, existing: Device | None = None) -> Device:
    if not isinstance(payload, dict):
        raise ValidationError("Ожидался JSON-объект")

    def base(field, default=None):
        return getattr(existing, field) if existing is not None else default

    parsed = split_address(str(payload.get("host", base("host", ""))))
    host = parsed.get("host", "")
    if not HOST_RE.fullmatch(host):
        raise ValidationError("Некорректный адрес устройства")

    name = str(payload.get("name", base("name", ""))).strip()[:120] or host
    site = str(payload.get("site", base("site", ""))).strip()[:120] or "Без объекта"
    category = str(payload.get("category", base("category", ""))).strip()[:80]
    note = str(payload.get("note", base("note", ""))).strip()[:400]

    # Схема, вписанная в сам адрес, выигрывает у выпадающего списка: если
    # вставили https-ссылку, менять её на http по умолчанию формы нельзя.
    scheme = parsed.get("scheme") or str(payload.get("scheme", base("scheme", "http"))).strip().lower()
    if scheme not in {"http", "https"}:
        raise ValidationError("Схема должна быть http или https")

    ports = clean_ports(payload.get("ports", base("ports", (80,))))

    web_port = payload.get("web_port")
    if web_port in ("", None):
        web_port = parsed.get("web_port") or base("web_port")
    if web_port in ("", None):
        web_port = None
    else:
        try:
            web_port = int(web_port)
        except (TypeError, ValueError):
            raise ValidationError("Порт веб-интерфейса — не число")
        if not 1 <= web_port <= 65535:
            raise ValidationError("Порт веб-интерфейса вне диапазона 1–65535")

    path = payload.get("path")
    if path in ("", None):
        path = parsed.get("path") or base("path") or "/"

    username = str(payload.get("username", base("username", ""))).strip()[:120]

    # Пароль приходит только при изменении: пустое поле означает «оставить как
    # было», иначе правка любого другого поля стирала бы учётку - наружу
    # пароль не отдаётся, и форма его не знает.
    raw_password = payload.get("password")
    if raw_password is None or raw_password == "":
        password_enc = base("password_enc", "")
    elif raw_password is False:
        password_enc = ""
    else:
        if not AUTH_ENABLED:
            raise ValidationError(
                "Пароли устройств нельзя хранить, пока панель без входа. "
                "Задайте DM_PASSWORD и перезапустите сервис."
            )
        password_enc = obfuscate(str(raw_password)[:200])

    return Device(
        id=base("id") or uuid.uuid4().hex,
        site=site,
        category=category,
        name=name,
        host=host,
        ports=ports,
        scheme=scheme,
        web_port=web_port,
        path=clean_path(path),
        username=username,
        password_enc=password_enc,
        note=note,
    )
