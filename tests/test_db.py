import json
import sqlite3
import time
from pathlib import Path

import pytest
from PIL import Image

from server.db import DB, DEFAULT_SETTINGS, THEME_IDS


class TestDB:
    @pytest.fixture
    def db(self, tmp_path: Path) -> DB:
        return DB(tmp_path)

    def test_default_settings(self, db: DB):
        settings = db.get_settings()
        assert settings == DEFAULT_SETTINGS
        assert settings["labels"] == ["person", "car", "truck", "bus", "motorcycle", "bicycle", "cat", "dog", "bird"]
        assert settings["min_score"] == 0.55
        assert settings["cooldown_s"] == 30
        assert settings["preview_fps"] == 2
        assert settings["detect_fps"] == 2
        assert settings["mode"] == "auto"
        assert settings["retention_days"] == 14
        assert settings["theme"] == "tokyo-night"
        assert settings["jpeg_quality"] == 0.7
        assert settings["capture_width"] == 960

    def test_update_settings_valid(self, db: DB):
        new_settings = db.update_settings({"min_score": 0.7, "labels": ["person", "car"]})
        assert new_settings["min_score"] == 0.7
        assert new_settings["labels"] == ["person", "car"]
        # Other settings unchanged
        assert new_settings["cooldown_s"] == 30

    def test_update_settings_invalid_min_score(self, db: DB):
        with pytest.raises(ValueError, match="min_score"):
            db.update_settings({"min_score": 0.05})
        with pytest.raises(ValueError, match="min_score"):
            db.update_settings({"min_score": 0.96})
        # Bool for min_score should raise
        with pytest.raises(ValueError, match="min_score"):
            db.update_settings({"min_score": True})

    def test_update_settings_invalid_cooldown(self, db: DB):
        with pytest.raises(ValueError, match="cooldown_s"):
            db.update_settings({"cooldown_s": -1})
        with pytest.raises(ValueError, match="cooldown_s"):
            db.update_settings({"cooldown_s": 3601})

    def test_update_settings_invalid_fps(self, db: DB):
        with pytest.raises(ValueError, match="preview_fps"):
            db.update_settings({"preview_fps": 0.1})
        assert db.update_settings({"preview_fps": 15})["preview_fps"] == 15
        with pytest.raises(ValueError, match="preview_fps"):
            db.update_settings({"preview_fps": 16})
        with pytest.raises(ValueError, match="detect_fps"):
            db.update_settings({"detect_fps": 0.1})
        with pytest.raises(ValueError, match="detect_fps"):
            db.update_settings({"detect_fps": 11})

    def test_update_settings_invalid_retention(self, db: DB):
        with pytest.raises(ValueError, match="retention_days"):
            db.update_settings({"retention_days": 0})
        with pytest.raises(ValueError, match="retention_days"):
            db.update_settings({"retention_days": 366})

    def test_update_settings_invalid_labels(self, db: DB):
        with pytest.raises(ValueError, match="labels"):
            db.update_settings({"labels": ["person", "invalid_label"]})
        with pytest.raises(ValueError, match="labels"):
            db.update_settings({"labels": "not a list"})
        # Empty list is valid
        new_settings = db.update_settings({"labels": []})
        assert new_settings["labels"] == []

    def test_update_settings_invalid_mode(self, db: DB):
        with pytest.raises(ValueError, match="mode"):
            db.update_settings({"mode": "invalid"})

    def test_update_settings_new_keys(self, db: DB):
        for tid in THEME_IDS:
            assert db.update_settings({"theme": tid})["theme"] == tid
        with pytest.raises(ValueError, match="theme"):
            db.update_settings({"theme": "neon"})
        for bad in (0.29, 0.96, True, "0.5"):
            with pytest.raises(ValueError, match="jpeg_quality"):
                db.update_settings({"jpeg_quality": bad})
        assert db.update_settings({"jpeg_quality": 0.3})["jpeg_quality"] == 0.3
        for bad in (319, 1921, True, 640.5):
            with pytest.raises(ValueError, match="capture_width"):
                db.update_settings({"capture_width": bad})
        assert db.update_settings({"capture_width": 1920})["capture_width"] == 1920

    def test_non_finite_numbers_rejected(self, db: DB):
        for key in ("min_score", "cooldown_s", "preview_fps", "detect_fps", "jpeg_quality"):
            for bad in (float("nan"), float("inf"), float("-inf")):
                with pytest.raises(ValueError, match=key):
                    db.update_settings({key: bad})
        assert db.get_settings() == DEFAULT_SETTINGS

    def test_validate_settings_no_side_effects(self, db: DB):
        patch = {"labels": ["car", "person"], "min_score": 0.8}
        merged = db.validate_settings(patch)
        assert merged["min_score"] == 0.8 and merged["labels"] == ["person", "car"]
        assert patch["labels"] == ["car", "person"]
        assert db.get_settings() == DEFAULT_SETTINGS
        with pytest.raises(ValueError):
            db.validate_settings({"min_score": float("nan")})

    def test_oversized_image_skipped(self, db: DB, make_jpeg):
        ev = db.add_event(1, "c", "C", [], 5000, 100, make_jpeg(5000, 100))
        assert ev["snapshot_url"] is None and ev["thumb_url"] is None
        assert db.get_event(ev["id"])["snapshot_url"] is None

    def test_update_settings_unknown_key(self, db: DB):
        with pytest.raises(ValueError, match="unknown"):
            db.update_settings({"bogus": 1})

    def test_add_event_with_jpeg(self, db: DB, make_jpeg):
        jpeg = make_jpeg(640, 480)
        detections = [
            {"label": "person", "score": 0.9, "bbox": [100, 100, 200, 300]},
            {"label": "car", "score": 0.8, "bbox": [300, 200, 150, 100]},
        ]
        event = db.add_event(
            ts=int(time.time() * 1000),
            camera_id="cam1",
            camera_name="Front Door",
            detections=detections,
            frame_w=640,
            frame_h=480,
            jpeg=jpeg,
        )
        assert event["id"] == 1
        assert event["camera_id"] == "cam1"
        assert event["camera_name"] == "Front Door"
        # Labels ordered by score descending
        assert event["labels"] == ["person", "car"]
        assert event["max_score"] == 0.9
        assert event["frame_w"] == 640
        assert event["frame_h"] == 480
        assert event["snapshot_url"] == "/api/events/1/snapshot.jpg"
        assert event["thumb_url"] == "/api/events/1/thumb.jpg"

        # Verify files exist
        snap_path = db.event_file(1, "snapshot")
        thumb_path = db.event_file(1, "thumb")
        assert snap_path is not None and snap_path.exists()
        assert thumb_path is not None and thumb_path.exists()

        # Verify thumb width <= 320
        with Image.open(thumb_path) as img:
            assert img.width <= 320

    def test_add_event_without_jpeg(self, db: DB):
        detections = [{"label": "person", "score": 0.9, "bbox": [100, 100, 200, 300]}]
        event = db.add_event(
            ts=int(time.time() * 1000),
            camera_id="cam1",
            camera_name="Front Door",
            detections=detections,
            frame_w=640,
            frame_h=480,
            jpeg=None,
        )
        assert event["snapshot_url"] is None
        assert event["thumb_url"] is None
        assert db.event_file(1, "snapshot") is None
        assert db.event_file(1, "thumb") is None

    def test_list_events_order_and_filters(self, db: DB, make_jpeg):
        jpeg = make_jpeg()
        # Add 3 events
        for i in range(3):
            db.add_event(
                ts=int(time.time() * 1000) + i * 1000,
                camera_id=f"cam{i % 2}",
                camera_name="Cam",
                detections=[{"label": "person" if i % 2 == 0 else "car", "score": 0.9, "bbox": [0, 0, 10, 10]}],
                frame_w=640,
                frame_h=480,
                jpeg=jpeg,
            )
        # Newest first
        events = db.list_events(limit=10)
        assert len(events) == 3
        assert events[0]["id"] == 3
        assert events[1]["id"] == 2
        assert events[2]["id"] == 1

        # Filter by label (exact match)
        person_events = db.list_events(label="person")
        assert len(person_events) == 2
        assert all("person" in e["labels"] for e in person_events)
        # Filter 'cat' should not match 'car'
        cat_events = db.list_events(label="cat")
        assert len(cat_events) == 0

        # Filter by camera_id
        cam0_events = db.list_events(camera_id="cam0")
        assert len(cam0_events) == 2
        assert all(e["camera_id"] == "cam0" for e in cam0_events)

        # Pagination with before_id
        page1 = db.list_events(limit=2)
        assert len(page1) == 2
        assert page1[0]["id"] == 3
        page2 = db.list_events(limit=2, before_id=page1[-1]["id"])
        assert len(page2) == 1
        assert page2[0]["id"] == 1

    def test_get_event(self, db: DB, make_jpeg):
        jpeg = make_jpeg()
        db.add_event(
            ts=int(time.time() * 1000),
            camera_id="cam1",
            camera_name="Test",
            detections=[{"label": "person", "score": 0.9, "bbox": [0, 0, 10, 10]}],
            frame_w=640,
            frame_h=480,
            jpeg=jpeg,
        )
        event = db.get_event(1)
        assert event is not None
        assert event["id"] == 1
        assert db.get_event(999) is None

    def test_delete_event(self, db: DB, make_jpeg):
        jpeg = make_jpeg()
        db.add_event(
            ts=int(time.time() * 1000),
            camera_id="cam1",
            camera_name="Test",
            detections=[{"label": "person", "score": 0.9, "bbox": [0, 0, 10, 10]}],
            frame_w=640,
            frame_h=480,
            jpeg=jpeg,
        )
        assert db.delete_event(1) is True
        assert db.get_event(1) is None
        assert db.event_file(1, "snapshot") is None
        assert db.delete_event(1) is False  # Already deleted

    def test_purge_older_than(self, db: DB, make_jpeg):
        jpeg = make_jpeg()
        now_ms = int(time.time() * 1000)
        old_ms = now_ms - 20 * 86400 * 1000  # 20 days ago
        # Add old event
        db.add_event(
            ts=old_ms,
            camera_id="cam1",
            camera_name="Old",
            detections=[{"label": "person", "score": 0.9, "bbox": [0, 0, 10, 10]}],
            frame_w=640,
            frame_h=480,
            jpeg=jpeg,
        )
        # Add recent event
        db.add_event(
            ts=now_ms,
            camera_id="cam1",
            camera_name="New",
            detections=[{"label": "person", "score": 0.9, "bbox": [0, 0, 10, 10]}],
            frame_w=640,
            frame_h=480,
            jpeg=jpeg,
        )
        # Purge older than 14 days
        deleted = db.purge_older_than(14)
        assert deleted == 1
        assert db.get_event(1) is None
        assert db.get_event(2) is not None

    def test_stats(self, db: DB, make_jpeg):
        jpeg = make_jpeg()
        db.add_event(
            ts=int(time.time() * 1000),
            camera_id="cam1",
            camera_name="Test",
            detections=[{"label": "person", "score": 0.9, "bbox": [0, 0, 10, 10]}],
            frame_w=640,
            frame_h=480,
            jpeg=jpeg,
        )
        stats = db.stats()
        assert stats["events_total"] == 1
        assert stats["events_24h"] == 1
        assert stats["disk_bytes"] > 0


