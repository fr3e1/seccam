import time

from fastapi.testclient import TestClient

from server import auth
from tests.api.conftest import ADMIN_PASSWORD, ADMIN_USER, login, make_admin
from tests.conftest import assert_shape


def post_login(client, username=ADMIN_USER, password=ADMIN_PASSWORD, **extra):
    return client.post("/api/login", json={"username": username, "password": password, **extra})


def test_login_success_sets_cookies(app):
    make_admin(app)
    with TestClient(app) as client:
        resp = post_login(client)
        assert resp.status_code == 200 and resp.json() == {"ok": True}
        cookies = {c.split("=")[0]: c for c in resp.headers.get_list("set-cookie")}
        session, csrf = cookies["seccam_session"].lower(), cookies["seccam_csrf"].lower()
        assert "httponly" in session and "samesite=lax" in session and "max-age=2592000" in session and "path=/" in session
        assert "httponly" not in csrf and "samesite=strict" in csrf
        me = client.get("/api/me")
        assert_shape("Me", me.json())
        assert me.json()["csrf_token"] == client.cookies.get("seccam_csrf")


def test_login_username_is_case_insensitive(app):
    make_admin(app)
    with TestClient(app) as client:
        assert post_login(client, username="ADMIN").status_code == 200


def test_bad_credentials_are_indistinguishable(app):
    make_admin(app)
    with TestClient(app) as client:
        wrong = post_login(client, password="wrong-password-1")
        unknown = post_login(client, username="nobody", password="wrong-password-1")
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json() == {"detail": "Invalid username or password", "code": "bad_credentials"}
        assert_shape("Error", wrong.json())


def test_login_bad_bodies(app):
    make_admin(app)
    with TestClient(app) as client:
        assert client.post("/api/login", content=b"{").status_code == 400
        assert client.post("/api/login", json={"username": "admin"}).status_code == 400
        assert client.post("/api/login", json={"username": 1, "password": "x"}).status_code == 400
        assert post_login(client, totp=5).status_code == 400


def test_login_ip_lock_after_five_failures(app):
    make_admin(app)
    with TestClient(app) as client:
        codes = [post_login(client, password="wrong-password-1").status_code for _ in range(5)]
        assert codes == [401] * 5
        locked = post_login(client)  # correct password is refused while locked
        assert locked.status_code == 429 and locked.json()["code"] == "rate_limited"
        assert int(locked.headers["Retry-After"]) > 0


def test_user_lock_spans_client_ips(app):
    make_admin(app)
    for i in range(4):
        with TestClient(app, client=(f"10.0.1.{i}", 1)) as client:
            for _ in range(5):
                assert post_login(client, password="wrong-password-1").status_code == 401
    with TestClient(app, client=("10.0.2.1", 1)) as other:
        resp = post_login(other)
        assert resp.status_code == 429 and resp.json()["code"] == "rate_limited"
        # a different username from the same fresh IP is not affected
        assert post_login(other, username="someone").status_code == 401


def test_login_does_not_hash_while_locked(app, monkeypatch):
    make_admin(app)
    with TestClient(app) as client:
        for _ in range(5):
            post_login(client, password="wrong-password-1")
        calls = []
        monkeypatch.setattr(auth, "verify_password", lambda *a: calls.append(a) or True)
        assert post_login(client).status_code == 429
        assert calls == []


def test_login_replaces_presented_session(app):
    make_admin(app)
    with TestClient(app) as client:
        login(client)
        old = auth.hash_token(client.cookies.get("seccam_session"))
        assert app.state.db.get_session(old) is not None
        assert post_login(client).status_code == 200
        assert app.state.db.get_session(old) is None


