"""Raw and deferred AF frames cannot consume imaging metadata."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from control.core.multi_point_worker import MultiPointWorker
from control.core.contrast_autofocus.search import FocusSample, SearchResult
from control.models.observation_state import ObservationState
import numpy as np
import pytest
from control.models.contrast_autofocus import ContrastAFSettings
from control._def import TriggerMode
from squid.abc import AbstractCamera
from squid.config import CameraPixelFormat


def _worker():
    worker = MultiPointWorker.__new__(MultiPointWorker)
    worker._af_owner_lock = threading.Lock()
    worker._af_session_active = True
    worker._af_expected_raw = 1
    worker._af_frame_ids = set()
    worker._current_capture_info = SimpleNamespace(file_id="imaging")
    worker._pending_capture_info_by_frame_id = {}
    worker._ready_for_next_trigger = threading.Event()
    worker._image_callback_idle = threading.Event()
    worker._image_callback_idle.set()
    worker._outstanding_lock = threading.Lock()
    worker._outstanding_frames = 0
    worker._capture_ts = {}
    worker._log = MagicMock()
    worker._emission_filter_wheel = None
    worker.wait_till_operation_is_completed = lambda: None
    worker.request_abort_fn = MagicMock()
    return worker


def test_raw_af_arrival_keeps_imaging_accounting_untouched():
    worker = _worker()
    worker._on_frame_arrived(42)
    assert worker._af_expected_raw == 0
    assert worker._af_frame_ids == {42}
    assert worker._current_capture_info.file_id == "imaging"
    assert worker._pending_capture_info_by_frame_id == {}
    assert not worker._ready_for_next_trigger.is_set()
    assert worker._image_callback_idle.is_set()


def test_late_decoded_af_frame_is_discarded_after_session_release():
    worker = _worker()
    worker._on_frame_arrived(42)
    worker._af_session_active = False
    worker._image_callback(SimpleNamespace(frame_id=42, frame=object()))
    assert worker._af_frame_ids == set()
    assert worker._current_capture_info.file_id == "imaging"
    assert worker._pending_capture_info_by_frame_id == {}
    worker.request_abort_fn.assert_not_called()


def test_untriggered_raw_frame_forces_abort():
    worker = _worker()
    worker._af_expected_raw = 0
    worker._on_frame_arrived(42)
    worker.request_abort_fn.assert_called_once()


def test_stale_raw_after_session_release_cannot_wake_imaging():
    worker = _worker()
    worker._af_session_active = False
    worker._current_capture_info = None
    worker._ready_for_next_trigger.clear()
    worker._on_frame_arrived(43)
    worker.request_abort_fn.assert_called_once()
    assert not worker._ready_for_next_trigger.is_set()
    assert worker._pending_capture_info_by_frame_id == {}


@pytest.mark.parametrize("method", ["legacy", "frequency_assisted"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_owned_scan_runs_synchronously_without_imaging_dispatch(monkeypatch, method, cleanup_fails):
    worker = _worker()
    worker._af_session_active = False
    worker._af_expected_raw = 0
    worker._current_capture_info = None
    worker._ready_for_next_trigger.set()
    worker.abort_requested_fn = lambda: False
    worker._use_deferred_decode_callback = True
    worker.camera = SimpleNamespace(get_total_frame_time=lambda: 1)
    axis = SimpleNamespace(MIN_POSITION=0, MAX_POSITION=1,
                           raw_to_canonical=lambda v: v,
                           convert_to_real_units=lambda v: 0.001)
    worker.stage = SimpleNamespace(
        get_pos=lambda: SimpleNamespace(z_mm=0.5),
        get_config=lambda: SimpleNamespace(Z_AXIS=axis))
    worker.autofocusController = SimpleNamespace(
        deltaZ=0.001, N=3, crop_width=16, crop_height=16,
        autofocus=MagicMock(),
        _resolve_frequency_request=lambda state: (
            state, state.contrast_af, 500, 490, 510, 1, 489, 511))
    worker._contrast_af_step_um = 1
    worker._contrast_af_count = 3
    worker._contrast_af_crop_width = 16
    worker._contrast_af_crop_height = 16
    worker._describe_camera_trigger_routing = lambda: "logical software / physical nidaq"

    class Session:
        cleanup_errors = ["restore failed"] if cleanup_fails else []
        capture_exposure_ms = 10

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def move_to(self, z):
            return z

        restore_to = move_to

        def capture_at(self, z):
            worker._on_frame_arrived(42)
            return FocusSample(np.ones((2, 2)), z)

    monkeypatch.setattr("control.core.contrast_autofocus.capture.StageCaptureSession", Session)

    def search(**kwargs):
        kwargs["capture_at"](500)
        return SearchResult("success", 500, final_z_um=500, accepted_z_um=500)

    monkeypatch.setattr("control.core.contrast_autofocus.service.run_contrast_search", search)
    state = ObservationState(name="AF", contrast_af=(
        ContrastAFSettings(method="frequency_assisted", coarse_step_um=10,
                           medium_step_um=2, fine_step_um=1,
                           window_below_um=20, window_above_um=20,
                           sensor_full_scale=65535)
        if method == "frequency_assisted" else ContrastAFSettings(method="legacy")))
    if cleanup_fails:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            worker._run_owned_contrast_scan(state)
        assert worker._last_af_result.status == "hardware_failed"
        assert worker._last_af_result.cleanup_errors == ["restore failed"]
        worker.request_abort_fn.assert_called_once()
        return
    result = worker._run_owned_contrast_scan(state)
    assert result.status == "success"
    assert worker._af_expected_raw == 0
    assert worker._pending_capture_info_by_frame_id == {}
    worker.autofocusController.autofocus.assert_not_called()
    worker.request_abort_fn.assert_not_called()
    monitor = worker._run_owned_contrast_scan(state, capture_only=True)
    assert isinstance(monitor, FocusSample)
    assert worker._af_expected_raw == 0
    assert worker._pending_capture_info_by_frame_id == {}
    worker.request_abort_fn.assert_not_called()


@pytest.mark.parametrize("deferred,decode_on_thread", [(True, False), (True, True), (False, False)])
def test_immediate_camera_capture_then_normal_imaging_callback(monkeypatch, deferred, decode_on_thread):
    worker = _worker()
    worker._af_session_active = False
    worker._af_expected_raw = 0
    worker._current_capture_info = None
    worker._ready_for_next_trigger.set()
    worker.abort_requested_fn = lambda: False
    worker._use_deferred_decode_callback = deferred
    delivered_imaging = []
    z_mm = [0.5]

    class Camera:
        capture_frame = AbstractCamera.capture_frame
        _propogate_frame = AbstractCamera._propogate_frame

        def __init__(self):
            self._capture_frame_lock = threading.Lock()
            self._capture_frame_callback = None
            self._capture_delivery_done = None
            self._frame_callbacks_enabled = True
            self._frame_callbacks = [(1, lambda frame: delivered_imaging.append(frame.frame_id))]
            self.streaming = True
            self.frame_id = 0
            self.decode_threads = []

        def get_is_streaming(self): return self.streaming
        def start_streaming(self): self.streaming = True
        def stop_streaming(self): self.streaming = False
        def get_callbacks_enabled(self): return self._frame_callbacks_enabled
        def enable_callbacks(self, enabled): self._frame_callbacks_enabled = enabled
        def get_exposure_time(self): return 1.0
        def set_exposure_time(self, value): pass
        def get_analog_gain(self): return 0.0
        def set_analog_gain(self, value): pass
        def get_binning(self): return (1, 1)
        def get_region_of_interest(self): return (0, 0, 16, 16)
        def get_pixel_format(self): return CameraPixelFormat.MONO8
        def set_pixel_format(self, value): pass
        def get_camera_mode(self): return "normal"
        def set_camera_mode(self, value): pass
        def set_binning(self, x, y): pass
        def set_region_of_interest(self, *roi): pass
        def get_total_frame_time(self): return 1.0
        def get_ready_for_trigger(self): return True
        def describe_trigger_routing(self): return "logical software / physical nidaq"

        def send_trigger(self, **kwargs):
            self.frame_id += 1
            if deferred:
                worker._on_frame_arrived(self.frame_id)
            value = 10 - abs(z_mm[0] * 1000 - 500)
            frame = SimpleNamespace(
                frame_id=self.frame_id, frame=np.full((16, 16), value, dtype=np.uint8))
            if decode_on_thread and self.frame_id <= 3:
                thread = threading.Thread(target=self._propogate_frame, args=(frame,))
                self.decode_threads.append(thread)
                thread.start()
            else:
                self._propogate_frame(frame)

    camera = Camera()
    worker.camera = camera
    axis = SimpleNamespace(MIN_POSITION=0, MAX_POSITION=1,
                           raw_to_canonical=lambda value: value,
                           canonical_to_raw=lambda value: value,
                           convert_to_real_units=lambda value: 0.001)
    stage = SimpleNamespace(
        get_pos=lambda: SimpleNamespace(z_mm=z_mm[0]),
        get_config=lambda: SimpleNamespace(Z_AXIS=axis),
        move_z_to=lambda target: z_mm.__setitem__(0, target),
        wait_for_idle=lambda timeout: None)
    worker.stage = stage
    ic = MagicMock()
    live = SimpleNamespace(is_live=False, trigger_mode=TriggerMode.SOFTWARE,
                           obs_controller=SimpleNamespace(ic=ic, microscope=MagicMock()),
                           set_trigger_mode=lambda mode: None)
    imaging_state = ObservationState(name="imaging")
    active_state = [imaging_state]
    live.obs_controller.current_observation_state = imaging_state
    live.obs_controller.read_capture_optical_state = lambda: {
        "confocal_mode": active_state[0].confocal_mode,
        "motor_running": None,
        "filters": {}, "confocal": {}, "auto_switch": False}
    live.obs_controller.apply_capture_optical_state = lambda state: active_state.__setitem__(0, state)
    live.obs_controller.capture_filter_targets = lambda state: {}
    live.obs_controller.restore_capture_optical_state = lambda snapshot: active_state.__setitem__(0, imaging_state)
    worker.autofocusController = SimpleNamespace(
        camera=camera, stage=stage, liveController=live,
        deltaZ=0.001, N=3, crop_width=16, crop_height=16,
        _log=MagicMock(), autofocus=MagicMock())
    worker._contrast_af_step_um = 1
    worker._contrast_af_count = 3
    worker._contrast_af_crop_width = 16
    worker._contrast_af_crop_height = 16
    worker._describe_camera_trigger_routing = camera.describe_trigger_routing
    monkeypatch.setattr("control.core.multi_point_worker.utils.calculate_focus_measure",
                        lambda image, metric: float(image[0, 0]))

    result = worker._run_owned_contrast_scan(ObservationState(name="AF"))
    for thread in camera.decode_threads:
        thread.join(timeout=1)
        assert not thread.is_alive()
    assert result.status == "success"
    assert result.frames == 3
    assert delivered_imaging == []
    assert worker._pending_capture_info_by_frame_id == {}
    assert camera.get_callbacks_enabled() is True
    assert camera.get_is_streaming() is True

    monitor = worker._run_owned_contrast_scan(ObservationState(name="AF"), capture_only=True)
    assert isinstance(monitor, FocusSample)
    assert delivered_imaging == []
    assert worker._pending_capture_info_by_frame_id == {}

    worker._current_capture_info = SimpleNamespace(file_id="next-imaging")
    worker._ready_for_next_trigger.clear()
    camera.send_trigger()
    assert delivered_imaging == [5]
    if deferred:
        assert worker._pending_capture_info_by_frame_id[5].file_id == "next-imaging"
    worker.autofocusController.autofocus.assert_not_called()
    worker.request_abort_fn.assert_not_called()
