"""AF diagnostics must preserve control behavior and produce attributable evidence."""

import csv
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from control.core.af_validation_collector import AFValidationCollector
from control.core.laser_auto_focus_controller import AFValidationRestoreError
from control.core.multi_point_controller import MultiPointController
from control.core.multi_point_worker import MultiPointWorker
from tests.control.core.test_laser_auto_focus_controller import (
    FakeFocusCamera, FakeStage, apply_reference_at_current_position, make_controller, render_spot,
)


class Stage(FakeStage):
    def __init__(self):
        super().__init__()
        self.x_mm = self.y_mm = 0.0

    def get_pos(self):
        return SimpleNamespace(x_mm=self.x_mm, y_mm=self.y_mm, z_mm=self.z_um / 1000)

    def move_z_to(self, value, blocking=True):
        self.z_um = value * 1000

    def move_x_to(self, value, blocking=True):
        self.x_mm = value

    def move_y_to(self, value, blocking=True):
        self.y_mm = value


class Camera(FakeFocusCamera):
    def __init__(self, render):
        super().__init__(render)
        self.roi = (100, 200, 1536, 256)
        self.streaming = True
        self.fail_restore = False

    def get_region_of_interest(self):
        return self.roi

    def set_region_of_interest(self, *roi):
        if self.fail_restore and roi == (100, 200, 1536, 256):
            raise RuntimeError("restore failed")
        self.roi = roi

    def get_resolution(self):
        return (3088, 2064)

    def get_is_streaming(self):
        return self.streaming

    def stop_streaming(self):
        self.streaming = False

    def start_streaming(self):
        self.streaming = True


def controller_fixture():
    stage = Stage()
    camera = Camera(render_spot(stage, 0.2))
    controller = make_controller(camera, stage)
    controller._save_failure_debug_image = MagicMock()
    apply_reference_at_current_position(controller)
    return controller, stage, camera


def test_diagnostics_do_not_change_correction_and_reset_each_operation():
    controller, stage, camera = controller_fixture()
    stage.z_um = 20
    with controller.collect_validation_event() as records:
        assert controller.move_to_target(0)
        controller.laser_af_properties = controller.laser_af_properties.model_copy(update={"has_reference": False})
        assert not controller.move_to_target(0)
    assert abs(stage.z_um) < 1
    assert len(records) == 2
    assert records[0]["success"] and records[0]["correlation"] > 0.9
    assert records[1]["failure_reason"] == "no_reference"
    assert records[1]["correlation"] is None and records[1]["frame"] is None
    assert controller._validation_records is None
    assert camera.get_callbacks_enabled()


def test_rejected_frames_survive_subsequent_verification_measurements():
    controller, stage, camera = controller_fixture()
    render = camera._render
    controller._validation_boundary = MagicMock()  # isolate centroid rejection from diagnostic captures
    images = [np.zeros_like(render())]
    camera._render = lambda: images.pop() if images else render()
    with controller.collect_validation_event() as records:
        assert controller.move_to_target(0)
    assert records[0]["success"]
    assert len(records[0]["rejected_frames"]) == 1
    assert records[0]["rejected_frames"][0][1]["reason"] == "no_spot"
    assert records[0]["frame"].max() > 0


@pytest.mark.parametrize("was_streaming,callbacks", [(True, True), (False, False)])
def test_snapshot_restores_actual_camera_state_and_retries_trigger(was_streaming, callbacks):
    controller, _, camera = controller_fixture()
    camera.streaming = was_streaming
    camera.enable_callbacks(callbacks)
    camera.drop_next_triggers = 1
    prior = camera.trigger_count
    assert controller.capture_validation_frame() is not None
    assert camera.trigger_count == prior + 2
    assert camera.roi == (100, 200, 1536, 256)
    assert camera.streaming == was_streaming
    assert camera.get_callbacks_enabled() == callbacks


