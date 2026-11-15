"""Fall logic tests on synthetic skeletons (no camera or model needed)."""

import math

import numpy as np

from safestep.detector import FallDetector, Phase
from safestep.pose import L_HIP, L_SHOULDER, R_HIP, R_SHOULDER, Person

FPS = 30
TORSO = 100.0  # pixels


def person(tid: int, hip_y: float, angle_deg: float, conf: float = 0.9, hip_x: float = 500) -> Person:
    """A skeleton whose torso is tilted angle_deg from vertical, with legs opposite."""
    a = math.radians(angle_deg)
    up = np.array([math.sin(a), -math.cos(a)])  # hip -> shoulder direction (image y down)
    side = np.array([-up[1], up[0]]) * 20
    hip = np.array([hip_x, hip_y])
    shoulder = hip + up * TORSO
    feet = hip - up * TORSO * 1.6
    head = shoulder + up * 30

    kp = np.zeros((17, 3))
    kp[:, :2] = hip  # default everything to hip, then place the parts we care about
    kp[:, 2] = conf
    kp[0, :2] = head
    kp[L_SHOULDER, :2], kp[R_SHOULDER, :2] = shoulder + side, shoulder - side
    kp[L_HIP, :2], kp[R_HIP, :2] = hip + side, hip - side
    kp[15, :2], kp[16, :2] = feet + side, feet - side

    pts = kp[:, :2]
    bbox = np.array([*(pts.min(0) - 15), *(pts.max(0) + 15)])
    return Person(tid, bbox, kp, 0.9)


def run(detector, segments, tid=1, t0=0.0):
    """segments: list of (duration_s, (hip_y0, angle0), (hip_y1, angle1)). Returns (events, end_t)."""
    events, t = [], t0
    for dur, (y0, a0), (y1, a1) in segments:
        n = max(1, round(dur * FPS))
        for i in range(n):
            f = (i + 1) / n
            p = person(tid if not callable(tid) else tid(t), y0 + (y1 - y0) * f, a0 + (a1 - a0) * f)
            t += 1 / FPS
            events += detector.update([p], t)
    return events, t


STAND = (400, 3)
FALL = [(1.5, STAND, STAND), (0.5, STAND, (550, 88)), (2.0, (550, 88), (550, 88))]


def test_standing_still_is_not_a_fall():
    d = FallDetector()
    events, _ = run(d, [(5, STAND, STAND)])
    assert events == []
    assert d.phase_of(1) is Phase.NORMAL


def test_fast_fall_to_floor_is_detected_once():
    d = FallDetector()
    events, _ = run(d, FALL)
    assert [e.type for e in events] == ["fall"]
    assert events[0].track_id == 1
    assert events[0].details["drop_torso_lengths"] >= 1.0
    assert d.phase_of(1) is Phase.FALLEN
    assert d.fallen_ids == [1]


def test_sitting_down_quickly_is_not_a_fall():
    d = FallDetector()
    events, _ = run(d, [(1.5, STAND, STAND), (0.4, STAND, (510, 10)), (4, (510, 10), (510, 10))])
    assert events == []
    assert d.phase_of(1) is Phase.NORMAL


def test_lying_down_slowly_is_not_a_fall():
    d = FallDetector()
    events, _ = run(d, [(1.5, STAND, STAND), (5, STAND, (550, 88)), (3, (550, 88), (550, 88))])
    assert events == []


def test_bending_over_is_not_a_fall():
    d = FallDetector()
    events, _ = run(d, [(1.5, STAND, STAND), (0.6, STAND, (430, 80)), (2, (430, 80), (430, 80))])
    assert events == []


def test_getting_back_up_emits_recovered():
    d = FallDetector()
    events, t = run(d, FALL)
    more, _ = run(d, [(1.0, (550, 88), STAND), (2.5, STAND, STAND)], t0=t)
    assert [e.type for e in events + more] == ["fall", "recovered"]
    assert d.phase_of(1) is Phase.NORMAL


def test_brief_sit_up_does_not_count_as_recovery():
    d = FallDetector()
    events, t = run(d, FALL)
    more, _ = run(d, [(0.3, (550, 88), (450, 10)), (0.5, (450, 10), (450, 10)),
                      (0.3, (450, 10), (550, 88)), (2, (550, 88), (550, 88))], t0=t)
    assert [e.type for e in events + more] == ["fall"]
    assert d.phase_of(1) is Phase.FALLEN


def test_tracker_id_switch_mid_fall_still_detected():
    d = FallDetector()
    switch_at = 1.75  # tracker reassigns ID partway through the drop
    events, _ = run(d, FALL, tid=lambda t: 1 if t < switch_at else 7)
    assert [(e.type, e.track_id) for e in events] == [("fall", 7)]


def test_two_people_tracked_independently():
    d = FallDetector()
    events, t = [], 0.0
    for i in range(round(4 * FPS)):
        t += 1 / FPS
        if t < 1.5:
            y, a = STAND
        elif t < 2.0:
            f = (t - 1.5) / 0.5
            y, a = 400 + 150 * f, 3 + 85 * f
        else:
            y, a = 550, 88
        events += d.update([person(1, y, a), person(2, *STAND, hip_x=1200)], t)
    assert [(e.type, e.track_id) for e in events] == [("fall", 1)]
    assert d.phase_of(2) is Phase.NORMAL


def test_low_confidence_keypoints_are_ignored():
    d = FallDetector()
    t = 0.0
    for i in range(round(3 * FPS)):
        t += 1 / FPS
        y, a = (STAND if i < 45 else (550, 88))
        assert d.update([person(1, y, a, conf=0.1)], t) == []
