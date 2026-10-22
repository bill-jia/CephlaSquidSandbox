from types import SimpleNamespace
import json
import threading

import numpy as np
import pytest

from control.core.sample_incidence_calibration import (
    AlignmentHardware,
    AlignmentSettings,
    CalibrationCancelled,
    SampleIncidenceSession,
    dither_offsets,
)


class FakeStage:
    def __init__(self):
        self.xyz = np.array([10.0, 11.0, 2.0])
        self.moves = []

    def get_pos(self):
        return SimpleNamespace(x_mm=self.xyz[0], y_mm=self.xyz[1], z_mm=self.xyz[2])

    def _move(self, axis, value):
        self.xyz[axis] = value
        self.moves.append((axis, value))

    def move_x_to(self, value):
        self._move(0, value)

    def move_y_to(self, value):
        self._move(1, value)

    def move_z_to(self, value):
        self._move(2, value)

    def get_config(self):
        axis = SimpleNamespace(canonical_to_raw=lambda x: x, MIN_POSITION=0, MAX_POSITION=50)
        return SimpleNamespace(X_AXIS=axis, Y_AXIS=axis, Z_AXIS=axis)


class FakeCamera:
    def __init__(self):
        self.mode = "original"
        self.streaming = True
        self.callbacks = True
        self.frame_id = 0
        self.stale = False

    def get_acquisition_mode(self):
        return self.mode

    def set_acquisition_mode(self, value):
        self.mode = value

    def get_is_streaming(self):
        return self.streaming

    def start_streaming(self):
        self.streaming = True

    def stop_streaming(self):
        self.streaming = False

    def get_callbacks_enabled(self):
        return self.callbacks

    def enable_callbacks(self, value):
        self.callbacks = value

    def get_exposure_time(self):
        return 10

    def get_ready_for_trigger(self):
        return True

    def get_frame_id(self):
        return self.frame_id

    def send_trigger(self, *args):
        self.frame_id += 1

    def read_camera_frame(self):
        return SimpleNamespace(frame_id=self.frame_id - int(self.stale), frame=np.ones((128, 128), np.uint16) * 1000)


class FakeLive:
    def __init__(self, live=True):
        self.is_live = live

    def start_live(self):
        self.is_live = True

    def stop_live(self):
        self.is_live = False


class FakePiezo:
    def __init__(self):
        self.position = 20

    def move_to(self, value):
        self.position = value


def hardware(piezo=False, initialized=True, reference=True):
    stage, camera = FakeStage(), FakeCamera()
    light = SimpleNamespace(on=False)
    obs = SimpleNamespace(
        current_observation_state=None,
        turn_on_illumination=lambda: setattr(light, "on", True),
        turn_off_illumination=lambda: setattr(light, "on", False),
    )
    scope = SimpleNamespace(
        stage=stage,
        camera=camera,
        live_controller=FakeLive(),
        live_controller_focus=FakeLive(),
        obs_controller=obs,
        addons=SimpleNamespace(piezo_stage=FakePiezo() if piezo else None),
    )
    calls = []

    def move(target):
        calls.append(target)
        if scope.addons.piezo_stage is None:
            stage.move_z_to(stage.get_pos().z_mm + 0.001)
        else:
            scope.addons.piezo_stage.move_to(21)
        return True

    af = SimpleNamespace(
        is_initialized=initialized,
        laser_af_properties=SimpleNamespace(has_reference=reference, x_reference=500 if reference else None),
        measure_displacement=lambda: -1,
        move_to_target=move,
    )
    return AlignmentHardware(scope, af), light, calls


