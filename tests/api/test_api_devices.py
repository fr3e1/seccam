from fastapi.testclient import TestClient

from server import auth
from tests.api.conftest import make_admin
from tests.conftest import assert_shape


def issue(admin_client, name=None):
    resp = admin_client.post("/api/devices/pairing", json={"name": name} if name else {})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_pairing_code_shape_and_limit(admin_client):
    body = issue(admin_client)
    assert set(body) == {"code", "expires_at"} and len(body["code"]) == 9 and body["code"][4] == "-"
    for _ in range(4):
        issue(admin_client)
    resp = admin_client.post("/api/devices/pairing", json={})
    assert (resp.status_code, resp.json()["code"]) == (409, "too_many_codes")


def test_pairing_name_validation(admin_client):
    assert admin_client.post("/api/devices/pairing", json={"name": "x" * 65}).status_code == 422
    assert admin_client.post("/api/devices/pairing", json={"name": 5}).status_code == 400


def test_pair_agent_returns_token_once(app, admin_client):
    code = issue(admin_client, "Garage")["code"]
    with TestClient(app) as anon:
        resp = anon.post("/api/pair", json={"code": code.lower().replace("-", " "), "kind": "agent"})
        assert resp.status_code == 201
        body = resp.json()
        assert set(body) == {"device_id", "name", "kind", "token"}
        assert body["name"] == "Garage" and body["kind"] == "agent"
        assert "seccam_device" not in resp.cookies
        parsed = auth.parse_device_token(body["token"])
        assert parsed[0] == body["device_id"]
        assert anon.post("/api/pair", json={"code": code, "kind": "agent"}).json()["code"] == "bad_pairing_code"
    listed = admin_client.get("/api/devices").json()["devices"]
    assert len(listed) == 1
    assert_shape("Device", listed[0])
    assert listed[0]["connected"] is False and "token_hash" not in listed[0]


def test_pair_browser_sets_cookie_and_name_precedence(app, admin_client):
    code = issue(admin_client, "Issued")["code"]
    with TestClient(app) as phone:
        resp = phone.post("/api/pair", json={"code": code, "kind": "browser", "name": "  Mine  "})
        assert resp.status_code == 201 and resp.json()["name"] == "Mine" and "token" not in resp.json()
        header = resp.headers["set-cookie"].lower()
        assert "seccam_device=" in header and "httponly" in header and "samesite=strict" in header
        assert "max-age=34560000" in header
    code = issue(admin_client)["code"]
    with TestClient(app) as phone:
        assert phone.post("/api/pair", json={"code": code, "kind": "browser"}).json()["name"] == "Camera"
    code = issue(admin_client, "FromIssue")["code"]
    with TestClient(app) as phone:
        assert phone.post("/api/pair", json={"code": code, "kind": "browser", "name": "  "}).json()["name"] == "FromIssue"


def test_pair_validation(app, admin_client):
    code = issue(admin_client)["code"]
    with TestClient(app) as anon:
        assert anon.post("/api/pair", json={"kind": "agent"}).status_code == 400
        assert anon.post("/api/pair", json={"code": 5, "kind": "agent"}).status_code == 400
        assert anon.post("/api/pair", json={"code": code}).status_code == 400
        assert anon.post("/api/pair", json={"code": code, "kind": "robot"}).status_code == 422
        assert anon.post("/api/pair", json={"code": code, "kind": "agent", "name": "x" * 65}).status_code == 422
        # a rejected request did not burn the code
        assert anon.post("/api/pair", json={"code": code, "kind": "agent"}).status_code == 201


def test_pair_rate_limited(app):
    make_admin(app)
    with TestClient(app) as anon:
        codes = [anon.post("/api/pair", json={"code": "AAAA-AAAA", "kind": "agent"}).status_code for _ in range(6)]
        assert codes == [401] * 5 + [429]


def test_pair_in_setup_mode(setup_client):
    resp = setup_client.post("/api/pair", json={"code": "AAAA-AAAA", "kind": "agent"})
    assert (resp.status_code, resp.json()["code"]) == (403, "setup_required")


def test_rename_and_delete_device(app, admin_client, device_token):
    device_id = auth.parse_device_token(device_token)[0]
    resp = admin_client.patch(f"/api/devices/{device_id}", json={"name": "  Porch  "})
    assert resp.status_code == 200
    assert_shape("Device", resp.json())
    assert resp.json()["name"] == "Porch"
    assert admin_client.patch(f"/api/devices/{device_id}", json={"name": "  "}).status_code == 422
    assert admin_client.patch(f"/api/devices/{device_id}", json={"name": "x" * 65}).status_code == 422
    assert admin_client.patch(f"/api/devices/{device_id}", json={}).status_code == 400
    assert admin_client.patch("/api/devices/ffffffffffff", json={"name": "a"}).status_code == 404
    resp = admin_client.delete(f"/api/devices/{device_id}")
    assert resp.status_code == 204 and resp.content == b""
    assert admin_client.delete(f"/api/devices/{device_id}").status_code == 404


def test_device_principal_scope(app, admin_client, device_token):
    with TestClient(app) as dev:
        # Bearer and cookie both open /camera
        assert dev.get("/camera", headers={"Authorization": f"Bearer {device_token}"}).status_code == 200
        dev.cookies.set("seccam_device", device_token)
        assert dev.get("/camera").status_code == 200
        # but no API and no index page
        assert dev.get("/api/me").status_code == 401
        assert dev.get("/api/settings", headers={"Authorization": f"Bearer {device_token}"}).status_code == 401
        assert dev.get("/", follow_redirects=False).status_code == 303
    device_id = auth.parse_device_token(device_token)[0]
    assert app.state.db.get_device(device_id)["last_seen_at"] is not None


def test_invalid_device_credential_counts_failures(app, admin_client, device_token):
    forged = device_token[:-3] + "xxx"
    with TestClient(app, client=("10.7.7.7", 1)) as dev:
        for _ in range(5):
            resp = dev.get("/camera", headers={"Authorization": f"Bearer {forged}"}, follow_redirects=False)
            assert resp.status_code == 303
        resp = dev.get("/camera", headers={"Authorization": f"Bearer {device_token}"}, follow_redirects=False)
        assert (resp.status_code, resp.json()["code"]) == (429, "rate_limited")
    with TestClient(app, client=("10.7.7.8", 1)) as dev:
        # an absent credential counts nothing
        for _ in range(8):
            assert dev.get("/camera", follow_redirects=False).status_code == 303
        assert dev.get("/camera", headers={"Authorization": f"Bearer {device_token}"}).status_code == 200


def test_revoked_device_loses_camera_page(app, admin_client, device_token):
    device_id = auth.parse_device_token(device_token)[0]
    admin_client.delete(f"/api/devices/{device_id}")
    with TestClient(app) as dev:
        resp = dev.get("/camera", headers={"Authorization": f"Bearer {device_token}"}, follow_redirects=False)
        assert resp.status_code == 303
