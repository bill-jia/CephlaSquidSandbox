"""Where each saving mode puts its per-timepoint sidecars.

Only INDIVIDUAL_IMAGES and MULTI_PAGE_TIFF write images into
``{exp}/{timepoint}/``. OME_TIFF (``ome_tiff/{region}.ome.tiff``) and ZARR_V3
write to their own root-level trees, so they get no per-timepoint folder at
all: their per-frame timing rows go to ``{exp}/acquisition_times.csv`` and their
measured stage positions to ``{exp}/acquired_positions.csv``.
"""

import csv
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from control._def import FileSavingOption
from control.core.job_processing import CaptureInfo, append_frame_acquisition_time_csv
from control.core.multi_point_worker import MultiPointWorker


def _worker(
    file_saving_option,
    *,
    skip_saving=False,
    downsampled=False,
    characterization=False,
    experiment_path=None,
    time_point=0,
):
    worker = MultiPointWorker.__new__(MultiPointWorker)
    worker.file_saving_option = file_saving_option
    worker.skip_saving = skip_saving
    worker._generate_downsampled_views = downsampled
    worker.laser_auto_focus_controller = None
    if characterization:
        worker.laser_auto_focus_controller = MagicMock(characterization_mode=True)
    worker.experiment_path = experiment_path
    worker.time_point = time_point
    worker._log = MagicMock()
    worker.use_piezo = False
    worker.initialize_coordinates_dataframe()
    return worker


ALL_OPTIONS = [
    FileSavingOption.INDIVIDUAL_IMAGES,
    FileSavingOption.MULTI_PAGE_TIFF,
    FileSavingOption.OME_TIFF,
    FileSavingOption.ZARR_V3,
]


@pytest.mark.parametrize(
    "option,expected",
    [
        (FileSavingOption.INDIVIDUAL_IMAGES, True),
        (FileSavingOption.MULTI_PAGE_TIFF, True),
        (FileSavingOption.OME_TIFF, False),
        (FileSavingOption.ZARR_V3, False),
    ],
)
def test_needs_per_timepoint_folder_per_saving_option(option, expected):
    assert _worker(option)._needs_per_timepoint_folder() is expected


@pytest.mark.parametrize("option", ALL_OPTIONS)
def test_skip_saving_drops_the_folder(option):
    assert _worker(option, skip_saving=True)._needs_per_timepoint_folder() is False


@pytest.mark.parametrize("option", ALL_OPTIONS)
def test_downsampled_views_force_the_folder(option):
    """The plate view lands in ``{t}/downsampled/``, so the folder must exist."""
    assert _worker(option, skip_saving=True, downsampled=True)._needs_per_timepoint_folder() is True


@pytest.mark.parametrize("option", ALL_OPTIONS)
def test_laser_af_characterization_forces_the_folder(option):
    """Characterization debug bmps land in the timepoint folder."""
    assert _worker(option, skip_saving=True, characterization=True)._needs_per_timepoint_folder() is True


# ── measured positions ───────────────────────────────────────────────────────


def _record_positions(worker, region, n_fov):
    for fov in range(n_fov):
        pos = MagicMock(x_mm=float(fov), y_mm=float(fov) * 2.0, z_mm=0.5)
        worker.update_coordinates_dataframe(region, 0, pos, fov)


@pytest.mark.parametrize("option", [FileSavingOption.OME_TIFF, FileSavingOption.ZARR_V3])
def test_acquired_positions_accumulate_across_timepoints(option, tmp_path: Path):
    worker = _worker(option, experiment_path=str(tmp_path))
    for t in range(2):
        worker.time_point = t
        worker.initialize_coordinates_dataframe()
        _record_positions(worker, "A1", 2)
        worker._append_acquired_positions_csv()

    out = tmp_path / "acquired_positions.csv"
    assert out.exists()
    df = pd.read_csv(out)
    # One header, four rows, time_point leading the measured columns.
    assert list(df.columns)[:4] == ["time_point", "region", "fov", "z_level"]
    assert "time" in df.columns
    assert list(df["time_point"]) == [0, 0, 1, 1]
    assert list(df["fov"]) == [0, 1, 0, 1]
    # No per-timepoint copy for these modes.
    assert not (tmp_path / "coordinates.csv").exists()


