import asyncio
import base64
import json
import time
from typing import AsyncGenerator

import pytest
from fastapi.testclient import TestClient

from server.hub import Hub
from server.db import DB
from server.detector import Detector
from server.config import Config
from server.auth import derive_token_secret, make_token


@pytest.fixture
def hub(cfg: Config, tmp_path) -> Hub:
    db = DB(cfg.data_dir)
    detector = Detector(cfg.model_path)
    return Hub(db, detector)


@pytest.fixture
def ws_auth_cookie(cfg: Config) -> str:
    """Create a valid auth cookie value for websocket testing using derived token secret."""
    # In test config, SECCAM_PIN=1234 is set and no DB auth exists yet
    pin_component = cfg.pin
    token_secret = derive_token_secret(cfg.secret, pin_component)
    return make_token(token_secret, ttl_s=30 * 86400)


class TestHubWebSocket:
    def test_camera_hello_receives_config(self, authed_client: TestClient, ws_auth_cookie: str):
        with authed_client.websocket_connect("/ws/camera", cookies={"seccam_session": ws_auth_cookie}) as ws:
            ws.send_text(json.dumps({"type": "hello", "camera_id": "test-cam", "name": "Test Camera"}))
            msg = ws.receive_text()
            data = json.loads(msg)
            assert data["type"] == "config"
            assert "settings" in data

    def test_viewer_receives_cameras_list(self, authed_client: TestClient, ws_auth_cookie: str):
        with authed_client.websocket_connect("/ws/viewer", cookies={"seccam_session": ws_auth_cookie}) as ws:
            msg = ws.receive_text()
            data = json.loads(msg)
            assert data["type"] == "cameras"
            assert "cameras" in data

    def test_camera_appears_in_viewer_list(self, authed_client: TestClient, ws_auth_cookie: str):
        with authed_client.websocket_connect("/ws/camera", cookies={"seccam_session": ws_auth_cookie}) as cam_ws:
            cam_ws.send_text(json.dumps({"type": "hello", "camera_id": "cam-list-test", "name": "List Test"}))
            cam_ws.receive_text()
            
            with authed_client.websocket_connect("/ws/viewer", cookies={"seccam_session": ws_auth_cookie}) as viewer_ws:
                viewer_ws.receive_text()
                msg = viewer_ws.receive_text()
                data = json.loads(msg)
                if data["type"] == "cameras":
                    cameras = data["cameras"]
                    assert any(c["camera_id"] == "cam-list-test" for c in cameras)

    def test_camera_sends_frame_and_detections_creates_event(self, authed_client: TestClient, ws_auth_cookie: str, make_jpeg):
        jpeg = make_jpeg(640, 480)
        
        with authed_client.websocket_connect("/ws/camera", cookies={"seccam_session": ws_auth_cookie}) as cam_ws:
            cam_ws.send_text(json.dumps({"type": "hello", "camera_id": "event-cam", "name": "Event Camera"}))
            cam_ws.receive_text()
            cam_ws.send_text(json.dumps({"type": "mode", "mode": "local"}))
            
            with authed_client.websocket_connect("/ws/viewer", cookies={"seccam_session": ws_auth_cookie}) as viewer_ws:
                viewer_ws.receive_text()
                viewer_ws.receive_text()
                
                cam_ws.send_bytes(jpeg)
                cam_ws.send_text(json.dumps({
                    "type": "detections",
                    "detections": [{"label": "person", "score": 0.9, "bbox": [100, 100, 200, 300]}],
                    "frame_w": 640,
                    "frame_h": 480,
                    "infer_ms": 50
                }))
                
                event_received = False
                for _ in range(20):
                    msg = viewer_ws.receive_text()
                    data = json.loads(msg)
                    if data["type"] == "event":
                        event_received = True
                        event = data["event"]
                        assert event["camera_id"] == "event-cam"
                        assert "person" in event["labels"]
                        assert event["max_score"] >= 0.9
                        assert event["snapshot_url"] is not None
                        break
                assert event_received, "Did not receive event message"

    def test_event_visible_in_api(self, authed_client: TestClient, ws_auth_cookie: str, make_jpeg):
        jpeg = make_jpeg(640, 480)
        
        with authed_client.websocket_connect("/ws/camera", cookies={"seccam_session": ws_auth_cookie}) as cam_ws:
            cam_ws.send_text(json.dumps({"type": "hello", "camera_id": "api-event-cam", "name": "API Event Camera"}))
            cam_ws.receive_text()
            cam_ws.send_text(json.dumps({"type": "mode", "mode": "local"}))
            
            with authed_client.websocket_connect("/ws/viewer", cookies={"seccam_session": ws_auth_cookie}) as viewer_ws:
                viewer_ws.receive_text()
                viewer_ws.receive_text()
                
                cam_ws.send_bytes(jpeg)
                cam_ws.send_text(json.dumps({
                    "type": "detections",
                    "detections": [{"label": "person", "score": 0.9, "bbox": [100, 100, 200, 300]}],
                    "frame_w": 640,
                    "frame_h": 480,
                    "infer_ms": 50
                }))
                
                event_id = None
                for _ in range(20):
                    msg = viewer_ws.receive_text()
                    data = json.loads(msg)
                    if data["type"] == "event":
                        event_id = data["event"]["id"]
                        break
                assert event_id is not None, "No event received"
                
                resp = authed_client.get(f"/api/events/{event_id}")
                assert resp.status_code == 200
                event = resp.json()
                assert event["id"] == event_id
                assert event["camera_id"] == "api-event-cam"
                
                resp = authed_client.get(f"/api/events/{event_id}/snapshot.jpg")
                assert resp.status_code == 200
                assert resp.headers["content-type"] == "image/jpeg"

    def test_cooldown_prevents_duplicate_events(self, authed_client: TestClient, ws_auth_cookie: str, make_jpeg):
        jpeg = make_jpeg(640, 480)
        
        with authed_client.websocket_connect("/ws/camera", cookies={"seccam_session": ws_auth_cookie}) as cam_ws:
            cam_ws.send_text(json.dumps({"type": "hello", "camera_id": "cooldown-cam", "name": "Cooldown Camera"}))
            cam_ws.receive_text()
            cam_ws.send_text(json.dumps({"type": "mode", "mode": "local"}))
            
            with authed_client.websocket_connect("/ws/viewer", cookies={"seccam_session": ws_auth_cookie}) as viewer_ws:
                viewer_ws.receive_text()
                viewer_ws.receive_text()
                
                # First detection
                cam_ws.send_bytes(jpeg)
                cam_ws.send_text(json.dumps({
                    "type": "detections",
                    "detections": [{"label": "person", "score": 0.9, "bbox": [100, 100, 200, 300]}],
                    "frame_w": 640,
                    "frame_h": 480,
                    "infer_ms": 50
                }))
                
                # Wait for first event via websocket
                event_id = None
                for _ in range(20):
                    msg = viewer_ws.receive_text()
                    data = json.loads(msg)
                    if data["type"] == "event":
                        event_id = data["event"]["id"]
                        break
                assert event_id is not None, "First event not received"
                
                # Second detection immediately after (within cooldown)
                cam_ws.send_bytes(jpeg)
                cam_ws.send_text(json.dumps({
                    "type": "detections",
                    "detections": [{"label": "person", "score": 0.9, "bbox": [100, 100, 200, 300]}],
                    "frame_w": 640,
                    "frame_h": 480,
                    "infer_ms": 50
                }))
                
                # Poll API to verify no new event created (cooldown should prevent it)
                # Wait a bit for processing
                time.sleep(0.5)
                for _ in range(20):
                    resp = authed_client.get("/api/events?limit=10&camera_id=cooldown-cam")
                    assert resp.status_code == 200
                    events = resp.json()["events"]
                    if len(events) == 1:
                        break
                    time.sleep(0.05)
                else:
                    pytest.fail("Timed out waiting for event count to stabilize")
                
                # Should only have 1 event due to cooldown (default 30s)
                resp = authed_client.get("/api/events?limit=10&camera_id=cooldown-cam")
                events = resp.json()["events"]
                assert len(events) == 1, f"Expected 1 event, got {len(events)}"

    def test_disabled_label_no_event(self, authed_client: TestClient, ws_auth_cookie: str, make_jpeg):
        jpeg = make_jpeg(640, 480)
        
        with authed_client.websocket_connect("/ws/camera", cookies={"seccam_session": ws_auth_cookie}) as cam_ws:
            cam_ws.send_text(json.dumps({"type": "hello", "camera_id": "label-cam", "name": "Label Camera"}))
            cam_ws.receive_text()
            cam_ws.send_text(json.dumps({"type": "mode", "mode": "local"}))
            
            with authed_client.websocket_connect("/ws/viewer", cookies={"seccam_session": ws_auth_cookie}) as viewer_ws:
                viewer_ws.receive_text()
                viewer_ws.receive_text()
                
                # Send detection for 'bear' which is not in default enabled labels
                cam_ws.send_bytes(jpeg)
                cam_ws.send_text(json.dumps({
                    "type": "detections",
                    "detections": [{"label": "bear", "score": 0.9, "bbox": [100, 100, 200, 300]}],
                    "frame_w": 640,
                    "frame_h": 480,
                    "infer_ms": 50
                }))
                
                # Poll API to verify no event created
                time.sleep(0.5)
                for _ in range(20):
                    resp = authed_client.get("/api/events?limit=10&camera_id=label-cam")
                    assert resp.status_code == 200
                    events = resp.json()["events"]
                    if len(events) == 0:
                        break
                    time.sleep(0.05)
                else:
                    pytest.fail("Timed out waiting for event count")
                
                resp = authed_client.get("/api/events?limit=10&camera_id=label-cam")
                events = resp.json()["events"]
                assert len(events) == 0, f"Event created for disabled label: {events}"

    def test_below_min_score_no_event(self, authed_client: TestClient, ws_auth_cookie: str, make_jpeg):
        jpeg = make_jpeg(640, 480)
        
        with authed_client.websocket_connect("/ws/camera", cookies={"seccam_session": ws_auth_cookie}) as cam_ws:
            cam_ws.send_text(json.dumps({"type": "hello", "camera_id": "score-cam", "name": "Score Camera"}))
            cam_ws.receive_text()
            cam_ws.send_text(json.dumps({"type": "mode", "mode": "local"}))
            
            with authed_client.websocket_connect("/ws/viewer", cookies={"seccam_session": ws_auth_cookie}) as viewer_ws:
                viewer_ws.receive_text()
                viewer_ws.receive_text()
                
                # Send detection with score below default min_score (0.55)
                cam_ws.send_bytes(jpeg)
                cam_ws.send_text(json.dumps({
                    "type": "detections",
                    "detections": [{"label": "person", "score": 0.3, "bbox": [100, 100, 200, 300]}],
                    "frame_w": 640,
                    "frame_h": 480,
                    "infer_ms": 50
                }))
                
                # Poll API to verify no event created
                time.sleep(0.5)
                for _ in range(20):
                    resp = authed_client.get("/api/events?limit=10&camera_id=score-cam")
                    assert resp.status_code == 200
                    events = resp.json()["events"]
                    if len(events) == 0:
                        break
                    time.sleep(0.05)
                else:
                    pytest.fail("Timed out waiting for event count")
                
                resp = authed_client.get("/api/events?limit=10&camera_id=score-cam")
                events = resp.json()["events"]
                assert len(events) == 0, f"Event created for detection below min_score: {events}"

    def test_camera_reconnect_same_id(self, authed_client: TestClient, ws_auth_cookie: str):
        with authed_client.websocket_connect("/ws/camera", cookies={"seccam_session": ws_auth_cookie}) as cam_ws:
            cam_ws.send_text(json.dumps({"type": "hello", "camera_id": "reconnect-cam", "name": "Reconnect Camera"}))
            cam_ws.receive_text()
        
        with authed_client.websocket_connect("/ws/camera", cookies={"seccam_session": ws_auth_cookie}) as cam_ws2:
            cam_ws2.send_text(json.dumps({"type": "hello", "camera_id": "reconnect-cam", "name": "Reconnect Camera"}))
            cam_ws2.receive_text()
            
            with authed_client.websocket_connect("/ws/viewer", cookies={"seccam_session": ws_auth_cookie}) as viewer_ws:
                viewer_ws.receive_text()
                viewer_ws.receive_text()
                for _ in range(10):
                    msg = viewer_ws.receive_text()
                    data = json.loads(msg)
                    if data["type"] == "cameras":
                        cameras = data["cameras"]
                        assert any(c["camera_id"] == "reconnect-cam" for c in cameras)
                        break


