"""Exercise the real Tucsen read/trigger/stream methods without opening hardware."""

import threading
import queue
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

import control.camera_tucsen as tucsen
from squid.abc import CameraAcquisitionMode
from tests.control.core.test_sample_incidence_calibration import hardware


def frame(frame_id):
    return SimpleNamespace(frame_id=frame_id, frame=np.full((32, 32), frame_id, dtype=np.uint16))


class ReadCamera:
    read_camera_frame = tucsen.TucsenCamera.read_camera_frame
    get_frame_id = tucsen.TucsenCamera.get_frame_id
    get_is_streaming = tucsen.TucsenCamera.get_is_streaming
    send_trigger = tucsen.TucsenCamera.send_trigger
    _uses_sdk_buffer_callback = tucsen.TucsenCamera._uses_sdk_buffer_callback

    def __init__(self, sdk_callback=True):
        self._frame_lock = threading.Lock()
        self._is_streaming = threading.Event()
        self._is_streaming.set()
        self._read_thread_running = threading.Event()
        if not sdk_callback:
            self._read_thread_running.set()
        self._read_thread_wait_period_s = 0.01
        self._model_properties = SimpleNamespace(is_genicam=True)
        self._capture_mode_genicam = (
            tucsen.TUCAM_CAPTURE_MODES.TUCCM_TRIGGER_STANDARD.value
            if sdk_callback
            else tucsen.TUCAM_CAPTURE_MODES.TUCCM_SEQUENCE.value
        )
        self._trigger_cb_obj = object() if sdk_callback else None
        self._decode_thread = SimpleNamespace(is_alive=lambda: True) if sdk_callback else None
        self._current_frame = frame(1)
        self._next_frame_id = 2
        self._last_trigger_frame_id = None
        self._last_trigger_timestamp = 0
        self._trigger_sent = threading.Event()
        self._acquisition_mode = CameraAcquisitionMode.SOFTWARE_TRIGGER
        self._virt_sw_trigger = False
        self._triggers_sent_since_start = 0
        self._log = MagicMock()
        self._set_genicam_parameter = MagicMock()

    def get_acquisition_mode(self):
        return self._acquisition_mode

    def get_total_frame_time(self):
        return 10

    def get_exposure_time(self):
        return 1

    def get_ready_for_trigger(self):
        return True


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    callback = [lambda: None]

    def sleep(seconds):
        now[0] += seconds
        callback[0]()

    monkeypatch.setattr(tucsen.time, "time", lambda: now[0])
    monkeypatch.setattr(tucsen.time, "sleep", sleep)
    return callback


def test_sdk_callback_delivers_blocking_read_without_polling_thread(clock):
    camera = ReadCamera()
    camera._last_trigger_frame_id = 1
    fresh = frame(2)
    clock[0] = lambda: setattr(camera, "_current_frame", fresh)
    assert not camera._read_thread_running.is_set()
    assert camera.read_camera_frame() is fresh
    camera._log.error.assert_not_called()


@pytest.mark.parametrize("virtual", [False, True])
def test_frame_decoded_before_read_is_returned_for_requested_trigger(clock, virtual):
    camera = ReadCamera()
    fresh = frame(2)

    def pulse(*args):
        camera._current_frame = fresh
        camera._next_frame_id = 3

    camera._virt_sw_trigger = virtual
    camera._hw_trigger_fn = pulse
    camera._set_genicam_parameter.side_effect = pulse
    camera.send_trigger()
    assert camera._last_trigger_frame_id == 1
    assert camera.read_camera_frame() is fresh
    camera._log.error.assert_not_called()


def test_frame_from_older_trigger_still_decoding_is_not_accepted(clock):
    camera = ReadCamera()
    # Frame 2 has arrived at the SDK and is queued; frame 1 is still cached.
    camera._next_frame_id = 3
    camera.send_trigger()
    assert camera._last_trigger_frame_id == 2
    delivered = [frame(2), frame(3)]
    reads = []

    def decode():
        reads.append(1)
        camera._current_frame = delivered.pop(0)

    clock[0] = decode
    result = camera.read_camera_frame()
    assert result.frame_id == 3 and len(reads) == 2


