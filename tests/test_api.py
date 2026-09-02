"""API-тесты через FastAPI TestClient."""


def test_auth_required(client):
    assert client.get("/api/v1/state").status_code == 401
    assert client.get("/api/v1/health").status_code == 200  # health открыт


def test_login_wrong_password(client):
    assert client.post("/api/v1/login", json={"username": "admin", "password": "nope"}).status_code == 401


def test_login_wrong_username(client):
    assert client.post("/api/v1/login", json={"username": "nope", "password": "test-secret"}).status_code == 401


def test_change_account(auth):
    # неверный текущий пароль — отказ
    assert auth.post("/api/v1/account", json={"current_password": "wrong", "new_password": "brandnew1"}).status_code == 403
    # смена пароля
    assert auth.post("/api/v1/account", json={"current_password": "test-secret", "new_password": "brandnew1"}).status_code == 200
    # старый больше не работает, новый работает
    fresh = auth
    assert fresh.post("/api/v1/login", json={"username": "admin", "password": "test-secret"}).status_code == 401
    assert fresh.post("/api/v1/login", json={"username": "admin", "password": "brandnew1"}).status_code == 200


def test_site_category_device_flow(auth):
    assert auth.post("/api/v1/sites", json={"name": "Объект-1"}).status_code == 200
    assert auth.post("/api/v1/categories", json={"site": "Объект-1", "name": "Камеры"}).status_code == 200
    r = auth.post("/api/v1/devices", json={
        "site": "Объект-1", "category": "Камеры", "name": "Камера",
        "host": "http://10.0.0.5:8080/admin/", "ports": "80,554",
    })
    assert r.status_code == 200
    dev = r.json()["device"]
    # разбор вставленной ссылки
    assert dev["web_port"] == 8080 and dev["path"] == "/admin/"
    assert dev["ports"] == [80, 554]

    state = auth.get("/api/v1/state").json()
    assert "Объект-1" in [s["name"] for s in state["sites"]]


def test_duplicate_site_unicode(auth):
    auth.post("/api/v1/sites", json={"name": "Точка"})
    r = auth.post("/api/v1/sites", json={"name": "ТОЧКА"})  # регистр, кириллица
    assert r.status_code == 400


def test_delete_nonempty_site_blocked(auth):
    auth.post("/api/v1/sites", json={"name": "С-Устройством"})
    auth.post("/api/v1/devices", json={"site": "С-Устройством", "host": "10.0.0.9", "ports": "80"})
    r = auth.delete("/api/v1/sites/%D0%A1-%D0%A3%D1%81%D1%82%D1%80%D0%BE%D0%B9%D1%81%D1%82%D0%B2%D0%BE%D0%BC")
    assert r.status_code == 400


def test_password_write_only(auth):
    r = auth.post("/api/v1/devices", json={"site": "P", "host": "10.0.0.1", "ports": "80",
                                           "username": "admin", "password": "hunter2"})
    dev_id = r.json()["device"]["id"]
    # в state пароля нет, только признак
    state = auth.get("/api/v1/state").json()
    dev = next(d for d in state["devices"] if d["id"] == dev_id)
    assert dev["has_password"] is True
    assert "hunter2" not in str(dev)
    # secret отдаёт по отдельному запросу
    secret = auth.get(f"/api/v1/devices/{dev_id}/secret").json()
    assert secret["password"] == "hunter2"


def test_partial_update_keeps_fields(auth):
    r = auth.post("/api/v1/devices", json={"site": "U", "name": "Имя", "host": "10.0.0.2", "ports": "80"})
    dev_id = r.json()["device"]["id"]
    # частичный PUT только пароль — имя и адрес не затираются
    auth.put(f"/api/v1/devices/{dev_id}", json={"password": "pw"})
    state = auth.get("/api/v1/state").json()
    dev = next(d for d in state["devices"] if d["id"] == dev_id)
    assert dev["name"] == "Имя" and dev["host"] == "10.0.0.2" and dev["has_password"] is True


def test_create_device_requires_host(auth):
    assert auth.post("/api/v1/devices", json={"site": "X", "name": "Y"}).status_code == 400


def test_proxy_validation(auth):
    assert auth.post("/api/v1/sites", json={"name": "Прокси-плохой", "proxy": "http://x:1"}).status_code == 422
    assert auth.post("/api/v1/sites", json={"name": "Прокси-ок", "proxy": "socks5://127.0.0.1:1081"}).status_code == 200
