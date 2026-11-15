#!/usr/bin/env python3
"""Generate 100 synthetic incidents for one fictional resident, without sending alerts.

Write a separate lifecycle log for the existing analytics/ingest.py shipper.
The same end date produces identical rows, so ingestion can be safely replayed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
from random import Random
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
PERSON = "Maria Demo (fictional)"
LOCAL = ZoneInfo("America/Chicago")
SCENARIOS = (
    ["fall_detected"] * 8 + ["near_fall"] * 4 + ["immobility", "help_request"]
    + ["low_light"] * 2 + ["night_wandering", "obstacle", "obstacle", "camera_offline"]
)
ROOMS = ("Bedroom", "Bathroom", "Hallway", "Living room", "Kitchen")


def generate(end_date: date) -> list[dict]:
    """Return lifecycle snapshots for exactly 100 incidents across 28 complete days."""
    rng = Random(42)
    events = []
    number = 0
    # Weekly totals: 37, 28, 21, 14. This is a staged demo trend, not patient data.
    for day in range(28):
        week = day // 7
        count = 5 - week + (1 if day < 2 else 0)
        local_day = end_date - timedelta(days=27 - day)
        for slot in range(count):
            number += 1
            event_type = SCENARIOS[(number - 1) % len(SCENARIOS)]
            room = ROOMS[(number + day) % len(ROOMS)]
            hour = (6, 9, 13, 17, 20, 23)[slot]
            if event_type in ("night_wandering", "low_light"):
                hour = (1, 3, 22)[number % 3]
            elif event_type == "fall_detected" and number % 3 == 0:
                hour = (2, 7, 21)[number % 3]
            detected = datetime.combine(local_day, time(hour, rng.randrange(5, 50)), LOCAL)
            # Store UTC like the real lifecycle log, including the DST transition.
            detected = detected.astimezone(timezone.utc)
            base = dict(
                incident_id=f"DEMO-MARIA-{end_date:%Y%m%d}-{number:03d}",
                source_event_id=f"demo-maria-{end_date:%Y%m%d}-{number:03d}",
                device_id="demo-maria-" + room.lower().replace(" ", "-"),
                camera_id="demo-cam-" + room.lower().replace(" ", "-"),
                person=PERSON, type=event_type, room=room,
                zone=f"Demo {room.lower()} monitoring area",
                detected_at=detected.isoformat(),
                confidence=round(rng.uniform(0.40, 0.99), 3),
                person_detected=event_type != "camera_offline",
                immobility_seconds=rng.randint(65, 180) if event_type == "immobility" else 0,
                details=f"SYNTHETIC DEMO ONLY: {event_type.replace('_', ' ')}; fictional resident; week {week + 1}.",
                schema_version=1, status="pending_confirmation" if event_type == "fall_detected" else "new",
                priority=5 if event_type in ("fall_detected", "immobility", "help_request") else 3,
                escalations=0, acked_by=None,
            )

            def emit(lifecycle, seconds, **updates):
                base.update(updates)
                events.append({**base, "lifecycle": lifecycle,
                               "logged_at": (detected + timedelta(seconds=seconds)).isoformat()})

            emit("detected", 0)
            cancelled = event_type == "fall_detected" and number % 4 == 0
            if cancelled:
                emit("cancelled", 12, status="cancelled", by=PERSON,
                     outcome="person_cancelled")
                closed_at = 12
            else:
                alert_delay = 20 if event_type == "fall_detected" else 2
                emit("alerted", alert_delay, status="alerted")
                failed = number % 13 == 0
                undelivered = number in (39, 78)
                if failed:
                    emit("delivery_failed", alert_delay + 1, outcome="demo_transport_failure")
                if not undelivered:
                    emit("delivered", alert_delay + (8 if failed else 1), outcome="demo_accepted")
                # Two unresolved delivery failures and two escalated alerts populate queues.
                if undelivered or number in (97, 98):
                    emit("escalated", alert_delay + 600, status="escalated", escalations=1)
                    continue
                ack_delay = (230, 160, 95, 40)[week] + rng.randint(5, 30)
                if week < 2 and number % 3 == 0:
                    emit("escalated", alert_delay + 120, status="escalated", escalations=1)
                emit("acknowledged", alert_delay + ack_delay, status="acknowledged",
                     acked_by="Demo caregiver", by="Demo caregiver")
                closed_at = alert_delay + ack_delay + rng.randint(60, 240)
                emit("resolved", closed_at, status="resolved", by="Demo caregiver")

            if event_type in ("fall_detected", "near_fall") and number % 5 != 0:
                outcome = "no_fall" if cancelled or event_type == "near_fall" else (
                    "unsure" if number % 3 == 0 else "confirmed_fall")
                note = {
                    "no_fall": "Resident sat down deliberately or recovered balance; no fall reported.",
                    "unsure": "Camera view was partially obscured; caregiver could not confirm a fall.",
                    "confirmed_fall": "Caregiver reported a fall and assisted the resident in this fictional scenario.",
                }[outcome]
                emit("reviewed", closed_at + 120, review_outcome=outcome,
                     review_note="SYNTHETIC DEMO: " + note, reviewed_by="Demo caregiver",
                     reviewed_at=(detected + timedelta(seconds=closed_at + 120)).isoformat())

    return sorted(events, key=lambda row: (row["logged_at"], row["incident_id"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end-date", type=date.fromisoformat,
                        default=datetime.now(LOCAL).date() - timedelta(days=1),
                        help="last demo day (YYYY-MM-DD); defaults to yesterday in Chicago")
    parser.add_argument("--output", type=Path, default=HERE / "demo/events.jsonl")
    args = parser.parse_args()
    if args.end_date >= datetime.now(LOCAL).date():
        parser.error("choose a completed day to avoid future lifecycle timestamps")
    rows = generate(args.end_date)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    detected = [row for row in rows if row["lifecycle"] == "detected"]
    print(json.dumps(dict(person=PERSON, incidents=len(detected), lifecycle_rows=len(rows),
                          first_day=str(args.end_date - timedelta(days=27)), last_day=str(args.end_date),
                          event_types=dict(Counter(row["type"] for row in detected)),
                          output=str(args.output)), indent=2))


if __name__ == "__main__":
    main()
