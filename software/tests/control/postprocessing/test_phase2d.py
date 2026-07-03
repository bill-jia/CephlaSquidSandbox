"""Tests for the built-in z-defocus 2D phase routine (requires waveorder)."""

import numpy as np
import pytest

pytest.importorskip("waveorder")

from control.postprocessing.base import InputStateSpec, PostprocessContext
from control.postprocessing.routines.phase2d import Phase2DRoutine, reference_z_index


def _ctx(nz, dz=0.5, pixel=0.325, wavelength_nm=532):
    return PostprocessContext(
        cache={},
        logger=__import__("logging").getLogger("test_phase2d"),
        pixel_size_um=pixel,
        dz_um=dz,
        nz=nz,
        nt=1,
        state_meta={"BF": {"wavelength_nm": wavelength_nm}},
    )


def _params():
    return {"wavelength_nm": 532, "na_detection": 0.4, "na_illumination": 0.33,
            "index_of_refraction_media": 1.333, "regularization": 0.05,
            "invert_phase_contrast": False}


def test_describe_outputs_requires_zstack():
    r = Phase2DRoutine()
    with pytest.raises(ValueError, match="Full z-stack"):
        r.describe_outputs({"BF": InputStateSpec("BF", acquire_z_stack=False, frames_per_visit=1)}, {})
    outs = r.describe_outputs({"BF": InputStateSpec("BF", acquire_z_stack=True, frames_per_visit=1)}, {})
    assert [o.name for o in outs] == ["phase", "bf_center"]


def test_reference_z_index_matches_center_convention():
    assert reference_z_index(1) == 0
    assert reference_z_index(3) == 1
    assert reference_z_index(11) == 5


def test_phase_reconstruction_shape_and_finiteness():
    nz = 11
    rng = np.random.default_rng(0)
    # (F=1, Z, Y, X) — a smooth blob defocus stack.
    yx = 64
    base = rng.normal(1000, 20, size=(yx, yx)).astype(np.float32)
    stack = np.stack([base + 5 * i for i in range(nz)], axis=0)[np.newaxis]
    r = Phase2DRoutine()
    ctx = _ctx(nz)
    out = r.process({"BF": stack}, ctx, _params())
    assert set(out) == {"phase", "bf_center"}
    assert out["phase"].shape == (yx, yx)
    assert out["phase"].dtype == np.float32
    assert np.all(np.isfinite(out["phase"]))
    # bf_center is the reference (center) plane of the raw stack.
    assert np.array_equal(out["bf_center"], stack[0, reference_z_index(nz)])


