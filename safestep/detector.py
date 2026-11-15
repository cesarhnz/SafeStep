"""Per-person fall detection from pose keypoints.

Each tracked person runs through a small state machine:

    NORMAL ──(hip drops fast)──▶ DESCENDING ──(lying for confirm_s)──▶ FALLEN
       ▲                             │                                    │
       └────(didn't end up lying)────┘                                    │
       └──────────────────────(upright for recover_s)─────────────────────┘

A fall needs BOTH a rapid drop AND ending up lying down. That's what separates
it from sitting down quickly (fast drop, torso stays upright) or lying down on
purpose (lying, but the drop is slow).

While someone is FALLEN the detector keeps watching them: if they lie without
meaningful movement for `immobility_s` it emits one "immobility" event, and when
they stand back up it emits "recovered". Both carry the `fall_id` of the fall
they belong to, so downstream systems can attach them to the same incident.

All distances are measured in "torso lengths" (shoulder-to-hip distance), so
the thresholds work the same whether someone is near or far from the camera.
"""

from __future__ import annotations

import math
import uuid
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

    # After a fall: "still on the floor" detection
    immobility_s: float = 60.0              # no meaningful movement this long -> immobility event
    still_move_threshold: float = 0.3       # moving hips/shoulders this far (torso lengths) = movement
    fallen_track_timeout_s: float = 30.0    # keep (and re-link) a fallen person this long when unseen

    # Fall evidence score: each factor is scaled 0..1 between (weak, strong) anchors,
    # then combined with these weights. A heuristic, NOT a calibrated probability.
    score_drop_range: tuple[float, float] = (0.5, 2.0)     # hip drop, torso lengths
    score_speed_range: tuple[float, float] = (1.0, 4.0)    # peak drop speed, torso lengths/s
    score_angle_range: tuple[float, float] = (45.0, 85.0)  # final torso angle from vertical, degrees
    score_weights: tuple[float, float, float] = (0.4, 0.35, 0.25)  # drop, speed, angle


class Phase(str, Enum):
    NORMAL = "normal"
    DESCENDING = "descending"
    FALLEN = "fallen"


@dataclass
class FallEvent:
    type: str                  # "fall", "immobility" or "recovered"
    track_id: int
    timestamp: float           # seconds (source time: video position or monotonic clock)
    bbox: list[float]          # x1, y1, x2, y2 at the moment of the event
    details: dict = field(default_factory=dict)

    @property
    def fall_id(self) -> str | None:
        return self.details.get("fall_id")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Sample:
    t: float
    hip_x: float
    hip_y: float
    shoulder_x: float
    shoulder_y: float
    torso_len: float
    angle: float     # torso angle from vertical: 0 upright, 90 horizontal, 180 upside down
    aspect: float    # bbox width / height


@dataclass
class Track:
    track_id: int
    last_seen: float
    bbox: np.ndarray
    person_score: float = 0.0          # YOLO's "this is a person" confidence (NOT fall evidence)
    history: deque = field(default_factory=deque)
    phase: Phase = Phase.NORMAL
    # descent (DESCENDING) measurements, kept up to date until the fall is confirmed
    descent_t: float | None = None
    fall_start_t: float | None = None
    top: Sample | None = None          # highest hip position just before the drop
    drop: float = 0.0                  # torso lengths
    peak_drop_speed: float = 0.0       # torso lengths / s (downward)
    peak_lateral_speed: float = 0.0    # torso lengths / s (sideways, either direction)
    lying_since: float | None = None
    # FALLEN state
    fall_id: str | None = None
    fall_t: float | None = None
    fall_scale: float = 0.0
    fall_score: float = 0.0
    upright_since: float | None = None
    still_anchor: Sample | None = None
    still_since: float | None = None
    immobility_sent: bool = False
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
    return Sample(t, float(hip[0]), float(hip[1]), float(shoulder[0]), float(shoulder[1]),
                  torso_len, angle, float(aspect))


def _ramp(x: float, lo: float, hi: float) -> float:
    return min(1.0, max(0.0, (x - lo) / (hi - lo))) if hi > lo else float(x >= hi)


