import asyncio
import base64
import json
import logging
import math
import struct
import time
from collections import deque
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Tuple

from server.db import DB
from server.detector import Detector

logger = logging.getLogger("seccam")

SendJson = Callable[[dict], Awaitable[None]]
SendBytes = Callable[[bytes], Awaitable[None]]
Close = Callable[[int], Awaitable[None]]

MAX_JPEG = 2 * 1024 * 1024
SEND_TIMEOUT = 5.0
VIEWER_CONTROL_MAX = 256
CAMERA_OUTBOX_MAX = 64
MAX_DIM = 4096  # per side; larger frames are decompression-bomb candidates
EVENT_MIN_GAP_MS = 1000  # hard per-camera floor regardless of cooldown_s
EVENT_GLOBAL_MAX = 120  # events per minute across all cameras
_MAGIC = b"SC\x02\x01"
_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def jpeg_dimensions(jpeg: bytes) -> Optional[Tuple[int, int]]:
    """(width, height) from the SOF marker without decoding; None if absent or malformed."""
    n = len(jpeg)
    i = 2
    dims: Optional[Tuple[int, int]] = None
    while i + 4 <= n:
        if jpeg[i] != 0xFF:
            return None
        m = jpeg[i + 1]
        if m == 0xFF:
            i += 1
            continue
        if m in _SOF:
            if dims is not None or i + 9 > n:
                return None  # a second SOF is ambiguous (parser differential)
            dims = (jpeg[i + 7] << 8) | jpeg[i + 8], (jpeg[i + 5] << 8) | jpeg[i + 6]
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        if m in (0xD9, 0xDA):
            return dims
        i += 2 + ((jpeg[i + 2] << 8) | jpeg[i + 3])
    return None


def encode_viewer_frame(header: dict, jpeg: bytes) -> bytes:
    h = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return _MAGIC + struct.pack(">I", len(h)) + h + bytes(jpeg)


