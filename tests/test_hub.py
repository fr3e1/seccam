import asyncio
import io
import json
import math
import time

import pytest
from PIL import Image

from server.db import DB
from server.hub import Hub, jpeg_dimensions, decode_viewer_frame, encode_viewer_frame, sanitize_detections

def _real_jpeg(w=32, h=24):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


JPEG = _real_jpeg()
PERSON = {"label": "person", "score": 0.9, "bbox": [1.0, 2.0, 3.0, 4.0]}


class Sink:
    """Fake viewer/camera connection recording what it receives."""

    def __init__(self):
        self.json = []
        self.bytes = []
        self.closed = []

    async def send_json(self, m):
        self.json.append(m)

    async def send_bytes(self, b):
        self.bytes.append(b)

    async def close(self, code):
        self.closed.append(code)

    def types(self):
        return [m["type"] for m in self.json]


class StubDetector:
    available = True

    def __init__(self, delay=0.0):
        self.delay = delay
        self.calls = []

    def detect(self, jpeg):
        self.calls.append(jpeg)
        if self.delay:
            time.sleep(self.delay)
        return [dict(PERSON)], 640, 480, 5.0


@pytest.fixture
def make_hub(db: DB):
    def _make(detector=None):
        return Hub(db, detector or StubDetector())
    return _make


async def wait_for(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.005)
    return pred()


def run(coro):
    return asyncio.run(coro)


class TestFrameCodec:
    def test_round_trip(self):
        header = {"camera_id": "c1", "ts": 123, "detections": [PERSON], "frame_w": 640, "frame_h": 480}
        data = encode_viewer_frame(header, JPEG)
        assert data[:4] == b"SC\x02\x01"
        assert int.from_bytes(data[4:8], "big") == len(data) - 8 - len(JPEG)
        h, j = decode_viewer_frame(data)
        assert h == header and j == JPEG

    def test_empty_jpeg(self):
        h, j = decode_viewer_frame(encode_viewer_frame({"a": 1}, b""))
        assert h == {"a": 1} and j == b""

    @pytest.mark.parametrize("bad", [
        b"", b"SC", b"XX\x02\x01\x00\x00\x00\x00", b"SC\x01\x01\x00\x00\x00\x00",
        b"SC\x02\x02\x00\x00\x00\x00", b"SC\x02\x01\x00\x00\x00\x10{}",
        b"SC\x02\x01\x00\x00\x00\x02{{", b"SC\x02\x01\x00\x00\x00\x02[]",
        b"SC\x02\x01\x00\x00\x00\x02\xff\xfe",
    ])
    def test_bad_inputs(self, bad):
        with pytest.raises(ValueError):
            decode_viewer_frame(bad)


class TestSanitize:
    def test_valid(self):
        d, w, h, ms = sanitize_detections({"detections": [PERSON], "frame_w": 640, "frame_h": 480, "infer_ms": 12})
        assert d == [PERSON] and (w, h, ms) == (640, 480, 12.0)

    @pytest.mark.parametrize("msg", [None, [], "x", 5, {}, {"detections": "x"}, {"detections": None}])
    def test_never_raises(self, msg):
        assert sanitize_detections(msg) == ([], 0, 0, None)

    def test_filters_bad_items(self):
        bad = [
            "str", {"label": 1, "score": 0.5, "bbox": [0, 0, 1, 1]},
            {"label": "x" * 33, "score": 0.5, "bbox": [0, 0, 1, 1]},
            {"label": "a", "score": 1.5, "bbox": [0, 0, 1, 1]},
            {"label": "a", "score": True, "bbox": [0, 0, 1, 1]},
            {"label": "a", "score": 0.5, "bbox": [0, 0, 1]},
            {"label": "a", "score": 0.5, "bbox": [0, 0, 1, math.inf]},
            {"label": "a", "score": 0.5, "bbox": [0, 0, 1, math.nan]},
            {"label": "a", "score": 0.5, "bbox": [0, 0, 1, "1"]},
            {"label": "a", "score": 0.5},
        ]
        d, _, _, _ = sanitize_detections({"detections": bad + [PERSON]})
        assert d == [PERSON]

    def test_cap_50(self):
        d, _, _, _ = sanitize_detections({"detections": [PERSON] * 80})
        assert len(d) == 50

    def test_dims_and_infer(self):
        assert sanitize_detections({"frame_w": 10001, "frame_h": -1, "infer_ms": "x"}) == ([], 0, 0, None)
        assert sanitize_detections({"frame_w": 1.5, "frame_h": True, "infer_ms": math.inf}) == ([], 0, 0, None)
        assert sanitize_detections({"frame_w": 10000, "frame_h": 0, "infer_ms": 0})[1:] == (10000, 0, 0.0)


