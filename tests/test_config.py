from pathlib import Path

import pytest

from server.config import load_config
from server.version import __version__

REPO = Path(__file__).resolve().parent.parent


def test_defaults(cfg, data_dir):
    assert cfg.secret == "test-secret-key"
    assert cfg.data_dir == data_dir.resolve()
    assert cfg.retention_days == 14
    assert cfg.host == "0.0.0.0"
    assert (cfg.https_port, cfg.http_port) == (8443, 8080)
    assert (cfg.cert, cfg.key, cfg.ca_cert) == ("", "", "")
    assert cfg.static_dir == (REPO / "static").resolve()
    assert cfg.legacy_pin == ""
    assert cfg.trusted_proxies == ()
    assert cfg.public_origin == ""
    assert cfg.cookie_secure == "auto"
    assert cfg.update_mode == "off"
    assert cfg.update_repo == "fr3e1/seccam"
    assert cfg.session_ttl_days == 30


def test_default_model_path(env, monkeypatch):
    monkeypatch.delenv("SECCAM_MODEL_PATH")
    assert load_config().model_path == REPO / "models" / "yolox_nano.onnx"


def test_all_vars(env, monkeypatch):
    for k, v in {
        "SECCAM_RETENTION_DAYS": " 7 ", "SECCAM_HOST": "127.0.0.1", "SECCAM_HTTPS_PORT": "9443",
        "SECCAM_HTTP_PORT": "9080", "SECCAM_CERT": "/c.pem", "SECCAM_KEY": "/k.pem", "SECCAM_CA_CERT": "/ca.pem",
        "SECCAM_PIN": "1234", "SECCAM_TRUSTED_PROXIES": "10.0.0.1, 192.168.0.0/16 ,::1",
        "SECCAM_PUBLIC_ORIGIN": "https://cam.example.com:8443/", "SECCAM_COOKIE_SECURE": "true",
        "SECCAM_UPDATE_MODE": "systemd", "SECCAM_UPDATE_REPO": "me/my-repo.x", "SECCAM_SESSION_TTL_DAYS": "90",
    }.items():
        monkeypatch.setenv(k, v)
    c = load_config()
    assert c.retention_days == 7 and c.host == "127.0.0.1"
    assert (c.https_port, c.http_port) == (9443, 9080)
    assert (c.cert, c.key, c.ca_cert) == ("/c.pem", "/k.pem", "/ca.pem")
    assert c.legacy_pin == "1234"
    assert c.trusted_proxies == ("10.0.0.1/32", "192.168.0.0/16", "::1/128")
    assert c.public_origin == "https://cam.example.com:8443"
    assert c.cookie_secure == "true"
    assert c.update_mode == "systemd"
    assert c.update_repo == "me/my-repo.x"
    assert c.session_ttl_days == 90


def test_empty_means_default(env, monkeypatch):
    monkeypatch.setenv("SECCAM_RETENTION_DAYS", "  ")
    monkeypatch.setenv("SECCAM_COOKIE_SECURE", "")
    c = load_config()
    assert c.retention_days == 14 and c.cookie_secure == "auto"


def test_data_dir_created_and_secret_persisted(env, monkeypatch, tmp_path):
    d = tmp_path / "a" / "b"
    monkeypatch.setenv("SECCAM_DATA_DIR", str(d))
    monkeypatch.delenv("SECCAM_SECRET")
    first = load_config()
    assert d.is_dir()
    assert (d / "secret").stat().st_mode & 0o777 == 0o600
    assert load_config().secret == first.secret and len(first.secret) == 64


@pytest.mark.parametrize("name,value", [
    ("SECCAM_RETENTION_DAYS", "abc"), ("SECCAM_RETENTION_DAYS", "0"), ("SECCAM_RETENTION_DAYS", "366"),
    ("SECCAM_HTTPS_PORT", "0"), ("SECCAM_HTTP_PORT", "70000"), ("SECCAM_HTTP_PORT", "x"),
    ("SECCAM_TRUSTED_PROXIES", "not-an-ip"), ("SECCAM_TRUSTED_PROXIES", "10.0.0.1,bogus"),
    ("SECCAM_PUBLIC_ORIGIN", "ftp://x.com"), ("SECCAM_PUBLIC_ORIGIN", "cam.example.com"),
    ("SECCAM_PUBLIC_ORIGIN", "https://a.com/path"),
    ("SECCAM_COOKIE_SECURE", "yes"), ("SECCAM_UPDATE_MODE", "on"),
    ("SECCAM_UPDATE_REPO", "noslash"), ("SECCAM_UPDATE_REPO", "a/b/c"), ("SECCAM_UPDATE_REPO", "a b/c"),
    ("SECCAM_SESSION_TTL_DAYS", "0"), ("SECCAM_SESSION_TTL_DAYS", "366"),
])
def test_invalid_values(env, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=f"^{name}: "):
        load_config()


def test_version():
    assert __version__ == (REPO / "VERSION").read_text().strip() == "2.0.0"