def test_full_totp_flow(app):
    make_admin(app)
    db = app.state.db
    with TestClient(app) as client:
        login(client)
        # setup requires the password
        assert client.post("/api/account/totp/setup", json={"password": "wrong-password-1"}).json()["code"] == "bad_credentials"
        resp = client.post("/api/account/totp/setup", json={"password": ADMIN_PASSWORD})
        assert resp.status_code == 200
        secret = resp.json()["secret"]
        assert resp.json()["otpauth_uri"].startswith("otpauth://totp/SecCam:admin?secret=" + secret)
        assert client.get("/api/me").json()["totp_enabled"] is False
        assert client.post("/api/account/totp/enable", json={"code": "000000"}).json()["code"] == "bad_totp"
        step = int(time.time() // 30)
        enable = client.post("/api/account/totp/enable", json={"code": auth.totp_code(secret, step)})
        assert enable.status_code == 200
        codes = enable.json()["recovery_codes"]
        assert len(codes) == 8
        assert client.get("/api/me").json()["totp_enabled"] is True
        assert client.post("/api/account/totp/setup", json={"password": ADMIN_PASSWORD}).json()["code"] == "totp_already_enabled"
        assert client.post("/api/account/totp/enable", json={"code": "123456"}).json()["code"] == "totp_not_pending"

    with TestClient(app) as fresh:
        # wrong password with 2FA on: bad_credentials, never totp_required
        wrong = post_login(fresh, password="wrong-password-1")
        assert wrong.json()["code"] == "bad_credentials"
        missing = post_login(fresh)
        assert (missing.status_code, missing.json()["code"]) == (401, "totp_required")
        bad = post_login(fresh, totp="000000")
        assert bad.json()["code"] == "bad_totp"
        # the step used at enable time cannot be replayed
        replay = post_login(fresh, totp=auth.totp_code(secret, step))
        assert replay.json()["code"] == "bad_totp"
        ok = post_login(fresh, totp=auth.totp_code(secret, step + 1))
        assert ok.status_code == 200
    with TestClient(app) as fresh:
        # recovery code works once
        assert post_login(fresh, totp=codes[0].upper()).status_code == 200
    with TestClient(app) as fresh:
        assert post_login(fresh, totp=codes[0]).json()["code"] == "bad_totp"
    assert len(db.get_user_by_username("admin")["recovery_codes"]) == 7


def test_totp_disable(app):
    make_admin(app)
    secret = auth.new_totp_secret()
    app.state.db.update_user(1, totp_secret=secret, totp_enabled=True)
    with TestClient(app) as client:
        step = int(time.time() // 30)
        assert post_login(client, totp=auth.totp_code(secret, step)).status_code == 200
        client.headers["X-CSRF-Token"] = client.cookies.get("seccam_csrf")
        resp = client.post("/api/account/totp/disable", json={"password": "wrong-password-1", "code": "x"})
        assert resp.json()["code"] == "bad_credentials"
        resp = client.post("/api/account/totp/disable", json={"password": ADMIN_PASSWORD, "code": "000000"})
        assert resp.json()["code"] == "bad_totp"
        resp = client.post("/api/account/totp/disable",
                           json={"password": ADMIN_PASSWORD, "code": auth.totp_code(secret, step + 1)})
        assert resp.status_code == 200 and resp.json() == {"ok": True}
        user = app.state.db.get_user(1)
        assert (user["totp_enabled"], user["totp_secret"], user["recovery_codes"]) == (False, None, [])
        again = client.post("/api/account/totp/disable", json={"password": ADMIN_PASSWORD, "code": "000000"})
        assert again.json()["code"] == "totp_not_enabled"


def test_logout_deletes_session(admin_client, app):
    sid = auth.hash_token(admin_client.cookies.get("seccam_session"))
    resp = admin_client.post("/api/logout")
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert app.state.db.get_session(sid) is None
    assert admin_client.get("/api/me").status_code == 401


def test_logout_without_session_is_ok(app):
    make_admin(app)
    with TestClient(app) as client:
        assert client.post("/api/logout").status_code == 200


def test_change_password_rotates_session(app):
    make_admin(app)
    with TestClient(app) as a, TestClient(app) as b:
        login(a)
        login(b)
        old_sid = auth.hash_token(a.cookies.get("seccam_session"))
        resp = a.post("/api/account/password", json={"current_password": "wrong-password-1", "new_password": "another-long-pass-9"})
        assert resp.status_code == 401 and resp.json()["code"] == "bad_credentials"
        resp = a.post("/api/account/password", json={"current_password": ADMIN_PASSWORD, "new_password": "short"})
        assert resp.status_code == 422
        resp = a.post("/api/account/password", json={"current_password": ADMIN_PASSWORD, "new_password": "another-long-pass-9"})
        assert resp.status_code == 200 and resp.json() == {"ok": True}
        assert app.state.db.get_session(old_sid) is None
        assert b.get("/api/me").status_code == 401
        a.headers["X-CSRF-Token"] = a.cookies.get("seccam_csrf")
        assert a.get("/api/me").status_code == 200
    with TestClient(app) as c:
        assert post_login(c).status_code == 401
        assert post_login(c, password="another-long-pass-9").status_code == 200


def test_reauth_is_rate_limited(admin_client):
    body = {"current_password": "wrong-password-1", "new_password": "another-long-pass-9"}
    codes = [admin_client.post("/api/account/password", json=body).status_code for _ in range(6)]
    assert codes == [401] * 5 + [429]


def test_revoke_others(app):
    make_admin(app)
    with TestClient(app) as a, TestClient(app) as b:
        login(a)
        login(b)
        resp = a.post("/api/sessions/revoke-others")
        assert resp.status_code == 200 and resp.json() == {"revoked": 1}
        assert b.get("/api/me").status_code == 401
        assert a.get("/api/me").status_code == 200


def test_pin_endpoint_is_gone(admin_client):
    resp = admin_client.post("/api/pin", json={"pin": "1234"})
    assert resp.status_code == 410
    assert resp.json() == {"detail": "PIN login was replaced by username and password", "code": "gone"}


def test_expired_and_idle_sessions_rejected(admin_client, app):
    sid = auth.hash_token(admin_client.cookies.get("seccam_session"))
    conn = app.state.db._conn
    conn.execute("UPDATE sessions SET expires_at = ? WHERE id = ?", (int(time.time() * 1000) - 1, sid))
    assert admin_client.get("/api/me").status_code == 401
    conn.execute("UPDATE sessions SET expires_at = ?, last_seen_at = ? WHERE id = ?",
                 (int(time.time() * 1000) + 10**9, int(time.time() * 1000) - 8 * 86400 * 1000, sid))
    assert admin_client.get("/api/me").status_code == 401


def test_session_touch_at_most_once_per_minute(admin_client, app):
    sid = auth.hash_token(admin_client.cookies.get("seccam_session"))
    old = int(time.time() * 1000) - 120_000
    app.state.db._conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (old, sid))
    admin_client.get("/api/me")
    touched = app.state.db.get_session(sid)["last_seen_at"]
    assert touched > old
    admin_client.get("/api/me")
    assert app.state.db.get_session(sid)["last_seen_at"] == touched


def test_user_lock_ignores_whitespace_and_case_variants(app):
    make_admin(app)
    variants = ["admin", " admin", "admin ", "ADMIN", " Admin "]
    for i in range(20):
        with TestClient(app, client=(f"10.1.{i}.1", 1)) as client:
            assert post_login(client, username=variants[i % 5], password="wrong-password-1").status_code == 401
    with TestClient(app, client=("10.9.9.9", 1)) as fresh:
        for name in ("admin", " admin "):
            resp = post_login(fresh, username=name)
            assert (resp.status_code, resp.json()["code"]) == (429, "rate_limited")


def test_concurrent_bad_logins_are_limited_inside_semaphore(app):
    import asyncio
    import httpx
    make_admin(app)

    async def run():
        transport = httpx.ASGITransport(app=app, client=("10.3.3.3", 1))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await asyncio.gather(*[
                client.post("/api/login", json={"username": "admin", "password": "wrong-password-1"})
                for _ in range(30)])

    codes = [r.status_code for r in asyncio.run(run())]
    assert codes.count(401) <= 5
    assert codes.count(401) + codes.count(429) == 30
