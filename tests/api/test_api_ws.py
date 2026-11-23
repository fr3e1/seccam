import json
import time

import pytest
from fastapi.testclient import TestClient

import server.app as app_module
from server import auth, hub as hub_module
from tests.api.conftest import ADMIN_PASSWORD, login, make_admin
from tests.conftest import assert_shape


def closing_code(ws, limit=50):
    """Read until the server closes; returns the close code."""
    for _ in range(limit):
        msg = ws.receive()
        if msg["type"] == "websocket.close":
            return msg["code"]
    raise AssertionError("socket did not close")


def recv_until(ws, pred, limit=50):
    for _ in range(limit):
        msg = ws.receive()
        if msg["type"] == "websocket.close":
            raise AssertionError(f"closed: {msg}")
        if pred(msg):
            return msg
    raise AssertionError("message not seen")


def is_json(kind):
    return lambda m: m.get("text") is not None and json.loads(m["text"]).get("type") == kind


def hello(ws, camera_id="cam1", name="Front", **extra):
    ws.send_text(json.dumps({"type": "hello", "camera_id": camera_id, "name": name, **extra}))
    return json.loads(recv_until(ws, is_json("config"))["text"])


def test_viewer_requires_admin_session(app):
    make_admin(app)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/viewer") as ws:
            assert closing_code(ws) == 4401


def test_viewer_gets_cameras_and_settings(admin_client):
    with admin_client.websocket_connect("/ws/viewer?proto=2") as ws:
        cams = json.loads(recv_until(ws, is_json("cameras"))["text"])
        assert cams["cameras"] == []
        settings = json.loads(recv_until(ws, is_json("settings"))["text"])
        assert_shape("Settings", settings["settings"])
        ws.send_text(json.dumps({"type": "ping"}))
        assert json.loads(recv_until(ws, is_json("pong"))["text"]) == {"type": "pong"}
        ws.send_text("garbage")
        ws.send_text(json.dumps({"type": "ping"}))
        recv_until(ws, is_json("pong"))


def test_admin_camera_flow_proto2_frame(admin_client, make_jpeg):
    jpeg = make_jpeg(64, 48)
    with admin_client.websocket_connect("/ws/viewer?proto=2") as viewer, \
            admin_client.websocket_connect("/ws/camera") as cam:
        config = hello(cam, "cam1", "Front")
        assert config["camera_id"] == "cam1"
        assert_shape("Settings", config["settings"])
        cam.send_text(json.dumps({"type": "mode", "mode": "local"}))
        cam.send_text(json.dumps({"type": "ping"}))
        recv_until(cam, is_json("pong"))
        cam.send_bytes(jpeg)
        frame = recv_until(viewer, lambda m: m.get("bytes") is not None)["bytes"]
        header, body = hub_module.decode_viewer_frame(frame)
        assert body == jpeg
        assert_shape("ViewerFrameHeader", header)
        assert header["camera_id"] == "cam1"
        cameras = admin_client.get("/api/cameras").json()["cameras"]
        assert cameras[0]["camera_id"] == "cam1" and cameras[0]["kind"] == "browser" and cameras[0]["device_id"] is None
        assert_shape("Camera", cameras[0])


def test_proto1_viewer_gets_base64_json_frames(admin_client, make_jpeg):
    import base64
    jpeg = make_jpeg(64, 48)
    with admin_client.websocket_connect("/ws/viewer") as viewer, admin_client.websocket_connect("/ws/camera") as cam:
        hello(cam)
        cam.send_bytes(jpeg)
        msg = json.loads(recv_until(viewer, is_json("frame"))["text"])
        assert msg["camera_id"] == "cam1" and base64.b64decode(msg["jpeg"]) == jpeg
        assert {"ts", "detections", "frame_w", "frame_h"} <= set(msg)


