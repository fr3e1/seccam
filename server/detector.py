import os
import sys
import threading
import time
from pathlib import Path
from typing import List, Dict, Tuple, Optional

import numpy as np
from PIL import Image

# COCO class names in standard order (80 classes)
COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush"
]

# Frames larger than this per side are rejected before decoding (decompression-bomb guard)
MAX_DIM = 4096
Image.MAX_IMAGE_PIXELS = MAX_DIM * MAX_DIM

# Strides and grid sizes for YOLOX-Nano
STRIDES = [8, 16, 32]
GRID_SIZES = [52, 26, 13]  # 416 / stride


class Detector:
    """
    YOLOX-Nano ONNX detector for server-side fallback inference.
    Thread-safe, lazy-loads onnxruntime session.
    """

    def __init__(
        self,
        model_path: Path,
        input_size: int = 416,
        score_thr: float = 0.3,
        nms_thr: float = 0.45
    ):
        self.model_path = Path(model_path)
        self.input_size = input_size
        self.score_thr = score_thr
        self.nms_thr = nms_thr

        self._session = None
        self._input_name = None
        self._grids = None
        self._expanded_strides = None
        self._lock = threading.Lock()
        self._initialized = False

    @property
    def available(self) -> bool:
        """Check if model file exists and onnxruntime is importable (without loading)."""
        if not self.model_path.exists():
            return False
        try:
            import onnxruntime  # noqa: F401
            return True
        except Exception:
            return False

    def _lazy_init(self) -> None:
        """Lazy initialization of onnxruntime session and precomputed grids."""
        if self._initialized:
            return

        with self._lock:
            if self._initialized:
                return

            import onnxruntime as ort

            # Session options for minimal threading and max optimization
            sess_options = ort.SessionOptions()
            sess_options.intra_op_num_threads = 1
            sess_options.inter_op_num_threads = 1
            sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

            # Load session
            self._session = ort.InferenceSession(
                str(self.model_path),
                sess_options=sess_options,
                providers=["CPUExecutionProvider"]
            )
            self._input_name = self._session.get_inputs()[0].name

            # Precompute grids and expanded strides for decoding
            self._grids = []
            self._expanded_strides = []
            for stride, grid_size in zip(STRIDES, GRID_SIZES):
                # Create grid: (grid_size, grid_size, 2)
                yv, xv = np.meshgrid(
                    np.arange(grid_size, dtype=np.float32),
                    np.arange(grid_size, dtype=np.float32),
                    indexing="ij"
                )
                grid = np.stack([xv, yv], axis=-1).reshape(1, -1, 2)
                self._grids.append(grid)
                expanded_stride = np.full((1, grid_size * grid_size, 1), stride, dtype=np.float32)
                self._expanded_strides.append(expanded_stride)

            self._grids = np.concatenate(self._grids, axis=1)  # (1, 3549, 2)
            self._expanded_strides = np.concatenate(self._expanded_strides, axis=1)  # (1, 3549, 1)

            self._initialized = True

    def _preprocess(self, image: Image.Image) -> Tuple[np.ndarray, float, int, int]:
        """
        Preprocess PIL image to model input.
        Returns: (input_tensor, ratio, orig_w, orig_h)
        """
        orig_w, orig_h = image.size

        # Fast downscale if image is much larger than 2x input size
        max_dim = max(orig_w, orig_h)
        if max_dim > self.input_size * 2:
            # Use draft mode for fast JPEG decode at reduced resolution
            scale = (self.input_size * 2) / max_dim
            new_w = int(orig_w * scale)
            new_h = int(orig_h * scale)
            image.draft("RGB", (new_w, new_h))
            image = image.resize((new_w, new_h), Image.Resampling.BILINEAR)
            orig_w, orig_h = new_w, new_h

        # Convert to RGB then BGR numpy array
        image = image.convert("RGB")
        img = np.array(image, dtype=np.uint8)  # HWC RGB
        img = img[:, :, ::-1]  # RGB -> BGR

        h, w = img.shape[:2]

        # Letterbox: resize keeping aspect ratio, paste at top-left on 114-filled canvas
        r = min(self.input_size / h, self.input_size / w)
        new_h, new_w = int(h * r), int(w * r)

        resized = cv2_resize(img, (new_w, new_h))

        # Create canvas filled with 114
        canvas = np.full((self.input_size, self.input_size, 3), 114, dtype=np.uint8)
        canvas[:new_h, :new_w] = resized

        # HWC -> CHW, add batch dim, float32 (0-255, NOT normalized)
        input_tensor = canvas.transpose(2, 0, 1)[np.newaxis, ...].astype(np.float32)

        return input_tensor, r, w, h

    def _postprocess(
        self,
        output: np.ndarray,
        ratio: float,
        orig_w: int,
        orig_h: int
    ) -> List[Dict]:
        """
        Decode YOLOX output, apply NMS, return detections.
        output shape: [1, 3549, 85] (85 = 4 box + 1 obj + 80 class)
        """
        # output: (1, 3549, 85)
        predictions = output[0]  # (3549, 85)

        # Box decoding: xy = (out[...,0:2] + grid) * stride, wh = exp(out[...,2:4]) * stride
        box_xy = (predictions[:, :2] + self._grids[0]) * self._expanded_strides[0]
        box_wh = np.exp(predictions[:, 2:4]) * self._expanded_strides[0]

        # Convert to x1y1x2y2 in letterboxed coordinates
        box_x1y1 = box_xy - box_wh / 2
        box_x2y2 = box_xy + box_wh / 2
        boxes = np.concatenate([box_x1y1, box_x2y2], axis=-1)  # (3549, 4)

        # Objectness * class prob
        obj_conf = predictions[:, 4:5]  # (3549, 1)
        class_conf = predictions[:, 5:]  # (3549, 80)
        scores = obj_conf * class_conf  # (3549, 80)

        # Get best class per box
        class_ids = np.argmax(scores, axis=1)  # (3549,)
        max_scores = scores[np.arange(len(scores)), class_ids]  # (3549,)

        # Filter by score threshold
        mask = max_scores >= self.score_thr
        if not np.any(mask):
            return []

        boxes = boxes[mask]
        scores = max_scores[mask]
        class_ids = class_ids[mask]

        # Convert boxes from letterboxed to original image coordinates
        boxes /= ratio

        # Clip to image bounds
        boxes[:, [0, 2]] = np.clip(boxes[:, [0, 2]], 0, orig_w)
        boxes[:, [1, 3]] = np.clip(boxes[:, [1, 3]], 0, orig_h)

        # Convert to xywh format
        boxes_xywh = np.zeros_like(boxes)
        boxes_xywh[:, 0] = boxes[:, 0]  # x
        boxes_xywh[:, 1] = boxes[:, 1]  # y
        boxes_xywh[:, 2] = boxes[:, 2] - boxes[:, 0]  # w
        boxes_xywh[:, 3] = boxes[:, 3] - boxes[:, 1]  # h

        # Class-wise NMS
        keep_indices = self._nms_classwise(boxes_xywh, scores, class_ids)

        detections = []
        for idx in keep_indices:
            x, y, w, h = boxes_xywh[idx]
            detections.append({
                "label": COCO_CLASSES[class_ids[idx]],
                "score": round(float(scores[idx]), 3),
                "bbox": [round(float(x), 1), round(float(y), 1), round(float(w), 1), round(float(h), 1)]
            })

        return detections

    def _nms_classwise(
        self,
        boxes: np.ndarray,  # (N, 4) xywh
        scores: np.ndarray,  # (N,)
        class_ids: np.ndarray  # (N,)
    ) -> List[int]:
        """Class-wise NMS using numpy."""
        keep = []
        unique_classes = np.unique(class_ids)

        for cls in unique_classes:
            cls_mask = class_ids == cls
            cls_boxes = boxes[cls_mask]
            cls_scores = scores[cls_mask]
            cls_indices = np.where(cls_mask)[0]

            # Sort by score descending
            order = cls_scores.argsort()[::-1]
            cls_boxes = cls_boxes[order]
            cls_scores = cls_scores[order]
            cls_indices = cls_indices[order]

            while len(cls_boxes) > 0:
                keep.append(cls_indices[0])
                if len(cls_boxes) == 1:
                    break

                # Compute IoU of first box with rest
                ious = self._iou_xywh(cls_boxes[0:1], cls_boxes[1:])
                mask = ious[0] <= self.nms_thr
                cls_boxes = cls_boxes[1:][mask]
                cls_scores = cls_scores[1:][mask]
                cls_indices = cls_indices[1:][mask]

        return keep

    def _iou_xywh(self, boxes1: np.ndarray, boxes2: np.ndarray) -> np.ndarray:
        """IoU between boxes1 (M,4) and boxes2 (N,4) in xywh format. Returns (M,N)."""
        # Convert to x1y1x2y2
        b1_x1 = boxes1[:, 0]
        b1_y1 = boxes1[:, 1]
        b1_x2 = boxes1[:, 0] + boxes1[:, 2]
        b1_y2 = boxes1[:, 1] + boxes1[:, 3]

        b2_x1 = boxes2[:, 0]
        b2_y1 = boxes2[:, 1]
        b2_x2 = boxes2[:, 0] + boxes2[:, 2]
        b2_y2 = boxes2[:, 1] + boxes2[:, 3]

        # Intersection
        inter_x1 = np.maximum(b1_x1[:, None], b2_x1[None, :])
        inter_y1 = np.maximum(b1_y1[:, None], b2_y1[None, :])
        inter_x2 = np.minimum(b1_x2[:, None], b2_x2[None, :])
        inter_y2 = np.minimum(b1_y2[:, None], b2_y2[None, :])

        inter_w = np.maximum(0, inter_x2 - inter_x1)
        inter_h = np.maximum(0, inter_y2 - inter_y1)
        inter_area = inter_w * inter_h

        # Union
        area1 = boxes1[:, 2] * boxes1[:, 3]
        area2 = boxes2[:, 2] * boxes2[:, 3]
        union = area1[:, None] + area2[None, :] - inter_area

        return inter_area / (union + 1e-6)

    def detect(self, jpeg: bytes) -> Tuple[List[Dict], int, int, float]:
        """
        Run detection on JPEG bytes.
        Returns: (detections, frame_w, frame_h, infer_ms)
        """
        self._lazy_init()

        # Decode JPEG with Pillow
        from io import BytesIO
        try:
            img = Image.open(BytesIO(jpeg))
        except Image.DecompressionBombError as e:
            raise ValueError("frame too large") from e
        frame_w, frame_h = img.size
        if not (0 < frame_w <= MAX_DIM and 0 < frame_h <= MAX_DIM):
            raise ValueError("frame too large")
        # Reduced-size JPEG decode (DCT scaling) keeps memory small; must precede load()
        img.draft("RGB", (self.input_size * 2, self.input_size * 2))
        img.load()

        # Preprocess
        input_tensor, ratio, orig_w, orig_h = self._preprocess(img)

        # Inference
        start = time.perf_counter()
        with self._lock:
            output = self._session.run(None, {self._input_name: input_tensor})[0]
        infer_ms = (time.perf_counter() - start) * 1000

        # Postprocess
        detections = self._postprocess(output, ratio, orig_w, orig_h)

        # Boxes are in the (possibly downscaled) processed space; map back to frame pixels
        if orig_w != frame_w or orig_h != frame_h:
            sx, sy = frame_w / orig_w, frame_h / orig_h
            for d in detections:
                x, y, w, h = d["bbox"]
                d["bbox"] = [round(x * sx, 1), round(y * sy, 1), round(w * sx, 1), round(h * sy, 1)]

        return detections, frame_w, frame_h, infer_ms


# OpenCV resize fallback using PIL (avoid cv2 dependency)
def cv2_resize(img: np.ndarray, size: Tuple[int, int]) -> np.ndarray:
    """Resize using PIL (LANCZOS) to avoid OpenCV dependency."""
    w, h = size
    pil_img = Image.fromarray(img)
    pil_img = pil_img.resize((w, h), Image.Resampling.LANCZOS)
    return np.array(pil_img, dtype=np.uint8)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m server.detector <image.jpg> [model_path]", file=sys.stderr)
        sys.exit(1)

    image_path = Path(sys.argv[1])
    model_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("./models/yolox_nano.onnx")

    if not image_path.exists():
        print(f"Image not found: {image_path}", file=sys.stderr)
        sys.exit(1)

    detector = Detector(model_path)

    if not detector.available:
        print(f"Model not available: {model_path}", file=sys.stderr)
        sys.exit(1)

    with open(image_path, "rb") as f:
        jpeg = f.read()

    detections, w, h, infer_ms = detector.detect(jpeg)

    import json
    result = {
        "detections": detections,
        "frame_w": w,
        "frame_h": h,
        "infer_ms": round(infer_ms, 1)
    }
    print(json.dumps(result, indent=2))
