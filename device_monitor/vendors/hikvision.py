"""Hikvision: коммутаторы DS-3E, камеры и всё с интерфейсом ISAPI."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from device_monitor.domain.models import Device
from device_monitor.vendors.base import VendorDriver

PROBE_PATH = "/ISAPI/System/deviceInfo"


class HikvisionDriver(VendorDriver):
    name = "Hikvision"

    @classmethod
    def detect(cls, device: Device) -> dict | None:
        realm = cls.challenge(device, PROBE_PATH)
        if not realm or realm.startswith("Login to "):
            return None
        # Hikvision кладёт в realm прямо модель устройства, поэтому она
        # известна ещё до авторизации - у Dahua там обезличенный хеш.
        return {"vendor": cls.name, "model": realm}

    @classmethod
    def collect(cls, device: Device, password: str) -> dict:
        body = cls.fetch(device, PROBE_PATH, password)
        if not body:
            return {"detail": "устройство не приняло учётные данные"}
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return {"detail": "устройство ответило неразборчиво"}

        # ISAPI отдаёт XML с namespace, и он различается между прошивками -
        # сравнивать надо по локальному имени тега, иначе разбор ломается
        # на первом же устройстве с другой версией.
        found = {tag.tag.rsplit("}", 1)[-1]: (tag.text or "").strip() for tag in root}
        return {
            "model": found.get("model"),
            "serial": found.get("serialNumber"),
            "mac": found.get("macAddress"),
            "firmware": found.get("firmwareVersion"),
            "released": found.get("firmwareReleasedDate"),
            "device_name": found.get("deviceName"),
        }
