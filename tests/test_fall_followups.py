"""Fall evidence score, immobility and recovery events (synthetic skeletons)."""

from safestep.detector import FallConfig, FallDetector, Phase, fall_evidence_score
from test_detector import FALL, FPS, STAND, person, run

LYING = (550, 88)


def fast_cfg(**kw):
    return FallConfig(immobility_s=5.0, **kw)


def fall_then(detector, segments):
    events, t = run(detector, FALL)
    more, t = run(detector, segments, t0=t)
    return events + more, t


def test_fall_confidence_is_fall_evidence_not_person_score():
    d = FallDetector()
    events, _ = run(d, FALL)
    fall = events[0].details
    # The synthetic skeleton's YOLO person score is 0.9; the fall score is separate.
    assert fall["person_detection_score"] == 0.9
    assert fall["fall_confidence"] != fall["person_detection_score"]
    assert 0 < fall["fall_confidence"] <= 1
    assert fall["fall_confidence"] == fall_evidence_score(
        fall["drop_torso_lengths"], fall["peak_drop_speed"], fall["torso_angle_deg"], d.cfg)


def test_weak_evidence_scores_lower_than_strong_evidence():
    strong = FallDetector()
    s_events, _ = run(strong, FALL)
    weak = FallDetector()
    # Just past the drop threshold, slower, ending only just inside "lying".
    w_events, _ = run(weak, [(1.5, STAND, STAND), (0.8, STAND, (505, 57)), (2, (505, 57), (505, 57))])
    s, w = s_events[0].details, w_events[0].details
    assert w["fall_confidence"] < s["fall_confidence"]
    assert w["evidence"] == "low"
    assert s["evidence"] in ("high", "very high")


def test_score_parts_are_bounded_and_monotonic():
    cfg = FallConfig()
    assert fall_evidence_score(0, 0, 0, cfg) == 0.0
    assert fall_evidence_score(10, 10, 120, cfg) == 1.0
    assert fall_evidence_score(1.0, 2.0, 70, cfg) < fall_evidence_score(1.5, 2.0, 70, cfg)
    assert fall_evidence_score(1.0, 2.0, 70, cfg) < fall_evidence_score(1.0, 3.0, 70, cfg)


def test_drop_and_peak_speed_cover_the_whole_fall():
    d = FallDetector()
    events, _ = run(d, FALL)
    details = events[0].details
    # The synthetic fall drops 150 px (1.5 torso lengths) in 0.5 s: 3.0 torso lengths/s.
    # The drop is measured until confirmation, not cut off when it first passes 1.0.
    assert abs(details["drop_torso_lengths"] - 1.5) < 0.05
    assert abs(details["peak_drop_speed"] - 3.0) < 0.1
    assert "vertical_velocity" not in details and "immobility_seconds" not in details


def test_lying_still_emits_one_immobility_event_linked_to_the_fall():
    d = FallDetector(fast_cfg())
    events, _ = fall_then(d, [(6, LYING, LYING), (10, LYING, LYING)])
    assert [e.type for e in events] == ["fall", "immobility"]
    fall, imm = events
    assert imm.fall_id == fall.fall_id
    assert 5 <= imm.details["immobility_seconds"] < 5.2
    assert imm.details["time_on_floor_s"] > imm.details["immobility_seconds"]
    assert d.phase_of(1) is Phase.FALLEN


def test_moving_on_the_floor_is_not_immobility():
    d = FallDetector(fast_cfg())
    shift = []
    for i in range(4):  # roll a little every 2 seconds
        x0, x1 = 550 + 40 * i, 550 + 40 * (i + 1)
        shift += [(1.8, (x0, 88), (x0, 88)), (0.2, (x0, 88), (x1, 88))]
    events, _ = fall_then(d, shift)
    assert [e.type for e in events] == ["fall"]


def test_small_keypoint_jitter_still_counts_as_immobile():
    d = FallDetector(fast_cfg())
    jitter = [(0.5, (550, 88), (556, 86)), (0.5, (556, 86), (548, 89))] * 7
    events, _ = fall_then(d, jitter)
    assert [e.type for e in events] == ["fall", "immobility"]


def test_recovery_reports_real_time_back_on_feet_and_links_to_fall():
    d = FallDetector()
    events, _ = fall_then(d, [(4.0, LYING, LYING), (1.0, LYING, STAND), (2.5, STAND, STAND)])
    fall, rec = events
    assert rec.type == "recovered" and rec.fall_id == fall.fall_id
    # Fall started at 1.5s, lay until 8s, counted as upright ~1.2s into the 1s get-up
    # plus its follow-on (torso <35° and box no longer wide) -> ~7.7s.
    # The 1.5s upright-confirmation wait afterwards is not counted.
    assert 7.0 < rec.details["recovery_seconds"] < 8.2
    assert rec.details["immobility_reported"] is False


def test_each_fall_gets_its_own_id():
    d = FallDetector()
    events, t = fall_then(d, [(1.0, LYING, STAND), (2.5, STAND, STAND)])
    more, _ = run(d, FALL, t0=t)
    falls = [e for e in events + more if e.type == "fall"]
    assert len(falls) == 2 and falls[0].fall_id != falls[1].fall_id


def test_fallen_person_lost_for_a_while_keeps_their_fall():
    d = FallDetector(fast_cfg())
    events, t = run(d, FALL)
    # Pose model loses the person lying on the floor for 10 s...
    for _ in range(round(10 * FPS)):
        t += 1 / FPS
        events += d.update([], t)
    # ...then finds them again under a new tracker ID, in the same place.
    more, _ = run(d, [(1.0, LYING, STAND), (2.5, STAND, STAND)], tid=42, t0=t)
    types = [(e.type, e.track_id) for e in events + more]
    assert types[0] == ("fall", 1) and types[-1] == ("recovered", 42)
    assert (events + more)[-1].fall_id == events[0].fall_id


def test_unseen_normal_person_is_still_forgotten_quickly():
    d = FallDetector()
    run(d, [(1, STAND, STAND)])
    d.update([], 10.0)
    assert d.tracks == {}
