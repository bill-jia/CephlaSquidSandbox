#!/usr/bin/env python3
"""Dump the SciMicroscopy LED array's per-LED NA positions to a cached CSV.

Source-coded Fourier Ptychography needs the host to know each LED's NA-space
position ``(na_x, na_y)`` so it can select the darkfield LEDs that tile Fourier
space. The firmware already knows this (it owns the hemispherical dome geometry
+ the configured working distance) and reports it via the ``pledposna`` command.
This tool sends that command once and caches the result as a versioned CSV that
``control.fpm_led_geometry`` then reads — so the geometry is ground-truth for the
actual flashed firmware at the configured WD, with no error-prone porting.

Run it **once on the rig** (and again only if the array, firmware, or working
distance changes)::

    conda activate squid
    python -m tools.dump_sci_dome_geometry            # from the software/ dir
    python tools/dump_sci_dome_geometry.py            # equivalent

By default the serial number and working distance are read from the active
machine config's ``led_matrix`` device; override with ``--sn`` / ``--distance``.
Use ``--sim`` to write a synthetic dome table for offline testing (NOT real
geometry).
"""

import argparse
import math
import sys
from pathlib import Path

# Runnable from the repo root or from software/.
_SOFTWARE_DIR = Path(__file__).resolve().parent.parent
if str(_SOFTWARE_DIR) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE_DIR))

from control import fpm_led_geometry as fpm  # noqa: E402
from control import serial_peripherals  # noqa: E402


def _config_sn_and_distance():
    """Read (serial_number, distance_mm) from the active machine config's
    led_matrix device. Returns (None, None) on any failure."""
    try:
        from control.core.config.repository import ConfigRepository

        mc = ConfigRepository().get_machine_config()
        entry = mc.get_device("led_matrix")
        if entry and entry.driver == "scimicroscopy_led_array":
            sn = entry.connection.serial_number if entry.connection else None
            dist = entry.config.get("distance", None)
            return sn, dist
    except Exception as e:  # noqa: BLE001
        print(f"[warn] could not read machine config: {e}", file=sys.stderr)
    return None, None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sn", default=None, help="Serial number of the SCI array (default: from machine config)")
    ap.add_argument("--distance", type=float, default=None, help="Array working distance in mm (default: from machine config)")
    ap.add_argument("--out", default=None, help=f"Output CSV path (default: {fpm.DEFAULT_NA_TABLE_PATH})")
    ap.add_argument("--sim", action="store_true", help="Write a synthetic dome table instead of querying hardware")
    args = ap.parse_args(argv)

    out = Path(args.out) if args.out else fpm.DEFAULT_NA_TABLE_PATH

    if args.sim:
        print("[sim] generating synthetic hemispherical dome (NOT real geometry)")
        rows = [(i, nx, ny) for i, (nx, ny) in fpm.synthetic_dome_na_table().items()]
    else:
        sn, dist = args.sn, args.distance
        cfg_sn, cfg_dist = _config_sn_and_distance()
        sn = sn if sn is not None else cfg_sn
        dist = dist if dist is not None else (cfg_dist if cfg_dist is not None else 65)
        if not sn:
            print(
                "No serial number for the SCI array. Pass --sn or configure a "
                "scimicroscopy_led_array led_matrix device in the machine config.",
                file=sys.stderr,
            )
            return 2
        print(f"Connecting to SCI array SN={sn} (array_distance={dist} mm) ...")
        arr = serial_peripherals.SciMicroscopyLEDArray(SN=sn, array_distance=dist)

        # Primary: per-LED NA directly from the firmware.
        rows = arr.dump_led_na_positions()
        if not rows:
            # Fallback: cartesian positions, converted to NA on the host. Some
            # firmware builds implement `pledpos` but not `pledposna`.
            print("pledposna returned nothing; trying pledpos (cartesian) ...")
            rows = arr.dump_led_cartesian_positions()

        if not rows:
            # Show the raw replies so the format/command support can be diagnosed.
            for cmd in ("pledposna", "pledpos"):
                raw = arr.read_streamed_response(cmd)
                snippet = raw[:600].replace("\r", "\\r").replace("\n", "\\n")
                print(f"\n[raw '{cmd}' reply: {len(raw)} chars] {snippet}", file=sys.stderr)

    if not rows:
        print(
            "\nNo LED positions parsed. Check the raw replies above: the firmware "
            "may not support pledposna/pledpos, or the output format differs. "
            "Type '?' on the array's serial console to list supported commands.",
            file=sys.stderr,
        )
        return 1

    path = fpm.save_na_table(rows, out)
    nas = [math.hypot(x, y) for _, x, y in rows]
    print(f"Wrote {len(rows)} LED NA positions to {path}")
    print(f"NA range: {min(nas):.3f} .. {max(nas):.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
