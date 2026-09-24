"""A completed acquisition must not consume the next run's or live view's frames."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from control.camera_tucsen import TucsenCamera
from control._sdk_watchdog import CameraTimeoutError
from tests.control.test_multipoint_af_frame_owner import _worker


class Camera:
    add_frame_arrived_callback = TucsenCamera.add_frame_arrived_callback
    remove_frame_arrived_callback = TucsenCamera.remove_frame_arrived_callback
    _fire_frame_arrived_callbacks = TucsenCamera._fire_frame_arrived_callbacks

    def __init__(self):
        self._frame_arrived_callbacks = []
        self.decoded = {}
        self._log = MagicMock()
        self.stop_streaming = MagicMock()
        self.start_streaming = MagicMock()

    def add_frame_callback(self, callback):
        token = len(self.decoded)  # first token is zero: still requires cleanup
        self.decoded[token] = callback
        return token

    def remove_frame_callback(self, token):
        self.decoded.pop(token, None)


def worker_for_run(camera):
    worker = _worker()
    worker.camera = camera
    worker._af_session_active = False
    worker._current_capture_info = None
    worker.validation_mode = False
    worker.liveController = SimpleNamespace(obs_controller=SimpleNamespace(_timing=None))
    worker.laser_auto_focus_controller = None
    worker.scan_region_fov_coords_mm = {"R0": [(0, 0)]}
    worker._timing = MagicMock()
    worker._seed_camera_for_first_observation_state = MagicMock()
    worker.dt = worker.time_point = worker.Nt = 0
    worker._slack_notifier = None
    worker.callbacks = MagicMock()
    worker.do_reflection_af = False
    worker._wait_for_outstanding_callback_images = MagicMock()
    worker._finish_jobs = MagicMock()
    worker._finalize_af_validation = MagicMock()
    return worker


@pytest.mark.parametrize("stop_error", [None, RuntimeError("stop failed"), CameraTimeoutError("SDK wedged")])
def test_run_finally_removes_both_callbacks_even_when_stop_fails(stop_error):
    camera = Camera()
    if stop_error:
        camera.stop_streaming.side_effect = [None, stop_error]
    worker = worker_for_run(camera)
    worker.run()
    assert camera.decoded == {}
    assert camera._frame_arrived_callbacks == []
    camera._fire_frame_arrived_callbacks(10)  # live view after the run
    worker.request_abort_fn.assert_not_called()
    worker._finish_jobs.assert_called_once()
    worker._finalize_af_validation.assert_called_once()


@pytest.mark.parametrize("aborted", [False, True])
def test_two_runs_on_same_camera_do_not_leave_workers_listening_to_live_view(aborted):
    camera = Camera()
    for _ in range(2):
        worker = worker_for_run(camera)
        if aborted:
            worker.Nt = 1
            worker.abort_requested_fn = lambda: True
        worker.run()
        camera._fire_frame_arrived_callbacks(20)
        worker.request_abort_fn.assert_not_called()
        assert camera._frame_arrived_callbacks == []
        assert camera.decoded == {}


def test_only_active_worker_gets_second_runs_capture_info():
    camera = Camera()
    first = worker_for_run(camera)
    token = first._register_acquisition_frame_callbacks()
    late_raw = camera._frame_arrived_callbacks[0]
    late_decoded = camera.decoded[token]
    first._detach_acquisition_frame_callbacks(token)
    second = worker_for_run(camera)
    token2 = second._register_acquisition_frame_callbacks()
    expected = SimpleNamespace(file_id="second_run")
    second._current_capture_info = expected
    camera._fire_frame_arrived_callbacks(21)
    assert second._pending_capture_info_by_frame_id[21] is expected
    assert first._pending_capture_info_by_frame_id == {}
    late_raw(22)  # already-snapshotted callbacks from the old delivery thread
    late_decoded(SimpleNamespace(frame_id=22))
    first.request_abort_fn.assert_not_called()
    second.request_abort_fn.assert_not_called()
    second._detach_acquisition_frame_callbacks(token2)


def test_registry_removal_preserves_other_listeners_and_is_idempotent():
    camera = Camera()
    one, two = MagicMock(), MagicMock()
    camera.add_frame_arrived_callback(one)
    camera.add_frame_arrived_callback(two)
    camera.add_frame_arrived_callback(two)
    camera.remove_frame_arrived_callback(one)
    camera.remove_frame_arrived_callback(one)
    camera._fire_frame_arrived_callbacks(30)
    one.assert_not_called()
    two.assert_called_once_with(30)


def test_partial_registration_is_unwound():
    camera = Camera()
    worker = worker_for_run(camera)
    register = camera.add_frame_arrived_callback

    def fail_after_register(callback):
        register(callback)
        raise RuntimeError("registration failed")

    camera.add_frame_arrived_callback = fail_after_register
    with pytest.raises(RuntimeError):
        worker._register_acquisition_frame_callbacks()
    assert camera.decoded == {}
    assert camera._frame_arrived_callbacks == []
