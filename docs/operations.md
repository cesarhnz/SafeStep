# Operating guide

Commands below run from the repository root. Detector, alert service and shipper are separate processes. Use separate terminals during development and a process supervisor on an always-on deployment host.

## 1. Install

Use Python 3.10+ and `uv`:

```sh
uv sync
uv pip install -r analytics/requirements.txt
```

`pyproject.toml` installs the detector and development tests; `analytics/requirements.txt` adds the Databricks SQL connector and certificate bundle. If a later environment sync removes extra packages, install the analytics requirements again. Alert-service and analytics unit tests use the Python standard library. Detector tests need pytest and NumPy; running the camera application additionally needs its declared vision dependencies and model weights.

## 2. Configure and start alerts

```sh
uv run python alerts/alert_service.py --dry-run
```

The first run writes `alerts/config.json` and generates a random topic if none is configured. Stop the process to edit configuration, then restart. Settings take effect at startup. Keep this file private.

| Setting | Default / purpose |
| --- | --- |
| `ntfy_server`, `ntfy_topic` | `https://ntfy.sh`, generated topic; subscribe from the phone |
| `ntfy_ack_topic` | Defaults to the primary topic plus `-ack` |
| `ntfy_escalation_topic` | Optional backup caregiver topic |
| `ntfy_token` | Optional server authentication token |
| `api_key` | Optional HTTP API key; detector uses `SAFESTEP_ALERT_API_KEY` |
| `host`, `port` | `0.0.0.0`, `8080`; choose the intended interface |
| `confirm_window_s` | 30-second cancellation window |
| `escalate_after_s`, `max_escalations` | 120 seconds, three reminders |
| `fall_dedupe_s` | 600-second recent-fall grouping window |
| `hazard_cooldown_s` | 300-second repeat-hazard cooldown |
| `heartbeat_timeout_s` | 60 seconds without a heartbeat triggers an offline alert |
| `event_log` | `data/events.jsonl`, resolved relative to `alerts/` |
| `dry_run` | Print notifications instead of contacting ntfy |

Start real delivery with `uv run python alerts/alert_service.py`. If `dry_run` is set in the config, also change it to false. Subscribe to the configured topic in ntfy on the caregiver's phone. Phone acknowledgement/review buttons post through the ack topic, so the phone does not need direct access to the alert service.

Read-only health check:

```sh
curl http://127.0.0.1:8080/health
```

Dry-run delivery is logged as simulated success and can enter analytics. Use a separate log/schema for testing when those records should not appear in production metrics.

## 3. Start monitoring

```sh
# Set privately when api_key is enabled in alerts/config.json.
export SAFESTEP_ALERT_API_KEY='replace-with-the-configured-key'
uv run safestep --source 0 --device-id uno_q_01 --room "Living room"
```

Omit the export when API authentication is disabled. Use `SAFESTEP_ALERT_URL` or `--alert-url` for a remote service. See [detector options](../safestep/README.md). The detector prints delivery/retry diagnostics; its optional `--events` file contains detector events rather than the alert service lifecycle log needed by ingestion.

## 4. Initialize and start analytics

For a new installation, copy `analytics/.env.example` to private `analytics/.env` and fill in the workspace hostname, SQL warehouse HTTP path, token and destination table. Preserve an existing configured file.

```sh
set -a
source analytics/.env
set +a
.venv/bin/python analytics/ingest.py --init --dry-run
.venv/bin/python analytics/ingest.py --init
.venv/bin/python analytics/ingest.py --once --dry-run
.venv/bin/python analytics/ingest.py --once
.venv/bin/python analytics/ingest.py
```

Initialization creates missing schema/table objects and replaces the incident view; it does not migrate an existing table. Inspect the SQL before applying to a shared schema. The shipper reads exported variables, not `.env` automatically. Retain its log and checkpoint. See [ingestion recovery](../analytics/README.md#recovery-and-operation) for rotation, retries and batching limits.

## 5. Dashboard and native email

Import or replace the dashboard using `analytics/dashboard/SafeStep.lvdash.json`, configure its warehouse and sharing, then publish it. The export uses `workspace.safestep`: update **all** dataset source references when using another catalog/schema. The deployment helper's `--publish` option updates only the incident dataset source; inspect lifecycle and data-quality sources separately.

Configure recipients, schedule and the published URL:

```sh
.venv/bin/python analytics/weekly_report.py --configure
.venv/bin/python analytics/deploy_dashboard_email.py
```

The helper requires an already published dashboard unless `--publish` is given. It enables the native weekly schedule immediately. The default is Sunday 08:00 America/Chicago. Use the Databricks Job UI for schedule/subscriber changes and run history. `--send-now` requests an immediate real email in addition to scheduling.

Native snapshots need no SMTP credentials; leave the wizard's sender blank. Do not enable an overlapping custom digest unless a second email is intended. Native snapshot date ranges follow the dashboard settings. See the [email guide](../analytics/databricks/README.md) and [optional custom digest](../analytics/reports/README.md).

## 6. Daily checks and recovery

| Symptom | Check / action |
| --- | --- |
| Detector cannot open source | Camera index/path, camera permissions and whether another process owns the device |
| Detector HTTP 401 | Match `SAFESTEP_ALERT_API_KEY` with alert-service `api_key`; restart after changing service config |
| No phone notification | Check dry-run mode, configured topic/server, phone subscription and service delivery logs |
| Repeated urgent reminders | Acknowledge through ntfy or the authenticated API; inspect the ack listener if acknowledgement is not processed |
| Camera-offline alert | Confirm frame processing and heartbeats; clean detector shutdown intentionally reports offline |
| Dashboard stale | Check shipper process, exported credentials, warehouse access and checkpoint/log paths |
| Ingestion stops at malformed row | Repair the source record; the checkpoint stays before the failed batch |
| Review missing | Confirm the incident is closed, the review was saved, the shipper caught up and the current view is installed |
| Email missing | Check native Job run history, subscribers, published dashboard permissions and inbox/spam |
| Counts include demo scenarios | Identify `DEMO-MARIA-` incident IDs; follow the specific demo cleanup procedure in the analytics guide |

For planned shutdown, stop the detector while the alert service is still reachable, allow ingestion to catch up, then stop the shipper and alert service. Preserve logs/checkpoints/configuration. Restart the alert service, shipper and detector as needed. Active incident timers and detector queues are not restored after restart; do not assume restarting continues unresolved reminders.

## Development checks

```sh
uv run pytest tests -q
python3 -m unittest discover -s alerts/tests -v
python3 -m unittest discover -s analytics/tests -v
python3 -m compileall -q safestep alerts analytics
git diff --check
```

The alert and analytics suites mock external delivery; the detector integration suite starts a local HTTP server and captures notifications. No real notifications or emails are required. See [verification status](verification.md) for the most recent recorded result.
