"""Overlay rendering: skeletons, per-person status, fall banner."""

from __future__ import annotations

import cv2
import numpy as np

from .detector import FallDetector, Phase
from .pose import SKELETON, Person

# BGR
PHASE_COLORS = {
    Phase.NORMAL: (90, 200, 90),
    Phase.DESCENDING: (0, 165, 255),
    Phase.FALLEN: (40, 40, 230),
}


def draw_overlay(
    frame: np.ndarray,
    people: list[Person],
    detector: FallDetector,
    privacy: bool = False,
    fps: float | None = None,
    kp_thresh: float = 0.3,
) -> np.ndarray:
    """Return a copy of the frame with skeletons drawn. privacy=True draws on black."""
    canvas = np.zeros_like(frame) if privacy else frame.copy()
    s = max(frame.shape[:2]) / 1000  # scale line widths/text to resolution
    lw = max(1, round(3 * s))

    for p in people:
        color = PHASE_COLORS[detector.phase_of(p.track_id)]
        kp = p.keypoints
        for a, b in SKELETON:
            if kp[a, 2] >= kp_thresh and kp[b, 2] >= kp_thresh:
                cv2.line(canvas, _pt(kp[a]), _pt(kp[b]), color, lw, cv2.LINE_AA)
        for x, y, c in kp:
            if c >= kp_thresh:
                cv2.circle(canvas, (int(x), int(y)), lw + 2, color, -1, cv2.LINE_AA)

        x1, y1, x2, y2 = map(int, p.bbox)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), color, max(1, lw - 1))
        label = f"#{p.track_id} {detector.phase_of(p.track_id).value.upper()}"
        _label(canvas, label, (x1, y1), color, s)

    if detector.fallen_ids:
        h = int(60 * s) + 10
        cv2.rectangle(canvas, (0, 0), (canvas.shape[1], h), PHASE_COLORS[Phase.FALLEN], -1)
        ids = ", ".join(f"#{i}" for i in detector.fallen_ids)
        cv2.putText(canvas, f"FALL DETECTED: {ids}", (int(15 * s) + 5, int(h * 0.72)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4 * s + 0.3, (255, 255, 255), lw, cv2.LINE_AA)

    if fps is not None:
        cv2.putText(canvas, f"{fps:.1f} FPS", (10, canvas.shape[0] - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8 * s + 0.2, (220, 220, 220), max(1, lw - 1), cv2.LINE_AA)
    return canvas


def _pt(k: np.ndarray) -> tuple[int, int]:
    return int(k[0]), int(k[1])


def _label(img: np.ndarray, text: str, org: tuple[int, int], color, s: float) -> None:
    font, scale, th = cv2.FONT_HERSHEY_SIMPLEX, 0.9 * s + 0.2, max(1, round(2 * s))
    (w, h), base = cv2.getTextSize(text, font, scale, th)
    x, y = org[0], max(org[1], h + base + 4)
    cv2.rectangle(img, (x, y - h - base - 4), (x + w + 6, y), color, -1)
    cv2.putText(img, text, (x + 3, y - base - 2), font, scale, (255, 255, 255), th, cv2.LINE_AA)