class TestCameras:
    def test_connect_shape_config_and_list(self, make_hub):
        async def go():
            hub, cam = make_hub(), Sink()
            await hub.camera_connected("c1", "Cam", cam.send_json, conn_token="t", device_id="d1", kind="agent")
            assert cam.json[0]["type"] == "config" and cam.json[0]["camera_id"] == "c1"
            (c,) = hub.cameras()
            assert c["camera_id"] == "c1" and c["name"] == "Cam" and c["mode"] == "starting"
            assert c["kind"] == "agent" and c["device_id"] == "d1" and c["connected"] is True
            assert set(c) == {"camera_id", "name", "mode", "kind", "device_id", "connected", "last_seen", "fps", "infer_ms"}
            await hub.shutdown()
        run(go())

    def test_replacement_closes_old_4409_and_stale_disconnect_ignored(self, make_hub):
        async def go():
            hub, old, new = make_hub(), Sink(), Sink()
            await hub.camera_connected("c1", "A", old.send_json, conn_token="old", close=old.close)
            await hub.camera_connected("c1", "B", new.send_json, conn_token="new", close=new.close)
            assert old.closed == [4409] and new.closed == []
            await hub.camera_disconnected("c1", conn_token="old")
            assert [c["name"] for c in hub.cameras()] == ["B"]
            await hub.camera_disconnected("c1", conn_token="new")
            assert hub.cameras() == []
            await hub.shutdown()
        run(go())

    def test_close_may_raise(self, make_hub):
        async def go():
            hub, cam = make_hub(), Sink()

            async def boom(code):
                raise RuntimeError("x")
            await hub.camera_connected("c1", "A", cam.send_json, conn_token=1, close=boom)
            await hub.camera_connected("c1", "B", cam.send_json, conn_token=2)
            assert hub.cameras()[0]["name"] == "B"
            await hub.shutdown()
        run(go())

    def test_mode_change_and_broadcast(self, make_hub):
        async def go():
            hub, cam, v = make_hub(), Sink(), Sink()
            await hub.camera_connected("c1", "A", cam.send_json)
            await hub.viewer_connected("v1", v.send_json)
            await hub.camera_mode("c1", "local")
            await hub.camera_mode("c1", "bogus")
            await hub.flush()
            assert v.types()[:2] == ["cameras", "settings"]
            assert v.json[-1]["cameras"][0]["mode"] == "local"
            await hub.shutdown()
        run(go())

    def test_settings_changed_pushes_config(self, make_hub):
        async def go():
            hub, cam, v = make_hub(), Sink(), Sink()
            await hub.camera_connected("c1", "A", cam.send_json)
            await hub.viewer_connected("v1", v.send_json)
            await hub.settings_changed({"min_score": 0.7})
            await hub.flush()
            assert cam.json[-1] == {"type": "config", "camera_id": "c1", "settings": {"min_score": 0.7}}
            assert v.json[-1] == {"type": "settings", "settings": {"min_score": 0.7}}
            await hub.shutdown()
        run(go())


