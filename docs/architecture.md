# Architecture and event lifecycle

## Components and ownership

| Component | Entry point | Responsibility |
| --- | --- | --- |
| Detector | `safestep.app:main` / `uv run safestep` | Process frames, track people, detect fall sequences, emit events and frame-driven heartbeats |
| Event adapter | `safestep/events.py` | Convert internal detector results to the HTTP contract |
| Detector HTTP client | `safestep/alerts.py` | Send queued events in order with bounded retries; heartbeat independently |
| Alert service | `alerts/alert_service.py` | Own incident IDs, status, confirmation timers, reminders, acknowledgements and reviews |
| Shipper | `analytics/ingest.py` | Read complete JSONL records, parameterize SQL MERGE batches, persist ingestion offset |
| Databricks view | `analytics/sql/schema.sql` | Collapse lifecycle rows into current incident state and delivery/review fields |
| Dashboard | `analytics/dashboard/SafeStep.lvdash.json` | Display incident, lifecycle, quality and caregiver-review data |
| Native email job | `analytics/deploy_dashboard_email.py` | Configure a published dashboard snapshot schedule and subscribers |
| Optional digest | `analytics/weekly_report.py`, `analytics/databricks_report.py` | Query weekly metrics, render HTML/text, send through SMTP with delivery tracking |

`alerts/safestep_client.py` is a separate lightweight legacy client used by the simulator and manual integrations. The detector uses `safestep/alerts.py`.

## Detector contract

An initial event resembles:

```json
{
  "event_id": "evt_example",
  "device_id": "uno_q_01",
  "room": "Living room",
  "timestamp": "2026-11-15T14:00:00Z",
  "event_type": "possible_fall",
  "confidence": 0.78,
  "person_detected": true,
  "details": "Rapid drop of 1.5 torso lengths; ended up lying down (88°).",
  "metrics": {
    "drop_torso_lengths": 1.5,
    "peak_drop_speed": 2.8,
    "peak_lateral_speed": 0.4,
    "torso_angle_deg": 88,
    "person_detection_score": 0.91
  }
}
```

`alerts/contracts.py` validates the agreed schema. Historical `fall` and detector `possible_fall` names normalize to `fall_detected` in the alert/analytics pipeline. The internal detector event name remains `fall`.

Follow-ups have distinct event IDs and `related_event_id` pointing to the initial fall event. `immobility` adds measured `immobility_seconds`; `recovered` carries `metrics.recovery_seconds`. The alert service links these to the original incident where available. An unmatched recovery is ignored; unmatched immobility can create a standalone urgent incident.

The service owns status, delivery state and caregiver review outcomes. Detector-provided equivalents do not set them. Raw detector payloads and metrics are retained in lifecycle JSON for analysis. Event-ID deduplication is bounded and in memory; it is not durable across service restarts.

## Incident behavior

| Input or transition | Result |
| --- | --- |
| Initial fall | Heads-up notification and a confirmation countdown (default 30 seconds) |
| Countdown expires | Urgent alert; reminder timer starts |
| Recovery during countdown | Cancel pending alert and request a caregiver review |
| Recovery after urgent alert | Mark `recovered`, stop reminders, notify caregivers and request review |
| Recovery after acknowledgement | Record recovery and notify the responding caregiver; acknowledgement remains the status |
| Linked immobility | Update the same incident once; pending/cancelled incidents fire urgently; active incidents receive an urgent update |
| Caregiver acknowledgement | Record the responder and stop reminders |
| Caregiver resolution | Close the incident and request a review for fall/near-fall incidents |
| No acknowledgement | Remind and optionally notify a backup topic; default 120-second interval, up to three escalations |
| Missing heartbeats | Camera-offline alert after the default 60-second timeout |
| Detector clean shutdown | A `stopped` heartbeat triggers an immediate camera-offline alert |

Acknowledgement means someone is responding. Recovery means the detector observed an upright posture. Neither establishes whether a fall happened or whether the person was injured. Caregiver outcomes are `confirmed_fall`, `no_fall`, or `unsure`; closed fall/near-fall incidents (`cancelled`, `resolved`, `recovered`) accept reviews. Corrections append history.

## Persistence and analytics

The service appends lifecycle snapshots to `alerts/data/events.jsonl`. Delivery callbacks record ntfy acceptance or failure. The shipper reads complete lines, hashes source records, and uses MERGE to avoid reinserting identical rows. A checkpoint advances only after a batch succeeds. Keep the source log and use one shipper per log/checkpoint pair.

The Delta table stores lifecycle records; the `incidents` view derives one row per incident. It normalizes old fall names and independently selects the latest review so later delivery records cannot erase a caregiver outcome. `alert_sent` means ntfy accepted a notification, not that a phone displayed it. Cancellation is not a verified false alarm.

The native email job sends a dashboard snapshot. Its weekly schedule does not force a seven-day date range: dashboard filters determine the snapshot's range. The optional SMTP digest instead computes seven complete local calendar days, compares the preceding week, and uses all dates for current follow-up queues.

## Runtime boundaries

Detector event queues, tracks, alert timers, active incidents and deduplication caches are in memory. The detector retries transient HTTP errors with bounded backoff, then logs that delivery was abandoned. The queue is not a durable outbox. Closed incidents can be recovered from the lifecycle log for caregiver reviews; active timers are not restored after a service restart.

Camera privacy mode changes the displayed/saved annotated frame to skeletons; the original frame still exists in memory for pose inference. Live preview files, logs, credentials, topic names and report settings should remain private. Generated demo events are fictional and will affect reports if ingested into the active dashboard's table.
