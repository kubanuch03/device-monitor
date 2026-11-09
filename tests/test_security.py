"""Фаза 0: пароль первого входа, перебор, сброс сессий, кэш и /docs."""

import stat

from fastapi.testclient import TestClient


def _login(client, password="test-secret"):
    return client.post("/api/v1/login", json={"username": "admin", "password": password})


def test_login_locked_after_limit_even_with_right_password(client):
    from device_monitor import config
    for _ in range(config.LOGIN_FAIL_LIMIT):
        assert _login(client, "wrong").status_code == 401
    r = _login(client)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1


def test_success_resets_fail_counter(client):
    from device_monitor import config
    for _ in range(config.LOGIN_FAIL_LIMIT - 1):
        _login(client, "wrong")
    assert _login(client).status_code == 200
    # Счётчик обнулился - снова можно ошибиться limit-1 раз без блокировки.
    for _ in range(config.LOGIN_FAIL_LIMIT - 1):
        assert _login(client, "wrong").status_code == 401
    assert _login(client).status_code == 200


def test_password_change_kicks_other_sessions(client):
    from device_monitor.web.fastapi_app import app
    with TestClient(app) as other:
        assert _login(other).status_code == 200
        assert _login(client).status_code == 200
        r = client.post("/api/v1/account", json={
            "current_password": "test-secret", "new_password": "new-secret-1"})
        assert r.status_code == 200
        assert client.get("/api/v1/state").status_code == 200   # своя сессия жива
        assert other.get("/api/v1/state").status_code == 401    # чужая выкинута


def test_api_responses_not_cached(auth):
    assert auth.get("/api/v1/state").headers["cache-control"] == "no-store"


def test_docs_disabled(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_first_start_generates_password(tmp_path, monkeypatch):
    from device_monitor import config
    from device_monitor.app import App
    from device_monitor.security import verify_password
    from device_monitor.storage.sqlalchemy_store import SqlAlchemyStorage

    monkeypatch.setattr(config, "DEFAULT_PASSWORD", "")
    monkeypatch.setattr(config, "FIRST_LOGIN_FILE", tmp_path / "first-login.txt")
    application = App(SqlAlchemyStorage(tmp_path / "monitor.db"))

    text = config.FIRST_LOGIN_FILE.read_text("utf-8")
    password = text.split("пароль: ")[1].splitlines()[0]
    assert len(password) >= 12
    assert stat.S_IMODE(config.FIRST_LOGIN_FILE.stat().st_mode) == 0o600
    assert verify_password(password, application.storage.get_user()["password_hash"])


def test_parallel_attempts_cannot_exceed_limit():
    """Резерв попытки атомарный: пачка параллельных запросов не проходит
    больше limit (раньше проходили все - проверка и запись были раздельными)."""
    import threading
    from device_monitor.security import LoginThrottle

    throttle = LoginThrottle(limit=5, window=60)
    allowed = []
    barrier = threading.Barrier(20)

    def attempt():
        barrier.wait()
        allowed.append(throttle.acquire("login:1.2.3.4") == 0)

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(allowed) == 5


def test_stale_keys_are_pruned():
    from device_monitor.security import LoginThrottle

    throttle = LoginThrottle(limit=5, window=60)
    throttle.MAX_KEYS = 3
    for i in range(3):
        throttle._fails[f"old:{i}"] = [0.0]   # метки из 1970 года
    throttle.acquire("fresh")
    assert set(throttle._fails) == {"fresh"}


def test_uvicorn_does_not_trust_forwarded_headers(monkeypatch):
    """Иначе клиент на 127.0.0.1 подставляет X-Forwarded-For и обходит лимит."""
    import uvicorn
    import monitor

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: seen.update(kw))
    monitor.main()
    assert seen["proxy_headers"] is False


def test_first_login_file_recreated_private(tmp_path, monkeypatch):
    """Оставшийся от прошлой установки файл с 644 не должен сохранить права."""
    from device_monitor import config
    from device_monitor.app import App
    from device_monitor.storage.sqlalchemy_store import SqlAlchemyStorage

    leftover = tmp_path / "first-login.txt"
    leftover.write_text("старое", "utf-8")
    leftover.chmod(0o644)
    monkeypatch.setattr(config, "DEFAULT_PASSWORD", "")
    monkeypatch.setattr(config, "FIRST_LOGIN_FILE", leftover)
    App(SqlAlchemyStorage(tmp_path / "monitor.db"))
    assert stat.S_IMODE(leftover.stat().st_mode) == 0o600
    assert "старое" not in leftover.read_text("utf-8")


def test_first_login_password_not_in_log(tmp_path, monkeypatch, capsys):
    from device_monitor import config
    from device_monitor.app import App
    from device_monitor.storage.sqlalchemy_store import SqlAlchemyStorage

    monkeypatch.setattr(config, "DEFAULT_PASSWORD", "")
    monkeypatch.setattr(config, "FIRST_LOGIN_FILE", tmp_path / "first-login.txt")
    App(SqlAlchemyStorage(tmp_path / "monitor.db"))
    password = config.FIRST_LOGIN_FILE.read_text("utf-8").split("пароль: ")[1].split()[0]
    assert password not in capsys.readouterr().out
