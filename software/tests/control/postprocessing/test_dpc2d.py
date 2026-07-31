"""Tests for the built-in 2D DPC routine (Tian & Waller 2015).

The centrepiece is ``test_matches_reference_solver``: the routine caches the
inversion in an algebraically-collapsed form (per-direction filters instead of
the WOTFs plus the Tikhonov Gramian), so it is checked against a verbatim
transcription of the Waller-Lab reference engine
(``qpm-analysis/example/tian2015/dpc_algorithm.py``) inlined below.
"""

import numpy as np
import pytest

from control.postprocessing.base import InputStateSpec, PostprocessContext
from control.postprocessing.routines.dpc2d import (
    ROLES,
    ROTATION_DEG,
    DPC2DRoutine,
    resolve_roles,
)

STATES = {"top": "DPC_top", "bottom": "DPC_bottom", "left": "DPC_left", "right": "DPC_right"}
WAVELENGTH_NM = 530
PIXEL_UM = 0.65
NA_DET = 0.4
NA_ILL = 0.4
REG_U = 1e-1
REG_P = 1e-2


def _ctx(pixel=PIXEL_UM):
    return PostprocessContext(
        cache={},
        logger=__import__("logging").getLogger("test_dpc2d"),
        pixel_size_um=pixel,
        dz_um=None,
        nz=1,
        nt=1,
        state_meta={s: {"wavelength_nm": WAVELENGTH_NM} for s in STATES.values()},
    )


def _params(**overrides):
    p = {
        "state_top": STATES["top"],
        "state_bottom": STATES["bottom"],
        "state_left": STATES["left"],
        "state_right": STATES["right"],
        "wavelength_nm": WAVELENGTH_NM,
        "na_detection": NA_DET,
        "na_illumination": NA_ILL,
        "na_illumination_inner": 0.0,
        "regularization_absorption": REG_U,
        "regularization_phase": REG_P,
        "output_absorption": True,
        "output_brightfield": True,
        "single_precision": False,
    }
    p.update(overrides)
    return p


def _input_states(**kwargs):
    return {
        s: InputStateSpec(s, kwargs.get("acquire_z_stack", False), kwargs.get("frames_per_visit", 1))
        for s in STATES.values()
    }


