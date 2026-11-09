"""Dahua: камеры ITC413, домофоны VTO и прочее с интерфейсом magicBox."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from device_monitor import config
from device_monitor.domain.models import Device
from device_monitor.vendors.base import VendorDriver

# Расхождение часов, с которого камера помечается: время события
# распознавания номера - это данные, и полминуты уже путают сверку проездов.
CLOCK_WARN_SECONDS = 30


def _local_now() -> datetime:
    try:
        tz = ZoneInfo(config.TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone(timedelta(hours=6))
    return datetime.now(tz).replace(tzinfo=None)


def _fmt_drift(seconds: float) -> str:
    value = abs(int(seconds))
    amount = f"{value} с" if value < 120 else f"{value // 60} мин" if value < 7200 else f"{value // 3600} ч"
    if value < 2:
        return "точно"
    return f"спешат на {amount}" if seconds > 0 else f"отстают на {amount}"


def clock_health(camera_time: str, ntp: dict, now: datetime | None = None) -> dict:
    """Сведения о часах камеры. Время камеры сравнивается с местным временем
    сервиса (DM_TZ): камеры объектов выставлены по местному, и события
    распознавания приходят с этим временем."""
    facts: dict = {}
    warnings = []
    try:
        drift = (datetime.strptime(camera_time.strip(), "%Y-%m-%d %H:%M:%S")
                 - (now or _local_now())).total_seconds()
    except (AttributeError, ValueError):
        drift = None
    # Нет ключа Enable (другая прошивка) - «не знаем», а не «выключена».
    ntp_known = "table.NTP.Enable" in ntp
    ntp_on = ntp.get("table.NTP.Enable") == "true"
    if ntp_known:
        address = ntp.get("table.NTP.Address", "")
        facts["ntp"] = f"включена ({address})" if ntp_on else "выключена"
    if drift is not None:
        facts["clock"] = f"часы {_fmt_drift(drift)}"
        # Красная плашка - только когда часы реально ушли. Выключенный NTP при
        # точных часах - просто сведение: камер без интернета много, и плашка
        # на каждой перестала бы читаться.
        if abs(drift) >= CLOCK_WARN_SECONDS:
            warnings.append(f"часы {_fmt_drift(drift)}")
            if ntp_known and not ntp_on:
                warnings.append("синхронизация времени (NTP) выключена")
    if warnings:
        facts["health"] = "; ".join(warnings)
    return facts

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

        # Часы и NTP. Состояние SD-карты (storageDevice.cgi) не спрашиваем:
        # на ITC436 он отвечает «Bad Request» - проверено на Торугарте 09.11.
        current = _kv(cls.fetch(device, "/cgi-bin/global.cgi?action=getCurrentTime", password) or "")
        now = _local_now()   # сразу после ответа камеры, а не после ещё одного запроса
        ntp = _kv(cls.fetch(device, "/cgi-bin/configManager.cgi?action=getConfig&name=NTP",
                            password) or "")
        facts.update(clock_health(current.get("result", ""), ntp, now=now))
        return facts
