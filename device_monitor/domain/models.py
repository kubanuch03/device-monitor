"""Модели предметной области.

Обычные dataclass-ы без единого упоминания SQL, HTTP и файлов: они описывают,
что такое устройство, точка и категория, и не должны меняться от того, где это
хранится и как отдаётся наружу.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Site:
    name: str
    note: str = ""
    # SOCKS-прокси для объектов без прямого маршрута (напр. socks5://127.0.0.1:1080).
    # Пусто = прямой доступ. Все устройства точки проверяются через него.
    proxy: str = ""


@dataclass(frozen=True)
class Category:
    site: str
    name: str
    note: str = ""


@dataclass(frozen=True)
class Device:
    id: str
    site: str
    name: str
    host: str
    ports: tuple[int, ...]
    category: str = ""
    scheme: str = "http"
    web_port: int | None = None
    path: str = "/"
    username: str = ""
    password_enc: str = ""
    note: str = ""

    @property
    def url(self) -> str:
        default = 80 if self.scheme == "http" else 443
        suffix = f":{self.web_port}" if self.web_port and self.web_port != default else ""
        return f"{self.scheme}://{self.host}{suffix}{self.path}"

    @property
    def has_password(self) -> bool:
        return bool(self.password_enc)

    def public(self) -> dict:
        """Представление для API.

        Пароль не попадает сюда ни в каком виде - ни открытым текстом, ни
        закодированным. Это единственная точка, где устройство превращается в
        JSON, поэтому забыть про пароль в каком-то одном эндпоинте невозможно.
        """
        return {
            "id": self.id,
            "site": self.site,
            "category": self.category,
            "name": self.name,
            "host": self.host,
            "ports": list(self.ports),
            "scheme": self.scheme,
            "web_port": self.web_port,
            "path": self.path,
            "username": self.username,
            "has_password": self.has_password,
            "note": self.note,
            "url": self.url,
        }

    def with_fields(self, **changes) -> "Device":
        return replace(self, **changes)


@dataclass
class ProbeResult:
    status: str                       # up | down
    ports: dict[int, bool] = field(default_factory=dict)
    latency_ms: int | None = None
