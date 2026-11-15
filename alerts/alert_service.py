#!/usr/bin/env python3
"""
SafeStep Alert Service
======================

Receives events from the SafeStep detection engine (pose estimation / fall
detection) over HTTP and turns them into caregiver alerts on ntfy.

    detection engine --POST /events--> alert service --> ntfy --> caregiver phone
                                              |
                                              +--> data/events.jsonl  (for Databricks)

Features
  * Fall confirmation window: the person gets N seconds to cancel a false alarm
    before the caregiver gets the URGENT alert.
  * Immediate alerts for help requests and long time on the floor.
  * Lower-priority "potential danger" alerts (obstacles, darkness, stumbles,
    night wandering, inactivity, camera offline) with cooldowns so they
    don't spam the caregiver.
  * Where / when / confidence / what-to-do in every message.
  * "I'm on my way" button in the notification. Tapping it acknowledges the
    incident (through a second ntfy topic, so the phone doesn't need to reach
    this server). If nobody acknowledges in time, the service escalates to a
    backup caregiver topic and keeps reminding.
  * Camera heartbeat watchdog: alerts if the detection engine goes silent.
  * Every lifecycle change is appended to data/events.jsonl for analytics.
    This never blocks or delays an alert.

Changes in this version
  * Accepts the team-agreed detector schema (see contract.py); the old
    {"type": ...} payloads still work.
  * Retries with the same event_id are ignored; falls with person_detected=false are ignored.
  * A second fall in a room is only treated as "the same emergency" for
    fall_dedupe_s seconds. Before, one unacknowledged fall silenced every later
    fall in that room until the service restarted.
  * Logs `delivered` / `delivery_failed` after each alert, so analytics can tell
    "we decided to alert" from "ntfy actually accepted it".
  * A malformed confidence/timestamp can no longer stop an alert from being built.
  * Timer and watchdog threads log errors instead of dying silently.
  * GET /health no longer reveals the private ntfy topic; GET /incidents needs the API key.

Standard library only. Python 3.9+.
"""

from __future__ import annotations

import json
import logging
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from contracts import to_internal

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

log = logging.getLogger("safestep")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",                 # primary caregiver topic (auto-generated)
    "ntfy_escalation_topic": "",      # backup caregiver topic ("" = disabled)
    "ntfy_ack_topic": "",             # defaults to "<ntfy_topic>-ack"
    "ntfy_token": "",                 # access token if your ntfy server needs one
    "home_name": "Home",              # shown in every alert, e.g. "Grandma's house"
    "host": "0.0.0.0",
    "port": 8080,
    "api_key": "",                    # if set, the engine must send X-API-Key
    "confirm_window_s": 30,           # time the person has to cancel a fall alert
    "escalate_after_s": 120,          # no ack in this time -> escalate
    "max_escalations": 3,
    "fall_dedupe_s": 600,             # a repeat fall in the same room within this long is the same incident
    "hazard_cooldown_s": 300,         # same hazard in same room is muted this long
    "heartbeat_timeout_s": 60,        # camera silent this long -> offline alert
    "event_log": "data/events.jsonl",
    "dry_run": False,                 # True = print notifications instead of sending
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    changed = False
    if not cfg["ntfy_topic"]:
        # Topics on ntfy.sh are public to anyone who knows the name, so use
        # a random name nobody can guess.
        cfg["ntfy_topic"] = "safestep-" + secrets.token_urlsafe(9).replace("_", "x").replace("-", "y").lower()
        changed = True
    if not CONFIG_PATH.exists() or changed:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    if not cfg["ntfy_ack_topic"]:
        cfg["ntfy_ack_topic"] = cfg["ntfy_topic"] + "-ack"
    cfg["ntfy_server"] = cfg["ntfy_server"].rstrip("/")
    return cfg


CFG: dict = {}

# --------------------------------------------------------------------------
# Event catalog: what each event type means to a caregiver
# --------------------------------------------------------------------------
# priority: 5 urgent, 4 high, 3 default, 2 low, 1 min (ntfy priorities)
# confirm:  wait for the confirmation window before alerting
# escalate: re-alert / notify backup caregiver until acknowledged
# cooldown: mute repeats of the same type in the same room

CALL_911 = "If they are unresponsive, not breathing normally, bleeding heavily, or hit their head: call 911."

