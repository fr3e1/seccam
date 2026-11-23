from fastapi.testclient import TestClient

from server.app import create_app
from server.config import load_config
from server.security import RateLimiter
from tests.api.conftest import ADMIN_PASSWORD, make_admin


def test_docs_disabled(admin_client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert admin_client.get(path).status_code == 404, path


def test_security_headers_on_api_html_and_static(admin_client):
    for path in ("/api/health", "/", "/static/css/base.css", "/nope"):
        resp = admin_client.get(path)
        h = resp.headers
        assert h["x-content-type-options"] == "nosniff", path
        assert h["referrer-policy"] == "no-referrer"
        assert h["x-frame-options"] == "DENY"
        assert h["permissions-policy"].startswith("camera=(self)")
        assert h["cross-origin-opener-policy"] == "same-origin"
        assert h["cross-origin-resource-policy"] == "same-origin"
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert "ws://testserver" in h["content-security-policy"]
        assert "strict-transport-security" not in h
        assert "server" not in h
    assert admin_client.get("/api/health").headers["cache-control"] == "no-store"
    assert admin_client.get("/static/css/base.css").headers["cache-control"] == "no-cache"


def test_hsts_and_secure_cookies_on_https(app):
    make_admin(app)
    with TestClient(app, base_url="https://testserver") as client:
        resp = client.post("/api/login", json={"username": "admin", "password": ADMIN_PASSWORD})
        assert resp.headers["strict-transport-security"] == "max-age=15552000"
        assert all("secure" in c.lower() for c in resp.headers.get_list("set-cookie"))
        assert "wss://testserver" in resp.headers["content-security-policy"]


def test_cookie_secure_false_override(env, monkeypatch):
    monkeypatch.setenv("SECCAM_COOKIE_SECURE", "true")
    application = create_app(load_config())
    make_admin(application)
    with TestClient(application) as client:
        resp = client.post("/api/login", json={"username": "admin", "password": ADMIN_PASSWORD})
        assert all("secure" in c.lower() for c in resp.headers.get_list("set-cookie"))


def test_xff_ignored_from_untrusted_peer(app):
    make_admin(app)
    with TestClient(app, client=("10.0.0.5", 1)) as client:
        for i in range(5):
            resp = client.post("/api/login", json={"username": "admin", "password": "wrong-password-1"},
                               headers={"X-Forwarded-For": f"203.0.113.{i}"})
            assert resp.status_code == 401
        resp = client.post("/api/login", json={"username": "admin", "password": ADMIN_PASSWORD},
                           headers={"X-Forwarded-For": "203.0.113.99"})
        assert resp.status_code == 429


def test_xff_used_from_trusted_proxy(env, monkeypatch):
    monkeypatch.setenv("SECCAM_TRUSTED_PROXIES", "10.0.0.0/8")
    application = create_app(load_config())
    make_admin(application)
    with TestClient(application, client=("10.0.0.5", 1)) as client:
        for _ in range(5):
            client.post("/api/login", json={"username": "admin", "password": "wrong-password-1"},
                        headers={"X-Forwarded-For": "203.0.113.7"})
        locked = client.post("/api/login", json={"username": "admin", "password": ADMIN_PASSWORD},
                             headers={"X-Forwarded-For": "203.0.113.7"})
        assert locked.status_code == 429
        # another real client behind the same proxy is judged on its own IP (user lock aside, use another name)
        other = client.post("/api/login", json={"username": "zed", "password": "wrong-password-1"},
                            headers={"X-Forwarded-For": "203.0.113.8"})
        assert other.status_code == 401


def test_api_rate_limit_middleware(app, monkeypatch):
    # freeze the bucket clock: the 600/min bucket refills 10 tokens/s, so real time makes the count flaky
    real_hit = RateLimiter.hit
    monkeypatch.setattr(RateLimiter, "hit", lambda self, key, now=None: real_hit(self, key, 1000.0))
    with TestClient(app) as client:
        codes = [client.get("/api/health").status_code for _ in range(130)]
        assert codes[:120] == [200] * 120 and 429 in codes[120:]
        resp = client.get("/api/health")
        assert resp.json()["code"] == "rate_limited" and "retry-after" in resp.headers
