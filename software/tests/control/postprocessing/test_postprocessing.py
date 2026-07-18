"""Unit tests for the online-postprocessing execution layer.

Covers the registry (builtin / custom-script / missing-ROUTINE), the model-level
plan accounting for postprocessed events, and the ``PostprocessJob`` accumulate →
compute → write → barrier path driven end-to-end through the real ``SaveZarrJob``
(reading the derived plates back via tensorstore). No hardware.
"""

import os
import tempfile
import textwrap
import time

import numpy as np
import pytest

import squid.abc
from control._def import FileSavingOption
from control.core.job_processing import (
    CaptureInfo,
    JobImage,
    PostprocessJob,
    PostprocessResult,
    SaveZarrJob,
    ZarrWriterInfo,
)
from control.models.acquisition_cycle import (
    AcquisitionCycle,
    CycleGroup,
    CycleStep,
    PostprocessSpec,
    RegionPlan,
    chain_frame_counts,
    frame_coord,
    is_dense,
    resolve_cycle,
)
from control.models.observation_state import CameraSettings, IlluminatorState, ObservationState
from control.postprocessing.base import InputStateSpec, OutputSpec, PostprocessRoutine
from control.postprocessing import registry


# ─────────────────────────── helpers ───────────────────────────


def _obs(name):
    return ObservationState(
        name=name,
        camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=1.0),
        illuminator_states=[IlluminatorState(illumination_channel=name, intensity=50.0, on=True)],
    )


def _read_tensorstore(array_path):
    import tensorstore as ts

    return ts.open(
        {"driver": "zarr3", "kvstore": {"driver": "file", "path": array_path}},
        create=False,
        open=True,
    ).result()


class _StubRoutine(PostprocessRoutine):
    """Emits one 2D float32 output = mean over (F, Z) of the single input state."""

    name = "stub_mean"
    display_name = "Stub Mean"

    def describe_outputs(self, input_states, params):
        return [OutputSpec(name="mean", z_size=1, dtype="float32")]

    def warmup(self, input_states, ctx, params):
        ctx.cache["warmed"] = True

    def process(self, inputs, ctx, params):
        (stack,) = inputs.values()  # (F, Z, Y, X)
        ctx.cache["calls"] = ctx.cache.get("calls", 0) + 1
        return {"mean": stack.reshape(-1, *stack.shape[2:]).mean(axis=0)}


# ─────────────────────────── registry ───────────────────────────


def test_registry_builtin_phase2d():
    spec = PostprocessSpec(routine="phase2d")
    routine = registry.load_routine(spec)
    assert routine.name == "phase2d"


def test_registry_unknown_routine():
    with pytest.raises(ValueError, match="Unknown postprocessing routine"):
        registry.load_routine(PostprocessSpec(routine="nope"))


def test_registry_custom_script_roundtrip():
    script = textwrap.dedent(
        """
        import numpy as np
        from control.postprocessing.base import OutputSpec, PostprocessRoutine

        class R(PostprocessRoutine):
            name = "custom"
            def describe_outputs(self, input_states, params):
                return [OutputSpec(name="out", z_size=1, dtype="float32")]
            def process(self, inputs, ctx, params):
                (s,) = inputs.values()
                return {"out": s.reshape(-1, *s.shape[2:]).max(axis=0)}

        ROUTINE = R()
        """
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "routine.py")
        with open(path, "w") as f:
            f.write(script)
        routine = registry.load_routine(PostprocessSpec(routine="script", script_path=path))
        assert routine.describe_outputs({}, {})[0].name == "out"


def test_registry_custom_script_missing_ROUTINE():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bad.py")
        with open(path, "w") as f:
            f.write("x = 1\n")
        with pytest.raises(ValueError, match="module-level ROUTINE"):
            registry.load_routine(PostprocessSpec(routine="script", script_path=path))


# ─────────────────────────── model accounting ───────────────────────────


def test_postprocessed_events_excluded_from_saved_layout():
    cyc = AcquisitionCycle(
        name="pp",
        items=[
            CycleStep(observation_state="BF", postprocess=PostprocessSpec(routine="stub_mean")),
            CycleStep(observation_state="GFP"),
        ],
    )
    plan = RegionPlan.from_events(resolve_cycle(cyc))
    # BF is postprocessed → not a saved plate; GFP is the only saved raw plate.
    assert plan.array_keys == ["GFP"]
    assert plan.channel_order == ["GFP"]
    assert "BF" not in chain_frame_counts(plan.events)
    # One postprocess group with BF as its single input.
    assert len(plan.postprocess_groups) == 1
    group = next(iter(plan.postprocess_groups.values()))
    assert list(group.input_states) == ["BF"]
    assert group.input_states["BF"].frames_per_visit == 1


