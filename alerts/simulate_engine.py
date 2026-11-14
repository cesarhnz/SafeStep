#!/usr/bin/env python3
"""
Fake detection engine for demos and testing. Sends the same events the real
YOLO pose pipeline would send.

    python simulate_engine.py fall           # fall, nobody cancels -> URGENT alert
    python simulate_engine.py fall-cancel    # fall, person cancels inside the window
    python simulate_engine.py help           # person signals for help
    python simulate_engine.py floor          # still on the floor after a fall
    python simulate_engine.py hazards        # potential dangers (stumble, obstacle, dark, night)
    python simulate_engine.py offline        # heartbeats then silence -> camera offline alert
    python simulate_engine.py demo           # a short tour of everything
"""

import argparse
import time

from safestep_client import SafeStepClient


def run(scenario: str, ss: SafeStepClient) -> None:
    if scenario == "fall":
        inc = ss.fall(confidence=0.93, zone="between couch and coffee table", person="Maria",
                      details="Rapid drop from standing to floor; torso horizontal 4s")
        print("fall incident:", inc, "- no cancel sent; urgent alert fires after the window")

    elif scenario == "fall-cancel":
        inc = ss.fall(confidence=0.71, zone="by the bed", person="Maria")
        print("fall incident:", inc, "- cancelling in 3s (person says they're OK)")
        time.sleep(3)
        ss.cancel(inc)

    elif scenario == "help":
        print("help:", ss.help_request(person="Maria", details="Held 'help' gesture for 3s"))

    elif scenario == "floor":
        print("immobility:", ss.immobility(seconds_on_floor=95, person="Maria", zone="hallway"))

    elif scenario == "hazards":
        sent = [ss.hazard("near_fall", confidence=0.8, zone="bathroom doorway",
                  details="Lost balance, caught themselves on door frame"),
                ss.hazard("obstacle", zone="hallway", details="Object detected in walking path for 10 min"),
                ss.hazard("low_light", details="Walking with room brightness below 10%"),
                ss.hazard("night_wandering", details="Walking at 3:12 AM")]
        for t in sent:
            t.join()
        print("sent 4 hazard events")

    elif scenario == "offline":
        ss.start_heartbeat(every_s=3)
        print("heartbeating for 10s, then going silent. Watch for the offline alert...")
        time.sleep(10)
        ss.stop_heartbeat()

    elif scenario == "demo":
        for s in ("hazards", "fall-cancel", "fall"):
            print(f"\n=== {s} ===")
            run(s, ss)
            time.sleep(3)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="SafeStep fake detection engine")
    p.add_argument("scenario", choices=["fall", "fall-cancel", "help", "floor", "hazards", "offline", "demo"])
    p.add_argument("--url", default="http://127.0.0.1:8080")
    p.add_argument("--room", default="Living room")
    p.add_argument("--camera", default="cam-1")
    a = p.parse_args()
    run(a.scenario, SafeStepClient(a.url, camera_id=a.camera, room=a.room))
