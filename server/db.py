import io
import json
import math
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image, ImageDraw, ImageFont


ALL_LABELS = [
    "person", "car", "truck", "bus", "motorcycle", "bicycle",
    "cat", "dog", "bird", "horse", "sheep", "cow", "bear",
    "backpack", "handbag", "suitcase", "umbrella",
]

LABEL_COLORS = {
    "person": "#f85149",
    "car": "#2f81f7", "truck": "#2f81f7", "bus": "#2f81f7",
    "motorcycle": "#2f81f7", "bicycle": "#2f81f7",
    "cat": "#3fb950", "dog": "#3fb950", "bird": "#3fb950",
    "horse": "#3fb950", "sheep": "#3fb950", "cow": "#3fb950", "bear": "#3fb950",
    "backpack": "#d29922", "handbag": "#d29922", "suitcase": "#d29922", "umbrella": "#d29922",
}

DEFAULT_SETTINGS = {
    "labels": ["person", "car", "truck", "bus", "motorcycle", "bicycle", "cat", "dog", "bird"],
    "min_score": 0.55,
    "cooldown_s": 30,
    "preview_fps": 2,
    "detect_fps": 2,
    "mode": "auto",
    "retention_days": 14,
    "theme": "tokyo-night",
    "jpeg_quality": 0.7,
    "capture_width": 960,
}

THEME_IDS = [
    "dracula", "nord", "gruvbox-dark", "tokyo-night",
    "catppuccin-mocha", "solarized-dark", "solarized-light", "one-dark",
]

SCHEMA_VERSION = 2
MAX_IMAGE_SIDE = 4096


def _now_ms() -> int:
    return int(time.time() * 1000)


_USER_COLS = ("id, username, password_hash, totp_secret, totp_enabled, totp_last_step, "
              "recovery_codes, created_at, password_changed_at")
_SESSION_COLS = "id, user_id, csrf, created_at, last_seen_at, expires_at, ip, user_agent"
_DEVICE_COLS = "id, name, kind, created_at, last_seen_at, last_ip"


def _user_row(row) -> Dict[str, Any]:
    return {
        "id": row[0], "username": row[1], "password_hash": row[2], "totp_secret": row[3],
        "totp_enabled": bool(row[4]), "totp_last_step": row[5],
        "recovery_codes": json.loads(row[6]), "created_at": row[7], "password_changed_at": row[8],
    }


def _session_row(row) -> Dict[str, Any]:
    keys = ("id", "user_id", "csrf", "created_at", "last_seen_at", "expires_at", "ip", "user_agent")
    return dict(zip(keys, row))


def _device_row(row) -> Dict[str, Any]:
    keys = ("id", "name", "kind", "created_at", "last_seen_at", "last_ip")
    return dict(zip(keys, row))