@pytest.mark.parametrize("option", [FileSavingOption.OME_TIFF, FileSavingOption.ZARR_V3])
def test_acquired_positions_appended_once_per_timepoint(option, tmp_path: Path):
    """An abort unwinds through the write path twice; rows must not double up."""
    worker = _worker(option, experiment_path=str(tmp_path))
    _record_positions(worker, "A1", 2)
    worker._append_acquired_positions_csv()
    worker._append_acquired_positions_csv()

    df = pd.read_csv(tmp_path / "acquired_positions.csv")
    assert len(df) == 2


@pytest.mark.parametrize(
    "option", [FileSavingOption.INDIVIDUAL_IMAGES, FileSavingOption.MULTI_PAGE_TIFF]
)
def test_tiff_modes_keep_their_per_timepoint_coordinates_csv(option, tmp_path: Path):
    worker = _worker(option, experiment_path=str(tmp_path))
    timepoint_dir = tmp_path / "000"
    timepoint_dir.mkdir()
    _record_positions(worker, "A1", 2)
    worker._write_timepoint_coordinates_csv(str(timepoint_dir))
    worker._append_acquired_positions_csv()

    df = pd.read_csv(timepoint_dir / "coordinates.csv")
    assert list(df["fov"]) == [0, 1]
    assert "time_point" not in df.columns
    assert not (tmp_path / "acquired_positions.csv").exists()


# ── per-frame timing CSV routing ─────────────────────────────────────────────


def _capture_info(tmp_path, option, *, with_root=True):
    state = MagicMock()
    state.name = "BF"
    return CaptureInfo(
        position=MagicMock(x_mm=1.0, y_mm=2.0, z_mm=3.0),
        z_index=0,
        capture_time=1_700_000_000.25,
        observation_state=state,
        save_directory=str(tmp_path),
        file_id="A1_0_0",
        region_id="A1",
        fov=0,
        configuration_idx=0,
        time_point=3,
        file_saving_option=option,
        acquisition_root=str(tmp_path) if with_root else None,
    )


@pytest.mark.parametrize("option", [FileSavingOption.OME_TIFF, FileSavingOption.ZARR_V3])
def test_timing_csv_consolidated_at_the_acquisition_root(option, tmp_path: Path):
    info = _capture_info(tmp_path, option)
    append_frame_acquisition_time_csv(info, "ome_tiff/A1.ome.tiff")

    consolidated = tmp_path / "acquisition_times.csv"
    assert consolidated.exists()
    assert not (tmp_path / "frame_acquisition_times.csv").exists()
    with open(consolidated, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["filename"] for r in rows] == ["ome_tiff/A1.ome.tiff"]
    assert [r["time_point"] for r in rows] == ["3"]


@pytest.mark.parametrize(
    "option", [FileSavingOption.INDIVIDUAL_IMAGES, FileSavingOption.MULTI_PAGE_TIFF]
)
def test_timing_csv_stays_in_the_timepoint_folder_for_tiff_modes(option, tmp_path: Path):
    info = _capture_info(tmp_path, option)
    append_frame_acquisition_time_csv(info, "A1_0_0_BF.tiff")

    assert (tmp_path / "frame_acquisition_times.csv").exists()
    assert not (tmp_path / "acquisition_times.csv").exists()


def test_timing_csv_falls_back_to_save_directory_without_a_root(tmp_path: Path):
    info = _capture_info(tmp_path, FileSavingOption.OME_TIFF, with_root=False)
    append_frame_acquisition_time_csv(info, "A1.ome.tiff")

    assert (tmp_path / "frame_acquisition_times.csv").exists()
    assert not (tmp_path / "acquisition_times.csv").exists()