def test_phase2d_end_to_end_through_postprocess_job():
    """Drive the real phase2d routine through PostprocessJob → SaveZarrJob and read
    the derived phase / bf_center plates back via tensorstore (no hardware)."""
    import os
    import tempfile
    import time

    import squid.abc
    from control._def import FileSavingOption
    from control.core.job_processing import CaptureInfo, JobImage, PostprocessJob, SaveZarrJob, ZarrWriterInfo
    from control.models.observation_state import CameraSettings, IlluminatorState, ObservationState

    def _read_ts(path):
        import tensorstore as ts

        return ts.open({"driver": "zarr3", "kvstore": {"driver": "file", "path": path}},
                       create=False, open=True).result()

    NZ = 9
    yx = 48
    obs = ObservationState(
        name="BF",
        camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=1.0),
        illuminator_states=[IlluminatorState(illumination_channel="BF", intensity=50.0, on=True)],
    )
    output_specs = [
        {"name": "phase", "z_size": 1, "dtype": "float32", "channel_color": "#FFFFFF", "wavelength_nm": 532},
        {"name": "bf_center", "z_size": 1, "dtype": "input", "channel_color": "#FFFFFF", "wavelength_nm": 532},
    ]
    params = {"routine": "phase2d", "script_path": None,
              "params": {"wavelength_nm": 532, "na_detection": 0.4, "na_illumination": 0.33,
                         "index_of_refraction_media": 1.333, "regularization": 0.05,
                         "invert_phase_contrast": False}}
    ctx_meta = {"pixel_size_um": 0.325, "dz_um": 0.5, "nz": NZ, "nt": 1,
                "state_meta": {"BF": {"wavelength_nm": 532}}}

    with tempfile.TemporaryDirectory() as tmp:
        try:
            zwi = ZarrWriterInfo(
                base_path=tmp, t_size=1, c_size=1, z_size=NZ, is_hcs=True,
                region_fov_counts={"A1": 1}, fov_translations_um={"A1": {0: (0.0, 0.0)}},
                pixel_size_um=0.325, channel_names=["phase"], channel_colors=["#FFFFFF"], channel_wavelengths=[532],
            )
            for z in range(NZ):
                cap = CaptureInfo(
                    position=squid.abc.Pos(x_mm=1.0, y_mm=2.0, z_mm=0.0, theta_rad=None),
                    z_index=z, capture_time=time.time(), observation_state=obs,
                    save_directory=tmp, file_id="A1_0_0", region_id="A1", fov=0, configuration_idx=0,
                    time_point=0, file_saving_option=FileSavingOption.ZARR_V3, acquisition_root=tmp,
                    postprocess_group="pp0",
                )
                job = PostprocessJob(
                    capture_info=cap,
                    capture_image=JobImage(image_array=np.full((yx, yx), 1000 + 10 * z, np.uint16)),
                    group_key="pp0", label="BF", spec_dict=params, expected_frames=NZ,
                    output_specs=output_specs,
                    input_state_specs={"BF": {"acquire_z_stack": True, "frames_per_visit": 1}},
                    ctx_meta=ctx_meta,
                )
                job.zarr_writer_info = zwi
                res = job.run()
            SaveZarrJob.finalize_all_writers()

            assert res is not None and res.error is None and res.outputs_written == 2
            assert os.path.isdir(os.path.join(tmp, "BF_phase.ome.zarr"))
            assert os.path.isdir(os.path.join(tmp, "BF_bf_center.ome.zarr"))
            assert not os.path.isdir(os.path.join(tmp, "BF.ome.zarr"))  # raw not saved
            phase = _read_ts(zwi.get_output_path("A1", 0, "BF_phase"))
            bfc = _read_ts(zwi.get_output_path("A1", 0, "BF_bf_center"))
            assert phase.shape[:3] == (1, 1, 1)
            assert np.all(np.isfinite(np.asarray(phase[0, 0, 0])))
            # bf_center is the raw center plane (z=4 → value 1040).
            assert int(np.asarray(bfc[0, 0, 0])[0, 0]) == 1040
        finally:
            PostprocessJob.clear_accumulators()
            SaveZarrJob.clear_writers()


def test_transfer_function_cached_across_calls(monkeypatch):
    import waveorder.models.isotropic_thin_3d as itd

    calls = {"n": 0}
    orig = itd.calculate_transfer_function

    def spy(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(itd, "calculate_transfer_function", spy)

    nz, yx = 7, 48
    stack = np.stack([np.full((yx, yx), 1000 + 3 * i, np.float32) for i in range(nz)], axis=0)[np.newaxis]
    r = Phase2DRoutine()
    ctx = _ctx(nz)  # shared cache across the two calls
    r.process({"BF": stack}, ctx, _params())
    r.process({"BF": stack}, ctx, _params())
    assert calls["n"] == 1  # transfer function computed once, reused on the 2nd FOV


def test_warmup_makes_first_process_a_cache_hit(monkeypatch):
    """warmup() with the run's yx_shape precomputes the TF so the first process()
    call is a cache hit (no second TF build)."""
    import waveorder.models.isotropic_thin_3d as itd
    from control.postprocessing.base import InputStateSpec

    calls = {"n": 0}
    orig = itd.calculate_transfer_function

    def spy(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(itd, "calculate_transfer_function", spy)

    nz, yx = 7, 40
    r = Phase2DRoutine()
    ctx = _ctx(nz)
    ctx.yx_shape = (yx, yx)
    r.warmup({"BF": InputStateSpec("BF", acquire_z_stack=True, frames_per_visit=1)}, ctx, _params())
    assert calls["n"] == 1
    stack = np.stack([np.full((yx, yx), 1000 + 3 * i, np.float32) for i in range(nz)], axis=0)[np.newaxis]
    r.process({"BF": stack}, ctx, _params())
    assert calls["n"] == 1  # first FOV reused the warmed transfer function