class TestFrames:
    def test_non_jpeg_and_oversize_dropped(self, make_hub):
        async def go():
            hub, cam, v = make_hub(), Sink(), Sink()
            await hub.camera_connected("c1", "A", cam.send_json)
            await hub.viewer_connected("v1", v.send_json, v.send_bytes)
            await hub.camera_frame("c1", b"notajpeg")
            await hub.camera_frame("c1", b"")
            await hub.camera_frame("c1", JPEG + b"0" * (2 * 1024 * 1024))
            await hub.flush()
            assert v.bytes == []
            await hub.camera_frame("c1", JPEG + b"0" * (2 * 1024 * 1024 - len(JPEG)))
            await hub.flush()
            assert len(v.bytes) == 1
            await hub.shutdown()
        run(go())

    def test_proto2_binary_and_proto1_json(self, make_hub):
        async def go():
            hub, cam, v1, v2 = make_hub(), Sink(), Sink(), Sink()
            await hub.camera_connected("c1", "A", cam.send_json)
            await hub.viewer_connected("p1", v1.send_json)
            await hub.viewer_connected("p2", v2.send_json, v2.send_bytes)
            await hub.camera_frame("c1", JPEG)
            await hub.flush()
            (fr,) = [m for m in v1.json if m["type"] == "frame"]
            import base64
            assert base64.b64decode(fr["jpeg"]) == JPEG and fr["camera_id"] == "c1"
            assert {"ts", "detections", "frame_w", "frame_h"} <= set(fr)
            assert not any(m["type"] == "frame" for m in v2.json)
            (b,) = v2.bytes
            header, jpeg = decode_viewer_frame(b)
            assert jpeg == JPEG and header["camera_id"] == "c1"
            assert set(header) == {"camera_id", "ts", "detections", "frame_w", "frame_h"}
            await hub.shutdown()
        run(go())

    def test_preview_rate_limited(self, make_hub):
        async def go():
            hub, cam, v = make_hub(), Sink(), Sink()
            await hub.camera_connected("c1", "A", cam.send_json)
            await hub.viewer_connected("v", v.send_json, v.send_bytes)
            for _ in range(10):
                await hub.camera_frame("c1", JPEG)
            await hub.flush()
            assert len(v.bytes) == 1  # preview_fps=2 -> 500 ms spacing
            await hub.shutdown()
        run(go())

    def test_preview_includes_recent_detections(self, make_hub):
        async def go():
            hub, cam, v = make_hub(), Sink(), Sink()
            await hub.camera_connected("c1", "A", cam.send_json)
            await hub.viewer_connected("v", v.send_json, v.send_bytes)
            await hub.camera_detections("c1", [PERSON], 640, 480, 7.0)
            await hub.camera_frame("c1", JPEG)
            await hub.flush()
            header, _ = decode_viewer_frame(v.bytes[0])
            assert header["detections"] == [PERSON] and header["frame_w"] == 640
            assert hub.cameras()[0]["infer_ms"] == 7.0
            await hub.shutdown()
        run(go())

    def test_fps_ema(self, make_hub):
        async def go():
            hub, cam = make_hub(), Sink()
            await hub.camera_connected("c1", "A", cam.send_json)
            for _ in range(6):
                await hub.camera_frame("c1", JPEG)
                await asyncio.sleep(0.05)
            assert 10 < hub.cameras()[0]["fps"] < 25
            await hub.shutdown()
        run(go())

    def test_latest_frame_replaces_pending(self, make_hub):
        async def go():
            hub, cam, v = make_hub(), Sink(), Sink()
            gate = asyncio.Event()
            got = []

            async def slow_bytes(b):
                await gate.wait()
                got.append(b)
            await hub.camera_connected("c1", "A", cam.send_json)
            await hub.camera_connected("c2", "B", cam.send_json)
            await hub.viewer_connected("v", v.send_json, slow_bytes)
            await hub.flush(0.05)  # control drained, sender idle
            await hub.camera_frame("c1", JPEG + b"1")
            await asyncio.sleep(0.02)  # sender now blocked on frame 1
            hub._settings["preview_fps"] = 1000
            await hub.camera_frame("c1", JPEG + b"2")
            await asyncio.sleep(0.01)
            await hub.camera_frame("c1", JPEG + b"3")
            gate.set()
            await hub.flush()
            assert [decode_viewer_frame(b)[1][-1:] for b in got] == [b"1", b"3"]
            await hub.shutdown()
        run(go())