def test_dual_use_state_keeps_contiguous_saved_t():
    # Same state saved AND postprocessed under a group → the saved copies keep a
    # contiguous 0..n-1 T index (postprocessed occurrences count separately).
    cyc = AcquisitionCycle(
        name="dual",
        items=[
            CycleStep(observation_state="BF", n_frames=2),  # saved
            CycleGroup(
                repeat=1,
                steps=[CycleStep(observation_state="BF", n_frames=2)],
                postprocess=PostprocessSpec(routine="stub_mean"),
            ),
        ],
    )
    plan = RegionPlan.from_events(resolve_cycle(cyc))
    saved = [e for e in plan.events if e.postprocess is None]
    assert [e.state_frame_index for e in saved] == [0, 1]
    assert plan.frame_counts == {"BF": 2}


def test_group_level_postprocess_pools_member_steps():
    cyc = AcquisitionCycle(
        name="dpc",
        items=[
            CycleGroup(
                repeat=1,
                steps=[CycleStep(observation_state=s) for s in ("dpc_l", "dpc_r", "dpc_t", "dpc_b")],
                postprocess=PostprocessSpec(routine="stub_mean", label="dpc"),
            )
        ],
    )
    plan = RegionPlan.from_events(resolve_cycle(cyc))
    assert len(plan.postprocess_groups) == 1
    group = next(iter(plan.postprocess_groups.values()))
    assert sorted(group.input_states) == ["dpc_b", "dpc_l", "dpc_r", "dpc_t"]
    # All four member events share one group id.
    gids = {e.postprocess_group for e in plan.events}
    assert gids == {next(iter(plan.postprocess_groups))}


def test_frame_coord_raises_for_postprocessed_event():
    cyc = AcquisitionCycle(
        name="pp", items=[CycleStep(observation_state="BF", postprocess=PostprocessSpec(routine="stub_mean"))]
    )
    plan = RegionPlan.from_events(resolve_cycle(cyc))
    ev = plan.events[0]
    with pytest.raises(ValueError, match="postprocessed"):
        frame_coord(plan, 1, 0, ev)


def test_postprocess_spec_yaml_roundtrip():
    spec = PostprocessSpec(routine="phase2d", params={"regularization": 1e-2}, label="bf")
    step = CycleStep(observation_state="BF", postprocess=spec)
    dumped = step.model_dump(mode="json")
    restored = CycleStep(**dumped)
    assert restored.postprocess.routine == "phase2d"
    assert restored.postprocess.params["regularization"] == 1e-2
    # Old YAML without the field still loads (default None).
    assert CycleStep(observation_state="BF").postprocess is None


# ─────────────────────────── PostprocessJob end-to-end ───────────────────────────


def _make_pp_job(tmp, zwi, group_key, label, expected_frames, output_specs, input_state_specs, ctx_meta,
                 state, z_index, image, t_scan=0):
    cap = CaptureInfo(
        position=squid.abc.Pos(x_mm=1.0, y_mm=2.0, z_mm=0.0, theta_rad=None),
        z_index=z_index,
        capture_time=time.time(),
        observation_state=_obs(state),
        save_directory=tmp,
        file_id="A1_0_0",
        region_id="A1",
        fov=0,
        configuration_idx=0,
        time_point=t_scan,
        file_saving_option=FileSavingOption.ZARR_V3,
        acquisition_root=tmp,
        postprocess_group=group_key,
    )
    job = PostprocessJob(
        capture_info=cap,
        capture_image=JobImage(image_array=image),
        group_key=group_key,
        label=label,
        spec_dict={"routine": "stub_mean", "script_path": None, "params": {}},
        expected_frames=expected_frames,
        output_specs=output_specs,
        input_state_specs=input_state_specs,
        ctx_meta=ctx_meta,
    )
    job.zarr_writer_info = zwi
    return job