def test_migrates_v1_db(tmp_path: Path):
    conn = sqlite3.connect(tmp_path / "seccam.db")
    conn.executescript("""
        CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, camera_id TEXT NOT NULL,
            camera_name TEXT NOT NULL, labels TEXT NOT NULL, max_score REAL NOT NULL, detections TEXT NOT NULL,
            frame_w INTEGER NOT NULL, frame_h INTEGER NOT NULL, snapshot TEXT, thumb TEXT);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO events (ts, camera_id, camera_name, labels, max_score, detections, frame_w, frame_h)
            VALUES (1000, 'c', 'Cam', '["person"]', 0.8, '[]', 640, 480);
        INSERT INTO settings VALUES ('auth', '{"pin_hash": "x"}');
        INSERT INTO settings VALUES ('settings', '{"min_score": 0.6}');
    """)
    conn.commit()
    conn.close()
    for _ in range(2):  # idempotent
        db = DB(tmp_path)
        assert db._conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.get_event(1)["camera_name"] == "Cam"
        assert db.get_auth() == {"pin_hash": "x"}
        s = db.get_settings()
        assert s["min_score"] == 0.6 and s["theme"] == "tokyo-night" and s["capture_width"] == 960
        assert db.count_users() == 0
    db.delete_legacy_auth()
    assert db.get_auth() is None
    assert db.get_settings()["min_score"] == 0.6


