"""Tests for LaserAutofocusController's closed-loop correction, calibration
fit, and fresh-frame acquisition.

A fake camera/stage pair models the true optical response: the spot's x
position moves with stage z at a configurable µm-per-pixel scale, so the
tests can express "the configured pixel_to_um is wrong by a factor k" and
assert how move_to_target / calibration behave — the exact failure mode that
broke multipoint laser AF in the field while live mode (operating at zero
displacement) looked fine.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from control.core.laser_auto_focus_controller import LASER_AF_TRIGGER_ATTEMPTS, LaserAutofocusController

IMAGE_H, IMAGE_W = 256, 1536
SPOT_X0 = 768.0
SPOT_Y = 128.0


class FakeStage:
    def __init__(self):
        self.z_um = 0.0

    def move_z(self, rel_mm, blocking=True):
        self.z_um += rel_mm * 1000.0


class FakeFocusCamera:
    """Minimal focus-camera stand-in with software-trigger semantics and a
    'latest frame' cache: reads without a new trigger return the same stale
    frame (mirroring DefaultCamera's fast path), so the controller's frame-id
    freshness check is exercised.

    ``drop_next_triggers`` lets a test simulate the Daheng camera dropping a
    software trigger: that many subsequent ``send_trigger`` calls are
    acknowledged (bumping ``trigger_count``) but never produce a new frame.
    """

    def __init__(self, render_fn):
        self._render = render_fn
        self._frame_id = 0
        self._pending_trigger = False
        self._latest = None
        self._callbacks_enabled = True
        self.trigger_count = 0
        self.drop_next_triggers = 0

    def get_frame_id(self):
        return self._frame_id

    def send_trigger(self, exposure_time=None):
        self.trigger_count += 1
        if self.drop_next_triggers > 0:
            self.drop_next_triggers -= 1
            return
        self._pending_trigger = True

    def read_camera_frame(self):
        if self._pending_trigger:
            self._pending_trigger = False
            self._frame_id += 1
            self._latest = SimpleNamespace(frame_id=self._frame_id, frame=self._render())
        return self._latest

    def get_is_streaming(self):
        return True

    def enable_callbacks(self, enabled):
        self._callbacks_enabled = enabled

    def get_callbacks_enabled(self):
        return self._callbacks_enabled

    def get_exposure_time(self):
        return 0.2

    def get_strobe_time(self):
        return 0.1


def render_spot(stage, true_um_per_px, x0=SPOT_X0, amplitude=220.0):
    """Image generator tying spot x to stage z through the TRUE optical scale."""

    def _render():
        x_pos = x0 + stage.z_um / true_um_per_px
        y, x = np.ogrid[:IMAGE_H, :IMAGE_W]
        img = amplitude * np.exp(-((x - x_pos) ** 2 + (y - SPOT_Y) ** 2) / (2 * 5.0**2))
        return np.clip(img, 0, 255).astype(np.uint8)

    return _render


def make_controller(camera, stage, **config_updates):
    live_controller = MagicMock()
    live_controller.microscope.config_repo.current_profile = None  # skip cache load/save
    controller = LaserAutofocusController(
        microcontroller=MagicMock(),
        camera=camera,
        liveController=live_controller,
        stage=stage,
        piezo=None,
        objectiveStore=None,
    )
    updates = dict(
        pixel_to_um=0.2,
        laser_af_range=100.0,
        laser_af_averaging_n=3,
        displacement_success_window_um=1.0,
        min_spot_intensity=10.0,
    )
    updates.update(config_updates)
    controller.laser_af_properties = controller.laser_af_properties.model_copy(update=updates)
    controller.is_initialized = True
    return controller


def apply_reference_at_current_position(controller):
    reference = controller.capture_reference()
    assert reference is not None
    controller.apply_reference(reference)


def test_verification_fast_path_does_not_register_y():
    stage = FakeStage()
    camera = FakeFocusCamera(render_spot(stage, 0.2))
    controller = make_controller(camera, stage)
    apply_reference_at_current_position(controller)
    controller._register_spot_y = MagicMock(side_effect=AssertionError("fast path must not register"))
    triggers = camera.trigger_count

    ok, correlation = controller._verify_spot_alignment_with_laser_on()

    assert ok and correlation > 0.99
    controller._register_spot_y.assert_not_called()
    assert camera.trigger_count - triggers == controller.laser_af_properties.laser_af_averaging_n


@pytest.mark.parametrize("shift_y", [-20, -9, 9, 20])
def test_verification_retries_y_on_the_same_frame(shift_y):
    stage = FakeStage()
    render = render_spot(stage, 0.2)
    camera = FakeFocusCamera(render)
    controller = make_controller(camera, stage)
    apply_reference_at_current_position(controller)
    camera._render = lambda: np.roll(render(), shift_y, axis=0)
    record = {}
    controller._validation_update = lambda **values: record.update(values)
    triggers = camera.trigger_count

    ok, correlation = controller._verify_spot_alignment_with_laser_on()

    assert ok and correlation > 0.99
    assert record["correlation_unregistered"] < 0.9
    assert record["y_registration_shift_px"] == shift_y
    assert camera.trigger_count - triggers == controller.laser_af_properties.laser_af_averaging_n
    assert stage.z_um == 0


@pytest.mark.parametrize("tolerance,shift_y", [(0, 9), (20, 40), (20, -40)])
def test_verification_y_retry_is_bounded_and_can_be_disabled(tolerance, shift_y):
    stage = FakeStage()
    render = render_spot(stage, 0.2)
    camera = FakeFocusCamera(render)
    controller = make_controller(camera, stage, correlation_y_tolerance_px=tolerance)
    apply_reference_at_current_position(controller)
    camera._render = lambda: np.roll(render(), shift_y, axis=0)

    ok, correlation = controller._verify_spot_alignment_with_laser_on()

    assert not ok and correlation < 0.9


def test_y_retry_does_not_rescue_out_of_tolerance_x():
    stage = FakeStage()
    render = render_spot(stage, 0.2)
    camera = FakeFocusCamera(render)
    controller = make_controller(camera, stage)
    apply_reference_at_current_position(controller)
    stage.z_um = 2.0  # 10 px in X, outside the 1 um displacement window.
    camera._render = lambda: np.roll(render(), 9, axis=0)
    controller._register_spot_y = MagicMock(side_effect=AssertionError("X failure must not register"))

    ok, _ = controller._verify_spot_alignment_with_laser_on()

    assert not ok
    controller._register_spot_y.assert_not_called()


def test_y_registration_never_translates_x():
    stage = FakeStage()
    render = render_spot(stage, 0.2)
    camera = FakeFocusCamera(render)
    controller = make_controller(camera, stage)
    apply_reference_at_current_position(controller)
    image = np.roll(np.roll(render(), 20, axis=1), 9, axis=0)

    correlation, shift = controller._register_spot_y(image, 718, 818, 78, 178)

    assert correlation < 0.9
    assert shift is None or abs(shift) <= 20


@pytest.mark.parametrize(
    "invalid", ["no_detection", "flat_reference", "flat_frame", "nonfinite_reference", "shape"]
)
def test_verification_does_not_register_invalid_evidence(invalid):
    stage = FakeStage()
    camera = FakeFocusCamera(render_spot(stage, 0.2))
    controller = make_controller(camera, stage)
    apply_reference_at_current_position(controller)
    if invalid == "no_detection":
        controller._get_laser_spot_centroid = MagicMock(return_value=None)
    elif invalid == "flat_reference":
        controller.reference_crop[:] = 1
    elif invalid == "nonfinite_reference":
        controller.reference_crop[0, 0] = np.nan
    elif invalid == "shape":
        controller.reference_crop = controller.reference_crop.reshape(50, 200)
    elif invalid == "flat_frame":
        controller.image = np.ones((IMAGE_H, IMAGE_W), dtype=np.uint8)
        controller._get_laser_spot_centroid = MagicMock(return_value=(SPOT_X0, SPOT_Y))
    controller._register_spot_y = MagicMock(side_effect=AssertionError("invalid evidence must not register"))

    ok, correlation = controller._verify_spot_alignment_with_laser_on()

    assert not ok and np.isnan(correlation)
    controller._register_spot_y.assert_not_called()


def test_y_registration_clips_search_to_camera_roi():
    stage = FakeStage()
    render = render_spot(stage, 0.2)
    controller = make_controller(FakeFocusCamera(render), stage)
    image = render()[75:185]  # Only 5 px of vertical margin on each side of the crop.
    controller.reference_crop = controller._normalized_spot_crop(image, SPOT_X0)
    shifted = np.roll(image, -4, axis=0)

    correlation, shift = controller._register_spot_y(shifted, 718, 818, 5, 105)

    assert correlation > 0.99 and shift == -4


@pytest.mark.parametrize("shift_y,expected_ok", [(-9, True), (40, False)])
def test_move_to_target_with_y_drift_keeps_or_rolls_back_correction(shift_y, expected_ok):
    stage = FakeStage()
    render = render_spot(stage, 0.2)
    camera = FakeFocusCamera(render)
    controller = make_controller(camera, stage)
    apply_reference_at_current_position(controller)
    stage.z_um = 25
    camera._render = lambda: np.roll(render(), shift_y, axis=0)

    assert controller.move_to_target(0) is expected_ok
    assert abs(stage.z_um - (0 if expected_ok else 25)) < 1


def test_af_laser_toggle_without_endpoint_uses_microcontroller():
    # Regression: with no IO endpoint these used to recurse infinitely.
    stage = FakeStage()
    controller = make_controller(FakeFocusCamera(render_spot(stage, 0.2)), stage)
    controller.turn_on_AF_laser()
    controller.microcontroller.turn_on_AF_laser.assert_called_once()
    controller.turn_off_AF_laser()
    controller.microcontroller.turn_off_AF_laser.assert_called_once()
    assert controller.microcontroller.wait_till_operation_is_completed.call_count == 2


def test_centroid_uses_distinct_frames_and_restores_callbacks():
    stage = FakeStage()
    camera = FakeFocusCamera(render_spot(stage, 0.2))
    controller = make_controller(camera, stage)

    result = controller._get_laser_spot_centroid(restrict_to_reference=False)

    assert result is not None
    assert abs(result[0] - SPOT_X0) < 2
    # One real trigger per averaged frame — a cached frame must never be
    # counted twice (that defeats averaging and can measure pre-move state).
    assert camera.trigger_count == controller.laser_af_properties.laser_af_averaging_n
    assert camera.get_frame_id() == controller.laser_af_properties.laser_af_averaging_n
    # Callback state must be restored (used to be left disabled forever).
    assert camera.get_callbacks_enabled() is True


def test_get_new_frame_retriggers_on_dropped_trigger():
    # The camera acks the first trigger but never produces a frame for it
    # (the ~0.3% dropped-software-trigger case) — get_new_frame must re-send
    # the trigger rather than burning the full timeout.
    stage = FakeStage()
    camera = FakeFocusCamera(render_spot(stage, 0.2))
    camera.drop_next_triggers = 1
    controller = make_controller(camera, stage)

    frame = controller.get_new_frame()

    assert frame is not None
    assert camera.trigger_count == 2


def test_get_new_frame_gives_up_after_max_attempts():
    # Every trigger is dropped: get_new_frame must give up after exactly
    # LASER_AF_TRIGGER_ATTEMPTS tries instead of hanging or retrying forever.
    stage = FakeStage()
    camera = FakeFocusCamera(render_spot(stage, 0.2))
    camera.drop_next_triggers = LASER_AF_TRIGGER_ATTEMPTS
    controller = make_controller(camera, stage)

    frame = controller.get_new_frame()

    assert frame is None
    assert camera.trigger_count == LASER_AF_TRIGGER_ATTEMPTS


def test_move_to_target_converges_with_accurate_scale():
    stage = FakeStage()
    controller = make_controller(FakeFocusCamera(render_spot(stage, 0.2)), stage, pixel_to_um=0.2)
    apply_reference_at_current_position(controller)

    stage.z_um = 30.0  # 30 µm true defocus
    assert controller.move_to_target(0) is True
    assert abs(stage.z_um) < 1.5


def test_move_to_target_converges_with_moderate_scale_error():
    # Configured scale 20% high: the closed loop shrinks the residual
    # geometrically and still lands within the success window.
    stage = FakeStage()
    controller = make_controller(FakeFocusCamera(render_spot(stage, 0.2)), stage, pixel_to_um=0.24)
    apply_reference_at_current_position(controller)

    stage.z_um = 30.0
    assert controller.move_to_target(0) is True
    assert abs(stage.z_um) < 2.0


def test_move_to_target_rolls_back_on_diverging_correction():
    # Configured scale 5x the true response — the field failure. The first
    # correction overshoots ~5x; the loop must detect the growing residual,
    # roll z back to the starting position, and fail instead of leaving z
    # hundreds of µm off target.
    stage = FakeStage()
    controller = make_controller(FakeFocusCamera(render_spot(stage, 0.2)), stage, pixel_to_um=1.0)
    apply_reference_at_current_position(controller)

    stage.z_um = 12.0
    assert controller.move_to_target(0) is False
    assert abs(stage.z_um - 12.0) < 1e-6


def test_move_to_target_rejects_out_of_range_displacement():
    stage = FakeStage()
    controller = make_controller(FakeFocusCamera(render_spot(stage, 0.2)), stage, pixel_to_um=0.2)
    apply_reference_at_current_position(controller)

    stage.z_um = 150.0  # beyond laser_af_range=100
    assert controller.move_to_target(0) is False
    assert abs(stage.z_um - 150.0) < 1e-6


def test_calibration_recovers_true_scale():
    stage = FakeStage()
    controller = make_controller(FakeFocusCamera(render_spot(stage, 0.2)), stage, pixel_to_um=1.0)

    assert controller._calibrate_pixel_to_um() is True
    assert abs(controller.laser_af_properties.pixel_to_um - 0.2) < 0.02
    # Sweep must return the stage to where it started.
    assert abs(stage.z_um) < 1e-6


def test_calibration_static_spot_falls_back_to_simulation_value():
    stage = FakeStage()

    def static_render():
        y, x = np.ogrid[:IMAGE_H, :IMAGE_W]
        img = 220.0 * np.exp(-((x - SPOT_X0) ** 2 + (y - SPOT_Y) ** 2) / (2 * 5.0**2))
        return np.clip(img, 0, 255).astype(np.uint8)

    controller = make_controller(FakeFocusCamera(static_render), stage, pixel_to_um=1.0)
    assert controller._calibrate_pixel_to_um() is True
    assert controller.laser_af_properties.pixel_to_um == 0.4  # legacy simulation value


def test_calibration_fails_on_garbage_detections():
    # Bright random noise: every frame "detects" a peak somewhere, but x has no
    # linear relation to z, so the fit gate must reject the sweep instead of
    # writing a garbage scale.
    stage = FakeStage()
    rng = np.random.default_rng(1234)

    def noise_render():
        return rng.integers(0, 100, size=(IMAGE_H, IMAGE_W)).astype(np.uint8)

    controller = make_controller(FakeFocusCamera(noise_render), stage, pixel_to_um=1.0)
    assert controller._calibrate_pixel_to_um() is False
    assert controller.laser_af_properties.pixel_to_um == 1.0  # unchanged


def test_measure_displacement_sign_and_magnitude():
    stage = FakeStage()
    controller = make_controller(FakeFocusCamera(render_spot(stage, 0.2)), stage, pixel_to_um=0.2)
    apply_reference_at_current_position(controller)

    stage.z_um = 20.0
    measured = controller.measure_displacement()
    assert abs(measured - 20.0) < 1.0

    stage.z_um = -20.0
    measured = controller.measure_displacement()
    assert abs(measured + 20.0) < 1.0


@pytest.mark.parametrize("error", [RuntimeError("verify failed"), BaseException("camera fatal")])
def test_verify_exception_restores_all_corrections(error):
    stage = FakeStage()
    controller = make_controller(FakeFocusCamera(render_spot(stage, 0.2)), stage, pixel_to_um=0.24)
    apply_reference_at_current_position(controller)
    stage.z_um = 30.0
    controller._verify_spot_alignment_with_laser_on = MagicMock(side_effect=error)
    if isinstance(error, Exception):
        assert controller.move_to_target(0) is False
    else:
        with pytest.raises(BaseException, match="camera fatal"):
            controller.move_to_target(0)
    assert abs(stage.z_um - 30.0) < 1e-6
    controller.microcontroller.turn_off_AF_laser.assert_called()


def test_laser_shutdown_retries_timeout_once():
    stage = FakeStage()
    controller = make_controller(FakeFocusCamera(render_spot(stage, 0.2)), stage)
    controller.microcontroller.wait_till_operation_is_completed.side_effect = [TimeoutError(), None]
    controller.turn_off_AF_laser()
    assert controller.microcontroller.turn_off_AF_laser.call_count == 2


def test_laser_shutdown_persistent_timeout_is_bounded():
    stage = FakeStage()
    controller = make_controller(FakeFocusCamera(render_spot(stage, 0.2)), stage)
    controller.microcontroller.wait_till_operation_is_completed.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        controller.turn_off_AF_laser()
    assert controller.microcontroller.turn_off_AF_laser.call_count == 2
