import contextlib
import os
import time
from typing import Any, List, Optional, Tuple

import cv2
from datetime import datetime
import math
import numpy as np
from qtpy.QtCore import QObject, Signal

from control import utils
import control._def
from control.core.config import ConfigRepository
from control.core.live_controller import LiveController
from control.core.objective_store import ObjectiveStore
from control.microcontroller import Microcontroller
from control.piezo import PiezoStage
from control.models import LaserAFConfig, LaserAFReference
from squid.abc import AbstractCamera, AbstractStage
import squid.logging

# Waiting longer than this in total for a frame newer than the last-seen frame
# id means the focus camera is not delivering (not streaming / hard fault).
# This is an overall cap across all of get_new_frame's re-trigger attempts.
FRESH_FRAME_TIMEOUT_S = 1.0
# The Daheng focus camera occasionally drops a software trigger (~0.3% of
# triggers in wellplate runs) and never produces a frame for it. get_new_frame
# re-sends the trigger rather than waiting out the full FRESH_FRAME_TIMEOUT_S,
# up to this many total attempts.
LASER_AF_TRIGGER_ATTEMPTS = 3
# Closed-loop correction bound for move_to_target. With an accurate
# pixel_to_um one iteration converges; a moderate scale error (< 2x) converges
# geometrically; a diverging correction aborts long before this bound.
MOVE_TO_TARGET_MAX_ITERATIONS = 5
# Calibration sweep: sample count over pixel_to_um_calibration_distance, and
# acceptance gates on the linear fit.
CALIBRATION_POSITIONS = 5
CALIBRATION_MIN_R2 = 0.90
CALIBRATION_MIN_TOTAL_PX = 5.0
# Total spot motion below this over the whole sweep means a static (simulated)
# camera image; fall back to the legacy canned scale instead of failing.
CALIBRATION_SIMULATION_PX = 0.5
# Failed-detection frames kept under <log dir>/laser_af_debug for post-mortem.
DEBUG_IMAGE_KEEP = 20


