from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from control._def import TriggerMode
from control.core.contrast_autofocus.capture import StageCaptureSession
from control.core.contrast_autofocus.search import CaptureFailure
from control.models.observation_state import CameraSettings, CameraLiveSnapshot, ConfocalSettings, ObservationState
from control.core.observation_state_controller import ObservationStateController
from squid.config import CameraPixelFormat


def make_session(*, unsupported_gain=False, state=None):
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
    if unsupported_gain:
        camera.get_analog_gain.side_effect = NotImplementedError(
            "Analog gain is not implemented for this camera.")
        camera.set_analog_gain.side_effect = NotImplementedError(
            "Analog gain is not implemented for this camera.")
    else:
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
    return StageCaptureSession(controller, state or ObservationState(), 0, 40, lambda: False), camera, live, pos, callbacks


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


def test_session_captures_without_analog_gain_support():
    state = ObservationState(camera_settings=CameraSettings(exposure_time_ms=1, gain_mode=3))
    session, camera, live, _, callbacks = make_session(unsupported_gain=True, state=state)
    assert session.original_gain is None
    session.controller._log.warning.assert_called_once()
    with session:
        frame = session.capture_at(25)
        assert frame.actual_z_um == 25
    camera.get_analog_gain.assert_called_once()
    camera.set_analog_gain.assert_not_called()
    assert live.is_live
    assert callbacks == [True]
    assert session.cleanup_errors == []


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


def test_acquisition_session_restores_camera_geometry():
    state = ObservationState(camera_live=CameraLiveSnapshot(
        exposure_time_ms=1, pixel_format="MONO16", camera_mode="fast",
        binning_x=2, binning_y=2, roi_offset_x=2, roi_offset_y=2,
        roi_width=4, roi_height=4))
    session, camera, _, _, _ = make_session(state=state)
    session.apply_camera_live_geometry = True
    geometry = {
        "format": CameraPixelFormat.MONO8, "mode": "normal",
        "binning": (1, 1), "roi": (0, 0, 4, 4),
    }
    camera.get_pixel_format.side_effect = lambda: geometry["format"]
    camera.set_pixel_format.side_effect = lambda value: geometry.__setitem__("format", value)
    camera.get_camera_mode.side_effect = lambda: geometry["mode"]
    camera.set_camera_mode.side_effect = lambda value: geometry.__setitem__("mode", value)
    camera.get_binning.side_effect = lambda: geometry["binning"]
    camera.set_binning.side_effect = lambda x, y: geometry.__setitem__("binning", (x, y))
    camera.get_region_of_interest.side_effect = lambda: geometry["roi"]
    camera.set_region_of_interest.side_effect = lambda *args: geometry.__setitem__("roi", args)
    session.original_geometry = (
        (1, 1), (0, 0, 4, 4), CameraPixelFormat.MONO8, "normal")
    with session:
        assert geometry == {
            "format": CameraPixelFormat.MONO16, "mode": "fast",
            "binning": (2, 2), "roi": (2, 2, 4, 4),
        }
    assert geometry == {
        "format": CameraPixelFormat.MONO8, "mode": "normal",
        "binning": (1, 1), "roi": (0, 0, 4, 4),
    }


def test_failed_acquisition_geometry_restore_does_not_restart_live():
    state = ObservationState(camera_live=CameraLiveSnapshot(
        exposure_time_ms=1, binning_x=2, binning_y=2))
    session, camera, live, _, callbacks = make_session(state=state)
    session.apply_camera_live_geometry = True
    binning = [(1, 1)]
    camera.get_binning.side_effect = lambda: binning[0]
    camera.set_binning.side_effect = lambda x, y: binning.__setitem__(0, (x, y)) if (x, y) == (2, 2) else None
    with pytest.raises(RuntimeError, match="cleanup failed"):
        with session:
            assert binning[0] == (2, 2)
    assert binning[0] == (2, 2)
    assert callbacks == [False]
    live.start_live.assert_not_called()


def test_waveform_arm_failure_turns_off_illumination_and_restores_session(monkeypatch):
    state = SimpleNamespace(camera_settings=None, camera_live=None,
                            is_waveform_driven=True)
    session, camera, live, _, callbacks = make_session(state=state)
    monkeypatch.setattr(
        "control.core.waveform_capture.apply_illumination_for_waveform_capture",
        MagicMock())
    monkeypatch.setattr(
        "control.core.waveform_capture.arm_nidaq_pulse_for_capture",
        MagicMock(side_effect=RuntimeError("arm failed")))
    with pytest.raises(RuntimeError, match="arm failed"):
        with session:
            session.capture_at(25)
    session.live.obs_controller.ic.turn_off_all.assert_called_once_with(
        preserve_logical_state=True)
    assert live.is_live
    assert callbacks == [True]
    camera.send_trigger.assert_not_called()


