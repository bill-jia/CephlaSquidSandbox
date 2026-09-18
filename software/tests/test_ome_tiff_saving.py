from __future__ import annotations

MM_TO_UM = 1000.0
PIEZO_STEP_UM = 10.0

"""Tests for the per-region multi-series OME-TIFF saving pipeline.

One file per region, one OME ``Image``/TIFF series per FOV:
``{exp}/ome_tiff/{region_id}[__{array_key}].ome.tiff``.
"""

import csv
import os
import sys
import tempfile
import warnings
import xml.etree.ElementTree as ET
import time
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Ensure relative resources in control._def resolve as expected
os.chdir(PROJECT_ROOT)

OME_NS = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2016-06"}


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


def _channels(names):
    from control.models.observation_state import ObservationState, CameraSettings, IlluminatorState

    return [
        ObservationState(
            name=name,
            camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=1.0),
            illuminator_states=[IlluminatorState(illumination_channel=name, intensity=5.0, on=True)],
        )
        for name in names
    ]


def _run_job(acquisition_info, capture_info, image):
    from control.core.job_processing import SaveOMETiffJob, JobImage

    job = SaveOMETiffJob(capture_info=capture_info, capture_image=JobImage(image_array=image))
    # Manually inject acquisition_info (normally done by JobRunner.dispatch)
    job.acquisition_info = acquisition_info
    assert job.run()


def _capture_info(
    channel, *, save_dir, region_id, fov, t, z, c, array_key=None, save_c_size=None, acquisition_root=None
):
    from control._def import FileSavingOption
    from control.core.job_processing import CaptureInfo
    import squid.abc

    return CaptureInfo(
        position=squid.abc.Pos(x_mm=float(fov) + 0.5, y_mm=float(fov) * 2.0, z_mm=float(z), theta_rad=None),
        z_index=z,
        capture_time=time.time(),
        observation_state=channel,
        save_directory=str(save_dir),
        file_id=f"test_{region_id}_{fov}_{t}_{c}_{z}",
        region_id=region_id,
        fov=fov,
        configuration_idx=c,
        z_piezo_um=float(z) * PIEZO_STEP_UM,
        time_point=t,
        array_key=array_key,
        save_c_index=(0 if array_key is not None else None),
        save_c_size=save_c_size,
        file_saving_option=FileSavingOption.OME_TIFF,
        acquisition_root=acquisition_root,
    )


def _expected_pixel(fov, t, z, c):
    return fov * 1000 + t * 100 + z * 10 + c