def test_snapshot_restores_state_when_laser_on_raises():
    controller, _, camera = controller_fixture()
    controller.turn_on_AF_laser = MagicMock(side_effect=RuntimeError("laser failed"))
    assert controller.capture_validation_frame() is None
    assert camera.roi == (100, 200, 1536, 256)
    assert camera.streaming and camera.get_callbacks_enabled()


def test_snapshot_restore_failure_is_not_swallowed():
    controller, _, camera = controller_fixture()
    camera.fail_restore = True
    with pytest.raises(AFValidationRestoreError):
        controller.capture_validation_frame()
    controller.microcontroller.turn_off_AF_laser.assert_called()


def operation(success=True):
    return dict(
        kind="correction", success=success, failure_reason=None if success else "verify_failed",
        timestamp=1.0, config={"x_reference": 4, "pixel_to_um": 0.2},
        reference_crop=np.eye(8, dtype=np.float32), frame=np.eye(8, dtype=np.uint8),
        rejected_frames=[], z_before_mm=1.0, z_after_mm=1.01,
    )


def metadata(region="R0", phase="acquisition", status="ok"):
    return dict(phase=phase, time_point=0, region_id=region, fov=0, af_status=status, snapshot_z_mm=1.0)


def rows(path):
    with (path / "events.csv").open(newline="", encoding="utf-8") as src:
        return list(csv.DictReader(src))


def test_collector_distinguishes_seed_stale_failure_and_unmeasured_table(tmp_path):
    collector = AFValidationCollector(tmp_path)
    collector.record(metadata(phase="seed"), operation())
    collector.record(metadata(status="stale"), operation(False))
    collector.record(metadata(status="table"))
    events = rows(tmp_path)
    assert len(events) == 3
    assert events[0]["phase"] == "seed"
    assert events[1]["af_success"] == "False" and events[1]["af_status"] == "stale"
    assert events[2]["af_attempted"] == "False" and events[2]["af_success"] == ""
    assert (tmp_path / "baseline" / "baseline_native.tiff").exists()
    failure = tmp_path / "events" / events[1]["event_id"]
    assert (failure / "current_native.tiff").exists()
    assert (failure / "previous_success_native.tiff").exists()


def test_collector_keeps_region_reference_comparisons_separate(tmp_path):
    collector = AFValidationCollector(tmp_path)
    collector.record(metadata("R0"), operation())
    collector.record(metadata("R1"), operation(False))
    event = rows(tmp_path)[-1]
    folder = tmp_path / "events" / event["event_id"]
    assert not (folder / "previous_success_native.tiff").exists()
    assert len(list((tmp_path / "references").iterdir())) == 1


def make_worker(tmp_path):
    controller, stage, camera = controller_fixture()
    worker = MultiPointWorker.__new__(MultiPointWorker)
    worker.validation_mode = True
    worker._af_validator = AFValidationCollector(tmp_path)
    worker.laser_auto_focus_controller = controller
    worker.stage = stage
    worker.do_reflection_af, worker.do_autofocus = True, False
    worker.use_piezo = False
    worker._supervision_policy = SimpleNamespace(mode="off")
    worker.Nt, worker.NZ, worker.time_point, worker.af_fov_count = 2, 3, 0, 0
    worker._log = MagicMock()
    worker._timing = SimpleNamespace(get_timer=lambda *args: nullcontext())
    worker._sleep = MagicMock()
    worker._wait_for_move_settled = MagicMock()
    worker._apply_region_laser_af_reference = MagicMock()
    worker.scan_region_fov_coords_mm = {"R0": [(0, 0), (1, 0), (2, 0)]}
    worker.scan_region_names = ["R0"]
    worker.scan_region_coords_mm = [(0, 0)]
    worker._fov_z_map = {("R0", i): 0.0 for i in range(3)}
    worker._fov_z_delta_map = dict(worker._fov_z_map)
    worker._z_pos_proposal = dict(worker._fov_z_map)
    worker._region_anchor_z_current = {"R0": 0.0}
    worker._region_anchor_fov = {"R0": 0}
    worker._fovs_since_refresh = {}
    worker._last_region_id = None
    worker._laser_af_refresh_every_n_fovs = 10
    worker._laser_af_consistency_threshold_um = 5.0
    worker._laser_af_check_last_fov_per_region = True
    worker._laser_af_table_path_audit = False
    worker._laser_af_successes = worker._laser_af_failures = 0
    worker._record_autofocus_event = MagicMock()
    worker._timepoint_fov_count = 0
    worker.update_coordinates_dataframe = MagicMock()
    worker.callbacks = MagicMock()
    worker.request_abort_fn = MagicMock()
    worker.abort_requested_fn = lambda: False
    worker.retract_z_between_regions = False
    worker._alignment_widget = None
    worker._last_move_region_id = None
    worker.prepare_z_stack = MagicMock(side_effect=AssertionError("validation must not stack"))
    worker.acquire_camera_image = MagicMock(side_effect=AssertionError("validation must not image"))
    worker._run_nidaq_stimulus = MagicMock(side_effect=AssertionError("validation must not stimulate"))
    return worker, controller, stage


