import asyncio
import hmac
import ipaddress
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from server import auth, security, updater
from server.config import Config, load_config
from server.db import DB
from server.detector import Detector
from server.hub import Hub, sanitize_detections
from server.version import __version__

logger = logging.getLogger("seccam")

SESSION_RECHECK_S = 60.0
SESSION_TOUCH_MS = 60_000
SESSION_IDLE_MS = 7 * 86400 * 1000
DEVICE_COOKIE_MAX_AGE = 34560000
HELLO_TIMEOUT_S = 10.0
UPDATE_CACHE_S = 600
UPDATE_REFRESH_MIN_S = 30
CAMERA_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RECOVERY_RE = re.compile(r"^[a-z2-9]{4}-[a-z2-9]{4}$")
TOTP_RE = re.compile(r"^[0-9]{6}$")
PUBLIC_API = frozenset({"/api/health", "/api/setup/status", "/api/setup"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PIN_CUTOFF = 20
PIN_GLOBAL_KEY = "setup:pin:global"
STATUS_CODES = {400: "bad_request", 401: "unauthorized", 403: "bad_origin", 404: "not_found",
                405: "method_not_allowed", 413: "payload_too_large", 422: "validation", 429: "rate_limited"}


class ApiError(Exception):
    def __init__(self, status: int, detail: str, code: str, headers: Optional[dict] = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.code = code
        self.headers = headers


def _json_error(status: int, detail: str, code: str, headers: Optional[dict] = None) -> JSONResponse:
    return JSONResponse({"detail": detail, "code": code}, status_code=status, headers=headers)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _bad_request(detail: str = "Invalid request") -> ApiError:
    return ApiError(400, detail, "bad_request")


def _validation(detail: str) -> ApiError:
    return ApiError(422, detail, "validation")


async def read_json(request: Request, allow_empty: bool = False) -> dict:
    raw = await request.body()
    if allow_empty and not raw.strip():
        return {}
    def _no_constants(name: str):
        raise ValueError(f"invalid JSON constant {name}")

    try:
        data = json.loads(raw, parse_constant=_no_constants)
    except (ValueError, RecursionError):
        raise _bad_request()
    if not isinstance(data, dict):
        raise _bad_request()
    return data


def req_str(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise _bad_request()
    return value


def opt_str(data: dict, key: str) -> Optional[str]:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _bad_request()
    return value


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def create_app(cfg: Optional[Config] = None, *, http_get: Optional[Callable[[str, int], bytes]] = None) -> FastAPI:
    cfg = cfg or load_config()
    db = DB(cfg.data_dir)
    detector = Detector(cfg.model_path)
    hub = Hub(db, detector)
    static_dir = Path(cfg.static_dir)
    update_cache: dict[str, Any] = {"info": None, "checked_at": None, "error": None}
    update_lock = asyncio.Lock()

    def purge() -> None:
        try:
            db.purge_older_than(db.get_settings().get("retention_days", 14))
        except Exception:
            logger.exception("Event purge error")
        try:
            db.purge_expired_sessions(_now_ms())
        except Exception:
            logger.exception("Session purge error")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.scrypt_sem = asyncio.Semaphore(2)
        await asyncio.to_thread(purge)
        if db.count_users() == 0:
            token = auth.ensure_setup_token(cfg.data_dir)
            logger.warning("SecCam setup required. Setup token: %s (also in %s)", token,
                           cfg.data_dir / "setup-token")
        if not cfg.cert and not _is_loopback(cfg.host) and not cfg.public_origin.startswith("https://"):
            logger.warning("serving plain HTTP on a non-loopback address; use TLS or a reverse proxy")

        async def purge_loop() -> None:
            while True:
                await asyncio.sleep(3600)
                await asyncio.to_thread(purge)

        task = asyncio.create_task(purge_loop())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await hub.shutdown()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.cfg = cfg
    app.state.db = db
    app.state.hub = hub
    app.state.detector = detector
    app.state.limiter = auth.LoginLimiter()
    app.state.user_limiter = auth.LoginLimiter(max_failures=20)
    app.state.pin_limiter = auth.LoginLimiter(max_failures=5)
    app.state.ws_limiter = security.RateLimiter(30, 10)
    app.state.pairing = auth.PairingCodes()
    app.state.http_get = http_get if http_get is not None else updater.default_http_get
    app.state.scrypt_sem = asyncio.Semaphore(2)

    limiter: auth.LoginLimiter = app.state.limiter
    user_limiter: auth.LoginLimiter = app.state.user_limiter
    pin_limiter: auth.LoginLimiter = app.state.pin_limiter
    ws_limiter: security.RateLimiter = app.state.ws_limiter
    pairing: auth.PairingCodes = app.state.pairing

    # Added innermost first: the last one added is the outermost.
    app.add_middleware(security.StaticGzipMiddleware)
    app.add_middleware(security.BodySizeLimitMiddleware, max_bytes=65536)
    app.add_middleware(security.ApiRateLimitMiddleware, limiter=security.RateLimiter(600, 120))
    app.add_middleware(security.SecurityHeadersMiddleware)
    app.add_middleware(security.ProxyHeadersMiddleware, trusted=cfg.trusted_proxies)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # ---- errors ----

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return _json_error(exc.status, exc.detail, exc.code, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        path = request.url.path
        if path.startswith("/api/") and path not in PUBLIC_API and exc.status_code in (404, 405) \
                and setup_mode():
            return _json_error(403, "Setup required", "setup_required")
        if exc.status_code == 404:
            return _json_error(404, "Not found", "not_found")
        if exc.status_code == 405:
            return _json_error(405, "Method not allowed", "method_not_allowed", dict(exc.headers or {}))
        code = STATUS_CODES.get(exc.status_code, "internal" if exc.status_code >= 500 else "bad_request")
        return _json_error(exc.status_code, str(exc.detail), code, dict(exc.headers or {}) or None)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        errors = exc.errors()
        detail = str(errors[0].get("msg", "Invalid request")) if errors else "Invalid request"
        return _json_error(422, detail, "validation")

    # ---- helpers ----

    def setup_mode() -> bool:
        return db.count_users() == 0

    def sem() -> asyncio.Semaphore:
        return app.state.scrypt_sem

    key_locks: dict[str, list] = {}

    @asynccontextmanager
    async def key_guard(key: str):
        entry = key_locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                key_locks.pop(key, None)

    async def guarded_thread(pairs: tuple, fn: Callable[..., Any], *args: Any) -> Any:
        """Run a credential check one at a time per rate-limit key (the first pair), under the scrypt semaphore.
        The limiters are re-checked right before hashing and a False result is counted as a failure on all pairs
        before the next waiter runs, so concurrent guesses cannot exceed the failure limit."""
        async with key_guard(pairs[0][1]) if pairs else nullcontext():
            async with sem():
                locked(*pairs)
                result = await asyncio.to_thread(fn, *args)
                if not result:
                    failure(*pairs)
                return result

    async def verify_pw(password: str, encoded: str, *pairs: tuple[auth.LoginLimiter, str]) -> bool:
        return await guarded_thread(pairs, auth.verify_password, password, encoded)

    async def hash_pw(password: str) -> str:
        async with sem():
            return await asyncio.to_thread(auth.hash_password, password)

    def locked(*pairs: tuple[auth.LoginLimiter, str]) -> None:
        wait = max((lim.retry_after(key) for lim, key in pairs), default=0)
        if wait > 0 or any(not lim.allowed(key) for lim, key in pairs):
            raise ApiError(429, "Too many attempts, try again later", "rate_limited",
                           {"Retry-After": str(max(1, wait))})

    def failure(*pairs: tuple[auth.LoginLimiter, str]) -> None:
        for lim, key in pairs:
            lim.failure(key)

    def success(*pairs: tuple[auth.LoginLimiter, str]) -> None:
        for lim, key in pairs:
            lim.success(key)

    def check_origin(conn: Any, scheme: str) -> bool:
        expected = security.allowed_origin(scheme, conn.headers.get("host"), cfg.public_origin)
        return security.origin_ok(conn.headers.get("origin"), expected)

    def load_session(sid_hash: str, touch: bool = True) -> Optional[dict]:
        sess = db.get_session(sid_hash)
        if sess is None:
            return None
        now = _now_ms()
        if now >= sess["expires_at"] or now - sess["last_seen_at"] >= SESSION_IDLE_MS:
            return None
        if touch and now - sess["last_seen_at"] >= SESSION_TOUCH_MS:
            db.touch_session(sid_hash, now)
        return {"user_id": sess["user_id"], "sid_hash": sid_hash, "csrf": sess["csrf"],
                "expires_at": sess["expires_at"]}

    def admin_principal(conn: Any) -> Optional[dict]:
        token = conn.cookies.get("seccam_session")
        if not token:
            return None
        return load_session(auth.hash_token(token))

    def device_credential(conn: Any) -> Optional[str]:
        cred = conn.cookies.get("seccam_device")
        if cred:
            return cred
        header = conn.headers.get("authorization")
        return header.strip() if header and header.strip() else None

    def device_principal(conn: Any) -> tuple[bool, Optional[dict]]:
        """(credential present, valid device record or None). Counts no failures itself."""
        cred = device_credential(conn)
        if cred is None:
            return False, None
        parsed = auth.parse_device_token(cred)
        if parsed is None:
            return True, None
        device = db.get_device(parsed[0])
        if device is None:
            return True, None
        if not hmac.compare_digest(auth.hash_token(parsed[1]), device["token_hash"]):
            return True, None
        return True, device

    def guard(request: Request, *, admin: bool = True, csrf: bool = True) -> Optional[dict]:
        path = request.url.path
        if path not in PUBLIC_API and setup_mode():
            raise ApiError(403, "Setup required", "setup_required")
        unsafe = request.method in UNSAFE_METHODS
        if unsafe and not check_origin(request, request.url.scheme):
            raise ApiError(403, "Bad origin", "bad_origin")
        if not admin:
            return None
        principal = admin_principal(request)
        if principal is None:
            raise ApiError(401, "Unauthorized", "unauthorized")
        if unsafe and csrf:
            sent = request.headers.get("x-csrf-token") or ""
            if not hmac.compare_digest(sent.encode("utf-8", "replace"), principal["csrf"].encode()):
                raise ApiError(403, "CSRF token missing or invalid", "csrf_failed")
        return principal

    def page(request: Request, name: str) -> FileResponse:
        return FileResponse(static_dir / name)

    def login_redirect(request: Request) -> RedirectResponse:
        target = request.url.path + ("?" + request.url.query if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)

    def secure_flag(request: Request) -> bool:
        return security.cookie_secure(cfg.cookie_secure, request.url.scheme, cfg.public_origin)

    def set_session_cookies(request: Request, response: Response, token: str, csrf: str) -> None:
        max_age = cfg.session_ttl_days * 86400
        secure = secure_flag(request)
        response.set_cookie("seccam_session", token, max_age=max_age, httponly=True, secure=secure,
                            samesite="lax", path="/")
        response.set_cookie("seccam_csrf", csrf, max_age=max_age, httponly=False, secure=secure,
                            samesite="strict", path="/")

    def clear_session_cookies(response: Response) -> None:
        response.delete_cookie("seccam_session", path="/")
        response.delete_cookie("seccam_csrf", path="/")

    def new_session(request: Request, user_id: int) -> tuple[str, str]:
        token, sid_hash = auth.new_session_token()
        csrf = auth.new_csrf_token()
        expires = _now_ms() + cfg.session_ttl_days * 86400 * 1000
        ua = (request.headers.get("user-agent") or "")[:200]
        db.create_session(sid_hash, user_id, csrf, expires, security.client_ip(request), ua)
        return token, csrf

    async def drop_presented_session(request: Request) -> None:
        token = request.cookies.get("seccam_session")
        if token:
            sid_hash = auth.hash_token(token)
            db.delete_session(sid_hash)
            await hub.disconnect_session(sid_hash)

    def device_view(device: dict, connected_ids: Optional[set] = None) -> dict:
        if connected_ids is None:
            connected_ids = {c["device_id"] for c in hub.cameras() if c["device_id"]}
        public = {k: device[k] for k in ("id", "name", "kind", "created_at", "last_seen_at", "last_ip")}
        public["connected"] = device["id"] in connected_ids
        return public

    async def second_factor(user: dict, code: str) -> bool:
        """Verify a TOTP or recovery code with the CAS rules of §6.2; True iff consumed."""
        code = code.strip()
        if TOTP_RE.match(code):
            step = auth.verify_totp(user["totp_secret"], code, user["totp_last_step"])
            return step is not None and db.consume_totp_step(user["id"], step)
        lowered = code.lower()
        if RECOVERY_RE.match(lowered):
            remaining = auth.use_recovery_code(lowered, user["recovery_codes"])
            return remaining is not None and db.replace_recovery_codes(user["id"], user["recovery_codes"], remaining)
        return False

    async def reauth(principal: dict, password: str) -> tuple[dict, tuple[auth.LoginLimiter, str]]:
        """Re-authentication with the account password; returns (user, limiter key) after the password check."""
        pair = (limiter, f"reauth:user:{principal['user_id']}")
        locked(pair)
        user = db.get_user(principal["user_id"])
        if user is None:
            raise ApiError(401, "Unauthorized", "unauthorized")
        if not await verify_pw(password, user["password_hash"], pair):
            raise ApiError(401, "Invalid password", "bad_credentials")
        return user, pair

    # ---- public API ----

    @app.get("/api/health")
    async def health(request: Request):
        return {"ok": True, "version": __version__, "model_available": detector.available,
                "setup_required": setup_mode()}

    def pin_disabled() -> bool:
        return pin_limiter.total_failures(PIN_GLOBAL_KEY) >= PIN_CUTOFF

    def legacy_credential() -> bool:
        return bool(db.get_auth() or cfg.legacy_pin)

    @app.get("/api/setup/status")
    async def setup_status(request: Request):
        required = setup_mode()
        return {"setup_required": required, "legacy_pin_accepted": required and legacy_credential()
                and not pin_disabled(), "version": __version__}

    async def check_setup_credential(request: Request, data: dict, ip: str) -> list[tuple[auth.LoginLimiter, str]]:
        """Validates the setup token / legacy PIN. Returns the limiter keys to clear on success."""
        token = opt_str(data, "setup_token")
        pin = opt_str(data, "legacy_pin")
        ip_pair = (limiter, f"setup:ip:{security.limiter_ip(ip)}")
        if token:
            locked(ip_pair)
            expected = auth.read_setup_token(cfg.data_dir)
            if not expected or not hmac.compare_digest(token.encode("utf-8", "replace"), expected.encode()):
                failure(ip_pair)
                logger.warning("setup failed: bad setup token from %s", ip)
                raise ApiError(401, "Invalid setup token", "bad_setup_token")
            return [ip_pair]
        if not pin:
            raise ApiError(401, "Invalid setup token", "bad_setup_token")
        if pin_disabled():
            raise ApiError(401, "PIN setup is disabled until restart; use the setup token", "pin_disabled")
        pin_pair = (pin_limiter, PIN_GLOBAL_KEY)
        locked(ip_pair, pin_pair)
        record = db.get_auth()
        if record is not None:
            ok = await guarded_thread((ip_pair, pin_pair), auth.verify_pin_hash, pin, record)
        elif cfg.legacy_pin:
            ok = auth.check_pin(cfg.legacy_pin, pin)
        else:
            failure(ip_pair)
            raise ApiError(401, "Invalid setup token", "bad_setup_token")
        if not ok:
            if record is None:
                failure(ip_pair, pin_pair)
            logger.warning("setup failed: bad legacy PIN from %s", ip)
            raise ApiError(401, "Invalid setup token", "bad_setup_token")
        return [ip_pair, pin_pair]

    @app.post("/api/setup")
    async def setup(request: Request):
        guard(request, admin=False)
        if not setup_mode():
            raise ApiError(409, "Already set up", "already_setup")
        data = await read_json(request)
        ip = security.client_ip(request)
        username_raw = req_str(data, "username")
        password = req_str(data, "password")
        confirm = req_str(data, "password_confirm")
        patch: dict[str, Any] = {}
        retention = data.get("retention_days")
        if retention is not None:
            if isinstance(retention, bool) or not isinstance(retention, int):
                raise _bad_request()
            patch["retention_days"] = retention
        mode, theme, labels = opt_str(data, "mode"), opt_str(data, "theme"), data.get("labels")
        if mode is not None:
            patch["mode"] = mode
        if theme is not None:
            patch["theme"] = theme
        if labels is not None:
            if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
                raise _bad_request()
            patch["labels"] = labels
        try:
            keys = await check_setup_credential(request, data, ip)
        except ApiError:
            if not setup_mode():  # lost a race against a concurrent setup
                raise ApiError(409, "Already set up", "already_setup")
            raise
        try:
            username = auth.validate_username(username_raw)
            auth.validate_password(password, username)
        except ValueError as exc:
            raise _validation(str(exc))
        if password != confirm:
            raise _validation("passwords do not match")
        if patch:
            try:
                db.validate_settings(patch)
            except ValueError as exc:
                raise _validation(str(exc))
        password_hash = await hash_pw(password)
        user = db.create_first_user(username, password_hash)
        if user is None:
            raise ApiError(409, "Already set up", "already_setup")
        if patch:
            await hub.settings_changed(db.update_settings(patch))
        db.delete_legacy_auth()
        auth.clear_setup_token(cfg.data_dir)
        success(*keys)
        await drop_presented_session(request)
        token, csrf = new_session(request, user["id"])
        logger.info("setup complete: admin %r created from %s", username, ip)
        response = JSONResponse({"ok": True})
        set_session_cookies(request, response, token, csrf)
        return response

    @app.post("/api/login")
    async def login(request: Request):
        guard(request, admin=False)
        data = await read_json(request)
        username = req_str(data, "username")
        password = req_str(data, "password")
        totp = data.get("totp")
        if totp is not None and not isinstance(totp, str):
            raise _bad_request()
        ip = security.client_ip(request)
        uname = username.strip()
        pairs = ((limiter, f"login:ip:{security.limiter_ip(ip)}"), (user_limiter, f"login:user:{uname[:64].casefold()}"))
        locked(*pairs)
        user = db.get_user_by_username(uname)
        ok = await verify_pw(password, user["password_hash"] if user else auth.DUMMY_HASH, *pairs)
        if user is None or not ok:
            logger.warning("login failed for %r from %s", uname[:64], ip)
            raise ApiError(401, "Invalid username or password", "bad_credentials")
        if user["totp_enabled"]:
            if not totp or not totp.strip():
                raise ApiError(401, "Two-factor code required", "totp_required")
            if not await second_factor(user, totp):
                failure(*pairs)
                logger.warning("login failed (2FA) for %r from %s", uname[:64], ip)
                raise ApiError(401, "Invalid two-factor code", "bad_totp")
        success(*pairs)
        if auth.needs_rehash(user["password_hash"]):
            db.update_user(user["id"], password_hash=await hash_pw(password))
        await drop_presented_session(request)
        token, csrf = new_session(request, user["id"])
        response = JSONResponse({"ok": True})
        set_session_cookies(request, response, token, csrf)
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        guard(request, admin=False)
        await drop_presented_session(request)
        response = JSONResponse({"ok": True})
        clear_session_cookies(response)
        return response

    @app.api_route("/api/pin", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def pin_gone(request: Request):
        guard(request, admin=False)
        raise ApiError(410, "PIN login was replaced by username and password", "gone")

    @app.post("/api/pair")
    async def pair(request: Request):
        guard(request, admin=False)
        ip = security.client_ip(request)
        pair_key = (limiter, f"pair:ip:{security.limiter_ip(ip)}")
        locked(pair_key)
        data = await read_json(request)
        code = req_str(data, "code")
        name = opt_str(data, "name")
        kind = req_str(data, "kind")
        if name is not None and len(name.strip()) > 64:
            raise _validation("name must be at most 64 characters")
        if kind not in ("browser", "agent"):
            raise _validation("kind must be browser or agent")
        normalized = auth.normalize_pairing_code(code)
        issued = pairing.consume(normalized) if normalized else None
        if issued is None:
            failure(pair_key)
            logger.warning("pairing failed from %s", ip)
            raise ApiError(401, "Invalid or expired pairing code", "bad_pairing_code")
        success(pair_key)
        final_name = ((name or "").strip() or (issued.get("name") or "").strip() or "Camera")[:64]
        device_id = auth.new_device_id()
        token, token_hash = auth.new_device_token(device_id)
        db.create_device(device_id, final_name, kind, token_hash)
        logger.info("device paired: %s (%s) kind=%s from %s", device_id, final_name, kind, ip)
        body: dict[str, Any] = {"device_id": device_id, "name": final_name, "kind": kind}
        if kind == "agent":
            body["token"] = token
        response = JSONResponse(body, status_code=201)
        if kind == "browser":
            response.set_cookie("seccam_device", token, max_age=DEVICE_COOKIE_MAX_AGE, httponly=True,
                                secure=secure_flag(request), samesite="strict", path="/")
        return response

    # ---- account ----

    @app.get("/api/me")
    async def me(request: Request):
        principal = guard(request)
        user = db.get_user(principal["user_id"])
        if user is None:
            raise ApiError(401, "Unauthorized", "unauthorized")
        return {"username": user["username"], "totp_enabled": user["totp_enabled"],
                "csrf_token": principal["csrf"], "session_expires_at": principal["expires_at"]}

    @app.post("/api/account/password")
    async def change_password(request: Request):
        principal = guard(request)
        data = await read_json(request)
        current = req_str(data, "current_password")
        new = req_str(data, "new_password")
        user, pair_key = await reauth(principal, current)
        try:
            auth.validate_password(new, user["username"])
        except ValueError as exc:
            raise _validation(str(exc))
        new_hash = await hash_pw(new)
        db.update_user(user["id"], password_hash=new_hash, password_changed_at=_now_ms())
        old_sids = db.list_session_ids(user["id"])
        db.delete_user_sessions(user["id"])
        for sid in old_sids:
            await hub.disconnect_session(sid)
        success(pair_key)
        token, csrf = new_session(request, user["id"])
        logger.info("password changed for %r", user["username"])
        response = JSONResponse({"ok": True})
        set_session_cookies(request, response, token, csrf)
        return response

    @app.post("/api/account/totp/setup")
    async def totp_setup(request: Request):
        principal = guard(request)
        data = await read_json(request)
        user, pair_key = await reauth(principal, req_str(data, "password"))
        if user["totp_enabled"]:
            raise ApiError(409, "Two-factor authentication is already enabled", "totp_already_enabled")
        secret = auth.new_totp_secret()
        db.update_user(user["id"], totp_secret=secret, totp_enabled=False)
        success(pair_key)
        return {"secret": secret, "otpauth_uri": auth.otpauth_uri(secret, user["username"])}

    @app.post("/api/account/totp/enable")
    async def totp_enable(request: Request):
        principal = guard(request)
        data = await read_json(request)
        code = req_str(data, "code")
        pair_key = (limiter, f"reauth:user:{principal['user_id']}")
        locked(pair_key)
        user = db.get_user(principal["user_id"])
        if user is None:
            raise ApiError(401, "Unauthorized", "unauthorized")
        if user["totp_enabled"] or not user["totp_secret"]:
            raise ApiError(409, "No pending two-factor setup", "totp_not_pending")
        step = auth.verify_totp(user["totp_secret"], code.strip(), user["totp_last_step"])
        if step is None or not db.consume_totp_step(user["id"], step):
            failure(pair_key)
            raise ApiError(401, "Invalid two-factor code", "bad_totp")
        codes, hashes = auth.new_recovery_codes(8)
        db.update_user(user["id"], totp_enabled=True, recovery_codes=hashes)
        success(pair_key)
        logger.info("2FA enabled for %r", user["username"])
        return {"recovery_codes": codes}

    @app.post("/api/account/totp/disable")
    async def totp_disable(request: Request):
        principal = guard(request)
        data = await read_json(request)
        password = req_str(data, "password")
        code = req_str(data, "code")
        user, pair_key = await reauth(principal, password)
        if not user["totp_enabled"]:
            raise ApiError(409, "Two-factor authentication is not enabled", "totp_not_enabled")
        if not await second_factor(user, code):
            failure(pair_key)
            raise ApiError(401, "Invalid two-factor code", "bad_totp")
        db.update_user(user["id"], totp_secret=None, totp_enabled=False, totp_last_step=0, recovery_codes=[])
        success(pair_key)
        logger.info("2FA disabled for %r", user["username"])
        return {"ok": True}

    @app.post("/api/sessions/revoke-others")
    async def revoke_others(request: Request):
        principal = guard(request)
        others = [s for s in db.list_session_ids(principal["user_id"]) if s != principal["sid_hash"]]
        revoked = db.delete_user_sessions(principal["user_id"], except_id=principal["sid_hash"])
        for sid in others:
            await hub.disconnect_session(sid)
        return {"revoked": revoked}

    # ---- settings, events, cameras ----

    @app.get("/api/settings")
    async def get_settings(request: Request):
        guard(request)
        return db.get_settings()

    @app.put("/api/settings")
    async def put_settings(request: Request):
        guard(request)
        patch = await read_json(request)
        try:
            settings = db.update_settings(patch)
        except ValueError as exc:
            raise _validation(str(exc))
        await hub.settings_changed(settings)
        return settings

    MAX_ID = 2**63 - 1

    def int_param(request: Request, name: str, default: Optional[int]) -> Optional[int]:
        raw = request.query_params.get(name)
        if raw is None or raw == "":
            return default
        try:
            value = int(raw)
        except ValueError:
            raise _validation(f"{name} must be an integer")
        if not -MAX_ID <= value <= MAX_ID:
            raise _validation(f"{name} is out of range")
        return value

    @app.get("/api/events")
    async def list_events(request: Request):
        guard(request)
        limit = int_param(request, "limit", 50)
        if limit < 1:
            raise _validation("limit must be at least 1")
        before = int_param(request, "before", None)
        events = await asyncio.to_thread(
            db.list_events, limit=min(limit, 200), before_id=before,
            label=request.query_params.get("label") or None, camera_id=request.query_params.get("camera_id") or None)
        return {"events": events}

    @app.get("/api/events/{event_id}")
    async def get_event(event_id: int, request: Request):
        guard(request)
        event = db.get_event(event_id) if 0 <= event_id <= MAX_ID else None
        if event is None:
            raise ApiError(404, "Not found", "not_found")
        return event

    @app.delete("/api/events/{event_id}")
    async def delete_event(event_id: int, request: Request):
        guard(request)
        if not 0 <= event_id <= MAX_ID or not await asyncio.to_thread(db.delete_event, event_id):
            raise ApiError(404, "Not found", "not_found")
        return {"ok": True}

    def event_image(kind: str):
        async def handler(event_id: int, request: Request):
            guard(request)
            path = db.event_file(event_id, kind) if 0 <= event_id <= MAX_ID else None
            if not path or not path.exists():
                return Response(status_code=404)
            return FileResponse(path, media_type="image/jpeg")
        return handler

    app.get("/api/events/{event_id}/snapshot.jpg")(event_image("snapshot"))
    app.get("/api/events/{event_id}/thumb.jpg")(event_image("thumb"))

    @app.get("/api/cameras")
    async def get_cameras(request: Request):
        guard(request)
        return {"cameras": hub.cameras()}

    @app.get("/api/stats")
    async def get_stats(request: Request):
        guard(request)
        return await asyncio.to_thread(db.stats)

    # ---- devices ----

    @app.get("/api/devices")
    async def list_devices(request: Request):
        guard(request)
        connected = {c["device_id"] for c in hub.cameras() if c["device_id"]}
        return {"devices": [device_view(d, connected) for d in db.list_devices()]}

    @app.post("/api/devices/pairing")
    async def create_pairing(request: Request):
        guard(request)
        data = await read_json(request, allow_empty=True)
        name = opt_str(data, "name")
        if name is not None and len(name.strip()) > 64:
            raise _validation("name must be at most 64 characters")
        try:
            code, expires_at = pairing.issue((name or "").strip() or None)
        except ValueError:
            raise ApiError(409, "Too many pairing codes are active", "too_many_codes")
        return JSONResponse({"code": code, "expires_at": expires_at}, status_code=201)

    @app.patch("/api/devices/{device_id}")
    async def rename_device(device_id: str, request: Request):
        guard(request)
        data = await read_json(request)
        name = req_str(data, "name").strip()
        if not 1 <= len(name) <= 64:
            raise _validation("name must be 1-64 characters")
        device = db.rename_device(device_id, name)
        if device is None:
            raise ApiError(404, "Not found", "not_found")
        return device_view(db.get_device(device_id) or device)

    @app.delete("/api/devices/{device_id}")
    async def delete_device(device_id: str, request: Request):
        guard(request)
        if not db.delete_device(device_id):
            raise ApiError(404, "Not found", "not_found")
        await hub.disconnect_device(device_id)
        logger.info("device revoked: %s", device_id)
        return Response(status_code=204)

    # ---- updates ----

    def update_view(enabled: bool) -> dict:
        info = update_cache["info"]
        latest = None
        available = False
        if info is not None:
            latest = {"version": info.version, "published_at": info.published_at, "notes": info.notes,
                      "html_url": info.html_url}
            available = updater.is_newer(info.version, __version__)
        return {"current_version": __version__, "enabled": enabled, "update_available": available,
                "latest": latest, "checked_at": update_cache["checked_at"], "error": update_cache["error"],
                "status": updater.read_status(cfg.data_dir)}

    async def fetch_release() -> Any:
        return await asyncio.to_thread(updater.fetch_latest_release, app.state.http_get, cfg.update_repo)

    @app.get("/api/update")
    async def get_update(request: Request):
        guard(request)
        async with update_lock:
            now = _now_ms()
            checked = update_cache["checked_at"]
            age_s = None if checked is None else (now - checked) / 1000
            refresh = request.query_params.get("refresh") == "1"
            if age_s is None or age_s > UPDATE_CACHE_S or (refresh and age_s >= UPDATE_REFRESH_MIN_S):
                try:
                    update_cache["info"] = await fetch_release()
                    update_cache["error"] = None
                except updater.UpdateError as exc:
                    update_cache["info"] = None
                    update_cache["error"] = str(exc)[:200] or "update check failed"
                update_cache["checked_at"] = now
            return update_view(cfg.update_mode == "systemd")

    @app.post("/api/update/apply")
    async def apply_update(request: Request):
        principal = guard(request)
        data = await read_json(request)
        version = req_str(data, "version")
        password = req_str(data, "password")
        user, pair_key = await reauth(principal, password)
        success(pair_key)
        if cfg.update_mode != "systemd":
            raise ApiError(409, "Updates are disabled on this install", "updates_disabled")
        if not updater.VERSION_RE.match(version):
            raise _validation("version must look like X.Y.Z")
        try:
            latest = await fetch_release()
        except updater.UpdateError as exc:
            raise ApiError(502, f"Could not check for updates: {str(exc)[:200]}", "upstream_error")
        update_cache.update(info=latest, error=None, checked_at=_now_ms())
        if version != latest.version:
            raise ApiError(409, "That is not the latest release", "not_latest")
        if not updater.is_newer(version, __version__):
            raise ApiError(409, "That version is not newer than the running one", "not_newer")
        try:
            updater.request_update(cfg.data_dir, version, "admin")
        except updater.UpdateError:
            raise ApiError(409, "An update is already pending", "update_pending")
        except ValueError as exc:
            raise _validation(str(exc))
        logger.info("update to %s requested by %r", version, user["username"])
        return JSONResponse({"ok": True, "status": updater.read_status(cfg.data_dir)}, status_code=202)

    # ---- HTML ----

    @app.get("/login")
    async def login_page(request: Request):
        if setup_mode():
            return RedirectResponse("/welcome", status_code=303)
        return page(request, "login.html")

    @app.get("/welcome")
    async def welcome_page(request: Request):
        if not setup_mode():
            return RedirectResponse("/", status_code=303)
        return page(request, "welcome.html")

    @app.get("/pair")
    async def pair_page(request: Request):
        if setup_mode():
            return RedirectResponse("/welcome", status_code=303)
        return page(request, "pair.html")

    @app.get("/setup")
    async def setup_page(request: Request):
        return page(request, "setup.html")

    @app.get("/ca.crt")
    async def ca_cert(request: Request):
        if cfg.ca_cert and Path(cfg.ca_cert).is_file():
            return FileResponse(cfg.ca_cert, media_type="application/x-x509-ca-cert")
        raise ApiError(404, "Not found", "not_found")

    @app.get("/")
    async def index(request: Request):
        if setup_mode():
            return RedirectResponse("/welcome", status_code=303)
        if admin_principal(request) is None:
            return login_redirect(request)
        return page(request, "index.html")

    @app.get("/camera")
    async def camera_page(request: Request):
        if setup_mode():
            return RedirectResponse("/welcome", status_code=303)
        if admin_principal(request) is not None:
            return page(request, "camera.html")
        dev_pair = (limiter, f"device:ip:{security.limiter_ip(security.client_ip(request))}")
        present, device = device_principal(request)
        if present:
            locked(dev_pair)
            if device is None:
                failure(dev_pair)
            else:
                success(dev_pair)
                db.touch_device(device["id"], _now_ms(), security.client_ip(request))
                return page(request, "camera.html")
        return RedirectResponse("/pair?next=/camera", status_code=303)

    # ---- WebSockets ----

    class Socket:
        """A WebSocket with serialised sends and a close that never raises."""

        def __init__(self, ws: WebSocket) -> None:
            self.ws = ws
            self._lock = asyncio.Lock()

        async def send_json(self, msg: dict) -> None:
            async with self._lock:
                await self.ws.send_text(json.dumps(msg))

        async def send_bytes(self, data: bytes) -> None:
            async with self._lock:
                await self.ws.send_bytes(data)

        async def close(self, code: int) -> None:
            try:
                await self.ws.close(code=code)
            except Exception:
                logger.debug("websocket close(%s) failed", code, exc_info=True)

    async def ws_reject(ws: WebSocket, code: int) -> None:
        await ws.accept()
        await Socket(ws).close(code)

    async def ws_preflight(ws: WebSocket) -> bool:
        """Rate limit, setup mode, Origin. Accepts and closes the socket when it returns False."""
        if ws_limiter.hit(f"ws:ip:{security.limiter_ip(security.client_ip(ws))}") > 0:
            await ws_reject(ws, 4429)
            return False
        if setup_mode():
            await ws_reject(ws, 4403)
            return False
        scheme = "https" if ws.url.scheme == "wss" else "http"
        if not check_origin(ws, scheme):
            await ws_reject(ws, 4403)
            return False
        return True

    async def watch_session(sock: Socket, sid_hash: str) -> None:
        while True:
            await asyncio.sleep(SESSION_RECHECK_S)
            if await asyncio.to_thread(load_session, sid_hash) is None:
                await sock.close(4401)
                return

    async def watch_device(sock: Socket, device_id: str, token_hash: str) -> None:
        while True:
            await asyncio.sleep(SESSION_RECHECK_S)
            current = await asyncio.to_thread(db.get_device, device_id)
            if current is None or not hmac.compare_digest(current["token_hash"], token_hash):
                await sock.close(4401)
                return

    async def recv(ws: WebSocket, timeout: Optional[float] = None) -> Optional[dict]:
        """Next message, or None on disconnect."""
        msg = await (asyncio.wait_for(ws.receive(), timeout) if timeout else ws.receive())
        return None if msg.get("type") == "websocket.disconnect" else msg

    def parse_text(msg: dict) -> Optional[dict]:
        if msg.get("text") is None:
            return None
        try:
            data = json.loads(msg["text"])
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    @app.websocket("/ws/camera")
    async def ws_camera(websocket: WebSocket):
        if not await ws_preflight(websocket):
            return
        ip = security.client_ip(websocket)
        principal = admin_principal(websocket)
        device: Optional[dict] = None
        if principal is None:
            dev_pair = (limiter, f"device:ip:{security.limiter_ip(ip)}")
            present, device = device_principal(websocket)
            if present and not limiter.allowed(dev_pair[1]):
                await ws_reject(websocket, 4429)
                return
            if device is None:
                if present:
                    failure(dev_pair)
                await ws_reject(websocket, 4401)
                return
            success(dev_pair)
            db.touch_device(device["id"], _now_ms(), ip)
        await websocket.accept()
        sock = Socket(websocket)
        camera_id: Optional[str] = None
        conn_token = object()
        watcher: Optional[asyncio.Task] = None
        try:
            try:
                first = await recv(websocket, HELLO_TIMEOUT_S)
            except asyncio.TimeoutError:
                await sock.close(4400)
                return
            hello = parse_text(first) if first else None
            if hello is None or hello.get("type") != "hello":
                await sock.close(4400)
                return
            name = hello.get("name", "")
            if not isinstance(name, str):
                await sock.close(4400)
                return
            name = name.strip()[:64]
            if device is not None:
                camera_id = device["id"]
                name = name or device["name"]
                kind = device["kind"]
            else:
                camera_id = hello.get("camera_id")
                if not isinstance(camera_id, str) or not CAMERA_ID_RE.match(camera_id):
                    camera_id = None
                    await sock.close(4400)
                    return
                kind = hello.get("kind") if hello.get("kind") in ("browser", "agent") else "browser"
            await hub.camera_connected(
                camera_id, name, sock.send_json, conn_token=conn_token,
                device_id=device["id"] if device else None, close=sock.close, kind=kind,
                session_id=principal["sid_hash"] if principal else None)
            if principal:
                watcher = asyncio.create_task(watch_session(sock, principal["sid_hash"]))
            elif device is not None:
                watcher = asyncio.create_task(watch_device(sock, device["id"], device["token_hash"]))
            while True:
                msg = await recv(websocket)
                if msg is None:
                    break
                if msg.get("bytes") is not None:
                    await hub.camera_frame(camera_id, msg["bytes"])
                    continue
                data = parse_text(msg)
                if data is None:
                    continue
                kind_of = data.get("type")
                if kind_of == "mode":
                    if data.get("mode") in ("local", "server"):
                        await hub.camera_mode(camera_id, data["mode"])
                elif kind_of == "detections":
                    dets, frame_w, frame_h, infer_ms = sanitize_detections(data)
                    await hub.camera_detections(camera_id, dets, frame_w, frame_h, infer_ms)
                elif kind_of == "ping":
                    await sock.send_json({"type": "pong"})
        except Exception:
            logger.debug("camera websocket ended", exc_info=True)
        finally:
            if watcher:
                watcher.cancel()
            if camera_id:
                await hub.camera_disconnected(camera_id, conn_token=conn_token)

    @app.websocket("/ws/viewer")
    async def ws_viewer(websocket: WebSocket):
        if not await ws_preflight(websocket):
            return
        principal = admin_principal(websocket)
        if principal is None:
            await ws_reject(websocket, 4401)
            return
        await websocket.accept()
        sock = Socket(websocket)
        viewer_id = uuid.uuid4().hex
        proto2 = websocket.query_params.get("proto") == "2"
        watcher = asyncio.create_task(watch_session(sock, principal["sid_hash"]))
        registered = False
        try:
            await hub.viewer_connected(viewer_id, sock.send_json, sock.send_bytes if proto2 else None,
                                       close=sock.close, session_id=principal["sid_hash"])
            registered = True
            while True:
                msg = await recv(websocket)
                if msg is None:
                    break
                data = parse_text(msg)
                if data is not None and data.get("type") == "ping":
                    await sock.send_json({"type": "pong"})
        except Exception:
            logger.debug("viewer websocket ended", exc_info=True)
        finally:
            watcher.cancel()
            if registered:
                await hub.viewer_disconnected(viewer_id)

    return app
