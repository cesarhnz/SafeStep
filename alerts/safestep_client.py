"""
Tiny client the detection engine (YOLO pose / fall detector) uses to talk to
the SafeStep alert service. Standard library only.

    from safestep_client import SafeStepClient
    ss = SafeStepClient("http://127.0.0.1:8080", camera_id="cam-1", room="Living room")
    ss.start_heartbeat()                       # lets the service notice if the engine dies

    inc = ss.fall(confidence=0.91, zone="near the couch")    # returns incident id
    ss.cancel(inc)                             # person waved "I'm OK" in the window
    ss.hazard("obstacle", details="Box on the floor in the walkway")
"""

from __future__ import annotations

import json
import threading
import urllib.request
from datetime import datetime, timezone


class SafeStepClient:
    def __init__(self, url: str = "http://127.0.0.1:8080", camera_id: str = "cam-1",
                 room: str = "Living room", api_key: str = "", timeout: float = 3.0):
        self.url = url.rstrip("/")
        self.camera_id = camera_id
        self.room = room
        self.api_key = api_key
        self.timeout = timeout
        self._hb_stop = threading.Event()

    # ---- low level --------------------------------------------------------
    def _post(self, path: str, body: dict) -> dict | None:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"{}")
        except Exception as e:  # never crash the CV loop over an alert call
            print(f"[safestep] POST {path} failed: {e}")
            return None

    def send(self, type: str, **fields) -> dict | None:
        evt = {"type": type, "room": self.room, "camera_id": self.camera_id,
               "timestamp": datetime.now(timezone.utc).isoformat(), **fields}
        return self._post("/events", evt)

    def send_async(self, type: str, **fields) -> threading.Thread:
        t = threading.Thread(target=self.send, args=(type,), kwargs=fields, daemon=True)
        t.start()
        return t

    # ---- emergencies (synchronous: you get the incident id back) ---------
    def fall(self, confidence: float, **fields) -> str | None:
        r = self.send("fall_detected", confidence=confidence, **fields)
        return r and r.get("incident_id")

    def help_request(self, **fields) -> str | None:
        r = self.send("help_request", **fields)
        return r and r.get("incident_id")

    def immobility(self, seconds_on_floor: float, **fields) -> str | None:
        r = self.send("immobility", details=f"On the floor for {int(seconds_on_floor)}s", **fields)
        return r and r.get("incident_id")

    def cancel(self, incident_id: str, by: str = "person") -> None:
        self._post(f"/incidents/{incident_id}/cancel", {"by": by})

    def review(self, incident_id: str, outcome: str, note: str | None = None,
               by: str = "caregiver") -> dict | None:
        """Review a cancelled/resolved fall: confirmed_fall, no_fall, or unsure."""
        return self._post(f"/incidents/{incident_id}/review",
                          {"outcome": outcome, "note": note, "by": by})

    # ---- potential dangers (fire and forget) ------------------------------
    def hazard(self, type: str, **fields) -> threading.Thread:
        """type: near_fall, obstacle, low_light, night_wandering, inactivity,
        or any custom type (pass title=... and advice=...)."""
        return self.send_async(type, **fields)

    # ---- heartbeat --------------------------------------------------------
    def start_heartbeat(self, every_s: float = 15) -> None:
        def loop():
            while not self._hb_stop.wait(every_s):
                self._post("/heartbeat", {"camera_id": self.camera_id, "room": self.room})
        self._post("/heartbeat", {"camera_id": self.camera_id, "room": self.room})
        threading.Thread(target=loop, daemon=True).start()

    def stop_heartbeat(self) -> None:
        self._hb_stop.set()
