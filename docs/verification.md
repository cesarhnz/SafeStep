# Verification record

Recorded November 15, 2026, for branch `codex/document-safestep`, based on merge `c492d40` (detector integration).

## Deployment status

The project owner confirmed that the **Databricks dashboard, phone notifications, and email delivery are working**. This records that confirmation; the documentation cleanup did not trigger another live notification, email, or deployment. Newly edited local code and dashboard definitions still need normal deployment to affect the running system.

## Local validation

| Area | Result | Scope |
| --- | --- | --- |
| Detector and integration | 28 passed | Synthetic pose sequences, event serialization, ordered HTTP delivery, real local alert handler, recovery and heartbeat behavior; outgoing notifications captured |
| Alert service | 32 passed | Contract validation, follow-ups, timers, acknowledgements, reviews, restart recovery for closed incidents |
| Analytics | 38 passed | Ingestion/checkpoints, incident-view behavior, demo data, weekly metrics, native email configuration and mocked SMTP/Databricks behavior |
| Python compilation | Passed | `safestep/`, `alerts/`, `analytics/` |
| Diff whitespace | Passed | `git diff --check` |

The detector checks were run in a temporary environment with pytest and NumPy, without installing/downloading the full camera inference stack:

```sh
UV_CACHE_DIR=/tmp/safestep-uv-cache uv run --no-project --with pytest --with numpy python -m pytest tests -q
python3 -m unittest discover -s alerts/tests -q
python3 -m unittest discover -s analytics/tests -q
python3 -m compileall -q safestep alerts analytics
git diff --check
```

## Merge corrections included

The updated checkout contained integration regressions discovered while validating documentation:

- Restored the missing follow-up event-type constant so linked recovery and immobility events validate successfully.
- Updated fall-notification evidence labels and a stale test reference to use canonical `fall_detected` naming.
- Allowed caregiver reviews of detector-recovered incidents, consistently with the existing closed-status policy.
- Excluded recovered incidents from the custom digest's open response/delivery queues and included them in its review queue.
- Included recovered incidents in the exported dashboard's awaiting-review metric and label.

The obsolete empty `EdgeDetection/` folder was removed; the active `safestep/` implementation remains.

## Validation boundaries

Synthetic tests establish behavior for the exercised scenarios, not real-world fall-detection accuracy. This run did not exercise a physical camera, YOLO inference performance, or fresh cloud deployment. Active alert timers, detector queues, and event-ID caches remain in-memory; service restart does not restore an active incident's reminders. Native dashboard snapshots and the optional custom SMTP digest have different reporting-window behavior, described in the [architecture guide](architecture.md).
