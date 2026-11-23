import pytest
from fastapi.testclient import TestClient

from server import auth
from server.app import create_app
from server.updater import UpdateError
from tests.conftest import assert_shape  # noqa: F401  (re-exported: tests/test_db.py does `from conftest import assert_shape`)

ADMIN_USER = "admin"
ADMIN_PASSWORD = "correct-horse-42"


class FakeHttpGet(dict):
    """url -> bytes; any other URL raises UpdateError. Records every call in .calls."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    def __call__(self, url: str, max_bytes: int) -> bytes:
        self.calls.append(url)
        if url not in self:
            raise UpdateError(f"no fake response for {url}")
        value = self[url]
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def fake_http_get() -> FakeHttpGet:
    return FakeHttpGet()


@pytest.fixture
def app(cfg, fake_http_get):
    return create_app(cfg, http_get=fake_http_get)


@pytest.fixture
def setup_client(app):
    with TestClient(app) as client:
        yield client


def make_admin(app, username: str = ADMIN_USER, password: str = ADMIN_PASSWORD) -> None:
    app.state.db.create_user(username, auth.hash_password(password))


def login(client: TestClient, username: str = ADMIN_USER, password: str = ADMIN_PASSWORD) -> None:
    resp = client.post("/api/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    client.headers["X-CSRF-Token"] = client.cookies.get("seccam_csrf")


@pytest.fixture
def admin_client(app):
    make_admin(app)
    with TestClient(app) as client:
        login(client)
        yield client


@pytest.fixture
def device_token(app) -> str:
    device_id = auth.new_device_id()
    token, token_hash = auth.new_device_token(device_id)
    app.state.db.create_device(device_id, "Garage", "agent", token_hash)
    return token
