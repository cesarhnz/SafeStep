"""Small, failure-isolated HTTP client for the existing alert service."""

from __future__ import annotations

import json
import os
import threading
from urllib.request import Request, urlopen


def send_event(url: str, event: dict, timeout: float = 3.0) -> dict:
    request = Request(
        url.rstrip("/") + "/events",
        data=json.dumps(event).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **({"X-API-Key": os.environ["SAFESTEP_ALERT_API_KEY"]}
               if os.getenv("SAFESTEP_ALERT_API_KEY") else {}),
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read() or b"{}")
    if not isinstance(payload, dict):
        raise ValueError("alert service returned a non-object response")
    return payload


def send_event_async(url: str, event: dict) -> None:
    """Keep alert-service/network latency out of the frame-processing loop."""
    def run() -> None:
        try:
            send_event(url, event)
        except Exception as exc:
            print(f"[safestep] alert service request failed: {exc}")

    threading.Thread(target=run, daemon=True).start()
