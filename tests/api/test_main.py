import logging

from fastapi.testclient import TestClient

from server.app import create_app
from server.config import load_config
from server.main import build_server_configs, create_http_redirect_app


def test_plain_config_without_cert(cfg):
    app = create_app(cfg)
    configs = build_server_configs(cfg, app)
    assert len(configs) == 1
    c = configs[0]
    assert (c.host, c.port) == (cfg.host, cfg.https_port)
    assert c.ssl_certfile is None and c.proxy_headers is False and c.server_header is False
    assert c.ws_max_size == 2 * 1024 * 1024 and c.ws_ping_interval == 20 and c.ws_ping_timeout == 20
    assert c.log_level == "warning" or c.log_level == 30


def test_tls_adds_redirect_config(env, monkeypatch, tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("x")
    key.write_text("x")
    monkeypatch.setenv("SECCAM_CERT", str(cert))
    monkeypatch.setenv("SECCAM_KEY", str(key))
    cfg = load_config()
    configs = build_server_configs(cfg, create_app(cfg))
    assert [c.port for c in configs] == [cfg.https_port, cfg.http_port]
    assert configs[0].ssl_certfile == str(cert) and configs[1].ssl_certfile is None


def test_redirect_app(cfg):
    client = TestClient(create_http_redirect_app(cfg), follow_redirects=False)
    resp = client.get("/some/path?x=1")
    assert resp.status_code == 301 and resp.headers["location"] == f"https://testserver:{cfg.https_port}/some/path?x=1"
    assert client.get("/setup").status_code == 200
    assert client.get("/ca.crt").status_code == 404


def test_lifespan_logs_setup_token_and_plain_http_warning(app, caplog):
    with caplog.at_level(logging.WARNING, logger="seccam"):
        with TestClient(app):
            pass
    text = caplog.text
    assert "SecCam setup required. Setup token: " in text
    assert "serving plain HTTP on a non-loopback address" in text
    token = (app.state.cfg.data_dir / "setup-token").read_text().strip()
    assert token in text


def test_no_warning_for_loopback_host(env, monkeypatch, caplog):
    monkeypatch.setenv("SECCAM_HOST", "127.0.0.1")
    with caplog.at_level(logging.WARNING, logger="seccam"):
        with TestClient(create_app(load_config())):
            pass
    assert "plain HTTP" not in caplog.text


def test_no_setup_token_log_when_configured(app, caplog):
    from tests.api.conftest import make_admin
    make_admin(app)
    with caplog.at_level(logging.WARNING, logger="seccam"):
        with TestClient(app):
            pass
    assert "Setup token" not in caplog.text


def test_lifespan_purges_expired_sessions(app):
    from tests.api.conftest import make_admin
    make_admin(app)
    app.state.db.create_session("a" * 64, 1, "csrf", 1, None, None)
    with TestClient(app):
        pass
    assert app.state.db.get_session("a" * 64) is None


def test_app_state_wiring(app):
    for name in ("cfg", "db", "hub", "detector", "limiter", "user_limiter", "pin_limiter", "ws_limiter",
                 "pairing", "http_get", "scrypt_sem"):
        assert hasattr(app.state, name), name
    assert app.docs_url is None and app.redoc_url is None and app.openapi_url is None