@pytest.mark.parametrize("shape", [(64, 48), (32, 32)])
def test_region_file_multi_fov_roundtrip(shape: tuple[int, int]) -> None:
    """A region's FOVs land in their own series, even arriving out of order."""
    import tifffile
    from control.core.job_processing import AcquisitionInfo

    channels = _channels(["DAPI", "GFP"])
    total_timepoints = 2
    total_channels = len(channels)
    total_z = 3
    n_fov = 3
    region_id = "well_A1"  # a user-editable name containing a single underscore

    with tempfile.TemporaryDirectory() as tmp_dir:
        experiment_dir = Path(tmp_dir) / "experiment"
        acquisition_info = AcquisitionInfo(
            total_time_points=total_timepoints,
            total_z_levels=total_z,
            total_channels=total_channels,
            channel_names=[c.name for c in channels],
            experiment_path=str(experiment_dir),
            time_increment_s=1.5,
            physical_size_z_um=4.5,
            physical_size_x_um=0.75,
            physical_size_y_um=0.8,
            fovs_per_region={region_id: n_fov},
        )

        # FOV 2 first: out-of-order arrival must still land in series 2.
        for t in range(total_timepoints):
            time_point_dir = experiment_dir / f"{t:03d}"
            time_point_dir.mkdir(parents=True, exist_ok=True)
            for fov in (2, 0, 1):
                for z in range(total_z):
                    for c, channel in enumerate(channels):
                        image = np.full(shape, fill_value=_expected_pixel(fov, t, z, c), dtype=np.uint16)
                        _run_job(
                            acquisition_info,
                            _capture_info(
                                channel,
                                save_dir=time_point_dir,
                                region_id=region_id,
                                fov=fov,
                                t=t,
                                z=z,
                                c=c,
                            ),
                            image,
                        )

        output_path = experiment_dir / "ome_tiff" / f"{region_id}.ome.tiff"
        assert output_path.exists(), "One OME-TIFF per region should be created"
        assert list((experiment_dir / "ome_tiff").glob("*.ome.tiff")) == [output_path]

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with tifffile.TiffFile(output_path) as tif:
                assert len(tif.series) == n_fov
                for fov in range(n_fov):
                    series = tif.series[fov]
                    assert series.axes.upper() == "TZCYX"
                    assert series.name == f"{region_id}:{fov}"
                    data = series.asarray()
                    assert data.shape == (total_timepoints, total_z, total_channels, *shape)
                    for t in range(total_timepoints):
                        for z in range(total_z):
                            for c in range(total_channels):
                                np.testing.assert_array_equal(data[t, z, c], _expected_pixel(fov, t, z, c))

                ome_xml = tif.ome_metadata or ""

        assert not caught

        root = ET.fromstring(ome_xml)
        images = root.findall("ome:Image", OME_NS)
        assert len(images) == n_fov
        assert [im.get("Name") for im in images] == [f"{region_id}:{fov}" for fov in range(n_fov)]

        for fov, image_elem in enumerate(images):
            pixels = image_elem.find("ome:Pixels", OME_NS)
            assert pixels is not None
            assert pixels.get("SizeT") == str(total_timepoints)
            assert pixels.get("SizeC") == str(total_channels)
            assert pixels.get("SizeZ") == str(total_z)
            assert float(pixels.get("TimeIncrement", "nan")) == pytest.approx(1.5)
            assert float(pixels.get("PhysicalSizeZ", "nan")) == pytest.approx(4.5)
            assert float(pixels.get("PhysicalSizeX", "nan")) == pytest.approx(0.75)
            assert float(pixels.get("PhysicalSizeY", "nan")) == pytest.approx(0.8)
            assert pixels.get("PhysicalSizeXUnit") == "µm"
            assert [ch.get("Name") for ch in pixels.findall("ome:Channel", OME_NS)] == ["DAPI", "GFP"]

            planes = pixels.findall("ome:Plane", OME_NS)
            assert len(planes) == total_timepoints * total_z * total_channels
            plane_map = {
                (int(p.get("TheT")), int(p.get("TheZ")), int(p.get("TheC"))): p for p in planes
            }
            for t in range(total_timepoints):
                for z in range(total_z):
                    for c in range(total_channels):
                        plane = plane_map[(t, z, c)]
                        # Positions are per-FOV, so each Image must carry its own.
                        assert float(plane.get("PositionX")) == pytest.approx(float(fov) + 0.5)
                        assert plane.get("PositionXUnit") == "mm"
                        assert float(plane.get("PositionY")) == pytest.approx(float(fov) * 2.0)
                        expected_total_um = float(z) * MM_TO_UM + float(z) * PIEZO_STEP_UM
                        assert float(plane.get("PositionZ")) == pytest.approx(expected_total_um, rel=1e-6)
                        assert plane.get("PositionZUnit") == "µm"
                        assert float(plane.get("DeltaT")) >= 0.0

        # A completed region finalises itself: no sidecar, no lock left behind.
        leftovers = sorted(p.name for p in (experiment_dir / "ome_tiff").iterdir())
        assert leftovers == [f"{region_id}.ome.tiff"]


