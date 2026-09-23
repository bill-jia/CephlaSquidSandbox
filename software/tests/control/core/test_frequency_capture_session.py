from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from control._def import TriggerMode
from control.core.contrast_autofocus.capture import StageCaptureSession
from control.core.contrast_autofocus.search import CaptureFailure
from control.models.observation_state import ObservationState


def make_session():
    streaming = [True]
    callbacks = [True]
    camera = MagicMock()
    camera.get_is_streaming.side_effect = lambda: streaming[0]
    camera.stop_streaming.side_effect = lambda: streaming.__setitem__(0, False)
    camera.start_streaming.side_effect = lambda: streaming.__setitem__(0, True)
    camera.get_callbacks_enabled.side_effect = lambda: callbacks[0]
    camera.enable_callbacks.side_effect = lambda value: callbacks.__setitem__(0, value)
    camera.get_ready_for_trigger.return_value = True
    camera.get_total_frame_time.return_value = 1
    camera.get_exposure_time.return_value = 1
    camera.get_analog_gain.return_value = 0
    camera.get_binning.return_value = (1, 1)
    camera.get_region_of_interest.return_value = (0, 0, 4, 4)
    camera.get_pixel_format.return_value = "MONO8"
    camera.get_camera_mode.return_value = "normal"

    def capture(trigger, timeout, cancelled=None):
        trigger()
        return np.arange(16, dtype=np.uint8).reshape(4, 4)

    camera.capture_frame.side_effect = capture
    pos = [0.02]
    stage = MagicMock()
    stage.move_z_to.side_effect = lambda z: pos.__setitem__(0, z)
    stage.get_pos.side_effect = lambda: SimpleNamespace(z_mm=pos[0])
    live = SimpleNamespace(is_live=True, trigger_mode=TriggerMode.CONTINUOUS)
    live.stop_live = MagicMock(side_effect=lambda: setattr(live, "is_live", False))
    live.start_live = MagicMock(side_effect=lambda: setattr(live, "is_live", True))
    live.set_trigger_mode = MagicMock(side_effect=lambda mode: setattr(live, "trigger_mode", mode))
    live.obs_controller = SimpleNamespace(ic=MagicMock(), microscope=MagicMock())
    controller = SimpleNamespace(camera=camera, stage=stage, liveController=live,
                                 crop_width=4, crop_height=4, _log=MagicMock())
    return StageCaptureSession(controller, ObservationState(), 0, 40, lambda: False), camera, live, pos, callbacks


def test_session_trigger_and_restore_live_stream_callbacks():
    session, camera, live, pos, callbacks = make_session()
    with session:
        assert live.trigger_mode == TriggerMode.SOFTWARE
        assert callbacks == [False]
        frame = session.capture_at(25)
        assert frame.actual_z_um == 25
        assert frame.image.shape == (4, 4)
        camera.send_trigger.assert_called_once_with(illumination_time=1)
    assert live.trigger_mode == TriggerMode.CONTINUOUS
    assert live.is_live
    assert callbacks == [True]
    assert camera.stop_streaming.call_count == 2
    assert camera.start_streaming.call_count == 1  # session; mock live has no camera implementation


def test_session_restores_after_trigger_failure():
    session, camera, live, _, callbacks = make_session()
    camera.capture_frame.side_effect = TimeoutError("missing frame")
    with pytest.raises(CaptureFailure, match="missing frame"):
        with session:
            session.capture_at(25)
    assert live.trigger_mode == TriggerMode.CONTINUOUS
    assert live.is_live
    assert callbacks == [True]


def test_capture_keeps_primary_error_when_illumination_cleanup_fails():
    session, camera, _, _, _ = make_session()
    camera.capture_frame.side_effect = TimeoutError("missing frame")
    session.live.obs_controller.ic.turn_off_all.side_effect = RuntimeError("lamp cleanup failed")
    with pytest.raises(CaptureFailure, match="missing frame"):
        with session:
            session.capture_at(25)
    assert "lamp cleanup failed" in session.cleanup_errors


def test_rollback_move_is_allowed_after_cancel():
    session, _, _, pos, _ = make_session()
    session.cancelled = lambda: True
    with pytest.raises(InterruptedError):
        session.move_to(20)
    assert session.restore_to(20) == 20
    assert pos == [0.02]
