from itertools import permutations, product
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from qtpy.QtCore import QModelIndex

from control.models.acquisition_cycle import AcquisitionCycle, CycleGroup, CycleStep, CycleWait, RegionPlan, _index_events, resolve_chain
from control.models.acquisition_order import DEFAULT_ORDER, channel_blocks, iter_ordered_visits, validate_order
from gui.widgets.acquisition_order import AcquisitionOrderWidget
from tests.control.test_multipoint_observation_snaking import _advanced_events, _worker


ORDERS = list(permutations(DEFAULT_ORDER))


def test_advanced_selection_runs_each_entire_cycle_at_each_inner_position():
    cycles = {
        "first": AcquisitionCycle(name="first", repeat=2, items=[
            CycleStep(observation_state="A"),
            CycleGroup(repeat=2, steps=[CycleStep(observation_state="B"), CycleWait(duration_ms=10)]),
        ]),
        "second": AcquisitionCycle(name="second", items=[CycleStep(observation_state="D")]),
    }
    events = resolve_chain(["first", "second"], cycles.get)
    plan = RegionPlan.from_events(events)
    visits = list(iter_ordered_visits(("T", "C", "Pos", "Z"), [plan, plan], 1, 1, 0))
    assert [v.position for v in visits] == [0, 1, 0, 1]
    full_cycle = ["A", "B", "", "B", ""] * 2
    assert [[e.observation_state for _, group in v.planes for e in group] for v in visits] == [
        full_cycle, full_cycle, ["D"], ["D"],
    ]


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("advanced", [False, True])
def test_every_loop_order_keeps_indices_and_advanced_contents(order, advanced):
    events = _advanced_events() if advanced else _index_events([("state", (n, True)) for n in "AB"])
    plan = RegionPlan.from_events(events)
    blocks = channel_blocks(events)
    actual = [
        (visit.time_point, visit.position, z, event.cycle_event_index)
        for visit in iter_ordered_visits(order, [plan, plan], 2, 3, 1)
        for z, group in visit.planes for event in group
    ]
    sizes = {"T": 2, "Pos": 2, "Z": 3, "C": len(blocks)}
    expected = []
    for values in product(*(range(sizes[axis]) for axis in order)):
        coord = dict(zip(order, values))
        for event in blocks[coord["C"]]:
            if event.acquire_z_stack or coord["Z"] == 1:
                expected.append((coord["T"], coord["Pos"], coord["Z"], event.cycle_event_index))
    assert actual == expected
    assert len(actual) == len(set(actual))


def test_ragged_channels_and_inactive_dimensions():
    plans = [RegionPlan.from_events(_index_events([("state", (n, True)) for n in names])) for names in ("AB", "C")]
    visits = list(iter_ordered_visits(("C", "Z", "T", "Pos"), plans, 1, 1, 0))
    assert [(v.position, e.observation_state) for v in visits for _, events in v.planes for e in events] == [
        (0, "A"), (1, "C"), (0, "B")
    ]


def test_order_widget_drag_and_nested_preview(qtbot):
    widget = AcquisitionOrderWidget()
    qtbot.addWidget(widget)
    widget.show()
    widget.set_active({"T", "C", "Pos"})
    assert widget.blocks.model().moveRows(QModelIndex(), 1, 1, QModelIndex(), 4)
    assert widget.order() == ("T", "Z", "C", "Pos")
    assert widget.preview.text() == "[ T [ C [ Pos [ Capture ] ] ] ]"
    widget.restore(("Pos", "C", "Z", "T"))
    assert widget.preview.text() == "[ Pos [ C [ T [ Capture ] ] ] ]"
    with pytest.raises(ValueError):
        validate_order(("T", "C", "C", "Pos"))


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("advanced", [False, True])
def test_worker_executes_scheduled_planes_with_original_save_indices(order, advanced):
    events = _advanced_events() if advanced else _index_events([("state", (n, True)) for n in "AB"])
    worker = _worker(events, nz=3, mode="FROM CENTER", piezo=False, snake=False)
    worker.acquisition_order = order
    worker.dt = 0
    worker.fluidics = None
    worker._generate_downsampled_views = False
    worker._camera_dropped_frame_count = lambda: 0
    worker._report_timepoint_frame_drops = lambda _: None
    worker._wait_for_outstanding_callback_images = lambda: None
    worker.stage.move_z_to = lambda z: setattr(worker.stage, "z", z)
    worker.stage.get_pos = lambda: SimpleNamespace(x_mm=0, y_mm=0, z_mm=worker.stage.z)
    captured = []
    completed = []
    worker.acquire_camera_image = lambda config, file_id, path, z, **kw: captured.append(
        (worker.time_point, worker._ordered_visit.position, z, config.name, kw["state_frame_index"])
    )

    def visit():
        region, fov = worker._ordered_position
        worker.acquire_at_position(region, "", fov)
        assert worker.stage.z == pytest.approx(5.0)
        if worker._ordered_final_timepoint:
            completed.append(worker.time_point)
    worker.run_single_time_point = visit
    expected = [
        (v.time_point, v.position, z if e.acquire_z_stack else 0, e.observation_state, e.state_frame_index)
        for v in iter_ordered_visits(order, [worker._global_plan] * 3, 2, 3, 1)
        for z, group in v.planes for e in group
    ]
    worker.run_ordered_acquisition()
    assert captured == expected
    assert sorted(completed) == [0, 1]
    assert worker._autofocus_and_record.call_count == 6
    progress = [call.args[0] for call in worker.callbacks.signal_region_progress.call_args_list]
    assert [item.current_fov for item in progress] == list(range(1, len(expected) + 1))
    assert all(item.region_fovs == len(expected) for item in progress)


