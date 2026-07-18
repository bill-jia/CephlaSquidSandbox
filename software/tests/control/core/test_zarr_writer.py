"""Tests for OME-NGFF Zarr v3 saving via ``ZarrWriter`` and ``SaveZarrJob``.

These tests exercise the production API after the repackaging-plan rewrite:
- 5D per-FOV stores of shape ``(T, C, Z, Y, X)``.
- Always-sharded layout (shard = ``(1, C, Z, Y, X)``) regardless of compression.
- Streaming multiscale pyramid (levels populated during ``write_frame``, not finalize).
- Per-frame timestamps in a ``frame_times`` zarr array.
- ``_squid.manifest_path`` pointer instead of an embedded full metadata block.
- OME-NGFF multiscales with both ``scale`` and ``translation`` transforms.
"""

from __future__ import annotations

import json
import os
import tempfile
import time

import numpy as np
import pytest

import squid.abc
from control._def import FileSavingOption, ZarrCompression
from control.core.job_processing import (
    CaptureInfo,
    JobImage,
    SaveZarrJob,
    ZarrWriteResult,
    ZarrWriterInfo,
)
from control.models.observation_state import CameraSettings, IlluminatorState, ObservationState


pytest.importorskip("tensorstore")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _obs(name: str = "BF LED matrix full") -> ObservationState:
    return ObservationState(
        name=name,
        camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=1.0),
        illuminator_states=[IlluminatorState(illumination_channel=name, intensity=50.0, on=True)],
    )


def _capture(
    *,
    region_id: str = "A1",
    fov: int = 0,
    t: int = 0,
    c_idx: int = 0,
    z: int = 0,
    save_directory: str = "",
    config_name: str = "BF LED matrix full",
    file_saving_option=None,
    acquisition_root=None,
) -> CaptureInfo:
    return CaptureInfo(
        position=squid.abc.Pos(x_mm=1.0, y_mm=2.0, z_mm=0.0, theta_rad=None),
        z_index=z,
        capture_time=time.time(),
        observation_state=_obs(config_name),
        save_directory=save_directory,
        file_id=f"{region_id}_{fov}_{z}",
        region_id=region_id,
        fov=fov,
        configuration_idx=c_idx,
        time_point=t,
        file_saving_option=file_saving_option,
        acquisition_root=acquisition_root,
    )


def _read_tensorstore(array_path: str):
    import tensorstore as ts

    return ts.open(
        {"driver": "zarr3", "kvstore": {"driver": "file", "path": array_path}},
        create=False,
        open=True,
    ).result()


def _read_zarr_json(group_dir: str) -> dict:
    with open(os.path.join(group_dir, "zarr.json"), "r") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# ZarrAcquisitionConfig
# ---------------------------------------------------------------------------


class TestZarrAcquisitionConfig:
    def test_shape_properties(self):
        from control.core.zarr_writer import ZarrAcquisitionConfig

        cfg = ZarrAcquisitionConfig(
            output_path="/tmp/x/0",
            shape=(2, 3, 4, 128, 256),
            dtype=np.uint16,
            pixel_size_um=0.5,
        )
        assert cfg.t_size == 2
        assert cfg.c_size == 3
        assert cfg.z_size == 4
        assert cfg.y_size == 128
        assert cfg.x_size == 256

    def test_default_compression_is_balanced(self):
        from control.core.zarr_writer import ZarrAcquisitionConfig

        cfg = ZarrAcquisitionConfig(
            output_path="/tmp/x/0",
            shape=(1, 1, 1, 64, 64),
            dtype=np.uint16,
            pixel_size_um=1.0,
        )
        assert cfg.compression == ZarrCompression.BALANCED


# ---------------------------------------------------------------------------
# ZarrWriter: shard shape and pyramid levels
# ---------------------------------------------------------------------------