def test_validation_walk_uses_real_af_cadence_without_images_stacks_or_stimuli(tmp_path):
    worker, controller, stage = make_worker(tmp_path)
    worker._backpressure = MagicMock()
    worker._prewarm_observation_states = MagicMock(side_effect=AssertionError("no camera prewarm"))
    worker._prewarm_postprocess_routines = MagicMock(side_effect=AssertionError("no postprocessing prewarm"))
    worker._get_region_plan = MagicMock(side_effect=AssertionError("validation needs no imaging plan"))
    worker._summarize_runner_outputs = lambda: SimpleNamespace(none_failed=True)
    worker._first_fov_pre_moved = False
    worker._upload_target = None
    worker.run_coordinate_acquisition(str(tmp_path))
    events = rows(tmp_path)
    assert [e["kind"] for e in events] == ["correction", "table", "measurement"]
    assert [e["af_status"] for e in events] == ["ok", "table", "table"]
    assert worker._laser_af_successes == 1
    assert worker._timepoint_fov_count == 3
    assert worker.total_scans == 3
    assert [call.args[0].current_fov for call in worker.callbacks.signal_region_progress.call_args_list] == [1, 2, 3]
    worker.prepare_z_stack.assert_not_called()
    worker.acquire_camera_image.assert_not_called()
    worker._run_nidaq_stimulus.assert_not_called()
    worker.request_abort_fn.assert_not_called()


def test_single_fov_success_avoids_duplicate_measurement(tmp_path):
    worker, _, _ = make_worker(tmp_path)
    worker.scan_region_fov_coords_mm = {"R0": [(0, 0)]}
    worker.acquire_at_position("R0", str(tmp_path), 0)
    assert [e["kind"] for e in rows(tmp_path)] == ["correction"]


def test_stale_fallback_is_recorded_as_failed_correction(tmp_path):
    worker, controller, _ = make_worker(tmp_path)
    controller.laser_af_properties = controller.laser_af_properties.model_copy(update={"has_reference": False})
    worker.acquire_at_position("R0", str(tmp_path), 0)
    event = rows(tmp_path)[0]
    assert event["af_status"] == "stale"
    assert event["af_success"] == "False" and event["failure_reason"] == "no_reference"
    assert worker._fovs_since_refresh["R0"] == 10


def test_worker_records_restore_failure_then_aborts(tmp_path):
    worker, controller, _ = make_worker(tmp_path)
    controller.camera.fail_restore = True
    with pytest.raises(AFValidationRestoreError):
        worker.acquire_at_position("R0", str(tmp_path), 0)
    assert "restore" in rows(tmp_path)[0]["snapshot_error"]
    worker.acquire_camera_image.assert_not_called()


