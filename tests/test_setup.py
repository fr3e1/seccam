import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from pathlib import Path

from server.auth import hash_pin, verify_pin_hash, PIN_RE


class TestSetupMode:
    """Tests for first-run setup mode when no PIN is configured."""

    def test_setup_status_public(self, setup_client: TestClient):
        resp = setup_client.get("/api/setup/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["setup_required"] is True
        assert data["pin_source"] == "none"
        assert data["version"] == "1.1.0"

    def test_root_redirects_to_welcome(self, setup_client: TestClient):
        resp = setup_client.get("/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/welcome"

    def test_camera_redirects_to_welcome(self, setup_client: TestClient):
        resp = setup_client.get("/camera", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/welcome"

    def test_login_redirects_to_welcome(self, setup_client: TestClient):
        resp = setup_client.get("/login", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/welcome"

    def test_welcome_page_accessible(self, setup_client: TestClient):
        resp = setup_client.get("/welcome")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_api_returns_403_in_setup_mode(self, setup_client: TestClient):
        for path in ["/api/settings", "/api/events", "/api/cameras", "/api/stats", "/api/events/1"]:
            resp = setup_client.get(path)
            assert resp.status_code == 403, f"Path {path} should return 403"
            assert resp.json() == {"detail": "Setup required"}

    def test_ws_camera_closes_4403_in_setup_mode(self, setup_client: TestClient):
        with pytest.raises(WebSocketDisconnect) as exc:
            with setup_client.websocket_connect("/ws/camera") as ws:
                ws.receive_text()
        assert exc.value.code == 4403

    def test_ws_viewer_closes_4403_in_setup_mode(self, setup_client: TestClient):
        with pytest.raises(WebSocketDisconnect) as exc:
            with setup_client.websocket_connect("/ws/viewer") as ws:
                ws.receive_text()
        assert exc.value.code == 4403

    def test_health_includes_setup_required(self, setup_client: TestClient):
        resp = setup_client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["setup_required"] is True


class TestSetupAPI:
    """Tests for POST /api/setup endpoint."""

    def test_setup_invalid_pin_non_digits(self, setup_client: TestClient):
        resp = setup_client.post("/api/setup", json={
            "pin": "abcd",
            "pin_confirm": "abcd",
            "retention_days": 14,
            "mode": "auto"
        })
        assert resp.status_code == 422
        assert "detail" in resp.json()

    def test_setup_invalid_pin_too_short(self, setup_client: TestClient):
        resp = setup_client.post("/api/setup", json={
            "pin": "123",
            "pin_confirm": "123",
            "retention_days": 14,
            "mode": "auto"
        })
        assert resp.status_code == 422

    def test_setup_invalid_pin_too_long(self, setup_client: TestClient):
        resp = setup_client.post("/api/setup", json={
            "pin": "1234567890123",
            "pin_confirm": "1234567890123",
            "retention_days": 14,
            "mode": "auto"
        })
        assert resp.status_code == 422

    def test_setup_pin_mismatch(self, setup_client: TestClient):
        resp = setup_client.post("/api/setup", json={
            "pin": "1234",
            "pin_confirm": "5678",
            "retention_days": 14,
            "mode": "auto"
        })
        assert resp.status_code == 422
        assert "detail" in resp.json()

    def test_setup_invalid_retention_days(self, setup_client: TestClient):
        resp = setup_client.post("/api/setup", json={
            "pin": "1234",
            "pin_confirm": "1234",
            "retention_days": 0,
            "mode": "auto"
        })
        assert resp.status_code == 422

    def test_setup_invalid_mode(self, setup_client: TestClient):
        resp = setup_client.post("/api/setup", json={
            "pin": "1234",
            "pin_confirm": "1234",
            "retention_days": 14,
            "mode": "invalid"
        })
        assert resp.status_code == 422

    def test_setup_success_sets_cookie_and_applies_settings(self, setup_client: TestClient):
        resp = setup_client.post("/api/setup", json={
            "pin": "1234",
            "pin_confirm": "1234",
            "retention_days": 30,
            "mode": "local",
            "labels": ["person", "car"]
        })
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert "seccam_session" in setup_client.cookies

        # Verify settings were applied
        resp = setup_client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["retention_days"] == 30
        assert data["mode"] == "local"
        assert data["labels"] == ["person", "car"]

    def test_setup_second_attempt_returns_409(self, setup_client: TestClient):
        # First setup
        resp = setup_client.post("/api/setup", json={
            "pin": "1234",
            "pin_confirm": "1234",
            "retention_days": 14,
            "mode": "auto"
        })
        assert resp.status_code == 200

        # Second setup should fail
        resp = setup_client.post("/api/setup", json={
            "pin": "5678",
            "pin_confirm": "5678",
            "retention_days": 14,
            "mode": "auto"
        })
        assert resp.status_code == 409
        assert resp.json() == {"detail": "Already set up"}

    def test_welcome_redirects_to_root_after_setup(self, setup_client: TestClient):
        # Complete setup
        setup_client.post("/api/setup", json={
            "pin": "1234",
            "pin_confirm": "1234",
            "retention_days": 14,
            "mode": "auto"
        })

        # Welcome should now redirect to root
        resp = setup_client.get("/welcome", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

    def test_login_works_after_setup(self, setup_client: TestClient):
        # Complete setup
        setup_client.post("/api/setup", json={
            "pin": "1234",
            "pin_confirm": "1234",
            "retention_days": 14,
            "mode": "auto"
        })

        # Login with new PIN should work
        resp = setup_client.post("/api/login", json={"pin": "1234"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_env_pin_irrelevant_after_db_pin_set(self, setup_client: TestClient, monkeypatch: pytest.MonkeyPatch):
        # Set env PIN after setup (simulating legacy env PIN)
        monkeypatch.setenv("SECCAM_PIN", "9999")

        # Complete setup with different PIN
        setup_client.post("/api/setup", json={
            "pin": "1234",
            "pin_confirm": "1234",
            "retention_days": 14,
            "mode": "auto"
        })

        # Login with DB PIN works
        resp = setup_client.post("/api/login", json={"pin": "1234"})
        assert resp.status_code == 200

        # Login with env PIN fails
        resp = setup_client.post("/api/login", json={"pin": "9999"})
        assert resp.status_code == 401


class TestPinChange:
    """Tests for POST /api/pin endpoint."""

    def test_pin_change_wrong_current(self, setup_client: TestClient):
        # Setup first
        setup_client.post("/api/setup", json={
            "pin": "1234",
            "pin_confirm": "1234",
            "retention_days": 14,
            "mode": "auto"
        })

        # Try to change with wrong current PIN
        resp = setup_client.post("/api/pin", json={
            "current_pin": "wrong",
            "new_pin": "5678",
            "new_pin_confirm": "5678"
        })
        assert resp.status_code == 401
        # Detail text is free per spec; only assert status

    def test_pin_change_mismatch(self, setup_client: TestClient):
        setup_client.post("/api/setup", json={
            "pin": "1234",
            "pin_confirm": "1234",
            "retention_days": 14,
            "mode": "auto"
        })

        resp = setup_client.post("/api/pin", json={
            "current_pin": "1234",
            "new_pin": "5678",
            "new_pin_confirm": "9999"
        })
        assert resp.status_code == 422
        assert "detail" in resp.json()

    def test_pin_change_invalid_new_pin(self, setup_client: TestClient):
        setup_client.post("/api/setup", json={
            "pin": "1234",
            "pin_confirm": "1234",
            "retention_days": 14,
            "mode": "auto"
        })

        resp = setup_client.post("/api/pin", json={
            "current_pin": "1234",
            "new_pin": "abc",
            "new_pin_confirm": "abc"
        })
        assert resp.status_code == 422

    def test_pin_change_success_invalidates_other_sessions(self, setup_client: TestClient, setup_cfg):
        # Setup first
        setup_client.post("/api/setup", json={
            "pin": "1234",
            "pin_confirm": "1234",
            "retention_days": 14,
            "mode": "auto"
        })

        # Create a second client with the same session (simulating another browser)
        client2 = TestClient(setup_client.app, raise_server_exceptions=False)
        client2.cookies = dict(setup_client.cookies)  # another browser holding the same (old) session

        # Both clients should be authenticated
        resp1 = setup_client.get("/api/settings")
        assert resp1.status_code == 200
        resp2 = client2.get("/api/settings")
        assert resp2.status_code == 200

        # Change PIN from first client
        resp = setup_client.post("/api/pin", json={
            "current_pin": "1234",
            "new_pin": "5678",
            "new_pin_confirm": "5678"
        })
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        # First client gets new cookie and stays authenticated
        resp1 = setup_client.get("/api/settings")
        assert resp1.status_code == 200

        # Second client's old cookie should now be invalid
        resp2 = client2.get("/api/settings")
        assert resp2.status_code == 401


class TestDBPinOverridesEnvPin:
    """Tests that DB-stored PIN hash takes precedence over env PIN."""

    def test_db_pin_overrides_env(self, tmp_path_factory, tmp_model_path, monkeypatch: pytest.MonkeyPatch):
        # Create data dir with existing DB that has a PIN hash
        data_dir = tmp_path_factory.mktemp("db_pin_data")
        
        # First, create a client with env PIN only
        monkeypatch.setenv("SECCAM_DATA_DIR", str(data_dir))
        monkeypatch.setenv("SECCAM_PIN", "envpin")
        monkeypatch.setenv("SECCAM_SECRET", "test-secret-key")
        monkeypatch.setenv("SECCAM_MODEL_PATH", str(tmp_model_path))
        monkeypatch.setenv("SECCAM_RETENTION_DAYS", "14")
        monkeypatch.setenv("SECCAM_HOST", "0.0.0.0")
        monkeypatch.setenv("SECCAM_HTTPS_PORT", "8443")
        monkeypatch.setenv("SECCAM_HTTP_PORT", "8080")
        monkeypatch.setenv("SECCAM_CERT", "")
        monkeypatch.setenv("SECCAM_KEY", "")
        monkeypatch.setenv("SECCAM_CA_CERT", "")
        monkeypatch.setenv("SECCAM_STATIC_DIR", str(Path(__file__).parent.parent / "static"))
        
        from server.config import load_config
        from server.app import create_app
        
        cfg = load_config()
        app = create_app(cfg)
        client = TestClient(app, raise_server_exceptions=False)
        
        # Login with env PIN should work initially
        resp = client.post("/api/login", json={"pin": "envpin"})
        assert resp.status_code == 200
        
        # Store a DB PIN (as /api/pin would) - it must take precedence over the env PIN
        from server.auth import hash_pin
        app.state.db.set_auth(hash_pin("4321"))
        if hasattr(app.state, "invalidate_auth_cache"):
            app.state.invalidate_auth_cache()
        
        # Now DB PIN should work
        client2 = TestClient(app, raise_server_exceptions=False)
        resp = client2.post("/api/login", json={"pin": "4321"})
        assert resp.status_code == 200
        
        # Env PIN should no longer work
        client3 = TestClient(app, raise_server_exceptions=False)
        resp = client3.post("/api/login", json={"pin": "envpin"})
        assert resp.status_code == 401


class TestHashPinRoundtrip:
    """Tests for hash_pin and verify_pin_hash functions."""

    def test_hash_pin_returns_dict_with_hex_strings(self):
        result = hash_pin("1234")
        assert "pin_hash" in result
        assert "salt" in result
        assert "iterations" in result
        assert isinstance(result["pin_hash"], str)
        assert isinstance(result["salt"], str)
        assert isinstance(result["iterations"], int)
        assert result["iterations"] == 200_000
        # Verify hex strings
        bytes.fromhex(result["pin_hash"])
        bytes.fromhex(result["salt"])

    def test_hash_pin_deterministic_with_given_salt(self):
        salt = bytes.fromhex("00" * 16)
        result1 = hash_pin("1234", salt=salt)
        result2 = hash_pin("1234", salt=salt)
        assert result1["pin_hash"] == result2["pin_hash"]
        assert result1["salt"] == result2["salt"]

    def test_hash_pin_different_salt_produces_different_hash(self):
        result1 = hash_pin("1234")
        result2 = hash_pin("1234")
        assert result1["salt"] != result2["salt"]
        assert result1["pin_hash"] != result2["pin_hash"]

    def test_verify_pin_hash_correct(self):
        rec = hash_pin("1234")
        assert verify_pin_hash("1234", rec) is True

    def test_verify_pin_hash_incorrect(self):
        rec = hash_pin("1234")
        assert verify_pin_hash("wrong", rec) is False

    def test_verify_pin_hash_constant_time(self):
        # Just verify it doesn't crash and returns False for wrong PIN
        rec = hash_pin("1234")
        for _ in range(10):
            assert verify_pin_hash("wrong", rec) is False
        for _ in range(10):
            assert verify_pin_hash("1234", rec) is True

    def test_pin_regex(self):
        assert PIN_RE.match("1234") is not None
        assert PIN_RE.match("123456789012") is not None
        assert PIN_RE.match("123") is None
        assert PIN_RE.match("1234567890123") is None
        assert PIN_RE.match("abcd") is None
        assert PIN_RE.match("12a4") is None
        assert PIN_RE.match("") is None


class TestSetupRateLimit:
    """Tests that setup endpoint uses LoginLimiter only for wrong current PIN (login), not setup PIN mismatches."""

    def test_login_rate_limited_after_failures(self, setup_client: TestClient):
        # First do setup
        setup_client.post("/api/setup", json={
            "pin": "1234",
            "pin_confirm": "1234",
            "retention_days": 14,
            "mode": "auto"
        })

        # Make 5 failed login attempts
        for _ in range(5):
            resp = setup_client.post("/api/login", json={"pin": "wrong"})
            assert resp.status_code == 401

        # 6th attempt should be rate limited (429)
        resp = setup_client.post("/api/login", json={"pin": "wrong"})
        assert resp.status_code == 429
        assert "detail" in resp.json()


class TestSetupWithExistingEnvPin:
    """Tests when SECCAM_PIN is set in env but no DB auth yet."""

    def test_setup_status_shows_env_pin_source(self, tmp_path_factory, tmp_model_path, monkeypatch: pytest.MonkeyPatch):
        data_dir = tmp_path_factory.mktemp("env_pin_data")
        monkeypatch.setenv("SECCAM_DATA_DIR", str(data_dir))
        monkeypatch.setenv("SECCAM_PIN", "envpin123")
        monkeypatch.setenv("SECCAM_SECRET", "test-secret-key")
        monkeypatch.setenv("SECCAM_MODEL_PATH", str(tmp_model_path))
        monkeypatch.setenv("SECCAM_RETENTION_DAYS", "14")
        monkeypatch.setenv("SECCAM_HOST", "0.0.0.0")
        monkeypatch.setenv("SECCAM_HTTPS_PORT", "8443")
        monkeypatch.setenv("SECCAM_HTTP_PORT", "8080")
        monkeypatch.setenv("SECCAM_CERT", "")
        monkeypatch.setenv("SECCAM_KEY", "")
        monkeypatch.setenv("SECCAM_CA_CERT", "")
        monkeypatch.setenv("SECCAM_STATIC_DIR", str(Path(__file__).parent.parent / "static"))
        
        from server.config import load_config
        from server.app import create_app
        
        cfg = load_config()
        app = create_app(cfg)
        client = TestClient(app, raise_server_exceptions=False)
        
        resp = client.get("/api/setup/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["setup_required"] is False
        assert data["pin_source"] == "env"

    def test_login_works_with_env_pin_before_setup(self, tmp_path_factory, tmp_model_path, monkeypatch: pytest.MonkeyPatch):
        data_dir = tmp_path_factory.mktemp("env_pin_login")
        monkeypatch.setenv("SECCAM_DATA_DIR", str(data_dir))
        monkeypatch.setenv("SECCAM_PIN", "envpin123")
        monkeypatch.setenv("SECCAM_SECRET", "test-secret-key")
        monkeypatch.setenv("SECCAM_MODEL_PATH", str(tmp_model_path))
        monkeypatch.setenv("SECCAM_RETENTION_DAYS", "14")
        monkeypatch.setenv("SECCAM_HOST", "0.0.0.0")
        monkeypatch.setenv("SECCAM_HTTPS_PORT", "8443")
        monkeypatch.setenv("SECCAM_HTTP_PORT", "8080")
        monkeypatch.setenv("SECCAM_CERT", "")
        monkeypatch.setenv("SECCAM_KEY", "")
        monkeypatch.setenv("SECCAM_CA_CERT", "")
        monkeypatch.setenv("SECCAM_STATIC_DIR", str(Path(__file__).parent.parent / "static"))
        
        from server.config import load_config
        from server.app import create_app
        
        cfg = load_config()
        app = create_app(cfg)
        client = TestClient(app, raise_server_exceptions=False)
        
        # Login with env PIN should work before setup
        resp = client.post("/api/login", json={"pin": "envpin123"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
