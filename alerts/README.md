# SafeStep Alert Service (detection engine → ntfy → caregiver)

```
YOLO pose / fall detector ──POST /events──▶ alert_service.py ──▶ ntfy ──▶ caregiver's phone
        (UNO Q / laptop)        heartbeat         │      ▲
                                                  │      └── "I'm on my way" button (ack topic)
                                                  └──▶ data/events.jsonl ──▶ Databricks (async)
```

The alert path never waits on analytics. Pure Python standard library, so it runs on the UNO Q's Linux side or a laptop.

## Quick start
1. `cd alerts` then `python alert_service.py`. The first run creates `config.json` (git-ignored) with a random private topic and prints it. See `config.example.json` for every setting.
2. The caregiver installs the **ntfy** app (Android/iOS), taps **+**, and subscribes to that topic.
3. In another terminal: `python simulate_engine.py demo` (or `fall`, `fall-cancel`, `help`, `floor`, `hazards`, `offline`).

Use `python alert_service.py --dry-run` to print notifications instead of sending them.

## What the caregiver gets
| Event `type` | Priority | Behaviour |
|---|---|---|
| `fall_detected` | URGENT (5) | Heads-up right away, then `confirm_window_s` (30s) for the person to cancel. If not cancelled → urgent alert with **I'm on my way / Call 911 / Map** buttons. No ack in `escalate_after_s` → reminders + backup caregiver topic. |
| `help_request` | URGENT | Immediate, escalates |
| `immobility` (still on floor) | URGENT | Immediate, escalates |
| `near_fall`, `night_wandering`, `inactivity` | High (4) | Potential danger, 5-min cooldown per room |
| `obstacle`, `low_light`, any custom type | Default (3) | Potential danger, cooldown |
| `camera_offline` | High | Automatic when heartbeats stop for `heartbeat_timeout_s`, or immediately when the detector reports a clean shutdown. One alert per outage, plus a "back online" note when heartbeats resume. |

Every alert says **who, where (room + zone + camera), when, fall evidence, details, and numbered "What to do" steps**. The evidence line reads e.g. `Fall evidence: high (score 0.78 of 1; a detection score, not a probability)`. It uses the same bands as the dashboard: low < 0.50, medium < 0.70, high < 0.85, very high.

### Detector follow-ups (linked to the original fall)
The SafeStep detector sends follow-ups with `related_event_id` set to the fall's `event_id`, so they update that incident instead of opening a new one:

| Follow-up | Incident was… | What happens |
|---|---|---|
| `recovered` (person stood up) | in the countdown | Cancelled by `SafeStep detector`; the urgent alert is never sent (the countdown timer re-checks status under the same lock) |
| | alerted / escalated | Status `recovered`; reminders stop; caregiver gets "Update: they got back up" and the review prompt |
| | acknowledged | Status unchanged; the responding caregiver is told they got up |
| `immobility` (still on the floor) | in the countdown | Urgent alert sent immediately; escalation starts |
| | alerted / escalated | Urgent "PERSON STILL ON THE FLOOR" update on the same incident; escalation continues |
| | cancelled by the person | Re-opened as an urgent alert ("cancelled, but they have not moved since") |
| | acknowledged / closed | Update notification only |

Each incident gets at most one immobility follow-up. A `recovered` event for an unknown fall (e.g. after a service restart) is ignored. An `immobility` event for an unknown fall still alerts, as its own incident. `recovered` counts as a closed status: it can be reviewed, acknowledged and resolved.

## Hooking up your detector

The integrated `safestep` command (see the [root README](../README.md)) posts the agreed event schema to `/events`: `possible_fall`, then `immobility` and/or `recovered` with `related_event_id`. Events go through one background worker in order, with retries (repeated `event_id`s are ignored, so retries are safe). It also sends heartbeats from its frame loop, and `status: "stopped"` on exit. Raw `metrics` go to the lifecycle log for Databricks; caregivers only see the plain-language `details`.

The legacy client below is still supported (the simulator uses it):

