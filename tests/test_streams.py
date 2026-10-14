"""Список источников go2rtc точки - по нему страница видео выбирает SD/HD."""

def test_site_streams_without_go2rtc_is_empty(auth):
    auth.post("/api/v1/sites", json={"name": "Без видео"})
    r = auth.get("/api/v1/sites/Без видео/streams")
    assert r.status_code == 200 and r.json() == {"names": []}


def test_site_streams_unknown_site(auth):
    assert auth.get("/api/v1/sites/Нет такой/streams").status_code == 404
