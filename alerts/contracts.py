"""Detector -> alert service contract (schema v1). Standard library only.

The detector sends the team-agreed event shape:

    event_id, device_id, room, timestamp, event_type, confidence,
    person_detected, immobility_seconds   (+ optional zone, person, snapshot_url, lat, lng)

`status`, `alert_sent` and `acknowledged_at` are owned by the alert service,
so anything the detector sends for them is ignored. They are derived in
Databricks from the lifecycle log (see analytics/sql/schema.sql).

The older dialect used by safestep_client.py ({"type": "fall_detected", ...}) has no
`event_type` key. Its fields are preserved; only historical fall names are normalized.
"""
from __future__ import annotations

from datetime import datetime

REQUIRED = ("event_id", "device_id", "room", "timestamp", "event_type",
            "confidence", "person_detected")
TYPE_MAP = {"fall": "fall_detected", "possible_fall": "fall_detected"}
PASSTHROUGH = ("zone", "person", "snapshot_url", "lat", "lng", "title", "advice")


def canonical_event_type(value):
    """Read old event names while emitting the current fall_detected name."""
    return TYPE_MAP.get(value, value) if isinstance(value, str) else value


def _number(payload: dict, key: str) -> float:
    v = payload[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"'{key}' must be a number, got {v!r}")
    return float(v)


def to_internal(payload: dict) -> dict:
    """Validate a detector payload and return the alert service's internal event."""
    if not isinstance(payload, dict):
        raise ValueError("event must be a JSON object")
    if "event_type" not in payload and "type" in payload:
        return {**payload, "type": canonical_event_type(payload["type"])}

    missing = [k for k in REQUIRED if k not in payload]
    if missing:
        raise ValueError("missing fields: " + ", ".join(missing))

    confidence = _number(payload, "confidence")
    if not 0 <= confidence <= 1:
        raise ValueError("'confidence' must be between 0 and 1")
    if not isinstance(payload["person_detected"], bool):
        raise ValueError("'person_detected' must be true or false")

    immobility = payload.get("immobility_seconds")
    if immobility is not None:
        immobility = _number(payload, "immobility_seconds")
        if immobility < 0:
            raise ValueError("'immobility_seconds' must be >= 0")

    try:
        datetime.fromisoformat(str(payload["timestamp"]).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("'timestamp' must be ISO 8601, e.g. 2026-11-14T20:15:00Z")

    for key in ("event_id", "device_id", "room", "event_type"):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise ValueError(f"'{key}' must be a non-empty string")

    raw_type = payload["event_type"].strip().lower()
    details = str(payload.get("details") or "")
    if immobility is not None:
        note = f"No movement for {immobility:.0f}s"
        details = f"{details}. {note}" if details else note

    evt = {
        "type": canonical_event_type(raw_type),
        "event_id": payload["event_id"].strip(),
        "device_id": payload["device_id"].strip(),
        "camera_id": payload.get("camera_id") or payload["device_id"].strip(),
        "room": payload["room"].strip(),
        "timestamp": str(payload["timestamp"]),
        "confidence": confidence,
        "person_detected": payload["person_detected"],
        "immobility_seconds": immobility,
        "details": details,
    }
    for key in PASSTHROUGH:
        if key in payload:
            evt[key] = payload[key]
    return evt