def _optical_session():
    af = ObservationState(name="AF", emission_filter_positions={"default": 3}, confocal_mode=True)
    imaging = ObservationState(name="imaging", emission_filter_positions={"default": 1}, confocal_mode=False)
    session, camera, live, _, callbacks = make_session(state=af)
    events = []
    active = [imaging]
    obs = live.obs_controller
    obs.current_observation_state = imaging
    def read():
        return {"confocal_mode": active[0].confocal_mode,
                "motor_running": None,
                "filters": {1: int(active[0].emission_filter_positions["default"])},
                "confocal": {}, "auto_switch": False}
    obs.read_capture_optical_state = read
    obs.capture_filter_targets = lambda state: {1: int(state.emission_filter_positions["default"])}
    def apply(state, **kwargs):
        events.append(("apply", state.emission_filter_positions["default"], state.confocal_mode))
        active[0] = state
    obs.apply_capture_optical_state = MagicMock(side_effect=apply)
    def restore(snapshot):
        events.append(("apply", snapshot["filters"][1], snapshot["confocal_mode"]))
        active[0] = imaging
    obs.restore_capture_optical_state = MagicMock(side_effect=restore)
    camera.send_trigger.side_effect = lambda **kwargs: events.append(("trigger", active[0].emission_filter_positions["default"]))
    session.apply_optical_state = True
    session.wait_for_hardware = lambda: events.append(("settled", active[0].emission_filter_positions["default"]))
    return session, camera, live, callbacks, events, obs


def test_optical_state_applies_before_trigger_and_restores_before_callbacks():
    session, camera, live, callbacks, events, obs = _optical_session()
    camera.enable_callbacks.side_effect = lambda value: (events.append(("callbacks", value)), callbacks.__setitem__(0, value))
    with session:
        session.capture_at(25)
    assert events.index(("apply", 3, True)) < events.index(("settled", 3)) < events.index(("trigger", 3))
    assert events.index(("apply", 1, False)) < events.index(("settled", 1)) < events.index(("callbacks", True))
    obs.apply_capture_optical_state.assert_called_once()
    obs.restore_capture_optical_state.assert_called_once()
    assert live.is_live


def test_optical_state_restores_after_cancelled_capture():
    session, camera, live, callbacks, events, _ = _optical_session()
    with pytest.raises(InterruptedError):
        with session:
            session.cancelled = lambda: True
            session.capture_at(25)
    assert ("apply", 1, False) in events
    assert callbacks == [True]
    assert live.is_live
    camera.send_trigger.assert_not_called()


def test_optical_apply_failure_restores_imaging_state():
    session, camera, live, callbacks, events, obs = _optical_session()
    original = obs.apply_capture_optical_state.side_effect
    def fail_af(state, **kwargs):
        original(state, **kwargs)
        if state.emission_filter_positions["default"] == 3:
            raise RuntimeError("AF filter failed")
    obs.apply_capture_optical_state.side_effect = fail_af
    with pytest.raises(RuntimeError, match="AF filter failed"):
        with session:
            pass
    assert ("apply", 1, False) in events
    assert callbacks == [True]
    assert live.is_live
    camera.send_trigger.assert_not_called()


def test_optical_restore_failure_keeps_imaging_callbacks_stopped():
    session, camera, live, callbacks, events, obs = _optical_session()
    obs.restore_capture_optical_state.side_effect = RuntimeError("imaging filter failed")
    with pytest.raises(RuntimeError, match="cleanup failed"):
        with session:
            session.capture_at(25)
    assert callbacks == [False]
    live.start_live.assert_not_called()


def test_capture_optical_apply_selects_mode_and_path_without_camera_writes():
    state = ObservationState(confocal_mode=True, emission_filter_positions={"default": 3})
    obs = ObservationStateController.__new__(ObservationStateController)
    obs._current_state = None
    obs.enable_channel_auto_filter_switching = False
    obs._write_capture_optical_values = MagicMock()
    obs.capture_filter_targets = lambda state: {1: 3}
    ObservationStateController.apply_capture_optical_state(obs, state)
    assert obs._write_capture_optical_values.call_args.args[0]["filters"] == {1: 3}
    assert obs._current_state is state


