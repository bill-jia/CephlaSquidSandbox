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
from control.models.contrast_autofocus import ContrastAFSettings
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


def test_frequency_settings_roundtrip_and_state_switch(tmp_path):
    repo = _repo_with_profile(tmp_path)
    obs = _make_controller(config_repo=repo)
    first = ObservationState(
        name="first", illuminator_states=[IlluminatorState(illumination_channel="TestLaser")],
        contrast_af=ContrastAFSettings(method="frequency_assisted", coarse_step_um=12,
                                       medium_step_um=3, fine_step_um=1,
                                       window_below_um=30, window_above_um=40,
                                       energy_threshold=0.6, sensor_full_scale=4095))
    second = ObservationState(name="second", contrast_af=ContrastAFSettings(method="legacy"))
    obs.set_active_observation_state(first)
    obs.set_contrast_af(first.contrast_af.model_copy(update={"energy_threshold": 0.7}))
    assert obs.collect_observation_state().contrast_af.energy_threshold == 0.7
    assert obs.cache_current_state_to_disk()
    reopened = ConfigRepository(base_path=repo.base_path)
    reopened.set_profile("p1")
    assert reopened.get_observation_state().contrast_af.fine_step_um == 1
    assert reopened.get_observation_state().contrast_af.energy_threshold == 0.7
    repo.save_observation_preset("first", obs.collect_observation_state())
    loaded = repo.load_observation_preset("first")
    assert loaded.contrast_af.coarse_step_um == 12
    assert loaded.contrast_af.energy_threshold == 0.7
    assert observation_state_to_yaml(loaded)["contrast_af"]["sensor_full_scale"] == 4095
    obs.set_active_observation_state(second)
    assert obs.current_observation_state.contrast_af.method == "legacy"
    assert first.contrast_af.energy_threshold == 0.7
    assert repo.get_observation_state().contrast_af.energy_threshold == 0.7
    obs.set_active_observation_state(loaded)
    assert obs.current_observation_state.contrast_af.medium_step_um == 3


def test_frequency_settings_require_explicit_window():
    with pytest.raises(ValidationError):
        ContrastAFSettings(method="frequency_assisted", coarse_step_um=10)


def test_multipoint_snapshots_frequency_settings_from_selected_state():
    from control.core.multi_point_controller import MultiPointController

    state = ObservationState(contrast_af=ContrastAFSettings(
        method="frequency_assisted", coarse_step_um=10, medium_step_um=2,
        fine_step_um=1, window_below_um=20, window_above_um=20))
    fake = SimpleNamespace(
        contrast_af_state_name=state.name, contrast_af_override=None,
        liveController=SimpleNamespace(get_observation_state_by_name=MagicMock(return_value=state)))
    snapshot = MultiPointController._effective_contrast_af_state(fake)
    assert snapshot.contrast_af.method == "frequency_assisted"
    state.contrast_af.coarse_step_um = 99
    assert snapshot.contrast_af.coarse_step_um == 10


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


def test_focus_widget_completion_does_not_restart(qtbot, tmp_path):
    from gui.gui_hcs.qt_controllers import QtAutoFocusController
    from gui.widgets.hardware_panels import AutoFocusWidget

    obs = _make_controller(config_repo=_repo_with_profile(tmp_path))
    obs.set_active_observation_state(ObservationState(
        contrast_af=ContrastAFSettings(method="legacy")))
    af = QtAutoFocusController(
        camera=MagicMock(), stage=MagicMock(),
        liveController=SimpleNamespace(obs_controller=obs), microcontroller=MagicMock(), nl5=None)
    widget = AutoFocusWidget(af)
    qtbot.addWidget(widget)
    af.autofocus = MagicMock()
    af.cancel_autofocus = MagicMock()
    widget.btn_autofocus.setChecked(True)
    af.autofocus.assert_called_once_with(False)
    widget.autofocus_is_finished()
    assert not widget.btn_autofocus.isChecked()
    af.autofocus.assert_called_once()
    af.cancel_autofocus.assert_not_called()


def _valid_frequency_widget(qtbot, tmp_path):
    from gui.gui_hcs.qt_controllers import QtAutoFocusController
    from gui.widgets.hardware_panels import AutoFocusWidget

    repo = _repo_with_profile(tmp_path)
    obs = _make_controller(config_repo=repo)
    settings = ContrastAFSettings(
        method="frequency_assisted", coarse_step_um=10, medium_step_um=2,
        fine_step_um=1, window_below_um=20, window_above_um=20,
        sensor_full_scale=255)
    state = ObservationState(contrast_af=settings)
    obs.set_active_observation_state(state)
    axis = SimpleNamespace(
        MIN_POSITION=0, MAX_POSITION=0.04,
        raw_to_canonical=lambda z: z,
        convert_to_real_units=lambda steps: steps * 0.001)
    stage = MagicMock()
    stage.get_config.return_value = SimpleNamespace(Z_AXIS=axis)
    stage.get_pos.return_value = SimpleNamespace(z_mm=0.02)
    camera = MagicMock()
    af = QtAutoFocusController(
        camera=camera, stage=stage,
        liveController=SimpleNamespace(obs_controller=obs),
        microcontroller=MagicMock(), nl5=None)
    widget = AutoFocusWidget(af)
    qtbot.addWidget(widget)
    return widget, af, obs, repo, stage, camera


