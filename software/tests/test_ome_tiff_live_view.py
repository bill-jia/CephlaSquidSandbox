"""Tests for the live-view side of OME-TIFF saving.

Two things the NDViewer's OME-TIFF push mode depends on:

* the GUI can predict a region's file path before the writer exists, from the
  same helper the writer's own path is built on; and
* :class:`SaveOMETiffJob` reports each plane back once it is on disk, exactly
  the way :class:`SaveZarrJob` does, so the viewer is never told to read a plane
  that has not been written.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Ensure relative resources in control._def resolve as expected
os.chdir(PROJECT_ROOT)


@pytest.fixture(autouse=True)
def _clear_region_files():
    """Region file handles are process-global; never leak them between tests."""
    from control.core.job_processing import SaveOMETiffJob

    SaveOMETiffJob._region_files.clear()
    SaveOMETiffJob._active_region_path = None
    yield
    for region_file in SaveOMETiffJob._region_files.values():
        region_file.close_memmap()
    SaveOMETiffJob._region_files.clear()
    SaveOMETiffJob._active_region_path = None


def _channel(name):
    from control.models.observation_state import ObservationState, CameraSettings, IlluminatorState

    return ObservationState(
        name=name,
        camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=1.0),
        illuminator_states=[IlluminatorState(illumination_channel=name, intensity=5.0, on=True)],
    )


def _capture_info(channel, *, save_dir, region_id, fov, t, z, c, array_key=None):
    from control._def import FileSavingOption
    from control.core.job_processing import CaptureInfo
    import squid.abc

    return CaptureInfo(
        position=squid.abc.Pos(x_mm=0.0, y_mm=0.0, z_mm=float(z), theta_rad=None),
        z_index=z,
        capture_time=time.time(),
        observation_state=channel,
        save_directory=str(save_dir),
        file_id=f"{region_id}_{fov}_{t}_{c}_{z}",
        region_id=region_id,
        fov=fov,
        configuration_idx=c,
        time_point=t,
        array_key=array_key,
        save_c_index=(0 if array_key is not None else None),
        file_saving_option=FileSavingOption.OME_TIFF,
    )


class TestRegionPathHelper:
    """``ome_region_file_path`` is the single source of the on-disk naming."""

    def test_dense_layout(self):
        from control.core.utils_ome_tiff_writer import ome_region_file_path

        assert ome_region_file_path(os.path.join("exp"), "A1") == os.path.join(
            "exp", "ome_tiff", "A1.ome.tiff"
        )

    def test_region_name_with_underscore(self):
        from control.core.utils_ome_tiff_writer import ome_region_file_path

        assert ome_region_file_path("exp", "well_A1") == os.path.join(
            "exp", "ome_tiff", "well_A1.ome.tiff"
        )

    def test_ragged_array_key_uses_double_underscore(self):
        from control.core.utils_ome_tiff_writer import ome_region_file_path

        assert ome_region_file_path("exp", "well_A1", "GFP_refz") == os.path.join(
            "exp", "ome_tiff", "well_A1__GFP_refz.ome.tiff"
        )

    @pytest.mark.parametrize("array_key", [None, "GFP_refz"])
    def test_writer_path_is_the_same_helper(self, array_key):
        """What the GUI predicts must be what ``SaveOMETiffJob`` writes."""
        from control.core.job_processing import AcquisitionInfo
        from control.core.utils_ome_tiff_writer import ome_output_path, ome_region_file_path

        experiment_path = os.path.join("some", "base", "exp_2026")
        acquisition_info = AcquisitionInfo(
            total_time_points=1,
            total_z_levels=1,
            total_channels=1,
            channel_names=["DAPI"],
            experiment_path=experiment_path,
            fovs_per_region={"well_A1": 2},
        )
        info = _capture_info(
            _channel("DAPI"),
            save_dir=os.path.join(experiment_path, "000"),
            region_id="well_A1",
            fov=1,
            t=0,
            z=0,
            c=0,
            array_key=array_key,
        )

        assert ome_output_path(acquisition_info, info) == ome_region_file_path(
            experiment_path, "well_A1", array_key
        )

    def test_flat_fov_to_series_mapping(self):
        """Series index is the FOV index inside its region, flat index is not."""
        from control.core.utils_ome_tiff_writer import ome_region_file_path

        experiment_path = "exp"
        fovs_per_region = {"A1": 2, "A2": 3}

        fov_files, fov_series = [], []
        for region, count in fovs_per_region.items():
            path = ome_region_file_path(experiment_path, region)
            for fov in range(count):
                fov_files.append(path)
                fov_series.append(fov)

        assert fov_series == [0, 1, 0, 1, 2]
        assert fov_files[1] == fov_files[0]
        assert fov_files[2] == ome_region_file_path(experiment_path, "A2")


class TestFrameWriteReporting:
    """SaveOMETiffJob reports a written plane the way SaveZarrJob does."""

    def _run(self, acquisition_info, info, image):
        from control.core.job_processing import SaveOMETiffJob, JobImage

        job = SaveOMETiffJob(capture_info=info, capture_image=JobImage(image_array=image))
        job.acquisition_info = acquisition_info
        return job.run()

    def test_reports_frame_written(self, tmp_path):
        from control.core.job_processing import AcquisitionInfo, FrameWriteResult

        channels = [_channel("DAPI"), _channel("GFP")]
        experiment_dir = tmp_path / "experiment"
        acquisition_info = AcquisitionInfo(
            total_time_points=2,
            total_z_levels=2,
            total_channels=2,
            channel_names=[c.name for c in channels],
            experiment_path=str(experiment_dir),
            fovs_per_region={"A1": 2, "A2": 1},
        )
        save_dir = experiment_dir / "000"
        save_dir.mkdir(parents=True, exist_ok=True)

        result = self._run(
            acquisition_info,
            _capture_info(
                channels[1], save_dir=save_dir, region_id="A2", fov=0, t=1, z=1, c=1
            ),
            np.full((32, 24), 321, dtype=np.uint16),
        )

        assert isinstance(result, FrameWriteResult)
        assert (result.fov, result.time_point, result.z_index) == (0, 1, 1)
        assert result.channel_name == "GFP"
        # A2 is the second region in scan order.
        assert result.region_idx == 1

    def test_reported_plane_is_readable_from_disk(self, tmp_path):
        """The notification is only correct if the plane is there when it fires."""
        import tifffile
        from control.core.job_processing import AcquisitionInfo

        channels = [_channel("DAPI")]
        experiment_dir = tmp_path / "experiment"
        acquisition_info = AcquisitionInfo(
            total_time_points=1,
            total_z_levels=1,
            total_channels=1,
            channel_names=["DAPI"],
            experiment_path=str(experiment_dir),
            fovs_per_region={"A1": 2},
        )
        save_dir = experiment_dir / "000"
        save_dir.mkdir(parents=True, exist_ok=True)

        result = self._run(
            acquisition_info,
            _capture_info(
                channels[0], save_dir=save_dir, region_id="A1", fov=1, t=0, z=0, c=0
            ),
            np.full((32, 24), 4242, dtype=np.uint16),
        )

        region_file = experiment_dir / "ome_tiff" / "A1.ome.tiff"
        assert region_file.exists()
        # Read it the way the viewer does — a memmap of the series, while the
        # writer still holds its own memmap of the same file open.
        stack = tifffile.memmap(str(region_file), series=result.fov, mode="r")
        assert np.all(np.asarray(stack).reshape(32, 24) == 4242)
        del stack

    def test_ragged_array_key_reports_its_own_t_index(self, tmp_path):
        """Cycle layouts carry their own T coordinate; the viewer needs that one."""
        from control.core.job_processing import AcquisitionInfo, FrameWriteResult

        experiment_dir = tmp_path / "experiment"
        acquisition_info = AcquisitionInfo(
            total_time_points=3,
            total_z_levels=1,
            total_channels=1,
            channel_names=["GFP"],
            experiment_path=str(experiment_dir),
            fovs_per_region={"A1": 1},
        )
        save_dir = experiment_dir / "000"
        save_dir.mkdir(parents=True, exist_ok=True)

        info = _capture_info(
            _channel("GFP"),
            save_dir=save_dir,
            region_id="A1",
            fov=0,
            t=2,
            z=0,
            c=0,
            array_key="GFP",
        )
        info.save_t_index = 1
        info.save_t_size = 2
        info.save_c_size = 1
        info.array_channel_names = ["GFP"]

        result = self._run(
            acquisition_info, info, np.full((32, 24), 7, dtype=np.uint16)
        )

        assert isinstance(result, FrameWriteResult)
        assert result.time_point == 1  # save_t_index, not time_point
        assert (experiment_dir / "ome_tiff" / "A1__GFP.ome.tiff").exists()