def _real_optical_controller(*, wheel=None, xlight=None):
    obs = ObservationStateController.__new__(ObservationStateController)
    obs._current_state = ObservationState(name="imaging", emission_filter_positions={"default": 1})
    obs._confocal_mode = False
    obs.enable_channel_auto_filter_switching = False
    obs.microscope = SimpleNamespace(
        addons=SimpleNamespace(emission_filter_wheel=wheel, xlight=xlight, dragonfly=None),
        illumination_controller=MagicMock())
    return obs


def test_standalone_wheel_readback_verifies_default_alias_and_restores():
    position = [1]
    wheel = SimpleNamespace(
        get_filter_wheel_position=lambda: {1: position[0]},
        set_filter_wheel_position=lambda slots: position.__setitem__(0, slots[1]))
    session, camera, live, _, callbacks = make_session(
        state=ObservationState(name="AF", emission_filter_positions={"default": 3}))
    obs = _real_optical_controller(wheel=wheel)
    live.obs_controller = obs
    session.apply_optical_state = True
    with session:
        assert position == [3]
        session.capture_at(25)
    assert position == [1]
    assert callbacks == [True]
    camera.send_trigger.assert_called_once()


@pytest.mark.parametrize("failure", ["setter", "readback"])
def test_real_wheel_failure_cannot_resume_imaging(failure):
    position = [1]
    reads = [0]
    def read():
        reads[0] += 1
        if failure == "readback" and reads[0] > 1:
            raise RuntimeError("readback unavailable")
        return {1: position[0]}
    def write(slots):
        if failure == "setter":
            raise RuntimeError("filter jammed")
        position[0] = slots[1]
    wheel = SimpleNamespace(get_filter_wheel_position=read, set_filter_wheel_position=write)
    session, camera, live, _, callbacks = make_session(
        state=ObservationState(name="AF", emission_filter_positions={"default": 3}))
    live.obs_controller = _real_optical_controller(wheel=wheel)
    session.apply_optical_state = True
    with pytest.raises(RuntimeError, match="filter jammed|readback unavailable"):
        with session:
            session.capture_at(25)
    assert callbacks == [False]
    live.start_live.assert_not_called()
    camera.send_trigger.assert_not_called()


@pytest.mark.parametrize("failure", ["setter", "readback"])
def test_real_wheel_restore_failure_keeps_imaging_stopped(failure):
    position = [1]
    restoring = [False]
    def read():
        if restoring[0] and failure == "readback":
            raise RuntimeError("restore readback unavailable")
        return {1: position[0]}
    def write(slots):
        if slots[1] == 1:
            restoring[0] = True
            if failure == "setter":
                raise RuntimeError("restore filter jammed")
        position[0] = slots[1]
    wheel = SimpleNamespace(get_filter_wheel_position=read, set_filter_wheel_position=write)
    session, camera, live, _, callbacks = make_session(
        state=ObservationState(name="AF", emission_filter_positions={"default": 3}))
    live.obs_controller = _real_optical_controller(wheel=wheel)
    session.apply_optical_state = True
    with pytest.raises(RuntimeError, match="cleanup failed"):
        with session:
            session.capture_at(25)
    assert callbacks == [False]
    live.start_live.assert_not_called()
    camera.send_trigger.assert_called_once()


def test_xlight_partial_confocal_state_preserves_unspecified_dichroic():
    hardware = {"disk": 0, "filter": 1, "iris": 10.0, "dichroic": 2}
    xlight = SimpleNamespace(
        spinning_disk_pos=0, has_spinning_disk_motor=False,
        has_emission_filters_wheel=True, disable_emission_filter_wheel=False,
        has_illumination_iris_diaphragm=True, has_emission_iris_diaphragm=False,
        has_dichroic_filters_wheel=True, has_dichroic_filter_slider=False,
        get_disk_position=lambda: hardware["disk"],
        get_emission_filter=lambda: hardware["filter"],
        get_illumination_iris=lambda: hardware["iris"],
        get_dichroic=lambda: hardware["dichroic"],
        set_emission_filter=lambda value, extraction=False: hardware.__setitem__("filter", value),
        set_illumination_iris=lambda value: hardware.__setitem__("iris", value),
        set_dichroic=lambda value: hardware.__setitem__("dichroic", value),
    )
    def set_disk(value):
        hardware["disk"] = value
        xlight.spinning_disk_pos = value
    xlight.set_disk_position = set_disk
    state = ObservationState(name="AF", confocal_mode=True,
                             emission_filter_positions={"default": 3},
                             confocal_hardware_settings=ConfocalSettings(illumination_iris=20))
    session, camera, live, _, callbacks = make_session(state=state)
    live.obs_controller = _real_optical_controller(xlight=xlight)
    session.apply_optical_state = True
    with session:
        assert hardware == {"disk": 1, "filter": 3, "iris": 20, "dichroic": 2}
        session.capture_at(25)
    assert hardware == {"disk": 0, "filter": 1, "iris": 10.0, "dichroic": 2}
    assert callbacks == [True]
    camera.send_trigger.assert_called_once()