class TestBackpressure:
    def test_ac16_stuck_viewer_does_not_block(self, make_hub):
        async def go():
            hub, cam, good = make_hub(), Sink(), Sink()
            never = asyncio.Event()

            async def stuck(_):
                await never.wait()
            await hub.camera_connected("c1", "A", cam.send_json)
            await hub.viewer_connected("stuck", stuck, stuck)
            await hub.viewer_connected("good", good.send_json, good.send_bytes)
            hub._settings["preview_fps"] = 1000
            worst = 0.0
            for _ in range(30):
                t = time.monotonic()
                await hub.camera_frame("c1", JPEG)
                worst = max(worst, time.monotonic() - t)
                await asyncio.sleep(0.002)
            assert worst < 0.05
            assert await wait_for(lambda: len(good.bytes) >= 5)
            await hub.camera_detections("c1", [PERSON], 640, 480, 1.0)
            assert await wait_for(lambda: "event" in good.types())
            await hub.shutdown()
        run(go())

    def test_send_timeout_drops_viewer_with_1011(self, make_hub, monkeypatch):
        import server.hub as hubmod
        monkeypatch.setattr(hubmod, "SEND_TIMEOUT", 0.1)

        async def go():
            hub, v = make_hub(), Sink()
            never = asyncio.Event()

            async def stuck(_):
                await never.wait()
            await hub.viewer_connected("v", stuck, close=v.close)
            assert await wait_for(lambda: v.closed == [1011])
            assert hub._viewers == {}
            await hub.shutdown()
        run(go())

    def test_send_error_drops_viewer(self, make_hub):
        async def go():
            hub, v = make_hub(), Sink()

            async def err(_):
                raise ConnectionError("gone")
            await hub.viewer_connected("v", err, close=v.close)
            assert await wait_for(lambda: v.closed == [1011])
            await hub.shutdown()
        run(go())

    def test_control_overflow_drops_viewer(self, make_hub):
        async def go():
            hub, cam, v = make_hub(), Sink(), Sink()
            never = asyncio.Event()

            async def stuck(_):
                await never.wait()
            await hub.camera_connected("c1", "A", cam.send_json)
            await hub.viewer_connected("v", stuck, close=v.close)
            for i in range(300):
                await hub.camera_mode("c1", "local" if i % 2 else "server")
            assert await wait_for(lambda: v.closed == [1011])
            assert "v" not in hub._viewers
            await hub.shutdown()
        run(go())

    def test_control_before_frames(self, make_hub):
        async def go():
            hub, cam, v = make_hub(), Sink(), Sink()
            order = []

            async def sj(m):
                order.append(m["type"])

            async def sb(b):
                order.append("bin")
            await hub.camera_connected("c1", "A", cam.send_json)
            await hub.viewer_connected("v", sj, sb)
            await hub.camera_frame("c1", JPEG)
            await hub.flush()
            assert order == ["cameras", "settings", "bin"]
            await hub.shutdown()
        run(go())


class TestDetectorFairness:
    def test_ac17_round_robin_no_reprocessing(self, make_hub):
        async def go():
            det = StubDetector(delay=0.02)
            hub = make_hub(det)
            hub._settings["detect_fps"] = 1000
            cams = {}
            for cid in ("a", "b", "c"):
                cams[cid] = Sink()
                await hub.camera_connected(cid, cid, cams[cid].send_json)
                await hub.camera_mode(cid, "server")
            # a floods frames; b and c send one each
            async def flood():
                for i in range(40):
                    await hub.camera_frame("a", JPEG + bytes([i]))
                    await asyncio.sleep(0.005)
            await hub.camera_frame("b", JPEG + b"B")
            await hub.camera_frame("c", JPEG + b"C")
            await flood()
            assert await wait_for(lambda: any(m["type"] == "detections" for m in cams["b"].json)
                                  and any(m["type"] == "detections" for m in cams["c"].json))
            await asyncio.sleep(0.2)
            assert det.calls.count(JPEG + b"B") == 1 and det.calls.count(JPEG + b"C") == 1
            assert len(det.calls) == len(set(det.calls))  # a frame is never inferred twice
            assert det.calls.index(JPEG + b"B") < 4 and det.calls.index(JPEG + b"C") < 4
            await hub.shutdown()
        run(go())

    def test_detect_fps_limits_rate(self, make_hub):
        async def go():
            det = StubDetector()
            hub = make_hub(det)
            hub._settings["detect_fps"] = 5
            cam = Sink()
            await hub.camera_connected("a", "a", cam.send_json)
            await hub.camera_mode("a", "server")
            for i in range(30):
                await hub.camera_frame("a", JPEG + bytes([i]))
                await asyncio.sleep(0.01)
            assert 1 <= len(det.calls) <= 3
            await hub.shutdown()
        run(go())

    def test_detector_error_reported_once(self, make_hub):
        class Boom(StubDetector):
            def detect(self, jpeg):
                raise RuntimeError("no model")

        async def go():
            hub = make_hub(Boom())
            hub._settings["detect_fps"] = 1000
            cam = Sink()
            await hub.camera_connected("a", "a", cam.send_json)
            await hub.camera_mode("a", "server")
            for i in range(5):
                await hub.camera_frame("a", JPEG + bytes([i]))
                await asyncio.sleep(0.02)
            await hub.flush()
            assert [m["type"] for m in cam.json].count("error") == 1
            await hub.shutdown()
        run(go())

    def test_server_detections_reach_camera_and_create_event(self, make_hub, db):
        async def go():
            hub = make_hub()
            cam, v = Sink(), Sink()
            await hub.camera_connected("a", "A", cam.send_json)
            await hub.viewer_connected("v", v.send_json)
            await hub.camera_mode("a", "server")
            await hub.camera_frame("a", JPEG)
            assert await wait_for(lambda: "event" in v.types())
            await hub.flush()
            det = [m for m in cam.json if m["type"] == "detections"][0]
            assert det["detections"][0]["label"] == "person" and det["infer_ms"] == 5.0
            await hub.shutdown()
        run(go())


