"""
Live image acquisition controller.

Handles camera streaming and triggering only:
- Software or hardware trigger modes
- Frame rate control via internal timer
- Streaming gate management for illumination synchronization

Observation state management (illumination, camera settings, optical path)
is handled by ObservationStateController.
"""

from __future__ import annotations

import threading
from typing import Optional, TYPE_CHECKING

import squid.logging
from squid.abc import CameraAcquisitionMode, AbstractCamera
from control._def import *

if TYPE_CHECKING:
    from control.core.observation_state_controller import ObservationStateController


class LiveController:
    """Controller for live image streaming and camera triggering.

    Does NOT own observation state or illumination — those are managed
    by ObservationStateController.
    """

    def __init__(
        self,
        microscope: "Microscope",
        camera: AbstractCamera,
        control_illumination: bool = True,
        use_internal_timer_for_hardware_trigger: bool = True,
        for_displacement_measurement: bool = False,
    ):
        self._log = squid.logging.get_logger(self.__class__.__name__ + "/" + camera.__class__.__name__)
        self.microscope = microscope
        self.camera: AbstractCamera = camera
        self.trigger_mode: Optional[TriggerMode] = TriggerMode.SOFTWARE
        self.is_live = False
        self.control_illumination = control_illumination
        self.use_internal_timer_for_hardware_trigger = use_internal_timer_for_hardware_trigger
        self.for_displacement_measurement = for_displacement_measurement

        # Set after construction to avoid circular dependency
        self.obs_controller: Optional["ObservationStateController"] = None

        # Frame rate control
        self.fps_trigger = 1
        self.timer_trigger_interval = (1.0 / self.fps_trigger) * 1000
        self._trigger_skip_count = 0
        self.timer_trigger: Optional[threading.Timer] = None
        self.trigger_ID = -1

        # Frame rate monitoring
        self.fps_real = 0
        self.counter = 0
        self.timestamp_last = 0

        self.display_resolution_scaling = 1
        self.enable_channel_auto_filter_switching: bool = True

        # Log-once guards for the waveform-driven (timed-pulse) live preview so a
        # persistently mis-wired pulse, or a state previewed in CONTINUOUS, does
        # not spam a message every frame.
        self._live_pulse_failure_logged = False
        self._live_waveform_mode_hint_logged = False

    # ─────────────────────────────────────────────────────────────────────
    # Live streaming
    # ─────────────────────────────────────────────────────────────────────

    def start_live(self):
        self.is_live = True
        # Open the streaming gate and turn on illumination BEFORE camera
        # so the first frame is correctly illuminated.
        if self.control_illumination and self.obs_controller:
            config = self.obs_controller.current_observation_state
            waveform_driven = bool(config is not None and getattr(config, "is_waveform_driven", False))
            if waveform_driven and self.trigger_mode == TriggerMode.SOFTWARE:
                # Gated-pulse preview: don't hold the LED on — the per-frame NIDAQ
                # pulse in trigger_acquisition gates it. Just stage DC intensities
                # (timed gates stay LOW) so the first triggered frame is faithful.
                from control.core.waveform_capture import apply_illumination_for_waveform_capture
                apply_illumination_for_waveform_capture(self.microscope, config, self._log)
            else:
                if waveform_driven and not self._live_waveform_mode_hint_logged:
                    self._log.info(
                        "Observation state '%s' uses a timed pulse; set the live trigger to "
                        "Software to preview the actual gated pulse (other modes show the LED "
                        "held on for the full exposure).",
                        getattr(config, "name", "?"),
                    )
                    self._live_waveform_mode_hint_logged = True
                self.obs_controller.turn_on_illumination()
            ic = self.microscope.illumination_controller
            ic.set_streaming_active(True)

        self.camera.start_streaming()

        if self.trigger_mode == TriggerMode.SOFTWARE or (
            self.trigger_mode == TriggerMode.HARDWARE and self.use_internal_timer_for_hardware_trigger
        ):
            self.camera.enable_callbacks(True)
            self._start_triggered_acquisition()

        if self.for_displacement_measurement:
            self.microscope.low_level_drivers.microcontroller.set_pin_level(MCU_PINS.AF_LASER, 1)

    def stop_live(self):
        self._log.info("stopping live")
        if self.is_live:
            self.is_live = False

            # Mirror start_live: only touch the illumination controller's
            # streaming gate when this LiveController actually owns illumination.
            # The focus-camera LiveController runs with control_illumination=False
            # and must not poke the main illumination controller's state.
            if self.control_illumination and self.obs_controller:
                ic = self.microscope.illumination_controller
                ic.set_streaming_active(False)

            if self.trigger_mode == TriggerMode.SOFTWARE or (
                self.trigger_mode == TriggerMode.HARDWARE and self.use_internal_timer_for_hardware_trigger
            ):
                self._stop_triggered_acquisition()
            if self.trigger_mode == TriggerMode.CONTINUOUS:
                self.camera.stop_streaming()

            if self.for_displacement_measurement:
                self.microscope.low_level_drivers.microcontroller.set_pin_level(MCU_PINS.AF_LASER, 0)

    # ─────────────────────────────────────────────────────────────────────
    # Triggering
    # ─────────────────────────────────────────────────────────────────────

    def _trigger_acquisition_timer_fn(self):
        if self.trigger_acquisition():
            if self.is_live:
                self._start_new_timer()
        else:
            if self.is_live:
                self._start_new_timer(maybe_custom_interval_ms=10)

    def trigger_acquisition(self):
        if not self.is_live:
            return False
        if not self.camera.get_ready_for_trigger():
            self._trigger_skip_count += 1
            if self._trigger_skip_count % 100 == 1:
                self._log.debug(
                    f"Not ready for trigger, skipping (_trigger_skip_count={self._trigger_skip_count})"
                )
            return False

        self._trigger_skip_count = 0

        # Waveform-driven (timed-pulse) states: preview the REAL gated pulse —
        # stage DC intensities and let a one-shot NIDAQ pulse synced to this
        # exposure drive the timed gate (the same path multipoint uses), instead
        # of holding the LED on for the whole frame. Only meaningful in the
        # per-frame triggered SOFTWARE mode; a free-running CONTINUOUS stream
        # can't sync a one-shot pulse per frame, so it falls back to full-on.
        nidaq_pulse_cleanup = None
        if self.control_illumination and self.obs_controller:
            config = self.obs_controller.current_observation_state
            waveform_driven = bool(config is not None and getattr(config, "is_waveform_driven", False))
            if waveform_driven and self.trigger_mode == TriggerMode.SOFTWARE:
                nidaq_pulse_cleanup = self._arm_live_waveform_pulse(config)
            elif waveform_driven and not self._live_waveform_mode_hint_logged:
                self._log.info(
                    "Observation state '%s' uses a timed pulse; set the live trigger to "
                    "Software to preview the actual gated pulse (other modes show the LED "
                    "held on for the full exposure).",
                    getattr(config, "name", "?"),
                )
                self._live_waveform_mode_hint_logged = True

            if nidaq_pulse_cleanup is None and self.trigger_mode == TriggerMode.SOFTWARE:
                # Standard (non-waveform) channel, or no NIDAQ to gate it: hold
                # illumination on for the exposure, exactly as before.
                if not self.microscope.illumination_controller.is_any_hardware_asserted():
                    self.obs_controller.turn_on_illumination()

        self.trigger_ID += 1
        self.camera.send_trigger(self.camera.get_exposure_time())
        # Bracket the trigger: the cleanup waits for the one-shot pulse to fire
        # during this exposure, then releases the task and drives the gate LOW.
        if nidaq_pulse_cleanup is not None:
            try:
                nidaq_pulse_cleanup()
            except Exception as e:
                self._log.warning("Live waveform pulse cleanup failed: %s", e)
        return True

    def _arm_live_waveform_pulse(self, config):
        """Stage DC illumination + arm the one-shot NIDAQ pulse for one live frame.

        Returns the cleanup closure (call after ``send_trigger``), or ``None`` to
        fall back to holding illumination on for the full exposure (no NIDAQ on
        this rig, or the waveform could not be built).
        """
        from control.core.waveform_capture import (
            apply_illumination_for_waveform_capture,
            arm_nidaq_pulse_for_capture,
        )
        try:
            apply_illumination_for_waveform_capture(self.microscope, config, self._log)
            return arm_nidaq_pulse_for_capture(
                self.microscope,
                config,
                log=self._log,
                on_wait_failure=self._on_live_pulse_wait_failure,
            )
        except Exception as e:
            self._log.warning(
                "Could not arm live waveform pulse for '%s': %s", getattr(config, "name", "?"), e
            )
            return None

    def _on_live_pulse_wait_failure(self, terminal, timeout_s, name, error) -> None:
        """Log once when the live preview pulse never fired (don't abort live)."""
        if not self._live_pulse_failure_logged:
            self._log.warning(
                "Live preview: NIDAQ pulse for '%s' did not fire within %.2fs "
                "(check the camera frame-signal terminal %s). Showing whatever the "
                "camera captured; the gated pulse may not be visible.",
                name, timeout_s, terminal,
            )
            self._live_pulse_failure_logged = True

    def _stop_existing_timer(self):
        if self.timer_trigger and self.timer_trigger.is_alive():
            self.timer_trigger.cancel()
        self.timer_trigger = None

    def _start_new_timer(self, maybe_custom_interval_ms=None):
        self._stop_existing_timer()
        interval_s = (maybe_custom_interval_ms or self.timer_trigger_interval) / 1000.0
        self.timer_trigger = threading.Timer(interval_s, self._trigger_acquisition_timer_fn)
        self.timer_trigger.daemon = True
        self.timer_trigger.start()

    def _start_triggered_acquisition(self):
        self._start_new_timer()

    def _set_trigger_fps(self, fps_trigger):
        if fps_trigger <= 0:
            raise ValueError(f"fps_trigger must be > 0, but {fps_trigger=}")
        self.fps_trigger = fps_trigger
        self.timer_trigger_interval = (1 / self.fps_trigger) * 1000
        # Only (re)start the trigger timer when we're actually running a triggered
        # acquisition. In CONTINUOUS the timer is meaningless, but fps_trigger itself
        # is still consulted by on_new_frame and is persisted into observation-state
        # snapshots, so we must keep the attribute up to date in every mode.
        if self.is_live and (
            self.trigger_mode == TriggerMode.SOFTWARE
            or (self.trigger_mode == TriggerMode.HARDWARE and self.use_internal_timer_for_hardware_trigger)
        ):
            self._start_new_timer()

    def _stop_triggered_acquisition(self):
        self._stop_existing_timer()

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
            obs = self.obs_controller.current_observation_state if self.obs_controller else None
            if obs:
                self.camera.set_exposure_time(obs.exposure_time)
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
        self._set_trigger_fps(fps)

    def get_trigger_mode(self):
        return self.trigger_mode

    # ─────────────────────────────────────────────────────────────────────
    # Frame callback
    # ─────────────────────────────────────────────────────────────────────

    def on_new_frame(self):
        if not self.is_live:
            return
        # The LED-off-between-shots behaviour only makes sense in triggered live mode,
        # where the trigger timer will turn the LED back on for the next frame. In
        # CONTINUOUS there is no such timer, so the illumination must stay asserted.
        if self.trigger_mode == TriggerMode.CONTINUOUS:
            return
        if self.fps_trigger <= 5:
            if self.control_illumination and self.microscope.illumination_controller.is_any_hardware_asserted():
                if self.obs_controller:
                    self.obs_controller.turn_off_illumination()

    def set_display_resolution_scaling(self, display_resolution_scaling):
        self.display_resolution_scaling = display_resolution_scaling / 100
