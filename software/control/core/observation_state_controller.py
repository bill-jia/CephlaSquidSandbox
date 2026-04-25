"""
Observation state controller.

Owns the current ObservationState and mediates all widget-to-hardware
communication for camera settings, illumination, and optical path.

Widgets call per-property mutation methods (e.g. ``set_exposure_time``,
``set_illumination_intensity``) which update the ObservationState in-memory
and apply the change to the appropriate hardware controller.

Hardware gating rules:
- Camera settings (exposure, gain, binning, mode, ROI): always applied
- Illumination intensity: always applied (IC intensity is ungated)
- Illumination on/off: gated — logical state always updated; hardware only when streaming
- Optical path (emission filters, confocal iris): always applied
"""
from __future__ import annotations
from control._def import NL5_USE_DOUT

import contextlib
import time
from typing import Any, Dict, List, Optional, Union, TYPE_CHECKING

import squid.logging
from control._def import *
from control.models.observation_state import (
    CameraLiveSnapshot,
    CameraSettings,
    ConfocalSettings,
    IlluminatorState,
    ObservationState,
)

if TYPE_CHECKING:
    from control.core.config.repository import ConfigRepository
    from control.core.live_controller import LiveController
    from control.lighting import IlluminationController
    from squid.abc import AbstractCamera

logger = squid.logging.get_logger(__name__)


