"""YOLO pose estimation + multi-person tracking."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# COCO-17 keypoint indices (the layout YOLO pose models output)
NOSE = 0
L_SHOULDER, R_SHOULDER = 5, 6
L_HIP, R_HIP = 11, 12

SKELETON = [
    (5, 6), (5, 7), (7, 9), (6, 8), (8, 10),          # shoulders + arms
    (5, 11), (6, 12), (11, 12),                       # torso
    (11, 13), (13, 15), (12, 14), (14, 16),           # legs
    (0, 1), (0, 2), (1, 3), (2, 4),                   # face
]


@dataclass
class Person:
    track_id: int
    bbox: np.ndarray        # (4,) x1, y1, x2, y2 in pixels
    keypoints: np.ndarray   # (17, 3) x, y, confidence
    score: float


class PoseEstimator:
    """Runs a YOLO pose model with ByteTrack so each person keeps a stable ID."""

    def __init__(
        self,
        model: str = "yolo26n-pose.pt",
        conf: float = 0.35,
        imgsz: int = 640,
        device: str | None = None,
        tracker: str = "bytetrack.yaml",
    ):
        from ultralytics import YOLO  # imported lazily so tests don't need torch

        self.model = YOLO(model)
        self.conf = conf
        self.imgsz = imgsz
        self.device = device
        self.tracker = tracker

    def __call__(self, frame: np.ndarray) -> list[Person]:
        result = self.model.track(
            frame,
            persist=True,
            conf=self.conf,
            imgsz=self.imgsz,
            device=self.device,
            tracker=self.tracker,
            classes=[0],
            verbose=False,
        )[0]

        boxes, kps = result.boxes, result.keypoints
        if boxes is None or boxes.id is None or kps is None or len(boxes) == 0:
            return []

        ids = boxes.id.int().cpu().numpy()
        xyxy = boxes.xyxy.cpu().numpy()
        scores = boxes.conf.cpu().numpy()
        kxy = kps.xy.cpu().numpy()
        kconf = kps.conf.cpu().numpy() if kps.conf is not None else np.ones(kxy.shape[:2])
        keypoints = np.concatenate([kxy, kconf[..., None]], axis=-1)

        return [
            Person(int(i), b, k, float(s))
            for i, b, k, s in zip(ids, xyxy, keypoints, scores)
        ]
