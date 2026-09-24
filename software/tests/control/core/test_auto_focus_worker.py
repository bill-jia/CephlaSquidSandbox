"""Autofocus receives frames through the camera protocol without leaking them to acquisition."""
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

import control._def as definitions
from control.core.auto_focus_worker import AutofocusWorker
from control.models.observation_state import IlluminatorState, ObservationState
from squid.abc import AbstractCamera


class Camera:
    capture_frame = AbstractCamera.capture_frame
    _propogate_frame = AbstractCamera._propogate_frame

    def __init__(self):
        self._capture_frame_lock = threading.Lock()
        self._capture_frame_callback = None
        self._frame_callbacks_enabled = False
        self.downstream = MagicMock()
        self._frame_callbacks = [(1, self.downstream)]
        self.get_is_streaming = MagicMock(return_value=False)
        self.start_streaming = MagicMock()
        self.stop_streaming = MagicMock()
        self.get_ready_for_trigger = MagicMock(return_value=True)
        self.get_exposure_time = MagicMock(return_value=10.0)
        self.get_total_frame_time = MagicMock(return_value=10.0)
        self.send_trigger = MagicMock(side_effect=self.deliver)
        self.frame_id = 0

    def deliver(self, **kwargs):
        self.frame_id += 1
        self._propogate_frame(SimpleNamespace(frame=np.full((32, 32), self.frame_id, dtype=np.uint8)))


def make_worker(mode=definitions.TriggerMode.SOFTWARE):
    camera = Camera()
    state = ObservationState(illuminator_states=[IlluminatorState(illumination_channel="LED", on=True)])
    obs = SimpleNamespace(current_observation_state=state, ic=MagicMock(), microscope=MagicMock())
    controller = SimpleNamespace(
        camera=camera, stage=MagicMock(), microcontroller=MagicMock(), nl5=MagicMock(),
        liveController=SimpleNamespace(obs_controller=obs, trigger_mode=mode),
        N=3, deltaZ=0.001, crop_width=32, crop_height=32,
        focus_measure_operator=definitions.FocusMeasureOperator.LAPE,
    )
    running = threading.Event()
    running.set()
    return AutofocusWorker(controller, MagicMock(), MagicMock(), running)


@pytest.mark.parametrize("mode", [definitions.TriggerMode.SOFTWARE, definitions.TriggerMode.HARDWARE])
def test_immediate_frames_are_received_and_routed_via_camera(mode, monkeypatch):
    worker = make_worker(mode)
    measure = MagicMock(side_effect=[1.0, 3.0, 2.0])
    monkeypatch.setattr("control.core.auto_focus_worker.utils.calculate_focus_measure", measure)
    worker.run()
    assert [call.args[0][0, 0] for call in measure.call_args_list] == [1, 2, 3]
    assert worker.camera.send_trigger.call_count == 3
    worker.camera.send_trigger.assert_called_with(illumination_time=10.0)
    worker.microcontroller.send_hardware_trigger.assert_not_called()
    worker.nl5.start_acquisition.assert_not_called()
    worker.camera.downstream.assert_not_called()
    worker.camera.start_streaming.assert_called_once()
    worker.camera.stop_streaming.assert_called_once()
    worker.liveController.obs_controller.ic.apply_observation_illumination.assert_called_with(
        worker.liveController.obs_controller.current_observation_state.active_illuminator_states,
        turn_on=True, force_hardware=True,
    )
    assert worker.liveController.obs_controller.ic.turn_off_all.call_count == 3
    assert worker.stage.move_z.call_args.args == (0.002,)
    assert worker.autofocusController.last_result.status == "success"
    assert worker.autofocusController.last_result.frames == 3
    worker._finished_fn.assert_called_once()


def test_owned_immediate_frame_cannot_leak_when_callbacks_reenabled():
    camera = Camera()
    camera._frame_callbacks_enabled = True
    frame = camera.capture_frame(camera.deliver, 0.1)
    camera._frame_callbacks_enabled = True
    assert frame[0, 0] == 1
    camera.downstream.assert_not_called()
    camera.deliver()
    camera.downstream.assert_called_once()


def test_capture_cancellation_does_not_retrigger():
    camera = Camera()
    running = threading.Event()
    running.set()
    trigger = MagicMock()
    timer = threading.Timer(0.01, running.clear)
    timer.start()
    try:
        with pytest.raises(InterruptedError):
            camera.capture_frame(trigger, 1, cancelled=lambda: not running.is_set())
    finally:
        timer.join()
    trigger.assert_called_once()


def test_capture_cancelled_before_trigger_releases_owner():
    camera = Camera()
    with pytest.raises(InterruptedError, match="cancelled"):
        camera.capture_frame(camera.deliver, 0.1, cancelled=lambda: True)
    assert camera.frame_id == 0
    assert camera.capture_frame(camera.deliver, 0.1)[0, 0] == 1


