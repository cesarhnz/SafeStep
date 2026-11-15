"""Detector -> alert service contract (schema v1). Standard library only.

The detector sends the team-agreed event shape:

    event_id, device_id, room, timestamp, event_type, confidence,
    person_detected, metrics, notification, review

`status` and the notification state are owned by the alert service, so values
the detector sends for them are treated as an initial snapshot only. The
Databricks view derives current notification state from lifecycle rows.

The older dialect used by safestep_client.py ({"type": "fall", ...}) has no
`event_type` key and is passed through unchanged, so the simulator keeps working.
"""
from __future__ import annotations

from datetime import datetime

REQUIRED = ("event_id", "device_id", "room", "timestamp", "event_type",
            "confidence", "person_detected")
TYPE_MAP = {"possible_fall": "fall"}  # agreed name -> alert-service catalog name
PASSTHROUGH = ("zone", "person", "snapshot_url", "lat", "lng", "title", "advice")


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
        return payload  # legacy dialect (safestep_client.py)

    missing = [k for k in REQUIRED if k not in payload]
    if missing:
        raise ValueError("missing fields: " + ", ".join(missing))

    confidence = _number(payload, "confidence")
    if not 0 <= confidence <= 1:
        raise ValueError("'confidence' must be between 0 and 1")
    if not isinstance(payload["person_detected"], bool):
        raise ValueError("'person_detected' must be true or false")

    metrics = payload.get("metrics") or {}
    if not isinstance(metrics, dict):
        raise ValueError("'metrics' must be an object")
    immobility = payload.get("immobility_seconds", metrics.get("immobility_seconds"))
    if immobility is not None:
        immobility = _number({"immobility_seconds": immobility}, "immobility_seconds")
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
    metric_notes = []
    for key in ("torso_angle_deg", "vertical_velocity", "lateral_velocity",
                "immobility_seconds", "recovery_seconds"):
        value = metrics.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metric_notes.append(f"{key}={value}")
    if metric_notes:
        note = "Detector metrics: " + ", ".join(metric_notes)
        details = f"{details}. {note}" if details else note
    if immobility is not None:
        note = f"No movement for {immobility:.0f}s"
        details = f"{details}. {note}" if details else note

    evt = {
        "type": TYPE_MAP.get(raw_type, raw_type),
        "event_id": payload["event_id"].strip(),
        "device_id": payload["device_id"].strip(),
        "camera_id": payload.get("camera_id") or payload["device_id"].strip(),
        "room": payload["room"].strip(),
        "timestamp": str(payload["timestamp"]),
        "confidence": confidence,
        "person_detected": payload["person_detected"],
        "immobility_seconds": immobility,
        "details": details,
        "metrics": metrics,
        "review": payload.get("review") if isinstance(payload.get("review"), dict) else {},
        "source_payload": payload,
    }
    for key in PASSTHROUGH:
        if key in payload:
            evt[key] = payload[key]
    return evt