def fall_evidence_score(drop: float, peak_speed: float, angle_deg: float, cfg: FallConfig) -> float:
    """How strongly the movement looks like a fall, 0..1.

    Combines how far the hips dropped, how fast, and how horizontal the body
    ended up. It's a heuristic ranking score, not a calibrated probability.
    """
    parts = (
        _ramp(drop, *cfg.score_drop_range),
        _ramp(peak_speed, *cfg.score_speed_range),
        _ramp(min(angle_deg, 90.0), *cfg.score_angle_range),  # head-down counts as fully horizontal
    )
    w = cfg.score_weights
    return round(sum(wi * p for wi, p in zip(w, parts)) / sum(w), 3)


def evidence_band(score: float) -> str:
    """Same bands the Databricks dashboard uses for `confidence`."""
    if score < 0.5:
        return "low"
    if score < 0.7:
        return "medium"
    if score < 0.85:
        return "high"
    return "very high"


class FallDetector:
    """Feed it tracked people every frame; it returns fall / immobility / recovered events."""

    def __init__(self, config: FallConfig | None = None):
        self.cfg = config or FallConfig()
        self.tracks: dict[int, Track] = {}
        self.now = 0.0

    def phase_of(self, track_id: int) -> Phase:
        tr = self.tracks.get(track_id)
        return tr.phase if tr else Phase.NORMAL

    def still_seconds(self, track_id: int) -> float | None:
        """For a FALLEN person: how long they have been lying without meaningful movement."""
        tr = self.tracks.get(track_id)
        if not tr or tr.phase is not Phase.FALLEN or tr.still_since is None:
            return None
        return max(0.0, self.now - tr.still_since)

    @property
    def fallen_ids(self) -> list[int]:
        return [i for i, tr in self.tracks.items() if tr.phase is Phase.FALLEN]

    def update(self, people: list[Person], t: float) -> list[FallEvent]:
        self.now = t
        events: list[FallEvent] = []
        present = {p.track_id for p in people}

        for person in people:
            tr = self._get_track(person, t, present)
            tr.last_seen = t
            tr.bbox = person.bbox
            tr.person_score = person.score
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

        for tid in [i for i, tr in self.tracks.items() if t - tr.last_seen > self._timeout(tr)]:
            del self.tracks[tid]

        return events

    # -- internals ---------------------------------------------------------

    def _timeout(self, tr: Track) -> float:
        # A person lying on the floor is easy for the pose model to lose for a
        # few seconds; keep their fall state around so it isn't forgotten.
        return self.cfg.fallen_track_timeout_s if tr.phase is Phase.FALLEN else self.cfg.track_timeout_s

    def _get_track(self, person: Person, t: float, present: set[int]) -> Track:
        tid = person.track_id
        if tid in self.tracks:
            return self.tracks[tid]

        # Trackers sometimes hand out a new ID mid-fall (fast motion + big shape
        # change). If a track vanished recently right where this one appeared,
        # treat it as the same person so the fall history isn't lost.
        cx, cy = (person.bbox[:2] + person.bbox[2:]) / 2
        best, best_d = None, float("inf")
        for old_id, old in self.tracks.items():
            window = self.cfg.fallen_track_timeout_s if old.phase is Phase.FALLEN else self.cfg.reassoc_s
            if old_id in present or t - old.last_seen > window:
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
            top = min(recent, key=lambda h: h.hip_y)
            drop = (s.hip_y - top.hip_y) / scale
            if drop >= cfg.descent_threshold:
                tr.phase = Phase.DESCENDING
                tr.descent_t = t
                tr.fall_start_t = top.t
                tr.top = top
                tr.drop = drop
                tr.lying_since = None
                since_top = [h for h in recent if h.t >= top.t]
                tr.peak_drop_speed = _peak_speed(since_top, scale, "y")
                tr.peak_lateral_speed = _peak_speed(since_top, scale, "x")

        if tr.phase is Phase.DESCENDING:
            # Keep measuring until the fall is confirmed, so drop and speed cover
            # the whole fall and not just its first part.
            tr.drop = max(tr.drop, (s.hip_y - tr.top.hip_y) / scale)
            if len(tr.history) >= 2:
                pair = [tr.history[-2], s]
                tr.peak_drop_speed = max(tr.peak_drop_speed, _peak_speed(pair, scale, "y"))
                tr.peak_lateral_speed = max(tr.peak_lateral_speed, _peak_speed(pair, scale, "x"))

            tr.lying_since = _since(tr.lying_since, t) if lying else None
            if tr.lying_since is not None and t - tr.lying_since >= cfg.confirm_s:
                return self._confirm_fall(tr, s, t, scale)
            if tr.lying_since is None and t - tr.descent_t > cfg.confirm_window_s:
                tr.phase = Phase.NORMAL  # dropped fast but didn't end up lying (e.g. sat down)

        elif tr.phase is Phase.FALLEN:
            tr.upright_since = _since(tr.upright_since, t) if upright else None
            if tr.upright_since is not None and t - tr.upright_since >= cfg.recover_s:
                return self._recover(tr, s, t)
            return self._check_immobility(tr, s, t)
        return None

    def _confirm_fall(self, tr: Track, s: Sample, t: float, scale: float) -> FallEvent:
        cfg = self.cfg
        tr.phase = Phase.FALLEN
        tr.fall_id = uuid.uuid4().hex
        tr.fall_t = t
        tr.fall_scale = scale
        tr.upright_since = None
        tr.still_anchor, tr.still_since, tr.immobility_sent = s, t, False
        tr.fall_score = fall_evidence_score(tr.drop, tr.peak_drop_speed, s.angle, cfg)
        return FallEvent("fall", tr.track_id, t, _bbox(tr.bbox), {
            "fall_id": tr.fall_id,
            "fall_confidence": tr.fall_score,
            "evidence": evidence_band(tr.fall_score),
            "drop_torso_lengths": round(tr.drop, 2),
            "peak_drop_speed": round(tr.peak_drop_speed, 2),
            "peak_lateral_speed": round(tr.peak_lateral_speed, 2),
            "torso_angle_deg": round(s.angle, 1),
            "person_detection_score": round(float(tr.person_score), 3),
            "fall_started_at": tr.fall_start_t,
        })

    def _check_immobility(self, tr: Track, s: Sample, t: float) -> FallEvent | None:
        cfg = self.cfg
        a = tr.still_anchor
        moved = max(math.hypot(s.hip_x - a.hip_x, s.hip_y - a.hip_y),
                    math.hypot(s.shoulder_x - a.shoulder_x, s.shoulder_y - a.shoulder_y)) / tr.fall_scale
        if moved >= cfg.still_move_threshold:
            # Measure movement from a fixed anchor, not frame-to-frame, so keypoint
            # jitter can't add up to "movement" and slow drift can't hide it.
            tr.still_anchor, tr.still_since = s, t
            return None
        still_for = t - tr.still_since
        if not tr.immobility_sent and still_for >= cfg.immobility_s:
            tr.immobility_sent = True
            return FallEvent("immobility", tr.track_id, t, _bbox(tr.bbox), {
                "fall_id": tr.fall_id,
                "fall_confidence": tr.fall_score,
                "immobility_seconds": round(still_for, 1),
                "time_on_floor_s": round(t - tr.fall_start_t, 1),
            })
        return None

    def _recover(self, tr: Track, s: Sample, t: float) -> FallEvent:
        event = FallEvent("recovered", tr.track_id, t, _bbox(tr.bbox), {
            "fall_id": tr.fall_id,
            "fall_confidence": tr.fall_score,
            # From the start of the fall until they were back on their feet. The
            # upright-confirmation wait (recover_s) is deliberately not included.
            "recovery_seconds": round(tr.upright_since - tr.fall_start_t, 1),
            "immobility_reported": tr.immobility_sent,
        })
        tr.phase = Phase.NORMAL
        tr.fall_id = None
        tr.still_anchor = tr.still_since = tr.upright_since = None
        tr.history.clear()
        tr.history.append(s)
        return event


def _since(start: float | None, t: float) -> float:
    return t if start is None else start


def _peak_speed(samples: list[Sample], scale: float, axis: str) -> float:
    """Peak hip speed in torso lengths/s: downward for "y", either direction for "x"."""
    best = 0.0
    for a, b in zip(samples, samples[1:]):
        if b.t > a.t:
            if axis == "y":
                v = (b.hip_y - a.hip_y) / (b.t - a.t) / scale
            else:
                v = abs(b.hip_x - a.hip_x) / (b.t - a.t) / scale
            best = max(best, v)
    return best


def _bbox(b: np.ndarray) -> list[float]:
    return [round(float(v), 1) for v in b]
