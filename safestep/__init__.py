"""SafeStep: camera-based fall detection."""

from .detector import FallConfig, FallDetector, FallEvent, Phase
from .pose import Person, PoseEstimator


def monitor(*args, **kwargs):
    """See safestep.app.monitor (imported lazily so `import safestep` stays light)."""
    from .app import monitor as _monitor

    return _monitor(*args, **kwargs)


__all__ = ["FallConfig", "FallDetector", "FallEvent", "Phase", "Person", "PoseEstimator", "monitor"]