CATALOG: dict[str, dict] = {
    "fall": {
        "title": "FALL DETECTED - IMMEDIATE HELP NEEDED",
        "priority": 5, "tags": ["rotating_light", "sos"],
        "confirm": True, "escalate": True, "cooldown": False,
        "advice": [
            "Go to them or call them right now.",
            CALL_911,
            "Don't lift them if they may be hurt. Keep them warm and still until help arrives.",
            "Tap \"I'm on my way\" so other caregivers know someone is responding.",
        ],
    },
    "help_request": {
        "title": "HELP REQUESTED",
        "priority": 5, "tags": ["sos", "raised_hand"],
        "confirm": False, "escalate": True, "cooldown": False,
        "advice": [
            "They signaled that they need help. Go to them or call them now.",
            CALL_911,
        ],
    },
    "immobility": {
        "title": "PERSON STILL ON THE FLOOR",
        "priority": 5, "tags": ["rotating_light", "hourglass"],
        "confirm": False, "escalate": True, "cooldown": False,
        "advice": [
            "They have not gotten up after going to the floor. Go to them now.",
            CALL_911,
            "Long time on the floor is dangerous even without an obvious injury.",
        ],
    },
    "near_fall": {
        "title": "Stumble / near fall",
        "priority": 4, "tags": ["warning"],
        "confirm": False, "escalate": False, "cooldown": True,
        "advice": [
            "They stumbled but recovered. Check in with them.",
            "Repeated stumbles in one spot are an early warning sign. Check that area for trip hazards.",
        ],
    },
    "obstacle": {
        "title": "Trip hazard in walking path",
        "priority": 3, "tags": ["construction"],
        "confirm": False, "escalate": False, "cooldown": True,
        "advice": [
            "Clear the walking path: loose rugs, cords, boxes, shoes, clutter.",
        ],
    },
    "low_light": {
        "title": "Moving around in the dark",
        "priority": 3, "tags": ["bulb"],
        "confirm": False, "escalate": False, "cooldown": True,
        "advice": [
            "The room is dark while they are walking. That raises fall risk.",
            "Consider a night light or a motion-activated light here.",
        ],
    },
    "night_wandering": {
        "title": "Up and walking at night",
        "priority": 4, "tags": ["crescent_moon"],
        "confirm": False, "escalate": False, "cooldown": True,
        "advice": [
            "Check in with them. Night-time walking has a higher fall risk.",
        ],
    },
    "inactivity": {
        "title": "No movement for a long time",
        "priority": 4, "tags": ["zzz"],
        "confirm": False, "escalate": True, "cooldown": True,
        "advice": [
            "No movement was detected for an unusually long time. Call or check on them.",
        ],
    },
    "camera_offline": {
        "title": "SafeStep camera offline",
        "priority": 4, "tags": ["no_entry", "camera"],
        "confirm": False, "escalate": False, "cooldown": True,
        "advice": [
            "Falls in this room will NOT be detected until it's back.",
            "Check power, the USB camera cable and the network.",
        ],
    },
}

GENERIC_HAZARD = {
    "title": "Potential danger",
    "priority": 3, "tags": ["warning"],
    "confirm": False, "escalate": False, "cooldown": True,
    "advice": ["Check on them when you can."],
}

# --------------------------------------------------------------------------
# Event log (for Databricks ingestion, e.g. Auto Loader on the JSONL file)
# --------------------------------------------------------------------------

_log_lock = threading.Lock()


def record(inc: "Incident", lifecycle: str, **extra) -> None:
    row = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "lifecycle": lifecycle,
        **inc.to_dict(),
        **extra,
    }
    try:
        path = BASE_DIR / CFG["event_log"]
        path.parent.mkdir(parents=True, exist_ok=True)
        with _log_lock, path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as e:  # logging must never break alerting
        log.warning("event log write failed: %s", e)


# --------------------------------------------------------------------------
# ntfy publishing
# --------------------------------------------------------------------------

def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {CFG['ntfy_token']}"} if CFG["ntfy_token"] else {}