@pytest.mark.parametrize("piezo", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_motorized_z_works_without_piezo_and_all_state_is_restored(piezo, failure):
    hw, light, calls = hardware(piezo)
    original = hw.stage.xyz.copy()
    try:
        with hw:
            assert not hw.live.is_live
            hw.validate_path(dither_offsets(100), 10)
            hw.move_xy(10100, 11100)
            base_z, piezo_z = hw.refocus(5)
            assert len(calls) == 1
            assert piezo_z == (21 if piezo else 0)
            hw.move_z(base_z + 2)
            assert hw.capture(0).shape == (128, 128)
            assert not light.on
            if failure:
                raise ValueError("Injected capture failure")
    except ValueError as error:
        assert failure and "Injected" in str(error)
    np.testing.assert_allclose(hw.stage.xyz, original)
    assert hw.camera.mode == "original"
    assert hw.camera.streaming and hw.camera.callbacks
    assert hw.live.is_live and hw.focus_live.is_live
    if piezo:
        assert hw.piezo.position == 20
    assert not light.on


@pytest.mark.parametrize("initialized,reference", [(False, True), (True, False)])
def test_uninitialized_or_unreferenced_laser_af_falls_back_to_stage_focus(initialized, reference):
    hw, _, calls = hardware(initialized=initialized, reference=reference)
    with hw:
        assert not hw.af_ready
        base, piezo = hw.refocus(5)
        assert base == 2000 and piezo == 0
        hw.move_z(base + 0.5)
    assert calls == []


def test_laser_af_runs_with_both_live_modes_stopped_and_specimen_illumination_off():
    hw, light, calls = hardware()
    light.on = True  # Include manually enabled illumination before takeover.
    measure = hw.af.measure_displacement
    correct = hw.af.move_to_target
    checked = []

    def isolated(action):
        assert not hw.live.is_live and not hw.focus_live.is_live
        assert not light.on
        checked.append(action)

    def measure_displacement():
        isolated("measure")
        return measure()

    def move_to_target(target):
        isolated("correct")
        return correct(target)

    hw.af.measure_displacement = measure_displacement
    hw.af.move_to_target = move_to_target
    with hw:
        hw.refocus(5)
        hw.capture(0)
        hw.refocus(5)
    assert checked == ["measure", "correct", "measure", "correct"]
    assert hw.live.is_live and hw.focus_live.is_live


def test_cancel_restores_state_and_out_of_range_moves_are_rejected():
    hw, light, _ = hardware()
    with pytest.raises(CalibrationCancelled):
        with hw:
            hw.move_xy(10100, 11100)
            hw.cancel.set()
            hw.capture(0)
    np.testing.assert_allclose(hw.stage.xyz, [10, 11, 2])
    assert not light.on and hw.live.is_live
    hw.cancel.clear()
    with hw:
        with pytest.raises(ValueError, match="exceeds"):
            hw.move_z(-1)


def test_dark_texture_never_passes_flatness_or_reaches_incidence():
    hw, _, _ = hardware(initialized=False)
    emitted = []
    session = SampleIncidenceSession(hw, AlignmentSettings(repeat=False, settle_s=0), 1, incidence_ready=emitted.append)
    with pytest.raises(ValueError, match="registered|texture|Texture"):
        session.run()
    assert emitted == []
    np.testing.assert_allclose(hw.stage.xyz, [10, 11, 2])


@pytest.mark.parametrize("piezo", [False, True])
def test_complete_two_stage_run_uses_stage_z_with_or_without_piezo(piezo, tmp_path):
    from scipy import ndimage
    from control.core.sample_incidence_recording import AlignmentRecorder

    hw, light, _ = hardware(piezo=piezo, initialized=False)
    rng = np.random.default_rng(14)
    specimen = ndimage.gaussian_filter(rng.normal(size=(512, 512)), 1)
    yy, xx = np.mgrid[:128, :128]
    xn, yn = 2 * (xx + 0.5) / 128 - 1, 2 * (yy + 0.5) / 128 - 1

    def frame():
        pos = hw.stage.get_pos()
        dx, dy = (pos.x_mm - 10) * 1000, (pos.y_mm - 11) * 1000
        z = (pos.z_mm - 2) * 1000
        grain = ndimage.map_coordinates(specimen, [yy + dy + 128, xx + dx + 128], order=1)
        texture = 1 + 0.5 * grain * np.exp(-0.5 * (z / 0.7) ** 2)
        illumination = np.exp(-((xn - (0.08 + 0.001 * z)) ** 2 + (yn + 0.05) ** 2) / 1.5)
        return SimpleNamespace(frame_id=hw.camera.frame_id, frame=2000 * texture * illumination)

    hw.camera.read_camera_frame = frame
    flatness, incidence = [], []
    settings = AlignmentSettings(dither_fraction=0.3125, repeat=False, settle_s=0)
    recorder = AlignmentRecorder(tmp_path)
    session = SampleIncidenceSession(
        hw, settings, 1, flatness_ready=flatness.append, incidence_ready=incidence.append, recorder=recorder
    )
    session.run()
    assert len(flatness) == 1 and flatness[0].accepted
    assert len(incidence) == 1
    assert incidence[0].angle_deg is not None
    assert incidence[0].reference_drift < 0.03
    np.testing.assert_allclose(hw.stage.xyz, [10, 11, 2])
    assert hw.live.is_live and hw.camera.mode == "original" and not light.on
    if piezo:
        assert hw.piezo.position == 20
    manifest = json.loads((recorder.path / "session.json").read_text())
    assert manifest["status"] == "completed"
    assert manifest["image_count"] == 1 + 9 * 9 + 9 * 3 + 3
    assert manifest["final_position"]["stage_canonical_mm"] == {"x": 10, "y": 11, "z": 2}
    assert manifest["final_position"]["piezo_um"] == (20 if piezo else None)
    events = [json.loads(line) for line in (recorder.path / "events.jsonl").read_text().splitlines()]
    assert sum(event["kind"] == "focus_scan" for event in events) == 9
    assert sum(event["kind"] == "incidence_illumination" for event in events) == 3
    assert sum(event["kind"] == "incidence" for event in events) == 1
    captures = [event for event in events if event["kind"] == "capture"]
    for event in captures:
        assert (recorder.path / event["image"]).is_file()
        if "target_z_um" in event:
            assert event["stage_canonical_mm"]["z"] * 1000 == pytest.approx(event["target_z_um"])


def test_capture_rejects_cached_pre_move_frame_before_accepting_fresh_frame():
    hw, _, _ = hardware(initialized=False)
    reads = []

    def read():
        reads.append(1)
        return SimpleNamespace(
            frame_id=hw.camera.frame_id - int(len(reads) == 1), frame=np.full((128, 128), len(reads), np.uint16)
        )

    hw.camera.read_camera_frame = read
    with hw:
        image = hw.capture(0)
    assert len(reads) == 2
    assert np.all(image == 2)


def test_exit_drains_pending_mcu_piezo_and_illumination_before_restoring_live():
    hw, _, _ = hardware(piezo=True)
    events = []

    class MCU:
        busy = False

        def wait_till_operation_is_completed(self, timeout_limit_s):
            events.append("mcu idle")
            self.busy = False

    hw.mcu = MCU()
    move_piezo = hw.piezo.move_to

    def piezo(value):
        move_piezo(value)
        hw.mcu.busy = True
        events.append("piezo command")

    hw.piezo.move_to = piezo
    start_live = hw.live.start_live

    def live():
        assert not hw.mcu.busy
        events.append("live restored")
        start_live()

    hw.live.start_live = live
    with hw:
        hw.piezo.move_to(25)
    assert events.index("live restored") > max(i for i, e in enumerate(events) if e == "piezo command")
    assert hw.piezo.position == 20
    assert not hw.mcu.busy


def test_preexisting_inflight_live_trigger_is_joined_before_camera_takeover():
    hw, _, _ = hardware()
    ended = threading.Event()
    old_stop = hw.live.stop_live

    def stop_live():
        old_stop()
        ended.set()

    hw.live.stop_live = stop_live
    hw.live.timer_trigger = threading.Thread(target=lambda: ended.wait(2))
    timer = hw.live.timer_trigger
    timer.start()
    with hw:
        assert not timer.is_alive()
        assert hw.live.timer_trigger is None
    assert hw.live.is_live


def test_mcu_restoration_failure_is_visible_and_live_does_not_restart():
    hw, _, _ = hardware()

    class MCU:
        fail = False

        def wait_till_operation_is_completed(self, timeout_limit_s):
            if self.fail:
                raise TimeoutError("MCU did not acknowledge")

    hw.mcu = MCU()
    with pytest.raises(RuntimeError, match="restoration failed.*MCU did not acknowledge"):
        with hw:
            hw.mcu.fail = True
    assert not hw.live.is_live
    assert not hw.camera.streaming


@pytest.mark.parametrize("manual_on", [False, True])
def test_capture_bypasses_live_gate_and_restores_logical_illumination(manual_on):
    import copy

    hw, _, _ = hardware(initialized=False)
    hw.live.is_live = False

    class Illumination:
        def __init__(self):
            self.states = {
                "laser": SimpleNamespace(intensity=7, is_on=False),
                "other": SimpleNamespace(intensity=20, is_on=True),
            }
            self._hardware_asserted = {"laser": manual_on, "other": False}
            self.streaming = False

        def snapshot(self):
            return SimpleNamespace(channel_states=copy.deepcopy(self.states))

        def is_streaming_active(self):
            return self.streaming

        def set_channel_intensity(self, name, value):
            self.states[name].intensity = value

        def set_channel_state(self, name, value, force_hardware=False):
            self.states[name].is_on = value
            if force_hardware or self.streaming:
                self._hardware_asserted[name] = value

        def set_channel_states_batch(self, states, force_hardware=False):
            for name, value in states.items():
                self.set_channel_state(name, value, force_hardware)

        def set_streaming_active(self, value):
            self.streaming = value
            for name, state in self.states.items():
                self._hardware_asserted[name] = state.is_on if value else False

        def turn_off_all_hardware_preserving_state(self):
            for name in self._hardware_asserted:
                self._hardware_asserted[name] = False

        def apply_observation_illumination(self, states, turn_on, force_hardware):
            assert force_hardware and not self.streaming
            for state in states:
                self.set_channel_intensity(state.illumination_channel, state.intensity)
                self.set_channel_state(state.illumination_channel, turn_on, force_hardware)

    ic = Illumination()
    hw.scope.illumination_controller = hw.illumination = ic
    hw.scope.obs_controller.current_observation_state = SimpleNamespace(
        active_illuminator_states=[SimpleNamespace(illumination_channel="laser", intensity=50)],
        is_waveform_driven=False,
    )
    read = hw.camera.read_camera_frame

    def read_lit_frame():
        assert ic._hardware_asserted["laser"]
        return read()

    hw.camera.read_camera_frame = read_lit_frame
    for _ in range(2):
        with hw:
            hw.capture(0)
            assert not any(ic._hardware_asserted.values())
        assert ic.states["laser"].intensity == 7 and not ic.states["laser"].is_on
        assert ic.states["other"].intensity == 20 and ic.states["other"].is_on
        assert ic._hardware_asserted == {"laser": manual_on, "other": False}
        assert not ic.streaming
    # A subsequent ordinary live interaction sees the user's original logical
    # channel choices, not those temporarily asserted by the calibration.
    ic.set_streaming_active(True)
    assert ic._hardware_asserted == {"laser": False, "other": True}


def test_originally_disabled_callbacks_and_stopped_stream_remain_stopped():
    hw, _, _ = hardware(initialized=False)
    hw.live.is_live = hw.focus_live.is_live = False
    hw.camera.streaming = hw.camera.callbacks = False
    with hw:
        hw.capture(0)
    assert not hw.camera.streaming and not hw.camera.callbacks
    assert not hw.live.is_live and not hw.focus_live.is_live


def test_last_live_timer_scheduled_during_stop_is_also_drained():
    hw, _, _ = hardware(initialized=False)
    stopped, fired = threading.Event(), threading.Event()

    def in_flight_trigger():
        stopped.wait(2)
        hw.live.timer_trigger = threading.Timer(5, fired.set)
        hw.live.timer_trigger.start()

    timer = threading.Thread(target=in_flight_trigger)
    hw.live.timer_trigger = timer
    original_stop = hw.live.stop_live

    def stop():
        original_stop()
        stopped.set()

    hw.live.stop_live = stop
    timer.start()
    with hw:
        assert hw.live.timer_trigger is None
    assert not fired.is_set()


@pytest.mark.parametrize("kwargs", [{"dither_fraction": 1}, {"focus_steps": 6}, {"z_baseline_um": 0}])
def test_invalid_motion_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        AlignmentSettings(**kwargs).validate()
