import json

import pytest
from fastapi.testclient import TestClient

import server.app as app_module
from server import updater
from server.updater import UpdateError
from tests.api.conftest import ADMIN_PASSWORD, login, make_admin
from tests.conftest import assert_shape

URL = "https://api.github.com/repos/fr3e1/seccam/releases/latest"


def release(version="2.1.0", **over):
    base = f"https://github.com/fr3e1/seccam/releases/download/v{version}/"
    data = {"tag_name": f"v{version}", "html_url": f"https://github.com/fr3e1/seccam/releases/tag/v{version}",
            "published_at": "2026-11-01T00:00:00Z", "body": "Fixes",
            "assets": [{"name": f"seccam-{version}.tar.gz", "browser_download_url": base + f"seccam-{version}.tar.gz"},
                       {"name": "SHA256SUMS", "browser_download_url": base + "SHA256SUMS"}], **over}
    return json.dumps(data).encode()


@pytest.fixture(autouse=True)
def running_version(monkeypatch):
    monkeypatch.setattr(app_module, "__version__", "2.0.0")


def systemd_client(env, monkeypatch, fake):
    monkeypatch.setenv("SECCAM_UPDATE_MODE", "systemd")
    from server.config import load_config
    application = app_module.create_app(load_config(), http_get=fake)
    make_admin(application)
    return application


def apply(client, version="2.1.0", password=ADMIN_PASSWORD):
    return client.post("/api/update/apply", json={"version": version, "password": password})


def test_update_info_available(admin_client, fake_http_get):
    fake_http_get[URL] = release()
    resp = admin_client.get("/api/update")
    assert resp.status_code == 200
    body = resp.json()
    assert_shape("UpdateInfo", body)
    assert body["enabled"] is False and body["update_available"] is True and body["error"] is None
    assert body["latest"]["version"] == "2.1.0" and body["current_version"] == "2.0.0"
    assert body["status"]["state"] == "idle"


def test_update_error_sets_error_and_null_latest(admin_client):
    body = admin_client.get("/api/update").json()
    assert_shape("UpdateInfo", body)
    assert body["latest"] is None and body["error"] and body["update_available"] is False


def test_update_cache_and_refresh_throttle(admin_client, fake_http_get):
    fake_http_get[URL] = release()
    admin_client.get("/api/update")
    admin_client.get("/api/update")
    admin_client.get("/api/update?refresh=1")  # within 30 s of the last check: served from cache
    assert len(fake_http_get.calls) == 1


def test_update_not_newer_means_not_available(admin_client, fake_http_get):
    fake_http_get[URL] = release("2.0.0")
    assert admin_client.get("/api/update").json()["update_available"] is False


def test_apply_writes_request(env, monkeypatch, fake_http_get):
    fake_http_get[URL] = release()
    application = systemd_client(env, monkeypatch, fake_http_get)
    with TestClient(application) as client:
        login(client)
        resp = apply(client)
        assert resp.status_code == 202
        assert resp.json()["ok"] is True
        assert_shape("UpdateStatus", resp.json()["status"])
        assert resp.json()["status"]["state"] == "requested"
        request = json.loads((application.state.cfg.data_dir / "updates" / "request.json").read_text())
        assert request["version"] == "2.1.0" and request["requested_by"] == "admin"
        again = apply(client)
        assert (again.status_code, again.json()["code"]) == (409, "update_pending")


def test_apply_error_branches(env, monkeypatch, fake_http_get):
    fake_http_get[URL] = release()
    application = systemd_client(env, monkeypatch, fake_http_get)
    with TestClient(application) as client:
        login(client)
        bad_pw = apply(client, password="wrong-password-1")
        assert (bad_pw.status_code, bad_pw.json()["code"]) == (401, "bad_credentials")
        bad_fmt = apply(client, version="latest")
        assert (bad_fmt.status_code, bad_fmt.json()["code"]) == (422, "validation")
        stale = apply(client, version="2.0.5")
        assert (stale.status_code, stale.json()["code"]) == (409, "not_latest")
        assert client.post("/api/update/apply", json={"version": "2.1.0"}).status_code == 400
        fake_http_get[URL] = release("2.0.0")
        not_newer = apply(client, version="2.0.0")
        assert (not_newer.status_code, not_newer.json()["code"]) == (409, "not_newer")
        fake_http_get[URL] = UpdateError("boom")
        upstream = apply(client)
        assert (upstream.status_code, upstream.json()["code"]) == (502, "upstream_error")


def test_apply_disabled_when_update_mode_off(admin_client, fake_http_get):
    fake_http_get[URL] = release()
    resp = apply(admin_client)
    assert (resp.status_code, resp.json()["code"]) == (409, "updates_disabled")
    assert not (admin_client.app.state.cfg.data_dir / "updates" / "request.json").exists()


def test_apply_password_checked_before_mode(admin_client):
    assert apply(admin_client, password="wrong-password-1").status_code == 401


def test_apply_password_rate_limited(admin_client):
    codes = [apply(admin_client, password="wrong-password-1").status_code for _ in range(6)]
    assert codes == [401] * 5 + [429]


def test_apply_needs_csrf(env, monkeypatch, fake_http_get):
    application = systemd_client(env, monkeypatch, fake_http_get)
    with TestClient(application) as client:
        client.post("/api/login", json={"username": "admin", "password": ADMIN_PASSWORD})
        resp = apply(client)
        assert (resp.status_code, resp.json()["code"]) == (403, "csrf_failed")


def test_status_reflects_updater_state(admin_client):
    data_dir = admin_client.app.state.cfg.data_dir
    (data_dir / "updates").mkdir()
    updater.write_status(data_dir, "downloading", "2.1.0", "2.0.0", "fetching")
    status = admin_client.get("/api/update").json()["status"]
    assert_shape("UpdateStatus", status)
    assert status["state"] == "downloading" and status["version"] == "2.1.0"
