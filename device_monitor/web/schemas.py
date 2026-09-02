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
    name: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=400)
    proxy: str = Field(default="", max_length=200)

    @field_validator("name", "note", "proxy")
    @classmethod
    def _strip(cls, v: str) -> str:
        return re.sub(r"[\x00-\x1f\x7f]", "", v).strip()

    @field_validator("proxy")
    @classmethod
    def _check_proxy(cls, v: str) -> str:
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


class LoginIn(BaseModel):
    username: str = ""
    password: str = ""


class AccountIn(BaseModel):
    """Смена учётки входа: нужен текущий пароль, новые логин и/или пароль."""
    current_password: str
    username: str | None = Field(default=None, max_length=120)
    new_password: str | None = Field(default=None, min_length=6, max_length=200)