def _make_writer(
    tmpdir: str,
    *,
    compression: ZarrCompression = ZarrCompression.FAST,
    translation_um=(0.0, 0.0),
    manifest_path: str = "../../../../acquisition.yaml",
    t=1,
    c=2,
    z=1,
    y=64,
    x=64,
    shard_per_z: bool = True,
):
    from control.core.zarr_writer import ZarrAcquisitionConfig, ZarrWriter

    out = os.path.join(tmpdir, "fov.ome.zarr", "0")
    cfg = ZarrAcquisitionConfig(
        output_path=out,
        shape=(t, c, z, y, x),
        dtype=np.uint16,
        pixel_size_um=0.325,
        z_step_um=1.5,
        time_increment_s=2.0,
        channel_names=[f"Ch{i}" for i in range(c)],
        channel_colors=["#FF0000"] * c,
        channel_wavelengths=[488] * c,
        compression=compression,
        translation_um=translation_um,
        manifest_path=manifest_path,
        shard_per_z=shard_per_z,
    )
    return ZarrWriter(cfg), out


class TestSharding:
    """Shard shape follows the layout mode; inner chunk is always one plane."""

    @pytest.mark.parametrize(
        "compression",
        [ZarrCompression.NONE, ZarrCompression.FAST, ZarrCompression.BALANCED, ZarrCompression.BEST],
    )
    def test_shard_per_z_shape(self, compression: ZarrCompression):
        """Default layout: shard = one z-slice (1, C, 1, Y, X)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            writer, out_path = _make_writer(tmpdir, compression=compression, c=3, z=4, y=64, x=96)
            writer.initialize()
            try:
                with open(os.path.join(out_path, "zarr.json"), "r") as f:
                    array_meta = json.load(f)
                chunk_grid = array_meta["chunk_grid"]["configuration"]["chunk_shape"]
                # The outer chunk (shard) is one z-slice, all channels
                assert chunk_grid == [1, 3, 1, 64, 96]

                # Inner chunk is one plane, inside the sharding_indexed codec
                codecs = array_meta["codecs"]
                sharding = next(c for c in codecs if c.get("name") == "sharding_indexed")
                assert sharding["configuration"]["chunk_shape"] == [1, 1, 1, 64, 96]
            finally:
                writer.finalize()

    def test_shard_per_fov_shape(self):
        """Legacy layout: shard = whole FOV timepoint (1, C, Z, Y, X)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            writer, out_path = _make_writer(tmpdir, c=3, z=4, y=64, x=96, shard_per_z=False)
            writer.initialize()
            try:
                with open(os.path.join(out_path, "zarr.json"), "r") as f:
                    array_meta = json.load(f)
                chunk_grid = array_meta["chunk_grid"]["configuration"]["chunk_shape"]
                assert chunk_grid == [1, 3, 4, 64, 96]
                codecs = array_meta["codecs"]
                sharding = next(c for c in codecs if c.get("name") == "sharding_indexed")
                assert sharding["configuration"]["chunk_shape"] == [1, 1, 1, 64, 96]
            finally:
                writer.finalize()


# ---------------------------------------------------------------------------
# ZarrWriter: multiscales + translation
# ---------------------------------------------------------------------------


