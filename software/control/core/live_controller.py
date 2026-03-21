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
from control.core.config.utils import apply_confocal_override
from control.models import merge_channel_configs

if TYPE_CHECKING:
    from control.models import AcquisitionChannel, IlluminationChannelConfig


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
            control_illumination: If True, automatically control illumination during acquisition
            use_internal_timer_for_hardware_trigger: Use Python timer vs microcontroller timer
            for_displacement_measurement: If True, used for laser autofocus/displacement measurement
        """
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self.microscope = microscope
        self.camera: AbstractCamera = camera
        self.currentConfiguration: Optional[AcquisitionChannel] = None
        self.trigger_mode: Optional[TriggerMode] = TriggerMode.SOFTWARE  # @@@ change to None
        self.is_live = False
        self.control_illumination = control_illumination
        self.illumination_on = False
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

    # ─────────────────────────────────────────────────────────────────────────────
    # Illumination channel helpers
    # ─────────────────────────────────────────────────────────────────────────────

    def _get_illumination_channel_name(self) -> Optional[str]:
        """Return the canonical illumination channel name for the current configuration.

        This is the value stored in
        ``AcquisitionChannel.illumination_settings.illumination_channel``, which
        matches a key in ``IlluminationController.channel_names``.
        """
        if not self.currentConfiguration:
            return None
        return self.currentConfiguration.primary_illumination_channel

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

    def get_channels(self, objective: str) -> List["AcquisitionChannel"]:
        """Get acquisition channels for an objective, with confocal mode applied.

        This method provides channels with the current confocal_mode state applied.
        It uses ConfigRepository for config I/O and applies confocal overrides
        based on this controller's confocal_mode state.

        Args:
            objective: Objective name (e.g., "10x", "20x")

        Returns:
            List of AcquisitionChannel objects with confocal overrides applied if
            in confocal mode. Returns empty list if no profile is set or no configs
            are available.
        """
        config_repo = self.microscope.config_repo

        # Check if a profile is set
        if config_repo.current_profile is None:
            self._log.warning("get_channels() returning empty list: no profile is set")
            return []

        # Get general config (shared settings)
        general = config_repo.get_general_config()
        if not general:
            self._log.warning(
                f"get_channels() returning empty list: no general config for profile '{config_repo.current_profile}'"
            )
            return []

        # Get objective-specific config
        obj_config = config_repo.get_objective_config(objective)

        # Merge configs (if no objective config, use general channels)
        if obj_config:
            channels = merge_channel_configs(general, obj_config)
        else:
            channels = list(general.channels)

        # Filter to only enabled channels
        channels = [ch for ch in channels if ch.enabled]

        # Apply confocal mode if active
        return apply_confocal_override(channels, self._confocal_mode)

    def get_channel_by_name(self, objective: str, name: str) -> Optional["AcquisitionChannel"]:
        """Get a specific channel by name.

        Args:
            objective: Objective name
            name: Channel name to find

        Returns:
            AcquisitionChannel if found, None otherwise
        """
        channels = self.get_channels(objective)
        return next((ch for ch in channels if ch.name == name), None)

    def set_active_channel_reference(self, configuration: Optional["AcquisitionChannel"]) -> None:
        """Record the selected acquisition channel without touching camera or illumination hardware.

        The UI (e.g. Live Control) chooses a channel from the profile before ``set_microscope_mode`` runs.
        We intentionally skip ``set_microscope_mode`` at startup so restored camera cache settings are not
        overwritten, but contrast/LUT, Observation State collection, and display code still need a stable
        ``currentConfiguration`` for channel *name* and preset bookkeeping.

        Observation State: ``collect_observation_state`` reads ``active_channel_name`` from
        ``currentConfiguration``; keeping this reference in sync with the Live Control / Napari dropdown
        matches that paradigm without forcing a full hardware apply.
        """
        self.currentConfiguration = configuration

    def get_channel_name_for_contrast(self) -> str:
        """Channel key for ContrastManager / display when only a logical selection exists."""
        if self.currentConfiguration is not None:
            return self.currentConfiguration.name
        objective = getattr(getattr(self.microscope, "objective_store", None), "current_objective", None)
        if objective:
            chs = self.get_channels(objective)
            if chs:
                return chs[0].name
        return "default"

    # ─────────────────────────────────────────────────────────────────────────────
    # Illumination control
    # ─────────────────────────────────────────────────────────────────────────────

    def turn_on_illumination(self):
        """Turn on illumination for the current channel."""
        channel_name = self._get_illumination_channel_name()
        if channel_name:
            if self.currentConfiguration is not None:
                ill = self.currentConfiguration.illumination_settings
                mode = getattr(ill, "led_matrix_mode", None)
                ic = self.microscope.illumination_controller
                if mode and getattr(ic, "has_unified_led_matrix", lambda: False)():
                    ic.set_led_matrix_mode(mode)
            self.microscope.illumination_controller.turn_on_channel(channel_name)
        else:
            self._log.warning(
                f"turn_on_illumination() skipped - no channel configured for "
                f"'{self.currentConfiguration.name if self.currentConfiguration else 'None'}'"
            )
        self.illumination_on = True

    def turn_off_illumination(self):
        """Turn off illumination for the current channel."""
        channel_name = self._get_illumination_channel_name()
        if channel_name:
            self.microscope.illumination_controller.turn_off_channel(channel_name)
        else:
            self._log.warning(
                f"turn_off_illumination() skipped - no channel configured for "
                f"'{self.currentConfiguration.name if self.currentConfiguration else 'None'}'"
            )
        self.illumination_on = False

    def update_illumination(self):
        """Set intensity for the current channel and apply any device-specific settings."""
        if self.currentConfiguration is None:
            self._log.warning("update_illumination() called with no currentConfiguration")
            return
        channel_name = self._get_illumination_channel_name()
        intensity = self.currentConfiguration.illumination_intensity
        ill = self.currentConfiguration.illumination_settings
        mode = getattr(ill, "led_matrix_mode", None)
        ic = self.microscope.illumination_controller
        if mode and getattr(ic, "has_unified_led_matrix", lambda: False)():
            ic.set_led_matrix_mode(mode)
        if channel_name:
            self.microscope.illumination_controller.set_channel_intensity(channel_name, intensity)
            # NL5 / CellX laser power forwarding (wavelength-specific accessories)
            wavelength = self.currentConfiguration.get_illumination_wavelength(
                self.microscope.config_repo.get_illumination_config()
            ) if self.microscope.config_repo.get_illumination_config() else None
            if wavelength and self.microscope.addons.nl5 and NL5_USE_DOUT:
                self.microscope.addons.nl5.set_active_channel(NL5_WAVENLENGTH_MAP[wavelength])
                if NL5_USE_AOUT:
                    self.microscope.addons.nl5.set_laser_power(NL5_WAVENLENGTH_MAP[wavelength], int(intensity))
                if self.microscope.addons.cellx and ENABLE_CELLX:
                    self.microscope.addons.cellx.set_laser_power(NL5_WAVENLENGTH_MAP[wavelength], int(intensity))

        # set emission filter position and iris values
        if ENABLE_SPINNING_DISK_CONFOCAL and self.microscope.addons.xlight and not USE_DRAGONFLY:
            try:
                if self.currentConfiguration.emission_filter_position:
                    self.microscope.addons.xlight.set_emission_filter(
                        self.currentConfiguration.emission_filter_position,
                        extraction=False,
                        validate=XLIGHT_VALIDATE_WHEEL_POS,
                    )
            except Exception as e:
                self._log.warning(f"Not setting emission filter position: {e}")
            # Apply per-channel iris values
            hw_settings = self.currentConfiguration.confocal_hardware_settings
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
                    self.currentConfiguration.emission_filter_position,
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
                    {1: self.currentConfiguration.emission_filter_position}
                )
            except Exception as e:
                self._log.warning(f"Not setting emission filter position: {e}")

    def start_live(self):
        self.is_live = True
        self.camera.start_streaming()
        if self.trigger_mode == TriggerMode.SOFTWARE or (
            self.trigger_mode == TriggerMode.HARDWARE and self.use_internal_timer_for_hardware_trigger
        ):
            self.camera.enable_callbacks(True)  # in case it's disabled e.g. by the laser AF controller
            self._start_triggerred_acquisition()
        # if controlling the laser displacement measurement camera
        if self.for_displacement_measurement:
            self.microscope.low_level_drivers.microcontroller.set_pin_level(MCU_PINS.AF_LASER, 1)

    def stop_live(self):
        if self.is_live:
            self.is_live = False
            if self.trigger_mode == TriggerMode.SOFTWARE:
                self._stop_triggerred_acquisition()
            if self.trigger_mode == TriggerMode.CONTINUOUS:
                self.camera.stop_streaming()
            if (self.trigger_mode == TriggerMode.SOFTWARE) or (
                self.trigger_mode == TriggerMode.HARDWARE and self.use_internal_timer_for_hardware_trigger
            ):
                self._stop_triggerred_acquisition()
            if self.control_illumination:
                self.turn_off_illumination()
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
        if self.trigger_mode == TriggerMode.SOFTWARE and self.control_illumination:
            if not self.illumination_on:
                self.turn_on_illumination()

        self.trigger_ID = self.trigger_ID + 1

        self.camera.send_trigger(self.camera.get_exposure_time())

        if self.trigger_mode == TriggerMode.SOFTWARE:
            if self.control_illumination and self.illumination_on == False:
                self.turn_on_illumination()

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

    def _start_triggerred_acquisition(self):
        self._start_new_timer()

    def _set_trigger_fps(self, fps_trigger):
        if fps_trigger <= 0:
            raise ValueError(f"fps_trigger must be > 0, but {fps_trigger=}")
        self._log.debug(f"Setting {fps_trigger=}")
        self.fps_trigger = fps_trigger
        self.timer_trigger_interval = (1 / self.fps_trigger) * 1000
        if self.is_live:
            self._start_new_timer()

    def _stop_triggerred_acquisition(self):
        self._stop_existing_timer()

    # trigger mode and settings
    def set_trigger_mode(self, mode):
        if mode == TriggerMode.SOFTWARE:
            if self.is_live and (
                self.trigger_mode == TriggerMode.HARDWARE and self.use_internal_timer_for_hardware_trigger
            ):
                self._stop_triggerred_acquisition()
            self.camera.set_acquisition_mode(CameraAcquisitionMode.SOFTWARE_TRIGGER)
            if self.is_live:
                self._start_triggerred_acquisition()
            self.microscope.low_level_drivers.microcontroller.set_trigger_mode(0)
        if mode == TriggerMode.HARDWARE:
            if self.trigger_mode == TriggerMode.SOFTWARE and self.is_live:
                self._stop_triggerred_acquisition()
            self.camera.set_acquisition_mode(CameraAcquisitionMode.HARDWARE_TRIGGER)
            self.camera.set_exposure_time(self.currentConfiguration.exposure_time)

            if self.is_live and self.use_internal_timer_for_hardware_trigger:
                self._start_triggerred_acquisition()

            self.microscope.low_level_drivers.microcontroller.set_trigger_mode(HARDWARE_TRIGGER_MODE)

        if mode == TriggerMode.CONTINUOUS:
            if (self.trigger_mode == TriggerMode.SOFTWARE) or (
                self.trigger_mode == TriggerMode.HARDWARE and self.use_internal_timer_for_hardware_trigger
            ):
                self._stop_triggerred_acquisition()
            self.camera.set_acquisition_mode(CameraAcquisitionMode.CONTINUOUS)
            self.microscope.low_level_drivers.microcontroller.set_trigger_mode(0)
        self.trigger_mode = mode

    def set_trigger_fps(self, fps):
        if (self.trigger_mode == TriggerMode.SOFTWARE) or (
            self.trigger_mode == TriggerMode.HARDWARE and self.use_internal_timer_for_hardware_trigger
        ):
            self._set_trigger_fps(fps)

    # set microscope mode
    def set_microscope_mode(self, configuration: "AcquisitionChannel"):
        if configuration is None:
            self._log.error("set_microscope_mode() called with None configuration - this is a bug in the caller")
            return
        # Channel switching for acquisition must follow channel configuration
        # (illumination intensity + illumination on/off behavior).
        self.control_illumination = True
        self._log.info("setting microscope mode to " + configuration.name)

        # temporarily stop live while changing mode
        if self.is_live is True:
            self._stop_existing_timer()
            if self.control_illumination:
                # Turn off illumination BEFORE switching self.currentConfiguration.
                # turn_off_illumination() reads self.currentConfiguration to determine which
                # laser wavelength to turn off. If we switch first, we'd turn off the NEW
                # channel's laser instead of the OLD channel's laser (which is still on).
                self.turn_off_illumination()

        self.currentConfiguration = configuration

        # set camera exposure time and analog gain
        self.camera.set_exposure_time(self.currentConfiguration.exposure_time)
        try:
            self.camera.set_analog_gain(self.currentConfiguration.analog_gain)
        except NotImplementedError:
            pass

        # set illumination
        if self.control_illumination:
            self.update_illumination()

        # restart live
        if self.is_live is True:
            if self.control_illumination:
                self.turn_on_illumination()
            self._start_new_timer()
        self._log.info("Done setting microscope mode.")

    def get_trigger_mode(self):
        return self.trigger_mode

    # slot
    def on_new_frame(self):
        if self.fps_trigger <= 5:
            if self.control_illumination and self.illumination_on == True:
                self.turn_off_illumination()

    def set_display_resolution_scaling(self, display_resolution_scaling):
        self.display_resolution_scaling = display_resolution_scaling / 100