def publish(topic: str, title: str, message: str, priority: int = 3,
            tags: list[str] | None = None, actions: list[dict] | None = None,
            click: str | None = None, attach: str | None = None) -> bool:
    payload = {"topic": topic, "title": title, "message": message,
               "priority": priority, "tags": tags or []}
    if actions:
        payload["actions"] = actions[:3]  # ntfy allows up to 3 buttons
    if click:
        payload["click"] = click
    if attach:
        payload["attach"] = attach

    if CFG["dry_run"]:
        print(f"\n----- [DRY RUN] ntfy -> {topic} (priority {priority}) -----\n"
              f"{title}\n{message}\n" + "-" * 50, flush=True)
        return True

    data = json.dumps(payload).encode("utf-8")
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(
                CFG["ntfy_server"], data=data, method="POST",
                headers={"Content-Type": "application/json", **_auth_headers()})
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            log.info("ntfy sent -> %s: %s", topic, title)
            return True
        except Exception as e:
            log.warning("ntfy publish failed (attempt %d/3): %s", attempt, e)
            time.sleep(attempt * 1.5)
    log.error("GAVE UP sending ntfy alert: %s", title)
    return False


def publish_async(*args, **kwargs) -> None:
    threading.Thread(target=publish, args=args, kwargs=kwargs, daemon=True).start()


# --------------------------------------------------------------------------
# Incidents and the alert state machine
# --------------------------------------------------------------------------
#   pending_confirmation --(timer)--> alerted --(no ack)--> escalated ...
#        |  cancel                      |  ack / cancel / resolve
#        v                              v
#    cancelled                acknowledged / resolved
#
# Extra lifecycle rows in the event log (they don't change `status`):
#   delivered / delivery_failed: whether ntfy accepted an alert message

ACTIVE = {"pending_confirmation", "alerted", "escalated"}


class Incident:
    def __init__(self, evt: dict, spec: dict):
        self.id = secrets.token_hex(3).upper()
        self.type = evt["type"]
        self.spec = spec
        self.source_event_id = evt.get("event_id")      # the detector's own id
        self.device_id = evt.get("device_id")
        self.person_detected = evt.get("person_detected")
        self.immobility_seconds = evt.get("immobility_seconds")
        self.metrics = evt.get("metrics") or {}
        self.review = evt.get("review") or {}
        self.room = evt.get("room") or "Unknown room"
        self.zone = evt.get("zone")                  # e.g. "near the couch"
        self.camera_id = evt.get("camera_id") or "cam-1"
        self.confidence = evt.get("confidence")
        self.person = evt.get("person")
        self.details = evt.get("details")
        self.snapshot_url = evt.get("snapshot_url")
        self.lat = evt.get("lat")
        self.lng = evt.get("lng")
        self.detected_at = evt.get("timestamp") or datetime.now(timezone.utc).isoformat()
        self.created = time.monotonic()
        self.status = "new"
        self.escalations = 0
        self.acked_by = None
        self.timer: threading.Timer | None = None

    # ---- formatting -----------------------------------------------------
    def where(self) -> str:
        s = self.room + (f" ({self.zone})" if self.zone else "")
        return f"{s}, {CFG['home_name']}  [camera {self.camera_id}]"

    def when(self) -> str:
        try:
            dt = datetime.fromisoformat(str(self.detected_at).replace("Z", "+00:00")).astimezone()
        except ValueError:
            dt = datetime.now()
        return dt.strftime("%I:%M:%S %p, %a %b %d").lstrip("0")

    def map_link(self) -> str | None:
        if self.lat is not None and self.lng is not None:
            return f"https://maps.google.com/?q={self.lat},{self.lng}"
        return None

    def body(self, headline: str | None = None) -> str:
        lines = []
        if headline:
            lines += [headline, ""]
        if self.person:
            lines.append(f"Who: {self.person}")
        lines.append(f"Where: {self.where()}")
        lines.append(f"When: {self.when()}")
        if self.confidence is not None:
            try:
                lines.append(f"Detection confidence: {round(float(self.confidence) * 100)}%")
            except (TypeError, ValueError):
                pass  # a bad confidence value must not stop the alert
        if self.details:
            lines.append(f"Details: {self.details}")
        lines += ["", "What to do:"]
        lines += [f"{i}. {a}" for i, a in enumerate(self.spec["advice"], 1)]
        lines += ["", f"Incident {self.id}"]
        return "\n".join(lines)

    def actions(self) -> list[dict]:
        acts = []
        if self.spec["escalate"]:
            acts.append({
                "action": "http", "label": "I'm on my way",
                "url": f"{CFG['ntfy_server']}/{CFG['ntfy_ack_topic']}",
                "method": "POST", "body": f"ack {self.id}",
                "headers": _auth_headers(), "clear": True,
            })
        if self.spec["priority"] >= 5:
            acts.append({"action": "view", "label": "Call 911", "url": "tel:911"})
        if self.map_link():
            acts.append({"action": "view", "label": "Map", "url": self.map_link()})
        if self.snapshot_url and len(acts) < 3:
            acts.append({"action": "view", "label": "Snapshot", "url": self.snapshot_url})
        return acts

    def to_dict(self) -> dict:
        return {
            "incident_id": self.id, "type": self.type, "status": self.status,
            "source_event_id": self.source_event_id, "device_id": self.device_id,
            "person_detected": self.person_detected,
            "immobility_seconds": self.immobility_seconds,
            "metrics": self.metrics, "review": self.review,
            "room": self.room, "zone": self.zone, "camera_id": self.camera_id,
            "confidence": self.confidence, "person": self.person,
            "details": self.details, "detected_at": self.detected_at,
            "priority": self.spec["priority"], "escalations": self.escalations,
            "acked_by": self.acked_by, "schema_version": 1,
        }


