"""Rebuild consolidated metadata for a completed acquisition or .ome.zarr store."""

import argparse
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control.core.zarr_metadata import consolidate_acquisition_metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="Completed acquisition directory or .ome.zarr store (writers must be stopped)")
    args = parser.parse_args()
    return 1 if consolidate_acquisition_metadata(args.path) else 0


if __name__ == "__main__":
    sys.exit(main())
