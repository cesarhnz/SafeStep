# SafeStep dashboard

`SafeStep.lvdash.json` is the exported dashboard definition plus the Caregiver Reviews page. It references `workspace.safestep.incidents` and `workspace.safestep.lifecycle_events`.

Apply the updated incident view first using the [analytics setup](../README.md#enable-caregiver-reviews-on-an-existing-deployment). In the Databricks dashboard draft, use **Replace dashboard** to upload this file, check the data and charts, and publish it. This updates the draft definition; publishing, warehouse configuration, permissions, schedules, and Genie Agent configuration are managed separately in Databricks.

Caregiver Reviews displays reported outcomes and notes. Outcome entry is through the alert notification buttons, the caregiver ack topic, or `POST /incidents/<id>/review`. The dashboard is a read-only view of these reviews.

The incident dataset reads the incident view directly, including its latest caregiver-review fields. This keeps review metrics aligned with the [weekly caregiver email](../reports/README.md). Email preferences are configured with `analytics/weekly_report.py --configure`; dashboard filters do not alter report settings or reporting windows.

The active email setup uses the native **Dashboard** task in **SafeStep Weekly Dashboard Email**. Databricks sends the Overview snapshot to configured recipients on Sundays at 08:00 America/Chicago. Edit the Job's schedule and task Subscribers to customize it; recipient mailbox credentials are not used. See [native email setup](../databricks/README.md).