class IncidentManager:
    MAX_REMEMBERED_EVENT_IDS = 5000

    def __init__(self):
        self.lock = threading.RLock()
        self.incidents: dict[str, Incident] = {}
        self.last_hazard: dict[tuple[str, str], float] = {}
        self.heartbeats: dict[str, dict] = {}   # camera_id -> {"t", "room", "offline"}
        self.seen_event_ids: OrderedDict[str, None] = OrderedDict()

    def _seen_before(self, event_id: str) -> bool:
        if event_id in self.seen_event_ids:
            return True
        self.seen_event_ids[event_id] = None
        while len(self.seen_event_ids) > self.MAX_REMEMBERED_EVENT_IDS:
            self.seen_event_ids.popitem(last=False)
        return False

    # ---- intake ----------------------------------------------------------
    def handle_event(self, evt: dict) -> dict:
        etype = str(evt.get("type", "")).strip().lower()
        if not etype:
            raise ValueError("event needs a 'type'")
        evt["type"] = etype
        spec = dict(CATALOG.get(etype, GENERIC_HAZARD))
        if etype not in CATALOG and evt.get("title"):
            spec["title"] = evt["title"]
        if evt.get("advice"):
            spec["advice"] = list(evt["advice"]) if isinstance(evt["advice"], list) else [evt["advice"]]

        with self.lock:
            # A detector retry with the same event_id is not a new event.
            sid = evt.get("event_id")
            if sid and self._seen_before(str(sid)):
                return {"status": "duplicate", "event_id": sid}

            # No person in frame: not a fall.
            if etype == "fall" and evt.get("person_detected") is False:
                return {"status": "ignored", "reason": "no person detected"}

            room = evt.get("room") or "Unknown room"
            key = (etype, room)
            if spec["cooldown"]:
                last = self.last_hazard.get(key, 0)
                if time.time() - last < CFG["hazard_cooldown_s"]:
                    log.info("suppressed repeat %s in %s (cooldown)", etype, room)
                    return {"status": "suppressed", "reason": "cooldown"}
                self.last_hazard[key] = time.time()

            # A new fall in a room that already has a RECENT active fall incident
            # is the same emergency, not a new one. After fall_dedupe_s it is a
            # new fall, even if nobody ever acknowledged the old one.
            if etype == "fall":
                for inc in self.incidents.values():
                    if (inc.type == "fall" and inc.room == room and inc.status in ACTIVE
                            and time.monotonic() - inc.created < CFG["fall_dedupe_s"]):
                        return {"status": "duplicate", "incident_id": inc.id}

            inc = Incident(evt, spec)
            self.incidents[inc.id] = inc
            # Keep the detector's complete agreed payload (including nested
            # metrics and review state) in the lifecycle log for analytics.
            record(inc, "detected", raw_event=evt.get("source_payload", evt))

            window = CFG["confirm_window_s"]
            if spec["confirm"] and window > 0 and not evt.get("skip_confirmation"):
                inc.status = "pending_confirmation"
                inc.timer = threading.Timer(window, self._confirm_timeout, args=(inc.id,))
                inc.timer.daemon = True
                inc.timer.start()
                log.info("[%s] %s in %s, waiting %ss for cancel", inc.id, etype, room, window)
                # Early heads-up so the caregiver isn't blindsided.
                publish_async(
                    CFG["ntfy_topic"], f"Possible fall in {inc.room}, checking...",
                    f"SafeStep detected a possible fall. If they don't respond in {window}s "
                    f"you will get an URGENT alert.\nWhere: {inc.where()}\nIncident {inc.id}",
                    priority=4, tags=["warning", "hourglass"])
            else:
                self._fire(inc)
            return {"status": inc.status, "incident_id": inc.id}

    # ---- transitions -----------------------------------------------------
    def _notify(self, inc: Incident, stage: str, topic: str, title: str, message: str, **kwargs) -> None:
        """Send on a background thread, then log whether ntfy accepted it."""
        def run() -> None:
            try:
                ok = publish(topic, title, message, **kwargs)
            except Exception:
                log.exception("[%s] notify crashed (%s)", inc.id, stage)
                ok = False
            record(inc, "delivered" if ok else "delivery_failed", stage=stage)
        threading.Thread(target=run, daemon=True).start()

    def _confirm_timeout(self, inc_id: str) -> None:
        with self.lock:
            inc = self.incidents.get(inc_id)
            if inc and inc.status == "pending_confirmation":
                try:
                    self._fire(inc, headline=f"No response from the person within {CFG['confirm_window_s']}s.")
                except Exception:
                    log.exception("[%s] could not send the alert", inc.id)
                    if inc.spec["escalate"] and inc.timer is None:
                        self._schedule_escalation(inc)

    def _fire(self, inc: Incident, headline: str | None = None) -> None:
        inc.status = "alerted"
        record(inc, "alerted")
        try:
            message = inc.body(headline)
        except Exception:
            log.exception("[%s] could not format the alert, sending a minimal one", inc.id)
            message = f"{inc.spec['title']} in {inc.room}. Incident {inc.id}"
        self._notify(inc, "alerted", CFG["ntfy_topic"], inc.spec["title"], message,
                     priority=inc.spec["priority"], tags=inc.spec["tags"],
                     actions=inc.actions(), click=inc.map_link(), attach=inc.snapshot_url)
        if inc.spec["escalate"]:
            self._schedule_escalation(inc)

    def _schedule_escalation(self, inc: Incident) -> None:
        inc.timer = threading.Timer(CFG["escalate_after_s"], self._escalate, args=(inc.id,))
        inc.timer.daemon = True
        inc.timer.start()

    def _escalate(self, inc_id: str) -> None:
        with self.lock:
            inc = self.incidents.get(inc_id)
            if not inc or inc.status not in ("alerted", "escalated"):
                return
            if inc.escalations >= CFG["max_escalations"]:
                log.warning("[%s] max escalations reached, still unacknowledged", inc.id)
                return
            try:
                inc.escalations += 1
                inc.status = "escalated"
                record(inc, "escalated")
                waited = CFG["escalate_after_s"] * inc.escalations
                waited_txt = f"{waited:g}s" if waited < 120 else f"{round(waited / 60)} min"
                headline = f"STILL NO CAREGIVER RESPONSE after {waited_txt} (reminder {inc.escalations})."
                title = "UNANSWERED: " + inc.spec["title"]
                self._notify(inc, "escalation", CFG["ntfy_topic"], title, inc.body(headline), priority=5,
                             tags=inc.spec["tags"], actions=inc.actions(), click=inc.map_link())
                if CFG["ntfy_escalation_topic"]:
                    self._notify(inc, "escalation_backup", CFG["ntfy_escalation_topic"],
                                 "BACKUP NEEDED: " + inc.spec["title"],
                                 inc.body("The primary caregiver has not responded. Please help."),
                                 priority=5, tags=inc.spec["tags"], actions=inc.actions(),
                                 click=inc.map_link())
            except Exception:
                log.exception("[%s] escalation failed", inc.id)
            finally:
                self._schedule_escalation(inc)  # keep trying; the max check above ends it

    def _stop_timer(self, inc: Incident) -> None:
        if inc.timer:
            inc.timer.cancel()
            inc.timer = None

    def _notify_all(self, inc: Incident, title: str, msg: str, priority: int, tags: list[str]) -> None:
        publish_async(CFG["ntfy_topic"], title, msg, priority=priority, tags=tags)
        if inc.escalations and CFG["ntfy_escalation_topic"]:
            publish_async(CFG["ntfy_escalation_topic"], title, msg, priority=priority, tags=tags)

    def cancel(self, inc_id: str, by: str = "person") -> dict:
        """The person said they're OK (button, gesture, voice...)."""
        with self.lock:
            inc = self._get(inc_id)
            if inc.status not in ACTIVE:
                return {"status": inc.status, "incident_id": inc.id}
            was_pending = inc.status == "pending_confirmation"
            self._stop_timer(inc)
            inc.status = "cancelled"
            record(inc, "cancelled", by=by)
            msg = (f"They indicated they are OK. No action needed.\nWhere: {inc.where()}\n"
                   f"Incident {inc.id}")
            if not was_pending:
                msg += "\nYou may still want to check in with them."
            self._notify_all(inc, f"Cancelled: {inc.spec['title'].split(' - ')[0].title()} in {inc.room}",
                             msg, priority=3, tags=["white_check_mark"])
            return {"status": inc.status, "incident_id": inc.id}

    def acknowledge(self, inc_id: str, by: str = "caregiver") -> dict:
        with self.lock:
            inc = self._get(inc_id)
            if inc.status not in ACTIVE:
                return {"status": inc.status, "incident_id": inc.id}
            self._stop_timer(inc)
            inc.status = "acknowledged"
            inc.acked_by = by
            record(inc, "acknowledged", by=by)
            self._notify_all(inc, f"Caregiver responding: {inc.room}",
                             f"{by} acknowledged incident {inc.id} and is on the way.",
                             priority=3, tags=["white_check_mark", "running"])
            return {"status": inc.status, "incident_id": inc.id}

    def resolve(self, inc_id: str, by: str = "caregiver", outcome: str | None = None) -> dict:
        with self.lock:
            inc = self._get(inc_id)
            self._stop_timer(inc)
            inc.status = "resolved"
            record(inc, "resolved", by=by, outcome=outcome)
            if inc.spec["priority"] >= 4:
                self._notify_all(inc, f"Resolved: incident {inc.id}",
                                 f"Marked resolved by {by}." + (f"\nOutcome: {outcome}" if outcome else ""),
                                 priority=2, tags=["heavy_check_mark"])
            return {"status": inc.status, "incident_id": inc.id}

    def _get(self, inc_id: str) -> Incident:
        inc = self.incidents.get(str(inc_id).upper())
        if not inc:
            raise KeyError(f"unknown incident {inc_id}")
        return inc

    # ---- camera heartbeat watchdog --------------------------------------
    def heartbeat(self, camera_id: str, room: str | None) -> None:
        with self.lock:
            hb = self.heartbeats.get(camera_id)
            if hb and hb["offline"]:
                publish_async(CFG["ntfy_topic"], f"SafeStep camera back online ({hb['room']})",
                              f"Camera {camera_id} is monitoring again.", priority=2,
                              tags=["white_check_mark", "camera"])
                log.info("camera %s back online", camera_id)
            self.heartbeats[camera_id] = {"t": time.time(), "room": room or (hb or {}).get("room") or "Unknown room",
                                          "offline": False}

    def _check_cameras(self) -> None:
        now = time.time()
        with self.lock:
            for cam, hb in list(self.heartbeats.items()):
                silent = now - hb["t"]
                if not hb["offline"] and silent > CFG["heartbeat_timeout_s"]:
                    hb["offline"] = True
                    # Offline alerts aren't muted by the hazard cooldown.
                    self.last_hazard.pop(("camera_offline", hb["room"]), None)
                    self.handle_event({"type": "camera_offline", "room": hb["room"], "camera_id": cam,
                                       "details": f"No data from the detection engine for {int(silent)}s."})

    def watchdog_loop(self) -> None:
        while True:
            time.sleep(5)
            try:
                self._check_cameras()
            except Exception:  # a dead watchdog would hide the next camera failure
                log.exception("camera watchdog error")

    def summary(self) -> list[dict]:
        with self.lock:
            return [i.to_dict() for i in sorted(self.incidents.values(), key=lambda i: i.detected_at, reverse=True)]


