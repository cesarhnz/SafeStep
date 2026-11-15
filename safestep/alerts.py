"""Failure-isolated HTTP client for the alert service (alerts/alert_service.py).

Nothing here can block or crash the frame loop:

* Events go through ONE background worker in order, so a "recovered" or
  "immobility" follow-up can never overtake the fall it belongs to. Network
  failures are retried with backoff; the alert service ignores repeated
  event_ids, so a retry can't create a duplicate incident.
* Heartbeats are sent from the frame loop (`heartbeat()` is called once per
  processed frame and only posts every `heartbeat_s`). If frames stop being
  processed (crash, frozen or unplugged camera) the heartbeats stop, and the
  alert service's own watchdog raises the offline alert.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.error
from urllib.request import Request, urlopen


def _headers() -> dict:
    key = os.getenv("SAFESTEP_ALERT_API_KEY")
    return {"Content-Type": "application/json", **({"X-API-Key": key} if key else {})}


def post_json(url: str, path: str, body: dict, timeout: float = 3.0) -> dict:
    request = Request(url.rstrip("/") + path, data=json.dumps(body).encode("utf-8"),
                      headers=_headers(), method="POST")
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read() or b"{}")
    if not isinstance(payload, dict):
        raise ValueError("alert service returned a non-object response")
    return payload


def send_event(url: str, event: dict, timeout: float = 3.0) -> dict:
    return post_json(url, "/events", event, timeout)


class AlertClient:
    def __init__(self, url: str, device_id: str, room: str, heartbeat_s: float = 15.0,
                 timeout: float = 3.0, retry_delays: tuple[float, ...] = (1, 2, 4, 8, 15)):
        self.url = url
        self.device_id = device_id
        self.room = room
        self.heartbeat_s = heartbeat_s
        self.timeout = timeout
        self.retry_delays = retry_delays
        self._queue: queue.Queue[dict | None] = queue.Queue()
        self._last_hb = float("-inf")
        self._hb_thread: threading.Thread | None = None
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    # ---- events -----------------------------------------------------------
    def send(self, event: dict) -> None:
        self._queue.put(event)

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            try:
                if event is None:
                    return
                self._deliver(event)
            finally:
                self._queue.task_done()

    def _deliver(self, event: dict) -> None:
        for attempt, delay in enumerate((0, *self.retry_delays)):
            time.sleep(delay)
            try:
                result = send_event(self.url, event, self.timeout)
                print(f"[safestep] {event['event_type']} -> alert service: {result}", flush=True)
                return
            except urllib.error.HTTPError as exc:
                if exc.code < 500:  # the service rejected it; resending won't help
                    print(f"[safestep] alert service rejected {event['event_id']}: "
                          f"{exc.code} {exc.read().decode(errors='replace')}", flush=True)
                    return
                err = exc
            except Exception as exc:  # connection refused, timeout, bad response...
                err = exc
            print(f"[safestep] alert service request failed (attempt {attempt + 1}): {err}", flush=True)
        print(f"[safestep] GAVE UP sending {event['event_type']} {event['event_id']}", flush=True)

    # ---- heartbeat --------------------------------------------------------
    def heartbeat(self, fps: float | None = None) -> None:
        """Call once per processed frame; posts at most every heartbeat_s."""
        now = time.monotonic()
        if now - self._last_hb < self.heartbeat_s:
            return
        if self._hb_thread and self._hb_thread.is_alive():
            return  # previous heartbeat still in flight; don't pile up threads
        self._last_hb = now
        body = {"camera_id": self.device_id, "room": self.room, "status": "running",
                "fps": round(fps, 1) if fps else None}
        self._hb_thread = threading.Thread(target=self._post_heartbeat, args=(body,), daemon=True)
        self._hb_thread.start()

    def _post_heartbeat(self, body: dict) -> None:
        try:
            post_json(self.url, "/heartbeat", body, self.timeout)
        except Exception as exc:
            print(f"[safestep] heartbeat failed: {exc}", flush=True)

    # ---- shutdown ---------------------------------------------------------
    def close(self, reason: str = "detector stopped", drain_s: float = 5.0) -> None:
        """Flush queued events, then tell the service monitoring has stopped on purpose."""
        deadline = time.monotonic() + drain_s
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.05)
        self._queue.put(None)
        if self._hb_thread:
            self._hb_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self._post_heartbeat({"camera_id": self.device_id, "room": self.room,
                              "status": "stopped", "reason": reason})