def test_time_intervals_are_local_to_outer_position():
    plan = RegionPlan.from_events(_index_events([("state", ("A", True))]))
    visits = list(iter_ordered_visits(("Pos", "T", "Z", "C"), [plan, plan], 3, 1, 0))
    assert [(v.time_group, v.time_point) for v in visits] == [
        ((0,), 0), ((0,), 1), ((0,), 2), ((1,), 0), ((1,), 1), ((1,), 2)
    ]


@pytest.mark.parametrize("order", [("C", "T", "Pos", "Z"), ("Pos", "T", "Z", "C")])
def test_real_timepoint_lifecycle_keeps_rows_stats_and_interval_clocks(tmp_path, monkeypatch, order):
    import pandas as pd
    from control._def import FileSavingOption
    from control.core.multi_point_worker import MultiPointWorker

    events = _index_events([("state", (n, True)) for n in "AB"])
    worker = _worker(events, nz=3, mode="FROM CENTER", piezo=False, snake=False)
    worker.acquisition_order = order
    worker.dt = 5
    worker.fluidics = None
    worker._generate_downsampled_views = False
    worker._slack_notifier = None
    worker.validation_mode = False
    worker.skip_saving = False
    worker.file_saving_option = FileSavingOption.ZARR_V3
    worker.experiment_path = str(tmp_path)
    worker.microcontroller = MagicMock()
    worker.microscope = MagicMock()
    worker._camera_dropped_frame_count = lambda: 0
    worker._report_timepoint_frame_drops = lambda _: None
    worker._wait_for_outstanding_callback_images = lambda: None
    worker._needs_per_timepoint_folder = lambda: False
    worker.initialize_z_stack = lambda: None
    worker.stage.move_z_to = lambda z: setattr(worker.stage, "z", z)
    worker.update_coordinates_dataframe = MultiPointWorker.update_coordinates_dataframe.__get__(worker)
    now = [100.0]
    monkeypatch.setattr("control.core.ordered_acquisition.time.time", lambda: now[0])
    worker._interruptible_sleep = lambda delay: now.__setitem__(0, now[0] + delay)
    starts, final_counts = [], []

    def capture(*args, **kwargs):
        worker._timepoint_image_count += 1
    worker.acquire_camera_image = capture

    def coordinates(path):
        starts.append((worker._ordered_visit.time_group, worker.time_point, now[0]))
        region, fov = worker._ordered_position
        worker.acquire_at_position(region, path, fov)
        if worker._ordered_final_timepoint:
            final_counts.append((worker.time_point, worker._timepoint_image_count, worker._timepoint_fov_count))
    worker.run_coordinate_acquisition = coordinates
    worker.run_ordered_acquisition()
    assert sorted(final_counts) == [(0, 18, 3), (1, 18, 3)]
    rows = pd.read_csv(tmp_path / "acquired_positions.csv")
    assert len(rows) == 18
    assert not rows.duplicated(["time_point", "region", "fov", "z_level"]).any()
    first_starts = {}
    for group, t, timestamp in starts:
        first_starts.setdefault((group, t), timestamp)
    for group, t in first_starts:
        if t == 1:
            assert first_starts[group, 1] - first_starts[group, 0] == 5


