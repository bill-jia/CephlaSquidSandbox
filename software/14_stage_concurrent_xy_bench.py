"""
Bench test: can the Squid+ MCU firmware execute XY moves concurrently?

Background: in multipoint acquisition, ``move_to_coordinate`` issues X and Y
moves serially (``move_x_to(blocking=True)`` → sleep → ``move_y_to(blocking=True)`` →
sleep). The motors are physically independent, so in principle both could move
at the same time for a ~2x speedup. Whether that works depends on firmware
behaviour: the MCU may (a) execute concurrently, (b) serialize internally,
or (c) drop the second command while busy with the first.

This script answers that by issuing the same set of XY round-trip moves two
ways and comparing wall-clock time and final-position accuracy:

  - serial    : move_x_to(blocking=True) ; sleep dwell ; move_y_to(blocking=True) ; sleep dwell
  - concurrent: move_x_to(blocking=False) ; move_y_to(blocking=False) ;
                mcu.wait_till_operation_is_completed() ; sleep dwell

Prereqs:
- Close any running Squid GUI/acquisition that holds the MCU serial port.
- Stage must have been homed by a prior GUI session. This script opens the
  microscope with ``skip_init=True`` so the MCU is not reset, preserving
  whatever homing + actuator config the GUI session left on the firmware.
  Without this, the MCU reset would wipe homing state and the firmware
  silently refuses absolute moves on an unhomed stage.
- Activate the conda env (``conda activate squid``) before running.

Usage::

    python 14_stage_concurrent_xy_bench.py

Output: per-mode total wall time, per-move wall time stats, and final-position
error for each mode.
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

SOFTWARE_DIR = Path(__file__).resolve().parent
if str(SOFTWARE_DIR) not in sys.path:
    sys.path.insert(0, str(SOFTWARE_DIR))

from control.microscope import Microscope


# ----------------------------------------------------------------------------
# Test parameters — edit if your rig needs different values
# ----------------------------------------------------------------------------

# Number of (+dx, +dy) → (origin, origin) round-trips per mode.
# Total moves per mode = 2 * N_ROUND_TRIPS.
N_ROUND_TRIPS = 10

# Per-axis offset magnitude for each round-trip (mm). Picked small to stay
# well inside the stage envelope regardless of starting position.
DX_MM = 2.0
DY_MM = 2.0

# Dwell after each move group (ms). 25 ms is what the rig's YAML uses for
# SCAN_STABILIZATION_TIME_MS_X/Y during multipoint.
DWELL_MS = 25

# Timeout for mcu.wait_till_operation_is_completed in concurrent mode.
# Generous relative to the ~300 ms max we expect for ~2 mm moves.
WAIT_TIMEOUT_S = 10.0


# ----------------------------------------------------------------------------


def _serial_move(stage, x_mm, y_mm):
    stage.move_x_to(x_mm)  # blocks until X done
    time.sleep(DWELL_MS / 1000.0)
    stage.move_y_to(y_mm)  # blocks until Y done
    time.sleep(DWELL_MS / 1000.0)


def _concurrent_move(stage, mcu, x_mm, y_mm):
    # Fire both commands back-to-back without waiting for the first ACK.
    stage.move_x_to(x_mm, blocking=False)
    stage.move_y_to(y_mm, blocking=False)
    # _cmd_id advances per command, so the busy flag clears when the MCU
    # ACKs the most recent (Y) command. If firmware serializes internally,
    # that's still correct (Y runs after X). If firmware runs them
    # concurrently on independent motors, Y's ACK arrives at ~max(t_X, t_Y).
    mcu.wait_till_operation_is_completed(WAIT_TIMEOUT_S)
    time.sleep(DWELL_MS / 1000.0)


def _run_mode(name, stage, mcu, origin_x, origin_y):
    durations = []
    drift_errors = []

    # N_ROUND_TRIPS round-trips: alternate (origin+dx, origin+dy) and (origin, origin)
    offsets = []
    for _ in range(N_ROUND_TRIPS):
        offsets.append((origin_x + DX_MM, origin_y + DY_MM))
        offsets.append((origin_x, origin_y))

    print(f"\n=== Mode: {name} ===")
    t_mode_start = time.perf_counter()
    for idx, (tx, ty) in enumerate(offsets):
        t0 = time.perf_counter()
        if name == "serial":
            _serial_move(stage, tx, ty)
        else:
            _concurrent_move(stage, mcu, tx, ty)
        dt = time.perf_counter() - t0
        durations.append(dt)

        pos = stage.get_pos()
        err_x = pos.x_mm - tx
        err_y = pos.y_mm - ty
        drift_errors.append((err_x, err_y))
        print(
            f"  move {idx + 1:2d}/{len(offsets)}: target=({tx:.4f}, {ty:.4f})  "
            f"actual=({pos.x_mm:.4f}, {pos.y_mm:.4f})  "
            f"err=({err_x * 1000:+.1f}, {err_y * 1000:+.1f}) um  "
            f"dt={dt * 1000:.1f} ms"
        )
    t_mode_total = time.perf_counter() - t_mode_start

    print(
        f"  total wall time:    {t_mode_total * 1000:.1f} ms "
        f"(moves only: {sum(durations) * 1000:.1f} ms over {len(durations)} moves)"
    )
    print(
        f"  per-move dt (ms):   mean={statistics.mean(durations) * 1000:.1f}  "
        f"median={statistics.median(durations) * 1000:.1f}  "
        f"min={min(durations) * 1000:.1f}  max={max(durations) * 1000:.1f}"
    )
    max_err_um = max(max(abs(ex), abs(ey)) for ex, ey in drift_errors) * 1000
    print(f"  max final-pos err:  {max_err_um:.1f} um")

    return {
        "name": name,
        "total_ms": t_mode_total * 1000,
        "moves_only_ms": sum(durations) * 1000,
        "n_moves": len(durations),
        "mean_ms": statistics.mean(durations) * 1000,
        "median_ms": statistics.median(durations) * 1000,
        "max_err_um": max_err_um,
    }


def main():
    # skip_init=True: connect to the MCU without issuing a RESET + actuator
    # reconfig. This preserves whatever homing state the previous GUI session
    # left on the MCU firmware. A RESET (the default) wipes homing, and the
    # firmware silently refuses absolute moves on an unhomed stage.
    scope: Microscope = Microscope.build_from_global_config(False, skip_init=True)
    stage = scope.stage
    mcu = scope.low_level_drivers.microcontroller

    pos0 = stage.get_pos()
    origin_x, origin_y = pos0.x_mm, pos0.y_mm
    print(f"Starting position: ({origin_x:.4f}, {origin_y:.4f}) mm")
    print(f"Round-trip size:   ({DX_MM}, {DY_MM}) mm")
    print(f"N round-trips:     {N_ROUND_TRIPS} (= {2 * N_ROUND_TRIPS} moves per mode)")
    print(f"Dwell per move:    {DWELL_MS} ms")

    # Sanity check: attempt one small move and confirm get_pos reflects it. If
    # not, the stage is likely unhomed and the firmware is silently rejecting
    # moves — bail out early rather than time nothing.
    sanity_target_x = origin_x + 0.1
    print(f"\nSanity move: X {origin_x:.4f} -> {sanity_target_x:.4f} mm")
    stage.move_x_to(sanity_target_x)
    pos_after = stage.get_pos()
    pos_delta_um = (pos_after.x_mm - origin_x) * 1000.0
    print(f"  after sanity move: x={pos_after.x_mm:.4f} mm (delta {pos_delta_um:+.1f} um)")
    if abs(pos_after.x_mm - sanity_target_x) > 0.001:
        print(
            "\n  !!! Sanity move did not reach target. Stage is probably unhomed,\n"
            "  or the MCU rejected the command. Home the stage via the GUI and try\n"
            "  again. Aborting bench before invalid timing numbers are reported."
        )
        return
    # Return to origin before the timed runs.
    stage.move_x_to(origin_x)

    # Serial (baseline)
    serial = _run_mode("serial", stage, mcu, origin_x, origin_y)

    # Return to origin just to be tidy, then run concurrent.
    stage.move_x_to(origin_x)
    stage.move_y_to(origin_y)

    concurrent = _run_mode("concurrent", stage, mcu, origin_x, origin_y)

    # Summary
    print("\n=== Summary ===")
    print(
        f"  serial     : {serial['moves_only_ms']:.0f} ms over {serial['n_moves']} moves  "
        f"(mean {serial['mean_ms']:.1f} ms/move, max err {serial['max_err_um']:.1f} um)"
    )
    print(
        f"  concurrent : {concurrent['moves_only_ms']:.0f} ms over {concurrent['n_moves']} moves  "
        f"(mean {concurrent['mean_ms']:.1f} ms/move, max err {concurrent['max_err_um']:.1f} um)"
    )
    savings_ms = serial["moves_only_ms"] - concurrent["moves_only_ms"]
    ratio = concurrent["moves_only_ms"] / serial["moves_only_ms"] if serial["moves_only_ms"] else 1.0
    print(f"  delta      : {savings_ms:+.0f} ms  ({ratio * 100:.0f}% of serial)")

    print("\nInterpretation:")
    print("  - concurrent/serial ratio ~50%: firmware executes X and Y concurrently → PROCEED with Phase B.")
    print("  - ratio ~100%: firmware serializes internally → Phase B alone won't help.")
    print("  - max final-pos err > ~1 um: commands may be dropped or queued incorrectly → STOP.")

    # Leave the stage at origin
    stage.move_x_to(origin_x)
    stage.move_y_to(origin_y)


if __name__ == "__main__":
    main()
