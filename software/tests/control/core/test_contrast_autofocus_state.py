"""Contrast AF measure follows the selected observation state and survives persistence."""

from types import SimpleNamespace
import threading
from unittest.mock import MagicMock

import numpy as np
import pytest
import yaml
from pydantic import ValidationError

import control._def as definitions
from control.core.auto_focus_controller import AutoFocusController
from control.core.auto_focus_worker import AutofocusWorker
from control.core.config.repository import ConfigRepository
from control.core.observation_state_service import observation_state_to_yaml
from control.models.observation_state import IlluminatorState, ObservationState
from tests.control.core.test_observation_state_confocal import _make_controller, _repo_with_profile


@pytest.mark.parametrize("measure", ["LAPE", "GLVA", "TENENGRAD"])
def test_measure_save_load_and_worker(tmp_path, monkeypatch, measure):
    repo = _repo_with_profile(tmp_path)
    obs = _make_controller(config_repo=repo)
    obs.bootstrap_state_from_hardware()
    af = AutoFocusController(
        camera=MagicMock(), stage=MagicMock(),
        liveController=SimpleNamespace(obs_controller=obs, trigger_mode=definitions.TriggerMode.SOFTWARE),
        microcontroller=MagicMock(), finished_fn=lambda: None,
        image_to_display_fn=lambda image: None, nl5=None,
    )
    af.set_focus_measure_operator(measure)
    assert obs.current_observation_state.focus_measure_operator == measure
    assert obs.cache_current_state_to_disk()
    general_path = repo.user_profiles_path / "p1" / "channel_configs" / "general.yaml"
    assert yaml.safe_load(general_path.read_text())["focus_measure_operator"] == measure
    reopened = ConfigRepository(base_path=repo.base_path)
    reopened.set_profile("p1")
    assert reopened.get_observation_state().focus_measure_operator == measure

    path = repo.save_observation_preset("contrast", obs.collect_observation_state())
    assert yaml.safe_load(path.read_text())["focus_measure_operator"] == measure
    obs.set_active_observation_state(ObservationState(focus_measure_operator="LAPE"))
    loaded = repo.load_observation_preset("contrast")
    obs.apply_full_observation_state(loaded)
    assert af.focus_measure_operator.value == measure
    # Collection must use the selected state even if general.yaml has another measure.
    repo.get_observation_state().focus_measure_operator = "LAPE"
    assert obs.collect_observation_state().focus_measure_operator == measure

    af.N = 3
    af.camera.capture_frame.return_value = np.ones((32, 32), dtype=np.uint8)
    af.camera.get_total_frame_time.return_value = 10.0
    obs.microscope.illumination_controller = MagicMock()
    af.microcontroller.is_busy.return_value = False
    keep_running = threading.Event()
    keep_running.set()
    worker = AutofocusWorker(af, lambda: None, lambda image: None, keep_running)
    # A running scan keeps one measure even if the selected state changes.
    obs.set_active_observation_state(ObservationState(focus_measure_operator="LAPE"))
    calculate = MagicMock(return_value=1.0)
    monkeypatch.setattr("control.core.auto_focus_worker.utils.calculate_focus_measure", calculate)
    worker.run_autofocus()
    assert calculate.call_count == 3
    assert all(call.args[1].value == measure for call in calculate.call_args_list)


def test_missing_measure_uses_machine_default(tmp_path, monkeypatch):
    monkeypatch.setattr(definitions, "FOCUS_MEASURE_OPERATOR", definitions.FocusMeasureOperator.GLVA)
    assert ObservationState().focus_measure_operator == "GLVA"
    repo = _repo_with_profile(tmp_path)
    data = observation_state_to_yaml(ObservationState(
        illuminator_states=[IlluminatorState(illumination_channel="TestLaser")]
    ))
    data.pop("focus_measure_operator")
    path = repo.user_profiles_path / "p1" / "observation_presets" / "old.yaml"
    path.write_text(yaml.safe_dump(data))
    assert repo.load_observation_preset("old").focus_measure_operator == "GLVA"


def test_invalid_measure_rejected():
    with pytest.raises(ValidationError):
        ObservationState(focus_measure_operator="unknown")


def test_focus_widget_selection_and_restore(qtbot, tmp_path):
    from gui.gui_hcs.qt_controllers import QtAutoFocusController
    from gui.widgets.hardware_panels import AutoFocusWidget

    repo = _repo_with_profile(tmp_path)
    obs = _make_controller(config_repo=repo)
    obs.set_active_observation_state(ObservationState(focus_measure_operator="GLVA"))
    af = QtAutoFocusController(
        camera=MagicMock(), stage=MagicMock(),
        liveController=SimpleNamespace(obs_controller=obs), microcontroller=MagicMock(), nl5=None,
    )
    widget = AutoFocusWidget(af)
    qtbot.addWidget(widget)
    assert widget.dropdown_focus_measure.currentText() == "GLVA"
    widget.dropdown_focus_measure.setCurrentText("TENENGRAD")
    assert af.focus_measure_operator == definitions.FocusMeasureOperator.TENENGRAD
    assert obs.current_observation_state.focus_measure_operator == "TENENGRAD"
    assert obs.cache_current_state_to_disk()
    loaded = ObservationState(focus_measure_operator="LAPE")
    obs.set_active_observation_state(loaded)
    widget.sync_from_observation_state(loaded)
    assert widget.dropdown_focus_measure.currentText() == "LAPE"
    # Refresh must not write to the previous state's general cache.
    assert repo.get_observation_state().focus_measure_operator == "TENENGRAD"
