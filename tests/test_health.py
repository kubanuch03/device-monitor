"""Здоровье камер Dahua: часы и NTP."""

from datetime import datetime

from device_monitor.vendors.dahua import clock_health

NOW = datetime(2026, 11, 9, 16, 20, 20)
NTP_OFF = {"table.NTP.Enable": "false", "table.NTP.Address": "time.windows.com"}
NTP_ON = {"table.NTP.Enable": "true", "table.NTP.Address": "pool.ntp.org"}


def test_camera_behind_with_ntp_off_is_flagged():
    """Реальный случай Торугарт .33 на 09.11: −39 с и NTP выключен."""
    facts = clock_health("2026-11-09 16:19:41", NTP_OFF, now=NOW)
    assert facts["clock"] == "часы отстают на 39 с"
    assert facts["ntp"] == "выключена"
    assert "отстают на 39 с" in facts["health"] and "NTP" in facts["health"]


def test_ntp_off_with_accurate_clock_is_fact_not_warning():
    facts = clock_health("2026-11-09 16:20:19", NTP_OFF, now=NOW)
    assert facts["ntp"] == "выключена" and "health" not in facts


def test_unknown_ntp_format_is_not_reported_as_off():
    facts = clock_health("2026-11-09 16:19:00", {"table.NTP.Address": "x"}, now=NOW)
    assert "ntp" not in facts and "NTP" not in facts["health"]


def test_accurate_clock_with_ntp_is_healthy():
    facts = clock_health("2026-11-09 16:20:20", NTP_ON, now=NOW)
    assert facts["clock"] == "часы точно" and facts["ntp"] == "включена (pool.ntp.org)"
    assert "health" not in facts


def test_two_hours_ahead_like_ala_archa():
    facts = clock_health("2026-11-09 18:20:30", NTP_ON, now=NOW)
    assert facts["health"] == "часы спешат на 2 ч"


def test_small_drift_not_flagged_and_garbage_ignored():
    assert "health" not in clock_health("2026-11-09 16:20:05", NTP_ON, now=NOW)
    assert clock_health("Error", {}, now=NOW) == {}