class DB:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.snapshots_dir = data_dir / "snapshots"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = data_dir / "seccam.db"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    camera_id TEXT NOT NULL,
                    camera_name TEXT NOT NULL,
                    labels TEXT NOT NULL,
                    max_score REAL NOT NULL,
                    detections TEXT NOT NULL,
                    frame_w INTEGER NOT NULL,
                    frame_h INTEGER NOT NULL,
                    snapshot TEXT,
                    thumb TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    totp_secret TEXT,
                    totp_enabled INTEGER NOT NULL DEFAULT 0,
                    totp_last_step INTEGER NOT NULL DEFAULT 0,
                    recovery_codes TEXT NOT NULL DEFAULT '[]',
                    created_at INTEGER NOT NULL,
                    password_changed_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    csrf TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    ip TEXT,
                    user_agent TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('browser','agent')),
                    token_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_seen_at INTEGER,
                    last_ip TEXT
                );
            """)
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version < SCHEMA_VERSION:
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def get_settings(self) -> Dict[str, Any]:
        with self._lock:
            cur = self._conn.execute("SELECT value FROM settings WHERE key = 'settings'")
            row = cur.fetchone()
            if row:
                stored = json.loads(row[0])
                merged = DEFAULT_SETTINGS.copy()
                merged.update(stored)
                return merged
            return DEFAULT_SETTINGS.copy()

    def validate_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Validate a partial settings patch without storing it; returns the full merged settings."""
        patch = dict(patch)
        current = self.get_settings()
        if "min_score" in patch:
            v = patch["min_score"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0.1 or v > 0.95:
                raise ValueError("min_score must be between 0.1 and 0.95")
        if "cooldown_s" in patch:
            v = patch["cooldown_s"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 or v > 3600:
                raise ValueError("cooldown_s must be between 0 and 3600")
        if "preview_fps" in patch:
            v = patch["preview_fps"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0.2 or v > 15:
                raise ValueError("preview_fps must be between 0.2 and 15")
        if "detect_fps" in patch:
            v = patch["detect_fps"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0.2 or v > 10:
                raise ValueError("detect_fps must be between 0.2 and 10")
        if "retention_days" in patch:
            v = patch["retention_days"]
            if isinstance(v, bool) or not isinstance(v, int) or v < 1 or v > 365:
                raise ValueError("retention_days must be between 1 and 365")
        if "mode" in patch:
            v = patch["mode"]
            if v not in ("auto", "local", "server"):
                raise ValueError("mode must be one of: auto, local, server")
        if "theme" in patch and patch["theme"] not in THEME_IDS:
            raise ValueError(f"theme must be one of: {', '.join(THEME_IDS)}")
        if "jpeg_quality" in patch:
            v = patch["jpeg_quality"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0.3 or v > 0.95:
                raise ValueError("jpeg_quality must be between 0.3 and 0.95")
        if "capture_width" in patch:
            v = patch["capture_width"]
            if isinstance(v, bool) or not isinstance(v, int) or v < 320 or v > 1920:
                raise ValueError("capture_width must be between 320 and 1920")
        if "labels" in patch:
            v = patch["labels"]
            if not isinstance(v, list):
                raise ValueError("labels must be a list")
            for item in v:
                if not isinstance(item, str):
                    raise ValueError("labels must be a list of strings")
            unknown = set(v) - set(ALL_LABELS)
            if unknown:
                raise ValueError(f"unknown labels: {', '.join(sorted(unknown))}")
            # Dedup, keep ALL_LABELS order
            v = [lbl for lbl in ALL_LABELS if lbl in v]
            patch["labels"] = v

        # Unknown keys (excluding 'auth' which is handled separately)
        unknown_keys = set(patch.keys()) - set(DEFAULT_SETTINGS.keys())
        if unknown_keys:
            raise ValueError(f"unknown settings keys: {', '.join(sorted(unknown_keys))}")

        new_settings = current.copy()
        new_settings.update(patch)
        return new_settings

    def update_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        new_settings = self.validate_settings(patch)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('settings', ?)",
                (json.dumps(new_settings),),
            )
        return new_settings

    def get_auth(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute("SELECT value FROM settings WHERE key = 'auth'")
            row = cur.fetchone()
            if row:
                return json.loads(row[0])
            return None

    def set_auth(self, rec: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('auth', ?)",
                (json.dumps(rec),),
            )

    def delete_legacy_auth(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM settings WHERE key = 'auth'")

    # ---- users ----
    def count_users(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def create_user(self, username: str, password_hash: str) -> Dict[str, Any]:
        username = username.strip()
        now = _now_ms()
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO users (username, password_hash, created_at, password_changed_at) VALUES (?, ?, ?, ?)",
                    (username, password_hash, now, now),
                )
            except sqlite3.IntegrityError:
                raise ValueError("username taken") from None
            row = self._conn.execute(f"SELECT {_USER_COLS} FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _user_row(row)

    def create_first_user(self, username: str, password_hash: str) -> Optional[Dict[str, Any]]:
        username = username.strip()
        now = _now_ms()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO users (username, password_hash, created_at, password_changed_at) "
                "SELECT ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM users)",
                (username, password_hash, now, now),
            )
            if cur.rowcount != 1:
                return None
            row = self._conn.execute(f"SELECT {_USER_COLS} FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _user_row(row)

    def consume_totp_step(self, user_id: int, step: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET totp_last_step = ? WHERE id = ? AND totp_last_step < ?",
                (step, user_id, step),
            )
            return cur.rowcount == 1

    def replace_recovery_codes(self, user_id: int, expected: List[str], new: List[str]) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET recovery_codes = ? WHERE id = ? AND recovery_codes = ?",
                (json.dumps(new), user_id, json.dumps(expected)),
            )
            return cur.rowcount == 1

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(f"SELECT {_USER_COLS} FROM users WHERE id = ?", (user_id,)).fetchone()
        return _user_row(row) if row else None

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_USER_COLS} FROM users WHERE username = ?", (username.strip(),)
            ).fetchone()
        return _user_row(row) if row else None

    def update_user(self, user_id: int, **fields: Any) -> None:
        allowed = {"password_hash", "totp_secret", "totp_enabled", "totp_last_step",
                   "recovery_codes", "password_changed_at"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown user fields: {', '.join(sorted(bad))}")
        if not fields:
            return
        values = []
        for key, val in fields.items():
            if key == "totp_enabled":
                val = 1 if val else 0
            elif key == "recovery_codes":
                val = json.dumps(list(val))
            values.append(val)
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE users SET {assignments} WHERE id = ?", (*values, user_id))

    # ---- sessions ----
    def create_session(self, sid_hash: str, user_id: int, csrf: str, expires_at: int,
                       ip: Optional[str], user_agent: Optional[str]) -> None:
        now = _now_ms()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (id, user_id, csrf, created_at, last_seen_at, expires_at, ip, user_agent) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid_hash, user_id, csrf, now, now, expires_at, ip, user_agent),
            )

    def get_session(self, sid_hash: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(f"SELECT {_SESSION_COLS} FROM sessions WHERE id = ?", (sid_hash,)).fetchone()
        return _session_row(row) if row else None

    def touch_session(self, sid_hash: str, now: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (now, sid_hash))

    def delete_session(self, sid_hash: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (sid_hash,))

    def delete_user_sessions(self, user_id: int, except_id: Optional[str] = None) -> int:
        with self._lock:
            if except_id is None:
                cur = self._conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            else:
                cur = self._conn.execute(
                    "DELETE FROM sessions WHERE user_id = ? AND id != ?", (user_id, except_id)
                )
            return cur.rowcount

    def list_session_ids(self, user_id: int) -> List[str]:
        with self._lock:
            rows = self._conn.execute("SELECT id FROM sessions WHERE user_id = ?", (user_id,)).fetchall()
        return [r[0] for r in rows]

    def purge_expired_sessions(self, now: int, idle_ms: int = 7 * 86400 * 1000) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM sessions WHERE expires_at <= ? OR last_seen_at <= ?", (now, now - idle_ms)
            )
            return cur.rowcount

    # ---- devices ----
    def create_device(self, device_id: str, name: str, kind: str, token_hash: str) -> Dict[str, Any]:
        with self._lock:
            self._conn.execute(
                "INSERT INTO devices (id, name, kind, token_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (device_id, name.strip(), kind, token_hash, _now_ms()),
            )
            row = self._conn.execute(f"SELECT {_DEVICE_COLS} FROM devices WHERE id = ?", (device_id,)).fetchone()
        return _device_row(row)

    def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_DEVICE_COLS}, token_hash FROM devices WHERE id = ?", (device_id,)
            ).fetchone()
        if not row:
            return None
        dev = _device_row(row[:6])
        dev["token_hash"] = row[6]
        return dev

    def list_devices(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_DEVICE_COLS} FROM devices ORDER BY created_at ASC, rowid ASC"
            ).fetchall()
        return [_device_row(r) for r in rows]

    def rename_device(self, device_id: str, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute("UPDATE devices SET name = ? WHERE id = ?", (name.strip(), device_id))
            if cur.rowcount != 1:
                return None
            row = self._conn.execute(f"SELECT {_DEVICE_COLS} FROM devices WHERE id = ?", (device_id,)).fetchone()
        return _device_row(row)

    def delete_device(self, device_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
            return cur.rowcount == 1

    def touch_device(self, device_id: str, now: int, ip: Optional[str]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE devices SET last_seen_at = ?, last_ip = ? WHERE id = ?", (now, ip, device_id)
            )

    def backup_to(self, dest: Path) -> None:
        target = sqlite3.connect(str(dest))
        try:
            with self._lock:
                self._conn.backup(target)
        finally:
            target.close()

    def add_event(
        self,
        ts: int,
        camera_id: str,
        camera_name: str,
        detections: List[Dict[str, Any]],
        frame_w: int,
        frame_h: int,
        jpeg: Optional[bytes],
    ) -> Dict[str, Any]:
        # Order labels by highest score first, dedup
        label_scores = {}
        for d in detections:
            lbl = d["label"]
            score = d["score"]
            if lbl not in label_scores or score > label_scores[lbl]:
                label_scores[lbl] = score
        labels = sorted(label_scores.keys(), key=lambda l: label_scores[l], reverse=True)
        max_score = max((d["score"] for d in detections), default=0.0)

        snapshot_rel = None
        thumb_rel = None

        # Insert event first to get ID
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO events (ts, camera_id, camera_name, labels, max_score, detections, frame_w, frame_h)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    camera_id,
                    camera_name,
                    json.dumps(labels),
                    max_score,
                    json.dumps(detections),
                    frame_w,
                    frame_h,
                ),
            )
            event_id = cur.lastrowid

        # Process JPEG if provided
        if jpeg:
            try:
                snapshot_rel, thumb_rel = self._process_jpeg(event_id, ts, jpeg, detections, frame_w, frame_h)
            except Exception:
                logging.getLogger('seccam').exception("Failed to process JPEG for event %s", event_id)

        # Update with paths
        with self._lock:
            self._conn.execute(
                "UPDATE events SET snapshot = ?, thumb = ? WHERE id = ?",
                (snapshot_rel, thumb_rel, event_id),
            )

        return {
            "id": event_id,
            "ts": ts,
            "camera_id": camera_id,
            "camera_name": camera_name,
            "labels": labels,
            "max_score": max_score,
            "detections": detections,
            "frame_w": frame_w,
            "frame_h": frame_h,
            "snapshot_url": f"/api/events/{event_id}/snapshot.jpg" if snapshot_rel else None,
            "thumb_url": f"/api/events/{event_id}/thumb.jpg" if thumb_rel else None,
        }

    def _process_jpeg(
        self,
        event_id: int,
        ts: int,
        jpeg: bytes,
        detections: List[Dict[str, Any]],
        frame_w: int,
        frame_h: int,
    ) -> tuple[str, str]:
        # Date directory
        date_str = time.strftime("%Y-%m-%d", time.gmtime(ts / 1000))
        day_dir = self.snapshots_dir / date_str
        day_dir.mkdir(parents=True, exist_ok=True)

        snapshot_path = day_dir / f"{event_id}.jpg"
        thumb_path = day_dir / f"{event_id}_t.jpg"

        # Load image
        img = Image.open(io.BytesIO(jpeg))
        if max(img.size) > MAX_IMAGE_SIDE:
            raise ValueError("image too large")
        img.load()  # force load to catch errors early
        if img.mode != "RGB":
            img = img.convert("RGB")

        img_w, img_h = img.size

        # Draw detections on a copy for snapshot
        snap_img = img.copy()
        draw = ImageDraw.Draw(snap_img)
        font = ImageFont.load_default()

        # Compute scaling factors if frame dimensions differ from image dimensions
        scale_x = 1.0
        scale_y = 1.0
        if frame_w > 0 and frame_h > 0 and (frame_w != img_w or frame_h != img_h):
            scale_x = img_w / frame_w
            scale_y = img_h / frame_h

        for det in detections:
            x, y, w, h = det["bbox"]
            label = det["label"]
            score = det["score"]
            color = LABEL_COLORS.get(label, "#d29922")

            # Scale bbox to image coordinates
            x_s = x * scale_x
            y_s = y * scale_y
            w_s = w * scale_x
            h_s = h * scale_y

            # Rectangle width
            rect_w = max(2, img_w // 200)
            draw.rectangle([x_s, y_s, x_s + w_s, y_s + h_s], outline=color, width=rect_w)

            # Label text
            text = f"{label} {int(score * 100)}%"
            text_bbox = draw.textbbox((x_s, y_s), text, font=font)
            text_w = text_bbox[2] - text_bbox[0]
            text_h = text_bbox[3] - text_bbox[1]

            # Position text box: try above the box, if not enough space, draw inside
            text_y = y_s - text_h - 2
            if text_y < 0:
                text_y = y_s + 2  # inside the box, just below top edge

            # Ensure text box stays within image horizontally
            text_x = x_s
            if text_x + text_w + 4 > img_w:
                text_x = img_w - text_w - 4
            if text_x < 0:
                text_x = 0

            # Draw text background
            draw.rectangle([text_x, text_y, text_x + text_w + 4, text_y + text_h + 2], fill=color)
            draw.text((text_x + 2, text_y), text, fill="white", font=font)

        # Save snapshot
        snap_img.save(snapshot_path, "JPEG", quality=82)

        # Thumbnail: max 320px wide, keep aspect ratio
        thumb_img = img.copy()
        thumb_img.thumbnail((320, 320 * img_h // img_w if img_w > 0 else 320))
        thumb_img.save(thumb_path, "JPEG", quality=70)

        # Return relative paths
        rel_snapshot = str(snapshot_path.relative_to(self.data_dir))
        rel_thumb = str(thumb_path.relative_to(self.data_dir))
        return rel_snapshot, rel_thumb

    def list_events(
        self,
        limit: int = 50,
        before_id: Optional[int] = None,
        label: Optional[str] = None,
        camera_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(limit, 200))
        query = "SELECT id, ts, camera_id, camera_name, labels, max_score, detections, frame_w, frame_h, snapshot, thumb FROM events"
        params = []
        conditions = []

        if before_id is not None:
            conditions.append("id < ?")
            params.append(before_id)
        if label is not None:
            # Match exact JSON token in the labels array
            conditions.append("labels LIKE ?")
            params.append(f'%"{label}"%')
        if camera_id is not None:
            conditions.append("camera_id = ?")
            params.append(camera_id)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        with self._lock:
            cur = self._conn.execute(query, params)
            rows = cur.fetchall()

        events = []
        for row in rows:
            event_id, ts, cam_id, cam_name, labels_json, max_score, detections_json, fw, fh, snap, thumb = row
            events.append({
                "id": event_id,
                "ts": ts,
                "camera_id": cam_id,
                "camera_name": cam_name,
                "labels": json.loads(labels_json),
                "max_score": max_score,
                "detections": json.loads(detections_json),
                "frame_w": fw,
                "frame_h": fh,
                "snapshot_url": f"/api/events/{event_id}/snapshot.jpg" if snap else None,
                "thumb_url": f"/api/events/{event_id}/thumb.jpg" if thumb else None,
            })
        return events

    def get_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, ts, camera_id, camera_name, labels, max_score, detections, frame_w, frame_h, snapshot, thumb FROM events WHERE id = ?",
                (event_id,),
            )
            row = cur.fetchone()

        if not row:
            return None

        event_id, ts, cam_id, cam_name, labels_json, max_score, detections_json, fw, fh, snap, thumb = row
        return {
            "id": event_id,
            "ts": ts,
            "camera_id": cam_id,
            "camera_name": cam_name,
            "labels": json.loads(labels_json),
            "max_score": max_score,
            "detections": json.loads(detections_json),
            "frame_w": fw,
            "frame_h": fh,
            "snapshot_url": f"/api/events/{event_id}/snapshot.jpg" if snap else None,
            "thumb_url": f"/api/events/{event_id}/thumb.jpg" if thumb else None,
        }

    def event_file(self, event_id: int, kind: str) -> Optional[Path]:
        if kind not in ("snapshot", "thumb"):
            return None
        with self._lock:
            cur = self._conn.execute(f"SELECT {kind} FROM events WHERE id = ?", (event_id,))
            row = cur.fetchone()
        if not row or not row[0]:
            return None
        return self.data_dir / row[0]

    def delete_event(self, event_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("SELECT snapshot, thumb FROM events WHERE id = ?", (event_id,))
            row = cur.fetchone()
            if not row:
                return False
            snap, thumb = row
            self._conn.execute("DELETE FROM events WHERE id = ?", (event_id,))

        # Delete files
        for rel in (snap, thumb):
            if rel:
                try:
                    (self.data_dir / rel).unlink(missing_ok=True)
                except Exception:
                    pass
        return True

    def purge_older_than(self, days: int) -> int:
        cutoff = int(time.time() * 1000) - days * 86400 * 1000
        deleted_count = 0

        with self._lock:
            cur = self._conn.execute("SELECT id, snapshot, thumb FROM events WHERE ts < ?", (cutoff,))
            rows = cur.fetchall()

        for event_id, snap, thumb in rows:
            for rel in (snap, thumb):
                if rel:
                    try:
                        (self.data_dir / rel).unlink(missing_ok=True)
                    except Exception:
                        pass

        with self._lock:
            cur = self._conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            deleted_count = cur.rowcount

        # Remove empty day directories
        if self.snapshots_dir.exists():
            for day_dir in self.snapshots_dir.iterdir():
                if day_dir.is_dir() and not any(day_dir.iterdir()):
                    try:
                        day_dir.rmdir()
                    except Exception:
                        pass

        return deleted_count

    def stats(self) -> Dict[str, Any]:
        now_ms = int(time.time() * 1000)
        day_ago = now_ms - 86400 * 1000

        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM events")
            total = cur.fetchone()[0]
            cur = self._conn.execute("SELECT COUNT(*) FROM events WHERE ts > ?", (day_ago,))
            last_24h = cur.fetchone()[0]

        # Disk usage
        disk_bytes = 0
        if self.snapshots_dir.exists():
            for f in self.snapshots_dir.rglob("*"):
                if f.is_file():
                    try:
                        disk_bytes += f.stat().st_size
                    except Exception:
                        pass

        return {
            "events_total": total,
            "events_24h": last_24h,
            "disk_bytes": disk_bytes,
        }
