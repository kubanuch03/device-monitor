"""Dahua: камеры ITC413, домофоны VTO и прочее с интерфейсом magicBox."""

from __future__ import annotations

from device_monitor.domain.models import Device
from device_monitor.vendors.base import VendorDriver

PROBE_PATH = "/cgi-bin/magicBox.cgi?action=getDeviceType"


def _kv(text: str) -> dict:
    """Ответы Dahua — plain text вида ключ=значение построчно."""
    out = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


class DahuaDriver(VendorDriver):
    name = "Dahua"

    @classmethod
    def detect(cls, device: Device) -> dict | None:
        realm = cls.challenge(device, PROBE_PATH)
        if not realm.startswith("Login to "):
            return None
        # Хеш в realm свой у каждого устройства и не меняется при смене IP -
        # единственный способ опознать конкретную камеру, не зная пароля.
        # После очередной перераздачи DHCP по нему видно, кто куда переехал.
        return {"vendor": cls.name, "fingerprint": realm[len("Login to "):]}

    @classmethod
    def collect(cls, device: Device, password: str) -> dict:
        facts: dict = {}
        info = _kv(cls.fetch(device, "/cgi-bin/magicBox.cgi?action=getSystemInfo", password) or "")
        if not info:
            return {"detail": "устройство не приняло учётные данные"}

        facts["model"] = info.get("deviceType")
        facts["serial"] = info.get("serialNumber")
        facts["hardware"] = info.get("hardwareVersion")

        version = _kv(cls.fetch(device, "/cgi-bin/magicBox.cgi?action=getSoftwareVersion", password) or "")
        if version.get("version"):
            facts["firmware"] = version["version"]

        net = _kv(cls.fetch(device, "/cgi-bin/netApp.cgi?action=getInterfaces", password) or "")
        for key, value in net.items():
            if key.endswith(".PhysicalAddress") and value:
                facts["mac"] = value
                break

        return facts
