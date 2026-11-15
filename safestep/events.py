"""Translate detector results into the agreed SafeStep event schema (v1).

Every event carries the contract fields (event_id, device_id, room, timestamp,
event_type, confidence, person_detected), a caregiver-readable `details`
sentence, and the raw numbers in `metrics` for analytics.

Follow-up events ("immobility", "recovered") carry `related_event_id`, the
event_id of the fall they belong to, so the alert service attaches them to the
same incident. Event IDs are derived from the detector's fall_id, which makes
retries idempotent: resending an event can never create a second incident.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .detector import FallEvent

EVENT_TYPES = {"fall": "possible_fall", "immobility": "immobility", "recovered": "recovered"}


def fall_event_id(fall_id: str) -> str:
    return f"evt_{fall_id}"


def describe(event: FallEvent) -> str:
    """One plain-language sentence for the caregiver notification."""
    d = event.details
    if event.type == "fall":
        return (f"Rapid drop of {d['drop_torso_lengths']:.1f} torso lengths; "
                f"ended up lying down ({d['torso_angle_deg']:.0f}°).")
    if event.type == "immobility":
        return (f"No meaningful movement for {d['immobility_seconds']:.0f}s; "
                f"on the floor for {d['time_on_floor_s']:.0f}s since the fall.")
    if event.type == "recovered":
        return f"Got back up {d['recovery_seconds']:.0f}s after the fall."
    return ""


def to_schema_event(event: FallEvent, device_id: str, room: str) -> dict:
    """Return the agreed schema payload for a detector event."""
    if event.type not in EVENT_TYPES:
        raise ValueError(f"unknown detector event type {event.type!r}")
    d = event.details
    base_id = fall_event_id(d["fall_id"])
    payload = {
        "event_id": base_id if event.type == "fall" else f"{base_id}_{event.type}",
        "device_id": device_id,
        "room": room,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event_type": EVENT_TYPES[event.type],
        # Fall evidence score from the detector (heuristic, 0..1). Follow-up
        # events repeat the score of the fall they belong to.
        "confidence": float(d["fall_confidence"]),
        "person_detected": True,
        "details": describe(event),
    }
    if event.type == "fall":
        payload["metrics"] = {k: d[k] for k in (
            "drop_torso_lengths", "peak_drop_speed", "peak_lateral_speed",
            "torso_angle_deg", "person_detection_score")}
    else:
        payload["related_event_id"] = base_id
        if event.type == "immobility":
            payload["immobility_seconds"] = d["immobility_seconds"]
            payload["metrics"] = {"time_on_floor_s": d["time_on_floor_s"]}
        else:
            payload["metrics"] = {"recovery_seconds": d["recovery_seconds"]}
    return payload
