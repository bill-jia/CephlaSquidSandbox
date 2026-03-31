"""
Live image acquisition controller.

This module handles continuous image acquisition (live view) with:
- Software or hardware triggering
- Automatic illumination control synchronized with camera exposure
- Frame rate control
- Channel switching with automatic filter wheel control

The LiveController manages the timing and sequencing of:
1. Illumination turn-on
2. Camera trigger
3. Image readout
4. Illumination turn-off

This ensures proper synchronization between illumination and camera exposure
for both software-triggered and hardware-triggered acquisition modes.
"""

from __future__ import annotations

import time
import threading
from typing import List, Optional, TYPE_CHECKING

import squid.logging
from squid.abc import CameraAcquisitionMode, AbstractCamera
from control._def import *
from control.models.observation_state import (
    ObservationState,
    IlluminatorState,
    CameraSettings,
    ConfocalSettings,
)

if TYPE_CHECKING:
    from control.models import IlluminationChannelConfig


class LiveController:
    """
    Controller for live image acquisition.

    Manages continuous image streaming with proper illumination synchronization.
    Supports both software and hardware trigger modes.

    Software trigger mode:
    - Python code controls timing
    - Manual illumination on/off
    - Good for low frame rates (<10 fps)

    Hardware trigger mode:
    - Microcontroller controls timing
    - Synchronized illumination and camera trigger
    - Good for high frame rates (>10 fps)
    """
    def __init__(
        self,
        microscope: "Microscope",
        # NOTE(imo): Right now, Microscope needs to import LiveController.  So we can't properly annotate it here.
        camera: AbstractCamera,
        control_illumination: bool = True,
        use_internal_timer_for_hardware_trigger: bool = True,
        for_displacement_measurement: bool = False,
    ):
        """
        Initialize the live controller.

        Args:
            microscope: Microscope instance (for stage, illumination, etc.)
            camera: Camera to use for acquisition
            control_illumination: If True, automatically control illumination during acquisition (i.e. master camera, but not secondary or focus cameras)
            use_internal_timer_for_hardware_trigger: Use Python timer vs microcontroller timer
            for_displacement_measurement: If True, used for laser autofocus/displacement measurement
        """
        self._log = squid.logging.get_logger(self.__class__.__name__ + "/" + camera.__class__.__name__)
        self.microscope = microscope
        self.camera: AbstractCamera = camera
        self.current_observation_state: Optional[ObservationState] = None
        self.trigger_mode: Optional[TriggerMode] = TriggerMode.SOFTWARE  # @@@ change to None
        self.is_live = False
        self.in_acquisition = False
        self.control_illumination = control_illumination
        self.use_internal_timer_for_hardware_trigger = (
            use_internal_timer_for_hardware_trigger  # use Timer vs timer in the MCU
        )
        self.for_displacement_measurement = for_displacement_measurement

        # Frame rate control for software trigger mode
        self.fps_trigger = 1  # Target frames per second
        self.timer_trigger_interval = (1.0 / self.fps_trigger) * 1000  # Interval in milliseconds
        self._trigger_skip_count = 0  # Counter for skipped triggers (if camera is slow)
        self.timer_trigger: Optional[threading.Timer] = None  # Timer for periodic triggering

        self.trigger_ID = -1  # ID for tracking triggers

        # Frame rate monitoring
        self.fps_real = 0  # Actual measured frame rate
        self.counter = 0  # Frame counter
        self.timestamp_last = 0  # Timestamp of last frame

        self.display_resolution_scaling = 1  # Scaling factor for display (for performance)

        # Automatic filter wheel switching when changing channels
        self.enable_channel_auto_filter_switching: bool = True

        # Confocal mode state - when True, use confocal_override from acquisition configs
        self._confocal_mode: bool = False
        self._log.info(f"Initialized with control_illumination={control_illumination}")

    # ─────────────────────────────────────────────────────────────────────────────
    # Backward-compatible property for external callers
    # ─────────────────────────────────────────────────────────────────────────────

    @property
    def currentConfiguration(self) -> Optional[ObservationState]:
        """Backward-compatible alias for current_observation_state.

        .. deprecated:: Access ``current_observation_state`` directly instead.

        External code (widgets, workers) that reads ``liveController.currentConfiguration``
        will get the ObservationState object.  Because ObservationState exposes the same
        ``.name``, ``.exposure_time``, and ``.analog_gain`` properties that callers
        typically use, most read sites will work without changes.
        """
        return self.current_observation_state

    @currentConfiguration.setter
    def currentConfiguration(self, value: Optional[ObservationState]) -> None:
        self.current_observation_state = value
        self._log.info(f"currentConfiguration set to: {value.name}")
    # ─────────────────────────────────────────────────────────────────────────────
    # Confocal mode
    # ─────────────────────────────────────────────────────────────────────────────

    def toggle_confocal_widefield(self, confocal: bool) -> None:
        """Toggle between confocal and widefield modes.

        This only updates the internal state. Hardware control (spinning disk position)
        should be handled separately by the microscope or widget.

        Args:
            confocal: Whether to enable confocal mode
        """
        self._confocal_mode = bool(confocal)
        self._log.info(f"Imaging mode set to: {'confocal' if self._confocal_mode else 'widefield'}")

    def is_confocal_mode(self) -> bool:
        """Check if currently in confocal mode."""
        return self._confocal_mode

    def sync_confocal_mode_from_hardware(self, confocal: bool) -> None:
        """Sync confocal mode state from hardware.

        Called during initialization to sync state with actual hardware position.
        """
        self.toggle_confocal_widefield(confocal)

    # ─────────────────────────────────────────────────────────────────────────────
    # Channel configuration access
    # ─────────────────────────────────────────────────────────────────────────────

    def get_observation_states(self) -> List[ObservationState]:
        """Get observation states from the current profile's general.yaml.

        Returns:
            List of ObservationState objects. Returns empty list if no profile
            is set or no configs are available.
        """
        self._log.info("get_observation_states: getting observation states from general.yaml")
        config_repo = self.microscope.config_repo

        if config_repo.current_profile is None:
            self._log.warning("get_observation_states() returning empty list: no profile is set")
            return []

        general = config_repo.get_general_config()
        if not general:
            self._log.warning(
                f"get_observation_states() returning empty list: no general config for "
                f"profile '{config_repo.current_profile}'"
            )
            return []

        return list(general.observation_states)

    def get_observation_state_by_name(self, name: str) -> Optional[ObservationState]:
        """Get a specific observation state by name.

        Args:
            name: Observation state name to find

        Returns:
            ObservationState if found, None otherwise
        """
        self._log.info(f"get_observation_state_by_name: getting observation state by name: {name}")
        states = self.get_observation_states()
        return next((s for s in states if s.name == name), None)

    def set_active_observation_state(self, state: Optional[ObservationState]) -> None:
        """Record the selected observation state without touching camera or illumination hardware.

        The UI (e.g. Live Control) chooses a channel from the profile before ``set_observation_state`` runs.
        We intentionally skip ``set_observation_state`` at startup so restored camera cache settings are not
        overwritten, but contrast/LUT, Observation State collection, and display code still need a stable
        ``current_observation_state`` for channel *name* and preset bookkeeping.

        Observation State: ``collect_observation_state`` reads ``active_channel_name`` from
        ``current_observation_state``; keeping this reference in sync with the Live Control / Napari dropdown
        matches that paradigm without forcing a full hardware apply.
        """
        self.current_observation_state = state

    def set_active_channel_reference(self, configuration: Optional[ObservationState]) -> None:
        """Backward-compatible alias for set_active_observation_state.

        .. deprecated:: Use :meth:`set_active_observation_state` instead.
        """
        self.set_active_observation_state(configuration)

    def get_active_channel_name(self) -> Optional[str]:
        """Return the name of the current observation state, or None."""
        if self.current_observation_state is not None:
            return self.current_observation_state.name
        return None

    def get_channel_name_for_contrast(self) -> str:
        """Channel key for ContrastManager / display when only a logical selection exists."""
        if self.current_observation_state is not None:
            return self.current_observation_state.name
        states = self.get_observation_states()
        if states:
            return states[0].name
        return "default"

    # ─────────────────────────────────────────────────────────────────────────────
    # Illumination control
    # ─────────────────────────────────────────────────────────────────────────────

    def turn_on_illumination(self):
        """Turn on illumination for the current observation state's active illuminators.

        Hardware is only commanded when the streaming gate is active (live view)
        or when force_hardware is used by acquisition callers.
        """
        if self.current_observation_state is None:
            self._log.warning("turn_on_illumination() skipped - no observation state set")
            return

        active = self.current_observation_state.active_illuminator_states
        if not active:
            self._log.warning(
                f"turn_on_illumination() skipped - no active illuminators for "
                f"'{self.current_observation_state.name}'"
            )
            return

        self.microscope.illumination_controller.apply_observation_illumination(active, turn_on=True)

    def turn_off_illumination(self):
        """Turn off illumination for the current observation state's active illuminators."""
        if self.current_observation_state is None:
            self._log.warning("turn_off_illumination() skipped - no observation state set")
            return

        active = self.current_observation_state.active_illuminator_states
        if not active:
            self._log.warning(
                f"turn_off_illumination() skipped - no active illuminators for "
                f"'{self.current_observation_state.name}'"
            )
            return

        self.microscope.illumination_controller.apply_observation_illumination(active, turn_on=False)

    def update_illumination(self):
        """Set intensity/LED-matrix mode and optical-path configuration for the current observation state."""
        if self.current_observation_state is None:
            self._log.warning("update_illumination() called with no current_observation_state")
            return
        self._log.info("update_illumination: updating illumination")
        self._apply_illumination_parameters()
        self._log.info("update_illumination: applied illumination parameters")
        self._apply_optical_path()

    def _apply_illumination_parameters(self):
        """Set per-channel intensity, LED matrix mode, logical on/off, and NL5/CellX laser power.

        Iterates ALL illuminator states (not just active ones) so that the IC's
        logical state reflects the full observation state. This ensures the
        illumination GUI panel shows correct on/off and intensity for every channel.
        """
        ic = self.microscope.illumination_controller

        for ist in self.current_observation_state.illuminator_states:
            self._log.info(f"apply_illumination_parameters: {ist.illumination_channel} {ist.on} {ist.intensity}")
            mode = ist.led_matrix_mode
            if mode and getattr(ic, "has_unified_led_matrix", lambda: False)():
                ic.set_led_matrix_mode(mode)

            ic.set_channel_intensity(ist.illumination_channel, ist.intensity)
            # Sync the logical on/off state (hardware follows only if streaming gate is open).
            ic.set_channel_state(ist.illumination_channel, ist.on)

            # NL5 / CellX laser power forwarding (wavelength-specific accessories)
            if not ist.on:
                continue
            illum_config = self.microscope.config_repo.get_illumination_config()
            wavelength = None
            if illum_config:
                ch_def = illum_config.get_channel_by_name(ist.illumination_channel)
                if ch_def:
                    wavelength = ch_def.wavelength_nm

            if wavelength and self.microscope.addons.nl5 and NL5_USE_DOUT:
                self.microscope.addons.nl5.set_active_channel(NL5_WAVENLENGTH_MAP[wavelength])
                if NL5_USE_AOUT:
                    self.microscope.addons.nl5.set_laser_power(NL5_WAVENLENGTH_MAP[wavelength], int(ist.intensity))
                if self.microscope.addons.cellx and ENABLE_CELLX:
                    self.microscope.addons.cellx.set_laser_power(NL5_WAVENLENGTH_MAP[wavelength], int(ist.intensity))

    def _apply_optical_path(self):
        """Set emission filter positions and confocal iris values for the current observation state."""
        emission_filter_position = self.current_observation_state.emission_filter_positions.get("default")

        if ENABLE_SPINNING_DISK_CONFOCAL and self.microscope.addons.xlight and not USE_DRAGONFLY:
            try:
                if emission_filter_position is not None:
                    self.microscope.addons.xlight.set_emission_filter(
                        emission_filter_position,
                        extraction=False,
                        validate=XLIGHT_VALIDATE_WHEEL_POS,
                    )
            except Exception as e:
                self._log.warning(f"Not setting emission filter position: {e}")
            # Apply per-channel iris values
            hw_settings = self.current_observation_state.confocal_hardware_settings
            if hw_settings is not None:
                xlight = self.microscope.addons.xlight
                try:
                    if hw_settings.illumination_iris is not None and xlight.has_illumination_iris_diaphragm:
                        xlight.set_illumination_iris(int(hw_settings.illumination_iris))
                    if hw_settings.emission_iris is not None and xlight.has_emission_iris_diaphragm:
                        xlight.set_emission_iris(int(hw_settings.emission_iris))
                except (OSError, ValueError) as e:
                    self._log.warning(f"Not setting iris values: {e}")
        elif ENABLE_SPINNING_DISK_CONFOCAL and USE_DRAGONFLY and self.microscope.addons.dragonfly:
            try:
                self.microscope.addons.dragonfly.set_emission_filter(
                    self.microscope.addons.dragonfly.get_camera_port(),
                    emission_filter_position,
                )
            except Exception as e:
                self._log.warning(f"Not setting emission filter position: {e}")

        if self.microscope.addons.emission_filter_wheel and self.enable_channel_auto_filter_switching:
            try:
                if self.trigger_mode == TriggerMode.SOFTWARE:
                    self.microscope.addons.emission_filter_wheel.set_delay_offset_ms(0)
                elif self.trigger_mode == TriggerMode.HARDWARE:
                    self.microscope.addons.emission_filter_wheel.set_delay_offset_ms(-self.camera.get_strobe_time())
                self.microscope.addons.emission_filter_wheel.set_filter_wheel_position(
                    {1: emission_filter_position}
                )
            except Exception as e:
                self._log.warning(f"Not setting emission filter position: {e}")

    def start_live(self):
        """Start live streaming."""
        self.is_live = True
        # Enable the streaming gate so illumination commands reach hardware,
        # then turn on illumination BEFORE starting the camera so that the
        # first frame is correctly illuminated.
        if self.control_illumination:
            self.turn_on_illumination()
            ic = self.microscope.illumination_controller
            ic.set_streaming_active(True)
            

        self.camera.start_streaming()
        self._log.info(f"starting live with trigger mode {self.trigger_mode}")

        if self.trigger_mode == TriggerMode.SOFTWARE or (
            self.trigger_mode == TriggerMode.HARDWARE and self.use_internal_timer_for_hardware_trigger
        ):
            self.camera.enable_callbacks(True)  # in case it's disabled e.g. by the laser AF controller
            self._start_triggered_acquisition()
        # if controlling the laser displacement measurement camera
        if self.for_displacement_measurement:
            self.microscope.low_level_drivers.microcontroller.set_pin_level(MCU_PINS.AF_LASER, 1)

    def stop_live(self):
        self._log.info("stopping live")
        if self.is_live:
            self._log.info("stopping live: is_live is True")
            self.is_live = False
            # Close the streaming gate FIRST so any in-flight timer callback
            # cannot command hardware (apply_hw will be False in set_channel_state).
            ic = self.microscope.illumination_controller
            ic.set_streaming_active(False)
            if self.trigger_mode == TriggerMode.SOFTWARE:
                self._stop_triggered_acquisition()
            if self.trigger_mode == TriggerMode.CONTINUOUS:
                self.camera.stop_streaming()
            if (self.trigger_mode == TriggerMode.SOFTWARE) or (
                self.trigger_mode == TriggerMode.HARDWARE and self.use_internal_timer_for_hardware_trigger
            ):
                self._stop_triggered_acquisition()
            # if controlling the laser displacement measurement camera
            if self.for_displacement_measurement:
                self.microscope.low_level_drivers.microcontroller.set_pin_level(MCU_PINS.AF_LASER, 0)

    def _trigger_acquisition_timer_fn(self):
        if self.trigger_acquisition():
            if self.is_live:
                self._start_new_timer()
        else:
            if self.is_live:
                # It failed, try again real soon
                # Use a short period so we get back here fast and check again.
                re_check_period_ms = 10
                self._start_new_timer(maybe_custom_interval_ms=re_check_period_ms)

    # software trigger related
    def trigger_acquisition(self):
        if not self.is_live:
            return False
        if not self.camera.get_ready_for_trigger():
            # TODO(imo): Before, send_trigger would pass silently for this case.  Now
            # we do the same here.  Should this warn?  I didn't add a warning because it seems like
            # we over-trigger as standard practice (eg: we trigger at our exposure time frequency, but
            # the cameras can't give us images that fast so we essentially always have at least 1 skipped trigger)
            self._trigger_skip_count += 1
            if self._trigger_skip_count % 100 == 1:
                self._log.debug(
                    f"Not ready for trigger, skipping (_trigger_skip_count={self._trigger_skip_count}, total frame time = {self.camera.get_total_frame_time()} [ms])."
                )
            return False

        self._trigger_skip_count = 0
        # Ensure illumination is on before triggering (idempotent via IC gate).
        if self.trigger_mode == TriggerMode.SOFTWARE and self.control_illumination:
            if not self.microscope.illumination_controller.is_any_hardware_asserted():
                self.turn_on_illumination()

        self.trigger_ID = self.trigger_ID + 1

        self.camera.send_trigger(self.camera.get_exposure_time())

        return True

    def _stop_existing_timer(self):
        if self.timer_trigger and self.timer_trigger.is_alive():
            self.timer_trigger.cancel()
        self.timer_trigger = None

    def _start_new_timer(self, maybe_custom_interval_ms=None):
        self._stop_existing_timer()
        if maybe_custom_interval_ms:
            interval_s = maybe_custom_interval_ms / 1000.0
        else:
            interval_s = self.timer_trigger_interval / 1000.0
        self.timer_trigger = threading.Timer(interval_s, self._trigger_acquisition_timer_fn)
        self.timer_trigger.daemon = True
        self.timer_trigger.start()

    def _start_triggered_acquisition(self):
        self._start_new_timer()

    def _set_trigger_fps(self, fps_trigger):
        if fps_trigger <= 0:
            raise ValueError(f"fps_trigger must be > 0, but {fps_trigger=}")
        self._log.debug(f"Setting {fps_trigger=}")
        self.fps_trigger = fps_trigger
        self.timer_trigger_interval = (1 / self.fps_trigger) * 1000
        if self.is_live:
            self._start_new_timer()

    def _stop_triggered_acquisition(self):
        self._stop_existing_timer()

    # trigger mode and settings
    def set_trigger_mode(self, mode):
        if mode == TriggerMode.SOFTWARE:
            if self.is_live and (
                self.trigger_mode == TriggerMode.HARDWARE and self.use_internal_timer_for_hardware_trigger
            ):
                self._stop_triggered_acquisition()
            self.camera.set_acquisition_mode(CameraAcquisitionMode.SOFTWARE_TRIGGER)
            if self.is_live:
                self._start_triggered_acquisition()
            self.microscope.low_level_drivers.microcontroller.set_trigger_mode(0)
        if mode == TriggerMode.HARDWARE:
            if self.trigger_mode == TriggerMode.SOFTWARE and self.is_live:
                self._stop_triggered_acquisition()
            self.camera.set_acquisition_mode(CameraAcquisitionMode.HARDWARE_TRIGGER)
            self.camera.set_exposure_time(self.current_observation_state.exposure_time)

            if self.is_live and self.use_internal_timer_for_hardware_trigger:
                self._start_triggered_acquisition()

            self.microscope.low_level_drivers.microcontroller.set_trigger_mode(HARDWARE_TRIGGER_MODE)

        if mode == TriggerMode.CONTINUOUS:
            if (self.trigger_mode == TriggerMode.SOFTWARE) or (
                self.trigger_mode == TriggerMode.HARDWARE and self.use_internal_timer_for_hardware_trigger
            ):
                self._stop_triggered_acquisition()
            self.camera.set_acquisition_mode(CameraAcquisitionMode.CONTINUOUS)
            self.microscope.low_level_drivers.microcontroller.set_trigger_mode(0)
        self.trigger_mode = mode

    def set_trigger_fps(self, fps):
        if (self.trigger_mode == TriggerMode.SOFTWARE) or (
            self.trigger_mode == TriggerMode.HARDWARE and self.use_internal_timer_for_hardware_trigger
        ):
            self._set_trigger_fps(fps)

    # set microscope mode
    def set_observation_state(self, state: ObservationState):
        if state is None:
            self._log.error("set_observation_state() called with None state - this is a bug in the caller")
            return
        # Channel switching for acquisition must follow channel configuration
        # (illumination intensity + illumination on/off behavior).
        self.control_illumination = True
        self._log.info("setting Observation state to " + state.name)

        _t_total = time.perf_counter()

        # temporarily stop live while changing mode
        if self.is_live is True:
            self._stop_existing_timer()
            if self.control_illumination:
                # Turn off illumination BEFORE switching self.current_observation_state.
                # turn_off_illumination() reads self.current_observation_state to determine which
                # laser wavelength to turn off. If we switch first, we'd turn off the NEW
                # channel's laser instead of the OLD channel's laser (which is still on).
                _t0 = time.perf_counter()
                self.turn_off_illumination()
                self._log.info("set_observation_state: turn_off_illumination took %.4fs", time.perf_counter() - _t0)

        self.current_observation_state = state

        # set camera exposure time and analog gain
        _t0 = time.perf_counter()
        self.camera.set_exposure_time(self.current_observation_state.exposure_time)
        self._log.info("set_observation_state: set_exposure_time took %.4fs", time.perf_counter() - _t0)
        _t0 = time.perf_counter()
        try:
            self.camera.set_analog_gain(self.current_observation_state.analog_gain)
        except NotImplementedError:
            pass
        self._log.info("set_observation_state: set_analog_gain took %.4fs", time.perf_counter() - _t0)

        # set illumination
        if self.control_illumination:
            _t0 = time.perf_counter()
            self._log.info("set_observation_state: updating illumination")
            self.update_illumination()
            self._log.info("set_observation_state: update_illumination took %.4fs", time.perf_counter() - _t0)

        # restart live
        if self.is_live is True:
            if self.control_illumination:
                _t0 = time.perf_counter()
                self.turn_on_illumination()
                self._log.info("set_observation_state: turn_on_illumination took %.4fs", time.perf_counter() - _t0)
            self._start_new_timer()
        self._log.info("set_observation_state: TOTAL took %.4fs", time.perf_counter() - _t_total)
        self._log.info(f"set_observation_state: current_observation_state: {self.current_observation_state.name}")
        self._log.info(f"Active illuminators: {self.current_observation_state.active_illuminator_states}")

    def set_microscope_mode(self, configuration: ObservationState):
        """Backward-compatible alias for set_observation_state.

        .. deprecated:: Use :meth:`set_observation_state` instead.
        """
        if configuration is None:
            self._log.error("set_microscope_mode() called with None configuration - this is a bug in the caller")
            return
        self.set_observation_state(configuration)

    def get_trigger_mode(self):
        return self.trigger_mode

    # slot
    def on_new_frame(self):
        if not self.is_live:
            return
        if self.fps_trigger <= 5:
            if self.control_illumination and self.microscope.illumination_controller.is_any_hardware_asserted():
                self.turn_off_illumination()

    def set_display_resolution_scaling(self, display_resolution_scaling):
        self.display_resolution_scaling = display_resolution_scaling / 100
