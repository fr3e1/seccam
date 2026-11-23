"""Tests for agent/seccam_agent.py (imported by path; no network beyond 127.0.0.1)."""
import asyncio
import importlib.util
import json
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import websockets
from websockets.asyncio.server import serve

_PATH = Path(__file__).resolve().parent.parent / "agent" / "seccam_agent.py"
_spec = importlib.util.spec_from_file_location("seccam_agent", _PATH)
agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent)

JPEG_A = b"\xff\xd8" + b"A" * 20 + b"\xff\xd9"
JPEG_B = b"\xff\xd8" + b"B" * 10 + b"\xff\xd9"


# ---- JpegSplitter ----
def test_splitter_whole_frames():
    assert agent.JpegSplitter().feed(JPEG_A + JPEG_B) == [JPEG_A, JPEG_B]


def test_splitter_chunk_boundaries():
    s = agent.JpegSplitter()
    data = JPEG_A + JPEG_B
    out = []
    for i in range(len(data)):  # one byte at a time, splits every marker
        out += s.feed(data[i:i + 1])
    assert out == [JPEG_A, JPEG_B]


def test_splitter_garbage_before_and_between():
    s = agent.JpegSplitter()
    assert s.feed(b"junk" + JPEG_A + b"\x00\x01zz" + JPEG_B[:5]) == [JPEG_A]
    assert s.feed(JPEG_B[5:]) == [JPEG_B]


def test_splitter_keeps_partial_tail():
    s = agent.JpegSplitter()
    assert s.feed(JPEG_A[:10]) == []
    assert s.feed(JPEG_A[10:]) == [JPEG_A]


def test_splitter_overflow_resets():
    s = agent.JpegSplitter()
    assert s.feed(b"\xff\xd8" + b"x" * (agent.MAX_BUFFER + 10)) == []
    assert s.feed(JPEG_A) == [JPEG_A]


# ---- pure helpers ----
def test_ws_url():
    assert agent.ws_url("https://h:8443") == "wss://h:8443/ws/camera"
    assert agent.ws_url("https://h:8443/") == "wss://h:8443/ws/camera"
    assert agent.ws_url("http://h") == "ws://h/ws/camera"
    for bad in ("ftp://h", "h:8443", "", "https://"):
        with pytest.raises(ValueError):
            agent.ws_url(bad)


def test_build_ffmpeg_cmd():
    assert agent.build_ffmpeg_cmd("ffmpeg", "/dev/video0", 1280, 720, 5, "mjpeg", 5) == [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2", "-input_format", "mjpeg",
        "-video_size", "1280x720", "-framerate", "5", "-i", "/dev/video0",
        "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "5", "-"]


def test_backoff_delay_bounds():
    assert agent.backoff_delay(0, rand=lambda: 0.0) == pytest.approx(0.5 * 0.7)
    assert agent.backoff_delay(0, rand=lambda: 1.0) == pytest.approx(0.5)
    assert agent.backoff_delay(3, rand=lambda: 1.0) == pytest.approx(4.0)
    assert agent.backoff_delay(50, rand=lambda: 1.0) == pytest.approx(30.0)
    for attempt in range(0, 12):
        d = agent.backoff_delay(attempt)
        cap = min(30.0, 0.5 * 2 ** attempt)
        assert 0.7 * cap - 1e-9 <= d <= cap + 1e-9


def test_send_interval():
    assert agent.send_interval({"preview_fps": 2, "detect_fps": 4}, 10) == pytest.approx(0.25)
    assert agent.send_interval({"preview_fps": 8, "detect_fps": 1}, 5) == pytest.approx(0.2)
    assert agent.send_interval({}, 5) == pytest.approx(0.5)


def test_hello_message():
    assert agent.hello_message("cam", "Garage", "2.0.0") == {
        "type": "hello", "camera_id": "cam", "name": "Garage", "kind": "agent", "version": "2.0.0"}


def test_default_camera_id(monkeypatch):
    monkeypatch.setattr(agent.socket, "gethostname", lambda: "my host.local" + "x" * 100)
    cid = agent.default_camera_id()
    assert cid.startswith("agent-my-host-local") and len(cid) <= 64
    assert agent.re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cid)


