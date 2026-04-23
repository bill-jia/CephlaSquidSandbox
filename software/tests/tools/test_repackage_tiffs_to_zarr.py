"""Tests for ``tools/repackage_tiffs_to_zarr.py`` covering:

- Golden-path HCS repackage.
- Non-HCS (flat) repackage.
- Missing-frame zero-fill + ``missing_frames.csv``.
- Trim-to-last-observed-t behavior.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest
import tifffile
import yaml


pytest.importorskip("tensorstore")

# Ensure the tools module is importable as `tools.repackage_tiffs_to_zarr`
_SOFTWARE_DIR = Path(__file__).resolve().parents[2]
if str(_SOFTWARE_DIR) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE_DIR))

from tools import repackage_tiffs_to_zarr as rptz  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic-acquisition builder
# ---------------------------------------------------------------------------


def _synth_acquisition(
    root: Path,
    *,
    widget_type: str,
    regions: list[tuple[str, tuple[float, float]]],  # (name, (x_mm, y_mm) of FOV0 origin)
    fovs_per_region: int,
    channels: list[str],
    t: int,
    z: int,
    dx_mm: float = 0.5,
    dy_mm: float = 0.0,
    height: int = 32,
    width: int = 32,
) -> Path:
    """Write a minimal INDIVIDUAL_IMAGES acquisition tree.

    Each TIFF is ``{region}_{fov}_{z}_{channel_safe}.tiff`` with a distinct
    intensity pattern so we can verify round-trip byte-equality in the test.
    """
    root.mkdir(parents=True, exist_ok=True)

    # acquisition.yaml
    acq_yaml = {
        "acquisition": {
            "experiment_id": "test",
            "start_time": 1_700_000_000.0,
            "widget_type": widget_type,
            "xy_mode": "Select Wells" if widget_type == "wellplate" else "Flexible",
            "skip_saving": False,
            "use_manual_focus_map": False,
            "keep_illuminators_on_between_captures": False,
        },
        "objective": {
            "name": "20x",
            "magnification": 20,
            "NA": 0.5,
            "pixel_size_um": 0.325,
            "camera_binning": [1, 1],
            "sensor_pixel_size_um": 6.5,
        },
        "sample": {"wellplate_format": "96 well plate" if widget_type == "wellplate" else None},
        "z_stack": {"nz": z, "delta_z_mm": 0.001 if z > 1 else None, "config": "from_bottom", "z_range_mm": None, "use_piezo": False},
        "time_series": {"nt": t, "delta_t_s": 5.0 if t > 1 else None},
        "autofocus": {"contrast_af": False, "laser_af": False},
        "channels": {"observation_state_names": channels},
    }
    if widget_type == "wellplate":
        acq_yaml["wellplate_scan"] = {
            "scan_size_mm": 1.0,
            "overlap_percent": 10.0,
            "nx": fovs_per_region,
            "ny": 1,
            "delta_x_mm": dx_mm,
            "delta_y_mm": dy_mm,
            "regions": [{"name": r[0], "center_mm": [r[1][0], r[1][1]], "shape": None} for r in regions],
        }
    else:
        acq_yaml["flexible_scan"] = {
            "nx": fovs_per_region,
            "ny": 1,
            "delta_x_mm": dx_mm,
            "delta_y_mm": dy_mm,
            "overlap_percent": 10.0,
            "positions": [{"name": r[0], "center_mm": [r[1][0], r[1][1]]} for r in regions],
        }

    with (root / "acquisition.yaml").open("w") as f:
        yaml.safe_dump(acq_yaml, f)

    # coordinates.csv: one row per (region, fov), FOV0 at the region origin plus dx_mm per FOV
    with (root / "coordinates.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["region", "x (mm)", "y (mm)", "z (mm)"])
        for name, (ox_mm, oy_mm) in regions:
            for fov in range(fovs_per_region):
                w.writerow([name, ox_mm + dx_mm * fov, oy_mm + dy_mm * fov, 0.0])

    # TIFFs
    for tp in range(t):
        tp_dir = root / f"{tp}"
        tp_dir.mkdir(parents=True, exist_ok=True)
        for region_name, _origin in regions:
            for fov in range(fovs_per_region):
                for z_i in range(z):
                    for c_i, channel in enumerate(channels):
                        img = _synth_image(height, width, tp, region_name, fov, z_i, c_i)
                        safe = channel.replace(" ", "_")
                        path = tp_dir / f"{region_name}_{fov}_{z_i}_{safe}.tiff"
                        tifffile.imwrite(str(path), img)

    return root


def _synth_image(h: int, w: int, t: int, region: str, fov: int, z: int, c: int) -> np.ndarray:
    """Unique uint16 intensity pattern per (t, region, fov, z, c)."""
    base = (t * 10_000 + (hash(region) & 0x7F) * 100 + fov * 50 + z * 10 + c) & 0xFFFF
    img = np.full((h, w), base, dtype=np.uint16)
    img[0, 0] = (base + 1) & 0xFFFF
    img[-1, -1] = (base + 2) & 0xFFFF
    return img


def _open_ts(array_path: Path):
    import tensorstore as ts

    return ts.open(
        {"driver": "zarr3", "kvstore": {"driver": "file", "path": str(array_path)}},
        create=False,
        open=True,
    ).result()


# ---------------------------------------------------------------------------
# Golden path: HCS
# ---------------------------------------------------------------------------


class TestRepackageHCS:
    def test_roundtrip_byte_equality(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "exp_in"
            out = Path(td) / "exp_out"
            _synth_acquisition(
                src,
                widget_type="wellplate",
                regions=[("A1", (0.0, 0.0)), ("B2", (5.0, 5.0))],
                fovs_per_region=2,
                channels=["BF LED matrix full", "Fluorescence 488 nm Ex"],
                t=2,
                z=1,
            )

            rc = rptz.main([
                "--input", str(src),
                "--output", str(out),
                "--jobs", "1",
                "--compression", "fast",
                "--force",
            ])
            assert rc == 0

            # Plate metadata exists
            plate_meta_path = out / "plate.ome.zarr" / "zarr.json"
            assert plate_meta_path.is_file()
            plate_meta = json.loads(plate_meta_path.read_text())
            assert plate_meta["attributes"]["ome"]["plate"]["name"] == "plate"

            # Per-FOV arrays contain the right bytes at every (t, c, z)
            for region, (ox, oy) in [("A1", (0.0, 0.0)), ("B2", (5.0, 5.0))]:
                row, col = region[0], region[1:]
                for fov in range(2):
                    array_path = out / "plate.ome.zarr" / row / col / str(fov) / "0"
                    ds = _open_ts(array_path)
                    assert tuple(ds.shape) == (2, 2, 1, 32, 32)
                    for tp in range(2):
                        for c_i in range(2):
                            got = ds[tp, c_i, 0, :, :].read().result()
                            expected = _synth_image(32, 32, tp, region, fov, 0, c_i)
                            assert np.array_equal(got, expected), (
                                f"mismatch at region={region} fov={fov} t={tp} c={c_i}"
                            )

            # Each FOV zarr has a translation in multiscales matching FOV position
            fov0_meta = json.loads((out / "plate.ome.zarr" / "A" / "1" / "0" / "zarr.json").read_text())
            trans = fov0_meta["attributes"]["ome"]["multiscales"][0]["datasets"][0][
                "coordinateTransformations"
            ][1]["translation"]
            # FOV 0 at region A1 origin (0, 0) mm -> (0, 0) um
            assert trans[3] == pytest.approx(0.0)
            assert trans[4] == pytest.approx(0.0)

            # FOV 1 at A1 is shifted by 0.5 mm in x -> 500 um
            fov1_meta = json.loads((out / "plate.ome.zarr" / "A" / "1" / "1" / "zarr.json").read_text())
            trans1 = fov1_meta["attributes"]["ome"]["multiscales"][0]["datasets"][0][
                "coordinateTransformations"
            ][1]["translation"]
            assert trans1[4] == pytest.approx(500.0)

            # Report exists
            assert (out / "repackage_report.json").is_file()
            report = json.loads((out / "repackage_report.json").read_text())
            assert report["fovs_failed"] == 0
            assert report["frames_found"] == 2 * 2 * 2 * 2  # regions × FOVs × T × C


# ---------------------------------------------------------------------------
# Non-HCS
# ---------------------------------------------------------------------------


class TestRepackageFlat:
    def test_flat_layout(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "exp_in"
            out = Path(td) / "exp_out"
            _synth_acquisition(
                src,
                widget_type="flexible",
                regions=[("region_0", (0.0, 0.0))],
                fovs_per_region=3,
                channels=["BF"],
                t=1,
                z=1,
            )
            rc = rptz.main(["--input", str(src), "--output", str(out), "--jobs", "1", "--force"])
            assert rc == 0

            for fov in range(3):
                path = out / "zarr" / "region_0" / f"fov_{fov}.ome.zarr" / "0"
                assert path.is_dir()
                ds = _open_ts(path)
                assert tuple(ds.shape) == (1, 1, 1, 32, 32)

            # No plate.ome.zarr in flat mode
            assert not (out / "plate.ome.zarr").exists()


# ---------------------------------------------------------------------------
# Missing frame handling
# ---------------------------------------------------------------------------


class TestMissingFrames:
    def test_zero_fill_policy(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "exp_in"
            out = Path(td) / "exp_out"
            _synth_acquisition(
                src,
                widget_type="wellplate",
                regions=[("A1", (0.0, 0.0))],
                fovs_per_region=1,
                channels=["BF", "GFP"],
                t=2,
                z=1,
            )
            # Delete one specific TIFF
            deleted = src / "1" / "A1_0_0_GFP.tiff"
            assert deleted.is_file()
            deleted.unlink()

            rc = rptz.main([
                "--input", str(src),
                "--output", str(out),
                "--jobs", "1",
                "--force",
            ])
            assert rc == 0

            # Verify missing frames CSV lists the deleted slot
            mf_path = out / "missing_frames.csv"
            assert mf_path.is_file()
            with mf_path.open() as f:
                rows = list(csv.DictReader(f))
            # Exactly one missing cell: (region=A1, fov=0, t=1, c=1, z=0)
            assert len(rows) == 1
            assert rows[0]["region"] == "A1"
            assert int(rows[0]["fov"]) == 0
            assert int(rows[0]["t"]) == 1
            assert int(rows[0]["channel_index"]) == 1

            # The corresponding zarr plane is zero-filled
            array_path = out / "plate.ome.zarr" / "A" / "1" / "0" / "0"
            ds = _open_ts(array_path)
            zero_plane = ds[1, 1, 0, :, :].read().result()
            assert int(zero_plane.sum()) == 0

            # Surviving planes are intact
            kept = ds[0, 0, 0, :, :].read().result()
            expected = _synth_image(32, 32, 0, "A1", 0, 0, 0)
            assert np.array_equal(kept, expected)

    def test_skip_fov_policy(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "exp_in"
            out = Path(td) / "exp_out"
            _synth_acquisition(
                src,
                widget_type="flexible",
                regions=[("region_0", (0.0, 0.0))],
                fovs_per_region=2,
                channels=["BF"],
                t=1,
                z=1,
            )
            # Kill one whole FOV's TIFF
            (src / "0" / "region_0_1_0_BF.tiff").unlink()

            rc = rptz.main([
                "--input", str(src),
                "--output", str(out),
                "--jobs", "1",
                "--on-missing", "skip-fov",
                "--force",
            ])
            assert rc == 0
            report = json.loads((out / "repackage_report.json").read_text())
            assert report["fovs_skipped"] >= 1


# ---------------------------------------------------------------------------
# Trim to last observed T
# ---------------------------------------------------------------------------


class TestTrimToLastObservedT:
    def test_trim(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "exp_in"
            out = Path(td) / "exp_out"
            _synth_acquisition(
                src,
                widget_type="flexible",
                regions=[("region_0", (0.0, 0.0))],
                fovs_per_region=1,
                channels=["BF"],
                t=3,
                z=1,
            )
            # Remove every TIFF for t=2 (and its timepoint dir if it ends up empty)
            t2_dir = src / "2"
            for p in t2_dir.iterdir():
                p.unlink()
            t2_dir.rmdir()

            rc = rptz.main([
                "--input", str(src),
                "--output", str(out),
                "--jobs", "1",
                "--trim-to-last-observed-t",
                "--force",
            ])
            assert rc == 0

            ds = _open_ts(out / "zarr" / "region_0" / "fov_0.ome.zarr" / "0")
            assert tuple(ds.shape) == (2, 1, 1, 32, 32)


# ---------------------------------------------------------------------------
# Filename parser sanity
# ---------------------------------------------------------------------------


class TestFilenameParser:
    def test_parse_basic(self):
        out = rptz._parse_tiff_name(
            "A1_0_0_BF_LED_matrix_full.tiff",
            region_names=["A1", "A10"],
            channel_names=["BF LED matrix full", "Fluorescence 488"],
        )
        assert out == ("A1", 0, 0, "BF LED matrix full", "tiff")

    def test_parse_prefers_longer_region(self):
        # "A10" should win over "A1" because we sort longest first.
        out = rptz._parse_tiff_name(
            "A10_5_2_BF_LED_matrix_full.tiff",
            region_names=["A1", "A10"],
            channel_names=["BF LED matrix full"],
        )
        assert out == ("A10", 5, 2, "BF LED matrix full", "tiff")

    def test_parse_unknown_channel_returns_none(self):
        out = rptz._parse_tiff_name(
            "A1_0_0_Unknown.tiff",
            region_names=["A1"],
            channel_names=["BF"],
        )
        assert out is None
