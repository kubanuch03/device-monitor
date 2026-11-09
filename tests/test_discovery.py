"""Поиск оборудования: опознание по признакам и сквозной скан на localhost."""

import asyncio
import json
import http.server
import socket
import threading

import pytest

from device_monitor.discovery.scan import DAHUA_PROBE, ScanError, classify, hosts_of, scan


def test_hosts_of_limits_and_validates():
    assert len(hosts_of("192.168.158.0/24")) == 254
    assert hosts_of("10.0.0.5/32") == ["10.0.0.5"]
    with pytest.raises(ScanError):
        hosts_of("10.0.0.0/16")
    with pytest.raises(ScanError):
        hosts_of("не подсеть")


@pytest.mark.parametrize("ports, web, expected", [
    ([80, 554, 37777], {"dahua": {"realm": "Login to 9f2c"}},
     {"vendor": "Dahua", "kind": "camera", "category": "Камеры", "fingerprint": "9f2c"}),
    ([80], {"hikvision": {"realm": "DS-3E0510P-E"}},
     {"vendor": "Hikvision", "kind": "switch", "category": "Коммутаторы", "model": "DS-3E0510P-E"}),
    ([80, 554], {"hikvision": {"realm": "DS-2CD2143G2-I"}},
     {"vendor": "Hikvision", "kind": "camera", "category": "Камеры"}),
    ([80], {"hikvision": {"realm": "DS-K1T341AM"}},
     {"kind": "intercom", "category": "Домофоны"}),
    ([80, 554, 8000], {"root": {"server": "App-webs/"}},
     {"vendor": "Hikvision", "kind": "camera"}),
    ([80, 23], {"root": {"title": "Web Smart Switch"}}, {"kind": "switch"}),
    ([80, 22], {"root": {"title": "RouterOS router configuration page"}},
     {"vendor": "MikroTik", "kind": "router"}),
    ([22, 10050], {}, {"kind": "server", "category": "Серверы"}),
    ([80], {"root": {"server": "Apache/2.4.54 (Debian)", "title": "Логин @ 20.0.5"}},
     {"kind": "unknown", "category": "Прочее"}),
])
def test_classify(ports, web, expected):
    result = classify("10.0.0.66", ports, web)
    for key, value in expected.items():
        assert result[key] == value, (key, result)
    assert result["name"].endswith(".66")


class _FakeDahua(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == DAHUA_PROBE:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Digest realm="Login to abc123", nonce="1"')
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html><title>WEB SERVICE</title></html>")

    def log_message(self, *args):
        pass


def test_scan_finds_and_identifies_local_fake_camera():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeDahua)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    web_port = server.server_address[1]
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    try:
        progress = []
        results = asyncio.run(scan(["127.0.0.1"], ports=(web_port, dead),
                                   web_ports=(web_port,), progress=lambda d, t: progress.append(d)))
    finally:
        server.shutdown()
        server.server_close()
    assert len(results) == 1
    found = results[0]
    assert found["ports"] == [web_port] and found["vendor"] == "Dahua"
    assert found["fingerprint"] == "abc123" and found["title"] == "WEB SERVICE"
    assert progress == [1]


def test_scan_reports_dead_tunnel():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    with pytest.raises(ScanError):
        asyncio.run(scan(["192.168.158.5"], proxy=f"socks5://127.0.0.1:{dead}"))


# -- фоновая задача скана и ИИ-помощник через API -------------------------------

def test_scan_job_marks_existing_and_missing(auth, monkeypatch):
    import time
    from device_monitor.discovery import jobs

    async def fake_scan(hosts, proxy="", progress=None, **kw):
        progress(len(hosts), len(hosts))
        return [{"host": "10.9.0.5", "ports": [80, 554], "vendor": "Dahua", "kind": "camera",
                 "category": "Камеры", "name": "Камера Dahua .5"},
                {"host": "10.9.0.7", "ports": [22], "vendor": "", "kind": "server",
                 "category": "Серверы", "name": "Сервер .7"}]
    monkeypatch.setattr(jobs, "scan", fake_scan)

    auth.post("/api/v1/sites", json={"name": "Скан"})
    for host, name in (("10.9.0.5", "Въезд"), ("10.9.0.9", "Пропавшая")):
        auth.post("/api/v1/devices", json={"site": "Скан", "name": name, "host": host, "ports": [80]})
    assert auth.get("/api/v1/sites/Скан/scan").json()["subnet"] == "10.9.0.0/24"

    job = auth.post("/api/v1/sites/Скан/scan", json={"subnet": "10.9.0.0/24"}).json()["job"]
    for _ in range(50):
        job = auth.get(f"/api/v1/scans/{job['id']}").json()["job"]
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job["status"] == "done" and job["done"] == job["total"] == 254
    by_host = {r["host"]: r for r in job["results"]}
    assert by_host["10.9.0.5"]["existing"]["name"] == "Въезд"
    assert by_host["10.9.0.7"]["existing"] is None
    assert [m["name"] for m in job["missing"]] == ["Пропавшая"]
    # Ничего не добавлено само.
    assert len(auth.get("/api/v1/state").json()["devices"]) == 2


def test_scan_rejects_huge_subnet(auth):
    auth.post("/api/v1/sites", json={"name": "Скан"})
    r = auth.post("/api/v1/sites/Скан/scan", json={"subnet": "10.0.0.0/16"})
    assert r.status_code == 400 and "/22" in r.json()["detail"]