def test_default_filter_uses_dragonfly_camera_port():
    obs = _real_optical_controller()
    obs.microscope.addons.dragonfly = SimpleNamespace(get_camera_port=lambda: 2)
    state = ObservationState(emission_filter_positions={"default": 3})
    assert obs.capture_filter_targets(state) == {2: 3}


def _motor_session(*, disk=1, running=False, start_failure=False, read_failure=False):
    hardware = {"disk": disk, "running": running}
    calls = {"reads": 0, "starts": 0, "disk_moves": 0}
    xlight = SimpleNamespace(
        spinning_disk_pos=disk, has_spinning_disk_motor=True,
        has_emission_filters_wheel=False,
        has_illumination_iris_diaphragm=False,
        has_emission_iris_diaphragm=False,
        has_dichroic_filters_wheel=False,
        has_dichroic_filter_slider=False,
        get_disk_position=lambda: hardware["disk"],
    )
    def read_motor():
        calls["reads"] += 1
        if read_failure and calls["reads"] > 1:
            raise RuntimeError("motor readback unavailable")
        return hardware["running"]
    def set_motor(value):
        calls["starts"] += 1
        if start_failure:
            raise RuntimeError("motor would not start")
        hardware["running"] = value
    def set_disk(value):
        calls["disk_moves"] += 1
        hardware["disk"] = value
        xlight.spinning_disk_pos = value
    xlight.get_disk_motor_state = read_motor
    xlight.set_disk_motor_state = set_motor
    xlight.set_disk_position = set_disk
    session, camera, live, _, callbacks = make_session(
        state=ObservationState(name="AF", confocal_mode=True))
    obs = _real_optical_controller(xlight=xlight)
    obs._confocal_mode = bool(disk)
    obs.current_observation_state = ObservationState(name="imaging", confocal_mode=bool(disk))
    live.obs_controller = obs
    session.apply_optical_state = True
    return session, camera, live, callbacks, hardware, calls


def test_inserted_stopped_xlight_motor_starts_before_confocal_exposure():
    session, camera, _, callbacks, hardware, calls = _motor_session()
    with session:
        assert hardware["running"] is True
        session.capture_at(25)
    assert calls["starts"] == 1
    assert calls["disk_moves"] == 1  # helper confirms motor, then reasserts disk
    assert callbacks == [True]
    camera.send_trigger.assert_called_once()


def test_running_xlight_motor_does_not_repeat_disk_move():
    session, camera, _, callbacks, hardware, calls = _motor_session(running=True)
    with session:
        session.capture_at(25)
    assert hardware["running"] is True
    assert calls["starts"] == calls["disk_moves"] == 0
    assert callbacks == [True]
    camera.send_trigger.assert_called_once()


@pytest.mark.parametrize("failure", ["start", "readback"])
def test_xlight_motor_failure_prevents_exposure(failure):
    session, camera, live, callbacks, _, _ = _motor_session(
        start_failure=failure == "start", read_failure=failure == "readback")
    with pytest.raises(RuntimeError, match="motor"):
        with session:
            session.capture_at(25)
    camera.send_trigger.assert_not_called()
    assert callbacks == [False]
    live.start_live.assert_not_called()


def test_widefield_restoration_leaves_xlight_motor_running():
    session, camera, _, callbacks, hardware, calls = _motor_session(disk=0)
    with session:
        assert hardware == {"disk": 1, "running": True}
        session.capture_at(25)
    assert hardware == {"disk": 0, "running": True}
    assert calls["starts"] == 1
    assert calls["disk_moves"] == 2
    assert callbacks == [True]
    camera.send_trigger.assert_called_once()