def test_preflight_requires_laser_af():
    controller = MultiPointController.__new__(MultiPointController)
    controller.validation_mode = True
    controller.do_reflection_af = False
    controller._log = MagicMock()
    assert not controller.validate_acquisition_settings()


@pytest.mark.parametrize("enabled,with_imaging", [(True, False), (True, True), (False, False)])
def test_validation_yaml_roundtrip(tmp_path, monkeypatch, enabled, with_imaging):
    from control.acquisition_yaml_loader import parse_acquisition_yaml
    from control.core.multi_point_controller import _save_unified_multipoint_acquisition_yaml
    from tests.control.test_multipoint_z_retract import _params

    monkeypatch.setattr(
        "control.core.acquisition_metadata_helpers.augment_multipoint_acquisition_yaml_dict",
        lambda base_yaml, **kwargs: {"schema_version": 2, **base_yaml},
    )
    _save_unified_multipoint_acquisition_yaml(
        _params(validation_mode=enabled, validation_with_imaging=with_imaging,
                skip_saving=enabled and not with_imaging), str(tmp_path),
        widget_type="flexible", repo=MagicMock(), live_controller=MagicMock(),
        camera=MagicMock(), objective_store=MagicMock(), recording_start_time=0.0,
        selected_observation_state_names=[], use_manual_focus_map=False, logger=MagicMock(),
    )
    restored = parse_acquisition_yaml(str(tmp_path / "acquisition.yaml"))
    assert restored.validation_mode is enabled
    assert restored.validation_with_imaging is with_imaging
    assert restored.skip_saving is (enabled and not with_imaging)


def test_fatal_af_error_records_evidence_without_another_camera_call(tmp_path):
    worker, controller, _ = make_worker(tmp_path)
    controller.get_new_frame = MagicMock(side_effect=KeyboardInterrupt("SDK stopped"))
    controller.capture_validation_frame = MagicMock(return_value=np.ones((8, 8), dtype=np.uint8))
    with pytest.raises(KeyboardInterrupt):
        worker.acquire_at_position("R0", str(tmp_path), 0)
    assert controller.capture_validation_frame.call_count == 2  # before only; none after fatal AF
    event = rows(tmp_path)[0]
    assert event["af_success"] == "False"
    assert "KeyboardInterrupt" in event["failure_reason"]
    assert "skipped after fatal" in event["snapshot_error"]


def test_fatal_snapshot_error_preserves_completed_af_result(tmp_path):
    worker, controller, _ = make_worker(tmp_path)
    controller.capture_validation_frame = MagicMock(side_effect=[
        np.ones((8, 8), dtype=np.uint8), np.ones((8, 8), dtype=np.uint8), KeyboardInterrupt("SDK stopped"),
    ])
    with pytest.raises(KeyboardInterrupt):
        worker.acquire_at_position("R0", str(tmp_path), 0)
    event = rows(tmp_path)[0]
    assert event["af_success"] == "True"
    assert "KeyboardInterrupt" in event["boundary_error"]


def test_save_all_retains_each_success_and_table_metadata(tmp_path):
    collector = AFValidationCollector(tmp_path, save_all=True)
    for fov in range(3):
        collector.record({**metadata(), "fov": fov}, operation(), np.eye(12, dtype=np.uint8))
    collector.record(metadata(status="table"))
    for event in rows(tmp_path):
        folder = tmp_path / event["artifacts_path"]
        assert (folder / "current.yaml").exists()
        if event["af_attempted"] == "True":
            assert (folder / "current_native.tiff").exists()
            assert (folder / "current_full_sensor.tiff").exists()