def test_missing_trigger_frame_times_out_instead_of_returning_stale_cache(clock):
    camera = ReadCamera()
    camera.send_trigger()
    assert camera.read_camera_frame() is None
    camera._log.warning.assert_called_once()
    camera._log.error.assert_not_called()


def test_continuous_mode_still_waits_for_a_new_frame(clock):
    camera = ReadCamera(sdk_callback=False)
    camera._acquisition_mode = CameraAcquisitionMode.CONTINUOUS
    camera._last_trigger_frame_id = 0
    fresh = frame(2)
    clock[0] = lambda: setattr(camera, "_current_frame", fresh)
    assert camera.read_camera_frame() is fresh


@pytest.mark.parametrize("failure", ["callback", "decoder_missing", "decoder_dead"])
def test_sdk_callback_delivery_health_is_checked(clock, failure):
    camera = ReadCamera()
    if failure == "callback":
        camera._trigger_cb_obj = None
    elif failure == "decoder_missing":
        camera._decode_thread = None
    else:
        camera._decode_thread = SimpleNamespace(is_alive=lambda: False)
    assert camera.read_camera_frame() is None
    assert "callback/decoder" in camera._log.error.call_args.args[0]


def test_polling_read_waits_for_thread_startup_flag(clock):
    camera = ReadCamera(sdk_callback=False)
    camera._read_thread_running = SimpleNamespace(wait=MagicMock(return_value=True))
    fresh = frame(2)
    clock[0] = lambda: setattr(camera, "_current_frame", fresh)
    assert camera.read_camera_frame() is fresh
    camera._read_thread_running.wait.assert_called_once_with(timeout=0.01)


def test_stopped_camera_and_shutdown_while_waiting_return_none(clock):
    camera = ReadCamera()
    camera._is_streaming.clear()
    assert camera.read_camera_frame() is None
    camera._is_streaming.set()
    clock[0] = camera._is_streaming.clear
    assert camera.read_camera_frame() is None


class StreamCamera(ReadCamera):
    start_streaming = tucsen.TucsenCamera.start_streaming
    stop_streaming = tucsen.TucsenCamera.stop_streaming

    def __init__(self):
        super().__init__()
        self._camera = object()
        self._m_frame = object()
        self._frames_received_since_start = 0
        self._max_acquisition_rate_hz = 250
        self._next_frame_id = 7
        self._last_trigger_frame_id = 6
        self.callbacks = True
        self.events = []
        self._cleanup_read_thread = MagicMock()
        self._stop_decode_thread = lambda: self.events.append("decode stopped")
        self._install_trigger_buffer_callback = MagicMock()
        self._start_decode_thread = MagicMock()
        self._update_internal_settings = MagicMock()

    def get_camera_mode(self):
        return "hdr"

    def get_callbacks_enabled(self):
        return self.callbacks

    def enable_callbacks(self, value):
        self.callbacks = value

    def set_acquisition_mode(self, value):
        self._acquisition_mode = value


@pytest.fixture
def stream_camera(monkeypatch):
    camera = StreamCamera()

    def cap_stop(*args):
        camera.events.append("SDK stopped")
        return tucsen.TUCAMRET.TUCAMRET_SUCCESS

    monkeypatch.setattr(tucsen, "TUCAM_Cap_Stop", cap_stop)
    monkeypatch.setattr(tucsen, "TUCAM_Cap_Start", lambda *args: tucsen.TUCAMRET.TUCAMRET_SUCCESS)
    return camera


def test_stop_drains_decoder_after_sdk_capture_stops_and_keeps_callback_alive(stream_camera):
    camera = stream_camera
    callback = camera._trigger_cb_obj
    camera.stop_streaming()
    assert camera.events == ["SDK stopped", "decode stopped"]
    assert camera._trigger_cb_obj is callback
    assert not camera.get_is_streaming()