class TestMultiscales:
    def test_translation_in_multiscales(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            writer, out_path = _make_writer(
                tmpdir, translation_um=(15000.0, 27000.0), c=1, z=1, y=64, x=64
            )
            writer.initialize()
            try:
                meta = _read_zarr_json(os.path.dirname(out_path))
                datasets = meta["attributes"]["ome"]["multiscales"][0]["datasets"]
                # Every pyramid level's transforms include both scale and translation.
                for ds in datasets:
                    types = [t["type"] for t in ds["coordinateTransformations"]]
                    assert "scale" in types
                    assert "translation" in types
                translation = datasets[0]["coordinateTransformations"][1]["translation"]
                # Layout is (t, c, z, y, x); only y, x are non-zero.
                assert translation[:3] == [0.0, 0.0, 0.0]
                assert translation[3] == pytest.approx(15000.0)
                assert translation[4] == pytest.approx(27000.0)
            finally:
                writer.finalize()

    def test_pyramid_level_count_and_shapes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            writer, out_path = _make_writer(tmpdir, c=1, z=1, y=512, x=512)
            writer.initialize()
            try:
                group_dir = os.path.dirname(out_path)
                # Level 0 at 512x512, level 1 at 256x256, level 2 at 128x128 (stop — next would be 64 < 128).
                assert os.path.isdir(os.path.join(group_dir, "0"))
                assert os.path.isdir(os.path.join(group_dir, "1"))
                assert os.path.isdir(os.path.join(group_dir, "2"))
                # min_pyramid_dim_px=128 so level 3 (64x64) is skipped
                assert not os.path.isdir(os.path.join(group_dir, "3"))
            finally:
                writer.finalize()

    def test_omero_channels(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            writer, out_path = _make_writer(tmpdir, c=2, z=1, y=64, x=64)
            writer.initialize()
            try:
                meta = _read_zarr_json(os.path.dirname(out_path))
                channels = meta["attributes"]["ome"]["omero"]["channels"]
                assert len(channels) == 2
                for i, ch in enumerate(channels):
                    assert ch["label"] == f"Ch{i}"
                    assert ch["color"] == "FF0000"
                    assert ch["emission_wavelength"]["value"] == 488
                    assert ch["emission_wavelength"]["unit"] == "nanometer"
            finally:
                writer.finalize()

    def test_squid_manifest_pointer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            writer, out_path = _make_writer(
                tmpdir, manifest_path="../../../../acquisition.yaml", c=1, z=1, y=64, x=64
            )
            writer.initialize()
            try:
                meta = _read_zarr_json(os.path.dirname(out_path))
                assert meta["attributes"]["_squid"]["manifest_path"] == "../../../../acquisition.yaml"
                # The previous full _squid block (with shape, dtype, chunk_mode, ...) is gone.
                assert "shape" not in meta["attributes"]["_squid"]
                assert "chunk_mode" not in meta["attributes"]["_squid"]
            finally:
                writer.finalize()


# ---------------------------------------------------------------------------
# ZarrWriter: streaming pyramid (levels populated on write_frame)
# ---------------------------------------------------------------------------


class TestStreamingPyramid:
    def test_pyramid_written_during_write_frame(self):
        """After write_frame, every pyramid level contains the downsampled plane
        (no extra work at finalize)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            writer, out_path = _make_writer(tmpdir, c=1, z=1, y=256, x=256)
            writer.initialize()
            try:
                img = (np.random.rand(256, 256) * 1000).astype(np.uint16)
                writer.write_frame(img, t=0, c=0, z=0)
                # Flush pending writes WITHOUT calling finalize.
                writer.wait_for_pending()

                group_dir = os.path.dirname(out_path)

                # Level 0
                ds0 = _read_tensorstore(os.path.join(group_dir, "0"))
                plane0 = ds0[0, 0, 0, :, :].read().result()
                assert np.array_equal(plane0, img)

                # Level 1 -- shape is (128, 128). Just verify it is populated (non-zero).
                ds1 = _read_tensorstore(os.path.join(group_dir, "1"))
                plane1 = ds1[0, 0, 0, :, :].read().result()
                assert plane1.shape == (128, 128)
                # Not all zero: pyramid actually wrote data
                assert int(plane1.sum()) > 0
            finally:
                writer.finalize()


# ---------------------------------------------------------------------------
# ZarrWriter: per-frame timestamps
# ---------------------------------------------------------------------------


class TestFrameTimes:
    def test_frame_times_array(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            writer, out_path = _make_writer(tmpdir, t=3, c=2, z=1, y=64, x=64)
            writer.initialize()
            try:
                stamps = {}
                img = np.zeros((64, 64), dtype=np.uint16)
                for t in range(3):
                    for c in range(2):
                        writer.write_frame(img, t=t, c=c, z=0)
                        ts = 1_700_000_000.0 + t * 10 + c
                        stamps[(t, c, 0)] = ts
                        writer.record_frame_time(t=t, c=c, z=0, unix_time_s=ts)
                writer.wait_for_pending()

                group_dir = os.path.dirname(out_path)
                ft_path = os.path.join(group_dir, "frame_times")
                assert os.path.isdir(ft_path)

                ft = _read_tensorstore(ft_path)
                assert tuple(ft.shape) == (3, 2, 1)
                data = ft.read().result()
                for (t, c, z), expected in stamps.items():
                    assert data[t, c, z] == pytest.approx(expected)
            finally:
                writer.finalize()


# ---------------------------------------------------------------------------
# ZarrWriter: upload-barrier shard staging
# ---------------------------------------------------------------------------


class TestDrainUnstagedShardPaths:
    """``drain_unstaged_shard_paths`` must return every shard written since
    the last drain — not just one per call. A dense/ragged acquisition-cycle
    FOV visit folds many frames into a contiguous block of array-t indices;
    a barrier that staged only the shard at the scan time_point silently
    skipped ~98% of the data (the bug this method replaced)."""

    def test_returns_all_written_timepoints_then_clears(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # One visit writes a block of 5 timepoints (e.g. 5 frames folded
            # into T at one FOV position), single channel/z, 3 pyramid levels.
            writer, out_path = _make_writer(tmpdir, t=10, c=1, z=1, y=512, x=512)
            writer.initialize()
            try:
                # Non-zero data: TensorStore omits all-fill-value (all-zero)
                # chunks, so a zero frame would write no shard file at all.
                rng = np.random.default_rng(0)
                for t in range(5):
                    img = (rng.random((512, 512)) * 1000 + 1).astype(np.uint16)
                    writer.write_frame(img, t=t, c=0, z=0)
                writer.wait_for_pending()

                n_levels = len(writer._level_shapes)
                assert n_levels >= 2  # multi-level pyramid at 512x512

                staged = writer.drain_unstaged_shard_paths()
                # Every (timepoint, level) shard, not just one.
                assert len(staged) == 5 * n_levels
                assert all(os.path.isfile(p) for p in staged)
                # Distinct array-t indices 0..4 are represented.
                ts_in_paths = {p.replace("\\", "/").split("/c/")[1].split("/")[0] for p in staged}
                assert ts_in_paths == {"0", "1", "2", "3", "4"}

                # Draining again returns nothing — each shard staged once.
                assert writer.drain_unstaged_shard_paths() == []

                # A second visit's frames are picked up by the next drain.
                for t in range(5, 8):
                    img = (rng.random((512, 512)) * 1000 + 1).astype(np.uint16)
                    writer.write_frame(img, t=t, c=0, z=0)
                writer.wait_for_pending()
                staged2 = writer.drain_unstaged_shard_paths()
                assert len(staged2) == 3 * n_levels
                ts2 = {p.replace("\\", "/").split("/c/")[1].split("/")[0] for p in staged2}
                assert ts2 == {"5", "6", "7"}
            finally:
                writer.finalize()

    def test_per_z_stages_each_zslice_once(self):
        """Shard-per-z: a deep stack stages one shard per (z, level), each once,
        and the writer reads back complete (no missing tail slices)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            C, Z = 2, 6
            writer, out_path = _make_writer(tmpdir, t=1, c=C, z=Z, y=256, x=256, shard_per_z=True)
            writer.initialize()
            try:
                rng = np.random.default_rng(1)
                # z-outer, channel-inner (matches the acquisition loop)
                for z in range(Z):
                    for c in range(C):
                        writer.write_frame((rng.random((256, 256)) * 1000 + 1).astype(np.uint16), t=0, c=c, z=z)
                writer.wait_for_pending()
                n_levels = len(writer._level_shapes)

                staged = writer.drain_unstaged_shard_paths()
                # One shard per (z, level): Z z-slices x n_levels.
                assert len(staged) == Z * n_levels
                assert all(os.path.isfile(p) for p in staged)
                # Path tail after "/c/" is <t>/<c_grid>/<z_grid>/<y_grid>/<x_grid>;
                # z_grid is index 2.
                z_grids = {p.replace("\\", "/").split("/c/")[1].split("/")[2] for p in staged}
                assert z_grids == {str(z) for z in range(Z)}
                # Each shard staged exactly once.
                assert writer.drain_unstaged_shard_paths() == []
            finally:
                writer.finalize()


# ---------------------------------------------------------------------------
# ZarrWriter: finalize / abort flags
# ---------------------------------------------------------------------------


class TestFinalizeAbort:
    def test_finalize_marks_complete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            writer, out_path = _make_writer(tmpdir, c=1, z=1, y=64, x=64)
            writer.initialize()
            img = np.zeros((64, 64), dtype=np.uint16)
            writer.write_frame(img, t=0, c=0, z=0)
            writer.finalize()

            meta = _read_zarr_json(os.path.dirname(out_path))
            squid = meta["attributes"]["_squid"]
            assert squid["acquisition_complete"] is True
            assert "aborted" not in squid or squid["aborted"] is False

    def test_abort_marks_aborted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            writer, out_path = _make_writer(tmpdir, c=1, z=1, y=64, x=64)
            writer.initialize()
            writer.abort()
            meta = _read_zarr_json(os.path.dirname(out_path))
            squid = meta["attributes"]["_squid"]
            assert squid["acquisition_complete"] is False
            assert squid["aborted"] is True


# ---------------------------------------------------------------------------
# ZarrWriter: validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_write_frame_out_of_range(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            writer, _ = _make_writer(tmpdir, t=2, c=1, z=1, y=32, x=32)
            writer.initialize()
            try:
                img = np.zeros((32, 32), dtype=np.uint16)
                with pytest.raises(ValueError):
                    writer.write_frame(img, t=5, c=0, z=0)
                with pytest.raises(ValueError):
                    writer.write_frame(img, t=0, c=3, z=0)
                with pytest.raises(ValueError):
                    writer.write_frame(img, t=0, c=0, z=9)
            finally:
                writer.finalize()

    def test_dtype_coerced(self):
        """Writes with a non-matching dtype are coerced to config.dtype."""
        with tempfile.TemporaryDirectory() as tmpdir:
            writer, out_path = _make_writer(tmpdir, c=1, z=1, y=32, x=32)
            writer.initialize()
            try:
                img_f = (np.ones((32, 32), dtype=np.float32) * 7.0)
                writer.write_frame(img_f, t=0, c=0, z=0)
                writer.wait_for_pending()
                ds = _read_tensorstore(out_path)
                plane = ds[0, 0, 0, :, :].read().result()
                assert plane.dtype == np.uint16
                assert int(plane[0, 0]) == 7
            finally:
                writer.finalize()


# ---------------------------------------------------------------------------
# HCS plate/well metadata writers
# ---------------------------------------------------------------------------


class TestHCSMetadata:
    def test_plate_metadata(self):
        from control.core.zarr_writer import write_plate_metadata

        with tempfile.TemporaryDirectory() as tmpdir:
            plate_path = os.path.join(tmpdir, "plate.ome.zarr")
            write_plate_metadata(
                plate_path,
                rows=["A", "B"],
                cols=[1, 2, 3],
                wells=[("A", 1), ("A", 2), ("B", 3)],
                plate_name="myplate",
            )
            meta = _read_zarr_json(plate_path)
            plate = meta["attributes"]["ome"]["plate"]
            assert plate["name"] == "myplate"
            assert plate["rows"] == [{"name": "A"}, {"name": "B"}]
            assert plate["columns"] == [{"name": "1"}, {"name": "2"}, {"name": "3"}]
            paths = [w["path"] for w in plate["wells"]]
            assert paths == ["A/1", "A/2", "B/3"]

    def test_well_metadata(self):
        from control.core.zarr_writer import write_well_metadata

        with tempfile.TemporaryDirectory() as tmpdir:
            well_path = os.path.join(tmpdir, "plate.ome.zarr", "A", "1")
            write_well_metadata(well_path, fields=[0, 1, 2, 3])
            meta = _read_zarr_json(well_path)
            well = meta["attributes"]["ome"]["well"]
            assert [img["path"] for img in well["images"]] == ["0", "1", "2", "3"]


# ---------------------------------------------------------------------------
# ZarrWriterInfo (job_processing dataclass)
# ---------------------------------------------------------------------------


class TestZarrWriterInfo:
    def test_hcs_output_path(self):
        info = ZarrWriterInfo(
            base_path="/exp",
            t_size=1,
            c_size=1,
            z_size=1,
            is_hcs=True,
            region_fov_counts={"A1": 4},
            fov_translations_um={"A1": {0: (5000.0, 9000.0)}},
        )
        assert info.get_output_path("A1", 0).endswith(os.path.join("A", "1", "0", "0"))
        assert info.get_fov_translation_um("A1", 0) == (5000.0, 9000.0)
        assert info.get_fov_translation_um("A1", 999) == (0.0, 0.0)

    def test_non_hcs_output_path(self):
        info = ZarrWriterInfo(
            base_path="/exp",
            t_size=1,
            c_size=1,
            z_size=1,
            is_hcs=False,
            region_fov_counts={"region_0": 4},
        )
        out = info.get_output_path("region_0", 3)
        assert out.endswith(os.path.join("region_0", "fov_3.ome.zarr", "0"))

    def test_manifest_path_relpath(self):
        info = ZarrWriterInfo(
            base_path="/exp",
            t_size=1,
            c_size=1,
            z_size=1,
            is_hcs=True,
            region_fov_counts={"A1": 1},
        )
        # HCS FOV group lives 4 levels deep under base_path.
        rel = info.get_manifest_path("A1", 0)
        # plate.ome.zarr / A / 1 / 0  -> up 4 to base -> acquisition.yaml
        assert rel == "../../../../acquisition.yaml"

    def test_hcs_structure_derives_rows_cols(self):
        info = ZarrWriterInfo(
            base_path="/exp",
            t_size=1,
            c_size=1,
            z_size=1,
            is_hcs=True,
            region_fov_counts={"A1": 2, "B3": 2, "A2": 2},
        )
        rows, cols, wells = info.get_hcs_structure()
        assert rows == ["A", "B"]
        assert cols == [1, 2, 3]
        assert ("A", 1) in wells and ("B", 3) in wells


# ---------------------------------------------------------------------------
# SaveZarrJob end-to-end (single-process path)
# ---------------------------------------------------------------------------


class TestSaveZarrJob:
    def test_writes_frame_and_frame_time(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            info = ZarrWriterInfo(
                base_path=tmpdir,
                t_size=1,
                c_size=2,
                z_size=1,
                is_hcs=True,
                region_fov_counts={"A1": 1},
                fov_translations_um={"A1": {0: (12000.0, 34000.0)}},
                pixel_size_um=0.325,
                z_step_um=None,
                time_increment_s=None,
                channel_names=["BF", "GFP"],
                channel_colors=["#FFFFFF", "#00FF00"],
                channel_wavelengths=[None, 488],
            )
            try:
                for c_idx in range(2):
                    img = (np.ones((64, 64), dtype=np.uint16) * (10 + c_idx))
                    # save_directory normally points at a per-timepoint folder; for ZARR_V3
                    # it can point at the experiment root since the per-frame CSV is
                    # consolidated there.
                    cap = _capture(
                        region_id="A1",
                        fov=0,
                        t=0,
                        c_idx=c_idx,
                        z=0,
                        save_directory=tmpdir,
                        file_saving_option=FileSavingOption.ZARR_V3,
                        acquisition_root=tmpdir,
                    )
                    job = SaveZarrJob(capture_info=cap, capture_image=JobImage(image_array=img))
                    job.zarr_writer_info = info
                    result = job.run()
                    assert isinstance(result, ZarrWriteResult)
                SaveZarrJob.finalize_all_writers()

                out = info.get_output_path("A1", 0)
                ds = _read_tensorstore(out)
                plane_bf = ds[0, 0, 0, :, :].read().result()
                plane_gfp = ds[0, 1, 0, :, :].read().result()
                assert int(plane_bf[0, 0]) == 10
                assert int(plane_gfp[0, 0]) == 11

                # Multiscales translation was picked up from the info map.
                group_dir = os.path.dirname(out)
                meta = _read_zarr_json(group_dir)
                trans = meta["attributes"]["ome"]["multiscales"][0]["datasets"][0][
                    "coordinateTransformations"
                ][1]["translation"]
                assert trans[3] == pytest.approx(12000.0)
                assert trans[4] == pytest.approx(34000.0)

                # Manifest pointer resolves relative to FOV group.
                assert meta["attributes"]["_squid"]["manifest_path"].endswith("acquisition.yaml")

                # Per-frame CSV is consolidated at the experiment root for ZARR_V3.
                consolidated = os.path.join(tmpdir, "acquisition_times.csv")
                assert os.path.isfile(consolidated)
                # And no per-timepoint frame_acquisition_times.csv was created.
                assert not os.path.exists(os.path.join(tmpdir, "frame_acquisition_times.csv"))
            finally:
                SaveZarrJob.clear_writers()

    def test_clear_writers_idempotent(self):
        SaveZarrJob.clear_writers()
        SaveZarrJob.clear_writers()
