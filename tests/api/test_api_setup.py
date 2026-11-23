import threading

from fastapi.testclient import TestClient

from server import auth
from tests.conftest import assert_shape

GOOD = {"username": "alice", "password": "correct-horse-42", "password_confirm": "correct-horse-42"}


def setup_body(app, **extra):
    token = auth.ensure_setup_token(app.state.cfg.data_dir)
    return {**GOOD, "setup_token": token, **extra}


def test_health_and_status_in_setup_mode(setup_client):
    health = setup_client.get("/api/health").json()
    assert health["ok"] is True and health["setup_required"] is True and health["model_available"] is False
    status = setup_client.get("/api/setup/status").json()
    assert status == {"setup_required": True, "legacy_pin_accepted": False, "version": health["version"]}


def test_setup_with_token_creates_admin_and_session(app, setup_client):
    resp = setup_client.post("/api/setup", json=setup_body(app, retention_days=7, mode="server", theme="nord",
                                                           labels=["person", "dog"]))
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert "seccam_session" in resp.cookies and "seccam_csrf" in resp.cookies
    me = setup_client.get("/api/me")
    assert me.status_code == 200
    assert_shape("Me", me.json())
    assert me.json()["username"] == "alice"
    settings = setup_client.get("/api/settings").json()
    assert_shape("Settings", settings)
    assert (settings["retention_days"], settings["mode"], settings["theme"]) == (7, "server", "nord")
    assert auth.read_setup_token(app.state.cfg.data_dir) is None
    assert setup_client.get("/api/setup/status").json()["setup_required"] is False
    again = setup_client.post("/api/setup", json=setup_body(app))
    assert again.status_code == 409 and again.json()["code"] == "already_setup"
    assert_shape("Error", again.json())


def test_setup_bad_token_and_missing_credential(app, setup_client):
    auth.ensure_setup_token(app.state.cfg.data_dir)
    resp = setup_client.post("/api/setup", json={**GOOD, "setup_token": "nope"})
    assert resp.status_code == 401 and resp.json()["code"] == "bad_setup_token"
    resp = setup_client.post("/api/setup", json=GOOD)
    assert resp.status_code == 401 and resp.json()["code"] == "bad_setup_token"
    assert setup_client.get("/api/setup/status").json()["setup_required"] is True


def test_setup_token_failures_rate_limited(app, setup_client):
    auth.ensure_setup_token(app.state.cfg.data_dir)
    codes = [setup_client.post("/api/setup", json={**GOOD, "setup_token": "x"}).status_code for _ in range(6)]
    assert codes == [401] * 5 + [429]
    resp = setup_client.post("/api/setup", json=setup_body(app))
    assert resp.status_code == 429 and resp.json()["code"] == "rate_limited" and int(resp.headers["Retry-After"]) > 0


def test_setup_validation_codes(app, setup_client):
    body = setup_body(app)
    cases = [
        ({**body, "password_confirm": "different-pass-1"}, 422, "validation"),
        ({**body, "password": "short", "password_confirm": "short"}, 422, "validation"),
        ({**body, "username": "a"}, 422, "validation"),
        ({**body, "mode": "weird"}, 422, "validation"),
        ({**body, "theme": "neon"}, 422, "validation"),
        ({**body, "retention_days": 0}, 422, "validation"),
        ({**body, "labels": ["unicorn"]}, 422, "validation"),
        ({k: v for k, v in body.items() if k != "username"}, 400, "bad_request"),
        ({**body, "password": 12345678901}, 400, "bad_request"),
        ({**body, "retention_days": "7"}, 400, "bad_request"),
        ({**body, "labels": "person"}, 400, "bad_request"),
    ]
    for payload, status, code in cases:
        resp = setup_client.post("/api/setup", json=payload)
        assert (resp.status_code, resp.json()["code"]) == (status, code), payload
    assert setup_client.post("/api/setup", content=b"not json").status_code == 400
    assert setup_client.post("/api/setup", content=b"[1]").status_code == 400
    assert setup_client.get("/api/setup/status").json()["setup_required"] is True