def test_restart_drops_previous_session_cache_before_reusing_frame_ids(stream_camera, clock):
    camera = stream_camera
    camera.stop_streaming()
    camera.start_streaming()
    assert camera.get_frame_id() == -1
    assert camera._next_frame_id == 1
    assert camera._last_trigger_frame_id is None
    fresh = frame(1)  # Same ID as the previous session's old cached frame.
    camera._set_genicam_parameter.side_effect = lambda *args: setattr(camera, "_current_frame", fresh)
    camera.send_trigger()
    assert camera.read_camera_frame() is fresh


def test_alignment_start_capture_and_restore_use_callback_delivery(stream_camera, clock):
    camera = stream_camera
    hw, _, _ = hardware(initialized=False)
    hw.scope.camera = hw.camera = camera
    fresh = frame(1)
    camera._set_genicam_parameter.side_effect = lambda *args: setattr(camera, "_current_frame", fresh)
    with hw:
        image = hw.capture(0)
        assert np.all(image == 1)
        assert not camera.callbacks
    assert camera.get_acquisition_mode() == CameraAcquisitionMode.SOFTWARE_TRIGGER
    assert camera.callbacks and camera.get_is_streaming() and hw.live.is_live
    camera._log.error.assert_not_called()


def test_decoder_that_cannot_stop_keeps_its_reference_and_prevents_restart():
    decoder = SimpleNamespace(join=MagicMock(), is_alive=lambda: True)
    camera = SimpleNamespace(
        _decode_thread=decoder, _decode_thread_stop=threading.Event(), _decode_queue=queue.Queue(), _log=MagicMock()
    )
    with pytest.raises(tucsen.CameraError, match="refused to exit"):
        tucsen.TucsenCamera._stop_decode_thread(camera, drain_timeout_s=0.01)
    assert camera._decode_thread is decoder
    assert camera._decode_thread_stop.is_set()
    with pytest.raises(tucsen.CameraError, match="still stopping"):
        tucsen.TucsenCamera._start_decode_thread(camera)


def test_decoder_stop_failure_still_marks_sdk_capture_stopped(stream_camera):
    camera = stream_camera

    def fail():
        raise tucsen.CameraError("Decoder did not stop")

    camera._stop_decode_thread = fail
    with pytest.raises(tucsen.CameraError, match="did not stop"):
        camera.stop_streaming()
    assert not camera.get_is_streaming()
    assert camera._trigger_cb_obj is not None


def test_inflight_decode_finishes_before_restart_clears_old_frame(stream_camera):
    camera = stream_camera
    camera._decode_thread = None
    camera._decode_queue = queue.Queue()
    camera._decode_thread_stop = threading.Event()
    entered, release = threading.Event(), threading.Event()
    errors = []

    def decode(item):
        entered.set()
        release.wait(2)
        camera._current_frame = frame(99)

    camera._decode_and_propogate = decode
    camera._decode_loop = lambda: tucsen.TucsenCamera._decode_loop(camera)
    camera._start_decode_thread = lambda: tucsen.TucsenCamera._start_decode_thread(camera)
    camera._stop_decode_thread = lambda: tucsen.TucsenCamera._stop_decode_thread(camera)

    def stop():
        try:
            camera.stop_streaming()
        except Exception as error:
            errors.append(error)

    camera._start_decode_thread()
    camera._decode_queue.put({"old session": True})
    stopping = threading.Thread(target=stop)
    try:
        assert entered.wait(1)
        stopping.start()
        assert camera._decode_thread_stop.wait(1)
        assert stopping.is_alive()
        release.set()
        stopping.join(2)
        assert not stopping.is_alive() and not errors
        assert camera._decode_thread is None
        camera.start_streaming()
        assert camera._current_frame is None
        assert camera._decode_thread.is_alive()
    finally:
        release.set()
        if stopping.ident is not None:
            stopping.join(2)
        camera.stop_streaming()