def _synthetic_halves(size=64, seed=0):
    """Four (F=1, Z=1, Y, X) half-circle captures of a smooth phase blob."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    blob = np.exp(-(((yy - size * 0.4) ** 2 + (xx - size * 0.55) ** 2) / (2 * (size / 8.0) ** 2)))
    gy, gx = np.gradient(blob)
    base = 2000.0 + 400.0 * blob
    halves = {
        "top": base + 300.0 * gy,
        "bottom": base - 300.0 * gy,
        "left": base + 300.0 * gx,
        "right": base - 300.0 * gx,
    }
    return {
        STATES[role]: (img + rng.normal(0, 2.0, img.shape))[np.newaxis, np.newaxis].astype(np.float64)
        for role, img in halves.items()
    }


# ─────────────────────────────────────────────────────────────────────────────
# Verbatim reference engine (Waller-Lab dpc_algorithm.py + the runner's
# VariableNADPCSolver source-NA extension), transcribed for comparison only.
# ─────────────────────────────────────────────────────────────────────────────

_naxis = np.newaxis
_F = lambda x: np.fft.fft2(x)  # noqa: E731
_IF = lambda x: np.fft.ifft2(x)  # noqa: E731


def _ref_pupil_gen(fxlin, fylin, wavelength, na, na_in=0.0):
    pupil = np.array(fxlin[_naxis, :] ** 2 + fylin[:, _naxis] ** 2 <= (na / wavelength) ** 2)
    if na_in != 0.0:
        pupil[fxlin[_naxis, :] ** 2 + fylin[:, _naxis] ** 2 < (na_in / wavelength) ** 2] = 0.0
    return pupil


def _ref_gen_grid(size, dx):
    xlin = np.arange(size, dtype="complex128")
    return (xlin - size // 2) * dx


def _ref_wotf(shape, wavelength, na, na_in, pixel_size, rotation, na_source):
    fxlin = np.fft.ifftshift(_ref_gen_grid(shape[-1], 1.0 / shape[-1] / pixel_size))
    fylin = np.fft.ifftshift(_ref_gen_grid(shape[-2], 1.0 / shape[-2] / pixel_size))
    pupil = _ref_pupil_gen(fxlin, fylin, wavelength, na)

    source = []  # VariableNADPCSolver.sourceGen
    src_pupil = _ref_pupil_gen(fxlin, fylin, wavelength, na_source, na_in=na_in)
    for rot_idx in range(4):
        source.append(np.zeros(shape[-2:]))
        rotdegree = rotation[rot_idx]
        if rotdegree < 180:
            source[-1][
                fylin[:, _naxis] * np.cos(np.deg2rad(rotdegree)) + 1e-15
                >= fxlin[_naxis, :] * np.sin(np.deg2rad(rotdegree))
            ] = 1.0
            source[-1] *= src_pupil
        else:
            source[-1][
                fylin[:, _naxis] * np.cos(np.deg2rad(rotdegree)) + 1e-15
                < fxlin[_naxis, :] * np.sin(np.deg2rad(rotdegree))
            ] = -1.0
            source[-1] *= src_pupil
            source[-1] += src_pupil
    source = np.asarray(source)

    Hu, Hp = [], []  # WOTFGen
    for rot_idx in range(source.shape[0]):
        FSP_cFP = _F(source[rot_idx] * pupil) * _F(pupil).conj()
        I0 = (source[rot_idx] * pupil * pupil.conj()).sum()
        Hu.append(2.0 * _IF(FSP_cFP.real) / I0)
        Hp.append(2.0j * _IF(1j * FSP_cFP.imag) / I0)
    return np.asarray(Hu), np.asarray(Hp)


def _ref_solve(dpc_imgs, wavelength, na, na_in, pixel_size, rotation, na_source, reg_u, reg_p):
    from scipy.ndimage import uniform_filter

    imgs = dpc_imgs.astype("float64").copy()
    for img in imgs:  # normalization()
        img /= uniform_filter(img, size=img.shape[0] // 2)
        img /= img.mean()
        img -= 1.0
    Hu, Hp = _ref_wotf(imgs.shape, wavelength, na, na_in, pixel_size, rotation, na_source)

    AHA = [  # solve()
        (Hu.conj() * Hu).sum(axis=0) + reg_u,
        (Hu.conj() * Hp).sum(axis=0),
        (Hp.conj() * Hu).sum(axis=0),
        (Hp.conj() * Hp).sum(axis=0) + reg_p,
    ]
    determinant = AHA[0] * AHA[3] - AHA[1] * AHA[2]
    fIntensity = np.asarray([_F(imgs[i]) for i in range(4)])
    AHy = np.asarray([(Hu.conj() * fIntensity).sum(axis=0), (Hp.conj() * fIntensity).sum(axis=0)])
    absorption = _IF((AHA[3] * AHy[0] - AHA[1] * AHy[1]) / determinant).real
    phase = _IF((AHA[0] * AHy[1] - AHA[2] * AHy[0]) / determinant).real
    return absorption, phase


# ─────────────────────────────────────────────────────────────────────────────
# Role resolution
# ─────────────────────────────────────────────────────────────────────────────


def test_roles_resolved_from_explicit_params():
    roles = resolve_roles(STATES.values(), _params())
    assert roles == STATES


@pytest.mark.parametrize(
    "names",
    [
        ("DPC_top", "DPC_bottom", "DPC_left", "DPC_right"),
        ("BF_top_half", "BF_bottom_half", "BF_left_half", "BF_right_half"),
        ("half_ann_t", "half_ann_b", "half_ann_l", "half_ann_r"),
        ("qpm th", "qpm bh", "qpm lh", "qpm rh"),
    ],
)
def test_roles_inferred_from_state_name_tags(names):
    roles = resolve_roles(names, {})
    assert [roles[r] for r in ROLES] == list(names)


def test_role_resolution_errors():
    with pytest.raises(ValueError, match="exactly 4 input states"):
        resolve_roles(["a", "b", "c"], {})
    with pytest.raises(ValueError, match="could not tell which input state"):
        resolve_roles(["a", "b", "c", "d"], {})
    with pytest.raises(ValueError, match="not one of this group's input states"):
        resolve_roles(STATES.values(), {"state_top": "nope"})
    with pytest.raises(ValueError, match="assigned to both"):
        resolve_roles(STATES.values(), {"state_top": "DPC_top", "state_bottom": "DPC_top"})


def test_partial_explicit_params_fill_the_rest_by_tag():
    """An explicit override for one role still lets the others auto-match."""
    names = ["weird_name", "DPC_bottom", "DPC_left", "DPC_right"]
    roles = resolve_roles(names, {"state_top": "weird_name"})
    assert roles == {"top": "weird_name", "bottom": "DPC_bottom", "left": "DPC_left", "right": "DPC_right"}


# ─────────────────────────────────────────────────────────────────────────────
# Output declaration
# ─────────────────────────────────────────────────────────────────────────────


def test_describe_outputs():
    r = DPC2DRoutine()
    assert [o.name for o in r.describe_outputs(_input_states(), _params())] == [
        "phase",
        "absorption",
        "brightfield",
    ]
    outs = r.describe_outputs(_input_states(), _params(output_absorption=False, output_brightfield=False))
    assert [o.name for o in outs] == ["phase"]
    assert outs[0].dtype == "float32" and outs[0].wavelength_nm == WAVELENGTH_NM
    brightfield = r.describe_outputs(_input_states(), _params())[2]
    assert brightfield.dtype == "input"


def test_describe_outputs_rejects_multiple_frames_per_visit():
    r = DPC2DRoutine()
    with pytest.raises(ValueError, match="one frame"):
        r.describe_outputs(_input_states(frames_per_visit=2), _params())


def test_describe_outputs_rejects_wrong_state_count():
    r = DPC2DRoutine()
    states = {s: InputStateSpec(s, False, 1) for s in ("DPC_top", "DPC_bottom")}
    with pytest.raises(ValueError, match="exactly 4 input states"):
        r.describe_outputs(states, _params())


def test_bad_optics_are_rejected_pre_flight():
    """Param-only problems surface at plan-build time, not at the first FOV."""
    r = DPC2DRoutine()
    with pytest.raises(ValueError, match="darkfield"):
        # Annulus entirely outside the objective pupil.
        r.describe_outputs(_input_states(), _params(na_illumination=0.9, na_illumination_inner=0.6))
    with pytest.raises(ValueError, match="regularization"):
        r.describe_outputs(_input_states(), _params(regularization_phase=0.0))
    with pytest.raises(ValueError, match="na_illumination_inner"):
        r.describe_outputs(_input_states(), _params(na_illumination=0.4, na_illumination_inner=0.4))
    with pytest.raises(ValueError, match="detection NA unknown"):
        r.describe_outputs(_input_states(), _params(na_detection=None))


# ─────────────────────────────────────────────────────────────────────────────
# Reconstruction
# ─────────────────────────────────────────────────────────────────────────────


def test_matches_reference_solver():
    """The collapsed/cached filter bank reproduces the verbatim Tian solver."""
    inputs = _synthetic_halves()
    out = DPC2DRoutine().process(inputs, _ctx(), _params())

    stack = np.asarray([inputs[STATES[r]][0, 0] for r in ROLES])
    rotation = [ROTATION_DEG[r] for r in ROLES]
    ref_absorption, ref_phase = _ref_solve(
        stack, WAVELENGTH_NM / 1000.0, NA_DET, 0.0, PIXEL_UM, rotation, NA_ILL, REG_U, REG_P
    )
    assert np.allclose(out["phase"], ref_phase.astype(np.float32), rtol=1e-4, atol=1e-6)
    assert np.allclose(out["absorption"], ref_absorption.astype(np.float32), rtol=1e-4, atol=1e-6)


def test_matches_reference_solver_with_annulus_and_decoupled_source_na():
    """Half-annulus geometry (inner NA > 0) and NA_ill != NA_det also match."""
    inputs = _synthetic_halves(seed=3)
    params = _params(na_illumination=0.8, na_illumination_inner=0.3)
    out = DPC2DRoutine().process(inputs, _ctx(), params)

    stack = np.asarray([inputs[STATES[r]][0, 0] for r in ROLES])
    rotation = [ROTATION_DEG[r] for r in ROLES]
    ref_absorption, ref_phase = _ref_solve(
        stack, WAVELENGTH_NM / 1000.0, NA_DET, 0.3, PIXEL_UM, rotation, 0.8, REG_U, REG_P
    )
    assert np.allclose(out["phase"], ref_phase.astype(np.float32), rtol=1e-4, atol=1e-6)
    assert np.allclose(out["absorption"], ref_absorption.astype(np.float32), rtol=1e-4, atol=1e-6)


def test_process_outputs_shape_dtype_and_brightfield():
    inputs = _synthetic_halves(size=48)
    uint_inputs = {k: v.astype(np.uint16) for k, v in inputs.items()}
    out = DPC2DRoutine().process(uint_inputs, _ctx(), _params())
    assert set(out) == {"phase", "absorption", "brightfield"}
    assert out["phase"].shape == (48, 48) and out["phase"].dtype == np.float32
    assert np.all(np.isfinite(out["phase"]))
    # brightfield = mean of the four halves, back in the raw dtype.
    expected = np.rint(sum(uint_inputs[STATES[r]][0, 0].astype(np.float64) for r in ROLES) / 4.0).astype(np.uint16)
    assert out["brightfield"].dtype == np.uint16
    assert np.array_equal(out["brightfield"], expected)


def test_absorption_output_suppressed_when_disabled():
    out = DPC2DRoutine().process(_synthetic_halves(size=32), _ctx(), _params(output_absorption=False))
    assert set(out) == {"phase", "brightfield"}


def test_recovers_a_forward_modelled_phase_object():
    """Round trip: synthesize the four halves through the weak-object forward
    model Î_d = Hp_d·p̂ from a known phase blob, then check the routine inverts
    it back — same sign, same place, strongly correlated."""
    size = 128
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    truth = 0.15 * np.exp(-(((yy - size * 0.45) ** 2 + (xx - size * 0.6) ** 2) / (2 * (size / 12.0) ** 2)))
    _, Hp = _ref_wotf(
        (size, size), WAVELENGTH_NM / 1000.0, NA_DET, 0.0, PIXEL_UM, [ROTATION_DEG[r] for r in ROLES], NA_ILL
    )
    spectrum = np.fft.fft2(truth)
    inputs = {
        STATES[role]: (2000.0 * (1.0 + np.fft.ifft2(Hp[i] * spectrum).real))[np.newaxis, np.newaxis]
        for i, role in enumerate(ROLES)
    }
    phase = DPC2DRoutine().process(inputs, _ctx(), _params(output_absorption=False))["phase"]

    # Right sign, right place.
    inside = ((yy - size * 0.45) ** 2 + (xx - size * 0.6) ** 2) < (size / 12.0) ** 2
    assert phase[inside].mean() > 0 > phase[~inside].mean()

    # Fidelity is bounded by the Tikhonov floor (which suppresses the low
    # frequencies DPC is weakest at) — relaxing it must monotonically recover
    # both the shape and the amplitude of the true phase.
    corrs, peaks = [], []
    for reg_p in (1e-2, 1e-3, 1e-4):
        recon = DPC2DRoutine().process(inputs, _ctx(), _params(output_absorption=False, regularization_phase=reg_p))
        corrs.append(float(np.corrcoef(recon["phase"].ravel(), truth.ravel())[0, 1]))
        peaks.append(float(recon["phase"].max()))
    assert corrs[0] < corrs[1] < corrs[2], f"correlation not monotonic in regularization: {corrs}"
    assert peaks[0] < peaks[1] < peaks[2] < truth.max()
    assert corrs[2] > 0.9, f"recovered phase correlates only {corrs[2]:.3f} with the ground truth"


def test_single_precision_tracks_double():
    inputs = _synthetic_halves(size=64, seed=11)
    double = DPC2DRoutine().process(inputs, _ctx(), _params(output_absorption=False))["phase"]
    single = DPC2DRoutine().process(inputs, _ctx(), _params(output_absorption=False, single_precision=True))["phase"]
    assert np.allclose(double, single, rtol=1e-3, atol=1e-4 * float(np.abs(double).max()))


def test_focus_plane_used_when_a_z_stack_was_acquired():
    """A DPC step left on 'Full z-stack' contributes only its focus plane."""
    flat = _synthetic_halves(size=48, seed=5)
    nz = 5
    stacked = {}
    for state, arr in flat.items():
        planes = [arr[0, 0] + 500.0 * (i - nz // 2) for i in range(nz)]  # focus plane == the flat image
        stacked[state] = np.stack(planes, axis=0)[np.newaxis]
    ctx = _ctx()
    assert np.allclose(
        DPC2DRoutine().process(stacked, ctx, _params(output_absorption=False))["phase"],
        DPC2DRoutine().process(flat, _ctx(), _params(output_absorption=False))["phase"],
    )


def test_process_rejects_mismatched_shapes():
    inputs = _synthetic_halves(size=32)
    inputs[STATES["right"]] = inputs[STATES["right"]][:, :, :16, :16]
    with pytest.raises(ValueError, match="different shapes"):
        DPC2DRoutine().process(inputs, _ctx(), _params())


def test_bad_parameters_are_rejected():
    r = DPC2DRoutine()
    inputs = _synthetic_halves(size=32)
    with pytest.raises(ValueError, match="regularization"):
        r.process(inputs, _ctx(), _params(regularization_phase=0.0))
    with pytest.raises(ValueError, match="na_illumination_inner"):
        r.process(inputs, _ctx(), _params(na_illumination=0.4, na_illumination_inner=0.4))
    with pytest.raises(ValueError, match="pixel size unknown"):
        r.process(inputs, _ctx(pixel=None), _params())
    with pytest.raises(ValueError, match="darkfield"):
        # Ring entirely outside the objective pupil -> no brightfield overlap.
        r.process(inputs, _ctx(), _params(na_illumination=0.9, na_illumination_inner=0.6))


# ─────────────────────────────────────────────────────────────────────────────
# Filter-bank caching
# ─────────────────────────────────────────────────────────────────────────────


def test_filter_bank_computed_once_across_fovs(monkeypatch):
    r = DPC2DRoutine()
    calls = {"n": 0}
    orig = DPC2DRoutine._build_filter_bank

    def spy(self, o, yx_shape):
        calls["n"] += 1
        return orig(self, o, yx_shape)

    monkeypatch.setattr(DPC2DRoutine, "_build_filter_bank", spy)
    ctx = _ctx()  # shared cache across the two FOVs
    inputs = _synthetic_halves(size=32)
    r.process(inputs, ctx, _params())
    r.process(inputs, ctx, _params())
    assert calls["n"] == 1


def test_warmup_makes_first_process_a_cache_hit(monkeypatch):
    r = DPC2DRoutine()
    calls = {"n": 0}
    orig = DPC2DRoutine._build_filter_bank

    def spy(self, o, yx_shape):
        calls["n"] += 1
        return orig(self, o, yx_shape)

    monkeypatch.setattr(DPC2DRoutine, "_build_filter_bank", spy)
    ctx = _ctx()
    ctx.yx_shape = (32, 32)
    r.warmup(_input_states(), ctx, _params())
    assert calls["n"] == 1
    r.process(_synthetic_halves(size=32), ctx, _params())
    assert calls["n"] == 1  # first FOV reused the warmed bank


def test_warmup_without_frame_shape_is_a_noop():
    r = DPC2DRoutine()
    ctx = _ctx()
    r.warmup(_input_states(), ctx, _params())
    assert ctx.cache == {}


def test_changed_geometry_replaces_the_cached_bank():
    """Only one bank stays resident — a new frame shape evicts the old one."""
    r = DPC2DRoutine()
    ctx = _ctx()
    r.process(_synthetic_halves(size=32), ctx, _params())
    r.process(_synthetic_halves(size=48), ctx, _params())
    assert len(ctx.cache) == 1


# ─────────────────────────────────────────────────────────────────────────────
# End to end through the job pipeline
# ─────────────────────────────────────────────────────────────────────────────


def test_dpc2d_end_to_end_through_postprocess_job():
    """Drive the routine through PostprocessJob → SaveZarrJob with four input
    states and read the derived plates back via tensorstore (no hardware)."""
    import os
    import tempfile
    import time

    import squid.abc
    from control._def import FileSavingOption
    from control.core.job_processing import CaptureInfo, JobImage, PostprocessJob, SaveZarrJob, ZarrWriterInfo
    from control.models.observation_state import CameraSettings, IlluminatorState, ObservationState

    def _read_ts(path):
        import tensorstore as ts

        return ts.open(
            {"driver": "zarr3", "kvstore": {"driver": "file", "path": path}}, create=False, open=True
        ).result()

    yx = 48
    inputs = _synthetic_halves(size=yx, seed=13)
    output_specs = [
        {"name": "phase", "z_size": 1, "dtype": "float32", "channel_color": "#FFFFFF", "wavelength_nm": WAVELENGTH_NM},
        {
            "name": "brightfield",
            "z_size": 1,
            "dtype": "input",
            "channel_color": "#FFFFFF",
            "wavelength_nm": WAVELENGTH_NM,
        },
    ]
    spec_dict = {
        "routine": "dpc2d",
        "script_path": None,
        "params": _params(output_absorption=False),
    }
    ctx_meta = {
        "pixel_size_um": PIXEL_UM,
        "dz_um": None,
        "nz": 1,
        "nt": 1,
        "state_meta": {s: {"wavelength_nm": WAVELENGTH_NM} for s in STATES.values()},
    }

    with tempfile.TemporaryDirectory() as tmp:
        try:
            zwi = ZarrWriterInfo(
                base_path=tmp,
                t_size=1,
                c_size=1,
                z_size=1,
                is_hcs=True,
                region_fov_counts={"A1": 1},
                fov_translations_um={"A1": {0: (0.0, 0.0)}},
                pixel_size_um=PIXEL_UM,
                channel_names=["phase"],
                channel_colors=["#FFFFFF"],
                channel_wavelengths=[WAVELENGTH_NM],
            )
            res = None
            for role in ROLES:
                state = STATES[role]
                obs = ObservationState(
                    name=state,
                    camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=1.0),
                    illuminator_states=[
                        IlluminatorState(illumination_channel="BF LED matrix", intensity=50.0, on=True)
                    ],
                )
                cap = CaptureInfo(
                    position=squid.abc.Pos(x_mm=1.0, y_mm=2.0, z_mm=0.0, theta_rad=None),
                    z_index=0,
                    capture_time=time.time(),
                    observation_state=obs,
                    save_directory=tmp,
                    file_id="A1_0_0",
                    region_id="A1",
                    fov=0,
                    configuration_idx=0,
                    time_point=0,
                    file_saving_option=FileSavingOption.ZARR_V3,
                    acquisition_root=tmp,
                    postprocess_group="pp0",
                )
                job = PostprocessJob(
                    capture_info=cap,
                    capture_image=JobImage(image_array=inputs[state][0, 0].astype(np.uint16)),
                    group_key="pp0",
                    label="DPC",
                    spec_dict=spec_dict,
                    expected_frames=4,
                    output_specs=output_specs,
                    input_state_specs={s: {"acquire_z_stack": False, "frames_per_visit": 1} for s in STATES.values()},
                    ctx_meta=ctx_meta,
                )
                job.zarr_writer_info = zwi
                res = job.run()
            SaveZarrJob.finalize_all_writers()

            assert res is not None and res.error is None and res.outputs_written == 2
            assert os.path.isdir(os.path.join(tmp, "DPC_phase.ome.zarr"))
            assert os.path.isdir(os.path.join(tmp, "DPC_brightfield.ome.zarr"))
            for state in STATES.values():  # raw halves are never saved
                assert not os.path.isdir(os.path.join(tmp, f"{state}.ome.zarr"))
            phase = np.asarray(_read_ts(zwi.get_output_path("A1", 0, "DPC_phase"))[0, 0, 0])
            assert phase.shape == (yx, yx) and np.all(np.isfinite(phase))
            expected = DPC2DRoutine().process(
                {s: inputs[s].astype(np.uint16) for s in STATES.values()},
                _ctx(),
                _params(output_absorption=False),
            )["phase"]
            assert np.allclose(phase, expected)
        finally:
            PostprocessJob.clear_accumulators()
            SaveZarrJob.clear_writers()