def test_postprocess_job_accumulate_compute_write():
    NZ = 3
    registry.BUILTIN_ROUTINES["stub_mean"] = _StubRoutine
    try:
        with tempfile.TemporaryDirectory() as tmp:
            zwi = ZarrWriterInfo(
                base_path=tmp,
                t_size=1,
                c_size=1,
                z_size=NZ,
                is_hcs=True,
                region_fov_counts={"A1": 1},
                fov_translations_um={"A1": {0: (0.0, 0.0)}},
                pixel_size_um=0.325,
                channel_names=["phase_mean"],
                channel_colors=["#FFFFFF"],
                channel_wavelengths=[None],
            )
            output_specs = [{"name": "mean", "z_size": 1, "dtype": "float32",
                             "channel_color": "#FFFFFF", "wavelength_nm": None}]
            input_state_specs = {"BF": {"acquire_z_stack": True, "frames_per_visit": 1}}
            ctx_meta = {"pixel_size_um": 0.325, "dz_um": 1.0, "nz": NZ, "nt": 1, "state_meta": {}}
            results = []
            for z in range(NZ):
                job = _make_pp_job(
                    tmp, zwi, "pp0", "bf", NZ, output_specs, input_state_specs, ctx_meta,
                    state="BF", z_index=z, image=np.full((16, 16), (z + 1) * 100, np.uint16),
                )
                results.append(job.run())
            SaveZarrJob.finalize_all_writers()

            # Only the last frame completes the group and returns a result.
            assert results[:-1] == [None, None]
            assert isinstance(results[-1], PostprocessResult)
            assert results[-1].outputs_written == 1
            assert "bf_mean" in results[-1].display_images

            # Derived plate exists (input BF plate does NOT).
            assert os.path.isdir(os.path.join(tmp, "bf_mean.ome.zarr"))
            assert not os.path.isdir(os.path.join(tmp, "BF.ome.zarr"))
            ds = _read_tensorstore(zwi.get_output_path("A1", 0, "bf_mean"))
            assert ds.shape[:3] == (1, 1, 1)
            # Mean of 100,200,300 = 200.
            assert np.isclose(np.asarray(ds[0, 0, 0]).mean(), 200.0)
    finally:
        registry.BUILTIN_ROUTINES.pop("stub_mean", None)
        PostprocessJob.clear_accumulators()
        SaveZarrJob.clear_writers()


def test_warmup_job_populates_shared_routine_cache():
    """PostprocessWarmupJob runs the routine's warmup and shares its cache (keyed
    by routine identity) with the per-FOV PostprocessJob in the same process."""
    from control.core.job_processing import PostprocessWarmupJob, PostprocessWarmupResult, postprocess_routine_key

    registry.BUILTIN_ROUTINES["stub_mean"] = _StubRoutine
    try:
        spec_dict = {"routine": "stub_mean", "script_path": None, "params": {}}
        ctx_meta = {"pixel_size_um": 0.3, "dz_um": 1.0, "nz": 3, "nt": 1, "state_meta": {}, "yx_shape": (16, 16)}
        wj = PostprocessWarmupJob(
            label="bf",
            spec_dict=spec_dict,
            input_state_specs={"BF": {"acquire_z_stack": True, "frames_per_visit": 1}},
            ctx_meta=ctx_meta,
        )
        res = wj.run()
        assert isinstance(res, PostprocessWarmupResult) and res.ok
        key = postprocess_routine_key(spec_dict)
        assert PostprocessJob._routine_caches[key].get("warmed") is True
        # A PostprocessJob with the same spec reuses that warmed cache instance.
        _routine, cache = PostprocessJob.ensure_routine(spec_dict)
        assert cache is PostprocessJob._routine_caches[key]
        assert cache.get("warmed") is True
    finally:
        registry.BUILTIN_ROUTINES.pop("stub_mean", None)
        PostprocessJob.clear_accumulators()


def test_routine_key_distinguishes_params():
    from control.core.job_processing import postprocess_routine_key

    a = postprocess_routine_key({"routine": "phase2d", "script_path": None, "params": {"regularization": 1e-3}})
    b = postprocess_routine_key({"routine": "phase2d", "script_path": None, "params": {"regularization": 1e-2}})
    same = postprocess_routine_key({"routine": "phase2d", "script_path": None, "params": {"regularization": 1e-3}})
    assert a != b and a == same


