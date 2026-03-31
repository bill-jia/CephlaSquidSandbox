from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

from control._def import TriggerMode
from control.core.multi_point_worker import MultiPointWorker


class _ReadyEventStub:
    def __init__(self, wait_results):
        self._wait_results = list(wait_results)
        self.cleared = 0

    def wait(self, _timeout=None):
        if self._wait_results:
            return self._wait_results.pop(0)
        return True

    def clear(self):
        self.cleared += 1

    def set(self):
        return None

    def is_set(self):
        return False


class _TimingStub:
    def get_timer(self, _name):
        return nullcontext()


def test_observation_snapshot_continuous_capture_uses_saved_illumination_state():
    """Observation-state captures should snap one continuous frame with the saved light state."""
    worker = MultiPointWorker.__new__(MultiPointWorker)
    worker._log = MagicMock()
    worker._timing = _TimingStub()
    worker.keep_illuminators_on_between_captures = False
    worker._last_illumination_config_name = None
    worker._use_observation_presets = True
    worker._ready_for_next_trigger = _ReadyEventStub([True, True])
    worker._backpressure = SimpleNamespace(should_throttle=lambda: False)
    worker._frame_wait_timeout_s = lambda: 1.0
    worker._sleep = lambda _sec: None
    worker.wait_till_operation_is_completed = lambda: None
    worker.request_abort_fn = MagicMock()
    worker._current_capture_info = None
    worker.use_piezo = False
    worker.z_piezo_um = None
    worker.time_point = 0
    worker.stage = SimpleNamespace(get_pos=lambda: SimpleNamespace(x_mm=1.0, y_mm=2.0, z_mm=3.0))
    worker._select_config = MagicMock()

    illum = MagicMock()
    illum.get_shutter_state.return_value = {"LaserA": True, "LaserB": False}
    worker.microscope = SimpleNamespace(illumination_controller=illum, addons=SimpleNamespace(nl5=None))

    live_controller = SimpleNamespace(
        trigger_mode=TriggerMode.CONTINUOUS,
        turn_on_illumination=MagicMock(),
        turn_off_illumination=MagicMock(),
    )
    worker.liveController = live_controller

    camera = MagicMock()
    camera.get_exposure_time.return_value = 10.0
    camera.send_trigger = MagicMock()
    worker.camera = camera

    config = SimpleNamespace(name="ObsPresetA")

    worker.acquire_camera_image(
        config,
        file_ID="f0",
        current_path=".",
        k=0,
        region_id=0,
        fov=0,
        config_idx=0,
    )

    worker._select_config.assert_called_once_with(config)
    camera.send_trigger.assert_not_called()
    live_controller.turn_on_illumination.assert_not_called()
    illum.set_channel_state.assert_any_call("LaserA", True, force_hardware=True)
    illum.set_channel_state.assert_any_call("LaserB", False, force_hardware=True)
    assert illum.set_channel_state.call_count == 2
    illum.turn_off_all.assert_called_once_with(preserve_logical_state=True)
    assert worker.request_abort_fn.call_count == 0
