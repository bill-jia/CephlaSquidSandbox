"""Autofocus dispatch with the observation-state API and focus-map cadence."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from control.core.multi_point_worker import MultiPointWorker


def _worker(*, use_map=False, coordinates=(), count=0):
    worker = MultiPointWorker.__new__(MultiPointWorker)
    worker.do_reflection_af = False
    worker.do_autofocus = True
    worker.af_fov_count = count
    worker._wait_for_move_settled = MagicMock()
    worker._sleep = MagicMock()
    worker._log = MagicMock()
    worker.request_abort_fn = MagicMock()
    worker._run_owned_contrast_scan = MagicMock(return_value=SimpleNamespace(status="success", error=None))
    worker.stage = SimpleNamespace(wait_for_idle=MagicMock(), get_pos=MagicMock(
        return_value=SimpleNamespace(x_mm=0.5, y_mm=0.5)), move_z_to=MagicMock())
    worker.autofocusController = SimpleNamespace(
        use_focus_map=use_map,
        focus_map_coords=list(coordinates),
        autofocus=MagicMock(),
        wait_till_autofocus_has_completed=MagicMock(),
    )
    worker.liveController = SimpleNamespace(
        get_observation_state_by_name=MagicMock(return_value=SimpleNamespace(name="AF")),
        get_observation_states=MagicMock(return_value=[]),
    )
    worker._contrast_af_state = worker.liveController.get_observation_state_by_name.return_value
    return worker


def test_focus_map_runs_between_cadence_hits_without_selecting_a_channel():
    worker = _worker(use_map=True, coordinates=[(0, 0, 1), (1, 0, 1), (0, 1, 1)], count=1)

    assert worker.perform_autofocus("region", 1)

    worker.stage.move_z_to.assert_called_once_with(1.0)
    worker.liveController.get_observation_state_by_name.assert_not_called()
    worker._sleep.assert_called_once()
    assert worker._last_af_status == "map"


def test_incomplete_map_falls_back_to_sweep_using_observation_state():
    worker = _worker(use_map=True, coordinates=[(0, 0, 1)])

    assert worker.perform_autofocus("region", 0)

    worker._run_owned_contrast_scan.assert_called_once_with(worker._contrast_af_state)
    assert worker._last_af_status == "ok"


def test_missing_autofocus_observation_state_reports_configuration_error():
    worker = _worker()
    worker._contrast_af_state = None

    with pytest.raises(RuntimeError, match="is not a defined Observation State"):
        worker.perform_autofocus("region", 0)

    worker.autofocusController.autofocus.assert_not_called()


def test_cadence_skip_does_not_move_or_measure():
    worker = _worker(count=1)
    assert worker.perform_autofocus("region", 1)
    worker._run_owned_contrast_scan.assert_not_called()
    worker.stage.move_z_to.assert_not_called()
    assert worker._last_af_status == "skipped"


def test_scan_failure_is_not_reported_as_success():
    worker = _worker()
    worker._run_owned_contrast_scan.return_value = SimpleNamespace(status="capture_failed", error="missing frame")
    with pytest.raises(RuntimeError, match="capture_failed"):
        worker.perform_autofocus("region", 0)
    assert worker._last_af_status == "failed"


def test_permitted_optical_failure_continues_only_at_verified_nominal_z():
    worker = _worker()
    worker._contrast_af_failure_policy = "continue_restored"
    worker.abort_requested_fn = lambda: False
    worker.stage.get_pos.return_value.z_mm = 0.5
    worker.stage.get_config = lambda: SimpleNamespace(
        Z_AXIS=SimpleNamespace(convert_to_real_units=lambda step: 0.001))
    result = SimpleNamespace(status="no_texture", error="flat image",
                             cleanup_errors=[], final_z_um=500,
                             starting_z_um=500)
    worker._run_owned_contrast_scan.return_value = result
    assert worker.perform_autofocus("region", 0) is result
    assert worker._af_failed_for_fov is True
    assert worker._last_af_status == "failed"
    worker.request_abort_fn.assert_not_called()


def test_optical_failure_with_bad_rollback_stops_even_under_continue_policy():
    worker = _worker()
    worker._contrast_af_failure_policy = "continue_restored"
    worker.abort_requested_fn = lambda: False
    worker.stage.get_pos.return_value.z_mm = 0.505
    worker.stage.get_config = lambda: SimpleNamespace(
        Z_AXIS=SimpleNamespace(convert_to_real_units=lambda step: 0.001))
    worker._run_owned_contrast_scan.return_value = SimpleNamespace(
        status="no_texture", error="flat image", cleanup_errors=[],
        final_z_um=505, starting_z_um=500)
    with pytest.raises(RuntimeError, match="no_texture"):
        worker.perform_autofocus("region", 0)
    worker.request_abort_fn.assert_called_once()