class TestUsers:
    def test_create_and_lookup(self, db: DB):
        u = db.create_user("  Alice ", "h1")
        assert set(u) == {"id", "username", "password_hash", "totp_secret", "totp_enabled", "totp_last_step",
                          "recovery_codes", "created_at", "password_changed_at"}
        assert u["username"] == "Alice" and u["totp_enabled"] is False and u["recovery_codes"] == []
        assert db.get_user(u["id"]) == u
        assert db.get_user_by_username("ALICE")["id"] == u["id"]
        assert db.get_user(999) is None and db.get_user_by_username("bob") is None
        assert db.count_users() == 1
        with pytest.raises(ValueError, match="username taken"):
            db.create_user("alice", "h2")

    def test_create_first_user_atomic(self, db: DB):
        first = db.create_first_user("admin", "h")
        assert first is not None and first["username"] == "admin"
        assert db.create_first_user("other", "h") is None
        assert db.count_users() == 1

    def test_create_first_user_blocked_by_existing(self, db: DB):
        db.create_user("a", "h")
        assert db.create_first_user("b", "h") is None

    def test_update_user(self, db: DB):
        u = db.create_user("alice", "h")
        db.update_user(u["id"], password_hash="h2", totp_secret="SEC", totp_enabled=True,
                       totp_last_step=5, recovery_codes=["a", "b"], password_changed_at=42)
        got = db.get_user(u["id"])
        assert got["password_hash"] == "h2" and got["totp_secret"] == "SEC" and got["totp_enabled"] is True
        assert got["totp_last_step"] == 5 and got["recovery_codes"] == ["a", "b"] and got["password_changed_at"] == 42
        with pytest.raises(ValueError):
            db.update_user(u["id"], username="x")

    def test_consume_totp_step(self, db: DB):
        u = db.create_user("alice", "h")
        assert db.consume_totp_step(u["id"], 10) is True
        assert db.consume_totp_step(u["id"], 10) is False
        assert db.consume_totp_step(u["id"], 9) is False
        assert db.consume_totp_step(u["id"], 11) is True
        assert db.get_user(u["id"])["totp_last_step"] == 11

    def test_replace_recovery_codes_cas(self, db: DB):
        u = db.create_user("alice", "h")
        db.update_user(u["id"], recovery_codes=["a", "b", "c"])
        assert db.replace_recovery_codes(u["id"], ["a", "b", "c"], ["b", "c"]) is True
        assert db.replace_recovery_codes(u["id"], ["a", "b", "c"], ["a"]) is False
        assert db.get_user(u["id"])["recovery_codes"] == ["b", "c"]


