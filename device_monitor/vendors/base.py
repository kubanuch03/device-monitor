"""Контракт вендорного драйвера.

Добавить поддержку нового производителя = добавить файл в этот пакет и
зарегистрировать класс. Ни опрос, ни веб, ни хранилище при этом не меняются -
это то самое «закрыто для изменения, открыто для расширения».
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from typing import ClassVar

from device_monitor.config import INSPECT_TIMEOUT
from device_monitor.domain.models import Device


class VendorDriver:
    """Базовый драйвер: умеет ходить на устройство и разбирать ответы.

    Наследник обязан объявить `name` и реализовать `detect` и `collect`.
    """

    name: ClassVar[str] = "unknown"

    # -- сеть -----------------------------------------------------------------

    @staticmethod
    def url(device: Device, path: str) -> str:
        default = 80 if device.scheme == "http" else 443
        suffix = f":{device.web_port}" if device.web_port and device.web_port != default else ""
        return f"{device.scheme}://{device.host}{suffix}{path}"

    @classmethod
    def challenge(cls, device: Device, path: str) -> str:
        """realm из ответа 401 — читается вообще без учётных данных.

        И Dahua, и Hikvision раскрывают себя в этом заголовке до всякой
        авторизации, поэтому определение производителя ничего не стоит и
        работает даже на устройствах, пароль от которых неизвестен.
        """
        try:
            urllib.request.urlopen(cls.url(device, path), timeout=INSPECT_TIMEOUT)
        except urllib.error.HTTPError as exc:
            header = exc.headers.get("WWW-Authenticate") or ""
            match = re.search(r'realm="([^"]*)"', header)
            return match.group(1) if match else ""
        except (urllib.error.URLError, OSError, ValueError):
            return ""
        return ""

    @classmethod
    def fetch(cls, device: Device, path: str, password: str) -> str | None:
        manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        url = cls.url(device, path)
        manager.add_password(None, url, device.username, password)
        opener = urllib.request.build_opener(
            urllib.request.HTTPDigestAuthHandler(manager),
            urllib.request.HTTPBasicAuthHandler(manager),
        )
        try:
            with opener.open(url, timeout=INSPECT_TIMEOUT) as response:
                return response.read(64 * 1024).decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, ValueError):
            return None

    # -- контракт --------------------------------------------------------------

    @classmethod
    def detect(cls, device: Device) -> dict | None:
        """Если устройство наше — вернуть начальные сведения, иначе None."""
        raise NotImplementedError

    @classmethod
    def collect(cls, device: Device, password: str) -> dict:
        """Полные сведения. Вызывается только когда учётка заведена."""
        raise NotImplementedError