class TestEvents:
    def test_event_cooldown_and_filters(self, make_hub):
        async def go():
            hub, cam, v = make_hub(), Sink(), Sink()
            await hub.camera_connected("c1", "A", cam.send_json)
            await hub.viewer_connected("v", v.send_json)
            await hub.camera_frame("c1", JPEG)
            low = {"label": "person", "score": 0.1, "bbox": [0, 0, 1, 1]}
            off = {"label": "toaster", "score": 0.99, "bbox": [0, 0, 1, 1]}
            await hub.camera_detections("c1", [low, off], 640, 480, None)
            await hub.flush()
            assert "event" not in v.types()
            await hub.camera_detections("c1", [low, PERSON], 640, 480, None)
            await hub.camera_detections("c1", [PERSON], 640, 480, None)  # within cooldown
            await hub.flush()
            events = [m for m in v.json if m["type"] == "event"]
            assert len(events) == 1
            assert [d["label"] for d in events[0]["event"]["detections"]] == ["person"]
            await hub.shutdown()
        run(go())


class TestDisconnects:
    def test_disconnect_device(self, make_hub):
        async def go():
            hub = make_hub()
            a, b, c = Sink(), Sink(), Sink()
            await hub.camera_connected("a", "A", a.send_json, device_id="d1", close=a.close)
            await hub.camera_connected("b", "B", b.send_json, device_id="d2", close=b.close)
            await hub.camera_connected("c", "C", c.send_json, close=c.close)
            assert await hub.disconnect_device("d1") == 1
            assert a.closed == [4401] and b.closed == [] and c.closed == []
            assert await hub.disconnect_device("nope") == 0
            assert sorted(x["camera_id"] for x in hub.cameras()) == ["b", "c"]
            await hub.shutdown()
        run(go())

    def test_disconnect_session(self, make_hub):
        async def go():
            hub = make_hub()
            cam, v1, v2, v3 = Sink(), Sink(), Sink(), Sink()
            await hub.camera_connected("a", "A", cam.send_json, close=cam.close, session_id="s1")
            await hub.viewer_connected("v1", v1.send_json, close=v1.close, session_id="s1")
            await hub.viewer_connected("v2", v2.send_json, close=v2.close, session_id="s2")
            await hub.viewer_connected("v3", v3.send_json, close=v3.close, session_id="s1")
            assert await hub.disconnect_session("s1") == 3
            assert cam.closed == [4401] and v1.closed == [4401] and v3.closed == [4401] and v2.closed == []
            assert list(hub._viewers) == ["v2"] and hub.cameras() == []
            assert await hub.disconnect_session("s1") == 0
            await hub.shutdown()
        run(go())

    def test_shutdown_closes_1001(self, make_hub):
        async def go():
            hub, cam, v = make_hub(), Sink(), Sink()
            await hub.camera_connected("a", "A", cam.send_json, close=cam.close)
            await hub.viewer_connected("v", v.send_json, close=v.close)
            await hub.shutdown()
            assert cam.closed == [1001] and v.closed == [1001]
        run(go())


class TestViewerLifecycle:
    def test_initial_messages_and_disconnect(self, make_hub):
        async def go():
            hub, cam, v = make_hub(), Sink(), Sink()
            await hub.camera_connected("a", "A", cam.send_json)
            await hub.viewer_connected("v", v.send_json)
            await hub.flush()
            assert v.types() == ["cameras", "settings"]
            assert v.json[0]["cameras"][0]["camera_id"] == "a"
            assert hub._timer_task is not None
            await hub.viewer_disconnected("v")
            assert hub._timer_task is None and hub._viewers == {}
            await hub.viewer_disconnected("v")
            await hub.shutdown()
        run(go())

    def test_cameras_timer_every_5s(self, make_hub, monkeypatch):
        real_sleep = asyncio.sleep

        async def fast(_):
            await real_sleep(0.01)
        import server.hub as hubmod
        monkeypatch.setattr(hubmod.asyncio, "sleep", fast)

        async def go():
            hub, v = make_hub(), Sink()
            await hub.viewer_connected("v", v.send_json)
            assert await wait_for(lambda: v.types().count("cameras") >= 3)
            await hub.shutdown()
        run(go())


