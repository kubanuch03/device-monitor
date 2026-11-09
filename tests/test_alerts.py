"""Правила оповещений: подтверждение, падение объекта, окна, повтор, рестарт."""

from datetime import datetime, timedelta, timezone

from device_monitor.alerts.rules import AlertEngine, AlertRules, Observation, render

T0 = datetime(2026, 11, 9, 4, 0, tzinfo=timezone.utc)  # 10:00 по Бишкеку


def obs(i, status, site="Балыкчы"):
    return Observation(f"d{i}", f"Камера {i}", site, f"10.0.0.{i}", status)


def engine(baseline=None, **rules):
    return AlertEngine(AlertRules(**rules), baseline or (lambda _id: (None, None)))


def never_muted(kind, target):
    return False


def run(e, rounds, tunnels=None, muted=never_muted, start=T0, step=30):
    """rounds - список кругов, каждый - список Observation. Возвращает тексты
    сообщений каждого круга."""
    out = []
    for n, observations in enumerate(rounds):
        now = start + timedelta(seconds=step * n)
        out.append([m.text for m in e.evaluate(observations, tunnels or {}, muted, now)])
    return out


def test_single_failure_is_not_an_alert():
    e = engine(down_confirm=3)
    fleet = [obs(i, "up") for i in range(1, 6)]
    flap = [obs(1, "down")] + fleet[1:]
    assert run(e, [fleet, flap, fleet, flap, fleet]) == [[], [], [], [], []]


def test_alert_after_confirm_rounds_then_recovery():
    e = engine(down_confirm=3)
    up = [obs(i, "up") for i in range(1, 6)]
    down = [obs(1, "down")] + up[1:]
    result = run(e, [up, down, down, down, down, up])
    assert result[:3] == [[], [], []]
    assert len(result[3]) == 1 and "Камера 1" in result[3][0] and "не отвечает" in result[3][0]
    assert result[4] == []                       # повторов нет
    assert "снова на связи" in result[5][0] and "простой" in result[5][0]


def test_whole_site_down_sends_one_message():
    e = engine(down_confirm=2, site_outage_ratio=0.7)
    up = [obs(i, "up") for i in range(1, 11)]
    down = [obs(i, "down") for i in range(1, 11)]
    result = run(e, [up, down, down, down, up])
    assert result[1] == []
    assert len(result[2]) == 1 and "Объект недоступен" in result[2][0]
    assert "10 из 10" in result[2][0]
    assert result[3] == []
    assert len(result[4]) == 1 and "Объект снова на связи" in result[4][0]


def test_tunnel_needs_confirmation_too():
    """Один сбой рукопожатия SOCKS - не «объект недоступен»."""
    e = engine(down_confirm=3)
    up = [obs(i, "up") for i in range(1, 4)]
    down = [obs(i, "down") for i in range(1, 4)]
    tunnels_down = {"Балыкчы": {"status": "down", "detail": "SOCKS 127.0.0.1:1081 недоступен"}}
    blip = []
    for observations, tunnels in [(down, tunnels_down), (up, {}), (up, {})]:
        blip.append([m.text for m in e.evaluate(observations, tunnels, never_muted, T0)])
    assert blip == [[], [], []]
    lasting = run(e, [down, down, down], tunnels=tunnels_down)
    assert lasting[:2] == [[], []]
    assert len(lasting[2]) == 1 and "туннель" in lasting[2][0]


def test_site_outage_has_hysteresis():
    """Около порога объект не мигает «недоступен / на связи» каждый круг."""
    e = engine(down_confirm=1, site_outage_ratio=0.7)
    seven = [obs(i, "down") for i in range(1, 8)] + [obs(i, "up") for i in range(8, 11)]
    six = [obs(i, "down") for i in range(1, 7)] + [obs(i, "up") for i in range(7, 11)]
    four = [obs(i, "down") for i in range(1, 5)] + [obs(i, "up") for i in range(5, 11)]
    result = run(e, [seven, six, seven, six, four])
    assert "Объект недоступен" in result[0][0]
    assert result[1:4] == [[], [], []]
    assert "Объект снова на связи" in result[4][0]