class LaserAutofocusController(QObject):
    image_to_display = Signal(np.ndarray)
    signal_displacement_um = Signal(float)
    signal_cross_correlation = Signal(float)
    signal_piezo_position_update = Signal()  # Signal to emit piezo position updates

    def __init__(
        self,
        microcontroller: Microcontroller,
        camera: AbstractCamera,
        liveController: LiveController,
        stage: AbstractStage,
        piezo: Optional[PiezoStage] = None,
        objectiveStore: Optional[ObjectiveStore] = None,
        af_laser_endpoint=None,
    ):
        QObject.__init__(self)
        self._log = squid.logging.get_logger(__class__.__name__)
        self.microcontroller = microcontroller
        self.camera: AbstractCamera = camera
        self.liveController: LiveController = liveController
        self.stage = stage
        self.piezo = piezo
        self.objectiveStore = objectiveStore
        self.characterization_mode = control._def.LASER_AF_CHARACTERIZATION_MODE
        self._af_laser_ep = af_laser_endpoint

        self.is_initialized = False

        self.laser_af_properties = LaserAFConfig()
        self.reference_crop = None

        self.spot_spacing_pixels = None  # spacing between the spots from the two interfaces (unit: pixel)

        self.image = None  # for saving the focus camera image for debugging when centroid cannot be found

        # Optional TimingManager for fine-grained profiling of move_to_target and
        # its sub-steps. None means timers are no-ops. MultiPointWorker attaches
        # its TimingManager here for the duration of acquisition.
        self._timing: Optional[Any] = None

        # Load configurations if available
        self.load_cached_configuration()

    def _time(self, name: str):
        """Context manager that records elapsed time under ``name`` when a
        TimingManager has been attached to ``self._timing``; otherwise a no-op."""
        if self._timing is None:
            return contextlib.nullcontext()
        return self._timing.get_timer(name)

    def turn_on_AF_laser(self):
        """Turn on the AF laser via IO endpoint or direct MCU call."""
        with self._time("af:turn_on_AF_laser"):
            if self._af_laser_ep is not None:
                self._af_laser_ep.set_digital(True)
                self._af_laser_ep.wait()
            else:
                self.microcontroller.turn_on_AF_laser()
                self.microcontroller.wait_till_operation_is_completed()

    def turn_off_AF_laser(self):
        """Turn off the AF laser via IO endpoint or direct MCU call."""
        with self._time("af:turn_off_AF_laser"):
            if self._af_laser_ep is not None:
                self._af_laser_ep.set_digital(False)
                self._af_laser_ep.wait()
            else:
                self.microcontroller.turn_off_AF_laser()
                self.microcontroller.wait_till_operation_is_completed()

    @property
    def _config_repo(self) -> ConfigRepository:
        """Access ConfigRepository via LiveController's microscope."""
        return self.liveController.microscope.config_repo

    @property
    def _current_profile(self) -> Optional[str]:
        """Get current profile from ConfigRepository."""
        return self._config_repo.current_profile

    def initialize_manual(self, config: LaserAFConfig) -> None:
        """Initialize laser autofocus with manual parameters."""
        # x_reference needs adjustment only if set
        x_ref_adjusted = config.x_reference - config.x_offset if config.x_reference is not None else None
        adjusted_config = config.model_copy(
            update={
                "x_reference": x_ref_adjusted,  # self.x_reference is relative to the cropped region
                "x_offset": int((config.x_offset // 8) * 8),
                "y_offset": int((config.y_offset // 2) * 2),
                "width": int((config.width // 8) * 8),
                "height": int((config.height // 2) * 2),
            }
        )

        self.laser_af_properties = adjusted_config

        if self.laser_af_properties.has_reference:
            self.reference_crop = self.laser_af_properties.reference_image_cropped

        self.camera.set_region_of_interest(
            self.laser_af_properties.x_offset,
            self.laser_af_properties.y_offset,
            self.laser_af_properties.width,
            self.laser_af_properties.height,
        )

        self.is_initialized = True

        # Update cache if objective store and profile is available
        if self.objectiveStore and self._current_profile and self.objectiveStore.current_objective:
            updated_config = LaserAFConfig(**config.model_dump())
            self._config_repo.save_laser_af_config(
                self._current_profile, self.objectiveStore.current_objective, updated_config
            )

    def load_cached_configuration(self):
        """Load configuration from the cache if available."""
        self._log.info(f"Loading cached configuration for profile: {self._current_profile}")
        if not self._current_profile:
            return

        current_objective = self.objectiveStore.current_objective if self.objectiveStore else None
        if not current_objective:
            return

        config = self._config_repo.get_laser_af_config(current_objective)
        if config is None:
            return
        # self._log.info(f"Loaded cached configuration successfully: {config}")

        # Update camera settings
        self.camera.set_exposure_time(config.focus_camera_exposure_time_ms)
        try:
            self.camera.set_analog_gain(config.focus_camera_analog_gain)
        except NotImplementedError:
            # Some camera drivers don't support analog gain; continue with existing gain
            self._log.debug(
                f"Focus camera does not support setting analog gain; "
                f"continuing with existing gain (requested: {config.focus_camera_analog_gain})"
            )

        # Initialize with loaded config
        self.initialize_manual(config)

    def initialize_auto(self) -> bool:
        """Automatically initialize laser autofocus by finding the spot and calibrating.

        This method:
        1. Finds the laser spot on full sensor
        2. Sets up ROI around the spot
        3. Calibrates pixel-to-um conversion using two z positions

        Returns:
            bool: True if initialization successful, False if any step fails
        """
        self.camera.set_region_of_interest(0, 0, 3088, 2064)

        # update camera settings
        self.camera.set_exposure_time(self.laser_af_properties.focus_camera_exposure_time_ms)
        try:
            self.camera.set_analog_gain(self.laser_af_properties.focus_camera_analog_gain)
        except NotImplementedError:
            pass

        # Find initial spot position
        self.turn_on_AF_laser()

        self._log.info("Finding laser spot for autofocus initialization using full sensor FOV")
        # Full-width search: the ROI/reference from a previous initialization
        # doesn't apply to the full-sensor readout used here.
        result = self._get_laser_spot_centroid(remove_background=True, restrict_to_reference=False)
        if result is None:
            self._log.error("Failed to find laser spot during initialization")
            self.turn_off_AF_laser()
            self.is_initialized = False
            return False
        x, y = result

        self.turn_off_AF_laser()

        # Set up ROI around spot and clear reference. Clamp offsets so the ROI
        # stays inside the full sensor (camera will reject offsets that push
        # offset + width past the sensor size).
        sensor_w, sensor_h = 3088, 2064
        roi_w = self.laser_af_properties.width
        roi_h = self.laser_af_properties.height
        x_offset = max(0.0, min(x - roi_w / 2, sensor_w - roi_w))
        y_offset = max(0.0, min(y - roi_h / 2, sensor_h - roi_h))
        config = self.laser_af_properties.model_copy(
            update={
                "x_offset": x_offset,
                "y_offset": y_offset,
                "has_reference": False,
            }
        )
        self.reference_crop = None
        config.set_reference_image(None)
        self._log.info(
            f"Laser spot location on the full sensor is ({int(x)}, {int(y)}); "
            f"ROI offset clamped to ({int(x_offset)}, {int(y_offset)})"
        )

        self.initialize_manual(config)

        # Calibrate pixel-to-um conversion
        if not self._calibrate_pixel_to_um():
            self._log.error("Failed to calibrate pixel-to-um conversion")
            # initialize_manual set is_initialized=True above; calibration failed,
            # so the system is not usable until re-initialized.
            self.is_initialized = False
            return False

        # Save configuration
        if self._current_profile:
            self._config_repo.save_laser_af_config(
                self._current_profile, self.objectiveStore.current_objective, self.laser_af_properties
            )

        return True

    def _calibrate_pixel_to_um(self) -> bool:
        """Calibrate the µm-of-Z-per-pixel scale of the spot's motion.

        Steps through CALIBRATION_POSITIONS z offsets spanning
        ``pixel_to_um_calibration_distance``, measures the spot at each, and
        least-squares fits x(z). The sweep descends once to the lowest offset
        (that downward move is backlash-compensated by the stage) and then only
        steps upward, so every sample is approached from the same direction —
        the old two-point (-d/2 then +d) scheme reversed direction mid-sweep,
        which let backlash/settling compress the measured span and inflate the
        scale several-fold. The fit is accepted only if the spot moved enough
        to measure (CALIBRATION_MIN_TOTAL_PX) and the fit is actually linear
        (CALIBRATION_MIN_R2), so a bad sweep fails loudly instead of writing a
        garbage scale that later wrecks every move_to_target.

        Returns:
            bool: True if calibration successful, False otherwise
        """
        try:
            self.turn_on_AF_laser()
        except TimeoutError:
            self._log.exception("Failed to turn on AF laser before pixel to um calibration, cannot continue!")
            return False

        span_um = self.laser_af_properties.pixel_to_um_calibration_distance
        offsets_um = np.linspace(-span_um / 2, span_um / 2, CALIBRATION_POSITIONS)

        measured_offsets_um: List[float] = []
        measured_xs: List[float] = []
        moved_um = 0.0
        try:
            for offset_um in offsets_um:
                self._move_z(offset_um - moved_um)
                moved_um = offset_um
                self._settle_after_move()
                result = self._get_laser_spot_centroid(restrict_to_reference=False)
                if result is None:
                    self._log.warning(f"No spot found at calibration offset {offset_um:+.1f} µm, skipping")
                    continue
                measured_offsets_um.append(float(offset_um))
                measured_xs.append(float(result[0]))
        finally:
            try:
                self.turn_off_AF_laser()
            except TimeoutError:
                self._log.exception(
                    "Error turning off AF laser after spot calibration acquisition.  Continuing in unknown state"
                )
            # move back to initial position
            self._move_z(-moved_um)
            self._settle_after_move()

        if len(measured_xs) < 3:
            self._log.error(
                f"Calibration failed: spot detected at only {len(measured_xs)}/{CALIBRATION_POSITIONS} z positions"
            )
            return False

        slope_px_per_um, intercept_px = np.polyfit(measured_offsets_um, measured_xs, 1)
        predicted = np.polyval((slope_px_per_um, intercept_px), measured_offsets_um)
        ss_res = float(np.sum((np.asarray(measured_xs) - predicted) ** 2))
        ss_tot = float(np.sum((np.asarray(measured_xs) - np.mean(measured_xs)) ** 2))
        r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        total_px = abs(slope_px_per_um) * (measured_offsets_um[-1] - measured_offsets_um[0])

        if total_px < CALIBRATION_SIMULATION_PX:
            # A static image (simulated camera) produces no spot motion at all.
            pixel_to_um = 0.4  # Simulation value
            self._log.warning("Using simulation value for pixel_to_um conversion")
        elif total_px < CALIBRATION_MIN_TOTAL_PX:
            self._log.error(
                f"Calibration failed: spot moved only {total_px:.1f} px over {span_um:.1f} µm — too little "
                f"signal to fit a scale. Increase the calibration distance."
            )
            return False
        elif r_squared < CALIBRATION_MIN_R2:
            self._log.error(
                f"Calibration failed: spot position vs z is not linear (R²={r_squared:.3f} over "
                f"{len(measured_xs)} points, spot moved {total_px:.1f} px). Suspect stage backlash, an "
                f"unstable spot, or detection artifacts — see laser_af_debug images."
            )
            return False
        else:
            pixel_to_um = 1.0 / slope_px_per_um
            self._log.info(
                f"Calibration fit over {len(measured_xs)} points: {total_px:.1f} px span, R²={r_squared:.3f}"
            )

        self._log.info(f"Pixel to um conversion factor is {pixel_to_um:.4f} um/pixel")
        calibration_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Update config with new calibration values
        self.laser_af_properties = self.laser_af_properties.model_copy(
            update={"pixel_to_um": pixel_to_um, "calibration_timestamp": calibration_timestamp}
        )

        # Update cache
        if self.objectiveStore and self._current_profile:
            self._config_repo.save_laser_af_config(
                self._current_profile, self.objectiveStore.current_objective, self.laser_af_properties
            )

        return True

    def _settle_after_move(self) -> None:
        """Wait out mechanical settling after a z move before measuring."""
        if self.piezo is not None:
            time.sleep(control._def.MULTIPOINT_PIEZO_DELAY_MS / 1000)
        else:
            time.sleep(control._def.SCAN_STABILIZATION_TIME_MS_Z / 1000)

    def set_laser_af_properties(self, updates: dict) -> None:
        """Update laser autofocus properties. Used for updating settings from GUI."""
        self.laser_af_properties = self.laser_af_properties.model_copy(update=updates)
        self.is_initialized = False

    def update_threshold_properties(self, updates: dict) -> None:
        """Update threshold properties. Save settings without re-initializing."""
        self.laser_af_properties = self.laser_af_properties.model_copy(update=updates)
        if self._current_profile and self.objectiveStore:
            self._config_repo.save_laser_af_config(
                self._current_profile, self.objectiveStore.current_objective, self.laser_af_properties
            )
        self._log.info("Updated threshold properties")

    def measure_displacement(self) -> float:
        """Measure the displacement of the laser spot from the reference position.

        Returns:
            float: Displacement in micrometers, or float('nan') if measurement fails
        """
        with self._time("af:measure_displacement"):
            try:
                self.turn_on_AF_laser()
            except TimeoutError:
                self._log.exception("Turning on AF laser timed out, failed to measure displacement.")
                self.signal_displacement_um.emit(float("nan"))
                return float("nan")

            try:
                return self._measure_displacement_with_laser_on()
            finally:
                try:
                    self.turn_off_AF_laser()
                except TimeoutError:
                    self._log.exception(
                        "Turning off AF laser timed out!  We got a displacement but laser may still be on."
                    )
                    # Continue with the measurement, but we're essentially in an unknown / weird state here.

    def _measure_displacement_with_laser_on(self) -> float:
        """:meth:`measure_displacement` without the laser on/off bracketing.

        For callers that hold the AF laser on across several measurements
        (move_to_target's correction loop) — each MCU toggle costs ~10 ms.
        """

        def finish_with(um: float) -> float:
            self.signal_displacement_um.emit(um)
            return um

        result = self._get_laser_spot_centroid()

        if result is None:
            self._log.error("Failed to detect laser spot during displacement measurement")
            return finish_with(float("nan"))  # Signal invalid measurement

        if self.laser_af_properties.x_reference is None:
            self._log.warning("Cannot calculate displacement - reference position not set")
            return finish_with(float("nan"))

        x, y = result
        displacement_um = (x - self.laser_af_properties.x_reference) * self.laser_af_properties.pixel_to_um
        return finish_with(displacement_um)

    def move_to_target(self, target_um: float) -> bool:
        """Move the stage to reach a target displacement from reference position.

        The correction is closed-loop: measure, move, re-measure, up to
        MOVE_TO_TARGET_MAX_ITERATIONS times until the residual is within
        ``displacement_success_window_um``, then a cross-correlation check
        verifies the spot matches the reference. A correction that fails to
        shrink the residual is a diverging loop — the configured ``pixel_to_um``
        does not match the spot's actual response (miscalibration) — so it
        aborts, rolls z back to the starting position, and logs the implied
        true scale to make recalibration actionable.

        Args:
            target_um: Target displacement in micrometers

        Returns:
            bool: True if move was successful, False if measurement failed or displacement was out of range
        """
        with self._time("af:move_to_target"):
            props = self.laser_af_properties
            if not props.has_reference:
                self._log.warning("Cannot move to target - reference not set")
                return False

            try:
                self.turn_on_AF_laser()
            except TimeoutError:
                self._log.exception("Turning on AF laser timed out, cannot move to target.")
                return False

            total_moved_um = 0.0
            try:
                current_um = self._measure_displacement_with_laser_on()
                self._log.debug(f"Current laser AF displacement: {current_um:.1f} μm")

                if math.isnan(current_um):
                    self._log.error("Cannot move to target: failed to measure current displacement")
                    return False

                if abs(current_um) > props.laser_af_range:
                    self._log.warning(
                        f"Measured displacement ({current_um:.1f} μm) is unreasonably large, using previous z position"
                    )
                    return False

                window_um = max(props.displacement_success_window_um, 1e-3)
                iterations = 0
                while abs(target_um - current_um) > window_um and iterations < MOVE_TO_TARGET_MAX_ITERATIONS:
                    iterations += 1
                    um_to_move = target_um - current_um
                    self._move_z(um_to_move)
                    total_moved_um += um_to_move

                    new_um = self._measure_displacement_with_laser_on()
                    if math.isnan(new_um):
                        self._log.error(
                            f"Lost the laser spot after a {um_to_move:+.1f} μm correction "
                            f"(iteration {iterations}); rolling back to the starting z"
                        )
                        self._rollback_z(total_moved_um)
                        return False

                    # A correction that doesn't shrink the residual (beyond noise)
                    # means the configured scale doesn't match the spot's response.
                    new_residual = abs(target_um - new_um)
                    if new_residual >= abs(um_to_move) and new_residual > 3 * window_um:
                        measured_response_um = new_um - current_um
                        implied_scale = (
                            props.pixel_to_um * um_to_move / measured_response_um
                            if abs(measured_response_um) > 1e-6
                            else float("nan")
                        )
                        self._log.error(
                            f"Laser AF correction diverged: commanded a {um_to_move:+.1f} μm z move but measured "
                            f"displacement went {current_um:.1f} → {new_um:.1f} μm. pixel_to_um is likely "
                            f"miscalibrated (configured {props.pixel_to_um:.4f} μm/px, spot response implies "
                            f"≈{implied_scale:.4f} μm/px) — recalibrate the laser AF. Rolling back to the starting z."
                        )
                        self._rollback_z(total_moved_um)
                        return False

                    current_um = new_um

                if abs(target_um - current_um) > window_um:
                    self._log.warning(
                        f"Laser AF residual {target_um - current_um:+.2f} μm still outside ±{window_um:.2f} μm "
                        f"after {iterations} corrections; accepting if cross-correlation verifies"
                    )

                # Verify using cross-correlation that spot is in same location as reference
                cc_result, correlation = self._verify_spot_alignment_with_laser_on()
                self.signal_cross_correlation.emit(correlation)
                if not cc_result:
                    self._log.warning("Cross correlation check failed - spots not well aligned")
                    # move back to the starting position
                    self._rollback_z(total_moved_um)
                    return False

                self._log.debug(
                    f"Moved to target: displacement {current_um:.2f} μm (target {target_um:.2f} μm) "
                    f"after {iterations} correction(s)"
                )
                return True
            finally:
                try:
                    self.turn_off_AF_laser()
                except TimeoutError:
                    self._log.exception("Failed to turn off AF laser after move_to_target, laser in unknown state!")

    def _move_z(self, um_to_move: float) -> None:
        with self._time("af:move_z"):
            if self.piezo is not None:
                # TODO: check if um_to_move is in the range of the piezo
                self.piezo.move_relative(um_to_move)
                self.signal_piezo_position_update.emit()
            else:
                self.stage.move_z(um_to_move / 1000)

    def _rollback_z(self, total_moved_um: float) -> None:
        """Undo the net z motion of a failed correction sequence."""
        if total_moved_um != 0.0:
            self._move_z(-total_moved_um)

    def _normalized_spot_crop(self, image: np.ndarray, x: float) -> np.ndarray:
        """Crop the spot region around ``x`` (vertically centered) and normalize it.

        Mean-subtracted and scaled by max, matching the form
        :meth:`_verify_spot_alignment` compares against.
        """
        center_y = int(image.shape[0] / 2)
        half = self.laser_af_properties.spot_crop_size // 2
        x_start = max(0, int(x) - half)
        x_end = min(image.shape[1], int(x) + half)
        y_start = max(0, center_y - half)
        y_end = min(image.shape[0], center_y + half)
        crop = image[y_start:y_end, x_start:x_end].astype(np.float32)
        return (crop - np.mean(crop)) / np.max(crop)

    def capture_reference(self) -> Optional[LaserAFReference]:
        """Measure the current spot and return it as a :class:`LaserAFReference`.

        Pure capture: does not mutate the controller's active reference, the live
        ``reference_crop``, or the per-objective cache. Used to snapshot a focus
        target for one region without disturbing the global reference. Returns
        ``None`` if not initialized or spot detection fails.
        """
        if not self.is_initialized:
            self._log.error("Laser autofocus is not initialized, cannot capture reference")
            return None

        try:
            self.turn_on_AF_laser()
        except TimeoutError:
            self._log.exception("Failed to turn on AF laser for reference capture!")
            return None

        # Full-width search: this call establishes a NEW reference, so windowing
        # the search around the previous one would defeat re-referencing.
        result = self._get_laser_spot_centroid(restrict_to_reference=False)
        reference_image = self.image

        try:
            self.turn_off_AF_laser()
        except TimeoutError:
            self._log.exception("Failed to turn off AF laser after capturing reference, laser is in an unknown state!")
            # Continue on since we got our reading, but the system is potentially in a weird state!

        if result is None or reference_image is None:
            self._log.error("Failed to detect laser spot while capturing reference")
            return None

        x, _ = result
        crop = self._normalized_spot_crop(reference_image, x)
        self._log.info(f"Captured laser AF reference at x={x:.1f}")
        return LaserAFReference.from_capture(x_reference=x, crop=crop)

    def apply_reference(self, reference: LaserAFReference) -> None:
        """Make ``reference`` the controller's active focus target.

        Sets ``x_reference`` and the live ``reference_crop`` (used by displacement
        measurement and cross-correlation verification) to exactly what
        ``reference`` carries — including a ``None`` crop. This is deterministic
        on purpose: it must NOT inherit whatever crop a previously applied
        reference left active, or one region's verification image could leak into
        another's. Crop fallback (borrowing the global crop for a spot-only
        reference) is the caller's responsibility — see
        ``MultiPointWorker._resolve_region_laser_af_reference``.
        """
        self.reference_crop = reference.reference_crop
        self.laser_af_properties = self.laser_af_properties.model_copy(
            update={"x_reference": reference.x_reference, "has_reference": True}
        )

    def get_active_reference(self) -> Optional[LaserAFReference]:
        """Snapshot the controller's current active reference, or ``None`` if unset."""
        if not self.laser_af_properties.has_reference or self.laser_af_properties.x_reference is None:
            return None
        return LaserAFReference.from_capture(self.laser_af_properties.x_reference, self.reference_crop)

    def set_reference(self) -> bool:
        """Set the current spot position as the global reference position.

        Captures the spot position and cropped reference image, makes it the
        active reference, and persists it to the per-objective cache.

        Returns:
            bool: True if reference was set successfully, False if spot detection failed
        """
        reference = self.capture_reference()
        if reference is None:
            return False

        self.apply_reference(reference)
        self.signal_displacement_um.emit(0)

        x = reference.x_reference
        self._log.info(f"Set reference position to x={x:.1f}")

        # Update cached file. reference_crop needs to be saved.
        if self._current_profile and self.objectiveStore:
            # Create config for saving with reference image encoded. The cache
            # stores the absolute-sensor x (x + x_offset); load adjusts it back.
            save_config = self.laser_af_properties.model_copy(
                update={"x_reference": x + self.laser_af_properties.x_offset, "has_reference": True}
            )
            save_config.set_reference_image(self.reference_crop)
            self._config_repo.save_laser_af_config(
                self._current_profile, self.objectiveStore.current_objective, save_config
            )

        self._log.info("Reference spot position set")

        return True

    def on_settings_changed(self) -> None:
        """Handle objective change or profile load event.

        This method is called when the objective changes. It resets the initialization
        status and loads the cached configuration for the new objective.
        """
        self.is_initialized = False
        self.load_cached_configuration()

    def _verify_spot_alignment(self) -> Tuple[bool, np.array]:
        """Verify laser spot alignment using cross-correlation with reference image.

        Captures current laser spot image and compares it with the reference image
        using normalized cross-correlation. Images are cropped around the expected
        spot location and normalized by maximum intensity before comparison.

        Returns:
            bool: True if spots are well aligned (correlation > CORRELATION_THRESHOLD), False otherwise
        """
        failure_return_value = False, float("nan")

        try:
            self.turn_on_AF_laser()
        except TimeoutError:
            self._log.exception("Failed to turn on AF laser for verifying spot alignment.")
            return failure_return_value

        try:
            return self._verify_spot_alignment_with_laser_on()
        finally:
            try:
                self.turn_off_AF_laser()
            except TimeoutError:
                self._log.exception("Failed to turn off AF laser after verifying spot alignment, laser in unknown state!")
                # Continue on because we got a reading, but the system is in a potentially weird and unknown state here.

    def _verify_spot_alignment_with_laser_on(self) -> Tuple[bool, float]:
        """:meth:`_verify_spot_alignment` without the laser on/off bracketing
        (for move_to_target, which holds the laser on across its whole loop)."""
        failure_return_value = False, float("nan")

        with self._time("af:verify_spot_alignment"):
            # Get current spot image
            self._get_laser_spot_centroid()
            current_image = self.image

            if self.reference_crop is None:
                self._log.warning("No reference crop stored")
                return failure_return_value

            if current_image is None:
                self._log.error("Failed to get images for cross-correlation check")
                return failure_return_value

            if self.laser_af_properties.x_reference is None:
                self._log.error("Cannot verify spot alignment - reference position not set")
                return failure_return_value

            with self._time("af:verify_cross_correlation"):
                # Crop and normalize current image
                center_x = int(self.laser_af_properties.x_reference)
                center_y = int(current_image.shape[0] / 2)

                x_start = max(0, center_x - self.laser_af_properties.spot_crop_size // 2)
                x_end = min(current_image.shape[1], center_x + self.laser_af_properties.spot_crop_size // 2)
                y_start = max(0, center_y - self.laser_af_properties.spot_crop_size // 2)
                y_end = min(current_image.shape[0], center_y + self.laser_af_properties.spot_crop_size // 2)

                current_crop = current_image[y_start:y_end, x_start:x_end].astype(np.float32)
                crop_max = float(np.max(current_crop))
                if crop_max <= 0 or current_crop.size != self.reference_crop.size:
                    self._log.warning(
                        f"Cross correlation check failed - crop unusable "
                        f"(max={crop_max:.1f}, size={current_crop.size} vs reference {self.reference_crop.size})"
                    )
                    return failure_return_value
                current_norm = (current_crop - np.mean(current_crop)) / crop_max

                # Calculate normalized cross correlation
                correlation = float(np.corrcoef(current_norm.ravel(), self.reference_crop.ravel())[0, 1])

            self._log.debug(f"Cross correlation with reference: {correlation:.3f}")

            # A NaN correlation (e.g. a flat crop) must fail, not slip past the
            # threshold comparison below (nan < x is False).
            if not math.isfinite(correlation) or correlation < self.laser_af_properties.correlation_threshold:
                self._log.warning(
                    f"Cross correlation check failed - spots not well aligned "
                    f"(correlation={correlation:.3f}, threshold={self.laser_af_properties.correlation_threshold})"
                )
                return False, correlation

            return True, correlation

    def get_new_frame(self) -> Optional[np.ndarray]:
        """Trigger the focus camera and return a frame newer than the last one.

        ``read_frame``/``read_camera_frame`` may serve a cached frame captured
        *before* the trigger (or before a preceding z move) if one arrived
        recently — DefaultCamera keeps a stale-frame fast path whose window
        includes a strobe estimate. Frame ids are compared so a stale frame is
        never accepted; without this, "averaging" reads the same frame
        repeatedly and post-move measurements can see pre-move data.

        The focus camera occasionally drops a software trigger and never
        produces a frame for it. Rather than wait out the full
        FRESH_FRAME_TIMEOUT_S for a frame that will never arrive, each attempt
        only waits a window sized from the camera's own frame time before
        re-sending the trigger, up to LASER_AF_TRIGGER_ATTEMPTS total.

        IMPORTANT: This assumes that the autofocus laser is already on!
        Returns None on timeout.
        """
        with self._time("af:get_new_frame"):
            last_frame_id = self.camera.get_frame_id()
            per_attempt_s = max(
                3 * (self.camera.get_exposure_time() + self.camera.get_strobe_time()) / 1000.0, 0.05
            )
            start_time = time.time()
            overall_deadline = start_time + FRESH_FRAME_TIMEOUT_S
            for attempt in range(1, LASER_AF_TRIGGER_ATTEMPTS + 1):
                with self._time("af:send_trigger"):
                    self.camera.send_trigger(self.camera.get_exposure_time())
                with self._time("af:read_frame"):
                    attempt_deadline = min(time.time() + per_attempt_s, overall_deadline)
                    while time.time() < attempt_deadline:
                        camera_frame = self.camera.read_camera_frame()
                        if camera_frame is not None and camera_frame.frame_id != last_frame_id:
                            if attempt > 1:
                                self._log.warning(
                                    f"Focus camera dropped a trigger; frame arrived on attempt "
                                    f"{attempt}/{LASER_AF_TRIGGER_ATTEMPTS}"
                                )
                            return camera_frame.frame
                        if camera_frame is None and not self.camera.get_is_streaming():
                            self._log.error("Focus camera is not streaming; cannot acquire a laser AF frame")
                            return None
                        time.sleep(0.001)
            self._log.warning(
                f"Focus camera dropped {LASER_AF_TRIGGER_ATTEMPTS} consecutive triggers; gave up after "
                f"{time.time() - start_time:.2f} s waiting for a fresh frame"
            )
            return None

    def _spot_search_range(self) -> Optional[Tuple[float, float]]:
        """Column window the spot must lie in to be usable, or None for full-width.

        With a reference set, only a spot within ``laser_af_range`` of it can be
        corrected to, so anything much further out is an artifact by definition.
        The window is 1.5x the range in pixels (a moderately out-of-range spot is
        still measured and reported as a real displacement rather than "not
        found") plus the spot-pair spacing (so the DUAL_* companion spot stays in
        view). Without this, the DUAL_* rightmost/leftmost-peak selection latches
        onto reflections or normalized noise anywhere in the frame whenever the
        true spot is dim or displaced.
        """
        props = self.laser_af_properties
        if not props.has_reference or props.x_reference is None or not props.pixel_to_um:
            return None
        range_px = abs(props.laser_af_range / props.pixel_to_um)
        half_window = 1.5 * range_px + props.spot_spacing
        return (props.x_reference - half_window, props.x_reference + half_window)

    def _get_laser_spot_centroid(
        self,
        remove_background: bool = False,
        use_center_crop: Optional[Tuple[int, int]] = None,
        restrict_to_reference: bool = True,
    ) -> Optional[Tuple[float, float]]:
        """Get the centroid location of the laser spot.

        Detects the spot on ``laser_af_averaging_n`` distinct frames (frame ids
        are enforced by :meth:`get_new_frame`) and returns the median position,
        which tolerates a single artifact detection. With
        ``restrict_to_reference`` (default) and a reference set, the search is
        windowed around the reference position — callers that establish a new
        reference or scan the full sensor pass False.

        Returns:
            Optional[Tuple[float, float]]: (x,y) coordinates of spot centroid, or None if detection fails
        """
        # Don't feed measurement frames to the live display / stream handler,
        # but restore the previous state afterwards (this used to permanently
        # disable focus-camera callbacks).
        callbacks_were_enabled = self.camera.get_callbacks_enabled()
        self.camera.enable_callbacks(False)

        # Clear the debug frame so a total read failure doesn't leave a stale
        # image masquerading as the current one (in debug saves and in
        # _verify_spot_alignment's crop).
        self.image = None

        xs: List[float] = []
        ys: List[float] = []
        search_range = (
            self._spot_search_range() if (restrict_to_reference and use_center_crop is None) else None
        )
        n_frames = self.laser_af_properties.laser_af_averaging_n

        try:
            with self._time("af:spot_centroid_loop"):
                for i in range(n_frames):
                    try:
                        with self._time("af:spot_centroid_loop:get_frame"):
                            image = self.get_new_frame()
                        if image is None:
                            self._log.warning(f"Failed to read frame {i + 1}/{n_frames}")
                            continue
                        self.image = image  # store for debugging and cross-correlation checks

                        with self._time("af:spot_centroid_loop:calculations"):
                            full_height, full_width = image.shape[:2]

                            if use_center_crop is not None:
                                image = utils.crop_image(image, use_center_crop[0], use_center_crop[1])

                            if remove_background:
                                # remove background using top hat filter
                                kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (50, 50))  # TODO: tmp hard coded value
                                image = cv2.morphologyEx(image, cv2.MORPH_TOPHAT, kernel)

                            # calculate centroid
                            spot_detection_params = {
                                "y_window": self.laser_af_properties.y_window,
                                "x_window": self.laser_af_properties.x_window,
                                "min_peak_width": self.laser_af_properties.min_peak_width,
                                "min_peak_distance": self.laser_af_properties.min_peak_distance,
                                "min_peak_prominence": self.laser_af_properties.min_peak_prominence,
                                "spot_spacing": self.laser_af_properties.spot_spacing,
                            }
                            with self._time("af:find_spot_location"):
                                result = utils.find_spot_location(
                                    image,
                                    mode=self.laser_af_properties.get_spot_detection_mode(),
                                    params=spot_detection_params,
                                    filter_sigma=self.laser_af_properties.filter_sigma,
                                    x_search_range=search_range,
                                    min_intensity=self.laser_af_properties.min_spot_intensity,
                                )
                            if result is None:
                                self._log.warning(f"No spot detected in frame {i + 1}/{n_frames}")
                                continue

                            if use_center_crop is not None:
                                x, y = (
                                    result[0] + (full_width - use_center_crop[0]) // 2,
                                    result[1] + (full_height - use_center_crop[1]) // 2,
                                )
                            else:
                                x, y = result

                        xs.append(float(x))
                        ys.append(float(y))

                    except Exception as e:
                        self._log.error(f"Error processing frame {i + 1}/{n_frames}: {str(e)}")
                        continue
        finally:
            self.camera.enable_callbacks(callbacks_were_enabled)

        # optionally display the image
        if control._def.LASER_AF_DISPLAY_SPOT_IMAGE and self.image is not None:
            self.image_to_display.emit(self.image)

        if not xs:
            search_note = (
                f" within x∈[{search_range[0]:.0f}, {search_range[1]:.0f}] around the reference"
                if search_range is not None
                else ""
            )
            self._log.error(f"No laser spot detected in any of {n_frames} frames{search_note}")
            self._save_failure_debug_image("no-spot")
            return None

        x = float(np.median(xs))
        y = float(np.median(ys))
        spread_px = max(xs) - min(xs)
        if len(xs) >= 2 and spread_px > self.laser_af_properties.x_window:
            self._log.warning(
                f"Laser spot x scattered over {spread_px:.1f} px across {len(xs)} frames "
                f"(median {x:.1f}) — detections may include artifacts"
            )

        self._log.debug(f"Spot centroid found at ({x:.1f}, {y:.1f}) from {len(xs)} detections")
        return (x, y)

    def _save_failure_debug_image(self, tag: str) -> None:
        """Persist the last focus-camera frame after a failed detection.

        Detection failures in the field are otherwise undiagnosable — the log
        can only say "no spot" with no way to tell laser-off from artifact from
        gross defocus. Keeps the newest DEBUG_IMAGE_KEEP files under
        ``<log dir>/laser_af_debug``. Best effort; never raises.
        """
        if self.image is None:
            return
        try:
            directory = os.path.join(squid.logging.get_default_log_directory(), "laser_af_debug")
            os.makedirs(directory, exist_ok=True)
            filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{tag}.png"
            cv2.imwrite(os.path.join(directory, filename), self.image)
            existing = sorted(f for f in os.listdir(directory) if f.endswith(".png"))
            for old in existing[:-DEBUG_IMAGE_KEEP]:
                os.remove(os.path.join(directory, old))
        except Exception:
            self._log.exception("Failed to save laser AF debug image")

    def get_image(self) -> Optional[np.ndarray]:
        """Capture and display a single image from the laser autofocus camera.

        Turns the laser on, captures an image, displays it, then turns the laser off.

        Returns:
            Optional[np.ndarray]: The captured image, or None if capture failed
        """
        # turn on the laser
        try:
            self.turn_on_AF_laser()
        except TimeoutError:
            self._log.exception("Failed to turn on laser AF laser before get_image, cannot get image.")
            return None

        try:
            # send trigger, grab image and display image
            self.camera.send_trigger()
            image = self.camera.read_frame()

            if image is None:
                self._log.error("Failed to read frame in get_image")
                return None

            self.image_to_display.emit(image)
            return image

        except Exception as e:
            self._log.error(f"Error capturing image: {str(e)}")
            return None

        finally:
            # turn off the laser
            try:
                self.turn_off_AF_laser()
            except TimeoutError:
                self._log.exception("Failed to turn off AF laser after get_image!")
