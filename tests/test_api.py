from starlette.websockets import WebSocketDisconnect
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from server.config import Config


class TestPublicEndpoints:
    def test_health_public(self, client: TestClient):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "model_available" in data
        assert data["version"] == "1.1.0"

    def test_login_page_public(self, client: TestClient):
        resp = client.get("/login")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_setup_page_public(self, client: TestClient):
        resp = client.get("/setup")
        assert resp.status_code == 200

    def test_ca_crt_not_configured(self, client: TestClient, cfg: Config):
        # CA cert not configured in fixture
        resp = client.get("/ca.crt")
        assert resp.status_code == 404

    def test_static_files(self, client: TestClient):
        resp = client.get("/static/style.css")
        assert resp.status_code == 200
        assert "text/css" in resp.headers["content-type"]


class TestAuthRequired:
    @pytest.mark.parametrize("path", ["/", "/camera"])
    def test_html_pages_redirect_to_login(self, client: TestClient, path: str):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login?next=")

    @pytest.mark.parametrize("path", ["/api/settings", "/api/events", "/api/cameras", "/api/stats", "/api/events/1"])
    def test_api_returns_401(self, client: TestClient, path: str):
        resp = client.get(path)
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Unauthorized"}

    def test_ws_camera_unauthenticated(self, client: TestClient):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws/camera") as ws:
                ws.receive_text()
        assert exc.value.code == 4401

    def test_ws_viewer_unauthenticated(self, client: TestClient):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws/viewer") as ws:
                ws.receive_text()
        assert exc.value.code == 4401


class TestLogin:
    def test_wrong_pin(self, client: TestClient):
        resp = client.post("/api/login", json={"pin": "wrong"})
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Invalid PIN"}

    def test_correct_pin_sets_cookie(self, client: TestClient, cfg: Config):
        resp = client.post("/api/login", json={"pin": "1234"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert "seccam_session" in client.cookies

    def test_rate_limit_after_5_failures(self, client: TestClient):
        for _ in range(5):
            resp = client.post("/api/login", json={"pin": "wrong"})
            assert resp.status_code == 401
        # 6th attempt should be rate limited
        resp = client.post("/api/login", json={"pin": "wrong"})
        assert resp.status_code == 429
        assert "detail" in resp.json()

    def test_malformed_json_returns_400(self, client: TestClient):
        resp = client.post("/api/login", content="not json", headers={"Content-Type": "application/json"})
        assert resp.status_code == 400

    def test_logout(self, authed_client: TestClient):
        resp = authed_client.post("/api/logout")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        # Cookie should be cleared
        assert "seccam_session" not in authed_client.cookies or authed_client.cookies.get("seccam_session") == ""


class TestSettings:
    def test_get_settings(self, authed_client: TestClient):
        resp = authed_client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "labels" in data
        assert "min_score" in data

    def test_put_settings_valid(self, authed_client: TestClient):
        resp = authed_client.put("/api/settings", json={"min_score": 0.7, "labels": ["person"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["min_score"] == 0.7
        assert data["labels"] == ["person"]

    def test_put_settings_invalid_min_score(self, authed_client: TestClient):
        resp = authed_client.put("/api/settings", json={"min_score": 0.05})
        assert resp.status_code == 422
        assert "detail" in resp.json()

    def test_put_settings_invalid_labels(self, authed_client: TestClient):
        resp = authed_client.put("/api/settings", json={"labels": ["invalid"]})
        assert resp.status_code == 422

    def test_put_settings_invalid_mode(self, authed_client: TestClient):
        resp = authed_client.put("/api/settings", json={"mode": "invalid"})
        assert resp.status_code == 422


class TestEvents:
    def test_list_events_empty(self, authed_client: TestClient):
        resp = authed_client.get("/api/events")
        assert resp.status_code == 200
        assert resp.json() == {"events": []}

    def test_get_event_not_found(self, authed_client: TestClient):
        resp = authed_client.get("/api/events/999")
        assert resp.status_code == 404

    def test_delete_event_not_found(self, authed_client: TestClient):
        resp = authed_client.delete("/api/events/999")
        assert resp.status_code == 404

    def test_snapshot_not_found(self, authed_client: TestClient):
        resp = authed_client.get("/api/events/1/snapshot.jpg")
        assert resp.status_code == 404

    def test_thumb_not_found(self, authed_client: TestClient):
        resp = authed_client.get("/api/events/1/thumb.jpg")
        assert resp.status_code == 404

    def test_events_limit_invalid(self, authed_client: TestClient):
        resp = authed_client.get("/api/events?limit=abc")
        assert resp.status_code == 422


class TestSecurityHeaders:
    def test_security_headers_on_pages(self, authed_client: TestClient):
        resp = authed_client.get("/")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Referrer-Policy"] == "no-referrer"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["Cache-Control"] == "no-store"

    def test_security_headers_on_api(self, authed_client: TestClient):
        resp = authed_client.get("/api/settings")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Referrer-Policy"] == "no-referrer"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["Cache-Control"] == "no-store"

    def test_no_open_redirect(self, client: TestClient):
        # Login page is static, but verify server doesn't redirect to external URLs
        resp = client.get("/login?next=//evil.com", follow_redirects=False)
        assert resp.status_code == 200  # Should serve login page, not redirect


class TestCamerasAndStats:
    def test_cameras_endpoint(self, authed_client: TestClient):
        resp = authed_client.get("/api/cameras")
        assert resp.status_code == 200
        data = resp.json()
        assert "cameras" in data
        assert isinstance(data["cameras"], list)

    def test_stats_endpoint(self, authed_client: TestClient):
        resp = authed_client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "events_total" in data
        assert "events_24h" in data
        assert "disk_bytes" in data