def decode_viewer_frame(data: bytes) -> Tuple[dict, bytes]:
    data = bytes(data)
    if len(data) < 8:
        raise ValueError("frame too short")
    if data[:2] != b"SC":
        raise ValueError("bad magic")
    if data[2] != 2:
        raise ValueError("bad version")
    if data[3] != 1:
        raise ValueError("bad type")
    (hlen,) = struct.unpack(">I", data[4:8])
    if 8 + hlen > len(data):
        raise ValueError("bad header length")
    try:
        header = json.loads(data[8:8 + hlen].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError("bad header") from e
    if not isinstance(header, dict):
        raise ValueError("header is not an object")
    return header, data[8 + hlen:]


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def sanitize_detections(msg: Any) -> Tuple[List[dict], int, int, Optional[float]]:
    if not isinstance(msg, dict):
        return [], 0, 0, None
    raw = msg.get("detections")
    out: List[dict] = []
    if isinstance(raw, list):
        for d in raw[:50]:
            if not isinstance(d, dict):
                continue
            label, score, bbox = d.get("label"), d.get("score"), d.get("bbox")
            if not isinstance(label, str) or len(label) > 32:
                continue
            if not _is_num(score) or not 0 <= score <= 1:
                continue
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or not all(_is_num(v) for v in bbox):
                continue
            out.append({"label": label, "score": float(score), "bbox": [float(v) for v in bbox]})

    def dim(v: Any) -> int:
        return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10000 else 0

    infer_ms = msg.get("infer_ms")
    infer_ms = float(infer_ms) if _is_num(infer_ms) else None
    return out, dim(msg.get("frame_w")), dim(msg.get("frame_h")), infer_ms


class _Viewer:
    def __init__(self, viewer_id: str, send_json: SendJson, send_bytes: Optional[SendBytes],
                 close: Optional[Close], session_id: Optional[str]) -> None:
        self.id = viewer_id
        self.send_json = send_json
        self.send_bytes = send_bytes
        self.close = close
        self.session_id = session_id
        self.control: Deque[dict] = deque()
        self.frames: Dict[str, Tuple[dict, bytes]] = {}
        self.wake = asyncio.Event()
        self.busy = False
        self.task: Optional[asyncio.Task] = None

    def idle(self) -> bool:
        return not self.control and not self.frames and not self.busy


class Hub:
    def __init__(self, db: DB, detector: Detector) -> None:
        self.db = db
        self.detector = detector
        self._cameras: Dict[str, Dict[str, Any]] = {}
        self._viewers: Dict[str, _Viewer] = {}
        self._settings = db.get_settings()
        self._detector_task: Optional[asyncio.Task] = None
        self._detector_wake = asyncio.Event()
        self._timer_task: Optional[asyncio.Task] = None
        self._bg: set = set()
        self._event_times: Deque[float] = deque()
        self._shutdown = False

    # --- public: state ---

    def cameras(self) -> List[dict]:
        return [{
            "camera_id": cid,
            "name": c["name"],
            "mode": c["mode"],
            "kind": c["kind"],
            "device_id": c["device_id"],
            "connected": True,
            "last_seen": c["last_seen"],
            "fps": c["fps"],
            "infer_ms": c["infer_ms"],
        } for cid, c in self._cameras.items()]

    # --- public: cameras ---

    async def camera_connected(self, camera_id: str, name: str, send_json: SendJson, conn_token: Any = None,
                               device_id: Optional[str] = None, close: Optional[Close] = None, kind: str = "browser",
                               session_id: Optional[str] = None) -> None:
        old = self._cameras.pop(camera_id, None)
        if old is not None:
            self._stop_camera_task(old)
            await self._safe_close(old.get("close"), 4409, f"camera {camera_id}")
        cam = {
            "name": name, "mode": "starting", "send_json": send_json, "close": close,
            "kind": kind if kind in ("browser", "agent") else "browser",
            "device_id": device_id, "session_id": session_id, "conn_token": conn_token,
            "last_seen": int(time.time() * 1000), "fps": 0.0, "infer_ms": 0.0,
            "last_frame_time": 0.0, "ema_interval": None, "last_event_ts": 0,
            "last_preview": 0, "last_dets": None, "jpeg": b"", "frame_seq": 0,
            "inferred_seq": 0, "last_inference": 0.0, "detector_error_sent": False,
            "out": deque(), "out_wake": asyncio.Event(), "out_task": None,
        }
        self._cameras[camera_id] = cam
        try:
            await asyncio.wait_for(send_json({"type": "config", "camera_id": camera_id, "settings": self._settings}),
                                   SEND_TIMEOUT)
        except Exception:
            logger.warning("config send to camera %s failed", camera_id, exc_info=True)
        cam["out_task"] = asyncio.create_task(self._camera_sender(camera_id, cam))
        self._broadcast_cameras()

    async def camera_disconnected(self, camera_id: str, conn_token: Any = None) -> None:
        cam = self._cameras.get(camera_id)
        if cam is None or cam["conn_token"] != conn_token:
            return
        del self._cameras[camera_id]
        self._stop_camera_task(cam)
        self._broadcast_cameras()

    async def camera_mode(self, camera_id: str, mode: str) -> None:
        cam = self._cameras.get(camera_id)
        if cam is None or mode not in ("local", "server"):
            return
        cam["mode"] = mode
        self._broadcast_cameras()
        self._detector_wake.set()
        self._ensure_detector()

    async def camera_frame(self, camera_id: str, jpeg: bytes) -> None:
        cam = self._cameras.get(camera_id)
        if cam is None or len(jpeg) > MAX_JPEG or jpeg[:2] != b"\xff\xd8":
            return
        dims = jpeg_dimensions(jpeg)
        if dims is None or not (0 < dims[0] <= MAX_DIM and 0 < dims[1] <= MAX_DIM):
            logger.warning("camera %s: dropped frame with bad/oversized dimensions %s", camera_id, dims)
            return
        now = time.time()
        now_ms = int(now * 1000)
        cam["last_seen"] = now_ms
        if cam["last_frame_time"] > 0:
            interval = now - cam["last_frame_time"]
            if interval <= 10.0:
                ema = cam["ema_interval"]
                ema = interval if ema is None else 0.8 * ema + 0.2 * interval
                cam["ema_interval"] = ema
                cam["fps"] = round(1.0 / ema, 2) if ema > 0 else 0.0
        cam["last_frame_time"] = now
        cam["jpeg"] = jpeg
        cam["frame_seq"] += 1
        self._detector_wake.set()
        self._ensure_detector()

        fps = self._settings.get("preview_fps", 2)
        min_interval_ms = 1000.0 / fps if fps > 0 else 0
        if not self._viewers or now_ms - cam["last_preview"] < min_interval_ms - 50:
            return
        cam["last_preview"] = now_ms
        dets, fw, fh = [], 0, 0
        ld = cam["last_dets"]
        if ld and now_ms - ld["ts"] < 2000:
            dets, fw, fh = ld["detections"], ld["frame_w"], ld["frame_h"]
        header = {"camera_id": camera_id, "ts": now_ms, "detections": dets, "frame_w": fw, "frame_h": fh}
        for v in list(self._viewers.values()):
            v.frames[camera_id] = (header, jpeg)
            v.wake.set()

    async def camera_detections(self, camera_id: str, detections: List[dict], frame_w: int, frame_h: int,
                                infer_ms: Optional[float]) -> None:
        cam = self._cameras.get(camera_id)
        if cam is None:
            return
        cam["last_seen"] = int(time.time() * 1000)
        if infer_ms is not None:
            cam["infer_ms"] = infer_ms
        cam["last_dets"] = {"detections": detections, "frame_w": frame_w, "frame_h": frame_h,
                            "ts": int(time.time() * 1000)}
        await self.handle_detections(camera_id, detections, frame_w, frame_h, infer_ms)

    async def handle_detections(self, camera_id: str, detections: List[dict], frame_w: int, frame_h: int,
                                infer_ms: Optional[float]) -> None:
        cam = self._cameras.get(camera_id)
        if cam is None:
            return
        labels = set(self._settings.get("labels", []))
        min_score = self._settings.get("min_score", 0.55)
        kept = [d for d in detections
                if d.get("label") in labels and isinstance(d.get("score"), (int, float)) and d["score"] >= min_score]
        if not kept:
            return
        kept.sort(key=lambda d: d["score"], reverse=True)
        now_ms = int(time.time() * 1000)
        gap_ms = max(self._settings.get("cooldown_s", 30) * 1000, EVENT_MIN_GAP_MS)
        if now_ms - cam["last_event_ts"] < gap_ms:
            return
        times = self._event_times
        while times and now_ms - times[0] > 60000:
            times.popleft()
        if len(times) >= EVENT_GLOBAL_MAX:
            logger.warning("global event cap reached; dropping event from camera %s", camera_id)
            return
        times.append(now_ms)
        prev_ts = cam["last_event_ts"]
        cam["last_event_ts"] = now_ms
        jpeg = cam["jpeg"]
        try:
            event = await asyncio.to_thread(self.db.add_event, now_ms, camera_id, cam["name"], kept,
                                            frame_w, frame_h, jpeg or None)
        except Exception:
            logger.exception("add_event failed for camera %s", camera_id)
            cam["last_event_ts"] = prev_ts
            try:
                times.remove(now_ms)
            except ValueError:
                pass
            return
        self._broadcast({"type": "event", "event": event})
        self._broadcast_cameras()

    # --- public: viewers ---

    async def viewer_connected(self, viewer_id: str, send_json: SendJson, send_bytes: Optional[SendBytes] = None,
                               close: Optional[Close] = None, session_id: Optional[str] = None) -> None:
        old = self._viewers.pop(viewer_id, None)
        if old is not None:
            self._stop_viewer_task(old)
        v = _Viewer(viewer_id, send_json, send_bytes, close, session_id)
        self._viewers[viewer_id] = v
        v.control.append({"type": "cameras", "cameras": self.cameras()})
        v.control.append({"type": "settings", "settings": self._settings})
        v.task = asyncio.create_task(self._viewer_sender(v))
        v.wake.set()
        if self._timer_task is None or self._timer_task.done():
            self._timer_task = asyncio.create_task(self._cameras_timer())

    async def viewer_disconnected(self, viewer_id: str) -> None:
        v = self._viewers.pop(viewer_id, None)
        if v is not None:
            await self._cancel(v.task)
        if not self._viewers and self._timer_task is not None:
            t, self._timer_task = self._timer_task, None
            await self._cancel(t)

    async def settings_changed(self, settings: dict) -> None:
        self._settings = settings
        for cid, cam in self._cameras.items():
            self._camera_enqueue(cam, {"type": "config", "camera_id": cid, "settings": settings})
        self._broadcast({"type": "settings", "settings": settings})

    async def disconnect_device(self, device_id: str) -> int:
        victims = [(cid, c) for cid, c in self._cameras.items() if c["device_id"] == device_id]
        return await self._kick_cameras(victims)

    async def disconnect_session(self, session_id: str) -> int:
        victims = [(cid, c) for cid, c in self._cameras.items() if c["session_id"] == session_id]
        count = await self._kick_cameras(victims)
        for v in [v for v in self._viewers.values() if v.session_id == session_id]:
            self._viewers.pop(v.id, None)
            self._stop_viewer_task(v)
            await self._safe_close(v.close, 4401, f"viewer {v.id}")
            count += 1
        return count

    async def flush(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(v.idle() for v in self._viewers.values()) and \
               all(not c["out"] for c in self._cameras.values()):
                return
            await asyncio.sleep(0.005)

    async def shutdown(self) -> None:
        self._shutdown = True
        for t in (self._detector_task, self._timer_task):
            await self._cancel(t)
        self._detector_task = self._timer_task = None
        for v in list(self._viewers.values()):
            self._stop_viewer_task(v)
            await self._safe_close(v.close, 1001, f"viewer {v.id}")
        self._viewers.clear()
        for cid, c in list(self._cameras.items()):
            self._stop_camera_task(c)
            await self._safe_close(c["close"], 1001, f"camera {cid}")
        self._cameras.clear()
        for t in list(self._bg):
            t.cancel()

    # --- internals ---

    @staticmethod
    async def _cancel(task: Optional[asyncio.Task]) -> None:
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    @staticmethod
    async def _safe_close(close: Optional[Close], code: int, what: str) -> None:
        if close is None:
            return
        try:
            await asyncio.wait_for(close(code), SEND_TIMEOUT)
        except Exception:
            logger.warning("close(%s) failed for %s", code, what, exc_info=True)

    def _spawn(self, coro: Awaitable[None]) -> None:
        t = asyncio.ensure_future(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    @staticmethod
    def _stop_viewer_task(v: _Viewer) -> None:
        if v.task is not None and v.task is not asyncio.current_task():
            v.task.cancel()

    @staticmethod
    def _stop_camera_task(cam: dict) -> None:
        t = cam.get("out_task")
        if t is not None and t is not asyncio.current_task():
            t.cancel()

    async def _kick_cameras(self, victims: List[Tuple[str, dict]]) -> int:
        for cid, cam in victims:
            if self._cameras.get(cid) is cam:
                del self._cameras[cid]
            self._stop_camera_task(cam)
            await self._safe_close(cam["close"], 4401, f"camera {cid}")
        if victims:
            self._broadcast_cameras()
        return len(victims)

    def _broadcast(self, msg: dict) -> None:
        for v in list(self._viewers.values()):
            if len(v.control) >= VIEWER_CONTROL_MAX:
                logger.warning("viewer %s control queue overflow, dropping", v.id)
                self._drop_viewer(v)
                continue
            v.control.append(msg)
            v.wake.set()

    def _broadcast_cameras(self) -> None:
        if self._viewers:
            self._broadcast({"type": "cameras", "cameras": self.cameras()})

    def _drop_viewer(self, v: _Viewer) -> None:
        if self._viewers.get(v.id) is v:
            del self._viewers[v.id]
        self._stop_viewer_task(v)
        self._spawn(self._safe_close(v.close, 1011, f"viewer {v.id}"))

    async def _viewer_sender(self, v: _Viewer) -> None:
        try:
            while True:
                await v.wake.wait()
                v.wake.clear()
                while True:
                    v.busy = True
                    try:
                        if v.control:
                            await asyncio.wait_for(v.send_json(v.control.popleft()), SEND_TIMEOUT)
                        elif v.frames:
                            cid = next(iter(v.frames))
                            header, jpeg = v.frames.pop(cid)
                            if v.send_bytes is not None:
                                await asyncio.wait_for(v.send_bytes(encode_viewer_frame(header, jpeg)), SEND_TIMEOUT)
                            else:
                                msg = dict(header, type="frame", jpeg=base64.b64encode(jpeg).decode("ascii"))
                                await asyncio.wait_for(v.send_json(msg), SEND_TIMEOUT)
                        else:
                            break
                    finally:
                        v.busy = False
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("send to viewer %s failed; dropping", v.id, exc_info=True)
            self._drop_viewer(v)

    def _camera_enqueue(self, cam: dict, msg: dict) -> None:
        if len(cam["out"]) >= CAMERA_OUTBOX_MAX:
            cam["out"].popleft()
        cam["out"].append(msg)
        cam["out_wake"].set()

    async def _camera_sender(self, camera_id: str, cam: dict) -> None:
        try:
            while True:
                await cam["out_wake"].wait()
                cam["out_wake"].clear()
                while cam["out"]:
                    await asyncio.wait_for(cam["send_json"](cam["out"].popleft()), SEND_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("send to camera %s failed; dropping", camera_id, exc_info=True)
            if self._cameras.get(camera_id) is cam:
                del self._cameras[camera_id]
                self._broadcast_cameras()
            self._spawn(self._safe_close(cam["close"], 1011, f"camera {camera_id}"))

    async def _cameras_timer(self) -> None:
        try:
            while self._viewers and not self._shutdown:
                await asyncio.sleep(5.0)
                self._broadcast_cameras()
        except asyncio.CancelledError:
            pass

    def _ensure_detector(self) -> None:
        if self._shutdown:
            return
        if (self._detector_task is None or self._detector_task.done()) and \
           any(c["mode"] == "server" for c in self._cameras.values()):
            self._detector_task = asyncio.create_task(self._detector_loop())

    def _pick_candidate(self, now: float) -> Optional[str]:
        fps = self._settings.get("detect_fps", 2)
        min_interval = 1.0 / fps if fps > 0 else 0
        best, best_t = None, 0.0
        for cid, c in self._cameras.items():
            if c["mode"] != "server" or c["frame_seq"] <= c["inferred_seq"]:
                continue
            if now - c["last_inference"] < min_interval:
                continue
            if best is None or c["last_inference"] < best_t:
                best, best_t = cid, c["last_inference"]
        return best

    async def _detector_loop(self) -> None:
        try:
            while not self._shutdown:
                cid = self._pick_candidate(time.time())
                if cid is None:
                    if not any(c["mode"] == "server" for c in self._cameras.values()):
                        return
                    self._detector_wake.clear()
                    try:
                        await asyncio.wait_for(self._detector_wake.wait(), 0.05)
                    except asyncio.TimeoutError:
                        pass
                    continue
                cam = self._cameras[cid]
                jpeg, seq = cam["jpeg"], cam["frame_seq"]
                cam["inferred_seq"] = seq
                cam["last_inference"] = time.time()
                try:
                    dets, fw, fh, infer_ms = await asyncio.to_thread(self.detector.detect, jpeg)
                except Exception:
                    logger.exception("Detector error for camera %s", cid)
                    if self._cameras.get(cid) is cam and not cam["detector_error_sent"]:
                        cam["detector_error_sent"] = True
                        self._camera_enqueue(cam, {"type": "error", "message": "Server detector unavailable"})
                    continue
                if self._cameras.get(cid) is not cam:
                    continue
                self._camera_enqueue(cam, {"type": "detections", "detections": dets, "frame_w": fw,
                                           "frame_h": fh, "infer_ms": infer_ms})
                cam["infer_ms"] = infer_ms
                cam["last_dets"] = {"detections": dets, "frame_w": fw, "frame_h": fh, "ts": int(time.time() * 1000)}
                await self.handle_detections(cid, dets, fw, fh, infer_ms)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Detector loop crashed")
