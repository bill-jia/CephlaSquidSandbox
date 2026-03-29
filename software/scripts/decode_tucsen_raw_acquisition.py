#!/usr/bin/env python3
"""
Decode frames from a fast-acquisition run saved as raw (frames.raw + frame_metadata.jsonl).

Does not use the TUCam SDK. Run from the ``software`` directory, e.g.::

    cd software
    python scripts/decode_tucsen_raw_acquisition.py /path/to/acquisition_output --packing cms12 --max-frames 3

The acquisition output folder is the one passed to FastAcquisitionWriter (it contains a ``frames/`` subfolder).
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List
from skimage.io import imsave

# Repo layout: software/scripts/this_file.py -> software on sys.path
_SOFTWARE_ROOT = Path(__file__).resolve().parent.parent
if str(_SOFTWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE_ROOT))

import numpy as np

from control.camera_tucsen import tucsen_raw_bytes_to_uint16
from control.core.fast_acquisition_writer import FastAcquisitionWriter


def _byte_length_from_record(rec: Dict[str, Any]) -> int:
    if "frame_byte_length" in rec:
        return int(rec["frame_byte_length"])
    return int(rec["byte_length"])


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Decode Tucsen packed raw frames using saved JSONL metadata (no camera)."
    )
    parser.add_argument(
        "acquisition_dir",
        type=Path,
        help="Fast acquisition output directory (contains frames/frames.raw and frame_metadata.jsonl)",
    )
    parser.add_argument(
        "--packing",
        required=True,
        choices=("hdr16", "cms12", "hs11"),
        help="Pixel packing mode used during capture (must match camera mode)",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=None,
        help="Decode at most this many frames (default: all)",
    )
    parser.add_argument(
        "--preview-npy",
        type=Path,
        default=None,
        help="Save the first decoded frame as a NumPy .npy file (uint16 HxW)",
    )
    args = parser.parse_args()

    frames_dir = args.acquisition_dir / "frames"
    raw_path = frames_dir / "frames.raw"
    jsonl_path = frames_dir / "frame_metadata.jsonl"
    if not raw_path.is_file():
        print(f"Missing {raw_path}", file=sys.stderr)
        return 1
    if not jsonl_path.is_file():
        print(f"Missing {jsonl_path}", file=sys.stderr)
        return 1

    records = _load_jsonl(jsonl_path)
    if args.max_frames is not None:
        records = records[: args.max_frames]

    decoded: list[np.ndarray] = []
    with raw_path.open("rb") as raw_f:
        for i, rec in enumerate(records):
            off = int(rec["byte_offset"])
            ln = _byte_length_from_record(rec)
            print(f"frame {i} off: {off}, ln: {ln}")
            raw_f.seek(off)
            chunk = raw_f.read(ln)
            if len(chunk) != ln:
                print(f"Frame {i}: short read {len(chunk)} != {ln}", file=sys.stderr)
                break
            if "expected_decode_bytes" in rec:
                chunk = FastAcquisitionWriter._pad_raw_to_expected(
                    chunk, int(rec["expected_decode_bytes"])
                )
            meta = {
                "height": int(rec["height"]),
                "width": int(rec["width"]),
            }
            img = tucsen_raw_bytes_to_uint16(chunk, meta, packing=args.packing)
            decoded.append(img)
            print(
                f"frame {i} id={rec.get('frame_id')} shape={img.shape} "
                f"min={int(img.min())} max={int(img.max())}"
            )

    if args.preview_npy is not None and decoded:
        args.preview_npy.parent.mkdir(parents=True, exist_ok=True)
        np.save(args.preview_npy, decoded[0])
        print(f"Wrote first frame to {args.preview_npy}")
    decoded = np.stack(decoded)
    imsave(args.acquisition_dir / "decoded.tiff", decoded)
    print(f"Wrote decoded stack to {args.acquisition_dir / 'decoded.tiff'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
