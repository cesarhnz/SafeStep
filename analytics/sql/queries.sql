-- Dashboard queries. All read safestep.incidents unless noted.

-- 1. Possible falls by room and hour of day
SELECT room, hour(`timestamp`) AS hour_of_day, COUNT(*) AS possible_falls
FROM safestep.incidents
WHERE event_type = 'fall'
GROUP BY room, hour(`timestamp`)
ORDER BY room, hour_of_day;

-- 2. False-alarm rate (person cancelled inside the confirmation window)
SELECT COUNT(*) AS possible_falls,
       SUM(CASE WHEN cancelled_at IS NOT NULL THEN 1 ELSE 0 END) AS cancelled,
       ROUND(100.0 * SUM(CASE WHEN cancelled_at IS NOT NULL THEN 1 ELSE 0 END) / COUNT(*), 1) AS false_alarm_pct
FROM safestep.incidents
WHERE event_type = 'fall';

-- 3. Time for a caregiver to acknowledge, by room
SELECT room,
       COUNT(*) AS acknowledged,
       ROUND(AVG(unix_timestamp(acknowledged_at) - unix_timestamp(alerted_at)), 1) AS avg_seconds_to_ack
FROM safestep.incidents
WHERE acknowledged_at IS NOT NULL AND alerted_at IS NOT NULL
GROUP BY room;

-- 4. Does confidence separate real alerts from cancelled ones? (use this to tune thresholds)
SELECT CASE WHEN cancelled_at IS NOT NULL THEN 'cancelled'
            WHEN acknowledged_at IS NOT NULL THEN 'acknowledged'
            WHEN alert_sent THEN 'alerted, no ack'
            WHEN alerted_at IS NOT NULL THEN 'ALERT NOT DELIVERED'
            ELSE 'pending' END AS outcome,
       COUNT(*) AS incidents,
       ROUND(AVG(confidence), 3) AS avg_confidence,
       ROUND(AVG(immobility_seconds), 1) AS avg_immobility_s
FROM safestep.incidents
WHERE event_type = 'fall'
GROUP BY 1;

-- 5. Incidents nobody answered in time
SELECT incident_id, room, `timestamp`, escalations
FROM safestep.incidents
WHERE escalations > 0
ORDER BY `timestamp` DESC;

-- 6. Alerts the service decided to send but ntfy never accepted (should be zero)
SELECT incident_id, room, `timestamp`, alerted_at
FROM safestep.incidents
WHERE alerted_at IS NOT NULL AND NOT alert_sent
ORDER BY `timestamp` DESC;

-- 7. Camera offline episodes (reads the lifecycle table)
SELECT camera_id, room, COUNT(*) AS offline_episodes
FROM safestep.lifecycle_events
WHERE event_type = 'camera_offline' AND lifecycle = 'detected'
GROUP BY camera_id, room;