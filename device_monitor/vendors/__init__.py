"""Реестр вендорных драйверов.

Новый производитель добавляется одной строкой в DRIVERS и своим файлом рядом.
Порядок в списке — порядок опроса при определении: первый, кто узнал
устройство, тот его и обслуживает.
"""

from __future__ import annotations

from device_monitor.domain.models import Device
from device_monitor.vendors.base import VendorDriver
from device_monitor.vendors.dahua import DahuaDriver
from device_monitor.vendors.hikvision import HikvisionDriver

DRIVERS: list[type[VendorDriver]] = [DahuaDriver, HikvisionDriver]


def identify(device: Device) -> tuple[type[VendorDriver] | None, dict]:
    for driver in DRIVERS:
        found = driver.detect(device)
        if found:
            return driver, found
    return None, {}
