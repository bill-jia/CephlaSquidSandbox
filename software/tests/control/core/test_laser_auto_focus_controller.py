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

from control.core.laser_auto_focus_controller import LaserAutofocusController

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
    freshness check is exercised."""

    def __init__(self, render_fn):
        self._render = render_fn
        self._frame_id = 0
        self._pending_trigger = False
        self._latest = None
        self._callbacks_enabled = True
        self.trigger_count = 0

    def get_frame_id(self):
        return self._frame_id

    def send_trigger(self, exposure_time=None):
        self._pending_trigger = True
        self.trigger_count += 1

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
        pixel_to_um_calibration_distance=6.0,
    )
    updates.update(config_updates)
    controller.laser_af_properties = controller.laser_af_properties.model_copy(update=updates)
    controller.is_initialized = True
    return controller


def apply_reference_at_current_position(controller):
    reference = controller.capture_reference()
    assert reference is not None
    controller.apply_reference(reference)


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
