# Databricks integration

`alerts/data/events.jsonl` → `ingest.py` → Delta lifecycle table → incident view → dashboard queries.
The shipper runs independently of the alert service. Run one shipper per log/checkpoint pair.

## Setup

Use Python 3.10+ and a Databricks SQL warehouse (or a cluster with Runtime 14.2+ for native parameters). Get the server hostname and HTTP path from the warehouse's Connection Details. Create a personal access token with access to the warehouse. The identity needs USE CATALOG, USE SCHEMA, CREATE SCHEMA (for initialization), CREATE TABLE, and SELECT/MODIFY privileges as appropriate. Initialization also creates or replaces the incident view.

From the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r analytics/requirements.txt
cp analytics/.env.example analytics/.env
# Edit analytics/.env with your credentials and an existing catalog name.
set -a
source analytics/.env
set +a
python analytics/ingest.py --init --dry-run
python analytics/ingest.py --init
python analytics/ingest.py --once --dry-run
python analytics/ingest.py --once
python analytics/ingest.py
```

The script reads exported environment variables; it does not load `.env` itself. Credentials, event logs and checkpoint files are ignored by Git. `DATABRICKS_HOST` remains supported as a fallback; either hostname variable accepts a hostname or HTTPS workspace URL. `SAFESTEP_TABLE` must be `schema.table` or `catalog.schema.table`, using letters, digits and underscores with a letter/underscore first. Default: `safestep.lifecycle_events` in the warehouse's current catalog. `--init` creates the table and a sibling `incidents` view; it does not ingest events or migrate an existing table. Inspect its dry run before using it against an existing schema.

Run `alerts/alert_service.py --dry-run` and `alerts/simulate_engine.py demo` in separate terminals to generate lifecycle data without sending phone notifications, then ingest with `--once`. In the SQL editor, verify:

```sql
SELECT lifecycle, COUNT(*) FROM your_catalog.safestep.lifecycle_events GROUP BY lifecycle;
SELECT * FROM your_catalog.safestep.incidents ORDER BY timestamp DESC LIMIT 20;
```

Use `sql/queries.sql` to build dashboard tiles, replacing `safestep.` with your configured catalog/schema prefix. These are query definitions; a hosted dashboard is not provisioned. Dry-run alert delivery is simulated, so use a separate schema for demo data.

## Recovery and operation

Each successful batch advances an atomic JSON checkpoint containing the log identity and destination. Failed SQL batches retain their offset for retry. Replayed rows use a content hash and MERGE; identical rows within a batch are deduplicated. Native parameters are capped at 255, allowing at most 11 rows per batch with the current 23 columns. Default batch size is 10. Input streams one batch at a time, and incomplete trailing lines wait until the next poll. Malformed JSON stops the batch without advancing the checkpoint; repair the source row before restarting.

A replaced file, a smaller file, a changed destination, or a legacy integer checkpoint causes replay from zero. Keep the append-only source file intact until ingestion succeeds. In-place truncate-and-regrow between polls cannot be reliably detected; rotate by rename/replacement after draining the old log. Rotation does not ingest an unread renamed file. Use a separate checkpoint for each source and destination. Deleting a checkpoint safely replays retained data but cannot recover removed source logs. The shipper retries connection failures every poll interval; monitor its logs and query undelivered alerts with query 6.

`delivered` means ntfy accepted a message; it does not confirm that a phone displayed it. Cancellation is a proxy for a false alarm, not a verified outcome. The current query counts all cancellations, including those after confirmation. Local tests cover batching, retry checkpoints, partial writes, replacement, dry run, and initialization:

```sh
python3 -m unittest discover -s analytics/tests -v
```

Connector reference: [Databricks SQL Connector](https://docs.databricks.com/aws/en/dev-tools/python-sql-connector), [native parameter limits](https://github.com/databricks/databricks-sql-python/blob/main/docs/parameters.md).

The SQL connector uses the `certifi` CA bundle for TLS verification. If your network requires an organization CA bundle, export `DATABRICKS_CA_BUNDLE=/absolute/path/to/ca-bundle.pem`. Certificate verification stays enabled.

## Enable caregiver reviews on an existing deployment

1. Deploy the updated alert service and restart it. Retain its event log.
2. With your existing Databricks environment variables exported, run `python analytics/ingest.py --init --dry-run` to inspect the SQL, then `python analytics/ingest.py --init`. This creates or replaces the incident view with four additional fields: `review_outcome`, `review_note`, `reviewed_by`, and `reviewed_at`. The lifecycle table needs no migration: reviews use its existing `raw` JSON column. Existing incidents remain unreviewed until a caregiver explicitly records an outcome.
3. Resume the shipper normally. It retains review fields in raw JSON; its checkpoint and batching format are unchanged.
4. Replace the existing dashboard draft using `analytics/dashboard/SafeStep.lvdash.json`, then publish it. The export uses `workspace.safestep` references; adjust these if your catalog/schema differs. The new **Caregiver Reviews** page shows outcomes, a review queue count, and notes. The table includes all incidents; blank review fields indicate no review.
5. Refresh Genie metadata or add the new view fields to its configuration as needed. Explain that `confirmed_fall`, `no_fall`, and `unsure` are caregiver reports. Null means unreviewed; cancellation is not a verified false alarm. The dashboard file does not provision a Genie Agent.

The view ranks `reviewed` lifecycle rows independently so a later notification delivery row cannot erase a review. Corrected reviews use the newest review row and retain earlier versions in the lifecycle table. The SQL examples in `sql/queries.sql` include outcomes and incidents awaiting review.

Validation: `python3 -m unittest discover -s analytics/tests -v`. Review join behavior is exercised locally with SQLite window functions and a JSON extraction shim; live Databricks SQL and dashboard rendering still require workspace validation.

## Weekly caregiver email

[Databricks-managed snapshot emails](databricks/README.md) refresh the published SafeStep dashboard and email its snapshot to configured recipients. No recipient mailbox credentials are needed. The default schedule is Sunday at 8:00 a.m. America/Chicago; edit the Job schedule and task Subscribers in Databricks, or redeploy with `analytics/deploy_dashboard_email.py` after changing the private report settings. Use `--send-now` to request a one-time snapshot. The custom HTML/SMTP digest remains an optional alternative.