MANAGER = IncidentManager()

# --------------------------------------------------------------------------
# Acknowledgements coming back from the caregiver's phone via ntfy
# --------------------------------------------------------------------------

def ack_listener_loop() -> None:
    """Subscribe to the ack topic. The 'I'm on my way' button publishes
    'ack <ID>' there, so the phone never needs to reach this server.
    Caregivers can also type 'ack <ID>' or 'resolve <ID>' into that topic."""
    if CFG["dry_run"]:
        return
    since = str(int(time.time()))
    while True:
        url = f"{CFG['ntfy_server']}/{CFG['ntfy_ack_topic']}/json?since={since}"
        try:
            req = urllib.request.Request(url, headers=_auth_headers())
            with urllib.request.urlopen(req, timeout=120) as resp:
                log.info("listening for caregiver acks on topic %s", CFG["ntfy_ack_topic"])
                for raw in resp:
                    msg = json.loads(raw)
                    if msg.get("event") != "message":
                        continue
                    since = msg["id"]
                    parts = msg.get("message", "").strip().split()
                    if len(parts) < 2:
                        continue
                    cmd, inc_id = parts[0].lower(), parts[1]
                    try:
                        if cmd in ("ack", "ok", "omw"):
                            MANAGER.acknowledge(inc_id, by="Caregiver (ntfy)")
                        elif cmd in ("resolve", "done"):
                            MANAGER.resolve(inc_id, by="Caregiver (ntfy)", outcome=" ".join(parts[2:]) or None)
                    except KeyError as e:
                        log.warning("ack for %s", e)
        except Exception as e:
            log.warning("ack listener reconnecting: %s", e)
            time.sleep(5)