class TestSessions:
    def test_lifecycle(self, db: DB):
        u = db.create_user("alice", "h")
        db.create_session("s1", u["id"], "csrf1", 10**13, "1.2.3.4", "UA")
        s = db.get_session("s1")
        assert set(s) == {"id", "user_id", "csrf", "created_at", "last_seen_at", "expires_at", "ip", "user_agent"}
        assert s["csrf"] == "csrf1" and s["ip"] == "1.2.3.4" and s["user_agent"] == "UA"
        db.touch_session("s1", 12345)
        assert db.get_session("s1")["last_seen_at"] == 12345
        db.delete_session("s1")
        assert db.get_session("s1") is None and db.get_session("nope") is None

    def test_delete_user_sessions_and_list(self, db: DB):
        a, b = db.create_user("a1x", "h"), db.create_user("b1x", "h")
        for sid, uid in (("s1", a["id"]), ("s2", a["id"]), ("s3", a["id"]), ("s4", b["id"])):
            db.create_session(sid, uid, "c", 10**13, None, None)
        assert sorted(db.list_session_ids(a["id"])) == ["s1", "s2", "s3"]
        assert db.delete_user_sessions(a["id"], except_id="s1") == 2
        assert db.list_session_ids(a["id"]) == ["s1"]
        assert db.delete_user_sessions(a["id"]) == 1
        assert db.list_session_ids(b["id"]) == ["s4"]

    def test_purge_expired(self, db: DB):
        u = db.create_user("alice", "h")
        now = int(time.time() * 1000)
        db.create_session("live", u["id"], "c", now + 100000, None, None)
        db.create_session("expired", u["id"], "c", now - 1, None, None)
        db.create_session("idle", u["id"], "c", now + 10**9, None, None)
        db.touch_session("idle", now - 8 * 86400 * 1000)
        assert db.purge_expired_sessions(now) == 2
        assert db.list_session_ids(u["id"]) == ["live"]

    def test_user_delete_cascades(self, db: DB):
        u = db.create_user("alice", "h")
        db.create_session("s1", u["id"], "c", 10**13, None, None)
        db._conn.execute("DELETE FROM users WHERE id = ?", (u["id"],))
        assert db.get_session("s1") is None

    def test_session_requires_user(self, db: DB):
        with pytest.raises(sqlite3.IntegrityError):
            db.create_session("s1", 999, "c", 1, None, None)


