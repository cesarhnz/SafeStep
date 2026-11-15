# SafeStep dashboard

`SafeStep.lvdash.json` is the exported dashboard definition plus the Caregiver Reviews page. It references `workspace.safestep.incidents` and `workspace.safestep.lifecycle_events`.

Apply the updated incident view first using the [analytics setup](../README.md#enable-caregiver-reviews-on-an-existing-deployment). In the Databricks dashboard draft, use **Replace dashboard** to upload this file, check the data and charts, and publish it. This updates the draft definition; publishing, warehouse configuration, permissions, schedules, and Genie Agent configuration are managed separately in Databricks.

Caregiver Reviews displays reported outcomes and notes. Outcome entry is through the alert notification buttons, the caregiver ack topic, or `POST /incidents/<id>/review`. The dashboard is a read-only view of these reviews.
