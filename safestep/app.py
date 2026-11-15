"""Video loop + command-line entry point.

Teammates: the easiest way to hook in (countdown, alerts, dashboard) is the
`monitor()` generator:

    from safestep import monitor

    for result in monitor(source=0):
        for event in result.events:          # FallEvent: "fall" / "immobility" / "recovered"
            send_alert(event.to_dict(), snapshot=result.frame)
        show_on_dashboard(result.annotated)  # BGR numpy image with skeletons

`main()` (the `safestep` command) already does this for the SafeStep alert
service: it posts every event in the agreed schema and sends heartbeats.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .detector import FallConfig, FallDetector, FallEvent
from .draw import draw_overlay
from .events import to_schema_event
from .alerts import AlertClient
from .pose import Person, PoseEstimator


@dataclass
class FrameResult:
    frame: np.ndarray          # original BGR frame (use for alert snapshots)
    annotated: np.ndarray      # frame with skeletons / status drawn
    people: list[Person]
    events: list[FallEvent]    # events that fired on THIS frame
    fallen_ids: list[int]      # everyone currently in the FALLEN state
    timestamp: float           # seconds since start (video time for files)
    fps: float


def open_source(source: str | int, width: int | None = None, height: int | None = None) -> cv2.VideoCapture:
    source_path = Path(str(source))
    is_device_path = (
        source_path.parent == Path("/dev")
        and source_path.name.startswith("video")
        and source_path.name[5:].isdigit()
    )
    if isinstance(source, int) or str(source).isdigit():
        backend = cv2.CAP_V4L2 if sys.platform.startswith("linux") else cv2.CAP_ANY
        cap = cv2.VideoCapture(int(source), backend)
    elif is_device_path:
        backend = cv2.CAP_V4L2 if sys.platform.startswith("linux") else cv2.CAP_ANY
        cap = cv2.VideoCapture(str(source_path), backend)
    else:
        if not Path(source).exists():
            raise FileNotFoundError(source)
        cap = cv2.VideoCapture(str(source))
    if isinstance(source, int) or str(source).isdigit() or is_device_path:
        # MJPG gives higher frame rates than raw YUYV on most USB webcams.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        if width and height:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video source {source!r}")
    return cap


def monitor(
    source: str | int = 0,
    estimator: PoseEstimator | None = None,
    detector: FallDetector | None = None,
    privacy: bool = False,
    width: int | None = 1280,
    height: int | None = 720,
) -> Iterator[FrameResult]:
    """Yield a FrameResult for every frame of a webcam index or video file."""
    estimator = estimator or PoseEstimator()
    detector = detector or FallDetector()
    is_file = not (isinstance(source, int) or str(source).isdigit())
    cap = open_source(source, width, height)
    video_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

    start = time.monotonic()
    last = start
    fps = 0.0
    i = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            # Video files use their own timeline so results don't depend on how
            # fast this machine processes them; webcams use the wall clock.
            t = i / video_fps if is_file else time.monotonic() - start

            people = estimator(frame)
            events = detector.update(people, t)

            now = time.monotonic()
            inst = 1.0 / max(now - last, 1e-6)
            fps = inst if fps == 0 else 0.9 * fps + 0.1 * inst
            last = now

            annotated = draw_overlay(frame, people, detector, privacy=privacy, fps=fps)
            yield FrameResult(frame, annotated, people, events, detector.fallen_ids, t, fps)
            i += 1
    finally:
        cap.release()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="SafeStep: camera-based fall detection")
    ap.add_argument("--source", default="0", help="webcam index (0, 1, ...) or path to a video file")
    ap.add_argument("--model", default="yolo26n-pose.pt", help="YOLO pose weights (n/s/m: faster -> more accurate)")
    ap.add_argument("--imgsz", type=int, default=640, help="model input size; lower = faster")
    ap.add_argument("--device", default=None, help="e.g. cpu, 0 (CUDA), mps")
    ap.add_argument("--width", type=int, default=1280, help="webcam capture width")
    ap.add_argument("--height", type=int, default=720, help="webcam capture height")
    ap.add_argument("--privacy", action="store_true", help="show skeletons only, no camera image")
    ap.add_argument("--no-show", action="store_true", help="don't open a preview window")
    ap.add_argument("--diagnostics", action="store_true",
                    help="log tracked-person arrivals, departures, and periodic frame status")
    ap.add_argument("--save", help="write the annotated video to this path (e.g. output/demo.mp4)")
    ap.add_argument("--events", help="append events as JSON lines to this file")
    ap.add_argument("--device-id", default=os.getenv("SAFESTEP_DEVICE_ID", "uno_q_01"),
                    help="device identifier written to the event schema")
    ap.add_argument("--room", default=os.getenv("SAFESTEP_ROOM", "living_room"),
                    help="room label written to the event schema")
    ap.add_argument("--alert-url", default=os.getenv("SAFESTEP_ALERT_URL", "http://127.0.0.1:8080"),
                    help="base URL of alerts/alert_service.py")
    ap.add_argument("--no-alerts", action="store_true", help="do not POST events to the alert service")
    ap.add_argument("--heartbeat", type=float, default=15.0,
                    help="seconds between heartbeats to the alert service")
    ap.add_argument("--immobility", type=float, default=FallConfig.immobility_s,
                    help="seconds lying without meaningful movement before a 'still on the floor' event")
    ap.add_argument("--descent", type=float, default=FallConfig.descent_threshold,
                    help="hip drop (torso lengths) that counts as a rapid fall")
    ap.add_argument("--lying-angle", type=float, default=FallConfig.lying_angle_deg,
                    help="torso angle from vertical (deg) that counts as lying")
    args = ap.parse_args(argv)

    cfg = FallConfig(descent_threshold=args.descent, lying_angle_deg=args.lying_angle,
                     immobility_s=args.immobility)
    estimator = PoseEstimator(model=args.model, imgsz=args.imgsz, device=args.device)
    detector = FallDetector(cfg)

    is_webcam = args.source.isdigit()
    file_fps = 30.0
    if not is_webcam and Path(args.source).exists():
        probe = cv2.VideoCapture(args.source)
        file_fps = probe.get(cv2.CAP_PROP_FPS) or 30.0
        probe.release()
    frames_seen = 0
    writer = None
    events_file = None
    if args.events:
        Path(args.events).parent.mkdir(parents=True, exist_ok=True)
        events_file = open(args.events, "a")
    client = None if args.no_alerts else AlertClient(
        args.alert_url, args.device_id, args.room, heartbeat_s=args.heartbeat)
    stop_reason = "video source ended"
    active_tracks: dict[int, tuple[float, float, list[float]]] = {}
    last_diagnostic = time.monotonic()

    print("SafeStep running. Press q or Esc in the window to quit.", file=sys.stderr)
    try:
        for r in monitor(args.source, estimator, detector, args.privacy, args.width, args.height):
            if args.diagnostics:
                now = time.monotonic()
                visible = {p.track_id: p for p in r.people}
                for track_id, (last_seen, _, _) in list(active_tracks.items()):
                    if track_id not in visible and now - last_seen >= 1.0:
                        print(f"[safestep] skeleton lost: track_id={track_id}",
                              file=sys.stderr, flush=True)
                        del active_tracks[track_id]

                for track_id, person in visible.items():
                    if track_id not in active_tracks:
                        bbox = [round(float(v)) for v in person.bbox]
                        print(
                            f"[safestep] skeleton acquired: track_id={track_id} "
                            f"confidence={person.score:.2f} bbox={bbox}",
                            file=sys.stderr,
                            flush=True,
                        )
                    active_tracks[track_id] = (now, person.score, person.bbox.tolist())

                if now - last_diagnostic >= 5.0:
                    ids = sorted(visible)
                    print(
                        f"[safestep] camera active: fps={r.fps:.1f} "
                        f"visible_skeletons={len(ids)} track_ids={ids}",
                        file=sys.stderr,
                        flush=True,
                    )
                    last_diagnostic = now

            for ev in r.events:
                event = to_schema_event(ev, args.device_id, args.room)
                line = json.dumps(event)
                print(line, flush=True)  # stdout = event stream, one JSON object per line
                if events_file:
                    events_file.write(line + "\n")
                    events_file.flush()
                if client:
                    client.send(event)
            if client:
                client.heartbeat(r.fps)  # only sent while frames are actually being processed

            if args.save:
                frames_seen += 1
                # Webcam: wait until the measured FPS settles so playback speed is right.
                if writer is None and (not is_webcam or frames_seen > 20):
                    Path(args.save).parent.mkdir(parents=True, exist_ok=True)
                    h, w = r.annotated.shape[:2]
                    out_fps = r.fps if is_webcam else file_fps
                    writer = cv2.VideoWriter(args.save, cv2.VideoWriter_fourcc(*"mp4v"), out_fps, (w, h))
                if writer is not None:
                    writer.write(r.annotated)

            if not args.no_show:
                cv2.imshow("SafeStep", r.annotated)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    stop_reason = "stopped by user"
                    break
    except KeyboardInterrupt:
        stop_reason = "stopped by user"
    except Exception as exc:
        stop_reason = f"detector error: {type(exc).__name__}"
        raise
    finally:
        if client:
            client.close(stop_reason)
        if writer:
            writer.release()
        if events_file:
            events_file.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
