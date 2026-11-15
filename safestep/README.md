# Detector

The active detector implementation is this package. It combines YOLO pose estimation and ByteTrack person tracking with a temporal fall state machine. See the [operating guide](../docs/operations.md) for service startup and the [architecture guide](../docs/architecture.md) for HTTP payloads and alert transitions.

## Run and configure

```sh
uv sync
uv run safestep --source 0 --device-id uno_q_01 --room "Living room"
# Headless tracking diagnostics: logs skeleton arrivals/departures and FPS/status.
uv run safestep --source /dev/video2 --no-show --diagnostics
# Offline processing: no alert-service calls, heartbeats, or preview window.
uv run safestep --source clip.mp4 --no-alerts --no-show --events logs/detector-events.jsonl
uv run safestep --help
```

| Option | Default / meaning |
| --- | --- |
| `--source` | `0`; webcam index or local video path |
| `--model` | `yolo26n-pose.pt`; pose weights used by the implementation |
| `--imgsz`, `--device` | `640`; optional inference device such as `cpu` or `mps` |
| `--width`, `--height` | Webcam request: 1280 × 720 |
| `--privacy` | Skeleton-only annotation; original frame remains in memory |
| `--no-show` | Disable the preview window |
| `--diagnostics` | Log tracked-person arrivals/departures and periodic FPS/visible-track status, including each fallen track's time on the floor, to stderr |
| `--save` | Optional annotated-video output path |
| `--events` | Optional append-only JSONL detector event output (not the service lifecycle log) |
| `--no-alerts` | Disable event posting and heartbeats |
| `--alert-url` | `SAFESTEP_ALERT_URL`, otherwise `http://127.0.0.1:8080` |
| `--device-id`, `--room` | `SAFESTEP_DEVICE_ID` / `SAFESTEP_ROOM`; defaults `uno_q_01` / `living_room` |
| `--heartbeat` | 15 seconds, sent only while frames are processed |
| `--immobility` | 60 seconds without meaningful movement |
| `--descent` | Hip drop threshold: 1 torso length |
| `--lying-angle` | Torso angle from vertical: 55 degrees |

Set `SAFESTEP_ALERT_API_KEY` to the alert service's `api_key` when enabled. Model initialization may download weights if they are not already available. Quit with `q`, Escape, or Ctrl-C. Closing sends a stopped heartbeat if alerts are enabled.

## Detection logic

`FallDetector.update(people, t)` tracks `NORMAL → DESCENDING → FALLEN`. A rapid hip drop followed by a lying posture held for 0.6 seconds confirms a detector fall. A drop that does not end in a lying posture returns to normal. Upright posture held for 1.5 seconds produces recovery. Sustained immobility produces one follow-up per fall.

Geometry is normalized by recent torso length. Track reassociation attempts to preserve fall state when the tracker changes IDs. Normal tracks expire after 3 seconds unseen; fallen tracks are retained for 30 seconds. Full defaults are in `FallConfig` in `detector.py`.

The evidence score weights hip drop (40%), peak downward speed (35%), and final torso angle (25%). It is a heuristic score, not a calibrated probability. Bands are low below 0.5, medium below 0.7, high below 0.85, and very high otherwise. YOLO person-detection confidence is a separate metric.

Video files use source-frame time for detector timing, while camera input uses elapsed monotonic time. Serialized HTTP events receive current UTC timestamps when emitted; replaying a clip into the live alert service therefore creates current incidents. Use `--no-alerts` for offline clip analysis.

## Python interface

```python
from safestep import monitor

for result in monitor(source=0, privacy=True):
    for event in result.events:
        print(event.to_dict())
```

`monitor()` yields original and annotated frames, tracked people, events, fallen IDs, source-relative timestamp and measured FPS. It does not itself send alerts; the CLI wires event serialization and the HTTP worker together. `events.py` converts detector `FallEvent` objects into the agreed HTTP schema.

## NCNN model on Arduino Uno Q

SafeStep accepts Ultralytics-exported model directories through `--model`. To export the pose checkpoint at 320 × 320 in NCNN format, run this from the repository root (Ultralytics may install its export dependencies on the first run):

```sh
uv run yolo export model=yolo26n-pose.pt format=ncnn imgsz=320
```

Ultralytics creates `yolo26n-pose_ncnn_model/`. Use that directory for inference and pass the matching input size explicitly:

```sh
uv run safestep --model yolo26n-pose_ncnn_model --imgsz 320 \
  --source /dev/video2 --no-show --diagnostics --width 640 --height 360 \
  --device-id uno_q_01 --room "Living room"
```

The NCNN export keeps the same pose task and can run on the CPU; export support alone does not guarantee faster inference on the Uno Q, so compare the reported FPS and skeleton stability with the PyTorch model. See the [Ultralytics NCNN export guide](https://docs.ultralytics.com/integrations/ncnn/) for export options.

Synthetic-pose and local HTTP integration tests live in `../tests/`. Real camera accuracy, occlusion behavior and inference performance require representative recordings or hardware checks.
