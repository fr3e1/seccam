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
