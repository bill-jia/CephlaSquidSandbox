"""FOV snaking preserves advanced blocks and canonical saved coordinates."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from control._def import TriggerMode
from control.acquisition_yaml_loader import parse_acquisition_yaml
from control.core.multi_point_controller import _save_unified_multipoint_acquisition_yaml
from control.core.multi_point_worker import MultiPointWorker
from control.models.acquisition_cycle import (
    AcquisitionCycle, CycleGroup, CycleStep, CycleWait, RegionPlan,
    _index_events, iter_acquisition_planes, resolve_chain, resolve_cycle,
)
from tests.control.test_multipoint_z_retract import _params


def _advanced_events():
    return resolve_cycle(AcquisitionCycle(name="mixed", items=[
        CycleStep(observation_state="A", n_frames=2),
        CycleGroup(repeat=2, steps=[
            CycleStep(observation_state="B"),
            CycleStep(observation_state="C", acquire_z_stack=False),
        ]),
        CycleStep(observation_state="D", acquire_z_stack=False),
    ]))


def test_chain_keeps_each_group_repeat_and_sweep_inside_one_block():
    from control.models.acquisition_cycle import CycleFPMBrightfield

    cycle = AcquisitionCycle(name="cycle", repeat=2, items=[
        CycleStep(observation_state="A", n_frames=2),
        CycleGroup(repeat=2, steps=[
            CycleStep(observation_state="B"), CycleWait(duration_ms=10),
            CycleStep(observation_state="stim"),
        ]),
        CycleFPMBrightfield(observation_state="FPM"),
    ])
    events = resolve_chain(
        ["cycle", "cycle"], lambda _: cycle, lambda name: name == "stim",
        lambda item: [(item.observation_state, (1,)), (item.observation_state, (2,))],
    )
    assert [e.acquisition_block_index for e in events] == [
        i for repeat in range(4) for i in [repeat * 3] * 2 + [repeat * 3 + 1] * 6 + [repeat * 3 + 2] * 2
    ]
    forward = list(iter_acquisition_planes(events, 3, 1, snake=True))
    reverse = list(iter_acquisition_planes(events, 3, 1, snake=True, reverse=True))
    # Reverse the blocks, including all Z planes, but never their contents.
    forward_blocks = [forward[i:i + 3] for i in range(0, len(forward), 3)]
    assert reverse == [plane for block in reversed(forward_blocks) for plane in block]
    assert [e.cycle_event_index for e in events] == list(range(len(events)))


def _worker(events, *, nz, mode, piezo, snake):
    worker = MultiPointWorker.__new__(MultiPointWorker)
    worker.NZ, worker.Nt = nz, 2
    worker.deltaZ = -0.002 if mode == "FROM TOP" else 0.002
    worker.z_stacking_config = mode
    worker.use_piezo = piezo
    worker.snake_observation_states = snake
    worker.time_point = 0
    worker.scan_region_fov_coords_mm = {"R0": [(0, 0)] * 2, "R1": [(1, 1)]}
    worker._global_plan = RegionPlan.from_events(events)
    worker._region_plans = {}
    worker._log = MagicMock()
    worker._timing = SimpleNamespace(get_timer=lambda _: nullcontext())
    worker.stage = SimpleNamespace(z=5.0)
    worker.stage.get_pos = lambda: SimpleNamespace(x_mm=0, y_mm=0, z_mm=worker.stage.z)
    worker.stage.move_z = lambda dz: setattr(worker.stage, "z", worker.stage.z + dz)
    worker.piezo = SimpleNamespace(position=100.0)
    worker.piezo.move_to = lambda z: setattr(worker.piezo, "position", z)
    worker.liveController = SimpleNamespace(trigger_mode=TriggerMode.SOFTWARE)
    worker._sleep = lambda _: None
    worker._autofocus_and_record = MagicMock()
    worker._apply_observation_state = lambda name: SimpleNamespace(name=name, is_stimulus_only=False)
    worker._channel_display_meta = lambda _: ("#FFFFFF", None)
    worker.handle_z_offset = MagicMock()
    worker.update_coordinates_dataframe = MagicMock()
    worker.callbacks = MagicMock()
    worker.abort_requested_fn = lambda: False
    worker.af_fov_count = worker._timepoint_fov_count = 0
    worker.total_scans = worker._captured_frames_per_fov(worker._global_plan) * 2
    return worker


@pytest.mark.parametrize("advanced", [False, True])
@pytest.mark.parametrize("snake", [False, True])
@pytest.mark.parametrize("piezo", [False, True])
@pytest.mark.parametrize("mode", ["FROM BOTTOM", "FROM CENTER", "FROM TOP"])
@pytest.mark.parametrize("nz", [1, 3, 4])
def test_acquire_order_positions_and_save_indices(advanced, snake, piezo, mode, nz):
    events = _advanced_events() if advanced else _index_events([("state", (n, True)) for n in "ABC"])
    worker = _worker(events, nz=nz, mode=mode, piezo=piezo, snake=snake)
    ref = worker._reference_z_level()
    visits = []
    for fov in range(2):
        captured = []

        def capture(config, file_id, path, z, **kwargs):
            layout = kwargs["save_layout"]
            event = events[layout.cycle_event_index]
            canonical_z = z if event.acquire_z_stack else ref
            physical_z = worker.stage.z + (worker.piezo.position - 100.0) / 1000
            assert physical_z == pytest.approx(5.0 + worker.deltaZ * (canonical_z - ref))
            assert int(file_id.rsplit("_", 1)[1]) == canonical_z
            assert layout.state_frame_index == event.state_frame_index
            assert kwargs["state_frame_index"] == event.state_frame_index
            assert layout.z_size == (nz if event.acquire_z_stack else 1)
            captured.append((canonical_z, layout.cycle_event_index, layout.t_index, layout.c_index))

        worker.acquire_camera_image = capture
        worker.acquire_at_position("R0", ".", fov)
        assert worker.stage.z == pytest.approx(5.0)
        assert worker.piezo.position == pytest.approx(100.0)
        assert len(captured) == worker._captured_frames_per_fov(worker._global_plan)
        visits.append(captured)

    assert sorted(visits[0]) == sorted(visits[1])
    if not snake:
        assert visits[0] == visits[1]
        assert [entry[0] for entry in visits[0]] == sorted(entry[0] for entry in visits[0])
    elif not advanced:
        assert visits[1] == list(reversed(visits[0]))
    else:
        blocks = [[entry for entry in visits[0] if events[entry[1]].acquisition_block_index == i] for i in range(3)]
        assert visits[1] == [entry for block in reversed(blocks) for entry in block]
        # A full stack for step A, then the full group stack, then ref-only D.
        assert [entry[0] for entry in blocks[0]] == [z for z in range(nz) for _ in range(2)]
        assert [entry[1] for entry in blocks[1] if entry[0] == ref] == [2, 3, 4, 5]
        assert [entry[0] for entry in blocks[2]] == [ref]
    progress = [call.args[0].current_fov for call in worker.callbacks.signal_region_progress.call_args_list]
    assert progress == list(range(1, len(visits[0]) * 2 + 1))
    assert worker.af_fov_count == 2
    assert worker.update_coordinates_dataframe.call_count == 2 * nz


def test_postprocess_captures_keep_frame_indices_when_blocks_reverse():
    from control._def import FileSavingOption
    from control.models.acquisition_cycle import PostprocessSpec

    events = resolve_cycle(AcquisitionCycle(name="processed", items=[
        CycleStep(observation_state="A", n_frames=2, postprocess=PostprocessSpec(routine="mean")),
        CycleStep(observation_state="B", acquire_z_stack=False),
    ]))
    worker = _worker(events, nz=3, mode="FROM CENTER", piezo=False, snake=True)
    worker.liveController.trigger_mode = TriggerMode.CONTINUOUS
    worker.keep_illuminators_on_between_captures = False
    worker._use_observation_presets = True
    worker._apply_current_illumination_state_to_hardware = MagicMock()
    worker._turn_off_capture_illumination_preserving_logical_state = MagicMock()
    worker._record_capture_sub_timings = MagicMock()
    worker._ready_for_next_trigger = MagicMock()
    worker._frame_wait_timeout_s = lambda: 1.0
    worker._backpressure = SimpleNamespace(should_throttle=lambda: False)
    worker.camera = MagicMock()
    worker.camera.get_total_frame_time.return_value = 1.0
    worker.file_saving_option = FileSavingOption.INDIVIDUAL_IMAGES
    worker.experiment_path = "."
    captured = []

    def capture(*args, **kwargs):
        MultiPointWorker.acquire_camera_image(worker, *args, **kwargs)
        info = worker._current_capture_info
        captured.append((info.observation_state.name, info.state_frame_index, info.z_index))

    worker.acquire_camera_image = capture
    worker.acquire_at_position("R0", ".", 1)
    assert captured == [("B", 0, 0)] + [("A", frame, z) for z in range(3) for frame in range(2)]


def test_direction_continues_across_regions_and_timepoints():
    worker = _worker(_advanced_events(), nz=3, mode="FROM CENTER", piezo=False, snake=True)
    directions = []
    for timepoint in range(2):
        worker.time_point = timepoint
        for region, coords in worker.scan_region_fov_coords_mm.items():
            directions.extend(worker._reverse_observation_order(region, fov) for fov in range(len(coords)))
    assert directions == [False, True, False, True, False, True]


@pytest.mark.parametrize("enabled", [False, True])
def test_yaml_roundtrip(tmp_path, monkeypatch, enabled):
    monkeypatch.setattr(
        "control.core.acquisition_metadata_helpers.augment_multipoint_acquisition_yaml_dict",
        lambda base_yaml, **kwargs: {"schema_version": 2, **base_yaml},
    )
    _save_unified_multipoint_acquisition_yaml(
        _params(snake_observation_states=enabled), str(tmp_path),
        widget_type="flexible", repo=MagicMock(), live_controller=MagicMock(),
        camera=MagicMock(), objective_store=MagicMock(), recording_start_time=0.0,
        selected_observation_state_names=[], use_manual_focus_map=False, logger=MagicMock(),
    )
    assert parse_acquisition_yaml(str(tmp_path / "acquisition.yaml")).snake_observation_states is enabled


def test_old_yaml_defaults_to_no_snaking(tmp_path):
    path = tmp_path / "acquisition.yaml"
    path.write_text("acquisition:\n  widget_type: flexible\n")
    assert not parse_acquisition_yaml(str(path)).snake_observation_states
