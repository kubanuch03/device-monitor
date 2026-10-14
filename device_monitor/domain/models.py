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
    # Адрес go2rtc объекта (напр. http://10.20.4.2:1983). Браузер не умеет
    # RTSP, поэтому поток показывает медиасервер, а мы только знаем, где он
    # и как называется источник. Пусто = у точки просмотра видео нет.
    stream_base: str = ""
    # Учётка по умолчанию для устройств точки. У камер одного объекта пароль
    # обычно общий, и требование вбить его отдельно на каждую - надёжный
    # способ не завести его нигде и остаться без автоподстановки вообще.
    username: str = ""
    password_enc: str = ""

    @property
    def has_password(self) -> bool:
        return bool(self.password_enc)

    def public(self) -> dict:
        """Представление для API - то же обещание, что у Device.public().

        Раньше точка уходила наружу через __dict__, и это была мина: любое
        новое поле попадало в API само собой, не спросив. Учётка точки как раз
        такое поле, поэтому список полей теперь перечислен явно.
        """
        return {
            "name": self.name,
            "note": self.note,
            "proxy": self.proxy,
            "stream_base": self.stream_base,
            "username": self.username,
            "has_password": self.has_password,
        }


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
    # Адрес для кнопки «Открыть», когда он отличается от адреса проверки.
    # Нужен для устройств за туннелем: проверяем их по реальному адресу в
    # сети объекта, а открываем через локальный проброшенный порт, потому
    # что до реального адреса браузер сам не дойдёт.
    open_url: str = ""
    # Имя источника в go2rtc точки. Пусто = у устройства видео нет
    # (роутер, сервер, коммутатор — им поток не положен).
    stream_name: str = ""
    # Откуда брать учётку: свою или точки. Наследование включается ЯВНО, а не
    # подразумевается по пустому паролю. Иначе пароль от камер сам уехал бы на
    # роутер и коммутаторы, где он не подходит: толку ноль, а неудачные попытки
    # входа и блокировки - вполне настоящие.
    use_site_creds: bool = False

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
            "open_url": self.open_url,
            "stream_name": self.stream_name,
            "use_site_creds": self.use_site_creds,
        }

    def with_fields(self, **changes) -> "Device":
        return replace(self, **changes)


@dataclass
class ProbeResult:
    status: str                       # up | down
    ports: dict[int, bool] = field(default_factory=dict)
    latency_ms: int | None = None