def test_site_recovery_lists_devices_still_down_and_reports_them_later():
    e = engine(down_confirm=1, site_outage_ratio=0.7)
    down = [obs(i, "down") for i in range(1, 5)]
    partly = [obs(1, "down")] + [obs(i, "up") for i in range(2, 5)]
    up = [obs(i, "up") for i in range(1, 5)]
    result = run(e, [down, partly, up])
    assert "Объект недоступен" in result[0][0]
    assert "Объект снова на связи" in result[1][0] and "Не отвечают: Камера 1" in result[1][0]
    assert len(result[2]) == 1 and "Камера 1" in result[2][0] and "снова на связи" in result[2][0]


def test_maintenance_window_is_silent_and_does_not_burst_after():
    e = engine(down_confirm=2)
    up = [obs(i, "up") for i in range(1, 6)]
    down = [obs(1, "down")] + up[1:]

    def site_muted(kind, target):
        return kind == "site" and target == "Балыкчы"

    muted_rounds = run(e, [up, down, down, down], muted=site_muted)
    assert muted_rounds == [[], [], [], []]
    # Окно кончилось, а устройство всё ещё лежит - одно сообщение об этом.
    after = run(e, [down, down, up], start=T0 + timedelta(minutes=5))
    assert len(after[0]) == 1 and "всё ещё не отвечает" in after[0][0]
    assert after[1] == []
    assert "снова на связи" in after[2][0]


def test_recovery_inside_window_is_silent():
    e = engine(down_confirm=1)
    up = [obs(i, "up") for i in range(1, 6)]
    down = [obs(1, "down")] + up[1:]
    quiet = run(e, [down, up], muted=lambda kind, target: target == "d1")
    assert quiet == [[], []]
    assert run(e, [up], start=T0 + timedelta(minutes=5)) == [[]]


def test_device_mute_only_affects_that_device():
    e = engine(down_confirm=1)
    both_down = [obs(1, "down"), obs(2, "down")] + [obs(i, "up") for i in range(3, 8)]
    result = run(e, [both_down], muted=lambda kind, target: kind == "device" and target == "d1")
    assert len(result[0]) == 1 and "Камера 2" in result[0][0]


def test_repeat_for_long_outage():
    e = engine(down_confirm=1, repeat_hours=1)
    down = [obs(1, "down")] + [obs(i, "up") for i in range(2, 6)]
    result = run(e, [down, down, down], step=40 * 60)   # круги раз в 40 минут
    assert "не отвечает" in result[0][0]
    assert result[1] == []                                # прошло 40 мин < 1 ч
    assert "всё ещё не отвечает" in result[2][0]


def test_restart_does_not_realert_known_outage():
    """После рестарта: лежавшее до него устройство (в истории down) - без
    повторной тревоги, а его подъём - сообщается."""
    e = engine(baseline=lambda device_id: ("down", "2026-11-09T03:00:00+00:00") if device_id == "d1"
               else ("up", None), down_confirm=2)
    down = [obs(1, "down")] + [obs(i, "up") for i in range(2, 6)]
    up = [obs(i, "up") for i in range(1, 6)]
    result = run(e, [down, down, down, up])
    assert result[:3] == [[], [], []]
    assert "снова на связи" in result[3][0]
    assert "простой 1 ч" in result[3][0]   # длительность - от записи в истории


def test_small_site_never_counts_as_whole_outage():
    """У точки из двух устройств падение обоих - это два сообщения про
    устройства, а не «объект недоступен»."""
    e = engine(down_confirm=1, site_outage_min=3)
    result = run(e, [[obs(1, "down", "Достук"), obs(2, "down", "Достук")]])
    assert len(result[0]) == 2 and all("не отвечает" in t for t in result[0])


def test_render_groups_by_site_and_splits_long():
    e = engine(down_confirm=1)
    msgs = e.evaluate([obs(1, "down", "А"), obs(2, "down", "Б")] +
                      [obs(i, "up", "А") for i in range(3, 9)] +
                      [obs(i, "up", "Б") for i in range(9, 15)], {}, never_muted, T0)
    texts = render(msgs)
    assert len(texts) == 1 and texts[0].startswith("А\n") and "\n\nБ\n" in texts[0]
    long = render(msgs * 200, limit=500)
    assert len(long) > 1 and all(len(t) <= 500 for t in long)
    # Ни одна строка не потеряна и не разрезана.
    sent = [line for text in long for line in text.split("\n")]
    for m in msgs * 200:
        assert m.text in sent
    assert sum(line.startswith("🔴") for line in sent) == 400


