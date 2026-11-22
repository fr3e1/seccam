import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from server.auth import LoginLimiter, check_pin, make_token, verify_token, hash_pin, verify_pin_hash, derive_token_secret, PIN_RE
from server.config import Config, load_config
from server.db import DB, DEFAULT_SETTINGS, ALL_LABELS
from server.detector import Detector
from server.hub import Hub

logger = logging.getLogger("seccam")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if not request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        return response


def create_app(cfg: Optional[Config] = None) -> FastAPI:
    cfg = cfg or load_config()
    db = DB(cfg.data_dir)
    detector = Detector(cfg.model_path)
    hub = Hub(db, detector)
    limiter = LoginLimiter()

    # Cache for auth record (refreshed on set_auth)
    _auth_cache: Dict[str, Optional[Dict[str, Any]]] = {"record": None}

    def get_cached_auth() -> Optional[Dict[str, Any]]:
        if _auth_cache["record"] is None:
            _auth_cache["record"] = db.get_auth()
        return _auth_cache["record"]

    def invalidate_auth_cache() -> None:
        _auth_cache["record"] = None

    def get_pin_component() -> str:
        """Return the PIN component used for token derivation: DB hash or env PIN."""
        auth_rec = get_cached_auth()
        if auth_rec:
            return auth_rec["pin_hash"]
        return cfg.pin

    def get_token_secret() -> str:
        """Derive the token HMAC secret from base secret and PIN component."""
        return derive_token_secret(cfg.secret, get_pin_component())

    def is_setup_mode() -> bool:
        """Check if we're in setup mode (no PIN configured anywhere)."""
        auth_rec = get_cached_auth()
        if auth_rec:
            return False
        return not cfg.pin

    def get_setup_status() -> Dict[str, Any]:
        auth_rec = get_cached_auth()
        if auth_rec:
            pin_source = "db"
        elif cfg.pin:
            pin_source = "env"
        else:
            pin_source = "none"
        return {
            "setup_required": pin_source == "none",
            "pin_source": pin_source,
            "version": "1.1.0",
        }

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def purge_loop():
            while True:
                await asyncio.sleep(3600)
                try:
                    settings = db.get_settings()
                    retention = settings.get("retention_days", 14)
                    await asyncio.to_thread(db.purge_older_than, retention)
                except Exception:
                    logger.exception("Purge task error")

        task = asyncio.create_task(purge_loop())
        try:
            settings = db.get_settings()
            retention = settings.get("retention_days", 14)
            await asyncio.to_thread(db.purge_older_than, retention)
        except Exception:
            logger.exception("Initial purge error")
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await hub.shutdown()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(SecurityHeadersMiddleware)

    app.state.cfg = cfg
    app.state.db = db
    app.state.hub = hub
    app.state.detector = detector
    app.state.limiter = limiter

    app.mount("/static", StaticFiles(directory=cfg.static_dir), name="static")

    def get_session(request: Request) -> Optional[str]:
        return request.cookies.get("seccam_session")

    def require_auth(request: Request) -> bool:
        token = get_session(request)
        return verify_token(get_token_secret(), token)

    def auth_redirect(request: Request) -> RedirectResponse:
        next_url = request.url.path
        if request.url.query:
            next_url += "?" + request.url.query
        return RedirectResponse(f"/login?next={quote(next_url)}", status_code=303)

    def check_setup_mode(request: Request) -> Optional[Response]:
        """Check if setup mode is active and return appropriate response for protected routes."""
        if is_setup_mode():
            if request.url.path.startswith("/api/"):
                # Allow public API endpoints
                public_paths = {"/api/login", "/api/health", "/api/setup", "/api/setup/status"}
                if request.url.path not in public_paths:
                    return JSONResponse({"detail": "Setup required"}, status_code=403)
            else:
                # HTML pages redirect to /welcome
                if request.url.path not in {"/welcome", "/login", "/setup", "/ca.crt"}:
                    return RedirectResponse("/welcome", status_code=303)
        return None

    @app.get("/login")
    async def login_page(request: Request):
        setup_status = get_setup_status()
        if setup_status["setup_required"]:
            return RedirectResponse("/welcome", status_code=303)
        return FileResponse(os.path.join(cfg.static_dir, "login.html"))

    @app.get("/welcome")
    async def welcome_page(request: Request):
        if not is_setup_mode():
            return RedirectResponse("/", status_code=303)
        return FileResponse(os.path.join(cfg.static_dir, "welcome.html"))

    @app.post("/api/login")
    async def login(request: Request):
        setup_check = check_setup_mode(request)
        if setup_check:
            return setup_check

        client_ip = request.client.host if request.client else "unknown"
        if not limiter.allowed(client_ip):
            return JSONResponse({"detail": "Too many attempts, try again later"}, status_code=429)
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"detail": "Invalid request"}, status_code=400)
        pin = data.get("pin", "")
        if not isinstance(pin, str) or len(pin) > 32:
            return JSONResponse({"detail": "Invalid request"}, status_code=400)

        # Check PIN: DB hash first, then env PIN
        auth_rec = get_cached_auth()
        pin_ok = False
        if auth_rec:
            pin_ok = verify_pin_hash(pin, auth_rec)
        elif cfg.pin:
            pin_ok = check_pin(cfg.pin, pin)

        if pin_ok:
            limiter.success(client_ip)
            token = make_token(get_token_secret())
            response = JSONResponse({"ok": True})
            secure = request.url.scheme == "https"
            response.set_cookie(
                "seccam_session",
                token,
                max_age=30 * 86400,
                httponly=True,
                secure=secure,
                samesite="lax",
                path="/",
            )
            return response
        else:
            limiter.failure(client_ip)
            return JSONResponse({"detail": "Invalid PIN"}, status_code=401)

    @app.get("/setup")
    async def setup_page(request: Request):
        return FileResponse(os.path.join(cfg.static_dir, "setup.html"))

    @app.get("/ca.crt")
    async def ca_cert(request: Request):
        if cfg.ca_cert and os.path.exists(cfg.ca_cert):
            return FileResponse(cfg.ca_cert, media_type="application/x-x509-ca-cert")
        return Response(status_code=404)

    @app.get("/api/health")
    async def health():
        return {"ok": True, "model_available": detector.available, "version": "1.1.0", "setup_required": is_setup_mode()}

    @app.get("/api/setup/status")
    async def setup_status():
        return get_setup_status()

    @app.post("/api/setup")
    async def setup(request: Request):
        if not is_setup_mode():
            return JSONResponse({"detail": "Already set up"}, status_code=409)

        client_ip = request.client.host if request.client else "unknown"
        if not limiter.allowed(client_ip):
            return JSONResponse({"detail": "Too many attempts, try again later"}, status_code=429)

        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"detail": "Invalid request"}, status_code=400)

        pin = data.get("pin", "")
        pin_confirm = data.get("pin_confirm", "")

        # Validate PIN
        if not isinstance(pin, str) or not PIN_RE.match(pin):
            return JSONResponse({"detail": "PIN must be 4-12 digits"}, status_code=422)
        if pin != pin_confirm:
            return JSONResponse({"detail": "PINs do not match"}, status_code=422)

        # Validate optional settings
        patch = {}
        if "retention_days" in data:
            try:
                v = int(data["retention_days"])
                if v < 1 or v > 365:
                    return JSONResponse({"detail": "retention_days must be between 1 and 365"}, status_code=422)
                patch["retention_days"] = v
            except (ValueError, TypeError):
                return JSONResponse({"detail": "Invalid retention_days"}, status_code=422)

        if "mode" in data:
            v = data["mode"]
            if v not in ("auto", "local", "server"):
                return JSONResponse({"detail": "mode must be one of: auto, local, server"}, status_code=422)
            patch["mode"] = v

        if "labels" in data:
            v = data["labels"]
            if not isinstance(v, list):
                return JSONResponse({"detail": "labels must be a list"}, status_code=422)
            for item in v:
                if not isinstance(item, str):
                    return JSONResponse({"detail": "labels must be a list of strings"}, status_code=422)
            unknown = set(v) - set(ALL_LABELS)
            if unknown:
                return JSONResponse({"detail": f"unknown labels: {', '.join(sorted(unknown))}"}, status_code=422)
            # Dedup, keep ALL_LABELS order
            v = [lbl for lbl in ALL_LABELS if lbl in v]
            patch["labels"] = v

        # Store PIN hash
        auth_rec = hash_pin(pin)
        db.set_auth(auth_rec)
        invalidate_auth_cache()

        # Apply settings if provided
        if patch:
            try:
                db.update_settings(patch)
            except ValueError as e:
                return JSONResponse({"detail": str(e)}, status_code=422)

        # Log the user in (set cookie)
        limiter.success(client_ip)
        token = make_token(get_token_secret())
        response = JSONResponse({"ok": True})
        secure = request.url.scheme == "https"
        response.set_cookie(
            "seccam_session",
            token,
            max_age=30 * 86400,
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/api/pin")
    async def change_pin(request: Request):
        setup_check = check_setup_mode(request)
        if setup_check:
            return setup_check

        auth = auth_dependency(request)
        if auth:
            return auth

        client_ip = request.client.host if request.client else "unknown"
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"detail": "Invalid request"}, status_code=400)

        current_pin = data.get("current_pin", "")
        new_pin = data.get("new_pin", "")
        new_pin_confirm = data.get("new_pin_confirm", "")

        # Validate current PIN
        auth_rec = get_cached_auth()
        current_ok = False
        if auth_rec:
            current_ok = verify_pin_hash(current_pin, auth_rec)
        elif cfg.pin:
            current_ok = check_pin(cfg.pin, current_pin)

        if not current_ok:
            limiter.failure(client_ip)
            return JSONResponse({"detail": "Current PIN is incorrect"}, status_code=401)

        # Validate new PIN
        if not isinstance(new_pin, str) or not PIN_RE.match(new_pin):
            return JSONResponse({"detail": "New PIN must be 4-12 digits"}, status_code=422)
        if new_pin != new_pin_confirm:
            return JSONResponse({"detail": "New PINs do not match"}, status_code=422)

        # Store new PIN hash
        new_auth_rec = hash_pin(new_pin)
        db.set_auth(new_auth_rec)
        invalidate_auth_cache()

        # Issue fresh cookie for this browser (old sessions everywhere else become invalid)
        limiter.success(client_ip)
        token = make_token(get_token_secret())
        response = JSONResponse({"ok": True})
        secure = request.url.scheme == "https"
        response.set_cookie(
            "seccam_session",
            token,
            max_age=30 * 86400,
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/",
        )
        return response

    def auth_dependency(request: Request):
        if not require_auth(request):
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            else:
                return auth_redirect(request)
        return None

    @app.get("/")
    async def index(request: Request):
        setup_check = check_setup_mode(request)
        if setup_check:
            return setup_check
        auth = auth_dependency(request)
        if auth:
            return auth
        return FileResponse(os.path.join(cfg.static_dir, "index.html"))

    @app.get("/camera")
    async def camera_page(request: Request):
        setup_check = check_setup_mode(request)
        if setup_check:
            return setup_check
        auth = auth_dependency(request)
        if auth:
            return auth
        return FileResponse(os.path.join(cfg.static_dir, "camera.html"))

    @app.post("/api/logout")
    async def logout(request: Request):
        response = JSONResponse({"ok": True})
        response.delete_cookie("seccam_session", path="/")
        return response

    @app.get("/api/events")
    async def list_events(request: Request):
        setup_check = check_setup_mode(request)
        if setup_check:
            return setup_check
        auth = auth_dependency(request)
        if auth:
            return auth
        try:
            limit = min(int(request.query_params.get("limit", 50)), 200)
        except ValueError:
            return JSONResponse({"detail": "Invalid limit"}, status_code=422)
        before = request.query_params.get("before")
        label = request.query_params.get("label")
        camera_id = request.query_params.get("camera_id")
        before_id = None
        if before:
            try:
                before_id = int(before)
            except ValueError:
                return JSONResponse({"detail": "Invalid before"}, status_code=422)
        events = db.list_events(
            limit=limit,
            before_id=before_id,
            label=label,
            camera_id=camera_id,
        )
        return {"events": events}

    @app.get("/api/events/{event_id}")
    async def get_event(event_id: int, request: Request):
        setup_check = check_setup_mode(request)
        if setup_check:
            return setup_check
        auth = auth_dependency(request)
        if auth:
            return auth
        event = db.get_event(event_id)
        if not event:
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return event

    @app.delete("/api/events/{event_id}")
    async def delete_event(event_id: int, request: Request):
        setup_check = check_setup_mode(request)
        if setup_check:
            return setup_check
        auth = auth_dependency(request)
        if auth:
            return auth
        ok = db.delete_event(event_id)
        if not ok:
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return {"ok": True}

    @app.get("/api/events/{event_id}/snapshot.jpg")
    async def event_snapshot(event_id: int, request: Request):
        setup_check = check_setup_mode(request)
        if setup_check:
            return setup_check
        auth = auth_dependency(request)
        if auth:
            return auth
        path = db.event_file(event_id, "snapshot")
        if not path or not path.exists():
            return Response(status_code=404)
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/events/{event_id}/thumb.jpg")
    async def event_thumb(event_id: int, request: Request):
        setup_check = check_setup_mode(request)
        if setup_check:
            return setup_check
        auth = auth_dependency(request)
        if auth:
            return auth
        path = db.event_file(event_id, "thumb")
        if not path or not path.exists():
            return Response(status_code=404)
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/settings")
    async def get_settings(request: Request):
        setup_check = check_setup_mode(request)
        if setup_check:
            return setup_check
        auth = auth_dependency(request)
        if auth:
            return auth
        return db.get_settings()

    @app.put("/api/settings")
    async def update_settings(request: Request):
        setup_check = check_setup_mode(request)
        if setup_check:
            return setup_check
        auth = auth_dependency(request)
        if auth:
            return auth
        try:
            patch = await request.json()
        except Exception:
            return JSONResponse({"detail": "Invalid request"}, status_code=422)
        if not isinstance(patch, dict):
            return JSONResponse({"detail": "Invalid request"}, status_code=422)
        try:
            new_settings = db.update_settings(patch)
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=422)
        await hub.settings_changed(new_settings)
        return new_settings

    @app.get("/api/cameras")
    async def get_cameras(request: Request):
        setup_check = check_setup_mode(request)
        if setup_check:
            return setup_check
        auth = auth_dependency(request)
        if auth:
            return auth
        return {"cameras": hub.cameras()}

    @app.get("/api/stats")
    async def get_stats(request: Request):
        setup_check = check_setup_mode(request)
        if setup_check:
            return setup_check
        auth = auth_dependency(request)
        if auth:
            return auth
        return db.stats()

    @app.websocket("/ws/camera")
    async def ws_camera(websocket: WebSocket):
        # Check setup mode first
        if is_setup_mode():
            await websocket.accept()
            await websocket.close(code=4403)
            return

        token = websocket.cookies.get("seccam_session")
        if not verify_token(get_token_secret(), token):
            await websocket.accept()
            await websocket.close(code=4401)
            return
        await websocket.accept()
        camera_id = None
        conn_token = id(websocket)
        try:
            try:
                hello_msg = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
            except asyncio.TimeoutError:
                await websocket.close(code=4400)
                return
            try:
                hello = json.loads(hello_msg)
            except json.JSONDecodeError:
                await websocket.close(code=4400)
                return
            if hello.get("type") != "hello":
                await websocket.close(code=4400)
                return
            camera_id = hello.get("camera_id", "")
            name = hello.get("name", "").strip()[:64]
            if not camera_id or len(camera_id) > 64 or not all(c.isalnum() or c in "-_" for c in camera_id):
                await websocket.close(code=4400)
                return

            async def send_json(msg: dict):
                await websocket.send_text(json.dumps(msg))

            await hub.camera_connected(camera_id, name, send_json, conn_token=conn_token)

            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    data = msg["bytes"]
                    if len(data) > 2 * 1024 * 1024:
                        continue
                    await hub.camera_frame(camera_id, data)
                elif msg.get("text") is not None:
                    try:
                        data = json.loads(msg["text"])
                    except json.JSONDecodeError:
                        continue
                    msg_type = data.get("type")
                    if msg_type == "mode":
                        mode = data.get("mode")
                        if mode in ("local", "server"):
                            await hub.camera_mode(camera_id, mode)
                    elif msg_type == "detections":
                        detections = data.get("detections", [])
                        if not isinstance(detections, list):
                            detections = []
                        sanitized = []
                        for d in detections[:50]:
                            if not isinstance(d, dict):
                                continue
                            label = d.get("label")
                            score = d.get("score")
                            bbox = d.get("bbox")
                            if not isinstance(label, str) or len(label) > 32:
                                continue
                            if not isinstance(score, (int, float)) or not (0 <= score <= 1):
                                continue
                            if not isinstance(bbox, list) or len(bbox) != 4:
                                continue
                            if not all(isinstance(v, (int, float)) and v == v and v != float('inf') and v != float('-inf') for v in bbox):
                                continue
                            sanitized.append({"label": label, "score": float(score), "bbox": [float(v) for v in bbox]})
                        frame_w = data.get("frame_w", 0)
                        frame_h = data.get("frame_h", 0)
                        infer_ms = data.get("infer_ms")
                        if not isinstance(frame_w, int) or not (0 <= frame_w <= 10000):
                            frame_w = 0
                        if not isinstance(frame_h, int) or not (0 <= frame_h <= 10000):
                            frame_h = 0
                        if infer_ms is not None and not isinstance(infer_ms, (int, float)):
                            infer_ms = None
                        await hub.camera_detections(camera_id, sanitized, frame_w, frame_h, infer_ms)
                    elif msg_type == "ping":
                        await websocket.send_text(json.dumps({"type": "pong"}))
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("Camera websocket error")
        finally:
            if camera_id:
                await hub.camera_disconnected(camera_id, conn_token=conn_token)

    @app.websocket("/ws/viewer")
    async def ws_viewer(websocket: WebSocket):
        # Check setup mode first
        if is_setup_mode():
            await websocket.accept()
            await websocket.close(code=4403)
            return

        token = websocket.cookies.get("seccam_session")
        if not verify_token(get_token_secret(), token):
            await websocket.accept()
            await websocket.close(code=4401)
            return
        await websocket.accept()
        viewer_id = uuid.uuid4().hex
        async def send_json(msg: dict):
            await websocket.send_text(json.dumps(msg))
        await hub.viewer_connected(viewer_id, send_json)
        try:
            while True:
                msg = await websocket.receive_text()
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("Viewer websocket error")
        finally:
            await hub.viewer_disconnected(viewer_id)

    return app