def test_capture_cancelled_during_immediate_delivery_releases_owner():
    camera = Camera()
    cancelled = threading.Event()

    def deliver_and_cancel():
        camera.deliver()
        cancelled.set()

    with pytest.raises(InterruptedError, match="cancelled"):
        camera.capture_frame(deliver_and_cancel, 0.1, cancelled=cancelled.is_set)
    assert camera.frame_id == 1
    assert camera.capture_frame(camera.deliver, 0.1)[0, 0] == 2


def test_capture_rejects_second_owner():
    camera = Camera()
    armed = threading.Event()
    release = threading.Event()
    received = []

    def first_trigger():
        armed.set()
        assert release.wait(1)
        camera.deliver()

    thread = threading.Thread(target=lambda: received.append(camera.capture_frame(first_trigger, 1)))
    thread.start()
    assert armed.wait(1)
    with pytest.raises(RuntimeError, match="owns frame delivery"):
        camera.capture_frame(camera.deliver, 0.1)
    release.set()
    thread.join(1)
    assert len(received) == 1


def test_capture_waits_for_delayed_frame():
    camera = Camera()
    timer = threading.Timer(0.01, camera.deliver)
    try:
        result = camera.capture_frame(timer.start, 1.0)
        assert result[0, 0] == 1
        assert camera._capture_frame_callback is None
    finally:
        timer.join()


def test_timeout_restores_stage_stream_and_illumination(monkeypatch):
    worker = make_worker()
    # Exercise the real receiver timeout without waiting the production allowance.
    worker.camera.send_trigger.side_effect = None
    capture = worker.camera.capture_frame
    worker.camera.capture_frame = lambda trigger, timeout: capture(trigger, 0.01)
    with pytest.raises(TimeoutError, match="camera frame"):
        worker.run()
    assert sum(call.args[0] for call in worker.stage.move_z.call_args_list) == pytest.approx(0)
    assert worker.camera._capture_frame_callback is None
    worker.camera.stop_streaming.assert_called_once()
    worker.liveController.obs_controller.ic.turn_off_all.assert_called_with(preserve_logical_state=True)
    worker._finished_fn.assert_called_once()
    worker._image_to_display_fn.assert_not_called()


def test_trigger_error_cleans_up_and_preserves_existing_stream():
    worker = make_worker()
    worker.camera.get_is_streaming.return_value = True
    worker.camera.send_trigger.side_effect = RuntimeError("trigger failed")
    with pytest.raises(RuntimeError, match="trigger failed"):
        worker.run()
    assert worker.camera._capture_frame_callback is None
    worker.camera.stop_streaming.assert_not_called()
    worker.liveController.obs_controller.ic.turn_off_all.assert_called_once_with(preserve_logical_state=True)
    assert sum(call.args[0] for call in worker.stage.move_z.call_args_list) == pytest.approx(0)


def test_continuous_capture_waits_without_trigger(monkeypatch):
    worker = make_worker(definitions.TriggerMode.CONTINUOUS)
    def capture(trigger, timeout):
        assert trigger is None
        timer = threading.Timer(0.01, worker.camera.deliver)
        try:
            return Camera.capture_frame(worker.camera, timer.start, timeout)
        finally:
            timer.join()
    worker.camera.capture_frame = capture
    monkeypatch.setattr("control.core.auto_focus_worker.utils.calculate_focus_measure", lambda *args: 1.0)
    worker.run()
    worker.camera.send_trigger.assert_not_called()
    assert worker._image_to_display_fn.call_count == 3


def test_cancel_before_first_plane_returns_to_start():
    worker = make_worker()
    worker._keep_running.clear()
    worker.run()
    worker.camera.send_trigger.assert_not_called()
    assert sum(call.args[0] for call in worker.stage.move_z.call_args_list) == pytest.approx(0)


def test_waveform_armed_before_trigger_and_cleaned_after_frame(monkeypatch):
    worker = make_worker()
    worker.liveController.obs_controller.current_observation_state = SimpleNamespace(is_waveform_driven=True)
    cleanup = MagicMock()
    arm = MagicMock(return_value=cleanup)
    apply = MagicMock()
    monkeypatch.setattr("control.core.waveform_capture.arm_nidaq_pulse_for_capture", arm)
    monkeypatch.setattr("control.core.waveform_capture.apply_illumination_for_waveform_capture", apply)
    def trigger(**kwargs):
        arm.assert_called_once()
        cleanup.assert_not_called()
        worker.camera.deliver()
    worker.camera.send_trigger.side_effect = trigger
    assert worker._acquire_frame()[0, 0] == 1
    cleanup.assert_called_once()
    worker.liveController.obs_controller.ic.turn_off_all.assert_called_once_with(preserve_logical_state=True)