def test_validation_with_imaging_captures_channels_and_stack_and_saves_every_af_frame(tmp_path):
    from control._def import FILE_ID_PADDING
    from control.models.acquisition_cycle import RegionPlan, _index_events

    worker, controller, stage = make_worker(tmp_path)
    worker.validation_with_imaging = True
    worker._af_validator = AFValidationCollector(tmp_path, save_all=True)
    worker._reference_z_level = lambda: 1
    worker.prepare_z_stack = MagicMock()
    worker.move_z_for_stack = MagicMock()
    worker.move_z_back_after_stack = MagicMock()
    worker.acquire_camera_image = MagicMock()
    worker._apply_observation_state = lambda name: SimpleNamespace(name=name, is_stimulus_only=False)
    worker._build_save_layout = lambda *args: SimpleNamespace(c_index=0)
    plan = RegionPlan.from_events(_index_events([("state", ("BF", True)), ("state", ("GFP", True))]))
    worker._get_region_plan = lambda region: plan
    worker.total_scans = 6
    stage.z_um = 20
    worker.acquire_at_position("R0", str(tmp_path), 0)

    assert worker.acquire_camera_image.call_count == 6
    assert [c.args[3] for c in worker.acquire_camera_image.call_args_list] == [0, 0, 1, 1, 2, 2]
    worker.prepare_z_stack.assert_called_once()
    assert worker.move_z_for_stack.call_count == 2
    worker.move_z_back_after_stack.assert_called_once()
    event = rows(tmp_path)[0]
    assert event["af_success"] == "True" and event["imaging_enabled"] == "True"
    prefix = f"R0_{0:0{FILE_ID_PADDING}}_"
    assert event["image_file_prefix"] == prefix
    assert all(c.args[1].startswith(prefix) for c in worker.acquire_camera_image.call_args_list)
    folder = tmp_path / event["artifacts_path"]
    assert int(event["native_frame_count"]) >= 3
    assert len(list(folder.glob("native_*.tiff"))) == int(event["native_frame_count"])
    assert (folder / "current_full_sensor.tiff").exists()
    assert controller._validation_save_all is False
    worker.request_abort_fn.assert_not_called()


def test_validation_with_imaging_rejects_dry_run():
    controller = MultiPointController.__new__(MultiPointController)
    controller.set_validation_mode(True, with_imaging=True)
    controller.do_reflection_af = True
    controller.laserAutoFocusController = MagicMock()
    controller.skip_saving = True
    controller._log = MagicMock()
    assert not controller.validate_acquisition_settings()


@pytest.mark.parametrize("imaging", [False, True])
def test_build_params_retains_imaging_plan_and_save_settings_only_in_imaging_mode(imaging):
    from tests.control.test_multipoint_z_retract import _params
    from control.models.acquisition_cycle import RegionPlan, _index_events

    params = _params(selected_observation_state_names=["BF"])
    controller = MultiPointController.__new__(MultiPointController)
    for key, value in vars(params).items():
        setattr(controller, key, value)
    controller.scanCoordinates = SimpleNamespace(format=None)
    controller.timestamp_acquisition_started = 0.0
    controller.do_reflection_af = True
    controller.set_validation_mode(True, with_imaging=imaging)
    controller.contrast_af_override = None
    controller.contrast_supervision_policy = MagicMock(mode="off")
    controller.autofocusController = SimpleNamespace(
        deltaZ=0.001, N=5, crop_width=256, crop_height=256, use_focus_map=False, focus_map_coords=[],
    )
    controller.zarr_upload_enabled = True
    plan = RegionPlan.from_events(_index_events([("state", ("BF", True))]))
    controller._build_region_plans = MagicMock(return_value=(plan, {}))
    controller._estimated_disk_bytes_or_zero = lambda: 1234

    built = controller.build_params(params.scan_position_information)
    assert built.skip_saving is (not imaging)
    assert built.validation_with_imaging is imaging
    assert built.zarr_upload_enabled is imaging
    assert built.global_region_plan is (plan if imaging else None)
    assert built.estimated_total_disk_bytes == (1234 if imaging else 0)
