from types import SimpleNamespace
from unittest.mock import MagicMock

from control.models.observation_state import ObservationState
from control.models.contrast_autofocus import AcquisitionContrastAFOverride, ContrastAFSettings
from gui.widgets.laser_autofocus_settings import LaserAutofocusSettingsDialog


def _controller():
    states = [ObservationState(name="AF A"), ObservationState(name="AF B")]
    return SimpleNamespace(
        do_autofocus=False, do_reflection_af=False,
        contrast_af_state_name="AF A", contrast_af_override=None,
        laserAutoFocusController=None,
        liveController=SimpleNamespace(
            get_observation_states=lambda: states,
            get_observation_state_by_name=lambda name: next((s for s in states if s.name == name), None)),
        autofocusController=SimpleNamespace(deltaZ=0.001, N=10, crop_width=100, crop_height=100),
        set_contrast_af_acquisition_settings=MagicMock(), set_af_flag=MagicMock(),
        set_reflection_af_flag=MagicMock(), set_laser_af_refresh_every_n_fovs=MagicMock(),
        set_laser_af_seed_mode=MagicMock(), set_laser_af_consistency_threshold_um=MagicMock(),
        set_laser_af_check_last_fov_per_region=MagicMock(), set_laser_af_table_path_audit=MagicMock(),
    )


def test_cancel_discards_acquisition_draft(qtbot):
    controller = _controller()
    dialog = LaserAutofocusSettingsDialog(controller)
    qtbot.addWidget(dialog)
    dialog.af_state.setCurrentIndex(dialog.af_state.findText("AF B"))
    dialog.af_inputs["energy_threshold"].setValue(0)
    dialog.reject()
    controller.set_contrast_af_acquisition_settings.assert_not_called()
    controller.set_af_flag.assert_not_called()


def test_ok_commits_state_bound_override_and_zero(qtbot):
    controller = _controller()
    dialog = LaserAutofocusSettingsDialog(controller)
    qtbot.addWidget(dialog)
    dialog.af_state.setCurrentIndex(dialog.af_state.findText("AF B"))
    dialog.af_inputs["energy_threshold"].setValue(0)
    dialog.cb_contrast_enabled.setChecked(True)
    dialog.accept()
    name, override = controller.set_contrast_af_acquisition_settings.call_args.args
    assert name == override.state_name == "AF B"
    assert override.settings.energy_threshold == 0
    controller.set_af_flag.assert_called_once_with(True)


def test_switching_af_state_discards_other_states_override(qtbot):
    controller = _controller()
    controller.contrast_af_override = AcquisitionContrastAFOverride(
        state_name="AF A", settings=ContrastAFSettings(energy_threshold=0.2),
        metric="GLVA", legacy_step_um=1, legacy_count=5,
        crop_width=64, crop_height=64)
    dialog = LaserAutofocusSettingsDialog(controller)
    qtbot.addWidget(dialog)
    assert dialog.af_inputs["energy_threshold"].value() == 0.2
    dialog.af_state.setCurrentIndex(dialog.af_state.findText("AF B"))
    assert dialog.af_inputs["energy_threshold"].value() == 0.5
    assert dialog.af_metric.currentData() == "LAPE"


def test_laser_only_accepts_missing_contrast_state(qtbot):
    controller = _controller()
    controller.laserAutoFocusController = object()
    controller.liveController.get_observation_states = lambda: []
    controller.liveController.get_observation_state_by_name = lambda name: None
    dialog = LaserAutofocusSettingsDialog(controller)
    qtbot.addWidget(dialog)
    dialog.cb_enabled.setChecked(True)
    dialog.accept()
    controller.set_contrast_af_acquisition_settings.assert_not_called()
    controller.set_af_flag.assert_called_once_with(False)
    controller.set_reflection_af_flag.assert_called_once_with(True)


def test_disabled_invalid_contrast_draft_does_not_block_laser_or_commit(qtbot):
    controller = _controller()
    controller.laserAutoFocusController = object()
    dialog = LaserAutofocusSettingsDialog(controller)
    qtbot.addWidget(dialog)
    dialog.af_inputs["coarse_step_um"].setValue(1)
    dialog.af_inputs["medium_step_um"].setValue(10)
    dialog.af_method.setCurrentIndex(dialog.af_method.findData("frequency_assisted"))
    dialog.cb_enabled.setChecked(True)
    dialog.accept()
    controller.set_contrast_af_acquisition_settings.assert_not_called()
    controller.set_reflection_af_flag.assert_called_once_with(True)


def test_window_close_discards_laser_and_contrast_draft(qtbot):
    controller = _controller()
    dialog = LaserAutofocusSettingsDialog(controller)
    qtbot.addWidget(dialog)
    dialog.cb_contrast_enabled.setChecked(True)
    dialog.close()
    controller.set_contrast_af_acquisition_settings.assert_not_called()
    controller.set_af_flag.assert_not_called()
    controller.set_reflection_af_flag.assert_not_called()