class TestDevices:
    def test_crud(self, db: DB):
        d = db.create_device("0123456789ab", " Garage ", "agent", "hash1")
        assert d == {"id": "0123456789ab", "name": "Garage", "kind": "agent",
                     "created_at": d["created_at"], "last_seen_at": None, "last_ip": None}
        assert "token_hash" not in d
        full = db.get_device("0123456789ab")
        assert full["token_hash"] == "hash1" and full["name"] == "Garage"
        assert db.get_device("ffffffffffff") is None
        db.touch_device("0123456789ab", 777, "10.0.0.5")
        got = db.get_device("0123456789ab")
        assert got["last_seen_at"] == 777 and got["last_ip"] == "10.0.0.5"
        assert db.rename_device("0123456789ab", " Porch ")["name"] == "Porch"
        assert db.rename_device("ffffffffffff", "x") is None
        assert db.delete_device("0123456789ab") is True
        assert db.delete_device("0123456789ab") is False

    def test_list_ordered_without_token_hash(self, db: DB):
        db.create_device("aaaaaaaaaaaa", "one", "browser", "h")
        db.create_device("bbbbbbbbbbbb", "two", "agent", "h")
        devs = db.list_devices()
        assert [d["id"] for d in devs] == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]
        assert all("token_hash" not in d for d in devs)

    def test_kind_check(self, db: DB):
        with pytest.raises(sqlite3.IntegrityError):
            db.create_device("cccccccccccc", "x", "toaster", "h")


def test_backup_to(db: DB, tmp_path: Path):
    db.create_user("alice", "h")
    dest = tmp_path / "backup.db"
    db.backup_to(dest)
    conn = sqlite3.connect(dest)
    assert conn.execute("SELECT username FROM users").fetchall() == [("alice",)]
    conn.close()


def test_contract_shapes(db: DB, make_jpeg):
    from conftest import assert_shape
    assert_shape("Settings", db.get_settings())
    ev = db.add_event(1, "c", "C", [{"label": "person", "score": 0.9, "bbox": [0, 0, 5, 5]}], 640, 480, make_jpeg())
    assert_shape("Event", ev)
    assert_shape("Device", {**db.create_device("dddddddddddd", "d", "agent", "h"), "connected": False})
