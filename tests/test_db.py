import json
import time
from pathlib import Path

import pytest
from PIL import Image

from server.db import DB, DEFAULT_SETTINGS, ALL_LABELS


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
        with pytest.raises(ValueError, match="preview_fps"):
            db.update_settings({"preview_fps": 11})
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