def test_time_in_message_is_local():
    from zoneinfo import ZoneInfo
    e = AlertEngine(AlertRules(down_confirm=1), lambda _id: (None, None), tz=ZoneInfo("Asia/Bishkek"))
    msgs = e.evaluate([obs(1, "down")] + [obs(i, "up") for i in range(2, 6)], {}, never_muted, T0)
    assert "с 10:00" in msgs[0].text


# -- API настроек и окон обслуживания ------------------------------------------

def test_alert_settings_token_is_write_only(auth):
    token = "123456789:" + "A" * 35
    r = auth.put("/api/v1/alerts", json={"telegram_token": token, "chat_id": "-1001234567890",
                                         "chat_title": "Дежурные", "down_confirm": 4})
    assert r.status_code == 200
    body = auth.get("/api/v1/alerts").json()
    assert body["has_token"] is True and body["chat_id"] == "-1001234567890"
    assert body["down_confirm"] == 4
    assert token not in auth.get("/api/v1/alerts").text
    assert token not in auth.get("/api/v1/state").text
    assert auth.get("/api/v1/state").json()["alerts_configured"] is True
    # Пустой токен в следующем PUT не стирает сохранённый, False - стирает.
    auth.put("/api/v1/alerts", json={"down_confirm": 2})
    assert auth.get("/api/v1/alerts").json()["has_token"] is True
    auth.put("/api/v1/alerts", json={"telegram_token": False})
    assert auth.get("/api/v1/alerts").json()["has_token"] is False


def test_alert_settings_validation(auth):
    assert auth.put("/api/v1/alerts", json={"telegram_token": "не токен"}).status_code == 400
    assert auth.put("/api/v1/alerts", json={"chat_id": "https://t.me/+abc"}).status_code == 400
    assert auth.put("/api/v1/alerts", json={"down_confirm": 0}).status_code == 400
    assert auth.put("/api/v1/alerts", json={"heartbeat_url": "ftp://x"}).status_code == 400


def test_alert_endpoints_need_login(client):
    assert client.get("/api/v1/alerts").status_code == 401
    assert client.post("/api/v1/alerts/test").status_code == 401


def test_test_message_without_token_is_clear_error(auth):
    r = auth.post("/api/v1/alerts/test")
    assert r.status_code == 400 and "токен" in r.json()["detail"]


def test_mute_site_shows_in_state_and_clears(auth):
    auth.post("/api/v1/sites", json={"name": "Балыкчы"})
    r = auth.post("/api/v1/mutes", json={"kind": "site", "target": "Балыкчы", "minutes": 60})
    assert r.status_code == 200 and "site:Балыкчы" in r.json()["mutes"]
    site = auth.get("/api/v1/state").json()["sites"][0]
    assert site["muted_until"]
    auth.post("/api/v1/mutes", json={"kind": "site", "target": "Балыкчы", "minutes": 0})
    assert auth.get("/api/v1/state").json()["sites"][0]["muted_until"] is None


def test_mute_unknown_target_404(auth):
    r = auth.post("/api/v1/mutes", json={"kind": "device", "target": "нет", "minutes": 10})
    assert r.status_code == 404


def test_service_sends_through_queue(tmp_path, monkeypatch):
    """Сквозь сервис: подтверждённое падение уходит в Telegram с токеном и
    чатом из настроек (сеть подменена)."""
    from device_monitor.alerts import service, telegram
    from device_monitor.storage.sqlalchemy_store import SqlAlchemyStorage

    sent = []
    monkeypatch.setattr(telegram, "send_message", lambda token, chat, text: sent.append((chat, text)))
    svc = service.AlertService(SqlAlchemyStorage(tmp_path / "m.db"), run_sender=False)
    svc.update_settings({"telegram_token": "123456789:" + "B" * 35, "chat_id": "-100500",
                         "down_confirm": 1})
    texts = svc.process([obs(1, "down")] + [obs(i, "up") for i in range(2, 6)], {}, now=T0)
    assert len(texts) == 1
    job, _what = svc.sender._queue.get_nowait()
    job()
    assert sent == [("-100500", texts[0])]



def test_site_recovery_then_no_immediate_repeat():
    e = engine(down_confirm=1, repeat_hours=1)
    down = [obs(i, "down") for i in range(1, 4)]
    partly = [obs(1, "down"), obs(2, "up"), obs(3, "up")]
    result = run(e, [down, partly, partly], step=2 * 3600)
    assert "Объект снова на связи" in result[1][0]
    assert len(result[2]) == 1 and "всё ещё не отвечает" in result[2][0]  # через 2 ч, не сразу


