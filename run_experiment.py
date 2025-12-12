"""
Wrapper script that launches the Spinnaker camera capture and LabJack stream
so both run in sync with a shared frame count and trigger cadence.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from labjack_stream_control import run_pulsed_stream, START_PULSE_WIDTH_S, DATA_LOG_PATH
from spinnaker_trigger import acquire_triggered_frames, VIDEO_PATH

# ---------------------------------------------------------------------------
# Experiment parameters
# ---------------------------------------------------------------------------
TIME_IN_SECONDS = 180  # total experiment duration in seconds
TRIGGER_RATE_HZ = 30.0
NUM_FRAMES = int(TIME_IN_SECONDS * TRIGGER_RATE_HZ)
EXPOSURE_TIME_US = 10000.0
BINNING_FACTOR = 2
CAMERA_VIDEO_PATH = Path("captures/session01.avi")      #VIDEO_PATH
LABJACK_LOG_PATH = Path("captures/labjack_stream.csv")  #DATA_LOG_PATH


def main():
    """Start the camera capture thread, then fire the LabJack pulse train."""
    timeout_ms = max(1000, int((1.0 / TRIGGER_RATE_HZ) * 2000))
    cam_thread = threading.Thread(
        target=acquire_triggered_frames,
        kwargs={
            "num_frames": NUM_FRAMES,
            "video_path": CAMERA_VIDEO_PATH,
            "timeout_ms": timeout_ms,
            "exposure_us": EXPOSURE_TIME_US,
            "video_frame_rate": TRIGGER_RATE_HZ,
            "binning_factor": BINNING_FACTOR,
        },
        daemon=False,
    )

    cam_thread.start()
    # Give the camera time to enter acquisition mode before pulses begin.
    time.sleep(1.0)

    run_pulsed_stream(
        num_frames=NUM_FRAMES,
        trigger_rate_hz=TRIGGER_RATE_HZ,
        pulse_width_s=START_PULSE_WIDTH_S,
        log_path=LABJACK_LOG_PATH,
    )

    cam_thread.join()
    print("Experiment complete: camera frames saved and LabJack stream stopped.")


if __name__ == "__main__":
    main()
