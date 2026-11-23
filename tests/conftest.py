import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from server.config import Config, load_config
from server.db import DB

REPO = Path(__file__).resolve().parent.parent
SHAPES = json.loads((Path(__file__).parent / "contract_shapes.json").read_text())

# Keys that may be null in addition to the example's own type (§6.3 "|null").
NULLABLE_KEYS = {"snapshot_url", "thumb_url", "device_id", "last_seen_at", "last_ip",
                 "latest", "checked_at", "error", "version", "previous_version", "updated_at"}


def _type_ok(example, value) -> bool:
    if example is None:
        return True
    if isinstance(example, bool):
        return isinstance(value, bool)
    if isinstance(example, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(example, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(example, str):
        return isinstance(value, str)
    if isinstance(example, list):
        return isinstance(value, list)
    if isinstance(example, dict):
        return isinstance(value, dict)
    return False


def _check(path: str, example: dict, obj: dict, depth: int) -> None:
    assert isinstance(obj, dict), f"{path}: expected object, got {type(obj).__name__}"
    assert set(obj) == set(example), f"{path}: keys {sorted(obj)} != {sorted(example)}"
    for key, ex in example.items():
        val = obj[key]
        if val is None:
            assert ex is None or key in NULLABLE_KEYS, f"{path}.{key}: unexpected null"
            continue
        assert _type_ok(ex, val), f"{path}.{key}: {type(val).__name__} does not match {type(ex).__name__}"
        if depth < 1:
            if isinstance(ex, dict):
                _check(f"{path}.{key}", ex, val, depth + 1)
            elif isinstance(ex, list) and ex and isinstance(ex[0], dict):
                for i, item in enumerate(val):
                    _check(f"{path}.{key}[{i}]", ex[0], item, depth + 1)


def assert_shape(name: str, obj: dict) -> None:
    """Assert obj has exactly the key set and value types of SHAPES[name] (nested one level)."""
    _check(name, SHAPES[name], obj, 0)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def model_path(tmp_path: Path) -> Path:
    return tmp_path / "models" / "none.onnx"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, data_dir: Path, model_path: Path) -> None:
    monkeypatch.setenv("SECCAM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SECCAM_SECRET", "test-secret-key")
    monkeypatch.setenv("SECCAM_MODEL_PATH", str(model_path))
    monkeypatch.setenv("SECCAM_STATIC_DIR", str(REPO / "static"))
    monkeypatch.setenv("SECCAM_UPDATE_MODE", "off")
    for name in ("SECCAM_PIN", "SECCAM_TRUSTED_PROXIES", "SECCAM_PUBLIC_ORIGIN", "SECCAM_COOKIE_SECURE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def cfg(env) -> Config:
    return load_config()


@pytest.fixture
def db(cfg: Config) -> DB:
    return DB(cfg.data_dir)


@pytest.fixture
def make_jpeg():
    def _make_jpeg(w: int = 640, h: int = 480, color: tuple = (128, 128, 128)) -> bytes:
        buf = BytesIO()
        Image.new("RGB", (w, h), color).save(buf, format="JPEG", quality=80)
        return buf.getvalue()
    return _make_jpeg
