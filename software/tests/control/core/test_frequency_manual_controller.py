from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from control._def import TriggerMode
from control.core.auto_focus_controller import AutoFocusController
from control.models.contrast_autofocus import ContrastAFSettings
from control.models.observation_state import CameraSettings, ObservationState


def test_manual_controller_runs_frequency_search_and_publishes_once():
    position = [0.02]
    streaming = [False]
    callbacks = [True]
    camera = MagicMock()
    camera.get_is_streaming.side_effect = lambda: streaming[0]
    camera.start_streaming.side_effect = lambda: streaming.__setitem__(0, True)
    camera.stop_streaming.side_effect = lambda: streaming.__setitem__(0, False)
    camera.get_callbacks_enabled.side_effect = lambda: callbacks[0]
    camera.enable_callbacks.side_effect = lambda value: callbacks.__setitem__(0, value)
    camera.get_exposure_time.return_value = 1
    camera.get_analog_gain.return_value = 0
    camera.get_binning.return_value = (1, 1)
    camera.get_region_of_interest.return_value = (0, 0, 16, 16)
    camera.get_pixel_format.return_value = "MONO8"
    camera.get_camera_mode.return_value = "normal"
    camera.get_total_frame_time.return_value = 1
    camera.get_ready_for_trigger.return_value = True
    camera.describe_trigger_routing.return_value = "native"
    checker = (np.indices((16, 16)).sum(axis=0) % 2).astype(np.uint8)

    def capture(trigger, timeout, cancelled=None):
        trigger()
        z = position[0] * 1000
        amplitude = round(max(1, 200 - 0.8 * (z - 20) ** 2))
        return 20 + checker * amplitude

    camera.capture_frame.side_effect = capture
    axis = SimpleNamespace(MIN_POSITION=0, MAX_POSITION=0.04,
                           raw_to_canonical=lambda raw: raw,
                           convert_to_real_units=lambda steps: steps * 0.001)
    stage = MagicMock()
    stage.get_config.return_value = SimpleNamespace(Z_AXIS=axis)
    stage.get_pos.side_effect = lambda: SimpleNamespace(z_mm=position[0])
    stage.move_z_to.side_effect = lambda z: position.__setitem__(0, z)
    state = ObservationState(
        focus_measure_operator="GLVA", camera_settings=CameraSettings(exposure_time_ms=1, gain_mode=0),
        contrast_af=ContrastAFSettings(method="frequency_assisted", coarse_step_um=10,
                                       medium_step_um=2, fine_step_um=1,
                                       window_below_um=20, window_above_um=20,
                                       sensor_full_scale=255))
    obs = SimpleNamespace(current_observation_state=state, ic=MagicMock(), microscope=MagicMock())
    live = SimpleNamespace(is_live=False, trigger_mode=TriggerMode.SOFTWARE, obs_controller=obs)
    live.stop_live = MagicMock()
    live.start_live = MagicMock()
    live.set_trigger_mode = MagicMock()
    finished = MagicMock()
    controller = AutoFocusController(camera, stage, live, MagicMock(), finished, lambda image: None, None)
    def completion_observes_result():
        assert controller.last_result is not None
        assert not controller.autofocus_in_progress
    finished.side_effect = completion_observes_result
    controller.autofocus()
    controller._completion.wait(5)
    assert controller.last_result.status == "success", controller.last_result.trace
    controller.wait_till_autofocus_has_completed(5)
    assert controller.last_result.status == "success"
    assert controller.last_result.accepted_z_um == 20
    assert position[0] == 0.02
    assert not controller.autofocus_in_progress
    finished.assert_called_once()
    assert callbacks == [True]
    assert streaming == [False]
    stage._BACKLASH_COMPENSATION_DISTANCE_MM = 0.005
    request = controller._resolve_frequency_request(state)
    assert request[3:5] == (5, 35)
    controller._focus_thread.join(1)
    before = stage.move_z_to.call_count
    controller.piezo_active = lambda: True
    with pytest.raises(RuntimeError, match="stage Z only"):
        controller.autofocus()
    assert stage.move_z_to.call_count == before
