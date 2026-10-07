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
    worker._select_config = MagicMock()
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
    return worker


def test_focus_map_runs_between_cadence_hits_without_selecting_a_channel():
    worker = _worker(use_map=True, coordinates=[(0, 0, 1), (1, 0, 1), (0, 1, 1)], count=1)

    assert worker.perform_autofocus("region", 1)

    worker.autofocusController.autofocus.assert_called_once_with()
    worker.liveController.get_observation_state_by_name.assert_not_called()
    worker._sleep.assert_called_once()
    assert worker._last_af_status == "map"


def test_incomplete_map_falls_back_to_sweep_using_observation_state():
    worker = _worker(use_map=True, coordinates=[(0, 0, 1)])

    assert worker.perform_autofocus("region", 0)

    worker.liveController.get_observation_state_by_name.assert_called_once()
    worker._select_config.assert_called_once_with(worker.liveController.get_observation_state_by_name.return_value)
    worker.autofocusController.autofocus.assert_called_once_with(focus_map_override=True)
    assert worker._last_af_status == "ok"


def test_missing_autofocus_observation_state_reports_configuration_error():
    worker = _worker()
    worker.liveController.get_observation_state_by_name.return_value = None

    with pytest.raises(RuntimeError, match="is not a defined Observation State"):
        worker.perform_autofocus("region", 0)

    worker.autofocusController.autofocus.assert_not_called()
