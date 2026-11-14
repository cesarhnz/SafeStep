"""Per-person fall detection from pose keypoints.

Each tracked person runs through a small state machine:

    NORMAL ──(hip drops fast)──▶ DESCENDING ──(lying for confirm_s)──▶ FALLEN
       ▲                             │                                    │
       └────(didn't end up lying)────┘                                    │
       └──────────────────────(upright for recover_s)─────────────────────┘

A fall needs BOTH a rapid drop AND ending up lying down. That's what separates
it from sitting down quickly (fast drop, torso stays upright) or lying down on
purpose (lying, but the drop is slow).

All distances are measured in "torso lengths" (shoulder-to-hip distance), so
the thresholds work the same whether someone is near or far from the camera.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum

import numpy as np

from .pose import L_HIP, L_SHOULDER, R_HIP, R_SHOULDER, Person


@dataclass
class FallConfig:
    min_kp_conf: float = 0.3          # ignore keypoints below this confidence
    descent_window_s: float = 1.0     # look-back window for a rapid drop
    descent_threshold: float = 1.0    # hip drop (torso lengths) within that window
    scale_window_s: float = 3.0       # torso length = max seen over this window
    lying_angle_deg: float = 55.0     # torso this far from vertical counts as lying
    lying_aspect: float = 1.3         # ...or bbox width/height at least this
    upright_angle_deg: float = 35.0   # torso this close to vertical counts as upright
    confirm_s: float = 0.6            # must stay lying this long to confirm a fall
    confirm_window_s: float = 2.5     # time allowed after the drop to end up lying
    recover_s: float = 1.5            # must stay upright this long to recover
    track_timeout_s: float = 3.0      # forget people unseen for this long
    reassoc_s: float = 1.0            # re-link a new track ID to a just-lost one


class Phase(str, Enum):
    NORMAL = "normal"
    DESCENDING = "descending"
    FALLEN = "fallen"


@dataclass
class FallEvent:
    type: str                  # "fall" or "recovered"
    track_id: int
    timestamp: float           # seconds (source time: video position or monotonic clock)
    bbox: list[float]          # x1, y1, x2, y2 at the moment of the event
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Sample:
    t: float
    hip_y: float
    torso_len: float
    angle: float     # torso angle from vertical: 0 upright, 90 horizontal, 180 upside down
    aspect: float    # bbox width / height


@dataclass
class Track:
    track_id: int
    last_seen: float
    bbox: np.ndarray
    history: deque = field(default_factory=deque)
    phase: Phase = Phase.NORMAL
    descent_t: float | None = None
    descent_info: dict = field(default_factory=dict)
    lying_since: float | None = None
    upright_since: float | None = None
    fall_t: float | None = None
    last_sample: Sample | None = None


def measure(person: Person, cfg: FallConfig, t: float) -> Sample | None:
    """Extract posture features from one person's keypoints, or None if unreliable."""
    k = person.keypoints

    def center(a: int, b: int) -> np.ndarray | None:
        pts = [k[i, :2] for i in (a, b) if k[i, 2] >= cfg.min_kp_conf]
        return np.mean(pts, axis=0) if pts else None

    shoulder = center(L_SHOULDER, R_SHOULDER)
    hip = center(L_HIP, R_HIP)
    if shoulder is None or hip is None:
        return None

    dx, dy = shoulder - hip
    torso_len = math.hypot(dx, dy)
    if torso_len < 5:  # pixels; too small to trust
        return None

    # Image y points down, so an upright person has shoulder above hip (dy < 0).
    angle = math.degrees(math.atan2(abs(dx), -dy))
    x1, y1, x2, y2 = person.bbox
    aspect = (x2 - x1) / max(y2 - y1, 1.0)
    return Sample(t, float(hip[1]), torso_len, angle, float(aspect))


