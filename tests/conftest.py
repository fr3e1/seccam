import os
import tempfile
from pathlib import Path
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from server.config import load_config, Config
from server.app import create_app
from server.auth import make_token, derive_token_secret


@pytest.fixture(scope="session")
def tmp_data_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("data")


@pytest.fixture(scope="session")
def tmp_model_path(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("models") / "none.onnx"


@pytest.fixture
def cfg(tmp_data_dir: Path, tmp_model_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("SECCAM_DATA_DIR", str(tmp_data_dir))
    monkeypatch.setenv("SECCAM_PIN", "1234")
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
    return load_config()


@pytest.fixture
def client(cfg: Config) -> Generator[TestClient, None, None]:
    app = create_app(cfg)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def authed_client(client: TestClient, cfg: Config) -> TestClient:
    # Login and get cookie
    resp = client.post("/api/login", json={"pin": "1234"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # Cookie is already set in client.cookies
    return client


@pytest.fixture
def make_jpeg():
    def _make_jpeg(w: int = 640, h: int = 480, color: tuple = (128, 128, 128)) -> bytes:
        img = Image.new("RGB", (w, h), color)
        from io import BytesIO
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return buf.getvalue()
    return _make_jpeg


@pytest.fixture
def auth_cookie(cfg: Config) -> str:
    """Create a valid auth cookie value for websocket testing using derived token secret."""
    # In test config, SECCAM_PIN=1234 is set and no DB auth exists yet
    pin_component = cfg.pin
    token_secret = derive_token_secret(cfg.secret, pin_component)
    return make_token(token_secret, ttl_s=30 * 86400)


@pytest.fixture
def setup_data_dir(tmp_path_factory) -> Path:
    """Fresh data directory for setup mode tests (no SECCAM_PIN in env)."""
    return tmp_path_factory.mktemp("setup_data")


@pytest.fixture
def setup_cfg(setup_data_dir: Path, tmp_model_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """Config with SECCAM_PIN removed from environment."""
    monkeypatch.setenv("SECCAM_DATA_DIR", str(setup_data_dir))
    monkeypatch.delenv("SECCAM_PIN", raising=False)
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
    return load_config()


@pytest.fixture
def setup_client(setup_cfg: Config) -> Generator[TestClient, None, None]:
    """Test client in setup mode (no PIN configured)."""
    app = create_app(setup_cfg)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
