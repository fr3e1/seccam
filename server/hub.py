import asyncio
import base64
import json
import logging
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Set

from server.db import DB, DEFAULT_SETTINGS, ALL_LABELS
from server.detector import Detector

logger = logging.getLogger("seccam")


class Hub:
    def __init__(self, db: DB, detector: Detector):
        self.db = db
        self.detector = detector
        self._cameras: Dict[str, Dict[str, Any]] = {}
        self._viewers: Dict[str, Callable[[dict], Any]] = {}
        self._settings = db.get_settings()
        self._detector_task: Optional[asyncio.Task] = None
        self._latest_frames: Dict[str, bytes] = {}  # camera_id -> jpeg
        self._frame_timestamps: Dict[str, float] = {}  # camera_id -> time of frame
        self._last_inference_time: Dict[str, float] = {}  # camera_id -> time of last inference
        self._cameras_broadcast_task: Optional[asyncio.Task] = None
        self._shutdown = False

    def cameras(self) -> List[dict]:
        result = []
        for cam_id, cam in self._cameras.items():
            result.append({
                "camera_id": cam_id,
                "name": cam["name"],
                "mode": cam["mode"],
                "connected": True,
                "last_seen": cam["last_seen"],
                "fps": cam.get("fps", 0.0),
                "infer_ms": cam.get("infer_ms", 0.0),
            })
        return result

    async def camera_connected(self, camera_id: str, name: str, send_json: Callable[[dict], Any], conn_token: Optional[Any] = None) -> None:
        self._cameras[camera_id] = {
            "name": name,
            "mode": "starting",
            "send_json": send_json,
            "last_seen": int(time.time() * 1000),
            "fps": 0.0,
            "infer_ms": 0.0,
            "last_frame_time": 0.0,
            "frame_intervals": [],
            "last_event_ts": 0,
            "conn_token": conn_token,
            "detector_error_sent": False,
        }
        self._latest_frames[camera_id] = b""
        self._frame_timestamps[camera_id] = 0.0
        self._last_inference_time[camera_id] = 0.0
        await self._send_to_camera(camera_id, {"type": "config", "settings": self._settings})
        await self._broadcast_cameras()
        # Start detector task if not running and there are server-mode cameras
        self._maybe_start_detector_task()

    async def camera_disconnected(self, camera_id: str, conn_token: Optional[Any] = None) -> None:
        cam = self._cameras.get(camera_id)
        if cam and cam.get("conn_token") == conn_token:
            del self._cameras[camera_id]
        if camera_id in self._latest_frames:
            del self._latest_frames[camera_id]
        if camera_id in self._frame_timestamps:
            del self._frame_timestamps[camera_id]
        if camera_id in self._last_inference_time:
            del self._last_inference_time[camera_id]
        await self._broadcast_cameras()
        # If no more server-mode cameras, stop detector task
        self._maybe_stop_detector_task()

    async def camera_mode(self, camera_id: str, mode: str) -> None:
        if camera_id in self._cameras:
            self._cameras[camera_id]["mode"] = mode
            await self._broadcast_cameras()
            self._maybe_start_detector_task()
            self._maybe_stop_detector_task()

    async def camera_frame(self, camera_id: str, jpeg: bytes) -> None:
        if camera_id not in self._cameras:
            return
        cam = self._cameras[camera_id]
        now = time.time()
        now_ms = int(now * 1000)
        cam["last_seen"] = now_ms

        # Update FPS estimate (EMA of frame intervals, ignore >10s)
        if cam["last_frame_time"] > 0:
            interval = now - cam["last_frame_time"]
            if interval <= 10.0:
                # EMA of the interval (not of 1/interval: back-to-back frames would spike the rate)
                ema = cam.get("ema_interval") or interval
                ema = 0.8 * ema + 0.2 * interval
                cam["ema_interval"] = ema
                cam["fps"] = round(1.0 / ema, 2) if ema > 0 else 0.0
        cam["last_frame_time"] = now

        # Store latest frame for preview broadcast and server-side detection
        self._latest_frames[camera_id] = jpeg
        self._frame_timestamps[camera_id] = now

        # Broadcast preview to viewers (rate-limited to preview_fps)
        await self._maybe_broadcast_preview(camera_id, jpeg, now_ms)

        # If server mode, the detector task will pick up the frame
        # No need to do anything here; the background task handles it.

    async def camera_detections(self, camera_id: str, detections: List[dict], frame_w: int, frame_h: int, infer_ms: Optional[float]) -> None:
        if camera_id not in self._cameras:
            return
        cam = self._cameras[camera_id]
        cam["last_seen"] = int(time.time() * 1000)
        if infer_ms is not None:
            cam["infer_ms"] = infer_ms
        # Store detections for preview broadcast
        cam["last_dets"] = {
            "detections": detections,
            "frame_w": frame_w,
            "frame_h": frame_h,
            "ts": int(time.time() * 1000),
        }
        # Handle detections (event creation) for local mode
        await self._handle_detections(camera_id, detections, frame_w, frame_h, infer_ms)

    async def handle_detections(self, camera_id: str, detections: List[dict], frame_w: int, frame_h: int, infer_ms: Optional[float]) -> None:
        await self._handle_detections(camera_id, detections, frame_w, frame_h, infer_ms)

    async def viewer_connected(self, viewer_id: str, send_json: Callable[[dict], Any]) -> None:
        self._viewers[viewer_id] = send_json
        await self._send_to_viewer(viewer_id, {"type": "cameras", "cameras": self.cameras()})
        await self._send_to_viewer(viewer_id, {"type": "settings", "settings": self._settings})
        # Start periodic cameras broadcast if not already running
        if self._cameras_broadcast_task is None or self._cameras_broadcast_task.done():
            self._cameras_broadcast_task = asyncio.create_task(self._periodic_cameras_broadcast())

    async def viewer_disconnected(self, viewer_id: str) -> None:
        if viewer_id in self._viewers:
            del self._viewers[viewer_id]
        # Stop periodic broadcast if no viewers
        if not self._viewers and self._cameras_broadcast_task and not self._cameras_broadcast_task.done():
            self._cameras_broadcast_task.cancel()
            try:
                await self._cameras_broadcast_task
            except asyncio.CancelledError:
                pass
            self._cameras_broadcast_task = None

    async def settings_changed(self, settings: dict) -> None:
        self._settings = settings
        # Push to all cameras
        await self._broadcast_to_cameras({"type": "config", "settings": settings})
        # Push to all viewers
        await self._broadcast_to_viewers({"type": "settings", "settings": settings})

    async def shutdown(self) -> None:
        self._shutdown = True
        if self._detector_task and not self._detector_task.done():
            self._detector_task.cancel()
            try:
                await self._detector_task
            except asyncio.CancelledError:
                pass
        if self._cameras_broadcast_task and not self._cameras_broadcast_task.done():
            self._cameras_broadcast_task.cancel()
            try:
                await self._cameras_broadcast_task
            except asyncio.CancelledError:
                pass

    # --- Internal methods ---

    async def _send_to_camera(self, camera_id: str, msg: dict) -> None:
        cam = self._cameras.get(camera_id)
        if not cam:
            return
        send_json = cam["send_json"]
        try:
            await asyncio.wait_for(send_json(msg), timeout=2.0)
        except Exception:
            logger.exception("Failed to send to camera %s", camera_id)

    async def _send_to_viewer(self, viewer_id: str, msg: dict) -> None:
        send_json = self._viewers.get(viewer_id)
        if not send_json:
            return
        try:
            await asyncio.wait_for(send_json(msg), timeout=2.0)
        except Exception:
            logger.exception("Failed to send to viewer %s", viewer_id)
            self._viewers.pop(viewer_id, None)

    async def _broadcast_to_cameras(self, msg: dict) -> None:
        if not self._cameras:
            return
        await asyncio.gather(*[self._send_to_camera(cid, msg) for cid in self._cameras], return_exceptions=True)

    async def _broadcast_to_viewers(self, msg: dict) -> None:
        if not self._viewers:
            return
        await asyncio.gather(*[self._send_to_viewer(vid, msg) for vid in list(self._viewers.keys())], return_exceptions=True)

    async def _broadcast_cameras(self) -> None:
        cameras_list = self.cameras()
        await self._broadcast_to_viewers({"type": "cameras", "cameras": cameras_list})

    async def _periodic_cameras_broadcast(self) -> None:
        try:
            while not self._shutdown and self._viewers:
                await asyncio.sleep(5.0)
                if self._shutdown or not self._viewers:
                    break
                await self._broadcast_cameras()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Periodic cameras broadcast error")

    async def _maybe_broadcast_preview(self, camera_id: str, jpeg: bytes, now_ms: int) -> None:
        cam = self._cameras.get(camera_id)
        if not cam:
            return
        preview_fps = self._settings.get("preview_fps", 2)
        min_interval = 1.0 / preview_fps if preview_fps > 0 else 0
        last_broadcast = cam.get("last_preview_broadcast", 0)
        if now_ms - last_broadcast < min_interval * 1000 - 50:
            return
        cam["last_preview_broadcast"] = now_ms

        detections = []
        frame_w = 0
        frame_h = 0
        last_dets = cam.get("last_dets")
        if last_dets and now_ms - last_dets["ts"] < 2000:
            detections = last_dets["detections"]
            frame_w = last_dets["frame_w"]
            frame_h = last_dets["frame_h"]

        b64 = base64.b64encode(jpeg).decode("ascii")
        msg = {
            "type": "frame",
            "camera_id": camera_id,
            "ts": now_ms,
            "jpeg": b64,
            "detections": detections,
            "frame_w": frame_w,
            "frame_h": frame_h,
        }
        await self._broadcast_to_viewers(msg)

    def _maybe_start_detector_task(self) -> None:
        if self._detector_task is None or self._detector_task.done():
            # Check if any camera is in server mode
            if any(cam["mode"] == "server" for cam in self._cameras.values()):
                self._detector_task = asyncio.create_task(self._detector_loop())

    def _maybe_stop_detector_task(self) -> None:
        if self._detector_task and not self._detector_task.done():
            # Check if any camera still in server mode
            if not any(cam["mode"] == "server" for cam in self._cameras.values()):
                self._detector_task.cancel()
                # Don't await here; let it clean up in background

    async def _detector_loop(self) -> None:
        """Background task that processes frames for server-mode cameras."""
        try:
            while not self._shutdown:
                # Find a camera with a new frame that hasn't been processed recently
                now = time.time()
                candidate = None
                for cam_id, cam in self._cameras.items():
                    if cam["mode"] != "server":
                        continue
                    # Check if we have a frame
                    if cam_id not in self._latest_frames or not self._latest_frames[cam_id]:
                        continue
                    # Rate limit per camera by detect_fps
                    detect_fps = self._settings.get("detect_fps", 2)
                    min_interval = 1.0 / detect_fps if detect_fps > 0 else 0
                    last_inf = self._last_inference_time.get(cam_id, 0)
                    if now - last_inf < min_interval:
                        continue
                    # This camera is ready for inference
                    candidate = cam_id
                    break

                if candidate is None:
                    await asyncio.sleep(0.05)
                    continue

                # Run detection for this camera
                jpeg = self._latest_frames[candidate]
                self._last_inference_time[candidate] = now
                try:
                    detections, frame_w, frame_h, infer_ms = await asyncio.to_thread(self.detector.detect, jpeg)
                except Exception:
                    logger.exception("Detector error for camera %s", candidate)
                    # Send error once
                    cam = self._cameras.get(candidate)
                    if cam and not cam.get("detector_error_sent"):
                        await self._send_to_camera(candidate, {"type": "error", "message": "Server detector unavailable"})
                        cam["detector_error_sent"] = True
                    continue

                # Send detections back to camera
                await self._send_to_camera(candidate, {
                    "type": "detections",
                    "detections": detections,
                    "frame_w": frame_w,
                    "frame_h": frame_h,
                    "infer_ms": infer_ms,
                })
                # Update camera infer_ms and last_dets for preview
                if candidate in self._cameras:
                    self._cameras[candidate]["infer_ms"] = infer_ms
                    self._cameras[candidate]["last_dets"] = {
                        "detections": detections,
                        "frame_w": frame_w,
                        "frame_h": frame_h,
                        "ts": int(time.time() * 1000),
                    }
                # Handle detections for event creation
                await self._handle_detections(candidate, detections, frame_w, frame_h, infer_ms)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Detector loop crashed")

    async def _handle_detections(self, camera_id: str, detections: List[dict], frame_w: int, frame_h: int, infer_ms: Optional[float]) -> None:
        cam = self._cameras.get(camera_id)
        if not cam:
            return
        enabled_labels = set(self._settings.get("labels", []))
        min_score = self._settings.get("min_score", 0.55)
        filtered = []
        for d in detections:
            label = d.get("label")
            score = d.get("score")
            if label in enabled_labels and isinstance(score, (int, float)) and score >= min_score:
                filtered.append(d)
        if not filtered:
            return
        # Sort by score descending
        filtered.sort(key=lambda x: x.get("score", 0), reverse=True)

        cooldown_s = self._settings.get("cooldown_s", 30)
        now_ms = int(time.time() * 1000)
        if now_ms - cam["last_event_ts"] < cooldown_s * 1000:
            return
        cam["last_event_ts"] = now_ms

        jpeg = self._latest_frames.get(camera_id, b"")
        event = await asyncio.to_thread(
            self.db.add_event,
            now_ms,
            camera_id,
            cam["name"],
            filtered,
            frame_w,
            frame_h,
            jpeg if jpeg else None,
        )
        await self._broadcast_to_viewers({"type": "event", "event": event})
        await self._broadcast_cameras()
