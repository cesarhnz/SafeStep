#!/usr/bin/env python3
"""Read-only local UI gateway. No simulation ever reaches the alert service.

Run `python3 ui/server.py` after `npm --prefix ui run build`, or alongside Vite.
The default is loopback. Use `--host 0.0.0.0` to serve on a trusted home LAN.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "ui" / "dist"


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def dashboard_url() -> str | None:
    value = os.environ.get("SAFESTEP_DASHBOARD_URL") or read_json(ROOT / "analytics/reports/config.json").get("dashboard_url")
    if not isinstance(value, str):
        return None
    url = urllib.parse.urlparse(value)
    host = url.hostname or ""
    allowed = any(host == domain or host.endswith("." + domain) for domain in ("databricks.com", "azuredatabricks.net"))
    if url.scheme == "https" and allowed and not url.username and not url.password and url.path.startswith(("/dashboards/", "/dashboardsv3/", "/sql/dashboards/")):
        return value
    return None


def log_records(path: Path) -> tuple[list[dict], bool]:
    try:
        # Bound memory even when a detector has been running for months.
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - 4_000_000))
            if size > 4_000_000:
                stream.readline()
            lines = deque(stream, maxlen=2000)
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
                if isinstance(row, dict) and row.get("incident_id"):
                    rows.append(row)
            except ValueError:
                continue  # An append may be in progress; retry on the next poll.
        return rows, True
    except OSError:
        return [], False


def normalize_events(rows: list[dict], incidents: list[dict]) -> list[dict]:
    latest: dict[str, dict] = {}
    delivery: dict[str, str] = {}
    demo_ids: set[str] = set()
    for row in rows:
        incident_id = str(row["incident_id"])
        latest[incident_id] = row
        if is_demo_record(row):
            demo_ids.add(incident_id)
        lifecycle = row.get("lifecycle")
        if lifecycle == "delivered":
            delivery[incident_id] = ("not_sent" if row.get("dry_run") else
                                     "accepted" if row.get("dry_run") is False else "unknown")
        elif lifecycle == "delivery_failed":
            delivery[incident_id] = "failed"
    for row in incidents:
        if isinstance(row, dict) and row.get("incident_id"):
            latest[str(row["incident_id"])] = {**latest.get(str(row["incident_id"]), {}), **row}
            if is_demo_record(row):
                demo_ids.add(str(row["incident_id"]))
    events = []
    for incident_id, row in latest.items():
        notification = delivery.get(incident_id, "pending" if row.get("status") == "pending_confirmation" else "unknown")
        events.append({
            "id": incident_id, "type": str(row.get("type", "unknown")),
            "status": str(row.get("status", "unknown")), "room": str(row.get("room") or "Home"),
            "timestamp": row.get("detected_at") or row.get("logged_at"),
            "updated_at": row.get("logged_at"),
            "source": "demo" if incident_id in demo_ids else "real",
            "notification": notification, "details": str(row.get("details") or "An event was recorded by SafeStep."),
        })
    ordered = sorted(events, key=lambda e: str(e["timestamp"] or ""), reverse=True)
    # Keep unresolved real falls visible even when newer routine activity is abundant.
    active = [e for e in ordered if e["source"] == "real"
              and e["type"] in {"fall", "fall_detected", "possible_fall", "immobility", "help_request"}
              and e["status"] in {"new", "pending_confirmation", "alerted", "escalated", "acknowledged"}]
    recent = [e for e in ordered if e not in active]
    return sorted((active + recent)[:100], key=lambda e: str(e["timestamp"] or ""), reverse=True)


def is_demo_record(row: dict) -> bool:
    raw = row.get("raw_event") or {}
    if not isinstance(raw, dict):
        raw = {}
    # Preserve explicit demo provenance, including the repository's existing seed/simulator IDs.
    return any(record.get("source") == "demo" or record.get("is_demo") is True
               or any(str(record.get(key) or "").startswith("demo-")
                      for key in ("camera_id", "device_id", "source_event_id", "event_id"))
               for record in (row, raw))


def snapshot() -> dict:
    config = read_json(ROOT / "alerts/config.json")
    base = os.environ.get("SAFESTEP_ALERT_URL", f"http://127.0.0.1:{config.get('port', 8080)}").rstrip("/")
    key = os.environ.get("SAFESTEP_ALERT_API_KEY", config.get("api_key", ""))

    def get(path: str):
        request = urllib.request.Request(base + path, headers={"X-API-Key": key})
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.load(response)

    connected, state, incidents = False, "unknown", []
    dry_run = bool(config.get("dry_run", False))
    try:
        health = get("/health")
        live = get("/incidents")
        if not isinstance(live, list) or health.get("ok") is not True:
            raise ValueError("Unexpected alert service response")
        connected, incidents, dry_run = True, live, bool(health.get("dry_run", False))
        try:
            state = get("/monitoring").get("monitoring", "unknown")
        except (OSError, ValueError):
            pass  # Older alert services still expose their incident history.
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    event_log = Path(os.environ.get("SAFESTEP_EVENT_LOG", str(ROOT / "alerts" / config.get("event_log", "data/events.jsonl"))))
    rows, history_available = log_records(event_log)
    return {
        "connected": connected, "monitoring": state if state in {"active", "offline", "waiting"} else "unknown",
        "dry_run": dry_run, "home_name": str(config.get("home_name") or "Your home"),
        "dashboard_url": dashboard_url(), "events": normalize_events(rows, incidents),
        "history_available": history_available, "checked_at": datetime.now(timezone.utc).isoformat(),
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/dashboard":
            try:
                payload = json.dumps(snapshot()).encode()
                self.respond(200, payload, "application/json")
            except Exception:
                # Keep the local dashboard available if a config or history file is malformed.
                self.respond(503, b'{"error":"Dashboard data is temporarily unavailable"}', "application/json")
            return
        if path.startswith("/api/"):
            self.respond(404, b'{"error":"Not found"}', "application/json")
            return
        if path in {"/", "/about"}:
            file = DIST / "index.html"
        else:
            file = (DIST / urllib.parse.unquote(path).lstrip("/")).resolve()
        if not file.is_relative_to(DIST.resolve()) or not file.is_file():
            self.respond(404, b"Not found. Build the UI with: npm --prefix ui run build", "text/plain")
            return
        self.respond(200, file.read_bytes(), mimetypes.guess_type(file.name)[0] or "application/octet-stream")

    def respond(self, status, payload, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store" if content_type == "application/json" else "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default: 127.0.0.1; use 0.0.0.0 for trusted LAN access)")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    print(f"SafeStep UI listening on {args.host}:{args.port}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