```python
from safestep_client import SafeStepClient
ss = SafeStepClient("http://127.0.0.1:8080", camera_id="cam-1", room="Living room")
ss.start_heartbeat()

# inside your frame loop, after your temporal fall logic decides:
inc = ss.fall(confidence=0.91, zone="near the couch", snapshot_url=None)
if person_gave_ok_gesture:
    ss.cancel(inc)
if seconds_on_floor > 60:
    ss.immobility(seconds_on_floor)
ss.hazard("obstacle", details="Box in walkway")          # fire-and-forget
ss.hazard("wet_floor", title="Wet floor", advice="Dry the floor near the sink")  # custom
```
Calls have a 3s timeout and never raise, so the CV loop can't crash on alerts.

Raw HTTP, if your engine isn't Python:
```
POST /events          {"type":"fall_detected","room":"Bedroom","zone":"by bed","confidence":0.9,
                       "person":"Maria","details":"...","lat":..,"lng":..,"snapshot_url":".."}
POST /incidents/<id>/cancel | ack | resolve   {"by":"...","outcome":"..."}
POST /heartbeat       {"camera_id":"cam-1","room":"Bedroom","status":"running","fps":29.5}
POST /heartbeat       {"camera_id":"cam-1","room":"Bedroom","status":"stopped","reason":"stopped by user"}
GET  /incidents       GET /health
```

## Acknowledging
The **I'm on my way** button posts `ack <ID>` to `<topic>-ack` on ntfy, and the service listens there. So the phone does **not** need to reach the service (no port forwarding). Caregivers can also type `ack <ID>` or `resolve <ID> <outcome>` into that topic.

## Config (`config.json`)
`home_name`, `ntfy_server`, `ntfy_topic`, `ntfy_escalation_topic` (backup caregiver), `ntfy_token` (for a private ntfy server), `api_key` (require `X-API-Key` from the engine), `confirm_window_s` (30), `escalate_after_s` (120), `max_escalations` (3), `hazard_cooldown_s` (300), `heartbeat_timeout_s` (60), `port` (8080).

## Databricks
`data/events.jsonl` gets one row per lifecycle step (`detected`, `alerted`, `escalated`, `acknowledged`, `cancelled`, `resolved`, plus detector follow-ups `recovered` and `immobility`) with room, type, confidence and timestamps. Run the repository’s [analytics shipper](../analytics/README.md) to populate the lifecycle table and incident view for dashboard trends. Cancellation rate is not a verified false-alarm rate.

## Notes
- Anyone who knows an ntfy.sh topic name can read it. Keep the random name or self-host ntfy with a token.
- This is a research prototype, not a certified emergency system.

## Caregiver event review

After helping, post `resolve <ID>` to the caregiver ack topic. Resolving or cancelling a fall or near-fall sends a separate low-priority notification with **Confirmed fall / No fall / Unsure** buttons. Cancellation alone never sets a review outcome. Acknowledging means someone is responding; an acknowledged incident must be resolved before reviewing it. Detector-recovered incidents also accept reviews.

For an optional note or a correction, publish a message to the same ack topic:

```text
review ABC123 confirmed_fall Witnessed a fall near the bed
review ABC123 no_fall Sat down deliberately
review ABC123 unsure Could not verify what happened
```

Or use the authenticated API:

```text
POST /incidents/ABC123/review
X-API-Key: <configured api_key>
Content-Type: application/json

{"outcome":"confirmed_fall","note":"Witnessed a fall near the bed","by":"Sam"}
```

Outcomes are `confirmed_fall`, `no_fall`, or `unsure`. Notes are optional and limited to 2,000 characters; `by` is a non-empty label of at most 200 characters. API reviews inherit the existing API-key configuration. ntfy commands use the shared caregiver topic and record `Caregiver (ntfy)` as the actor, not a verified individual identity. The Python client also exposes `ss.review(incident_id, outcome, note=None, by="caregiver")`.

Cancelled, resolved, or detector-recovered fall/near-fall incidents accept reviews. Reviewing never cancels timers or changes incident status. Corrections append `reviewed` lifecycle rows; identical outcome/note/actor submissions are idempotent. The service sends a save receipt to the primary topic. Retain `data/events.jsonl`: closed incidents can be recovered from this log for review after restart. Active incident/timer recovery remains outside this review feature.

Run local review tests with `python3 -m unittest discover -s alerts/tests -v` from the repository root.
