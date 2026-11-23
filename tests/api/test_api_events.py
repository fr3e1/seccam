from tests.conftest import assert_shape

DETS = [{"label": "person", "score": 0.9, "bbox": [1, 2, 3, 4]}]


def add(app, make_jpeg, ts, camera="cam1", labels=None):
    dets = [{"label": l, "score": 0.9, "bbox": [1, 2, 3, 4]} for l in (labels or ["person"])]
    return app.state.db.add_event(ts, camera, "Cam " + camera, dets, 640, 480, make_jpeg())


def test_events_list_filter_and_paginate(app, admin_client, make_jpeg):
    ids = [add(app, make_jpeg, 1760000000000 + i, "cam%d" % (i % 2), ["dog"] if i == 0 else None)["id"] for i in range(5)]
    body = admin_client.get("/api/events").json()
    assert set(body) == {"events"} and len(body["events"]) == 5
    for ev in body["events"]:
        assert_shape("Event", ev)
    assert [e["id"] for e in admin_client.get("/api/events?limit=2").json()["events"]] == [ids[4], ids[3]]
    page = admin_client.get(f"/api/events?limit=2&before={ids[3]}").json()["events"]
    assert [e["id"] for e in page] == [ids[2], ids[1]]
    assert [e["id"] for e in admin_client.get("/api/events?label=dog").json()["events"]] == [ids[0]]
    assert {e["camera_id"] for e in admin_client.get("/api/events?camera_id=cam1").json()["events"]} == {"cam1"}
    assert len(admin_client.get("/api/events?limit=500").json()["events"]) == 5


def test_events_bad_query_is_422(admin_client):
    for q in ("limit=abc", "limit=0", "before=x"):
        resp = admin_client.get("/api/events?" + q)
        assert (resp.status_code, resp.json()["code"]) == (422, "validation"), q


def test_event_get_delete_and_images(app, admin_client, make_jpeg):
    ev = add(app, make_jpeg, 1760000000000)
    got = admin_client.get(f"/api/events/{ev['id']}")
    assert got.status_code == 200
    assert_shape("Event", got.json())
    for kind in ("snapshot", "thumb"):
        img = admin_client.get(f"/api/events/{ev['id']}/{kind}.jpg")
        assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg" and img.content[:2] == b"\xff\xd8"
    assert admin_client.delete(f"/api/events/{ev['id']}").json() == {"ok": True}
    assert admin_client.get(f"/api/events/{ev['id']}").json()["code"] == "not_found"
    assert admin_client.delete(f"/api/events/{ev['id']}").status_code == 404
    miss = admin_client.get(f"/api/events/{ev['id']}/snapshot.jpg")
    assert miss.status_code == 404 and miss.content == b""


def test_settings_get_put(admin_client):
    settings = admin_client.get("/api/settings").json()
    assert_shape("Settings", settings)
    new = admin_client.put("/api/settings", json={"theme": "nord", "min_score": 0.7}).json()
    assert_shape("Settings", new)
    assert (new["theme"], new["min_score"]) == ("nord", 0.7)
    for bad in ({"theme": "neon"}, {"jpeg_quality": 2}, {"capture_width": 100}, {"bogus": 1}, {"preview_fps": 99}):
        resp = admin_client.put("/api/settings", json=bad)
        assert (resp.status_code, resp.json()["code"]) == (422, "validation"), bad
    assert admin_client.put("/api/settings", content=b"[]").status_code == 400


def test_cameras_and_stats(admin_client):
    assert admin_client.get("/api/cameras").json() == {"cameras": []}
    stats = admin_client.get("/api/stats").json()
    assert set(stats) == {"events_total", "events_24h", "disk_bytes"}
