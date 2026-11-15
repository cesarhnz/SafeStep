"""Translate detector results into the agreed SafeStep event schema."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .detector import FallEvent


def to_schema_event(event: FallEvent, device_id: str, room: str) -> dict:
    """Return the supplied event schema for a detector lifecycle event."""
    details = event.details
    return {
        "event_id": f"evt_{uuid.uuid4().hex}",
        "device_id": device_id,
        "room": room,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event_type": "possible_fall" if event.type == "fall" else "recovered",
        "confidence": float(details.get("confidence", 0.0)),
        "status": "detected",
        "person_detected": True,
        "metrics": {
            "torso_angle_deg": details.get("torso_angle_deg"),
            "vertical_velocity": details.get("vertical_velocity", 0.0),
            "lateral_velocity": details.get("lateral_velocity", 0.0),
            "immobility_seconds": details.get("immobility_seconds", 0.0),
            "recovery_seconds": details.get("recovery_seconds", 0.0),
        },
        "notification": {
            "alert_required": event.type == "fall",
            "alert_sent": False,
            "alert_sent_at": None,
            "acknowledged_at": None,
        },
        "review": {
            "confirmed_event_type": None,
            "reviewed_at": None,
        },
    }