def test_setup_bad_origin(app, setup_client):
    resp = setup_client.post("/api/setup", json=setup_body(app), headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403 and resp.json()["code"] == "bad_origin"


def test_setup_public_origin_expected(env, monkeypatch):
    monkeypatch.setenv("SECCAM_PUBLIC_ORIGIN", "https://cam.example.com")
    from server.app import create_app
    from server.config import load_config
    application = create_app(load_config())
    with TestClient(application) as client:
        body = setup_body(application)
        bad = client.post("/api/setup", json=body, headers={"Origin": "http://testserver"})
        assert bad.status_code == 403
        ok = client.post("/api/setup", json=body, headers={"Origin": "https://cam.example.com"})
        assert ok.status_code == 200
        assert "secure" in ok.headers["set-cookie"].lower()


def test_setup_mode_gates(setup_client):
    for path in ("/api/me", "/api/settings", "/api/events", "/api/devices", "/api/nothing-here"):
        resp = setup_client.get(path)
        assert (resp.status_code, resp.json()["code"]) == (403, "setup_required"), path
    for method in ("post", "put", "delete"):
        assert getattr(setup_client, method)("/api/login").status_code == 403
    pin = setup_client.post("/api/pin", json={})
    assert (pin.status_code, pin.json()["code"]) == (403, "setup_required")
    for path in ("/", "/camera", "/login", "/pair"):
        resp = setup_client.get(path, follow_redirects=False)
        assert (resp.status_code, resp.headers["location"]) == (303, "/welcome"), path
    for path in ("/welcome", "/setup", "/static/css/base.css"):
        assert setup_client.get(path, follow_redirects=False).status_code == 200, path
    assert setup_client.get("/ca.crt").status_code == 404


def test_welcome_redirects_when_configured(admin_client):
    resp = admin_client.get("/welcome", follow_redirects=False)
    assert (resp.status_code, resp.headers["location"]) == (303, "/")


def test_migration_via_db_auth_record_keeps_events(app, setup_client, make_jpeg):
    db = app.state.db
    db.set_auth(auth.hash_pin("1234"))
    db.add_event(1760000000000, "cam1", "Cam", [{"label": "person", "score": 0.9, "bbox": [1, 2, 3, 4]}], 640, 480, make_jpeg())
    assert setup_client.get("/api/setup/status").json()["legacy_pin_accepted"] is True
    resp = setup_client.post("/api/setup", json={**GOOD, "legacy_pin": "1234"})
    assert resp.status_code == 200
    assert db.get_auth() is None
    events = setup_client.get("/api/events").json()["events"]
    assert len(events) == 1
    assert_shape("Event", events[0])
    assert setup_client.get("/api/setup/status").json()["legacy_pin_accepted"] is False


def test_migration_via_env_pin(env, monkeypatch):
    monkeypatch.setenv("SECCAM_PIN", "4321")
    from server.app import create_app
    from server.config import load_config
    application = create_app(load_config())
    with TestClient(application) as client:
        assert client.get("/api/setup/status").json()["legacy_pin_accepted"] is True
        wrong = client.post("/api/setup", json={**GOOD, "legacy_pin": "0000"})
        assert (wrong.status_code, wrong.json()["code"]) == (401, "bad_setup_token")
        assert client.post("/api/setup", json={**GOOD, "legacy_pin": "4321"}).status_code == 200


def test_pin_path_without_legacy_credential(setup_client):
    resp = setup_client.post("/api/setup", json={**GOOD, "legacy_pin": "1234"})
    assert (resp.status_code, resp.json()["code"]) == (401, "bad_setup_token")


def test_global_pin_lock_across_client_ips(app):
    app.state.db.set_auth(auth.hash_pin("1234"))
    for i in range(5):
        with TestClient(app, client=(f"10.0.0.{i + 1}", 5000)) as client:
            resp = client.post("/api/setup", json={**GOOD, "legacy_pin": "0000"})
            assert resp.status_code == 401
    with TestClient(app, client=("10.9.9.9", 5000)) as fresh:
        resp = fresh.post("/api/setup", json={**GOOD, "legacy_pin": "1234"})
        assert (resp.status_code, resp.json()["code"]) == (429, "rate_limited")
        # the setup token path is not affected by the PIN lock
        assert fresh.post("/api/setup", json=setup_body(app)).status_code == 200


def test_pin_path_disabled_after_20_failures(app, setup_client):
    app.state.db.set_auth(auth.hash_pin("1234"))
    for _ in range(20):
        app.state.pin_limiter.failure("setup:pin:global")
    assert setup_client.get("/api/setup/status").json()["legacy_pin_accepted"] is False
    resp = setup_client.post("/api/setup", json={**GOOD, "legacy_pin": "1234"})
    assert (resp.status_code, resp.json()["code"]) == (401, "pin_disabled")
    assert setup_client.post("/api/setup", json=setup_body(app)).status_code == 200


def test_concurrent_setup_yields_one_winner(app, setup_client):
    body = setup_body(app)
    results = []

    def attempt():
        results.append(setup_client.post("/api/setup", json=body).status_code)

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 409, 409, 409]
    assert app.state.db.count_users() == 1


def test_setup_409_path_never_writes_settings(app, setup_client, monkeypatch):
    body = setup_body(app, theme="nord", mode="server")
    monkeypatch.setattr(app.state.db, "create_first_user", lambda *a: None)
    resp = setup_client.post("/api/setup", json=body)
    assert (resp.status_code, resp.json()["code"]) == (409, "already_setup")
    assert app.state.db.get_settings()["theme"] == "tokyo-night"


def test_setup_422_does_not_write_settings_or_user(app, setup_client):
    resp = setup_client.post("/api/setup", json=setup_body(app, theme="nord", mode="weird"))
    assert resp.status_code == 422
    assert app.state.db.get_settings()["theme"] == "tokyo-night" and app.state.db.count_users() == 0


def test_setup_settings_applied_and_pushed_to_hub(app, setup_client):
    seen = []
    real = app.state.hub.settings_changed

    async def spy(settings):
        seen.append(settings["theme"])
        await real(settings)

    app.state.hub.settings_changed = spy
    assert setup_client.post("/api/setup", json=setup_body(app, theme="nord")).status_code == 200
    assert seen == ["nord"] and app.state.db.get_settings()["theme"] == "nord"
