import asyncio
import gzip
import logging

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from server import security as sec
from server.security import (ApiRateLimitMiddleware, BodySizeLimitMiddleware, ProxyHeadersMiddleware, RateLimiter,
                             SecurityHeadersMiddleware, StaticGzipMiddleware)


async def _ok(request):
    return JSONResponse({"ok": True})


async def _echo(request):
    body = await request.body()
    return JSONResponse({"n": len(body)})


async def _static(request):
    return PlainTextResponse("x" * 5000)


async def _server_hdr(request):
    return PlainTextResponse("hi", headers={"server": "leaky"})


def _app():
    return Starlette(routes=[Route("/api/x", _ok, methods=["GET", "POST"]), Route("/api/echo", _echo, methods=["POST"]),
                             Route("/static/a.txt", _static), Route("/page", _ok), Route("/api/srv", _server_hdr)])


# --- security headers ---

def test_headers_http_no_hsts():
    c = TestClient(SecurityHeadersMiddleware(_app()))
    r = c.get("/page", headers={"host": "cam.local:8080"})
    h = r.headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "no-referrer"
    assert h["x-frame-options"] == "DENY"
    assert h["permissions-policy"] == "camera=(self), microphone=(), geolocation=(), payment=(), usb=()"
    assert h["cross-origin-opener-policy"] == "same-origin"
    assert h["cross-origin-resource-policy"] == "same-origin"
    assert h["cache-control"] == "no-store"
    assert "strict-transport-security" not in h
    assert "ws://cam.local:8080" in h["content-security-policy"]


def test_headers_https_hsts_and_wss():
    c = TestClient(SecurityHeadersMiddleware(_app()), base_url="https://cam.example")
    r = c.get("/page")
    assert r.headers["strict-transport-security"] == "max-age=15552000"
    assert "connect-src 'self' wss://cam.example;" in r.headers["content-security-policy"]


def test_headers_cache_control_per_path_and_server_removed():
    c = TestClient(SecurityHeadersMiddleware(_app()))
    assert c.get("/static/a.txt").headers["cache-control"] == "no-cache"
    assert c.get("/api/x").headers["cache-control"] == "no-store"
    assert "server" not in c.get("/api/srv").headers


def test_build_csp_exact():
    expected = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
                "media-src 'self' blob: mediastream:; connect-src 'self' wss://h.example:8443; font-src 'self'; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; "
                "worker-src 'self' blob:; manifest-src 'self'")
    assert sec.build_csp("h.example:8443", "https") == expected
    assert sec.build_csp("[::1]:80", "http").count("ws://[::1]:80") == 1
    for bad in (None, "", "evil.com;script-src *", "a b", "x" * 256):
        assert "connect-src 'self';" in sec.build_csp(bad, "http")


# --- rate limiter ---

def test_rate_limiter_bucket():
    rl = RateLimiter(60, 3)
    assert [rl.hit("k", now=0.0) for _ in range(3)] == [0.0, 0.0, 0.0]
    wait = rl.hit("k", now=0.0)
    assert wait == pytest.approx(1.0)
    assert rl.hit("k", now=1.0) == 0.0
    assert rl.hit("other", now=0.0) == 0.0
    assert rl.hit("k", now=100.0) == 0.0  # refilled, capped at burst
    assert [rl.hit("k", now=100.0) for _ in range(3)][-1] > 0


def test_rate_limiter_key_hash_and_eviction():
    rl = RateLimiter(60, 1, max_keys=3)
    rl.hit("y" * 300, now=0.0)
    assert all(len(k) <= 128 for k in rl._buckets)
    for i in range(5):
        rl.hit(f"k{i}", now=float(i))
    assert len(rl._buckets) == 3 and "k0" not in rl._buckets


def test_api_rate_limit_middleware_429():
    mw = ApiRateLimitMiddleware(_app(), RateLimiter(60, 2))
    c = TestClient(mw)
    assert c.get("/api/x").status_code == 200
    assert c.get("/api/x").status_code == 200
    r = c.get("/api/x")
    assert r.status_code == 429
    assert r.json() == {"detail": "Too many requests", "code": "rate_limited"}
    assert int(r.headers["retry-after"]) >= 1
    assert c.get("/page").status_code == 200  # outside the prefix


# --- body size ---

def test_body_size_content_length_413():
    c = TestClient(BodySizeLimitMiddleware(_app(), 100))
    r = c.post("/api/echo", content=b"x" * 101)
    assert r.status_code == 413
    assert r.json() == {"detail": "Payload too large", "code": "payload_too_large"}
    assert c.post("/api/echo", content=b"x" * 100).json() == {"n": 100}


