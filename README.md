# SafeStep

Camera-based fall detection. A YOLO pose model tracks every person in view.
A per-person state machine flags a **fall** when someone drops fast **and**
ends up lying down. It flags a **recovery** when they stand back up.

## Setup

```bash
uv sync          # installs everything (CPU-only torch on Linux)
```

The pose weights (`yolo26n-pose.pt`, ~7 MB) download automatically on first run.

## Run

```bash
uv run safestep                             # webcam 0, preview window
uv run safestep --source 1                  # a different webcam
uv run safestep --source clips/fall.mp4     # a video file (great for testing/backup demo)
uv run safestep --privacy                   # skeletons on black, no camera image
uv run safestep --save output/demo.mp4 --events output/events.jsonl
```

Press `q` or `Esc` in the window to quit. Run `uv run safestep --help` for all options.

**Colors:** green = normal, orange = descending (possible fall), red = fallen.

## Hooking in (countdown / alerts / dashboard)

**Option A: Python.** Iterate over `monitor()`:

```python
from safestep import monitor

for result in monitor(source=0, privacy=False):
    for event in result.events:          # fires once per fall / recovery
        if event.type == "fall":
            start_countdown(event.track_id, snapshot=result.frame)
        elif event.type == "recovered":
            cancel_countdown(event.track_id)
    dashboard.update_frame(result.annotated, fallen=result.fallen_ids)
```

`FrameResult` fields: `frame` (raw BGR image), `annotated` (with overlay),
`people`, `events`, `fallen_ids`, `timestamp`, `fps`.

**Option B: separate process.** Each event is printed to stdout as one JSON line:

```json
{"type": "fall", "track_id": 3, "timestamp": 12.4, "bbox": [412.0, 380.5, 801.2, 590.0],
 "details": {"drop_torso_lengths": 1.6, "peak_speed_torso_lengths_per_s": 5.2,
             "torso_angle_deg": 82.0, "fall_started_at": 11.7}}
{"type": "recovered", "track_id": 3, "timestamp": 20.1, "bbox": [...], "details": {"fallen_for_s": 7.7}}
```

```bash
uv run safestep | python alert_service.py
```

## How detection works

[`safestep/detector.py`](safestep/detector.py) measures each person's posture on every frame:

- **Hip height**: the midpoint of the hip keypoints.
- **Torso angle**: the angle of the hip-to-shoulder line, measured from vertical.
- **Bounding-box aspect ratio**: width divided by height.

Distances are measured in **torso lengths**, so the same thresholds work whether the
person is near or far from the camera.

```
NORMAL ──hip drops ≥1 torso length within 1s──▶ DESCENDING ──lying ≥0.6s──▶ FALLEN
   ▲                                                │                          │
   └──────── didn't end up lying within 2.5s ───────┘                          │
   └─────────────────────── upright ≥1.5s ─────────────────────────────────────┘
```

Requiring both conditions filters out the usual false alarms:

| Action | Fast drop? | Ends up lying? | Result |
|---|---|---|---|
| Falling | ✅ | ✅ | **fall** |
| Sitting down quickly | ✅ | ❌ | ignored |
| Lying down on purpose | ❌ (slow) | ✅ | ignored |
| Bending over | ❌ | ❌ | ignored |

If the tracker gives a person a new ID mid-fall (fast motion makes this common),
the detector links the new ID back to the one that just disappeared, so the fall
isn't missed.

Every threshold is in `FallConfig`. The most useful ones to tune are on the CLI:
`--descent` (lower = more sensitive) and `--lying-angle`.

## Tests

```bash
uv run pytest
```

The tests feed synthetic skeletons through the detector: falls, sitting, lying
down slowly, bending, recovery, ID switches and multiple people. No camera needed.

## Known limitations

- **Falling straight toward or away from the camera** can look upright in 2D.
  The bounding-box aspect check helps, but a side-on camera angle works best.
- Someone who falls **completely behind furniture** disappears from view, so the
  fall can't be confirmed.
- Someone who was already on the floor before the program started won't trigger
  a fall, because the program never saw them drop.