def test_tuning_popup_holds_controls_and_20x_defaults_start_frequency_scan(qtbot, tmp_path):
    from control.models.contrast_autofocus import default_20x_contrast_af_settings

    widget, af, obs, _, _, _ = _valid_frequency_widget(qtbot, tmp_path)
    obs.set_active_observation_state(ObservationState())
    widget.sync_from_observation_state()
    defaults = default_20x_contrast_af_settings()
    assert widget.tuning_dialog.windowTitle() == "Contrast AF Tuning"
    assert widget.af_method.parentWidget() is widget
    assert widget.entry_delta.parentWidget() is widget
    assert widget.entry_N.parentWidget() is widget
    assert widget.btn_autolevel.parentWidget() is widget
    assert widget.dropdown_focus_measure.parentWidget() is widget
    assert widget.tuning_dialog.isAncestorOf(widget.af_inputs["coarse_step_um"])
    assert widget.tuning_dialog.isAncestorOf(widget.af_fallback)
    assert widget.layout().count() == 6  # Three control rows, help text, run button, stretch.
    assert widget.af_method.currentData() == "frequency_assisted"
    assert widget.af_inputs["coarse_step_um"].value() == defaults.coarse_step_um
    assert widget.af_inputs["window_below_um"].value() == defaults.window_below_um
    assert widget.af_inputs["sensor_full_scale"].value() == defaults.sensor_full_scale
    af.autofocus = MagicMock()
    widget.btn_autofocus.setChecked(True)
    af.autofocus.assert_called_once_with(False)
    assert obs.current_observation_state.contrast_af == defaults


@pytest.mark.parametrize("field", ["fine_step_um", "max_time_s"])
def test_invalid_displayed_frequency_settings_cannot_start_stale_scan(qtbot, tmp_path, monkeypatch, field):
    widget, af, obs, _, stage, camera = _valid_frequency_widget(qtbot, tmp_path)
    assert obs.current_observation_state.contrast_af.fine_step_um == 1
    monkeypatch.setattr("gui.widgets.hardware_panels.QMessageBox.warning", MagicMock())
    af.autofocus = MagicMock()
    widget.af_inputs[field].setValue(0)
    assert "Configure AF" in widget.af_interval.text()
    widget.btn_autofocus.setChecked(True)
    assert not widget.btn_autofocus.isChecked()
    af.autofocus.assert_not_called()
    stage.move_z_to.assert_not_called()
    camera.send_trigger.assert_not_called()
    assert obs.current_observation_state.contrast_af.fine_step_um == 1


def test_widget_refresh_preserves_zero_settings_and_other_state_cache(qtbot, tmp_path):
    widget, _, obs, repo, _, _ = _valid_frequency_widget(qtbot, tmp_path)
    zero = obs.current_observation_state.model_copy(deep=True)
    zero.contrast_af = zero.contrast_af.model_copy(update={
        "energy_threshold": 0, "verification_tolerance": 0})
    repo.save_observation_state("p1", zero.model_copy(deep=True))
    obs.set_active_observation_state(zero)
    widget.sync_from_observation_state(zero)
    assert widget.af_inputs["energy_threshold"].value() == 0
    assert widget.af_inputs["verification_tolerance"].value() == 0
    assert zero.contrast_af.energy_threshold == 0
    assert zero.contrast_af.verification_tolerance == 0
    assert repo.get_observation_state().contrast_af.energy_threshold == 0
    assert repo.get_observation_state().contrast_af.verification_tolerance == 0

    nonzero = zero.model_copy(deep=True)
    nonzero.contrast_af = nonzero.contrast_af.model_copy(update={
        "energy_threshold": 0.7, "verification_tolerance": 0.3})
    obs.set_active_observation_state(nonzero)
    widget.sync_from_observation_state(nonzero)
    assert widget.af_inputs["energy_threshold"].value() == 0.7
    assert widget.af_inputs["verification_tolerance"].value() == 0.3
    assert repo.get_observation_state().contrast_af.energy_threshold == 0
    assert repo.get_observation_state().contrast_af.verification_tolerance == 0

    obs.set_active_observation_state(zero)
    widget.sync_from_observation_state(zero)
    assert widget.af_inputs["energy_threshold"].value() == 0
    assert widget.af_inputs["verification_tolerance"].value() == 0
