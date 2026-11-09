"""Фаза 2: пароль точки виден «глазом», форма устройства не путает свою
учётку с учёткой точки, пароль можно удалить."""


def _site_and_device(auth, inherit=True):
    auth.post("/api/v1/sites", json={"name": "Балыкчы", "username": "site-user",
                                     "password": "site-pass"})
    r = auth.post("/api/v1/devices", json={
        "site": "Балыкчы", "name": "Камера", "host": "10.0.0.5", "ports": [80],
        "use_site_creds": inherit})
    return r.json()["device"]["id"]


def test_site_secret_endpoint(auth):
    _site_and_device(auth)
    body = auth.get("/api/v1/sites/Балыкчы/secret").json()
    assert body == {"username": "site-user", "password": "site-pass", "has_password": True}
    assert auth.get("/api/v1/sites/Нет такой/secret").status_code == 404


def test_site_secret_needs_login(client):
    assert client.get("/api/v1/sites/Балыкчы/secret").status_code == 401


def test_state_separates_own_and_effective_creds(auth):
    _site_and_device(auth, inherit=True)
    device = auth.get("/api/v1/state").json()["devices"][0]
    assert device["username"] == "site-user" and device["has_password"] is True   # для «Открыть»
    assert device["own_username"] == "" and device["own_has_password"] is False   # для формы


def test_secret_own_vs_effective(auth):
    device_id = _site_and_device(auth, inherit=True)
    assert auth.get(f"/api/v1/devices/{device_id}/secret").json()["password"] == "site-pass"
    own = auth.get(f"/api/v1/devices/{device_id}/secret?own=true").json()
    assert own["password"] == "" and own["has_password"] is False


def test_unticking_inherit_leaves_device_without_own_password(auth):
    """Бэкенд-половина: снятие наследования само по себе не даёт устройству
    своего пароля. Фронтовую ошибку (форма заполнялась учёткой точки)
    закрывает test_state_separates_own_and_effective_creds - поля own_*."""
    device_id = _site_and_device(auth, inherit=True)
    auth.put(f"/api/v1/devices/{device_id}", json={"use_site_creds": False})
    assert auth.get(f"/api/v1/devices/{device_id}/secret").json()["has_password"] is False


def test_device_password_can_be_removed(auth):
    device_id = _site_and_device(auth, inherit=False)
    auth.put(f"/api/v1/devices/{device_id}", json={"username": "admin", "password": "cam-1"})
    assert auth.get(f"/api/v1/devices/{device_id}/secret?own=true").json()["password"] == "cam-1"
    auth.put(f"/api/v1/devices/{device_id}", json={"password": False})
    assert auth.get(f"/api/v1/devices/{device_id}/secret?own=true").json()["has_password"] is False


def test_site_password_can_be_removed(auth):
    _site_and_device(auth)
    auth.put("/api/v1/sites/Балыкчы", json={"name": "Балыкчы", "password": False})
    assert auth.get("/api/v1/sites/Балыкчы/secret").json()["has_password"] is False


def test_ticking_inherit_with_password_false_removes_own_password(auth):
    """Форма при включении «Брать учётку точки» шлёт password: false -
    собственный пароль устройства из базы удаляется, а не остаётся лежать."""
    device_id = _site_and_device(auth, inherit=False)
    auth.put(f"/api/v1/devices/{device_id}", json={"username": "cam", "password": "own-1"})
    auth.put(f"/api/v1/devices/{device_id}", json={"use_site_creds": True, "username": "",
                                                   "password": False})
    own = auth.get(f"/api/v1/devices/{device_id}/secret?own=true").json()
    assert own == {"username": "", "password": "", "has_password": False}


def test_slash_in_site_or_category_name_rejected(auth):
    assert auth.post("/api/v1/sites", json={"name": "A/B"}).status_code == 400
    auth.post("/api/v1/sites", json={"name": "Точка"})
    assert auth.post("/api/v1/categories", json={"site": "Точка", "name": "x/y"}).status_code == 400
