from fastapi.testclient import TestClient

from tests.api.conftest import ADMIN_PASSWORD, login, make_admin


def test_unsafe_requests_need_csrf(admin_client):
    token = admin_client.headers.pop("X-CSRF-Token")
    for method, path in (("put", "/api/settings"), ("post", "/api/sessions/revoke-others"),
                         ("post", "/api/devices/pairing"), ("delete", "/api/events/1"),
                         ("post", "/api/account/password")):
        resp = admin_client.request(method.upper(), path, json={})
        assert (resp.status_code, resp.json()["code"]) == (403, "csrf_failed"), path
    bad = admin_client.put("/api/settings", json={}, headers={"X-CSRF-Token": "wrong"})
    assert bad.status_code == 403
    nonascii = admin_client.put("/api/settings", json={}, headers={"X-CSRF-Token": "é".encode("latin-1")})
    assert nonascii.status_code == 403
    assert admin_client.put("/api/settings", json={}, headers={"X-CSRF-Token": token}).status_code == 200


def test_safe_methods_and_exempt_routes_need_no_csrf(app):
    make_admin(app)
    with TestClient(app) as client:
        assert client.post("/api/login", json={"username": "admin", "password": ADMIN_PASSWORD}).status_code == 200
        assert client.get("/api/settings").status_code == 200
        assert client.post("/api/logout").status_code == 200


def test_unauthenticated_api_is_401(app):
    make_admin(app)
    with TestClient(app) as client:
        for path in ("/api/me", "/api/settings", "/api/events", "/api/cameras", "/api/stats",
                     "/api/devices", "/api/update", "/api/events/1/snapshot.jpg"):
            resp = client.get(path)
            assert (resp.status_code, resp.json()["code"]) == (401, "unauthorized"), path
        assert client.put("/api/settings", json={}).status_code == 401


def test_origin_checked_on_unsafe_methods_only(admin_client):
    evil = {"Origin": "http://evil.example"}
    resp = admin_client.put("/api/settings", json={}, headers=evil)
    assert (resp.status_code, resp.json()["code"]) == (403, "bad_origin")
    assert admin_client.post("/api/logout", headers=evil).status_code == 403
    assert admin_client.post("/api/login", json={}, headers=evil).status_code == 403
    assert admin_client.put("/api/settings", json={}, headers={"Origin": "null"}).status_code == 403
    assert admin_client.get("/api/settings", headers=evil).status_code == 200
    assert admin_client.put("/api/settings", json={}, headers={"Origin": "http://testserver"}).status_code == 200
    assert admin_client.put("/api/settings", json={}, headers={"Origin": "HTTP://TESTSERVER"}).status_code == 200


def test_pair_origin_checked(app, setup_client):
    make_admin(app)
    resp = setup_client.post("/api/pair", json={}, headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403 and resp.json()["code"] == "bad_origin"


def test_html_redirects_to_login(app):
    make_admin(app)
    with TestClient(app) as client:
        resp = client.get("/?a=b", follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/login?next=/%3Fa%3Db"
        assert client.get("/login").status_code == 200
        assert client.get("/pair").status_code == 200
        cam = client.get("/camera", follow_redirects=False)
        assert (cam.status_code, cam.headers["location"]) == (303, "/pair?next=/camera")


def test_html_pages_for_admin(admin_client):
    for path in ("/", "/camera", "/login", "/pair", "/setup"):
        resp = admin_client.get(path)
        assert resp.status_code == 200 and "text/html" in resp.headers["content-type"], path


def test_ca_cert(app, setup_client, tmp_path):
    assert setup_client.get("/ca.crt").status_code == 404


def test_ca_cert_served_when_configured(env, tmp_path, monkeypatch):
    ca = tmp_path / "ca.crt"
    ca.write_text("CERT")
    monkeypatch.setenv("SECCAM_CA_CERT", str(ca))
    from server.app import create_app
    from server.config import load_config
    with TestClient(create_app(load_config())) as client:
        resp = client.get("/ca.crt")
        assert resp.status_code == 200 and resp.text == "CERT"
        assert resp.headers["content-type"].startswith("application/x-x509-ca-cert")


def test_error_shapes(admin_client):
    resp = admin_client.get("/api/does-not-exist")
    assert (resp.status_code, resp.json()) == (404, {"detail": "Not found", "code": "not_found"})
    resp = admin_client.post("/api/health")
    assert (resp.status_code, resp.json()) == (405, {"detail": "Method not allowed", "code": "method_not_allowed"})
    resp = admin_client.get("/api/events/abc")
    assert resp.status_code == 422 and resp.json()["code"] == "validation" and isinstance(resp.json()["detail"], str)
    assert admin_client.get("/nope").json()["code"] == "not_found"


def test_unhandled_exception_is_500_json(app, monkeypatch):
    make_admin(app)
    monkeypatch.setattr(app.state.db, "stats", lambda: 1 / 0)
    with TestClient(app, raise_server_exceptions=False) as client:
        login(client)
        resp = client.get("/api/stats")
        assert (resp.status_code, resp.json()) == (500, {"detail": "Internal error", "code": "internal"})
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["x-frame-options"] == "DENY"
        assert "content-security-policy" in resp.headers


def test_oversize_body_413(admin_client):
    resp = admin_client.put("/api/settings", content=b"x" * 70000)
    assert (resp.status_code, resp.json()["code"]) == (413, "payload_too_large")


def test_nan_and_infinity_rejected_with_400(admin_client):
    for raw in (b'{"min_score": NaN}', b'{"min_score": Infinity}', b'{"min_score": -Infinity}'):
        resp = admin_client.put("/api/settings", content=raw, headers={"Content-Type": "application/json"})
        assert (resp.status_code, resp.json()["code"]) == (400, "bad_request"), raw


def test_huge_ids_never_500(admin_client):
    big = 2**63
    assert admin_client.get(f"/api/events/{big}").status_code == 404
    assert admin_client.delete(f"/api/events/{big}").status_code == 404
    assert admin_client.get(f"/api/events/{big}/snapshot.jpg").status_code == 404
    assert admin_client.get(f"/api/events/{big}/thumb.jpg").status_code == 404
    assert admin_client.get(f"/api/events/-{big}").status_code in (404, 422)
    for q in (f"before={big}", f"limit={big}", f"before=-{big}"):
        assert admin_client.get(f"/api/events?{q}").status_code == 422, q
