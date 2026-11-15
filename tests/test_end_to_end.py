"""Detector -> schema events -> AlertClient (HTTP) -> real alert service.

Runs the alert service's HTTP handler on a local port with notifications
captured instead of sent, then drives it with synthetic falls.
"""

import json
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from safestep.alerts import AlertClient
from safestep.detector import FallConfig, FallDetector
from safestep.events import to_schema_event
from test_detector import FALL, STAND, run

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "alerts"))
import alert_service as service  # noqa: E402
from contracts import to_internal  # noqa: E402

LYING = (550, 88)


@pytest.fixture
def svc():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = {**service.DEFAULT_CONFIG, "event_log": str(Path(tmp) / "events.jsonl"),
               "ntfy_topic": "t", "ntfy_ack_topic": "t-ack", "confirm_window_s": 30}
        with patch.object(service, "CFG", cfg), \
                patch.object(service, "publish_async") as pub_async, \
                patch.object(service, "publish", return_value=True) as pub, \
                patch.object(service, "MANAGER", service.IncidentManager()) as manager:
            server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            client = AlertClient(f"http://127.0.0.1:{server.server_port}", "cam-test", "Bedroom",
                                 heartbeat_s=0.0, retry_delays=(0.1,))
            yield manager, client, pub_async, pub, Path(tmp) / "events.jsonl"
            server.shutdown()
            for inc in manager.incidents.values():
                manager._stop_timer(inc)
            time.sleep(0.05)


def wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def send_all(client, events):
    for e in events:
        client.send(to_schema_event(e, "cam-test", "Bedroom"))


def only_incident(manager):
    assert wait(lambda: len(manager.incidents) == 1)
    return next(iter(manager.incidents.values()))


def test_detector_payloads_satisfy_the_contract():
    d = FallDetector(FallConfig(immobility_s=3))
    events, t = run(d, FALL)
    more, _ = run(d, [(4, LYING, LYING), (1, LYING, STAND), (2.5, STAND, STAND)], t0=t)
    payloads = [to_schema_event(e, "cam-test", "Bedroom") for e in events + more]
    assert [p["event_type"] for p in payloads] == ["possible_fall", "immobility", "recovered"]
    fall, imm, rec = (to_internal(p) for p in payloads)
    assert imm["related_event_id"] == rec["related_event_id"] == fall["event_id"]
    assert fall["confidence"] == events[0].details["fall_confidence"]
    assert fall["immobility_seconds"] is None and imm["immobility_seconds"] >= 3
    for p in payloads:  # caregiver text is plain language, raw numbers stay in metrics
        assert "=" not in p["details"] and "_" not in p["details"]
        assert "review" not in p and "notification" not in p and "status" not in p
    assert "vertical_velocity" not in payloads[0]["metrics"]


def test_scenario_fall_and_stays_down(svc):
    manager, client, _, pub, log = svc
    d = FallDetector(FallConfig(immobility_s=3))
    events, _ = run(d, FALL + [(4, LYING, LYING)])
    send_all(client, events)
    inc = only_incident(manager)
    # Read under the service's lock so we see the finished state, not a half-done update.
    def escalating():
        with manager.lock:
            return inc.immobility_reported_at is not None and inc.timer is not None
    assert wait(escalating)  # urgent alert sent and escalation scheduled as normal
    assert inc.status == "alerted" and inc.immobility_seconds >= 3
    assert wait(lambda: pub.called)
    assert "STILL ON THE FLOOR" in pub.call_args.args[2]


def test_scenario_gets_up_during_countdown(svc):
    manager, client, pub_async, pub, _ = svc
    d = FallDetector()
    events, _ = run(d, FALL + [(1, LYING, STAND), (2.5, STAND, STAND)])
    send_all(client, events)
    inc = only_incident(manager)
    assert wait(lambda: inc.status == "cancelled")
    assert inc.recovered_at is not None
    manager._confirm_timeout(inc.id)  # countdown expiring afterwards: no urgent alert
    assert inc.status == "cancelled" and not pub.called


def test_scenario_gets_up_after_urgent_alert(svc):
    manager, client, *_ = svc
    d = FallDetector()
    events, t = run(d, FALL)
    send_all(client, events)
    inc = only_incident(manager)
    manager._confirm_timeout(inc.id)  # 30 s pass with no cancel
    assert inc.status == "alerted"
    more, _ = run(d, [(1, LYING, STAND), (2.5, STAND, STAND)], t0=t)
    send_all(client, more)
    assert wait(lambda: inc.status == "recovered")
    assert inc.timer is None


def test_scenario_uncertain_fall_has_low_evidence(svc):
    manager, client, *_ = svc
    d = FallDetector()
    events, _ = run(d, [(1.5, STAND, STAND), (0.8, STAND, (505, 57)), (2, (505, 57), (505, 57))])
    send_all(client, events)
    inc = only_incident(manager)
    assert inc.confidence < 0.5
    assert "Fall evidence: low" in inc.body()


def test_scenario_heartbeat_running_then_clean_stop(svc):
    manager, client, *_ = svc
    client.heartbeat(fps=29.0)
    assert wait(lambda: "cam-test" in manager.heartbeats)
    assert manager.heartbeats["cam-test"]["fps"] == 29.0
    client.close("stopped by user")
    offline = [i for i in manager.incidents.values() if i.type == "camera_offline"]
    assert len(offline) == 1 and "stopped by user" in offline[0].details
    # The watchdog later sees the silence but doesn't alert a second time.
    manager.heartbeats["cam-test"]["t"] -= 999
    manager._check_cameras()
    assert len([i for i in manager.incidents.values() if i.type == "camera_offline"]) == 1


def test_lifecycle_log_carries_detector_metrics_for_analytics(svc):
    manager, client, _, _, log = svc
    d = FallDetector()
    events, _ = run(d, FALL + [(1, LYING, STAND), (2.5, STAND, STAND)])
    send_all(client, events)
    inc = only_incident(manager)
    assert wait(lambda: inc.status == "cancelled")
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    detected = next(r for r in rows if r["lifecycle"] == "detected")
    recovered = next(r for r in rows if r["lifecycle"] == "recovered")
    assert detected["raw_event"]["metrics"]["peak_drop_speed"] > 0
    assert recovered["raw_event"]["metrics"]["recovery_seconds"] > 0
    assert recovered["by"] == service.DETECTOR