class FallDetector:
    """Feed it tracked people every frame; it returns fall/recovery events."""

    def __init__(self, config: FallConfig | None = None):
        self.cfg = config or FallConfig()
        self.tracks: dict[int, Track] = {}

    def phase_of(self, track_id: int) -> Phase:
        tr = self.tracks.get(track_id)
        return tr.phase if tr else Phase.NORMAL

    @property
    def fallen_ids(self) -> list[int]:
        return [i for i, tr in self.tracks.items() if tr.phase is Phase.FALLEN]

    def update(self, people: list[Person], t: float) -> list[FallEvent]:
        events: list[FallEvent] = []
        present = {p.track_id for p in people}

        for person in people:
            tr = self._get_track(person, t, present)
            tr.last_seen = t
            tr.bbox = person.bbox
            sample = measure(person, self.cfg, t)
            if sample is None:
                continue
            tr.last_sample = sample
            tr.history.append(sample)
            while tr.history and t - tr.history[0].t > self.cfg.scale_window_s:
                tr.history.popleft()
            event = self._step(tr, sample, t)
            if event:
                events.append(event)

        for tid in [i for i, tr in self.tracks.items() if t - tr.last_seen > self.cfg.track_timeout_s]:
            del self.tracks[tid]

        return events

    # -- internals ---------------------------------------------------------

    def _get_track(self, person: Person, t: float, present: set[int]) -> Track:
        tid = person.track_id
        if tid in self.tracks:
            return self.tracks[tid]

        # Trackers sometimes hand out a new ID mid-fall (fast motion + big shape
        # change). If a track vanished moments ago right where this one appeared,
        # treat it as the same person so the fall history isn't lost.
        cx, cy = (person.bbox[:2] + person.bbox[2:]) / 2
        best, best_d = None, float("inf")
        for old_id, old in self.tracks.items():
            if old_id in present or t - old.last_seen > self.cfg.reassoc_s:
                continue
            ox1, oy1, ox2, oy2 = old.bbox
            d = math.hypot(cx - (ox1 + ox2) / 2, cy - (oy1 + oy2) / 2)
            if d < max(ox2 - ox1, oy2 - oy1) and d < best_d:
                best, best_d = old_id, d

        if best is not None:
            tr = self.tracks.pop(best)
            tr.track_id = tid
        else:
            tr = Track(track_id=tid, last_seen=t, bbox=person.bbox)
        self.tracks[tid] = tr
        return tr

    def _step(self, tr: Track, s: Sample, t: float) -> FallEvent | None:
        cfg = self.cfg
        scale = max(h.torso_len for h in tr.history)  # upright torso length, robust to foreshortening
        lying = s.angle >= cfg.lying_angle_deg or s.aspect >= cfg.lying_aspect
        upright = s.angle <= cfg.upright_angle_deg and not lying

        if tr.phase is Phase.NORMAL:
            recent = [h for h in tr.history if t - h.t <= cfg.descent_window_s]
            top = min(h.hip_y for h in recent)
            drop = (s.hip_y - top) / scale
            if drop >= cfg.descent_threshold:
                tr.phase = Phase.DESCENDING
                tr.descent_t = t
                tr.lying_since = None
                tr.descent_info = {
                    "drop_torso_lengths": round(drop, 2),
                    "peak_speed_torso_lengths_per_s": round(_peak_down_speed(recent, scale), 2),
                }

        if tr.phase is Phase.DESCENDING:
            tr.lying_since = _since(tr.lying_since, t) if lying else None
            if tr.lying_since is not None and t - tr.lying_since >= cfg.confirm_s:
                tr.phase = Phase.FALLEN
                tr.fall_t = t
                tr.upright_since = None
                return FallEvent(
                    "fall", tr.track_id, t, _bbox(tr.bbox),
                    {**tr.descent_info, "torso_angle_deg": round(s.angle, 1),
                     "fall_started_at": tr.descent_t},
                )
            if tr.lying_since is None and t - tr.descent_t > cfg.confirm_window_s:
                tr.phase = Phase.NORMAL  # dropped fast but didn't end up lying (e.g. sat down)

        elif tr.phase is Phase.FALLEN:
            tr.upright_since = _since(tr.upright_since, t) if upright else None
            if tr.upright_since is not None and t - tr.upright_since >= cfg.recover_s:
                tr.phase = Phase.NORMAL
                tr.history.clear()
                tr.history.append(s)
                return FallEvent(
                    "recovered", tr.track_id, t, _bbox(tr.bbox),
                    {"fallen_for_s": round(t - tr.fall_t, 2)},
                )
        return None


def _since(start: float | None, t: float) -> float:
    return t if start is None else start


def _peak_down_speed(samples: list[Sample], scale: float) -> float:
    best = 0.0
    for a, b in zip(samples, samples[1:]):
        if b.t > a.t:
            best = max(best, (b.hip_y - a.hip_y) / (b.t - a.t) / scale)
    return best


def _bbox(b: np.ndarray) -> list[float]:
    return [round(float(v), 1) for v in b]
