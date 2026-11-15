# SafeStep

Fall monitoring prototype with camera pose detection, caregiver alerts, and Databricks analytics.

- [Alert service setup](alerts/README.md)
- [Databricks integration setup and recovery](analytics/README.md)
- `safestep/`: pose estimation, fall detection, event serialization, and alert-service client

## Run the detector

Install the Python project with `uv sync`, then start the alert service in another terminal with `python alerts/alert_service.py`. Start detection with:

```sh
uv run safestep --source 0 --device-id uno_q_01 --room living_room
```

Detected falls are sent asynchronously to `http://127.0.0.1:8080/events` by default. Set `SAFESTEP_ALERT_URL` to use another service URL, `SAFESTEP_ALERT_API_KEY` if the service requires a key, and `SAFESTEP_DEVICE_ID` / `SAFESTEP_ROOM` to configure the event identity. Use `--no-alerts` to run detection without posting events.

The detector emits the agreed JSON event shape. `event_type` is `possible_fall` for a confirmed fall and maps to the alert service's `fall` incident type. Velocity metrics are normalized by torso length per second. Recovery events are written to the local event output but are not sent to the alert service.