# ---- run_session ----
class FakeFrames:
    def __init__(self):
        self.seq = 1

    def latest(self):
        return (self.seq, JPEG_A)


def _session(handler_close_code, token="scd_abc.secret"):
    seen = {"auth": None, "texts": [], "binary": []}

    async def handler(ws):
        seen["auth"] = ws.request.headers.get("Authorization")
        await ws.send(json.dumps({"type": "config", "camera_id": "x", "settings": {"preview_fps": 10, "detect_fps": 10}}))
        while len(seen["binary"]) < 1 or len(seen["texts"]) < 2:
            msg = await asyncio.wait_for(ws.recv(), 5)
            (seen["binary"] if isinstance(msg, bytes) else seen["texts"]).append(msg)
        await ws.close(handler_close_code)

    async def go():
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            return await asyncio.wait_for(
                agent.run_session(f"ws://127.0.0.1:{port}/ws/camera", token, FakeFrames(), "Garage", 5, None), 10)

    return asyncio.run(go()), seen


def test_run_session_returns_4401():
    code, seen = _session(4401)
    assert code == 4401
    assert seen["auth"] == "Bearer scd_abc.secret"
    hello = json.loads(seen["texts"][0])
    assert hello["type"] == "hello" and hello["kind"] == "agent" and hello["name"] == "Garage"
    assert json.loads(seen["texts"][1]) == {"type": "mode", "mode": "server"}
    assert seen["binary"][0] == JPEG_A


def test_run_session_other_close_code():
    code, _ = _session(1001)
    assert code == 1001


def test_run_session_connection_refused():
    assert asyncio.run(agent.run_session("ws://127.0.0.1:1/ws/camera", "t", FakeFrames(), "n", 5, None)) == 1006


# ---- pair ----
class _Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.last = body
        if body["code"] == "AAAA-BBBB":
            status, out = 201, {"device_id": "0123456789ab", "name": body["name"], "kind": "agent", "token": "scd_x.y"}
        else:
            status, out = 401, {"detail": "Invalid or expired pairing code", "code": "bad_pairing_code"}
        data = json.dumps(out).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def stub():
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_pair_writes_token_0600(stub, tmp_path):
    srv, url = stub
    tf = tmp_path / "sub" / "token"
    rc = agent.main(["pair", "--server", url, "--code", "AAAA-BBBB", "--name", "Cam", "--token-file", str(tf)])
    assert rc == 0
    assert tf.read_text().strip() == "scd_x.y"
    assert stat.S_IMODE(tf.stat().st_mode) == 0o600
    assert srv.last == {"code": "AAAA-BBBB", "name": "Cam", "kind": "agent"}


def test_pair_rejected_shows_detail(stub, tmp_path, capsys):
    _, url = stub
    tf = tmp_path / "token"
    rc = agent.main(["pair", "--server", url, "--code", "ZZZZ-ZZZZ", "--name", "Cam", "--token-file", str(tf)])
    assert rc == 1
    assert "Invalid or expired pairing code" in capsys.readouterr().err
    assert not tf.exists()


def test_pair_server_unreachable(tmp_path):
    assert agent.pair("http://127.0.0.1:1", "AAAA-BBBB", "c", str(tmp_path / "t"), None, False) == 1


# ---- CLI ----
def test_main_usage_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("SECCAM_AGENT_SERVER", raising=False)
    assert agent.main(["run"]) == 2
    assert agent.main(["run", "--server", "ftp://x"]) == 2
    assert agent.main(["run", "--server", "http://x", "--token-file", str(tmp_path / "missing")]) == 2
    assert agent.main(["bogus"]) == 2


def test_run_ffmpeg_missing_exits_4(tmp_path):
    tf = tmp_path / "token"
    tf.write_text("scd_a.b\n")
    rc = agent.main(["run", "--server", "http://127.0.0.1:1", "--token-file", str(tf),
                     "--ffmpeg", str(tmp_path / "no-such-ffmpeg")])
    assert rc == 4
