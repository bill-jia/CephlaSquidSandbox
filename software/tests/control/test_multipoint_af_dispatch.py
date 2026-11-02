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


@pytest.mark.parametrize("first", [(1, 1, 2), (-1, 1, 2), (1, -1, 2), (-1, -1, 2)])
def test_legacy_entry_walk_uses_actual_first_tile_and_carries_focus(first):
    worker = MultiPointWorker.__new__(MultiPointWorker)
    worker.scan_region_names = ["r"]
    worker.scan_region_coords_mm = [(0, 0, 2)]
    worker.scan_region_fov_coords_mm = {"r": [first]}
    worker.abort_requested_fn = lambda: False
    worker._apply_region_laser_af_reference = MagicMock()
    worker.move_to_coordinate = MagicMock()
    worker.stage = SimpleNamespace(get_pos=lambda: SimpleNamespace(z_mm=2.1))
    worker.laser_auto_focus_controller = SimpleNamespace(move_to_target=MagicMock(return_value=True))
    worker._z_pos_proposal = {}
    worker._log = MagicMock()

    worker._initialize_legacy_region_entries()

    moves = worker.move_to_coordinate.call_args_list
    assert moves[0].args[0] == (0, 0, 2)
    assert moves[1].args[0] == (first[0] / 2, first[1] / 2, 2.1)
    assert moves[-1].args[0] == (first[0], first[1], 2.1)
    assert worker.laser_auto_focus_controller.move_to_target.call_count == 3
    assert worker._z_pos_proposal == {("r", 0): 2.1}

    worker._z_pos_proposal.clear()
    worker.laser_auto_focus_controller.move_to_target.return_value = False
    with pytest.raises(RuntimeError, match="verify center"):
        worker._initialize_legacy_region_entries()
    assert not worker._z_pos_proposal