def _bomb_jpeg(w, h):
    buf = io.BytesIO()
    Image.new("L", (w, h), 128).save(buf, format="JPEG", quality=1)
    return buf.getvalue()


class TestBombAndFloods:
    def test_jpeg_dimensions(self):
        assert jpeg_dimensions(_real_jpeg(77, 55)) == (77, 55)
        assert jpeg_dimensions(b"\xff\xd8") is None
        assert jpeg_dimensions(b"\xff\xd8\xff\xe0\x00") is None
        assert jpeg_dimensions(b"\xff\xd8garbage") is None

    def test_second_sof_rejected(self):
        j = _real_jpeg(77, 55)
        i = j.index(b"\xff\xc0")
        sof = j[i:i + 19]
        assert jpeg_dimensions(j[:i] + sof + sof + j[i + 19:]) is None

    def test_add_event_failure_keeps_cooldown(self, make_hub):
        async def go():
            hub, cam, v = make_hub(), Sink(), Sink()
            await hub.camera_connected("c1", "A", cam.send_json)
            await hub.viewer_connected("v", v.send_json)
            real = hub.db.add_event

            def boom(*a, **k):
                raise RuntimeError("disk full")
            hub.db.add_event = boom
            await hub.camera_detections("c1", [PERSON], 64, 48, None)
            assert hub._cameras["c1"]["last_event_ts"] == 0 and not hub._event_times
            hub.db.add_event = real
            await hub.camera_detections("c1", [PERSON], 64, 48, None)
            await hub.flush()
            assert [m["type"] for m in v.json].count("event") == 1
            await hub.shutdown()
        run(go())

    def test_m2_oversized_dimensions_dropped_before_decode(self, make_hub):
        bomb = _bomb_jpeg(13000, 13000)
        assert len(bomb) < 2 * 1024 * 1024 and jpeg_dimensions(bomb) == (13000, 13000)

        async def go():
            hub, cam, v = make_hub(), Sink(), Sink()
            await hub.camera_connected("c1", "A", cam.send_json)
            await hub.viewer_connected("v", v.send_json, v.send_bytes)
            await hub.camera_mode("c1", "server")
            await hub.camera_frame("c1", bomb)
            await hub.flush()
            assert v.bytes == [] and hub._cameras["c1"]["jpeg"] == b"" and hub._cameras["c1"]["frame_seq"] == 0
            await hub.camera_frame("c1", _real_jpeg(4096, 16))
            assert hub._cameras["c1"]["frame_seq"] == 1
            await hub.shutdown()
        run(go())

    def test_l8_cooldown_zero_has_one_per_second_floor(self, make_hub, db):
        async def go():
            hub, cam, v = make_hub(), Sink(), Sink()
            hub._settings["cooldown_s"] = 0
            await hub.camera_connected("c1", "A", cam.send_json)
            await hub.viewer_connected("v", v.send_json)
            for _ in range(20):
                await hub.camera_detections("c1", [PERSON], 64, 48, None)
            await hub.flush()
            assert [m["type"] for m in v.json].count("event") == 1
            await hub.shutdown()
        run(go())

    def test_l8_global_event_cap(self, make_hub, monkeypatch):
        import server.hub as hubmod
        monkeypatch.setattr(hubmod, "EVENT_GLOBAL_MAX", 3)
        monkeypatch.setattr(hubmod, "EVENT_MIN_GAP_MS", 0)

        async def go():
            hub, v = make_hub(), Sink()
            hub._settings["cooldown_s"] = 0
            await hub.viewer_connected("v", v.send_json)
            for i in range(6):
                await hub.camera_connected(f"c{i}", "A", Sink().send_json)
                await hub.camera_detections(f"c{i}", [PERSON], 64, 48, None)
            await hub.flush()
            assert [m["type"] for m in v.json].count("event") == 3
            await hub.shutdown()
        run(go())
