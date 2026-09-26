from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from control.core.config.repository import ConfigRepository
from control.core.laser_auto_focus_controller import LaserAutofocusController
from control.models.laser_af_config import LaserAFConfig
from control.models.machine_config import LaserAFDeviceSettings
from tests.control.core.test_af_validation import controller_fixture


def controller_for_repo(repo):
    repo.get_laser_af_settings = lambda: LaserAFDeviceSettings(
        focus_camera_exposure_time_ms=1.7, focus_camera_analog_gain=8,
    )
    return LaserAutofocusController(
        camera=MagicMock(), stage=MagicMock(), microcontroller=MagicMock(), piezo=None,
        objectiveStore=SimpleNamespace(current_objective="20x"),
        liveController=SimpleNamespace(microscope=SimpleNamespace(config_repo=repo)),
    )


def test_gain_exposure_persist_across_controller_and_repository_restart(tmp_path):
    repo = ConfigRepository(tmp_path)
    repo.create_profile("one")
    repo.set_profile("one")
    config = LaserAFConfig(x_offset=104, x_reference=204, has_reference=True, calibration_timestamp="calibrated")
    config.set_reference_image(np.eye(10, dtype=np.uint8))
    repo.save_laser_af_config("one", "20x", config)
    controller = controller_for_repo(repo)
    assert controller.laser_af_properties.x_reference == 100
    controller.update_camera_settings(exposure_time_ms=2.5, analog_gain=12)
    assert controller.is_initialized
    controller.camera.set_exposure_time.assert_called_with(2.5)
    controller.camera.set_analog_gain.assert_called_with(12)

    reopened = ConfigRepository(tmp_path)
    reopened.set_profile("one")
    loaded = controller_for_repo(reopened)
    assert loaded.laser_af_properties.focus_camera_exposure_time_ms == 2.5
    assert loaded.laser_af_properties.focus_camera_analog_gain == 12
    assert loaded.laser_af_properties.x_reference == 100
    np.testing.assert_array_equal(loaded.reference_crop, np.eye(10, dtype=np.uint8))
    loaded.camera.set_analog_gain.assert_called_with(12)


def test_empty_profile_uses_machine_camera_defaults_and_clears_previous_reference(tmp_path):
    repo = ConfigRepository(tmp_path)
    repo.create_profile("one")
    repo.create_profile("two")
    repo.set_profile("one")
    controller = controller_for_repo(repo)
    controller.update_camera_settings(exposure_time_ms=3, analog_gain=15)
    controller.reference_crop = np.ones((8, 8))
    repo.set_profile("two")
    controller.on_settings_changed()
    assert controller.laser_af_properties.focus_camera_exposure_time_ms == 1.7
    assert controller.laser_af_properties.focus_camera_analog_gain == 8
    assert controller.reference_crop is None
    assert not controller.is_initialized
    controller.camera.set_exposure_time.assert_called_with(1.7)
    controller.camera.set_analog_gain.assert_called_with(8)
    assert repo.get_laser_af_config("20x") is None


@pytest.mark.parametrize("kwargs", [{"focus_camera_exposure_time_ms": 0}, {"focus_camera_analog_gain": -1}])
def test_machine_camera_defaults_reject_invalid_values(kwargs):
    with pytest.raises(ValueError):
        LaserAFDeviceSettings(**kwargs)


def test_popup_capture_with_live_off_starts_streaming_and_retries_dropped_trigger():
    from gui.widgets.hardware_panels import LaserAutofocusSettingWidget

    controller, _, camera = controller_fixture()
    camera.streaming = False
    camera.drop_next_triggers = 1
    live = SimpleNamespace(is_live=False, trigger_acquisition=MagicMock(return_value=False))
    holder = SimpleNamespace(laserAutofocusController=controller, liveController=live)
    frame = LaserAutofocusSettingWidget.illuminate_and_get_frame(holder)
    assert frame is not None
    assert not camera.streaming
    assert camera.roi == (100, 200, 1536, 256)
    assert camera.get_callbacks_enabled()
    live.trigger_acquisition.assert_not_called()
    controller.microcontroller.turn_off_AF_laser.assert_called()


def test_popup_camera_edits_reach_persisting_controller():
    from gui.widgets.hardware_panels import LaserAutofocusSettingWidget

    holder = SimpleNamespace(laserAutofocusController=MagicMock(), signal_newExposureTime=MagicMock(),
                             signal_newAnalogGain=MagicMock())
    LaserAutofocusSettingWidget.update_exposure_time(holder, 1.2)
    holder.laserAutofocusController.update_camera_settings.assert_called_with(exposure_time_ms=1.2)
    LaserAutofocusSettingWidget.update_analog_gain(holder, 13)
    holder.laserAutofocusController.update_camera_settings.assert_called_with(analog_gain=13)
