"""
Quick test: NIDAQ digital output line 12 triggers a Toupcam camera via hardware trigger.

Usage:
    conda activate squid
    python test_hw_trigger_toupcam.py

What it does:
    1. Opens the first Toupcam, puts it in external trigger mode (edge, GPIO0 source)
    2. Starts the pull-mode callback so we get notified of frames
    3. Creates a simple NIDAQ digital output pulse on line 12
    4. Fires the pulse and waits for a frame callback
    5. Prints frame info and saves a raw numpy file for inspection
"""

import sys
import os
import time
import threading
import ctypes
import numpy as np

# Add the software directory to path so we can import control modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import control.toupcam as toupcam

# ── Configuration ──
DO_LINE = 12               # NIDAQ digital output line
DEVICE_NAME = "Dev1"       # NIDAQ device name
DO_PORT = "port0"
EXPOSURE_TIME_US = 10000   # 10 ms exposure
TRIGGER_PULSE_MS = 0.04       # Width of the trigger pulse
NUM_TRIGGERS = 3           # Number of trigger pulses to send
TIMEOUT_S = 5.0            # Timeout waiting for each frame


def main():
    # ── Step 1: Open Toupcam ──
    print("Enumerating Toupcam devices...")
    devices = toupcam.Toupcam.EnumV2()
    if len(devices) == 0:
        print("ERROR: No Toupcam devices found. Is the camera connected?")
        return 1
    for i, dev in enumerate(devices):
        print(f"  [{i}] {dev.displayname} (SN: {dev.id})")

    print(f"\nOpening device 0: {devices[0].displayname}")
    cam = toupcam.Toupcam.Open(devices[0].id)
    if cam is None:
        print("ERROR: Failed to open camera")
        return 1

    try:
        # ── Step 2: Configure camera ──
        # RAW mode, 8-bit
        cam.put_Option(toupcam.TOUPCAM_OPTION_RAW, 1)
        cam.put_Option(toupcam.TOUPCAM_OPTION_BITDEPTH, 0)  # 8-bit

        # Set exposure
        cam.put_ExpoTime(EXPOSURE_TIME_US)
        actual_expo = cam.get_ExpoTime()
        print(f"Exposure time: requested {EXPOSURE_TIME_US} us, actual {actual_expo} us")

        # Get resolution for buffer sizing
        width, height = cam.get_Size()
        print(f"Resolution: {width} x {height}")

        # Allocate read buffer (8-bit RAW = 1 byte per pixel)
        buf_size = width * height
        read_buffer = bytes(buf_size)

        # ── Step 3: Set up frame event ──
        frame_event = threading.Event()
        frame_count = [0]
        frame_times = []

        def event_callback(nEvent, ctx):
            if nEvent == toupcam.TOUPCAM_EVENT_IMAGE:
                frame_event.set()
                frame_count[0] += 1
                frame_times.append(time.time())
            elif nEvent == toupcam.TOUPCAM_EVENT_TRIGGERFAIL:
                print("  WARNING: TOUPCAM_EVENT_TRIGGERFAIL received!")
            elif nEvent == toupcam.TOUPCAM_EVENT_NOFRAMETIMEOUT:
                print("  WARNING: TOUPCAM_EVENT_NOFRAMETIMEOUT received!")
            elif nEvent == toupcam.TOUPCAM_EVENT_TRIGGER_ALLOW:
                pass  # Camera is ready for next trigger

        print("Starting pull mode with callback...")
        cam.StartPullModeWithCallback(event_callback, cam)

        # ── Step 4: Set external trigger mode (EDGE, GPIO0) ──
        print("Setting external trigger mode...")
        cam.put_Option(toupcam.TOUPCAM_OPTION_TRIGGER, 2)  # External trigger

        # Trigger source = GPIO0 (index 1 for the trigger config)
        cam.IoControl(1, toupcam.TOUPCAM_IOCONTROLTYPE_SET_TRIGGERSOURCE, 1)  # GPIO0
        # Rising edge activation
        cam.IoControl(0, toupcam.TOUPCAM_IOCONTROLTYPE_SET_INPUTACTIVATION, 0)  # Rising edge

        # Set GPIO1 as output for trigger-wait feedback (optional but useful)
        try:
            cam.IoControl(3, toupcam.TOUPCAM_IOCONTROLTYPE_SET_OUTPUTMODE, 0)  # Frame trigger wait
            cam.IoControl(3, toupcam.TOUPCAM_IOCONTROLTYPE_SET_OUTPUTINVERTER, 0)
        except Exception as e:
            print(f"  (GPIO1 output setup skipped: {e})")

        print("Camera is now in external trigger mode (edge, GPIO0)")

        # ── Step 5: Set up NIDAQ digital output ──
        print(f"\nSetting up NIDAQ {DEVICE_NAME}/{DO_PORT}/line{DO_LINE}...")
        import nidaqmx
        from nidaqmx.constants import LineGrouping, AcquisitionType, LogicFamily
        from nidaqmx.system.physical_channel import PhysicalChannel

        SAMPLE_RATE_HZ = 100_000  # 100 kHz

        # Configure port logic family to 3.3V BEFORE creating any tasks
        do_port_name = f"{DEVICE_NAME}/{DO_PORT}"
        phys_channel = PhysicalChannel(do_port_name)
        phys_channel.dig_port_logic_family = LogicFamily.THREE_POINT_THREE_V
        print(f"  Set {do_port_name} logic family to 3.3V")

        # First make sure the line starts LOW
        with nidaqmx.Task("do_init") as task:
            task.do_channels.add_do_chan(
                f"{DEVICE_NAME}/{DO_PORT}/line{DO_LINE}",
                line_grouping=LineGrouping.CHAN_PER_LINE,
            )
            task.write(False)
        print("  Line initialized LOW")

        # Give camera a moment to be ready
        time.sleep(0.5)

        # ── Step 6: Send triggers and collect frames ──
        print(f"\nSending {NUM_TRIGGERS} trigger pulses (pulse width = {TRIGGER_PULSE_MS} ms)...\n")

        # Build a single-pulse waveform at 100 kHz
        pulse_samples = max(1, int(SAMPLE_RATE_HZ * TRIGGER_PULSE_MS / 1000.0))
        # Pad with LOW after the pulse so the task has a clean finish
        pad_samples = max(2, pulse_samples)
        waveform = np.concatenate([
            np.ones(pulse_samples, dtype=bool),
            np.zeros(pad_samples, dtype=bool),
        ])
        total_samples = len(waveform)
        print(f"  Waveform: {pulse_samples} HIGH + {pad_samples} LOW samples at {SAMPLE_RATE_HZ/1000:.0f} kHz")

        for i in range(NUM_TRIGGERS):
            frame_event.clear()
            t_start = time.time()

            # Use clocked DO output for precise pulse timing
            with nidaqmx.Task(f"do_trigger_{i}") as task:
                task.do_channels.add_do_chan(
                    f"{DEVICE_NAME}/{DO_PORT}/line{DO_LINE}",
                    line_grouping=LineGrouping.CHAN_PER_LINE,
                )
                task.timing.cfg_samp_clk_timing(
                    rate=SAMPLE_RATE_HZ,
                    sample_mode=AcquisitionType.FINITE,
                    samps_per_chan=total_samples,
                )
                task.write(waveform, auto_start=True)
                task.wait_until_done(timeout=5.0)

            # Wait for the frame
            got_frame = frame_event.wait(timeout=TIMEOUT_S)
            elapsed = time.time() - t_start

            if got_frame:
                # Pull the image
                cam.PullImageV2(read_buffer, 8, None)
                image = np.frombuffer(read_buffer, dtype=np.uint8).reshape(height, width)
                print(
                    f"  Frame {i+1}/{NUM_TRIGGERS}: received in {elapsed*1000:.1f} ms | "
                    f"shape={image.shape} | mean={image.mean():.1f} | min={image.min()} | max={image.max()}"
                )

                # Save the last frame for inspection
                if i == NUM_TRIGGERS - 1:
                    fname = "test_hw_trigger_frame.npy"
                    np.save(fname, image)
                    print(f"\n  Last frame saved to {fname}")
            else:
                print(f"  Frame {i+1}/{NUM_TRIGGERS}: TIMEOUT after {TIMEOUT_S}s - no frame received!")

            # Small delay between triggers
            time.sleep(0.1)

        # ── Summary ──
        print(f"\n{'='*50}")
        print(f"Summary: {frame_count[0]} frames received out of {NUM_TRIGGERS} triggers")
        if len(frame_times) >= 2:
            intervals = np.diff(frame_times)
            print(f"  Inter-frame intervals: {[f'{dt*1000:.1f} ms' for dt in intervals]}")
        if frame_count[0] == NUM_TRIGGERS:
            print("SUCCESS: All triggers produced frames!")
        else:
            print("ISSUE: Not all triggers produced frames. Check wiring and trigger polarity.")

    finally:
        # ── Cleanup ──
        print("\nCleaning up...")
        try:
            cam.put_Option(toupcam.TOUPCAM_OPTION_TRIGGER, 0)  # Back to video mode
        except Exception:
            pass
        cam.Close()
        print("Done.")

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