class TestHubDirect:
    def test_hub_camera_connected(self, hub: Hub):
        messages = []
        async def send_json(msg):
            messages.append(msg)
        
        asyncio.run(hub.camera_connected("test-cam", "Test Camera", send_json))
        assert len(messages) == 1
        assert messages[0]["type"] == "config"
        assert "settings" in messages[0]

    def test_hub_viewer_connected(self, hub: Hub):
        messages = []
        async def send_json(msg):
            messages.append(msg)
        
        asyncio.run(hub.viewer_connected("viewer1", send_json))
        types = [m["type"] for m in messages]
        assert "cameras" in types
        assert "settings" in types

    def test_hub_cameras_list(self, hub: Hub):
        messages = []
        async def send_json(msg):
            messages.append(msg)
        
        asyncio.run(hub.camera_connected("cam1", "Camera 1", send_json))
        cameras = hub.cameras()
        assert len(cameras) == 1
        assert cameras[0]["camera_id"] == "cam1"
        assert cameras[0]["name"] == "Camera 1"
        assert cameras[0]["connected"] is True

    def test_hub_camera_disconnected(self, hub: Hub):
        messages = []
        async def send_json(msg):
            messages.append(msg)
        
        asyncio.run(hub.camera_connected("cam1", "Camera 1", send_json))
        asyncio.run(hub.camera_disconnected("cam1"))
        cameras = hub.cameras()
        assert len(cameras) == 0