def test_camera_local_detections_create_event(admin_client):
    with admin_client.websocket_connect("/ws/viewer?proto=2") as viewer, admin_client.websocket_connect("/ws/camera") as cam:
        hello(cam)
        cam.send_text(json.dumps({"type": "mode", "mode": "local"}))
        dets = [{"label": "person", "score": 0.95, "bbox": [1, 2, 30, 40]}, {"label": 5, "score": 2, "bbox": [1]}]
        cam.send_text(json.dumps({"type": "detections", "detections": dets, "frame_w": 640, "frame_h": 480, "infer_ms": 12}))
        msg = json.loads(recv_until(viewer, is_json("event"))["text"])
        assert_shape("Event", msg["event"])
        assert [d["label"] for d in msg["event"]["detections"]] == ["person"]


def test_camera_hello_rules(admin_client):
    with admin_client.websocket_connect("/ws/camera") as ws:
        ws.send_text(json.dumps({"type": "ping"}))
        assert closing_code(ws) == 4400
    with admin_client.websocket_connect("/ws/camera") as ws:
        ws.send_text("not json")
        assert closing_code(ws) == 4400
    with admin_client.websocket_connect("/ws/camera") as ws:
        ws.send_bytes(b"\xff\xd8")
        assert closing_code(ws) == 4400
    for bad in ("../x", "", "a" * 65, 5):
        with admin_client.websocket_connect("/ws/camera") as ws:
            ws.send_text(json.dumps({"type": "hello", "camera_id": bad, "name": "n"}))
            assert closing_code(ws) == 4400, bad


def test_hello_timeout(admin_client, monkeypatch):
    monkeypatch.setattr(app_module, "HELLO_TIMEOUT_S", 0.2)
    with admin_client.websocket_connect("/ws/camera") as ws:
        assert closing_code(ws) == 4400


def test_newer_connection_replaces_same_camera_id(admin_client):
    with admin_client.websocket_connect("/ws/camera") as first:
        hello(first, "dup")
        with admin_client.websocket_connect("/ws/camera") as second:
            hello(second, "dup")
            assert closing_code(first) == 4409
            second.send_text(json.dumps({"type": "ping"}))
            recv_until(second, is_json("pong"))


def test_settings_change_pushed_to_camera_and_viewer(admin_client):
    with admin_client.websocket_connect("/ws/viewer?proto=2") as viewer, admin_client.websocket_connect("/ws/camera") as cam:
        hello(cam)
        admin_client.put("/api/settings", json={"theme": "nord"})
        assert json.loads(recv_until(cam, is_json("config"))["text"])["settings"]["theme"] == "nord"
        recv_until(viewer, lambda m: m.get("text") and json.loads(m["text"]).get("type") == "settings"
                   and json.loads(m["text"])["settings"]["theme"] == "nord")


def test_logout_closes_open_viewer_with_4401(admin_client):
    with admin_client.websocket_connect("/ws/viewer?proto=2") as ws:
        recv_until(ws, is_json("settings"))
        assert admin_client.post("/api/logout").status_code == 200
        assert closing_code(ws) == 4401


def test_revoke_others_closes_other_sessions_sockets(app):
    make_admin(app)
    with TestClient(app) as a, TestClient(app) as b:
        login(a)
        login(b)
        with b.websocket_connect("/ws/viewer?proto=2") as vb, b.websocket_connect("/ws/camera") as cb, \
                a.websocket_connect("/ws/viewer?proto=2") as va:
            hello(cb, "bcam")
            recv_until(va, is_json("settings"))
            assert a.post("/api/sessions/revoke-others").json() == {"revoked": 1}
            assert closing_code(vb) == 4401
            assert closing_code(cb) == 4401
            va.send_text(json.dumps({"type": "ping"}))
            recv_until(va, is_json("pong"))


def test_password_change_closes_all_old_sockets(admin_client):
    with admin_client.websocket_connect("/ws/viewer?proto=2") as ws:
        recv_until(ws, is_json("settings"))
        resp = admin_client.post("/api/account/password",
                                 json={"current_password": ADMIN_PASSWORD, "new_password": "another-long-pass-9"})
        assert resp.status_code == 200
        assert closing_code(ws) == 4401