def test_body_size_streamed_413():
    c = TestClient(BodySizeLimitMiddleware(_app(), 100))

    def gen():
        for _ in range(5):
            yield b"x" * 40

    r = c.post("/api/echo", content=gen())
    assert r.status_code == 413
    assert r.json()["code"] == "payload_too_large"


def test_body_size_ignores_non_api():
    c = TestClient(BodySizeLimitMiddleware(_app(), 10))
    assert c.post("/page", content=b"x" * 100).status_code in (200, 405)


# --- gzip ---

def test_static_gzip_only_static():
    c = TestClient(StaticGzipMiddleware(_app()))
    r = c.get("/static/a.txt", headers={"accept-encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip"
    assert r.text == "x" * 5000
    assert "content-encoding" not in c.get("/api/x", headers={"accept-encoding": "gzip"}).headers


# --- origin / cookie / client_ip ---

def test_origin_helpers():
    assert sec.allowed_origin("https", "a.b:1", "") == "https://a.b:1"
    assert sec.allowed_origin("http", "a", "https://pub.example") == "https://pub.example"
    assert sec.origin_ok(None, "http://a") is True
    assert sec.origin_ok("null", "http://a") is False
    assert sec.origin_ok("HTTP://A", "http://a") is True
    assert sec.origin_ok("http://evil", "http://a") is False


def test_cookie_secure():
    assert sec.cookie_secure("true", "http") is True
    assert sec.cookie_secure("false", "https") is False
    assert sec.cookie_secure("auto", "https") is True
    assert sec.cookie_secure("auto", "http") is False
    assert sec.cookie_secure("auto", "http", "https://x.y") is True
    assert sec.cookie_secure("auto", "http", "http://x.y") is False


def test_client_ip_variants():
    assert sec.client_ip({"client": ("1.2.3.4", 5)}) == "1.2.3.4"
    assert sec.client_ip({"client": None}) == "unknown"
    assert sec.client_ip({}) == "unknown"
    req = Request({"type": "http", "headers": [], "client": ("9.9.9.9", 1)})
    assert sec.client_ip(req) == "9.9.9.9"
    assert sec.client_ip(Request({"type": "http", "headers": []})) == "unknown"


# --- ProxyHeadersMiddleware with synthetic scopes ---

def _run_proxy(trusted, peer, headers, typ="http"):
    seen = {}

    async def app(scope, receive, send):
        seen.update(scope)

    mw = ProxyHeadersMiddleware(app, tuple(trusted))
    scope = {"type": typ, "client": (peer, 1234), "scheme": "http" if typ == "http" else "ws",
             "headers": [(k.encode(), v.encode()) for k, v in headers]}
    asyncio.run(mw(scope, None, None))
    return seen


def test_proxy_untrusted_peer_untouched(caplog):
    with caplog.at_level(logging.WARNING, logger="seccam"):
        s = _run_proxy(["10.0.0.0/8"], "8.8.8.8", [("x-forwarded-for", "1.1.1.1"), ("x-forwarded-proto", "https")])
    assert s["client"] == ("8.8.8.8", 1234) and s["scheme"] == "http"
    assert any("untrusted" in r.message for r in caplog.records)


def test_proxy_warns_once():
    mw = ProxyHeadersMiddleware(lambda *a: asyncio.sleep(0), ())
    scope = {"type": "http", "client": ("8.8.8.8", 1), "headers": [(b"x-forwarded-for", b"1.1.1.1")]}
    logger = logging.getLogger("seccam")
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger.addHandler(handler)
    try:
        asyncio.run(mw(scope, None, None))
        asyncio.run(mw(scope, None, None))
    finally:
        logger.removeHandler(handler)
    assert len(records) == 1


def test_proxy_trusted_takes_rightmost_untrusted():
    s = _run_proxy(["10.0.0.0/8"], "10.0.0.1",
                   [("x-forwarded-for", "6.6.6.6, 1.2.3.4"), ("x-forwarded-for", "10.0.0.7")])
    assert s["client"] == ("1.2.3.4", 0)


def test_proxy_spoofed_leftmost_ignored():
    s = _run_proxy(["127.0.0.1"], "127.0.0.1", [("x-forwarded-for", "6.6.6.6, 5.5.5.5")])
    assert s["client"] == ("5.5.5.5", 0)


def test_proxy_all_trusted_uses_leftmost():
    s = _run_proxy(["10.0.0.0/8"], "10.0.0.1", [("x-forwarded-for", "10.0.0.5, 10.0.0.6")])
    assert s["client"] == ("10.0.0.5", 0)


def test_proxy_invalid_entry_ignores_xff():
    s = _run_proxy(["10.0.0.0/8"], "10.0.0.1", [("x-forwarded-for", "1.2.3.4, bogus")])
    assert s["client"] == ("10.0.0.1", 1234)
    s = _run_proxy(["10.0.0.0/8"], "10.0.0.1", [("x-forwarded-for", "1.2.3.4,")])
    assert s["client"] == ("10.0.0.1", 1234)


def test_proxy_proto_last_value_and_websocket():
    s = _run_proxy(["10.0.0.0/8"], "10.0.0.1", [("x-forwarded-proto", "http, https")])
    assert s["scheme"] == "https"
    s = _run_proxy(["10.0.0.0/8"], "10.0.0.1", [("x-forwarded-proto", "https")], typ="websocket")
    assert s["scheme"] == "wss"
    s = _run_proxy(["10.0.0.0/8"], "10.0.0.1", [("x-forwarded-proto", "ftp")])
    assert s["scheme"] == "http"
    s = _run_proxy(["10.0.0.0/8"], "10.0.0.1", [("x-forwarded-proto", "http")], typ="websocket")
    assert s["scheme"] == "ws"


def test_proxy_ipv6_and_empty_trust_list():
    s = _run_proxy(["::1"], "::1", [("x-forwarded-for", "2001:db8::5")])
    assert s["client"] == ("2001:db8::5", 0)
    s = _run_proxy([], "127.0.0.1", [("x-forwarded-for", "1.2.3.4")])
    assert s["client"] == ("127.0.0.1", 1234)


def test_proxy_passes_lifespan_and_does_not_mutate_scope():
    called = []

    async def app(scope, receive, send):
        called.append(scope["type"])

    mw = ProxyHeadersMiddleware(app, ("10.0.0.0/8",))
    asyncio.run(mw({"type": "lifespan"}, None, None))
    assert called == ["lifespan"]
    scope = {"type": "http", "client": ("10.0.0.1", 1), "headers": [(b"x-forwarded-for", b"1.2.3.4")]}
    asyncio.run(mw(scope, None, None))
    assert scope["client"] == ("10.0.0.1", 1)


def test_limiter_ip_collapses_ipv6_to_64():
    assert sec.limiter_ip("203.0.113.7") == "203.0.113.7"
    assert sec.limiter_ip("2001:db8:1:2:aaaa:bbbb:cccc:dddd") == "2001:db8:1:2::/64"
    assert sec.limiter_ip("2001:db8:1:2::1") == sec.limiter_ip("2001:db8:1:2:ffff::9")
    assert sec.limiter_ip("2001:db8:1:3::1") != sec.limiter_ip("2001:db8:1:2::1")
    assert sec.limiter_ip("::ffff:1.2.3.4") == "1.2.3.4"
    assert sec.limiter_ip("unknown") == "unknown"


def test_rate_limiter_eviction_prefers_refilled_buckets():
    rl = RateLimiter(60, 2, max_keys=2)
    for _ in range(3):
        rl.hit("drained", now=0.0)
    rl.hit("idle", now=0.0)
    rl.hit("new", now=1.0)  # "idle" has refilled by now; "drained" has not
    assert set(rl._buckets) == {"drained", "new"}


def test_unhandled_exception_is_500_json_with_security_headers():
    async def app(scope, receive, send):
        raise RuntimeError("x")

    c = TestClient(SecurityHeadersMiddleware(app), raise_server_exceptions=False, base_url="https://cam.example")
    r = c.get("/api/boom")
    assert r.status_code == 500
    assert r.json() == {"detail": "Internal error", "code": "internal"}
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert "strict-transport-security" in r.headers


def test_exception_after_response_started_is_reraised():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("late")

    mw = SecurityHeadersMiddleware(app)
    sent = []

    async def send(m):
        sent.append(m)

    with pytest.raises(RuntimeError):
        asyncio.run(mw({"type": "http", "path": "/x", "headers": []}, None, send))
    assert [m["type"] for m in sent] == ["http.response.start"]


def test_api_rate_limit_shares_bucket_within_ipv6_64():
    mw = ApiRateLimitMiddleware(_app(), RateLimiter(60, 1))
    seen = []

    async def send(m):
        seen.append(m.get("status"))

    def scope(ip):
        return {"type": "http", "path": "/api/x", "client": (ip, 1), "headers": [], "method": "GET"}

    async def go():
        for ip in ("2001:db8:1:2::1", "2001:db8:1:2:ffff::9", "2001:db8:1:3::1"):
            await mw(scope(ip), None, send)

    async def recv():
        return {"type": "http.request", "body": b""}

    seen.clear()
    asyncio.run(go())
    assert 429 in seen[:3] and seen.count(429) == 1