def test_device_down_on_site_recovery_round_is_not_duplicated():
    e = engine(down_confirm=1)
    all_down = [obs(i, "down") for i in range(1, 4)]
    one_down = [obs(1, "up"), obs(2, "up"), obs(3, "down")]
    result = run(e, [all_down, one_down, one_down])
    assert "Не отвечают: Камера 3" in result[1][0]
    assert result[2] == []


def test_renamed_site_does_not_inherit_old_outage():
    e = engine(down_confirm=1)
    run(e, [[obs(i, "down", "Старая") for i in range(1, 4)]])
    run(e, [[obs(i, "up", "Новая") for i in range(1, 4)]])
    result = run(e, [[obs(i, "up", "Старая") for i in range(4, 7)]])
    assert result == [[]]


def test_undeliverable_then_configured_reports_ongoing_outage(tmp_path, monkeypatch):
    """Пока токена нет - молча; после настройки - одно «всё ещё не отвечает»,
    а не одинокое «снова на связи» по неизвестному падению."""
    from device_monitor.alerts import service, telegram
    from device_monitor.storage.sqlalchemy_store import SqlAlchemyStorage

    monkeypatch.setattr(telegram, "send_message", lambda *a: None)
    svc = service.AlertService(SqlAlchemyStorage(tmp_path / "m.db"), run_sender=False)
    svc.update_settings({"down_confirm": 1})
    fleet = [obs(1, "down")] + [obs(i, "up") for i in range(2, 6)]
    assert svc.process(fleet, {}, now=T0) == []
    svc.update_settings({"telegram_token": "123456789:" + "C" * 35, "chat_id": "-1"})
    texts = svc.process(fleet, {}, now=T0 + timedelta(minutes=1))
    assert len(texts) == 1 and "всё ещё не отвечает" in texts[0]


def test_heartbeat_does_not_use_alert_queue(tmp_path, monkeypatch):
    from device_monitor.alerts import service
    from device_monitor.storage.sqlalchemy_store import SqlAlchemyStorage

    calls = []
    monkeypatch.setattr(service._Heartbeat, "ping", lambda self, url: calls.append(url))
    svc = service.AlertService(SqlAlchemyStorage(tmp_path / "m.db"), run_sender=False)
    svc.update_settings({"heartbeat_url": "https://hc.example/abc"})
    svc.tick()
    assert calls == ["https://hc.example/abc"]
    assert svc.sender._queue.empty()


def test_down_during_restart_alerts_with_real_monitor(tmp_path, monkeypatch):
    """Сквозь настоящий Monitor и базу: устройство было «up» в истории, после
    рестарта лежит - тревога приходит (раньше baseline читался уже после
    записи текущего круга, и тревоги не было никогда)."""
    from device_monitor.alerts import service, telegram
    from device_monitor.domain.models import Device, ProbeResult
    from device_monitor.probing import monitor as monitor_mod
    from device_monitor.storage.sqlalchemy_store import SqlAlchemyStorage

    storage = SqlAlchemyStorage(tmp_path / "m.db")
    for i in range(1, 6):
        storage.add_device(Device(id=f"{i:032x}", site="Балыкчы", category="", name=f"Камера {i}",
                                  host=f"10.0.0.{i}", ports=(80,)))
        storage.record_status(f"{i:032x}", "up", "2026-11-09T03:00:00+00:00")

    sent = []
    monkeypatch.setattr(telegram, "send_message", lambda token, chat, text: sent.append(text))
    monkeypatch.setattr(monitor_mod, "probe", lambda device, proxy: ProbeResult(
        status="down" if device.host == "10.0.0.1" else "up", ports={80: device.host != "10.0.0.1"}))
    monkeypatch.setattr(monitor_mod.Monitor, "_refresh_facts", lambda self, devices: None)

    alerts = service.AlertService(storage, run_sender=False)
    alerts.update_settings({"telegram_token": "123456789:" + "D" * 35, "chat_id": "-1",
                            "down_confirm": 2})
    mon = monitor_mod.Monitor(storage, alerts)   # «после рестарта»: память пустая
    mon.run_once()
    mon.run_once()
    while not alerts.sender._queue.empty():
        job, _ = alerts.sender._queue.get_nowait()
        job()
    assert len(sent) == 1 and "Камера 1" in sent[0] and "не отвечает" in sent[0]
