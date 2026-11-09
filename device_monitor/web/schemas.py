"""Pydantic-схемы запросов и ответов.

Заменяют ручную валидацию: типы, границы и обязательность описаны декларативно,
а FastAPI сам отдаёт 422 с понятным телом при нарушении и строит по ним OpenAPI.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from device_monitor.probing.socks import ProxyError, parse_proxy

HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")


class SiteIn(BaseModel):
    """None у любого поля, кроме имени, означает «не менять» при правке точки,
    поэтому роут передаёт model_dump(exclude_none=True). Дефолт "" здесь был бы
    ошибкой: правка одного поля молча стирала бы прокси и заметку точки."""
    name: str = Field(min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=400)
    proxy: str | None = Field(default=None, max_length=200)
    stream_base: str | None = Field(default=None, max_length=200)
    username: str | None = Field(default=None, max_length=120)
    # None/"" — не менять; False — забыть; строка — задать (как у устройства).
    password: str | bool | None = None

    @field_validator("name", "note", "proxy")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return None if v is None else re.sub(r"[\x00-\x1f\x7f]", "", v).strip()

    @field_validator("proxy")
    @classmethod
    def _check_proxy(cls, v: str | None) -> str | None:
        if v:
            try:
                parse_proxy(v)
            except ProxyError as exc:
                raise ValueError(f"Прокси: {exc}")
        return v


class CategoryIn(BaseModel):
    site: str = Field(default="", max_length=120)
    name: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=400)

    @field_validator("name", "note", "site")
    @classmethod
    def _strip(cls, v: str) -> str:
        return re.sub(r"[\x00-\x1f\x7f]", "", v).strip()


class DeviceIn(BaseModel):
    """Все поля необязательны — обязательность адреса, разбор вставленной ссылки,
    портов-строки и логика пароля живут в clean_device: при создании он требует
    адрес, при обновлении мержит с существующим устройством. Поэтому дефолтов
    здесь нет — иначе дефолт затирал бы разобранное из URL и ломал частичный PUT.
    """

    site: str | None = Field(default=None, max_length=120)
    category: str | None = Field(default=None, max_length=80)
    name: str | None = Field(default=None, max_length=120)
    host: str | None = None
    ports: list[int] | str | None = None
    scheme: str | None = None
    web_port: int | None = None
    path: str | None = None
    username: str | None = Field(default=None, max_length=120)
    # None/"" — не менять; False — забыть; строка — задать. Логика в clean_device.
    password: str | bool | None = None
    note: str | None = Field(default=None, max_length=400)
    open_url: str | None = Field(default=None, max_length=300)
    stream_name: str | None = Field(default=None, max_length=120)
    use_site_creds: bool | None = None


class AlertSettingsIn(BaseModel):
    """Всё необязательно: присланное поле меняется, остальное - как было.
    telegram_token: строка - задать, False - убрать (как пароль устройства)."""
    enabled: bool | None = None
    telegram_token: str | bool | None = Field(default=None)
    chat_id: str | None = Field(default=None, max_length=64)
    chat_title: str | None = Field(default=None, max_length=120)
    down_confirm: int | None = None
    site_outage_ratio: float | None = None
    repeat_hours: float | None = None
    heartbeat_url: str | None = Field(default=None, max_length=300)


class ScanIn(BaseModel):
    subnet: str = Field(min_length=1, max_length=43)


class AssistantIn(BaseModel):
    base_url: str | None = Field(default=None, max_length=300)
    model: str | None = Field(default=None, max_length=120)
    api_key: str | bool | None = None
    enabled: bool | None = None


class MuteIn(BaseModel):
    kind: str = Field(pattern="^(site|device)$")
    target: str = Field(min_length=1, max_length=120)
    minutes: int | None = None   # None или 0 - снять окно


class LoginIn(BaseModel):
    username: str = ""
    password: str = ""


class AccountIn(BaseModel):
    """Смена учётки входа: нужен текущий пароль, новые логин и/или пароль."""
    current_password: str
    username: str | None = Field(default=None, max_length=120)
    new_password: str | None = Field(default=None, min_length=6, max_length=200)
