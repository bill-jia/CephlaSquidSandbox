"""
Plot fast acquisition DAQ waveforms (trigger DO and exposure DI) with a time axis.

Usage:
    python plot_fast_acq_waveforms.py --folder /path/to/fast_acq_output \
        [--trigger-line 1] [--exposure-line 0] [--save]

By default, the script tries to auto-detect lines if only one DO/DI line exists.
It plots the digital output trigger line and the digital input exposure line
against real time (seconds) using the sample rate and samples acquired stored
in waveforms/daq_data.h5.
"""

import argparse
import os
import sys
from typing import Optional, Tuple

import h5py
import matplotlib.pyplot as plt
import numpy as np


def load_daq_file(folder: str) -> Tuple[h5py.File, str]:
    """Open the DAQ HDF5 file inside the given fast acquisition folder."""
    h5_path = os.path.join(folder, "waveforms", "daq_data.h5")
    if not os.path.exists(h5_path):
        raise FileNotFoundError(f"DAQ data file not found at {h5_path}")
    return h5py.File(h5_path, "r"), h5_path


def list_available_lines(h5f: h5py.File) -> Tuple[list, list]:
    """Return (digital_output_lines, digital_input_lines) present in the file."""
    do_lines, di_lines = [], []
    if "digital_output" in h5f:
        do_lines = [int(name.replace("line", "")) for name in h5f["digital_output"].keys()]
    if "digital_input" in h5f:
        di_lines = [int(name.replace("line", "")) for name in h5f["digital_input"].keys()]
    return sorted(do_lines), sorted(di_lines)


def pick_line(requested: Optional[int], available: list, kind: str) -> int:
    """Pick a line: use requested if provided; otherwise auto-select when one available."""
    if requested is not None:
        if requested not in available:
            raise ValueError(f"Requested {kind} line {requested} not found in file. Available: {available}")
        return requested
    if len(available) == 1:
        return available[0]
    raise ValueError(
        f"Cannot auto-select {kind} line; please specify with --{kind.replace(' ', '-')}. "
        f"Available {kind} lines: {available}"
    )


def main():
    parser = argparse.ArgumentParser(description="Plot fast acquisition DAQ waveforms (trigger DO and exposure DI).")
    parser.add_argument("--folder", required=True, help="Fast acquisition output folder (contains waveforms/daq_data.h5)")
    parser.add_argument("--trigger-line", type=int, default=None, help="Digital output line for camera trigger (e.g., 1)")
    parser.add_argument("--exposure-line", type=int, default=None, help="Digital input line for camera exposure/frames (e.g., 0)")
    parser.add_argument("--save", action="store_true", help="Save plot as PNG in the same folder")
    args = parser.parse_args()

    h5f, h5_path = load_daq_file(args.folder)
    print(f"Loaded DAQ data from {h5_path}")

    # Read metadata for timing
    sample_rate = h5f.attrs.get("sample_rate_hz", None)
    samples_acquired = h5f.attrs.get("samples_acquired", None)
    if sample_rate is None or samples_acquired is None:
        # Some files may store these inside datasets; handle gracefully
        try:
            sample_rate = float(h5f.attrs["sample_rate_hz"])
            samples_acquired = int(h5f.attrs["samples_acquired"])
        except Exception as e:
            h5f.close()
            raise RuntimeError(f"Sample rate / samples acquired not found in {h5_path}: {e}")

    do_lines, di_lines = list_available_lines(h5f)
    trigger_line = pick_line(args.trigger_line, do_lines, "trigger (DO)")
    exposure_line = pick_line(args.exposure_line, di_lines, "exposure (DI)")

    trigger_ds = h5f["digital_output"][f"line{trigger_line}"][:]
    exposure_ds = h5f["digital_input"][f"line{exposure_line}"][:]

    n_samples = len(trigger_ds)
    t = np.arange(n_samples) / sample_rate

    fig, ax = plt.subplots(figsize=(10, 5), sharex=True)
    ax.plot(t, trigger_ds.astype(float), label=f"Trigger DO line {trigger_line}", drawstyle="steps-post")
    ax.plot(t, exposure_ds.astype(float) + 1.1, label=f"Exposure DI line {exposure_line}", drawstyle="steps-post")

    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Level (offset applied)")
    ax.set_title("Fast Acquisition: Trigger and Exposure Waveforms")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right")

    plt.tight_layout()

    if args.save:
        out_path = os.path.join(args.folder, "waveforms", "daq_waveforms.png")
        plt.savefig(out_path, dpi=150)
        print(f"Saved plot to {out_path}")
    else:
        plt.show()

    h5f.close()


if __name__ == "__main__":
    sys.exit(main())
