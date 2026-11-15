"""Alert-service handling of detector follow-ups (recovery, immobility) and heartbeats."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import alert_service as service
from contracts import to_internal


def payload(event_type, event_id, **extra):
    base = {"event_id": event_id, "device_id": "cam-1", "room": "Bedroom",
            "timestamp": "2026-11-15T10:00:00Z", "event_type": event_type,
            "confidence": 0.78, "person_detected": True}
    return to_internal({**base, **extra})


FALL = lambda eid="evt_a": payload("possible_fall", eid, details="Rapid drop of 1.5 torso lengths; ended up lying down (88°).",
                                    metrics={"drop_torso_lengths": 1.5, "peak_drop_speed": 3.0})
RECOVERED = lambda eid="evt_a", secs=7.7: payload("recovered", f"{eid}_recovered", related_event_id=eid,
                                                  details=f"Got back up {secs:.0f}s after the fall.",
                                                  metrics={"recovery_seconds": secs})
IMMOBILE = lambda eid="evt_a": payload("immobility", f"{eid}_immobility", related_event_id=eid,
                                       immobility_seconds=60.0,
                                       details="No meaningful movement for 60s; on the floor for 63s since the fall.")


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "events.jsonl"
        config = {**service.DEFAULT_CONFIG, "event_log": str(self.path), "ntfy_topic": "test",
                  "ntfy_ack_topic": "test-ack", "confirm_window_s": 30, "escalate_after_s": 120}
        for target, value in (("CFG", config),):
            p = patch.object(service, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.publish_async = patch.object(service, "publish_async").start()
        self.publish = patch.object(service, "publish", return_value=True).start()
        self.addCleanup(patch.stopall)
        self.m = service.IncidentManager()
        self.addCleanup(self._stop_timers)

    def _stop_timers(self):
        for inc in self.m.incidents.values():
            self.m._stop_timer(inc)
        time.sleep(0.05)  # let notify threads finish writing before the temp dir goes

    def rows(self):
        time.sleep(0.05)  # delivery rows are written from notify threads
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def lifecycles(self, inc_id):
        return [r["lifecycle"] for r in self.rows() if r["incident_id"] == inc_id]

    def titles(self):
        return [c.args[1] for c in self.publish_async.call_args_list] + \
               [c.args[1] for c in self.publish.call_args_list]


class FollowUpTests(Base):
    # -- scenario 1: falls and stays on the floor ---------------------------
    def test_immobility_during_countdown_alerts_immediately_once(self):
        inc_id = self.m.handle_event(FALL())["incident_id"]
        inc = self.m.incidents[inc_id]
        self.assertEqual(inc.status, "pending_confirmation")
        r = self.m.handle_event(IMMOBILE())
        self.assertEqual(r, {"status": "alerted", "incident_id": inc_id})  # same incident
        self.assertEqual(inc.immobility_seconds, 60.0)
        self.assertIsNotNone(inc.timer)  # escalation scheduled as for any urgent alert
        self.assertIn("STILL ON THE FLOOR", self.publish.call_args.args[2])
        # A retry of the same event, or a second immobility report, does nothing new.
        self.assertEqual(self.m.handle_event(IMMOBILE())["status"], "duplicate")
        self.assertEqual(self.m.handle_event(payload("immobility", "other", related_event_id="evt_a",
                                                     immobility_seconds=90.0))["status"], "duplicate")
        self.assertEqual(self.lifecycles(inc_id).count("immobility"), 1)
        self.assertEqual(len(self.m.incidents), 1)

    def test_immobility_after_alert_sends_urgent_update_and_keeps_escalating(self):
        inc_id = self.m.handle_event(FALL())["incident_id"]
        self.m._confirm_timeout(inc_id)
        inc = self.m.incidents[inc_id]
        self.m.handle_event(IMMOBILE())
        time.sleep(0.05)
        kwargs = self.publish.call_args.kwargs
        self.assertEqual(self.publish.call_args.args[1], "PERSON STILL ON THE FLOOR (Bedroom)")
        self.assertEqual(kwargs["priority"], 5)
        self.assertEqual(inc.status, "alerted")
        self.assertIsNotNone(inc.timer)
        self.m._escalate(inc_id)
        self.assertEqual(inc.status, "escalated")

    def test_immobility_after_person_cancelled_reopens_the_alert(self):
        inc_id = self.m.handle_event(FALL())["incident_id"]
        self.m.cancel(inc_id)
        self.m.handle_event(IMMOBILE())
        self.assertEqual(self.m.incidents[inc_id].status, "alerted")
        self.assertIn("cancelled, but they have not moved", self.publish.call_args.args[2])

    def test_immobility_for_unknown_fall_still_alerts(self):
        r = self.m.handle_event(IMMOBILE("evt_never_seen"))
        inc = self.m.incidents[r["incident_id"]]
        self.assertEqual((inc.type, inc.status), ("immobility", "alerted"))

    # -- scenario 2: gets up during the countdown ----------------------------
    def test_recovery_during_countdown_cancels_and_urgent_alert_never_fires(self):
        inc_id = self.m.handle_event(FALL())["incident_id"]
        inc = self.m.incidents[inc_id]
        r = self.m.handle_event(RECOVERED())
        self.assertEqual(r["status"], "cancelled")
        self.assertIsNone(inc.timer)
        self.assertIsNotNone(inc.recovered_at)
        # The countdown callback racing in after the recovery must not alert.
        self.m._confirm_timeout(inc_id)
        self.assertEqual(inc.status, "cancelled")
        lifecycle = self.lifecycles(inc_id)
        self.assertNotIn("alerted", lifecycle)
        self.assertEqual(lifecycle[:3], ["detected", "recovered", "cancelled"])
        cancelled = next(r for r in self.rows() if r["lifecycle"] == "cancelled")
        self.assertEqual(cancelled["by"], service.DETECTOR)
        self.assertTrue(any("The urgent alert was not sent" in c.args[2]
                            for c in self.publish_async.call_args_list))
        self.assertIn(f"Review incident {inc_id}: Bedroom", self.titles())

    def test_recovery_with_a_real_countdown_timer(self):
        service.CFG["confirm_window_s"] = 0.3
        inc_id = self.m.handle_event(FALL())["incident_id"]
        self.m.handle_event(RECOVERED())
        time.sleep(0.5)
        self.assertEqual(self.m.incidents[inc_id].status, "cancelled")
        self.publish.assert_not_called()  # no urgent alert went out

    def test_no_recovery_keeps_normal_escalation(self):
        service.CFG["confirm_window_s"] = 0.2
        inc_id = self.m.handle_event(FALL())["incident_id"]
        time.sleep(0.4)
        inc = self.m.incidents[inc_id]
        self.assertEqual(inc.status, "alerted")
        self.assertEqual(self.publish.call_args.args[1], service.CATALOG["fall"]["title"])
        self.assertIsNotNone(inc.timer)

    # -- scenario 3: gets up after the urgent alert ---------------------------
    def test_recovery_after_alert_marks_recovered_and_stops_reminders(self):
        inc_id = self.m.handle_event(FALL())["incident_id"]
        self.m._confirm_timeout(inc_id)
        inc = self.m.incidents[inc_id]
        self.m.handle_event(RECOVERED())
        self.assertEqual(inc.status, "recovered")
        self.assertIsNone(inc.timer)
        self.m._escalate(inc_id)  # a reminder timer racing in does nothing
        self.assertEqual((inc.status, inc.escalations), ("recovered", 0))
        self.assertIn("Update: they got back up (Bedroom)", self.titles())
        # Still a closed, reviewable incident; a caregiver can still respond to it.
        self.m.acknowledge(inc_id, by="Sam")
        self.assertEqual((inc.status, inc.acked_by), ("recovered", "Sam"))
        self.m.review(inc_id, "confirmed_fall", by="Sam")
        self.assertEqual(inc.review_outcome, "confirmed_fall")
        self.m.resolve(inc_id, by="Sam")
        self.assertEqual(inc.status, "resolved")

    def test_recovery_after_acknowledgement_keeps_status_and_informs_caregiver(self):
        inc_id = self.m.handle_event(FALL())["incident_id"]
        self.m._confirm_timeout(inc_id)
        self.m.acknowledge(inc_id, by="Sam")
        self.m.handle_event(RECOVERED())
        inc = self.m.incidents[inc_id]
        self.assertEqual(inc.status, "acknowledged")
        self.assertIsNotNone(inc.recovered_at)
        self.assertIn("recovered", self.lifecycles(inc_id))

    def test_recovery_for_unknown_fall_is_ignored(self):
        self.assertEqual(self.m.handle_event(RECOVERED("evt_never_seen"))["status"], "ignored")
        self.assertEqual(self.m.incidents, {})

    def test_recovery_links_to_merged_repeat_fall(self):
        first = self.m.handle_event(FALL("evt_a"))["incident_id"]
        self.assertEqual(self.m.handle_event(FALL("evt_b"))["status"], "duplicate")
        self.m.handle_event(RECOVERED("evt_b"))
        self.assertEqual(self.m.incidents[first].status, "cancelled")

    # -- caregiver-facing text -------------------------------------------------
    def test_alert_text_shows_evidence_band_and_plain_details(self):
        inc = self.m.incidents[self.m.handle_event(FALL())["incident_id"]]
        body = inc.body()
        self.assertIn("Fall evidence: high (score 0.78 of 1; a detection score, not a probability)", body)
        self.assertIn("Details: Rapid drop of 1.5 torso lengths; ended up lying down (88°).", body)
        self.assertNotIn("%", body)
        self.assertNotIn("No movement for", body)
        self.assertNotIn("=", body)

    def test_detector_review_and_notification_objects_are_not_kept(self):
        evt = payload("possible_fall", "evt_r", review={"confirmed_event_type": None},
                      notification={"alert_sent": False}, status="detected")
        inc = self.m.incidents[self.m.handle_event(evt)["incident_id"]]
        self.assertNotIn("review", inc.to_dict())
        self.assertIsNone(inc.review_outcome)

    def test_contract_rejects_bad_links(self):
        with self.assertRaises(ValueError):
            payload("recovered", "x")  # needs related_event_id
        with self.assertRaises(ValueError):
            payload("possible_fall", "y", related_event_id="evt_a")
        with self.assertRaises(ValueError):
            payload("immobility", "z", related_event_id="")

    def test_immobility_text_only_when_measured(self):
        self.assertEqual(payload("possible_fall", "a", immobility_seconds=0.0)["details"], "")
        self.assertEqual(payload("immobility", "b", immobility_seconds=75.0)["details"], "No movement for 75s")


class HeartbeatTests(Base):
    def offline_incidents(self):
        return [i for i in self.m.incidents.values() if i.type == "camera_offline"]

    # -- scenario 4: detector crashes / stops processing frames ----------------
    def test_silence_raises_one_offline_alert(self):
        self.m.heartbeat("cam-1", "Bedroom")
        self.m.heartbeats["cam-1"]["t"] -= service.CFG["heartbeat_timeout_s"] + 1
        self.m._check_cameras()
        self.m._check_cameras()
        self.assertEqual(len(self.offline_incidents()), 1)
        self.assertIn("No data from the detection engine", self.offline_incidents()[0].details)

    def test_clean_shutdown_alerts_immediately_without_duplicates(self):
        self.m.heartbeat("cam-1", "Bedroom", fps=29.5)
        self.m.heartbeat("cam-1", "Bedroom", status="stopped", reason="stopped by user")
        self.m.heartbeat("cam-1", "Bedroom", status="stopped", reason="stopped by user")
        self.m.heartbeats["cam-1"]["t"] -= service.CFG["heartbeat_timeout_s"] + 1
        self.m._check_cameras()  # the watchdog doesn't add a second alert
        offline = self.offline_incidents()
        self.assertEqual(len(offline), 1)
        self.assertIn("shut down (stopped by user)", offline[0].details)

    # -- scenario 5: detector resumes ------------------------------------------
    def test_resume_sends_back_online_once_and_new_outage_alerts_again(self):
        self.m.heartbeat("cam-1", "Bedroom")
        self.m.heartbeats["cam-1"]["t"] -= 999
        self.m._check_cameras()
        self.m.heartbeat("cam-1", "Bedroom")
        self.m.heartbeat("cam-1", "Bedroom")
        back = [t for t in self.titles() if "back online" in t]
        self.assertEqual(back, ["SafeStep camera back online (Bedroom)"])
        self.assertFalse(self.m.heartbeats["cam-1"]["offline"])
        # A later outage is a new episode and alerts again (not muted by the cooldown).
        self.m.heartbeats["cam-1"]["t"] -= 999
        self.m._check_cameras()
        self.assertEqual(len(self.offline_incidents()), 2)

    def test_bad_heartbeat_status_rejected(self):
        with self.assertRaises(ValueError):
            self.m.heartbeat("cam-1", "Bedroom", status="sleepy")


if __name__ == "__main__":
    unittest.main()
