# Databricks-managed dashboard email

SafeStep sends a dashboard snapshot **to** your configured email address using Databricks' own email service. Your Gmail is a recipient only. No Gmail password, app password, SMTP server, or separate hosting is needed.

The current deployment uses a native **Dashboard** task in **SafeStep Weekly Dashboard Email**. The published SafeStep dashboard is refreshed on its SQL warehouse, then Databricks sends the snapshot. The default is Sunday at 08:00 America/Chicago. The job runs in Databricks while your laptop is off.

## Change schedule or recipients

In Databricks, open **Jobs & Pipelines → SafeStep Weekly Dashboard Email**:

- Edit the schedule to change the day, time, or timezone.
- Edit the **dashboard_snapshot** task's **Subscribers** to change recipients.
- Use **Run now** to request an immediate snapshot email.

Recipients selected by email must be Databricks workspace users. For people outside the workspace, an administrator can configure an email notification destination in Databricks. This does not require access to the recipient's mailbox.

The job sends the dashboard's configured snapshot. Its date range follows the dashboard/filter settings; a weekly send schedule does not itself restrict the charts to the previous seven days. By default, Databricks includes the first dashboard page. The task's Attachments settings can choose additional pages. This configuration uses the existing Overview page for a concise snapshot.

## Deploy from this repository

The private `analytics/reports/config.json` holds `recipients`, `weekday`, `time`, `timezone`, and `dashboard_url`. The `sender` field is unused for native snapshots and can remain empty. To update settings through the existing wizard, run `python analytics/weekly_report.py --configure` and leave the custom SMTP sender blank.

Export the existing Databricks environment and apply the native email configuration:

```sh
set -a
source analytics/.env
set +a
.venv/bin/python analytics/deploy_dashboard_email.py
```

The script reuses the existing report Job ID and replaces its custom Python task with a native Dashboard task. Its schedule is enabled immediately. It does not trigger a one-time send unless requested:

```sh
.venv/bin/python analytics/deploy_dashboard_email.py --send-now
```

For initial setup or after changing the dashboard draft, use `--publish`. This saves a private rollback copy, aligns the incident source with the current incident view, and publishes the dashboard using individual data permissions. It does not add dashboard sharing permissions. Without `--publish`, the dashboard must already be published.

`reports/dashboard-email-deployment.json` retains Job/dashboard IDs and the most recent explicitly triggered run ID. Runtime settings, deployment state, and dashboard rollback copies are git-ignored. Keep them private. Avoid deploying an additional overlapping schedule, as each schedule can send its own email.

## Check delivery

Open the Job's run history to inspect snapshot generation and email-task success. After a successful run, check the configured inbox and spam folder. A successful Databricks run does not independently prove that Gmail placed the message in the inbox.

The local test suite checks recipient-only settings, custom schedules, native task configuration, replacement of the old custom Job, and the explicit one-time send behavior. The deployed task is also verified through a live Databricks run.

```sh
python3 -m unittest discover -s analytics/tests -v
```

The earlier custom HTML/SMTP digest is retained as an [optional alternative](CUSTOM_DIGEST.md). It is not the active delivery mechanism.

[Databricks dashboard tasks](https://docs.databricks.com/aws/en/jobs/tasks/dashboard) · [Dashboard subscription behavior](https://docs.databricks.com/aws/en/dashboards/share/schedule-subscribe)