class ObservationStateController:
    """Central authority for the current ObservationState.

    All UI widgets should call methods on this controller rather than
    talking directly to camera or illumination hardware.
    """

    def __init__(
        self,
        microscope: Any,
        camera: "AbstractCamera",
    ):
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self.microscope = microscope
        self.camera = camera
        self._current_state: Optional[ObservationState] = None
        self._confocal_mode: bool = False
        self.enable_channel_auto_filter_switching: bool = True
        # Set after construction to avoid circular dependency
        self.live_controller: Optional["LiveController"] = None
        # Optional timing manager for fine-grained profiling of apply paths.
        # Callers (e.g. MultiPointWorker) may assign a TimingManager here to
        # collect sub-step timings; None means timers are no-ops.
        self._timing: Optional[Any] = None

    def _time(self, name: str):
        """Return a context manager that records elapsed time under ``name`` if
        a TimingManager has been attached to ``self._timing``; otherwise a no-op."""
        if self._timing is None:
            return contextlib.nullcontext()
        return self._timing.get_timer(name)

    # ─────────────────────────────────────────────────────────────────────
    # Properties
    # ─────────────────────────────────────────────────────────────────────

    @property
    def current_observation_state(self) -> Optional[ObservationState]:
        return self._current_state

    @current_observation_state.setter
    def current_observation_state(self, value: Optional[ObservationState]) -> None:
        self._current_state = value

    @property
    def ic(self) -> "IlluminationController":
        return self.microscope.illumination_controller

    @property
    def config_repo(self) -> "ConfigRepository":
        return self.microscope.config_repo

    # ─────────────────────────────────────────────────────────────────────
    # Per-property mutation methods (called by widgets)
    # ─────────────────────────────────────────────────────────────────────

    def set_exposure_time(self, value_ms: float) -> None:
        """Update exposure time in ObservationState and apply to camera hardware."""
        if self._current_state is not None:
            if self._current_state.camera_settings is None:
                self._current_state.camera_settings = CameraSettings(
                    exposure_time_ms=value_ms, gain_mode=0.0
                )
            else:
                self._current_state.camera_settings.exposure_time_ms = value_ms
            self.config_repo.update_channel_setting("ExposureTime", value_ms)
        try:
            self.camera.set_exposure_time(value_ms)
        except Exception as e:
            self._log.warning("Could not set exposure time: %s", e)

    def set_analog_gain(self, value: float) -> None:
        """Update analog gain in ObservationState and apply to camera hardware."""
        if self._current_state is not None:
            if self._current_state.camera_settings is None:
                self._current_state.camera_settings = CameraSettings(
                    exposure_time_ms=1.0, gain_mode=value
                )
            else:
                self._current_state.camera_settings.gain_mode = value
            self.config_repo.update_channel_setting("AnalogGain", value
            )
        try:
            self.camera.set_analog_gain(value)
        except NotImplementedError:
            pass
        except Exception as e:
            self._log.warning("Could not set analog gain: %s", e)

    def set_camera_mode(self, mode: str) -> None:
        """Update camera mode and apply to hardware."""
        try:
            self.camera.set_camera_mode(mode)
        except Exception as e:
            self._log.warning("Could not set camera mode: %s", e)

    def set_binning(self, bx: int, by: int) -> None:
        """Update binning and apply to camera hardware."""
        try:
            self.camera.set_binning(bx, by)
        except Exception as e:
            self._log.warning("Could not set binning: %s", e)

    def set_illumination_intensity(self, channel: str, intensity: float) -> None:
        """Update illumination intensity for a channel. Always applied to hardware (ungated)."""
        if self._current_state is not None:
            for ist in self._current_state.illuminator_states:
                if ist.illumination_channel == channel:
                    ist.intensity = intensity
                    break
        try:
            self.ic.set_channel_intensity(channel, intensity)
        except Exception as e:
            self._log.warning("Could not set illumination intensity for %r: %s", channel, e)

    def set_illumination_on_off(self, channel: str, is_on: bool) -> None:
        """Update illumination on/off. Hardware gated by streaming state."""
        if self._current_state is not None:
            for ist in self._current_state.illuminator_states:
                if ist.illumination_channel == channel:
                    ist.on = is_on
                    break
        try:
            self.ic.set_channel_state(channel, is_on)
        except Exception as e:
            self._log.warning("Could not set illumination on/off for %r: %s", channel, e)

    def set_led_matrix_mode(self, mode: str) -> None:
        """Update LED matrix mode on relevant illuminator states and apply to IC."""
        if self._current_state is not None:
            for ist in self._current_state.illuminator_states:
                if ist.led_matrix_mode is not None or (
                    hasattr(self.ic, "illumination_maps_to_unified_led_matrix")
                    and self.ic.illumination_maps_to_unified_led_matrix(ist.illumination_channel)
                ):
                    ist.led_matrix_mode = mode
        try:
            self.ic.set_led_matrix_mode(mode)
        except Exception as e:
            self._log.warning("Could not set LED matrix mode: %s", e)

    def set_trigger_mode(self, mode) -> None:
        """Delegate trigger mode to LiveController."""
        if self.live_controller is not None:
            self.live_controller.set_trigger_mode(mode)

    def set_trigger_fps(self, fps: float) -> None:
        """Delegate trigger FPS to LiveController."""
        if self.live_controller is not None:
            self.live_controller.set_trigger_fps(fps)

    def persist_iris_config(self, setting_name: str, value: float) -> None:
        """Persist confocal iris setting to in-memory config."""
        self.config_repo.update_channel_setting(setting_name, value)

    # ─────────────────────────────────────────────────────────────────────
    # State management (moved from LiveController)
    # ─────────────────────────────────────────────────────────────────────

    def get_observation_state(self) -> Optional[ObservationState]:
        """Get the observation state from the current profile's general.yaml."""
        return self.config_repo.get_observation_state()

    def set_active_observation_state(self, state: Optional[ObservationState]) -> None:
        """Record the selected observation state without touching hardware."""
        self._current_state = state

    def get_active_channel_name(self) -> Optional[str]:
        """Return the name of the current observation state, or None."""
        if self._current_state is not None:
            return self._current_state.name
        return None

    def get_channel_name_for_contrast(self) -> str:
        """Channel key for ContrastManager / display."""
        if self._current_state is not None:
            return self._current_state.name
        return "default"

    # ─────────────────────────────────────────────────────────────────────
    # Confocal mode
    # ─────────────────────────────────────────────────────────────────────

    def toggle_confocal_widefield(self, confocal: bool) -> None:
        """Toggle between confocal and widefield modes (state only, not hardware)."""
        self._confocal_mode = bool(confocal)
        # self._log.info("Imaging mode set to: %s", "confocal" if self._confocal_mode else "widefield")

    def is_confocal_mode(self) -> bool:
        return self._confocal_mode

    def sync_confocal_mode_from_hardware(self, confocal: bool) -> None:
        self.toggle_confocal_widefield(confocal)

    # ─────────────────────────────────────────────────────────────────────
    # Illumination control (moved from LiveController)
    # ─────────────────────────────────────────────────────────────────────

    def turn_on_illumination(self) -> None:
        """Turn on active illuminators. Hardware gated by streaming state."""
        if self._current_state is None:
            self._log.warning("turn_on_illumination() skipped - no observation state set")
            return
        active = self._current_state.active_illuminator_states
        if not active:
            return
        self.ic.apply_observation_illumination(active, turn_on=True)

    def turn_off_illumination(self) -> None:
        """Turn off active illuminators."""
        if self._current_state is None:
            self._log.warning("turn_off_illumination() skipped - no observation state set")
            return
        active = self._current_state.active_illuminator_states
        if not active:
            return
        self.ic.apply_observation_illumination(active, turn_on=False)

    def apply_illumination_parameters(self) -> None:
        """Set per-channel intensity, LED matrix mode, logical on/off, and NL5/CellX laser power."""
        if self._current_state is None:
            return
        ic = self.ic
        for ist in self._current_state.illuminator_states:
            with self._time("obs:ip:set_led_matrix_mode"):
                mode = ist.led_matrix_mode
                if mode and getattr(ic, "has_unified_led_matrix", lambda: False)():
                    ic.set_led_matrix_mode(mode)

            with self._time("obs:ip:set_channel_intensity"):
                ic.set_channel_intensity(ist.illumination_channel, ist.intensity)
            
            with self._time("obs:ip:set_channel_state"):
                ic.set_channel_state(ist.illumination_channel, ist.on)


            if self.microscope.addons.nl5 and NL5_USE_DOUT:
                with self._time("obs:ip:get_illumination_config"):
                    illum_config = self.config_repo.get_illumination_config()
                wavelength = None
                if illum_config:
                    ch_def = illum_config.get_channel_by_name(ist.illumination_channel)
                    if ch_def:
                        wavelength = ch_def.wavelength_nm

                if wavelength:
                    with self._time("obs:ip:nl5_cellx"):
                        self.microscope.addons.nl5.set_active_channel(NL5_WAVENLENGTH_MAP[wavelength])
                        if NL5_USE_AOUT:
                            self.microscope.addons.nl5.set_laser_power(NL5_WAVENLENGTH_MAP[wavelength], int(ist.intensity))
                        if self.microscope.addons.cellx and ENABLE_CELLX:
                            self.microscope.addons.cellx.set_laser_power(NL5_WAVENLENGTH_MAP[wavelength], int(ist.intensity))

    def apply_optical_path(self) -> None:
        """Set emission filter positions and confocal iris values."""
        if self._current_state is None:
            return
        emission_filter_position = self._current_state.emission_filter_positions.get("default")

        if ENABLE_SPINNING_DISK_CONFOCAL and self.microscope.addons.xlight and not USE_DRAGONFLY:
            try:
                if emission_filter_position is not None:
                    with self._time("obs:op:xlight_set_emission_filter"):
                        self.microscope.addons.xlight.set_emission_filter(
                            emission_filter_position,
                            extraction=False,
                            validate=XLIGHT_VALIDATE_WHEEL_POS,
                        )
            except Exception as e:
                self._log.warning("Not setting emission filter position: %s", e)
            hw_settings = self._current_state.confocal_hardware_settings
            if hw_settings is not None:
                xlight = self.microscope.addons.xlight
                try:
                    with self._time("obs:op:xlight_iris"):
                        if hw_settings.illumination_iris is not None and xlight.has_illumination_iris_diaphragm:
                            xlight.set_illumination_iris(int(hw_settings.illumination_iris))
                        if hw_settings.emission_iris is not None and xlight.has_emission_iris_diaphragm:
                            xlight.set_emission_iris(int(hw_settings.emission_iris))
                except (OSError, ValueError) as e:
                    self._log.warning("Not setting iris values: %s", e)
        elif ENABLE_SPINNING_DISK_CONFOCAL and USE_DRAGONFLY and self.microscope.addons.dragonfly:
            try:
                with self._time("obs:op:dragonfly_set_emission_filter"):
                    self.microscope.addons.dragonfly.set_emission_filter(
                        self.microscope.addons.dragonfly.get_camera_port(),
                        emission_filter_position,
                    )
            except Exception as e:
                self._log.warning("Not setting emission filter position: %s", e)

        if self.microscope.addons.emission_filter_wheel and self.enable_channel_auto_filter_switching:
            lc = self.live_controller
            trigger_mode = lc.trigger_mode if lc else None
            try:
                with self._time("obs:op:efw_set_delay_offset"):
                    if trigger_mode == TriggerMode.SOFTWARE:
                        self.microscope.addons.emission_filter_wheel.set_delay_offset_ms(0)
                    elif trigger_mode == TriggerMode.HARDWARE:
                        self.microscope.addons.emission_filter_wheel.set_delay_offset_ms(
                            -self.camera.get_strobe_time()
                        )
                with self._time("obs:op:efw_set_position"):
                    self.microscope.addons.emission_filter_wheel.set_filter_wheel_position(
                        {1: emission_filter_position}
                    )
            except Exception as e:
                self._log.warning("Not setting emission filter position: %s", e)

    # ─────────────────────────────────────────────────────────────────────
    # Full observation state apply (replaces LiveController.set_observation_state)
    # ─────────────────────────────────────────────────────────────────────

    def apply_full_observation_state(self, state: ObservationState) -> None:
        """Apply a complete observation state to all hardware.

        Handles camera settings, illumination, and optical path.
        If live, pauses triggering and manages illumination transitions.
        """
        if state is None:
            self._log.error("apply_full_observation_state() called with None")
            return

        lc = self.live_controller
        is_live = lc.is_live if lc else False

        if is_live and lc:
            lc._stop_existing_timer()
            self.turn_off_illumination()

        self._current_state = state

        # Camera settings
        with self._time("obs:fos:set_exposure_time"):
            self.camera.set_exposure_time(state.exposure_time)
        try:
            with self._time("obs:fos:set_analog_gain"):
                self.camera.set_analog_gain(state.analog_gain)
        except NotImplementedError:
            pass

        # Illumination + optical path
        with self._time("obs:fos:apply_illumination_parameters"):
            self.apply_illumination_parameters()
        with self._time("obs:fos:apply_optical_path"):
            self.apply_optical_path()

        if is_live and lc:
            self.turn_on_illumination()
            lc._start_new_timer()

    # ─────────────────────────────────────────────────────────────────────
    # Preset apply
    # ─────────────────────────────────────────────────────────────────────

    def apply_observation_state_preset(
        self,
        state: ObservationState,
        *,
        emission_filter_wheel: Any = None,
        apply_camera_live_snapshot: bool = True,
    ) -> None:
        """Apply a saved observation state preset.

        Handles confocal mode, emission filters, camera_live snapshot (ROI, binning, trigger),
        then delegates to apply_full_observation_state for camera + illumination + optical path.

        ``apply_camera_live_snapshot`` gates the entire camera_live block (ROI, binning,
        camera_mode, pixel_format, trigger). Multipoint acquisition passes ``False``: those
        settings were established before the run started and must not be re-asserted between
        channel switches (it both wastes time and risks re-applying stale fields like a
        ``camera_mode`` saved by a different camera class).
        """
        with self._time("obs:preset:toggle_confocal_widefield"):
            self.toggle_confocal_widefield(state.confocal_mode)

        if state.enable_channel_auto_filter_switching is not None:
            self.enable_channel_auto_filter_switching = bool(state.enable_channel_auto_filter_switching)

        # Emission filter wheel (from preset)
        if (
            state.emission_filter_positions
            and emission_filter_wheel is not None
            and hasattr(emission_filter_wheel, "set_filter_wheel_position")
        ):
            try:
                pos = {int(k): int(v) for k, v in state.emission_filter_positions.items()}
                with self._time("obs:preset:efw_set_position"):
                    emission_filter_wheel.set_filter_wheel_position(pos)
            except Exception as e:
                self._log.warning("Could not apply emission filter positions: %s", e)

        # Camera settings from camera_settings block
        if state.camera_settings is not None:
            try:
                with self._time("obs:preset:cs_set_exposure_time"):
                    self.camera.set_exposure_time(state.camera_settings.exposure_time_ms)
            except Exception as e:
                self._log.warning("Could not set exposure: %s", e)
            try:
                with self._time("obs:preset:cs_set_analog_gain"):
                    self.camera.set_analog_gain(state.camera_settings.gain_mode)
            except Exception:
                pass
            if state.camera_settings.pixel_format:
                try:
                    from squid.config import CameraPixelFormat
                    pf_str = state.camera_settings.pixel_format
                    pf = getattr(CameraPixelFormat, pf_str, None)
                    if pf is None:
                        for e in CameraPixelFormat:
                            if e.value == pf_str or e.name == pf_str:
                                pf = e
                                break
                    if pf is not None:
                        with self._time("obs:preset:cs_set_pixel_format"):
                            self.camera.set_pixel_format(pf)
                except Exception as e:
                    self._log.warning("Could not set pixel format: %s", e)

        # Camera live snapshot (ROI, binning, camera_mode, trigger). Skipped during
        # multipoint acquisition — see docstring.
        if apply_camera_live_snapshot and state.camera_live is not None:
            with self._time("obs:preset:apply_camera_live_snapshot"):
                self._apply_camera_live_snapshot(state.camera_live)

        # Full apply (illumination + optical path + state switch)
        with self._time("obs:preset:apply_full_observation_state"):
            self.apply_full_observation_state(state)

    # ─────────────────────────────────────────────────────────────────────
    # Collection (moved from observation_state_service)
    # ─────────────────────────────────────────────────────────────────────

    def collect_observation_state(
        self,
        *,
        emission_filter_positions: Optional[Dict[str, Union[str, int]]] = None,
    ) -> ObservationState:
        """Build ObservationState from current hardware + logical state."""
        saved = self.get_observation_state()
        illuminator_states = list(saved.illuminator_states) if saved else []

        # Merge with hardware state
        ic = self.ic
        illuminator_states = self._merge_illumination_hardware(illuminator_states, ic)
        illuminator_states = self._merge_led_matrix_mode(illuminator_states, ic)
        illuminator_states = self._reconcile_with_hardware(illuminator_states, ic)

        camera_settings = self._collect_camera_settings()
        camera_live = self._collect_camera_live_snapshot()

        base = saved or ObservationState()
        return ObservationState(
            name=base.name,
            confocal_mode=self.is_confocal_mode(),
            camera_settings=camera_settings,
            illuminator_states=illuminator_states,
            z_offset_um=base.z_offset_um,
            confocal_hardware_settings=base.confocal_hardware_settings,
            display_color=base.display_color,
            channel_groups=base.channel_groups,
            emission_filter_positions=dict(emission_filter_positions or {}),
            camera_live=camera_live,
            enable_channel_auto_filter_switching=self.enable_channel_auto_filter_switching,
        )

    # ─────────────────────────────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────────────────────────────

    def _collect_camera_settings(self) -> Optional[CameraSettings]:
        try:
            exposure = float(self.camera.get_exposure_time())
        except Exception:
            return None
        try:
            gain = float(self.camera.get_analog_gain())
        except Exception:
            gain = 0.0
        pf = None
        try:
            pf_val = self.camera.get_pixel_format()
            pf = pf_val.value if hasattr(pf_val, "value") else str(pf_val)
        except Exception:
            pass
        return CameraSettings(exposure_time_ms=exposure, gain_mode=gain, pixel_format=pf)

    def _collect_camera_live_snapshot(self) -> Optional[CameraLiveSnapshot]:
        from control.core.observation_state_service import infer_roi_centered_from_camera

        try:
            exposure = float(self.camera.get_exposure_time())
        except Exception:
            return None
        try:
            gain = float(self.camera.get_analog_gain())
        except Exception:
            gain = 0.0
        try:
            mode_raw = self.camera.get_camera_mode()
            mode = mode_raw if isinstance(mode_raw, str) else (getattr(mode_raw, "value", str(mode_raw)) if mode_raw else None)
        except Exception:
            mode = None
        try:
            bx, by = self.camera.get_binning()
        except Exception:
            bx, by = 1, 1
        try:
            roi = self.camera.get_region_of_interest()
            rx, ry, rw, rh = int(roi[0]), int(roi[1]), int(roi[2]), int(roi[3])
        except Exception:
            rx, ry, rw, rh = 0, 0, 0, 0

        trigger_mode: Optional[str] = None
        trigger_fps: Optional[float] = None
        lc = self.live_controller
        if lc is not None:
            try:
                tm = lc.get_trigger_mode()
                trigger_mode = tm if isinstance(tm, str) else getattr(tm, "value", str(tm))
            except Exception:
                pass
            try:
                fps = float(getattr(lc, "fps_trigger", 0.0) or 0.0)
                if fps > 0:
                    trigger_fps = fps
            except Exception:
                pass

        pf = None
        try:
            pf_val = self.camera.get_pixel_format()
            pf = pf_val.value if hasattr(pf_val, "value") else str(pf_val)
        except Exception:
            pass

        return CameraLiveSnapshot(
            exposure_time_ms=exposure,
            analog_gain=gain,
            pixel_format=pf,
            camera_mode=mode,
            binning_x=bx,
            binning_y=by,
            roi_offset_x=rx,
            roi_offset_y=ry,
            roi_width=rw,
            roi_height=rh,
            trigger_mode=trigger_mode,
            trigger_fps=trigger_fps,
            roi_centered=infer_roi_centered_from_camera(self.camera),
        )

    def _apply_camera_live_snapshot(self, snap: CameraLiveSnapshot) -> None:
        """Apply ROI/binning/mode/trigger saved with the preset."""
        try:
            with self._time("obs:cls:set_exposure_time"):
                self.camera.set_exposure_time(snap.exposure_time_ms)
        except Exception as e:
            self._log.warning("Could not set exposure: %s", e)
        try:
            with self._time("obs:cls:set_analog_gain"):
                self.camera.set_analog_gain(snap.analog_gain)
        except Exception:
            pass
        if snap.pixel_format:
            try:
                from squid.config import CameraPixelFormat
                pf = getattr(CameraPixelFormat, snap.pixel_format, None)
                if pf is None:
                    for e in CameraPixelFormat:
                        if e.value == snap.pixel_format or e.name == snap.pixel_format:
                            pf = e
                            break
                if pf is not None:
                    with self._time("obs:cls:set_pixel_format"):
                        self.camera.set_pixel_format(pf)
            except Exception as e:
                self._log.warning("Could not set pixel format: %s", e)
        if snap.camera_mode is not None:
            try:
                with self._time("obs:cls:set_camera_mode"):
                    self.camera.set_camera_mode(snap.camera_mode)
            except Exception as e:
                self._log.warning("Could not set camera mode: %s", e)
        try:
            with self._time("obs:cls:set_binning"):
                self.camera.set_binning(snap.binning_x, snap.binning_y)
        except Exception as e:
            self._log.warning("Could not set binning: %s", e)
        if snap.roi_width > 0 and snap.roi_height > 0:
            try:
                with self._time("obs:cls:set_region_of_interest"):
                    self.camera.set_region_of_interest(
                        snap.roi_offset_x, snap.roi_offset_y, snap.roi_width, snap.roi_height
                    )
            except Exception as e:
                self._log.warning("Could not set ROI: %s", e)

        lc = self.live_controller
        if lc is not None:
            if snap.trigger_mode:
                try:
                    with self._time("obs:cls:set_trigger_mode"):
                        lc.set_trigger_mode(snap.trigger_mode)
                except Exception as e:
                    self._log.warning("Could not set trigger mode: %s", e)
            if snap.trigger_fps is not None and snap.trigger_fps > 0:
                try:
                    with self._time("obs:cls:set_trigger_fps"):
                        lc.set_trigger_fps(snap.trigger_fps)
                except Exception as e:
                    self._log.warning("Could not set trigger FPS: %s", e)

    @staticmethod
    def _merge_illumination_hardware(
        states: List[IlluminatorState], ic: Any
    ) -> List[IlluminatorState]:
        if ic is None:
            return states
        try:
            snap = ic.snapshot()
        except Exception:
            return states
        if snap is None or not getattr(snap, "channel_states", None):
            return states
        hw_states = snap.channel_states

        out: List[IlluminatorState] = []
        for ist in states:
            hw_name = ist.illumination_channel
            snap_key = hw_name
            if hasattr(ic, "snapshot_key_for_acquisition_illumination_channel"):
                try:
                    snap_key = ic.snapshot_key_for_acquisition_illumination_channel(hw_name) or hw_name
                except Exception:
                    snap_key = hw_name
            if snap_key not in hw_states:
                out.append(ist)
                continue
            st = hw_states[snap_key]
            out.append(ist.model_copy(update={"intensity": float(st.intensity), "on": bool(st.is_on)}))
        return out

    @staticmethod
    def _merge_led_matrix_mode(
        states: List[IlluminatorState], ic: Any
    ) -> List[IlluminatorState]:
        if ic is None or not getattr(ic, "has_unified_led_matrix", lambda: False)():
            return states
        mode = ic.get_led_matrix_mode()
        if mode is None:
            return states
        uses_matrix = getattr(ic, "illumination_maps_to_unified_led_matrix", None)
        unified = getattr(ic, "unified_led_matrix_channel_name", lambda: None)()
        out: List[IlluminatorState] = []
        for ist in states:
            hw = ist.illumination_channel
            applies = False
            if hw and uses_matrix is not None:
                try:
                    applies = bool(uses_matrix(hw))
                except Exception:
                    applies = False
            if not applies and unified is not None and hw == unified:
                applies = True
            out.append(ist.model_copy(update={"led_matrix_mode": mode}) if applies else ist)
        return out

    @staticmethod
    def _reconcile_with_hardware(
        states: List[IlluminatorState], ic: Any
    ) -> List[IlluminatorState]:
        if ic is None:
            return states
        hw_channels = set(getattr(ic, "channel_names", []))
        if not hw_channels:
            return states

        resolve = getattr(ic, "snapshot_key_for_acquisition_illumination_channel", None)

        def _ic_key(name: str):
            if resolve is not None:
                try:
                    return resolve(name)
                except Exception:
                    pass
            return name if name in hw_channels else None

        kept: list[IlluminatorState] = []
        seen_hw: set[str] = set()
        for ist in states:
            key = _ic_key(ist.illumination_channel)
            if key is not None and key in hw_channels:
                kept.append(ist)
                seen_hw.add(key)

        for ch in hw_channels:
            if ch not in seen_hw:
                try:
                    snap = ic.snapshot()
                    hw_st = snap.channel_states.get(ch)
                    intensity = float(hw_st.intensity) if hw_st else 0.0
                    on = bool(hw_st.is_on) if hw_st else False
                except Exception:
                    intensity, on = 0.0, False
                kept.append(IlluminatorState(illumination_channel=ch, intensity=intensity, on=on))

        return kept