def test_session_recheck_closes_after_cli_revocation(admin_client, app, monkeypatch):
    monkeypatch.setattr(app_module, "SESSION_RECHECK_S", 0.1)
    with admin_client.websocket_connect("/ws/viewer?proto=2") as ws:
        recv_until(ws, is_json("settings"))
        app.state.db.delete_user_sessions(1)  # what `manage revoke-sessions` does
        assert closing_code(ws, limit=200) == 4401


def test_device_camera_forced_id_and_name(app, admin_client, device_token):
    device_id = auth.parse_device_token(device_token)[0]
    with TestClient(app) as dev:
        with dev.websocket_connect("/ws/camera", headers={"Authorization": f"Bearer {device_token}"}) as ws:
            config = hello(ws, "attacker-chosen", "", kind="browser")
            assert config["camera_id"] == device_id
            cam = admin_client.get("/api/cameras").json()["cameras"][0]
            assert (cam["camera_id"], cam["name"], cam["kind"], cam["device_id"]) == (device_id, "Garage", "agent", device_id)
            assert admin_client.get("/api/devices").json()["devices"][0]["connected"] is True
    with TestClient(app) as dev:
        dev.cookies.set("seccam_device", device_token)
        with dev.websocket_connect("/ws/camera") as ws:
            assert hello(ws, "x", "Named")["camera_id"] == device_id
            assert admin_client.get("/api/cameras").json()["cameras"][0]["name"] == "Named"


def test_device_cannot_open_viewer(app, device_token):
    make_admin(app)
    with TestClient(app) as dev:
        with dev.websocket_connect("/ws/viewer", headers={"Authorization": f"Bearer {device_token}"}) as ws:
            assert closing_code(ws) == 4401


def test_revoking_device_closes_its_socket(app, admin_client, device_token):
    device_id = auth.parse_device_token(device_token)[0]
    with TestClient(app) as dev:
        with dev.websocket_connect("/ws/camera", headers={"Authorization": f"Bearer {device_token}"}) as ws:
            hello(ws)
            assert admin_client.delete(f"/api/devices/{device_id}").status_code == 204
            assert closing_code(ws) == 4401
        with dev.websocket_connect("/ws/camera", headers={"Authorization": f"Bearer {device_token}"}) as ws:
            assert closing_code(ws) == 4401


def test_cli_revoked_device_dropped_by_recheck(app, device_token, monkeypatch):
    make_admin(app)
    monkeypatch.setattr(app_module, "SESSION_RECHECK_S", 0.1)
    device_id = auth.parse_device_token(device_token)[0]
    with TestClient(app) as dev:
        with dev.websocket_connect("/ws/camera", headers={"Authorization": f"Bearer {device_token}"}) as ws:
            hello(ws)
            app.state.db.delete_device(device_id)
            assert closing_code(ws, limit=200) == 4401


def test_invalid_device_credential_4401_then_4429(app, device_token):
    make_admin(app)
    forged = device_token[:-3] + "xxx"
    with TestClient(app, client=("10.5.5.5", 1)) as dev:
        for _ in range(5):
            with dev.websocket_connect("/ws/camera", headers={"Authorization": f"Bearer {forged}"}) as ws:
                assert closing_code(ws) == 4401
        with dev.websocket_connect("/ws/camera", headers={"Authorization": f"Bearer {forged}"}) as ws:
            assert closing_code(ws) == 4429


def test_ws_setup_mode_4403(setup_client):
    for path in ("/ws/camera", "/ws/viewer"):
        with setup_client.websocket_connect(path) as ws:
            assert closing_code(ws) == 4403


def test_ws_bad_origin_4403(admin_client):
    for path in ("/ws/camera", "/ws/viewer"):
        with admin_client.websocket_connect(path, headers={"Origin": "http://evil.example"}) as ws:
            assert closing_code(ws) == 4403
    with admin_client.websocket_connect("/ws/viewer", headers={"Origin": "http://testserver"}) as ws:
        recv_until(ws, is_json("settings"))


def test_ws_rate_limit_4429(admin_client, app):
    for _ in range(10):
        app.state.ws_limiter.hit("ws:ip:testclient")
    with admin_client.websocket_connect("/ws/viewer") as ws:
        assert closing_code(ws) == 4429