@pytest.mark.parametrize("widget_type", ["wellplate", "flexible"])
def test_order_yaml_roundtrip(tmp_path, monkeypatch, widget_type):
    from control.acquisition_yaml_loader import parse_acquisition_yaml
    from control.core.multi_point_controller import _save_unified_multipoint_acquisition_yaml
    from tests.control.test_multipoint_z_retract import _params

    order = ("C", "Pos", "T", "Z")
    monkeypatch.setattr(
        "control.core.acquisition_metadata_helpers.augment_multipoint_acquisition_yaml_dict",
        lambda base_yaml, **kwargs: {"schema_version": 2, **base_yaml},
    )
    _save_unified_multipoint_acquisition_yaml(
        _params(acquisition_order=order), str(tmp_path), widget_type=widget_type,
        repo=MagicMock(), live_controller=MagicMock(), camera=MagicMock(), objective_store=MagicMock(),
        recording_start_time=0, selected_observation_state_names=[], use_manual_focus_map=False, logger=MagicMock(),
    )
    assert parse_acquisition_yaml(str(tmp_path / "acquisition.yaml")).acquisition_order == order


def test_downsample_accumulators_do_not_mix_interleaved_timepoints(monkeypatch):
    import numpy as np
    from control.core.job_processing import DownsampledViewJob

    DownsampledViewJob.clear_accumulators()
    image = np.ones((16, 16), dtype=np.uint16)
    monkeypatch.setattr(DownsampledViewJob, "image_array", lambda self: image * (self.time_point + 1))
    try:
        for t in (0, 1):
            job = DownsampledViewJob(
                capture_info=MagicMock(), capture_image=MagicMock(),
                well_id="A1", time_point=t, total_fovs_in_well=2, skip_saving=True,
            )
            assert job.run() is None
        assert DownsampledViewJob.get_accumulator_count() == 2
        for t in (0, 1):
            result = DownsampledViewJob(
                capture_info=MagicMock(), capture_image=MagicMock(),
                well_id="A1", time_point=t, fov_index=1, total_fovs_in_well=2, skip_saving=True,
                target_resolutions_um=[], plate_resolution_um=1,
            ).run()
            assert result.time_point == t
            assert np.max(result.well_images[0]) == t + 1
        assert DownsampledViewJob.get_accumulator_count() == 0
    finally:
        DownsampledViewJob.clear_accumulators()


def test_ordered_coordinate_visit_does_not_scan_other_positions():
    worker = _worker(_advanced_events(), nz=3, mode="FROM CENTER", piezo=False, snake=False)
    worker._ordered_visit = SimpleNamespace()
    worker._ordered_position = ("R1", 0)
    worker._ordered_prewarmed = False
    worker._backpressure = MagicMock()
    worker.validation_mode = False
    worker._prewarm_observation_states = MagicMock()
    worker._prewarm_postprocess_routines = MagicMock()
    worker.scan_region_coords_mm = [(0, 0), (1, 1)]
    worker._summarize_runner_outputs = lambda: SimpleNamespace(none_failed=True)
    worker._first_fov_pre_moved = False
    worker._upload_target = None
    worker.move_to_coordinate = MagicMock()
    worker.acquire_at_position = MagicMock()
    worker.run_coordinate_acquisition("path")
    worker.acquire_at_position.assert_called_once_with("R1", "path", 0)
    worker.move_to_coordinate.assert_called_once_with((1, 1), "R1", 0)
    worker.run_coordinate_acquisition("path")
    worker._prewarm_observation_states.assert_called_once()
    worker._backpressure.reset.assert_called_once()


def test_abort_stops_the_remaining_channel_block_and_restores_z():
    worker = _worker(_advanced_events(), nz=3, mode="FROM CENTER", piezo=False, snake=False)
    captured = []
    worker.acquire_camera_image = lambda *a, **kw: captured.append(kw)
    worker.abort_requested_fn = lambda: bool(captured)
    worker.handle_acquisition_abort = MagicMock()
    worker.acquire_at_position("R0", "path", 0)
    assert len(captured) == 1
    assert worker.stage.z == pytest.approx(5.0)
