# SafeStep

Fall monitoring prototype with camera pose detection, caregiver alerts, and Databricks analytics.

- `safestep/`: pose estimation, fall detection, event serialization, and alert-service client
- [Alert service setup](alerts/README.md)
- [Databricks integration setup and recovery](analytics/README.md)
- [Weekly dashboard email and schedule settings](analytics/databricks/README.md)

## Run the detector

Install the Python project with `uv sync`. Start the alert service in another terminal with `python alerts/alert_service.py` (add `--dry-run` to print notifications instead of sending them). Then start detection:

```sh
uv run safestep --source 0 --device-id uno_q_01 --room "Living room"
```

Useful options: `--immobility 15` (seconds without movement before "still on the floor"; default 60), `--heartbeat 15` (seconds between heartbeats), `--no-alerts` (detect without contacting the alert service), `--privacy` (skeletons only), `--source clip.mp4` (video file). Environment variables: `SAFESTEP_ALERT_URL`, `SAFESTEP_ALERT_API_KEY`, `SAFESTEP_DEVICE_ID`, `SAFESTEP_ROOM`.

## What the detector sends

Each event uses the agreed schema (see `alerts/contracts.py`) and is posted to `/events` in order, with retries:

| Detector event | `event_type` | Alert service behaviour |
|---|---|---|
| Fast drop, then lying down | `possible_fall` | 30 s countdown, then an urgent alert |
| Lying without meaningful movement for `--immobility` seconds | `immobility` + `related_event_id` | Escalates the **same** incident once ("still on the floor") |
| Standing upright again for 1.5 s | `recovered` + `related_event_id` | During the countdown: cancels it, so no urgent alert. After the alert: marks the incident `recovered` and stops reminders. |

- **`confidence`** is the **fall evidence score** (0–1). It combines how far the hips dropped (40%), the peak drop speed (35%) and how horizontal the body ended up (25%). It's a heuristic, not a calibrated probability. Alerts show it as low / medium / high / very high, matching the dashboard's bands. YOLO's person-detection score is kept separately as `metrics.person_detection_score`.
- **`details`** is a plain-language sentence for caregivers, e.g. *"Rapid drop of 1.5 torso lengths; ended up lying down (88°)."*
- **`metrics`** holds the raw numbers for analytics (stored in Databricks' `raw` column):

  | Metric | Event | Meaning |
  |---|---|---|
  | `drop_torso_lengths` | fall | How far the hips dropped |
  | `peak_drop_speed` | fall | Fastest downward hip speed during the fall |
  | `peak_lateral_speed` | fall | Fastest sideways hip speed during the fall |
  | `torso_angle_deg` | fall | Final torso angle (0° upright, 90° flat) |
  | `person_detection_score` | fall | YOLO's person-detection confidence |
  | `time_on_floor_s` | immobility | Time since the fall started |
  | `recovery_seconds` | recovered | Time from the start of the fall until upright again |

  Speeds and distances are in torso lengths (per second). `immobility_seconds` is only sent once it has actually been measured.
- **Heartbeats** go to `/heartbeat` every `--heartbeat` seconds, and **only while frames are being processed**. A crash, frozen camera or unplugged camera stops them, and the alert service's watchdog raises "camera offline" after `heartbeat_timeout_s`. A clean shutdown sends `status: "stopped"`, which raises the offline alert immediately, without a second one later.

## Tests

```sh
uv run pytest tests                                   # detector + end-to-end through the alert service
python3 -m unittest discover -s alerts/tests -v       # alert service
python3 -m unittest discover -s analytics/tests -v    # analytics, view, weekly report
```
