"""
Minimal standalone test for software triggering on the Tucsen camera driver.

Goal: isolate whether our TucsenCamera wrapper correctly delivers a frame per
software trigger — both back-to-back (like live view) and with long idle gaps
between triggers (like a multipoint acquisition doing stage moves / AF between
channel captures).

The current multipoint bug symptom is that the worker's frame callback never
fires after send_trigger(). This script exercises exactly that path, one layer
below the MultiPointWorker.

Run as:
    conda activate squid
    python 11_tucsen_sw_trigger_test.py

Edit CAMERA_MODEL / PIXEL_FORMAT / BINNING below to match your hardware if the
defaults are wrong for your setup.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

# Make `import control.*` / `import squid.*` resolve the same way as the app.
SOFTWARE_DIR = Path(__file__).resolve().parent / "software"
if str(SOFTWARE_DIR) not in sys.path:
    sys.path.insert(0, str(SOFTWARE_DIR))

from squid.abc import CameraAcquisitionMode, CameraFrame
from squid.config import (
    CameraConfig,
    CameraPixelFormat,
    CameraVariant,
    TucsenCameraModel,
)
from control.camera_tucsen import TucsenCamera


# ---- Adapt these if your camera is different ---------------------------------
CAMERA_MODEL = TucsenCameraModel.ARIES_6506  # e.g. ARIES_6510, DHYANA_400BSI_V3
PIXEL_FORMAT = CameraPixelFormat.MONO16
BINNING = (2, 2)
EXPOSURE_MS = 20.0
# ------------------------------------------------------------------------------


def build_config() -> CameraConfig:
    return CameraConfig(
        camera_type=CameraVariant.TUCSEN,
        camera_model=CAMERA_MODEL,
        default_pixel_format=PIXEL_FORMAT,
        default_binning=BINNING,
    )


class FrameCounter:
    """Records frames received via the driver's callback path."""

    def __init__(self):
        self.count = 0
        self.last_frame_id = None
        self.got_frame = threading.Event()
        self._lock = threading.Lock()

    def on_frame(self, camera_frame: CameraFrame):
        with self._lock:
            self.count += 1
            self.last_frame_id = camera_frame.frame_id
            mean = float(camera_frame.frame.mean())
            mx = int(camera_frame.frame.max())
        print(
            f"  [callback] frame_id={camera_frame.frame_id} "
            f"shape={camera_frame.frame.shape} mean={mean:.1f} max={mx}"
        )
        self.got_frame.set()

    def arm(self):
        self.got_frame.clear()


def test_trigger_burst(camera, counter: FrameCounter, n_triggers: int, gap_s: float) -> int:
    """Send n_triggers software triggers spaced by gap_s and wait for each frame.
    Returns the number of frames we successfully saw via the callback."""
    received = 0
    per_frame_timeout_s = max(1.0, (EXPOSURE_MS / 1000.0) * 5 + 2.0)

    for i in range(n_triggers):
        if gap_s > 0 and i > 0:
            print(f"  sleeping {gap_s:.2f}s before trigger {i + 1}")
            time.sleep(gap_s)

        counter.arm()
        # Respect the driver's rate-limit gate so we don't get 'too early' errors.
        wait_start = time.time()
        while not camera.get_ready_for_trigger():
            if time.time() - wait_start > 2.0:
                print("  WARN: camera.get_ready_for_trigger() never went True")
                break
            time.sleep(0.001)

        t0 = time.time()
        camera.send_trigger(illumination_time=EXPOSURE_MS)
        ok = counter.got_frame.wait(per_frame_timeout_s)
        dt = time.time() - t0

        if ok:
            received += 1
            print(f"  trigger {i + 1}/{n_triggers}: frame arrived in {dt * 1000:.1f} ms")
        else:
            print(
                f"  trigger {i + 1}/{n_triggers}: TIMEOUT after {dt:.2f}s "
                f"(timeout={per_frame_timeout_s:.2f}s)"
            )

    return received


def main() -> int:
    print(f"=== Tucsen software-trigger test ({CAMERA_MODEL.value}) ===")
    config = build_config()

    # We never use hardware trigger here; pass None for the two hooks.
    camera = TucsenCamera(config, hw_trigger_fn=None, hw_set_strobe_delay_ms_fn=None)

    try:
        camera.set_exposure_time(EXPOSURE_MS)
        print(f"Current acquisition_mode pre-switch: {camera.get_acquisition_mode()}")
        camera.set_acquisition_mode(CameraAcquisitionMode.SOFTWARE_TRIGGER)
        print(f"Current acquisition_mode post-switch: {camera.get_acquisition_mode()}")

        counter = FrameCounter()
        cb_id = camera.add_frame_callback(counter.on_frame)

        camera.start_streaming()
        print(f"Streaming: {camera.get_is_streaming()}")

        # ---- Case 1: burst at ~10 Hz, like live view ------------------------
        print("\n[Case 1] 5 triggers with 100 ms spacing (live-view-like)")
        got = test_trigger_burst(camera, counter, n_triggers=5, gap_s=0.1)
        print(f"  result: {got}/5 frames received\n")

        # # ---- Case 2: long idle gap, like multipoint stage-move/AF ------------
        # print("[Case 2] 5 triggers with 3 s spacing (multipoint-like)")
        # got_slow = test_trigger_burst(camera, counter, n_triggers=5, gap_s=3.0)
        # print(f"  result: {got_slow}/5 frames received\n")

        # print(f"Total frames counted by callback: {counter.count}")
        # ok = (got == 5) and (got_slow == 5)
        # print("PASS" if ok else "FAIL — one or more triggers did not produce a frame")

        camera.remove_frame_callback(cb_id)
        camera.stop_streaming()
        return 0 if ok else 1
    finally:
        try:
            camera.close()
        except Exception as e:
            print(f"(warn) error during camera.close(): {e}")


if __name__ == "__main__":
    sys.exit(main())
