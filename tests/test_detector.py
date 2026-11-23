import os
import pytest
from pathlib import Path

from server.detector import Detector


class TestDetector:
    def test_detector_unavailable_when_model_missing(self, tmp_path: Path):
        model_path = tmp_path / "nonexistent.onnx"
        detector = Detector(model_path)
        assert detector.available is False

    def test_detector_available_when_model_exists(self, tmp_path: Path):
        # Create a dummy model file
        model_path = tmp_path / "dummy.onnx"
        model_path.write_bytes(b"dummy")
        detector = Detector(model_path)
        # available checks file exists AND onnxruntime importable
        # onnxruntime should be importable in test env
        assert detector.available is True

    @pytest.mark.skipif(
        not os.environ.get("SECCAM_TEST_MODEL"),
        reason="SECCAM_TEST_MODEL not set; set to path of real YOLOX-Nano ONNX model to run detection test"
    )
    def test_detector_real_model(self, make_jpeg):
        model_path = Path(os.environ["SECCAM_TEST_MODEL"])
        detector = Detector(model_path)
        assert detector.available is True
        
        jpeg = make_jpeg(640, 480)
        detections, frame_w, frame_h, infer_ms = detector.detect(jpeg)
        
        assert isinstance(detections, list)
        assert frame_w == 640
        assert frame_h == 480
        assert isinstance(infer_ms, float)
        assert infer_ms > 0
        
        for det in detections:
            assert "label" in det
            assert "score" in det
            assert "bbox" in det
            assert isinstance(det["bbox"], list)
            assert len(det["bbox"]) == 4
            assert 0 <= det["score"] <= 1


def _jpeg(w, h, mode="RGB"):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new(mode, (w, h), 128 if mode == "L" else (1, 2, 3)).save(buf, format="JPEG", quality=1)
    return buf.getvalue()


class TestDetectBounds:
    def test_oversized_frame_rejected_before_decode(self, tmp_path, monkeypatch):
        from PIL import Image
        det = Detector(tmp_path / "m.onnx")
        monkeypatch.setattr(det, "_lazy_init", lambda: None)
        bomb = _jpeg(13000, 13000, "L")

        def no_load(self, *a, **k):
            raise AssertionError("decoded an oversized frame")
        monkeypatch.setattr(Image.Image, "load", no_load)
        with pytest.raises(ValueError, match="too large"):
            det.detect(bomb)

    def test_large_frame_is_draft_decoded_and_boxes_mapped_to_frame(self, tmp_path, monkeypatch):
        import numpy as np
        det = Detector(tmp_path / "m.onnx")
        monkeypatch.setattr(det, "_lazy_init", lambda: None)

        class Sess:
            def run(self, *_):
                return [np.zeros((1, 3549, 85), dtype=np.float32)]
        det._session, det._input_name = Sess(), "x"
        seen = {}

        def post(out, ratio, w, h):
            seen["wh"] = (w, h)
            return [{"label": "person", "score": 0.9, "bbox": [10.0, 20.0, 30.0, 40.0]}]
        monkeypatch.setattr(det, "_postprocess", post)
        dets, fw, fh, _ = det.detect(_jpeg(3200, 2400))
        assert (fw, fh) == (3200, 2400)
        pw, ph = seen["wh"]
        assert pw < 3200 and ph < 2400
        sx = 3200 / pw
        assert dets[0]["bbox"][0] == pytest.approx(10.0 * sx, abs=0.1)