def test_frame_acquisition_times_reference_region_file() -> None:
    """The per-frame CSV records the region file path, not a per-FOV file.

    OME-TIFF has no per-timepoint folder, so the rows are consolidated into
    ``{exp}/acquisition_times.csv`` and ``filename`` is relative to the
    experiment root.
    """
    from control.core.job_processing import AcquisitionInfo

    channels = _channels(["DAPI"])
    with tempfile.TemporaryDirectory() as tmp_dir:
        experiment_dir = Path(tmp_dir) / "experiment"
        experiment_dir.mkdir(parents=True, exist_ok=True)
        acquisition_info = AcquisitionInfo(
            total_time_points=1,
            total_z_levels=1,
            total_channels=1,
            channel_names=["DAPI"],
            experiment_path=str(experiment_dir),
            fovs_per_region={"A1": 2},
        )
        for fov in range(2):
            _run_job(
                acquisition_info,
                _capture_info(
                    channels[0],
                    save_dir=experiment_dir,
                    region_id="A1",
                    fov=fov,
                    t=0,
                    z=0,
                    c=0,
                    acquisition_root=str(experiment_dir),
                ),
                np.zeros((16, 16), dtype=np.uint16),
            )

        csv_path = experiment_dir / "acquisition_times.csv"
        assert csv_path.exists()
        assert not (experiment_dir / "000").exists()
        with open(csv_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert [row["filename"] for row in rows] == ["ome_tiff/A1.ome.tiff"] * 2
        assert [row["fov"] for row in rows] == ["0", "1"]


def test_sidecar_lives_next_to_the_data() -> None:
    """Bookkeeping is a dotfile in ome_tiff/, never in the system temp dir."""
    from control.core import utils_ome_tiff_writer as ome_tiff_writer
    from control.core.job_processing import AcquisitionInfo, SaveOMETiffJob

    channels = _channels(["DAPI"])
    with tempfile.TemporaryDirectory() as tmp_dir:
        experiment_dir = Path(tmp_dir) / "experiment"
        time_point_dir = experiment_dir / "000"
        time_point_dir.mkdir(parents=True, exist_ok=True)
        acquisition_info = AcquisitionInfo(
            total_time_points=1,
            total_z_levels=2,
            total_channels=1,
            channel_names=["DAPI"],
            experiment_path=str(experiment_dir),
            fovs_per_region={"A1": 2},
        )

        # FOV 0 fully, then one frame of FOV 1: the FOV switch flushes the sidecar.
        for z in range(2):
            _run_job(
                acquisition_info,
                _capture_info(channels[0], save_dir=time_point_dir, region_id="A1", fov=0, t=0, z=z, c=0),
                np.zeros((16, 16), dtype=np.uint16),
            )
        _run_job(
            acquisition_info,
            _capture_info(channels[0], save_dir=time_point_dir, region_id="A1", fov=1, t=0, z=0, c=0),
            np.zeros((16, 16), dtype=np.uint16),
        )

        output_path = experiment_dir / "ome_tiff" / "A1.ome.tiff"
        metadata_path, lock_path = ome_tiff_writer.sidecar_paths(str(output_path))
        assert Path(metadata_path) == experiment_dir / "ome_tiff" / ".A1.meta.json"
        assert os.path.exists(metadata_path), "sidecar should be flushed on FOV change"
        assert Path(lock_path).parent == experiment_dir / "ome_tiff"
        assert not list(Path(tempfile.gettempdir()).glob("squid_ome_*_metadata.json"))

        assert SaveOMETiffJob.finalize_all_writers()
        assert not os.path.exists(metadata_path)
        assert not os.path.exists(lock_path)


def test_aborted_run_still_gets_plane_metadata() -> None:
    """Abort: finalize_all_writers writes Planes for whatever was captured."""
    import tifffile
    from control.core import utils_ome_tiff_writer as ome_tiff_writer
    from control.core.job_processing import AcquisitionInfo, SaveOMETiffJob

    channels = _channels(["DAPI", "GFP"])
    total_timepoints = 3
    total_z = 2
    n_fov = 4

    with tempfile.TemporaryDirectory() as tmp_dir:
        experiment_dir = Path(tmp_dir) / "experiment"
        time_point_dir = experiment_dir / "000"
        time_point_dir.mkdir(parents=True, exist_ok=True)
        acquisition_info = AcquisitionInfo(
            total_time_points=total_timepoints,
            total_z_levels=total_z,
            total_channels=len(channels),
            channel_names=[c.name for c in channels],
            experiment_path=str(experiment_dir),
            fovs_per_region={"A1": n_fov},
        )

        # Only t=0, and only the first two FOVs: the run is aborted after that.
        captured = []
        for fov in range(2):
            for z in range(total_z):
                for c, channel in enumerate(channels):
                    _run_job(
                        acquisition_info,
                        _capture_info(
                            channel, save_dir=time_point_dir, region_id="A1", fov=fov, t=0, z=z, c=c
                        ),
                        np.full((16, 16), _expected_pixel(fov, 0, z, c), dtype=np.uint16),
                    )
                    captured.append((fov, 0, z, c))

        output_path = experiment_dir / "ome_tiff" / "A1.ome.tiff"
        metadata_path, lock_path = ome_tiff_writer.sidecar_paths(str(output_path))

        # The JobRunner's end-of-run hook, which also runs on abort/shutdown.
        assert SaveOMETiffJob.finalize_all_writers()
        assert not os.path.exists(metadata_path)
        assert not os.path.exists(lock_path)

        with tifffile.TiffFile(output_path) as tif:
            assert len(tif.series) == n_fov
            for fov in range(2):
                data = tif.series[fov].asarray()
                for z in range(total_z):
                    for c in range(len(channels)):
                        np.testing.assert_array_equal(data[0, z, c], _expected_pixel(fov, 0, z, c))
            root = ET.fromstring(tif.ome_metadata)

        images = root.findall("ome:Image", OME_NS)
        assert len(images) == n_fov
        for fov in range(2):
            planes = images[fov].findall("ome:Pixels/ome:Plane", OME_NS)
            assert len(planes) == total_z * len(channels)
            assert all(float(p.get("PositionX")) == pytest.approx(float(fov) + 0.5) for p in planes)
        # FOVs never visited are allocated but carry no plane metadata.
        for fov in range(2, n_fov):
            assert images[fov].findall("ome:Pixels/ome:Plane", OME_NS) == []


def test_interleaved_regions_get_their_own_files() -> None:
    """Two regions in flight: each file keeps its own series, data and sidecar."""
    import tifffile
    from control.core import utils_ome_tiff_writer as ome_tiff_writer
    from control.core.job_processing import AcquisitionInfo, SaveOMETiffJob

    channels = _channels(["DAPI"])
    regions = ["A1", "B2"]
    with tempfile.TemporaryDirectory() as tmp_dir:
        experiment_dir = Path(tmp_dir) / "experiment"
        acquisition_info = AcquisitionInfo(
            total_time_points=2,
            total_z_levels=1,
            total_channels=1,
            channel_names=["DAPI"],
            experiment_path=str(experiment_dir),
            fovs_per_region={"A1": 2, "B2": 2},
        )

        # t, then region, then fov — the real acquisition nesting.
        for t in range(2):
            time_point_dir = experiment_dir / f"{t:03d}"
            time_point_dir.mkdir(parents=True, exist_ok=True)
            for r, region_id in enumerate(regions):
                for fov in range(2):
                    _run_job(
                        acquisition_info,
                        _capture_info(
                            channels[0],
                            save_dir=time_point_dir,
                            region_id=region_id,
                            fov=fov,
                            t=t,
                            z=0,
                            c=0,
                        ),
                        np.full((16, 16), r * 100 + fov * 10 + t, dtype=np.uint16),
                    )

        ome_dir = experiment_dir / "ome_tiff"
        assert sorted(p.name for p in ome_dir.iterdir()) == ["A1.ome.tiff", "B2.ome.tiff"]
        assert not SaveOMETiffJob._region_files, "both regions completed and were released"

        for r, region_id in enumerate(regions):
            path = ome_dir / f"{region_id}.ome.tiff"
            metadata_path, _ = ome_tiff_writer.sidecar_paths(str(path))
            assert not os.path.exists(metadata_path)
            with tifffile.TiffFile(path) as tif:
                assert len(tif.series) == 2
                for fov in range(2):
                    data = tif.series[fov].asarray().reshape(2, 1, 1, 16, 16)
                    for t in range(2):
                        np.testing.assert_array_equal(data[t, 0, 0], r * 100 + fov * 10 + t)
                root = ET.fromstring(tif.ome_metadata)
            images = root.findall("ome:Image", OME_NS)
            assert [im.get("Name") for im in images] == [f"{region_id}:0", f"{region_id}:1"]
            for image_elem in images:
                assert len(image_elem.findall("ome:Pixels/ome:Plane", OME_NS)) == 2


def test_writing_into_a_finalized_region_never_clobbers_it() -> None:
    """An extra frame after a region finalised adopts the file instead of wiping it."""
    import tifffile
    from control.core.job_processing import AcquisitionInfo, SaveOMETiffJob

    channels = _channels(["DAPI"])
    with tempfile.TemporaryDirectory() as tmp_dir:
        experiment_dir = Path(tmp_dir) / "experiment"
        time_point_dir = experiment_dir / "000"
        time_point_dir.mkdir(parents=True, exist_ok=True)
        acquisition_info = AcquisitionInfo(
            total_time_points=1,
            total_z_levels=2,
            total_channels=1,
            channel_names=["DAPI"],
            experiment_path=str(experiment_dir),
            fovs_per_region={"A1": 1},
        )

        for z in range(2):
            _run_job(
                acquisition_info,
                _capture_info(channels[0], save_dir=time_point_dir, region_id="A1", fov=0, t=0, z=z, c=0),
                np.full((16, 16), 100 + z, dtype=np.uint16),
            )

        output_path = experiment_dir / "ome_tiff" / "A1.ome.tiff"
        # Completing the region finalised and released it.
        assert str(output_path) not in SaveOMETiffJob._region_files

        # A duplicate frame arrives for the same region.
        _run_job(
            acquisition_info,
            _capture_info(channels[0], save_dir=time_point_dir, region_id="A1", fov=0, t=0, z=1, c=0),
            np.full((16, 16), 999, dtype=np.uint16),
        )
        assert SaveOMETiffJob.finalize_all_writers()

        with tifffile.TiffFile(output_path) as tif:
            data = tif.series[0].asarray().reshape(1, 2, 1, 16, 16)
            np.testing.assert_array_equal(data[0, 0, 0], 100)  # untouched, not re-allocated
            np.testing.assert_array_equal(data[0, 1, 0], 999)
            root = ET.fromstring(tif.ome_metadata)
        planes = root.findall("ome:Image/ome:Pixels/ome:Plane", OME_NS)
        assert len(planes) == 2


def test_ragged_array_key_naming() -> None:
    """Ragged cycle layout: one single-channel file per state, ``__`` separated."""
    import tifffile
    from control.core.job_processing import AcquisitionInfo

    channels = _channels(["DAPI", "GFP"])
    with tempfile.TemporaryDirectory() as tmp_dir:
        experiment_dir = Path(tmp_dir) / "experiment"
        time_point_dir = experiment_dir / "000"
        time_point_dir.mkdir(parents=True, exist_ok=True)
        acquisition_info = AcquisitionInfo(
            total_time_points=1,
            total_z_levels=1,
            total_channels=2,
            channel_names=["DAPI", "GFP"],
            experiment_path=str(experiment_dir),
            fovs_per_region={"my_region": 2},
        )

        for fov in range(2):
            for channel in channels:
                info = _capture_info(
                    channel,
                    save_dir=time_point_dir,
                    region_id="my_region",
                    fov=fov,
                    t=0,
                    z=0,
                    c=0,
                    array_key=channel.name,
                    save_c_size=1,
                )
                info.array_channel_names = [channel.name]
                _run_job(acquisition_info, info, np.zeros((16, 16), dtype=np.uint16))

        ome_dir = experiment_dir / "ome_tiff"
        assert sorted(p.name for p in ome_dir.glob("*.ome.tiff")) == [
            "my_region__DAPI.ome.tiff",
            "my_region__GFP.ome.tiff",
        ]
        with tifffile.TiffFile(ome_dir / "my_region__GFP.ome.tiff") as tif:
            assert len(tif.series) == 2
            assert tif.series[1].name == "my_region:1"
            # A fully singleton TZC stack reads back squeezed to YX.
            assert tif.series[0].asarray().reshape(1, 1, 1, 16, 16).shape == (1, 1, 1, 16, 16)
            root = ET.fromstring(tif.ome_metadata)
        for image_elem in root.findall("ome:Image", OME_NS):
            pixels = image_elem.find("ome:Pixels", OME_NS)
            assert pixels.get("SizeC") == "1"
            assert [ch.get("Name") for ch in pixels.findall("ome:Channel", OME_NS)] == ["GFP"]


def test_save_z_size_overrides_total_z_levels() -> None:
    """A self-describing frame's ``save_z_size`` wins over the acquisition's Z.

    Reference-z-only cycle states and postprocess outputs both collapse an
    N-plane acquisition to their own Z, so the region file must be allocated
    from the frame, not from ``AcquisitionInfo.total_z_levels``.
    """
    import tifffile
    from control.core import utils_ome_tiff_writer as ome_tiff_writer
    from control.core.job_processing import AcquisitionInfo, SaveOMETiffJob

    channels = _channels(["DAPI"])
    with tempfile.TemporaryDirectory() as tmp_dir:
        experiment_dir = Path(tmp_dir) / "experiment"
        experiment_dir.mkdir(parents=True, exist_ok=True)
        acquisition_info = AcquisitionInfo(
            total_time_points=2,
            total_z_levels=5,  # the acquisition's z-stack depth
            total_channels=3,
            channel_names=["DAPI", "GFP", "RFP"],
            experiment_path=str(experiment_dir),
            fovs_per_region={"A1": 2},
        )

        info = _capture_info(
            channels[0],
            save_dir=experiment_dir,
            region_id="A1",
            fov=0,
            t=0,
            z=0,
            c=0,
            array_key="single_z",
            save_c_size=1,
            acquisition_root=str(experiment_dir),
        )
        info.save_z_size = 1
        info.save_t_size = 2
        info.array_channel_names = ["single_z"]

        # The pure-metadata helper honours the frame's own Z...
        assert ome_tiff_writer._ome_z_size(acquisition_info, info) == 1
        metadata = ome_tiff_writer.initialize_metadata(
            acquisition_info, info, np.zeros((8, 8), np.uint16), n_series=2
        )
        assert metadata[ome_tiff_writer.SHAPE_KEY] == [2, 1, 1, 8, 8]
        # ...and the expected-plane arithmetic follows it (2 series x T2 x Z1 x C1).
        assert metadata[ome_tiff_writer.EXPECTED_COUNT_KEY] == 4

        # ...and so does the file actually allocated on disk.
        _run_job(acquisition_info, info, np.zeros((8, 8), dtype=np.uint16))
        assert SaveOMETiffJob.finalize_all_writers()

        output_path = experiment_dir / "ome_tiff" / "A1__single_z.ome.tiff"
        with tifffile.TiffFile(output_path) as tif:
            assert len(tif.series) == 2
            assert tif.series[0].asarray().reshape(2, 1, 1, 8, 8).shape == (2, 1, 1, 8, 8)
            pixels = ET.fromstring(tif.ome_metadata).find("ome:Image/ome:Pixels", OME_NS)
        assert pixels.get("SizeZ") == "1"
        assert pixels.get("SizeT") == "2"
        assert pixels.get("SizeC") == "1"


def test_needs_bigtiff_decision() -> None:
    """The BigTIFF switch is decided up front from the whole file's size."""
    from control.core import utils_ome_tiff_writer as ome_tiff_writer

    plane_bytes = 4096 * 3000 * 2  # a 12 MP 16-bit frame

    # A small region: classic TIFF.
    assert not ome_tiff_writer.needs_bigtiff(n_series=4, planes_per_series=10, plane_bytes=plane_bytes)
    # 200 FOVs x 10 planes ~ 49 GB: BigTIFF.
    assert ome_tiff_writer.needs_bigtiff(n_series=200, planes_per_series=10, plane_bytes=plane_bytes)
    # Either side of the classic 4 GiB offset limit.
    just_over = ome_tiff_writer.CLASSIC_TIFF_MAX_BYTES // plane_bytes + 1
    assert ome_tiff_writer.needs_bigtiff(n_series=1, planes_per_series=just_over, plane_bytes=plane_bytes)
    assert not ome_tiff_writer.needs_bigtiff(
        n_series=1, planes_per_series=just_over - 1, plane_bytes=plane_bytes
    )
    # Pixels alone would fit, but the per-plane IFD + Plane-XML overhead does not.
    assert ome_tiff_writer.needs_bigtiff(n_series=1, planes_per_series=4_290_000, plane_bytes=1000)
    assert ome_tiff_writer.needs_bigtiff(n_series=4_290_000, planes_per_series=1, plane_bytes=1000)
    assert not ome_tiff_writer.needs_bigtiff(n_series=0, planes_per_series=0, plane_bytes=plane_bytes)


def test_bigtiff_file_is_written_as_bigtiff() -> None:
    """A region whose computed size needs BigTIFF is opened as one."""
    import tifffile
    from control.core import utils_ome_tiff_writer as ome_tiff_writer

    with tempfile.TemporaryDirectory() as tmp_dir:
        path = os.path.join(tmp_dir, "big.ome.tiff")
        metadata = {
            ome_tiff_writer.DTYPE_KEY: np.dtype(np.uint16).str,
            ome_tiff_writer.AXES_KEY: "TZCYX",
            ome_tiff_writer.SHAPE_KEY: [1, 1, 1, 8, 8],
            ome_tiff_writer.N_SERIES_KEY: 2,
            ome_tiff_writer.REGION_ID_KEY: "A1",
            ome_tiff_writer.CHANNEL_NAMES_KEY: ["DAPI"],
        }
        # Force the BigTIFF branch without allocating gigabytes.
        original = ome_tiff_writer.needs_bigtiff
        ome_tiff_writer.needs_bigtiff = lambda *a, **k: True
        try:
            ome_tiff_writer.create_multiseries_file(path, metadata)
        finally:
            ome_tiff_writer.needs_bigtiff = original

        with tifffile.TiffFile(path) as tif:
            assert tif.is_bigtiff
            assert len(tif.series) == 2


def test_region_fov_count_falls_back_to_the_frame() -> None:
    """An undeclared region still writes, sized to the frame's own FOV index."""
    from control.core import utils_ome_tiff_writer as ome_tiff_writer
    from control.core.job_processing import AcquisitionInfo

    acquisition_info = AcquisitionInfo(
        total_time_points=1, total_z_levels=1, total_channels=1, channel_names=["DAPI"]
    )
    channel = _channels(["DAPI"])[0]
    info = _capture_info(channel, save_dir=".", region_id="A1", fov=3, t=0, z=0, c=0)
    assert ome_tiff_writer.region_fov_count(acquisition_info, info) == 4

    acquisition_info.fovs_per_region = {"A1": 7}
    assert ome_tiff_writer.region_fov_count(acquisition_info, info) == 7


def test_job_runner_injects_acquisition_info() -> None:
    """Test that JobRunner.dispatch() properly injects acquisition_info into SaveOMETiffJob."""
    from control.core.job_processing import SaveOMETiffJob, JobImage, AcquisitionInfo, JobRunner

    acquisition_info = AcquisitionInfo(
        total_time_points=1,
        total_z_levels=1,
        total_channels=1,
        channel_names=["DAPI"],
        experiment_path=os.path.join(tempfile.gettempdir(), "test"),
        time_increment_s=1.0,
        physical_size_z_um=1.0,
        physical_size_x_um=0.5,
        physical_size_y_um=0.5,
        fovs_per_region={"A1": 1},
    )

    capture_info = _capture_info(
        _channels(["DAPI"])[0],
        save_dir=os.path.join(tempfile.gettempdir(), "test"),
        region_id="A1",
        fov=0,
        t=0,
        z=0,
        c=0,
    )

    job = SaveOMETiffJob(
        capture_info=capture_info,
        capture_image=JobImage(image_array=np.zeros((32, 32), dtype=np.uint16)),
    )

    # Verify acquisition_info is None before dispatch
    assert job.acquisition_info is None

    runner = JobRunner(acquisition_info=acquisition_info)
    try:
        runner.dispatch(job)

        assert job.acquisition_info is not None
        assert job.acquisition_info.total_time_points == 1
        assert job.acquisition_info.channel_names == ["DAPI"]
        assert job.acquisition_info.fovs_per_region == {"A1": 1}
    finally:
        # Clean up - signal shutdown (don't call shutdown() since process wasn't started)
        runner._shutdown_event.set()


def test_simulated_write_accounts_for_all_series() -> None:
    """The simulated-IO path keys on the region file and counts every FOV."""
    import control._def
    from control.core import io_simulation

    io_simulation._simulated_ome_stacks.clear()
    shape = (1, 2, 1, 16, 16)
    image = np.zeros((16, 16), dtype=np.uint16)
    key = "/exp/ome_tiff/A1.ome.tiff"

    original_compression = control._def.SIMULATED_DISK_IO_COMPRESSION
    control._def.SIMULATED_DISK_IO_COMPRESSION = False  # imagecodecs is optional
    try:
        for fov in range(2):
            for z in range(2):
                io_simulation.simulated_ome_tiff_write(
                    image=image,
                    stack_key=key,
                    shape=shape,
                    n_series=2,
                    series_index=fov,
                    time_point=0,
                    z_index=z,
                    channel_index=0,
                )
                if (fov, z) != (1, 1):
                    # All FOVs of the region share one stack: FOV x T x Z x C.
                    assert io_simulation._simulated_ome_stacks[key]["expected_count"] == 4
    finally:
        control._def.SIMULATED_DISK_IO_COMPRESSION = original_compression

    # Completing every series of the region drops the stack.
    assert key not in io_simulation._simulated_ome_stacks