def test_disk_estimate_excludes_postprocessed_raw_and_adds_derived():
    """The disk-size estimator must NOT count postprocessed raw frames (they are
    consumed by the routine and never saved) and MUST add the derived output
    plates. Uses a no-hardware controller stub (subclass with a no-op __init__)."""
    from types import SimpleNamespace

    import control._def
    from control._def import FileSavingOption
    from squid.config import CameraPixelFormat
    from control.core.multi_point_controller import MultiPointController

    # Plan: one saved GFP step + one postprocessed BF step producing 2 outputs.
    cyc = AcquisitionCycle(
        name="c",
        items=[
            CycleStep(observation_state="GFP"),
            CycleStep(observation_state="BF", postprocess=PostprocessSpec(routine="stub")),
        ],
    )
    plan = RegionPlan.from_events(resolve_cycle(cyc))
    # The postprocessed raw frame is excluded from the saved count / plates.
    assert plan.frames_per_position == 1  # only GFP
    assert plan.array_keys == ["GFP"]
    group = next(iter(plan.postprocess_groups.values()))
    group.outputs = [
        OutputSpec(name="phase", z_size=1, dtype="float32"),
        OutputSpec(name="bf_center", z_size=2, dtype="input"),
    ]

    class _MPC(MultiPointController):
        def __init__(self):  # bypass the heavy real init
            pass

    mpc = _MPC()
    mpc.selected_cycle_names = ["c"]
    mpc.region_cycle_map = None
    mpc.Nt = 3
    mpc.file_saving_option = FileSavingOption.INDIVIDUAL_IMAGES
    mpc.scanCoordinates = SimpleNamespace(region_fov_coordinates={"R0": [0, 1, 2, 3]})  # 4 FOVs
    mpc.camera = SimpleNamespace(
        get_crop_size=lambda: (100, 50),  # W×H = 5000 px/plane
        get_pixel_format=lambda: CameraPixelFormat.MONO16,
    )
    mpc._resolve_plan = lambda names, region: plan

    # Derived-only bytes (INDIVIDUAL_IMAGES factor = 1.0), Nt×FOVs = 12 visits:
    #   phase     (float32=4 B, z=1): 5000·4·12·1 = 240_000
    #   bf_center (input mono=2 B, z=2): 5000·2·12·2 = 240_000
    assert mpc._postprocess_derived_bytes() == 480_000

    # Full estimate = raw (already excludes postprocessed) + derived.
    mpc.get_acquisition_image_count = lambda: 12  # e.g. GFP: Nt·NZ·FOVs·1
    mpc._raw_bytes_per_image = lambda: 10_000
    assert mpc.estimate_acquisition_disk_bytes() == 12 * 10_000 + 480_000

    # A postprocessing-only run (no raw saved frames) still estimates the outputs.
    mpc.get_acquisition_image_count = lambda: 0
    assert mpc.estimate_acquisition_disk_bytes() == 480_000

    # The selected save format's factor is applied to derived plates too.
    mpc.file_saving_option = FileSavingOption.ZARR_V3
    assert mpc._postprocess_derived_bytes() == int(480_000 * mpc._format_size_factor())


def test_image_count_zmode_aware_and_derived_count():
    """The raw image count must count reference-z (single-plane) steps ONCE, not
    once per z, and exclude postprocessed inputs; the derived count reports the
    output plates. Mirrors the '11-z reconstruction + 2 single-plane BF' case."""
    from types import SimpleNamespace

    import control._def
    from control.core.multi_point_controller import MultiPointController
    from control.models.acquisition_cycle import AcquisitionCycle, CycleStep, PostprocessSpec, RegionPlan, resolve_cycle

    cyc = AcquisitionCycle(
        name="c",
        items=[
            # Full-z (11-plane) BF, postprocessed -> reconstruction (raw NOT saved).
            CycleStep(observation_state="BF", postprocess=PostprocessSpec(routine="stub")),
            # Two single-plane (reference-z) brightfield saves.
            CycleStep(observation_state="BF_a", acquire_z_stack=False),
            CycleStep(observation_state="BF_b", acquire_z_stack=False),
        ],
    )
    plan = RegionPlan.from_events(resolve_cycle(cyc))
    group = next(iter(plan.postprocess_groups.values()))
    group.outputs = [
        OutputSpec(name="phase", z_size=1, dtype="float32"),
        OutputSpec(name="bf_center", z_size=1, dtype="input"),
    ]

    class _MPC(MultiPointController):
        def __init__(self):
            pass

    mpc = _MPC()
    mpc.NZ = 11
    mpc.Nt = 1
    mpc.selected_cycle_names = ["c"]
    mpc.selected_observation_state_names = []
    mpc.region_cycle_map = None
    mpc.scanCoordinates = SimpleNamespace(region_fov_coordinates={"R0": [0]})  # 1 FOV
    mpc._resolve_plan = lambda names, region: plan

    merge_saved = control._def.MERGE_CHANNELS
    control._def.MERGE_CHANNELS = False
    try:
        # Two reference-z steps captured ONCE each (NOT ×NZ=11) → 2, not 22.
        assert mpc.get_acquisition_image_count() == 2
        # Derived: phase + bf_center, one set per FOV visit → 2.
        assert mpc.get_acquisition_derived_image_count() == 2
        # Live-estimate total the user sees: 4.
        assert mpc.get_acquisition_image_count() + mpc.get_acquisition_derived_image_count() == 4
    finally:
        control._def.MERGE_CHANNELS = merge_saved


