# Optional custom SMTP digest

The active SafeStep setup uses native Databricks dashboard emails. This alternative is only for custom HTML email and requires a separate sending service.

## Run the custom digest in Databricks

The report runs as a serverless notebook Job. Your laptop can be off. Spark queries the existing incident/lifecycle tables using the Job owner's permissions; no Databricks PAT is required inside the runtime. SMTP credentials come from Databricks Secrets. The default is Sunday 08:00 America/Chicago, configurable through the report settings wizard and redeployment.

## Deploy and verify

From the repository root, activate `.venv` and export the existing private deployment credentials:

```sh
set -a
source analytics/.env
set +a
.venv/bin/python analytics/deploy_report_job.py --run-preview
```

This uploads the runtime files and notebook to your own workspace folder and creates or updates **SafeStep Weekly Caregiver Report**. It leaves the schedule **paused**, then runs hosted verification without sending email. Notebook serverless compute must be enabled for the workspace and owner. Compute usage is billed according to your workspace plan.

The verification run queries real data, renders the HTML/text digest, and exercises real Delta delivery tracking with an in-memory email sink. It checks persistence, duplicate suppression, uncertain delivery protection, explicit retries, and multipart MIME. It creates a uniquely named test table and drops it in a `finally` block. If the job is forcibly terminated, a `weekly_report_test_*` table may remain for an operator to remove. Verification also checks SMTP TLS connectivity without authentication or sending. A failed connectivity result means the workspace network policy or endpoint must be fixed before activation. It does not verify the Gmail App Password or inbox receipt. Job run output reports the checks; use the Databricks Job run history to inspect the result.

## Gmail credentials and activation

The runtime defaults to Gmail (`smtp.gmail.com`, STARTTLS, port 587), with the configured sender as the login. Choose the sender and recipients through `analytics/weekly_report.py --configure`. Gmail requires a Google App Password on an eligible account with 2-Step Verification. Put that value in the private `analytics/.env` as `SMTP_PASSWORD`; reload the exported environment, then run:

```sh
.venv/bin/python analytics/deploy_report_job.py --store-smtp-secret
# After hosted verification succeeds:
.venv/bin/python analytics/deploy_report_job.py --activate
```

The first command creates the `safestep-email` secret scope if needed and stores `smtp-password`, without printing the value. It also refreshes the paused deployment. Scope creation uses the creator's default management permissions; it does not grant everyone access. The second command checks the report settings and secret key, changes the runtime to send mode, and unpauses the weekly schedule. Only the schedule is activated; it does not send an immediate test email. Use **Run now** on the Job if you want to send the latest due report immediately. SMTP connectivity and inbox receipt still require a real delivery check after adding the credential. Your workspace's outbound network policy must allow Gmail SMTP on port 587.

The SMTP transport can be customized with the Job task parameters `smtp_host`, `smtp_port`, `smtp_security`, and `smtp_username`. Keep the password in the secret scope. Set equivalent `SMTP_*` environment variables before redeployment to retain transport customization. Changing sender to another Gmail account requires that account's app password. Recipients need permission to open the dashboard. Use its published URL and grant access before adding other caregivers.

## Settings and delivery guarantees

The Job task's `config_json` contains the report preferences. It is not a secret store. Change preferences with the local wizard and redeploy with `--activate` to update both the runtime preferences and the weekly Quartz schedule. The Job uses the selected timezone. Databricks can delay runs for compute availability; DST can shift/skip ambiguous local scheduled times. Consider a time outside the DST transition hour. A manual run uses the latest due complete week.

Production state is `catalog.schema.weekly_report_deliveries`, beside the source tables. It survives compute shutdowns. The Job owner needs SELECT on the source tables and CREATE TABLE / SELECT / MODIFY in this schema. Restrict ledger modifications to the report owner. The ledger contains hashed delivery identities, pending/sent status, claim token, and update time; it stores no report body or raw addresses.

**Use one owning Job per ledger.** The deployed Job has `max_concurrent_runs=1` and automatic task retries disabled. Delta does not enforce unique keys; independently running writer jobs against the same ledger are unsupported. The deployment script reuses its private local Job ID and checks for an existing matching Job before creating one. Keep `reports/databricks-deployment.json` private and retain the production ledger.

A pending intent is committed before SMTP. A successful server acceptance is marked sent; retries skip sent identities. Ambiguous failures remain pending and block automatic resend for that recipient, while other recipients can succeed. After checking provider logs, override the Job task parameter `retry_uncertain=true` for a **single manual run** when a retry is appropriate. It can duplicate a previously accepted message; never save it as the scheduled default. SMTP acceptance is not proof of inbox delivery. Local SQLite delivery history is not migrated: switching hosts can resend a week already sent locally.

The local cron runner remains an alternative. Do not enable both local and hosted delivery for the same subscription.

## References

[Serverless Job configuration](https://docs.databricks.com/aws/en/dev-tools/bundles/examples), [Spark SQL parameters](https://docs.databricks.com/aws/en/pyspark/reference/classes/sparksession/sql), [Job scheduling](https://docs.databricks.com/gcp/en/jobs/scheduled).
