"""Pure-ASGI security middlewares and request helpers."""
import ipaddress
import json
import logging
import math
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

from starlette.middleware.gzip import GZipMiddleware

from .auth import norm_limiter_key

log = logging.getLogger("seccam")

_HOST_RE = re.compile(r"^[A-Za-z0-9.\-\[\]:]{1,255}$")


def _headers(scope: dict, name: bytes) -> list[str]:
    return [v.decode("latin-1") for k, v in scope.get("headers", []) if k.lower() == name]


def client_ip(scope_or_conn: Any) -> str:
    """Peer IP from an ASGI scope dict, a Request or a WebSocket; "unknown" if absent."""
    if isinstance(scope_or_conn, dict):
        client = scope_or_conn.get("client")
        return str(client[0]) if client else "unknown"
    client = getattr(scope_or_conn, "client", None)
    host = getattr(client, "host", None)
    return str(host) if host else "unknown"


def limiter_ip(ip: str) -> str:
    """Limiter-key form of an address: IPv4 unchanged, IPv6 collapsed to its /64 (a /64 must not rotate past a lock)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6:
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def allowed_origin(scheme: str, host: Optional[str], public_origin: str) -> str:
    return public_origin if public_origin else f"{scheme}://{host}"


def origin_ok(origin: Optional[str], expected: str) -> bool:
    if origin is None:
        return True
    if origin == "null":
        return False
    return origin.casefold() == expected.casefold()


def cookie_secure(mode: str, scheme: str, public_origin: str = "") -> bool:
    if mode == "true":
        return True
    if mode == "false":
        return False
    return scheme == "https" or public_origin.startswith("https://")


def build_csp(host: Optional[str], scheme: str) -> str:
    ws = ""
    if host and _HOST_RE.match(host):
        ws = " " + ("wss" if scheme == "https" else "ws") + "://" + host
    return ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
            "media-src 'self' blob: mediastream:; "
            f"connect-src 'self'{ws}; "
            "font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; worker-src 'self' blob:; manifest-src 'self'")


class RateLimiter:
    """Token bucket per key."""

    def __init__(self, rate_per_min: int, burst: int, max_keys: int = 10000) -> None:
        self._rate = rate_per_min / 60.0
        self._burst = float(burst)
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._buckets: "OrderedDict[str, tuple[float, float]]" = OrderedDict()  # key -> (tokens, last)

    def hit(self, key: str, now: Optional[float] = None) -> float:
        key = norm_limiter_key(key)
        now = time.monotonic() if now is None else now
        with self._lock:
            tokens, last = self._buckets.pop(key, (self._burst, now))
            tokens = min(self._burst, tokens + max(0.0, now - last) * self._rate)
            if tokens >= 1.0:
                tokens -= 1.0
                wait = 0.0
            else:
                wait = (1.0 - tokens) / self._rate if self._rate > 0 else 3600.0
            self._buckets[key] = (tokens, now)
            while len(self._buckets) > self._max_keys:
                # prefer evicting a bucket that has fully refilled (no state lost); else the oldest
                victim = next((k for k, (t, last_) in self._buckets.items()
                               if k != key and t + (now - last_) * self._rate >= self._burst), None)
                if victim is None:
                    victim = next(iter(self._buckets))
                del self._buckets[victim]
            return wait


async def _send_json(send, status: int, body: dict, extra: Optional[list] = None) -> None:
    payload = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    headers.extend(extra or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


class ProxyHeadersMiddleware:
    def __init__(self, app, trusted: tuple[str, ...] = ()) -> None:
        self.app = app
        self._nets = [ipaddress.ip_network(t, strict=False) for t in trusted]
        self._warned = False

    def _is_trusted(self, ip: ipaddress._BaseAddress) -> bool:
        return any(ip in net for net in self._nets if net.version == ip.version)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        try:
            peer = ipaddress.ip_address(client[0]) if client else None
        except ValueError:
            peer = None
        if peer is None or not self._is_trusted(peer):
            if not self._warned and (_headers(scope, b"x-forwarded-for") or _headers(scope, b"x-forwarded-proto")):
                self._warned = True
                log.warning("X-Forwarded-* headers received from untrusted peer %s; ignoring "
                            "(set SECCAM_TRUSTED_PROXIES if behind a reverse proxy)", client[0] if client else "?")
            await self.app(scope, receive, send)
            return
        scope = dict(scope)
        xff = ",".join(_headers(scope, b"x-forwarded-for"))
        if xff:
            try:
                entries = [ipaddress.ip_address(e.strip()) for e in xff.split(",")]
            except ValueError:
                entries = []
            if entries:
                chosen = entries[0]
                for entry in reversed(entries):
                    if not self._is_trusted(entry):
                        chosen = entry
                        break
                scope["client"] = (str(chosen), 0)
        protos = [p.strip().lower() for line in _headers(scope, b"x-forwarded-proto") for p in line.split(",")]
        if protos and protos[-1] in ("http", "https"):
            secure = protos[-1] == "https"
            scope["scheme"] = ("wss" if secure else "ws") if scope["type"] == "websocket" \
                else ("https" if secure else "http")
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        scheme = scope.get("scheme", "http")
        hosts = _headers(scope, b"host")
        csp = build_csp(hosts[0] if hosts else None, scheme)
        static = scope.get("path", "").startswith("/static/")

        started = False

        async def send_wrapper(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                replaced = {b"server", b"x-content-type-options", b"referrer-policy", b"x-frame-options",
                            b"permissions-policy", b"cross-origin-opener-policy", b"cross-origin-resource-policy",
                            b"content-security-policy", b"strict-transport-security", b"cache-control"}
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in replaced]
                headers += [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (b"permissions-policy", b"camera=(self), microphone=(), geolocation=(), payment=(), usb=()"),
                    (b"cross-origin-opener-policy", b"same-origin"),
                    (b"cross-origin-resource-policy", b"same-origin"),
                    (b"content-security-policy", csp.encode("latin-1")),
                    (b"cache-control", b"no-cache" if static else b"no-store"),
                ]
                if scheme == "https":
                    headers.append((b"strict-transport-security", b"max-age=15552000"))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            if started:
                raise
            log.exception("unhandled exception")
            payload = json.dumps({"detail": "Internal error", "code": "internal"}).encode()
            await send_wrapper({"type": "http.response.start", "status": 500, "headers": [
                (b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]})
            await send_wrapper({"type": "http.response.body", "body": payload})


class ApiRateLimitMiddleware:
    def __init__(self, app, limiter: RateLimiter, prefix: str = "/api/") -> None:
        self.app = app
        self.limiter = limiter
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith(self.prefix):
            wait = self.limiter.hit("api:ip:" + limiter_ip(client_ip(scope)))
            if wait > 0:
                retry = max(1, math.ceil(wait))
                await _send_json(send, 429, {"detail": "Too many requests", "code": "rate_limited"},
                                 [(b"retry-after", str(retry).encode())])
                return
        await self.app(scope, receive, send)


class BodySizeLimitMiddleware:
    def __init__(self, app, max_bytes: int = 65536) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return
        too_large = False
        started = False
        try:
            declared = int(next(iter(_headers(scope, b"content-length")), "0"))
        except ValueError:
            declared = 0
        if declared > self.max_bytes:
            await _send_json(send, 413, {"detail": "Payload too large", "code": "payload_too_large"})
            return
        received = 0

        async def receive_wrapper():
            nonlocal received, too_large
            if too_large:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    too_large = True
                    return {"type": "http.disconnect"}
            return message

        async def send_wrapper(message):
            nonlocal started
            if too_large:
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except Exception:
            if not too_large:
                raise
        if too_large and not started:
            await _send_json(send, 413, {"detail": "Payload too large", "code": "payload_too_large"})


class StaticGzipMiddleware:
    def __init__(self, app, minimum_size: int = 1024) -> None:
        self.app = app
        self._gzip = GZipMiddleware(app, minimum_size=minimum_size)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/static/"):
            await self._gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)