def test_prepare_display_image_is_display_safe():
    """Display previews must be integer dtype (contrast manager uses np.iinfo) at
    native resolution (the shared live viewer holds one size across channels)."""
    job = PostprocessJob(capture_info=None, capture_image=JobImage(image_array=None))
    # Float output -> normalized uint16, same 2D shape.
    phase = np.linspace(-3.0, 5.0, 64 * 64, dtype=np.float32).reshape(64, 64)
    disp = job._prepare_display_image(phase)
    assert disp.dtype == np.uint16
    assert disp.shape == (64, 64)
    assert disp.min() == 0 and disp.max() == 65535
    # Integer output passes through as uint16, unchanged shape.
    raw = np.full((48, 48), 1234, np.uint16)
    disp2 = job._prepare_display_image(raw)
    assert disp2.dtype == np.uint16 and disp2.shape == (48, 48)
    # A 3D (Z,Y,X) output previews its center plane.
    vol = np.zeros((3, 20, 20), np.float32)
    assert job._prepare_display_image(vol).shape == (20, 20)


def test_postprocess_job_partial_group_returns_none_and_holds_bytes():
    import multiprocessing as mp

    NZ = 3
    registry.BUILTIN_ROUTINES["stub_mean"] = _StubRoutine
    bp_bytes = mp.Value("q", 0)
    cap_evt = mp.Event()
    PostprocessJob._bp_pending_bytes = bp_bytes
    PostprocessJob._bp_capacity_event = cap_evt
    try:
        with tempfile.TemporaryDirectory() as tmp:
            zwi = ZarrWriterInfo(
                base_path=tmp, t_size=1, c_size=1, z_size=NZ, is_hcs=True,
                region_fov_counts={"A1": 1}, fov_translations_um={"A1": {0: (0.0, 0.0)}},
                pixel_size_um=0.325, channel_names=["m"], channel_colors=["#FFFFFF"], channel_wavelengths=[None],
            )
            output_specs = [{"name": "mean", "z_size": 1, "dtype": "float32",
                             "channel_color": "#FFFFFF", "wavelength_nm": None}]
            input_state_specs = {"BF": {"acquire_z_stack": True, "frames_per_visit": 1}}
            ctx_meta = {"pixel_size_um": 0.325, "dz_um": 1.0, "nz": NZ, "nt": 1, "state_meta": {}}
            img = np.zeros((16, 16), np.uint16)
            # First two frames: held bytes accumulate, no result.
            for z in range(2):
                job = _make_pp_job(tmp, zwi, "pp0", "bf", NZ, output_specs, input_state_specs, ctx_meta,
                                   state="BF", z_index=z, image=img)
                assert job.run() is None
            assert bp_bytes.value == 2 * img.nbytes
            # Final frame completes the group and releases all held bytes.
            job = _make_pp_job(tmp, zwi, "pp0", "bf", NZ, output_specs, input_state_specs, ctx_meta,
                               state="BF", z_index=2, image=img)
            assert isinstance(job.run(), PostprocessResult)
            assert bp_bytes.value == 0
            SaveZarrJob.finalize_all_writers()
    finally:
        PostprocessJob._bp_pending_bytes = None
        PostprocessJob._bp_capacity_event = None
        registry.BUILTIN_ROUTINES.pop("stub_mean", None)
        PostprocessJob.clear_accumulators()
        SaveZarrJob.clear_writers()
