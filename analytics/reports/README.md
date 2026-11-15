# Weekly dashboard email

**Active setup: [Databricks-managed snapshot emails](../databricks/README.md).** Databricks sends to the recipient; no Gmail password or SMTP settings are required. The custom HTML digest below is an optional alternative.


A short caregiver digest built from the **same Databricks incident view as the dashboard**. The email has four headline numbers, previous-week comparisons, a prioritized follow-up list, three leading rooms, caregiver outcomes, and a link to the full dashboard. Both HTML and plain text are sent. Review notes, person names, and individual incident details are omitted. Each recipient gets a separate email.

The default is **Sunday, 8:00 a.m., America/Chicago**. Users can choose any weekday, time, timezone, recipients, and dashboard link through the settings wizard. Scheduling uses the configured timezone, including daylight saving changes, independently of the scheduler host's timezone.

## Set up and preview

From the repository root, install `analytics/requirements.txt` into `.venv`, export your Databricks variables as described in [analytics setup](../README.md), and apply the current incident view with `analytics/ingest.py --init` if needed. No new tables or changes to the alert service are required. If the report says the incident view is missing caregiver-review fields, inspect `analytics/ingest.py --init --dry-run` and apply the updated view before enabling delivery.

```sh
# Offline sample; no credentials, SQL calls, SMTP, or delivery-state changes.
python3 analytics/weekly_report.py --demo

# Interactive user choices. Press Enter to retain a current/default value.
python3 analytics/weekly_report.py --configure
# Or copy reports/config.example.json to reports/config.json and edit it.

# Read actual dashboard data without sending anything.
.venv/bin/python analytics/weekly_report.py --preview
# Optional explicit exclusive local end date, for previewing an earlier week:
.venv/bin/python analytics/weekly_report.py --preview --week-ending 2026-11-15
```

Open `analytics/reports/preview.html` in a browser; `preview.txt` is the plain-text counterpart. Sample previews are conspicuously labeled. Keep previews private: live previews contain dashboard aggregates. Runtime settings, generated previews, and delivery state in this folder are git-ignored. Custom paths passed through `--config`, `--output`, or `--state` must also be kept outside version control.

`reports/config.json` fields:

| Setting | Choice |
| --- | --- |
| `name` | Email report title; use a distinct name for distinct dashboards |
| `weekday` | `monday` through `sunday`; default `sunday` |
| `time` | Local 24-hour `HH:MM`; default `08:00` |
| `timezone` | IANA timezone; default `America/Chicago` |
| `recipients` | List of caregiver email addresses |
| `sender` | Sender address authorized by your SMTP provider |
| `dashboard_url` | Published dashboard HTTPS link; required for sending |
| `stale_after_hours` | Warn when the newest lifecycle event is older than this; default `24` |

A Sunday email covers the previous **Sunday 00:00 through Saturday 23:59:59**, using an exclusive Sunday 00:00 boundary. A Monday email covers Monday through Sunday. Comparisons use the preceding seven complete local days. UTC query bounds preserve local calendar days across DST. Arrival occurs on the first scheduler tick at or after the selected time; an hourly/minute choice in a DST gap is handled on the first tick after the gap. A repeated hour sends once.

## Databricks hosting

Native Databricks snapshots are the active delivery method; see [setup and schedule settings](../databricks/README.md). The optional custom digest can also run as a serverless Job; see [custom SMTP deployment](../databricks/CUSTOM_DIGEST.md). This replaces the always-on host and local cron setup below.

## SMTP and automatic delivery

Add the following to your private `analytics/.env` or your deployment's secret environment. The script reads **exported variables**; the scheduler wrapper loads `analytics/.env`. The settings wizard never stores SMTP passwords.

```sh
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USERNAME=your-smtp-user
SMTP_PASSWORD=your-smtp-password
# For implicit TLS, use SMTP_SECURITY=ssl and SMTP_PORT=465.
```

Both modes verify certificates and require encryption. Use provider-issued SMTP credentials and an authorized sender. SMTP username and password may both be omitted when using a trusted TLS relay. No emails are sent by preview/configure commands.

```sh
# Send the latest due report, then exit. Safe for repeated scheduler invocations.
analytics/reports/run_weekly_report.sh
```

