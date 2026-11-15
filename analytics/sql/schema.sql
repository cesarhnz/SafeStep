-- Run once in a Databricks SQL editor (or notebook) before starting ingest.py.

CREATE SCHEMA IF NOT EXISTS safestep;

-- Append-only: one row per lifecycle step of an incident
-- (detected, alerted, delivered, delivery_failed, escalated, acknowledged,
-- cancelled, resolved, reviewed). 'alerted' = the service decided to alert;
-- 'delivered' / 'delivery_failed' = whether ntfy accepted the message.
CREATE TABLE IF NOT EXISTS safestep.lifecycle_events (
  row_id             STRING,     -- sha1 of the source line; makes re-ingest idempotent
  logged_at          TIMESTAMP,
  lifecycle          STRING,
  incident_id        STRING,
  source_event_id    STRING,     -- detector's event_id (agreed schema)
  device_id          STRING,
  event_type         STRING,     -- alert-service type: 'fall' = agreed 'possible_fall'
  status             STRING,
  room               STRING,
  zone               STRING,
  camera_id          STRING,
  confidence         DOUBLE,
  person             STRING,
  details            STRING,
  detected_at        TIMESTAMP,
  priority           INT,
  escalations        INT,
  acked_by           STRING,
  person_detected    BOOLEAN,
  immobility_seconds DOUBLE,
  actor              STRING,     -- who cancelled / acknowledged / resolved
  outcome            STRING,
  raw                STRING      -- full original JSON row
) USING DELTA;

-- One row per incident, using the agreed schema's field names.
-- alert_sent (ntfy accepted an alert) and acknowledged_at are derived from the lifecycle rows.
CREATE OR REPLACE VIEW safestep.incidents AS
WITH ranked AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY incident_id ORDER BY logged_at DESC) AS rn
  FROM safestep.lifecycle_events
),
latest AS (SELECT * FROM ranked WHERE rn = 1),
-- Reviews use the existing raw JSON column; later delivery callbacks cannot erase them.
reviews AS (
  SELECT incident_id,
         get_json_object(raw, '$.review_outcome') AS review_outcome,
         get_json_object(raw, '$.review_note') AS review_note,
         get_json_object(raw, '$.reviewed_by') AS reviewed_by,
         CAST(get_json_object(raw, '$.reviewed_at') AS TIMESTAMP) AS reviewed_at,
         ROW_NUMBER() OVER (PARTITION BY incident_id ORDER BY logged_at DESC, row_id DESC) AS rn
  FROM safestep.lifecycle_events
  WHERE lifecycle = 'reviewed'
),
agg AS (
  SELECT incident_id,
         MIN(CASE WHEN lifecycle = 'alerted'      THEN logged_at END) AS alerted_at,
         MIN(CASE WHEN lifecycle = 'delivered'    THEN logged_at END) AS delivered_at,
         MAX(CASE WHEN lifecycle = 'delivery_failed' THEN 1 ELSE 0 END) AS had_delivery_failure,
         MIN(CASE WHEN lifecycle = 'acknowledged' THEN logged_at END) AS acknowledged_at,
         MIN(CASE WHEN lifecycle = 'cancelled'    THEN logged_at END) AS cancelled_at,
         MAX(escalations) AS escalations
  FROM safestep.lifecycle_events
  GROUP BY incident_id
)
SELECT l.incident_id,
       l.source_event_id AS event_id,
       l.device_id,
       l.room,
       l.detected_at     AS `timestamp`,
       l.event_type,
       l.confidence,
       l.status,                          -- latest lifecycle status
       l.person_detected,
       l.immobility_seconds,
       a.delivered_at IS NOT NULL AS alert_sent,   -- ntfy accepted at least one alert message
       a.had_delivery_failure = 1 AS delivery_failed,
       a.alerted_at,
       a.delivered_at,
       a.acknowledged_at,
       a.cancelled_at,
       a.escalations,
       r.review_outcome,
       r.review_note,
       r.reviewed_by,
       r.reviewed_at
FROM latest l
JOIN agg a ON l.incident_id = a.incident_id
LEFT JOIN reviews r ON l.incident_id = r.incident_id AND r.rn = 1;