# --------------------------------------------------------------------------
# HTTP API for the detection engine
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "SafeStepAlerts/1.0"

    def log_message(self, fmt, *args):
        log.debug("http %s", fmt % args)

    def _send(self, code: int, obj) -> None:
        data = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        if CFG["api_key"]:
            sent = (self.headers.get("X-API-Key") or "").encode()
            if not secrets.compare_digest(sent, CFG["api_key"].encode()):
                self._send(401, {"error": "bad or missing X-API-Key"})
                return False
        return True

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/", "/health"):
            # Deliberately minimal: the ntfy topic name is the secret that protects the alert feed.
            self._send(200, {"ok": True, "dry_run": CFG["dry_run"]})
        elif path == "/incidents":
            if not self._authorized():
                return
            self._send(200, MANAGER.summary())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        try:
            body = self._json()
            if path == "/events":
                events = body if isinstance(body, list) else [body]
                results = [MANAGER.handle_event(to_internal(e)) for e in events]
                self._send(200, results if isinstance(body, list) else results[0])
            elif not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            elif path == "/heartbeat":
                MANAGER.heartbeat(body.get("camera_id", "cam-1"), body.get("room"))
                self._send(200, {"ok": True})
            elif path.startswith("/incidents/"):
                _, _, inc_id, action = (path.split("/") + [""])[:4]
                by = body.get("by")
                if action == "cancel":
                    self._send(200, MANAGER.cancel(inc_id, by or "person"))
                elif action == "ack":
                    self._send(200, MANAGER.acknowledge(inc_id, by or "caregiver"))
                elif action == "resolve":
                    self._send(200, MANAGER.resolve(inc_id, by or "caregiver", body.get("outcome")))
                else:
                    self._send(404, {"error": "use /incidents/<id>/cancel|ack|resolve"})
            else:
                self._send(404, {"error": "not found"})
        except KeyError as e:
            self._send(404, {"error": str(e).strip("'")})
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            log.exception("request failed")
            self._send(500, {"error": str(e)})


def main() -> None:
    global CFG
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    CFG = load_config()
    if "--dry-run" in sys.argv:
        CFG["dry_run"] = True
    if not CFG["api_key"] and CFG["host"] not in ("127.0.0.1", "localhost"):
        log.warning("api_key is empty and the service listens on %s: anyone on the network can post "
                    "events or cancel alerts. Set api_key in config.json.", CFG["host"])

    threading.Thread(target=ack_listener_loop, daemon=True).start()
    threading.Thread(target=MANAGER.watchdog_loop, daemon=True).start()

    srv = ThreadingHTTPServer((CFG["host"], CFG["port"]), Handler)
    print(f"""
 SafeStep alert service on http://127.0.0.1:{CFG['port']}   {'(DRY RUN - nothing is sent)' if CFG['dry_run'] else ''}

 Caregiver: install the ntfy app (Android / iOS) and subscribe to this topic:
     {CFG['ntfy_topic']}        (server: {CFG['ntfy_server']})
   or open {CFG['ntfy_server']}/{CFG['ntfy_topic']} in a browser.
 Backup caregiver topic: {CFG['ntfy_escalation_topic'] or '(not set; add ntfy_escalation_topic in config.json)'}
""", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("bye")


if __name__ == "__main__":
    main()
