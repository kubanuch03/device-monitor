"""Экспорт и импорт данных: круговой проход, пароли и отказ на чужом файле."""


def _seed(client):
    client.post("/api/v1/sites", json={
        "name": "Балыкчы", "stream_base": "http://10.1.1.1:1983",
        "username": "op", "password": "site-pass-1",
    })
    client.post("/api/v1/categories", json={"site": "Балыкчы", "name": "Камеры", "note": "вход"})
    r = client.post("/api/v1/devices", json={
        "site": "Балыкчы", "category": "Камеры", "name": "Камера 1",
        "host": "10.20.5.11", "ports": [80, 554], "username": "admin",
        "password": "cam-pass-1", "open_url": "http://127.0.0.1:18001/",
        "stream_name": "cam1",
    })
    assert r.status_code == 200
    return r.json()["device"]["id"]


def test_export_without_secrets_has_no_passwords(auth):
    _seed(auth)
    r = auth.get("/api/v1/export")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    body = r.json()
    assert body["format"] == "device-monitor/1"
    assert body["includes_secrets"] is False
    assert body["sites"][0]["name"] == "Балыкчы"
    assert body["devices"][0]["host"] == "10.20.5.11"
    assert "password" not in body["devices"][0]
    assert "password" not in body["sites"][0]
    assert "cam-pass-1" not in r.text and "site-pass-1" not in r.text


def test_export_with_secrets_includes_passwords(auth):
    _seed(auth)
    body = auth.get("/api/v1/export?secrets=true").json()
    assert body["includes_secrets"] is True
    assert body["devices"][0]["password"] == "cam-pass-1"
    assert body["sites"][0]["password"] == "site-pass-1"


def test_roundtrip_restores_deleted_device(auth):
    device_id = _seed(auth)
    exported = auth.get("/api/v1/export").json()

    assert auth.delete(f"/api/v1/devices/{device_id}").status_code == 200
    assert auth.get("/api/v1/state").json()["devices"] == []

    r = auth.post("/api/v1/import", json=exported)
    assert r.status_code == 200
    report = r.json()
    assert report["errors"] == []
    assert report["sites"] == 1 and report["categories"] == 1 and report["devices"] == 1

    device = auth.get("/api/v1/state").json()["devices"][0]
    assert device["id"] == device_id
    assert device["host"] == "10.20.5.11"
    assert device["open_url"] == "http://127.0.0.1:18001/"
    assert device["stream_name"] == "cam1"


def test_import_without_passwords_keeps_existing_secret(auth):
    device_id = _seed(auth)
    exported = auth.get("/api/v1/export").json()  # без паролей
    exported["devices"][0]["name"] = "Переименована"

    assert auth.post("/api/v1/import", json=exported).json()["errors"] == []
    assert auth.get(f"/api/v1/devices/{device_id}/secret").json()["password"] == "cam-pass-1"


def test_import_reports_bad_item_and_continues(auth):
    _seed(auth)
    payload = auth.get("/api/v1/export").json()
    payload["devices"].append({"name": "Битая", "host": "не адрес!"})

    report = auth.post("/api/v1/import", json=payload).json()
    assert report["devices"] == 1
    assert len(report["errors"]) == 1
    assert "Битая" in report["errors"][0]


def test_import_rejects_foreign_file(auth):
    r = auth.post("/api/v1/import", json={"sites": []})
    assert r.status_code == 400
    assert "device-monitor" in r.json()["detail"]


def test_partial_site_update_keeps_proxy_and_note(auth):
    """Правка одного поля точки не должна стирать остальные (был баг: PUT с
    одним stream_base обнулял прокси и заметку)."""
    auth.post("/api/v1/sites", json={
        "name": "Туннельная", "note": "через ssh", "proxy": "socks5://127.0.0.1:1081",
    })
    r = auth.put("/api/v1/sites/Туннельная", json={
        "name": "Туннельная", "stream_base": "http://127.0.0.1:1985",
    })
    site = r.json()["site"]
    assert site["proxy"] == "socks5://127.0.0.1:1081"
    assert site["note"] == "через ssh"
    assert site["stream_base"] == "http://127.0.0.1:1985"

