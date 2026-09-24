"""Daheng cached reads must not reuse a frame delivered before a trigger."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import control.camera as camera_module
from control.camera import DefaultCamera


@pytest.mark.parametrize("trigger", [0.0, 9.998])
def test_recent_frame_after_trigger_or_in_continuous_mode_is_cached(monkeypatch, trigger):
    frame = SimpleNamespace(timestamp=9.999, frame_id=1)
    camera = SimpleNamespace(
        _log=MagicMock(), get_frame_id=lambda: 1, get_is_streaming=lambda: True,
        _exposure_time_ms=5, _strobe_delay_us=0, _current_frame=frame,
        _last_trigger_timestamp=trigger,
    )
    monkeypatch.setattr(camera_module.time, "time", lambda: 10.0)
    assert DefaultCamera.read_camera_frame(camera) is frame


@pytest.mark.parametrize("deliver", [True, False])
def test_pretrigger_cache_waits_for_new_frame_or_times_out(monkeypatch, deliver):
    now = [10.0]
    stale = SimpleNamespace(timestamp=9.999, frame_id=1)
    fresh = SimpleNamespace(timestamp=10.001, frame_id=2)
    camera = SimpleNamespace(
        _log=MagicMock(), get_is_streaming=lambda: True,
        _exposure_time_ms=5, _strobe_delay_us=0, _current_frame=stale,
        _last_trigger_timestamp=10.0, _frame_lock=nullcontext(),
    )
    camera.get_frame_id = lambda: camera._current_frame.frame_id
    def sleep(seconds):
        now[0] += seconds
        if deliver:
            camera._current_frame = fresh
    monkeypatch.setattr(camera_module.time, "time", lambda: now[0])
    monkeypatch.setattr(camera_module.time, "sleep", sleep)
    assert DefaultCamera.read_camera_frame(camera) is (fresh if deliver else None)