class _FakeLLM(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeLLM.seen.append((self.headers.get("Authorization"), body))
        reply = {"devices": [
            {"host": "10.9.0.5", "name": "Камера ANPR въезд", "category": "Камеры"},
            {"host": "10.9.0.7", "name": "Сервер объекта", "category": "Космос"},
            {"host": "8.8.8.8", "name": "чужой", "category": "Камеры"},
        ]}
        payload = json.dumps({"choices": [{"message": {"content": json.dumps(reply)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def test_assistant_names_are_filtered_and_key_hidden(auth):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeLLM)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _FakeLLM.seen = []
    try:
        r = auth.put("/api/v1/assistant", json={
            "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1",
            "model": "qwen-test", "api_key": "sk-secret-123", "enabled": True})
        assert r.json()["has_key"] is True and "sk-secret-123" not in r.text
        assert "sk-secret-123" not in auth.get("/api/v1/assistant").text
        assert auth.get("/api/v1/state").json()["assistant_configured"] is True

        candidates = [{"host": "10.9.0.5", "ports": [80], "vendor": "Dahua", "password": "x"},
                      {"host": "10.9.0.7", "ports": [22]}]
        out = auth.post("/api/v1/assistant/names", json={"candidates": candidates}).json()
    finally:
        server.shutdown()
        server.server_close()
    got = {s["host"]: s for s in out["suggestions"]}
    assert got["10.9.0.5"] == {"host": "10.9.0.5", "name": "Камера ANPR въезд", "category": "Камеры"}
    assert got["10.9.0.7"]["category"] is None          # «Космос» не из списка
    assert "8.8.8.8" not in got                          # хоста не было в запросе
    auth_header, body = _FakeLLM.seen[0]
    assert auth_header == "Bearer sk-secret-123"
    assert "password" not in json.dumps(body)            # в модель - только открытые поля


def test_assistant_not_configured_is_clear_error(auth):
    r = auth.post("/api/v1/assistant/names", json={"candidates": [{"host": "10.0.0.1"}]})
    assert r.status_code == 400 and "не настроен" in r.json()["detail"]


@pytest.mark.parametrize("realm", ["Restricted", "MikroTik", "TP-LINK Router", "Zabbix"])
def test_non_hikvision_realm_is_not_hikvision(realm):
    result = classify("10.0.0.1", [80], {"hikvision": {"realm": realm}})
    assert result["vendor"] != "Hikvision"


def test_router_title_wins_over_foreign_isapi_realm():
    result = classify("10.0.0.1", [80, 22], {"hikvision": {"realm": "MikroTik"},
                                             "root": {"title": "RouterOS router"}})
    assert result["vendor"] == "MikroTik" and result["kind"] == "router"


def test_http_reply_in_chunks_is_read_fully():
    """Через туннель ответ приходит частями - заголовок авторизации во втором
    куске не должен теряться."""
    import time
    from device_monitor.discovery.scan import _http_get

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.0 401 Unauthorized\r\nServer: x\r\n")
        time.sleep(0.2)
        conn.sendall(b'WWW-Authenticate: Digest realm="Login to deadbeef"\r\n\r\n<title>WEB</title>')
        conn.close()
    threading.Thread(target=serve, daemon=True).start()
    try:
        got = asyncio.run(_http_get("127.0.0.1", port, "", "/"))
    finally:
        srv.close()
    assert got["realm"] == "Login to deadbeef" and got["title"] == "WEB"


def test_scan_job_error_does_not_stick_running(auth, monkeypatch):
    import time
    from device_monitor.discovery import jobs

    async def boom(*a, **kw):
        raise RuntimeError("сеть упала")
    monkeypatch.setattr(jobs, "scan", boom)
    auth.post("/api/v1/sites", json={"name": "Скан"})
    job = auth.post("/api/v1/sites/Скан/scan", json={"subnet": "10.9.0.0/30"}).json()["job"]
    for _ in range(50):
        job = auth.get(f"/api/v1/scans/{job['id']}").json()["job"]
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job["status"] == "error" and "сеть упала" in job["error"]
    again = auth.post("/api/v1/sites/Скан/scan", json={"subnet": "10.9.0.0/30"}).json()["job"]
    assert again["id"] != job["id"]   # новая задача, а не залипшая старая


def test_parse_devices_with_prose_and_junk_candidates(auth):
    from device_monitor.assist.llm import _parse_devices
    assert _parse_devices('Вот ответ: {"devices": [{"host": "a"}]} — готово') == [{"host": "a"}]
    assert _parse_devices("") == [] and _parse_devices("нет json") == []
    auth.put("/api/v1/assistant", json={"base_url": "http://127.0.0.1:9/v1", "model": "m",
                                        "enabled": True})
    r = auth.post("/api/v1/assistant/names", json={"candidates": [1, {"host": ["x"]}]})
    assert r.status_code == 400   # мусор отфильтрован, а не 500
    assert auth.put("/api/v1/assistant", json={"api_key": True}).status_code == 400


def test_assistant_key_dropped_when_host_changes(auth):
    auth.put("/api/v1/assistant", json={"base_url": "http://10.1.1.1:7700/v1", "model": "m",
                                        "api_key": "k-1", "enabled": True})
    auth.put("/api/v1/assistant", json={"base_url": "http://10.1.1.1:7700/v2"})
    assert auth.get("/api/v1/assistant").json()["has_key"] is True     # тот же хост
    auth.put("/api/v1/assistant", json={"base_url": "http://evil.example/v1"})
    assert auth.get("/api/v1/assistant").json()["has_key"] is False    # другой хост