Install the following with `crontab -e` on the **always-on host** running this checkout, substituting its absolute path. The wrapper expects `.venv/bin/python` and loads the private `.env`. Keep the shipper running separately. Monitor scheduler exit codes/logs; errors return nonzero.

```cron
*/15 * * * * /absolute/path/to/SafeStep/analytics/reports/run_weekly_report.sh >> /absolute/path/to/private-weekly-report.log 2>&1
```

Scheduling is not installed automatically by this implementation. The dashboard remains read-only; settings live in the wizard/config, not Databricks dashboard controls. For several dashboards or subscription groups, use separate `--config` and `--state` paths and schedule each invocation. Use a distinct report `name` for each group.

On first run, the latest scheduled report is sent immediately, even if its due time has passed. Before this week's selected send time, the most recent due report is the previous week's. Downtime catches up **only the latest due week**, avoiding a flood of historical emails. Changing weekday/time affects the next selected report window; a time-only change does not resend an already delivered window. Preview the new settings before enabling a schedule.

## Delivery state and recovery

The local SQLite ledger records each recipient/report window. It prevents ordinary retries and concurrent scheduler runs from resending delivered reports. Keep a single durable `--state` file per subscription; deleting it or running multiple hosts with independent copies can send duplicates. Adding recipients sends only to the new recipients for the latest due week. Successful recipients remain recorded if another recipient fails.

SMTP cannot guarantee exactly-once delivery: a timeout can happen after server acceptance. The ledger commits a `pending` intent before sending and a `sent` result after acceptance. A crash, failed request, or lost SMTP response leaves `pending`; later invocations return an error instead of silently duplicating that message. These failures do not block other recipients. Check your provider's logs and retry only if appropriate:

```sh
analytics/reports/run_weekly_report.sh --retry-uncertain
```

This explicitly retries pending recipients for the current due window and can duplicate a message if the original was accepted. A deterministic Message-ID helps trace the message but does not guarantee provider deduplication. Pending older windows remain in the ledger; the scheduler does not replay them. If the provider confirms acceptance, an operator can mark that row `sent` in the private SQLite ledger instead of retrying it. SMTP acceptance is not proof of inbox delivery. Check provider bounces/logs separately.

## Metric meaning

Weekly counts are grouped by **detection timestamp**, not notification/review date, with one row per incident. Caregiver-confirmed counts include reviews of fall and near-fall detections from that week. Cancellation never implies a verified false alarm. Latest reviews/statuses are used as of report generation; late review corrections can change a subsequent preview of a past week. The SQL queries are sequential reads, not a historical point-in-time snapshot.

Response time is the median between first alert decision and first caregiver acknowledgement with a non-negative duration; sample counts accompany current and previous values. Delivery failures include incidents with any failed attempt even if a retry succeeded. The separate no-alert-accepted count identifies incidents where ntfy has accepted no alert.

Follow-ups use **all dates and current status**: open alerts without acceptance, open alerts without acknowledgement, and closed fall/near-fall detections awaiting review. Closed/cancelled incidents are excluded from the open delivery/response queues. Top rooms include fall and near-fall detections only. Prevention signals are obstacle, low-light, night-wandering, and inactivity detections; near falls have their own headline. Camera-offline counts are episodes, not downtime.

A missing/stale lifecycle timestamp triggers a data warning. A quiet event log does not establish monitoring uptime; a zero-count week is worded as “no incidents recorded.” Missing detection timestamps are flagged and excluded from weekly counts. This digest complements immediate alerts.

## Verification

```sh
python3 -m unittest discover -s analytics/tests -v
python3 -m unittest discover -s alerts/tests -v
```

The report tests run offline: real SQL aggregation is exercised with a SQLite compatibility shim, and SMTP is mocked. Live Databricks execution, SMTP delivery, and inbox rendering require deployment credentials. Connector behavior follows the [Databricks SQL connector](https://docs.databricks.com/aws/en/dev-tools/python-sql-connector), [session timezone](https://docs.databricks.com/aws/en/sql/language-manual/parameters/timezone), and Python's [SMTP client](https://docs.python.org/3/library/smtplib.html).
