"""
Collect and apply Observation State (objective-free presets).

Pure control logic — no Qt. GUI calls these with LiveController / ConfigRepository /
ObjectiveStore from the main window.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union
import squid.logging

from control.models import GeneralObservationConfig
from control.models.observation_state import (
    CameraLiveSnapshot,
    CameraSettings,
    IlluminatorState,
    ObservationState,
)

if TYPE_CHECKING:
    from control.core.config.repository import ConfigRepository
    from control.core.live_controller import LiveController
    from control.core.objective_store import ObjectiveStore

logger = squid.logging.get_logger(__name__)

_PRESET_FILENAME_RE = re.compile(r"^[\w\- ]+$")


# ── Hardware reads (unchanged) ────────────────────────────────────────────────


def collect_emission_filter_positions(emission_filter_wheel: Optional[Any]) -> Dict[str, Union[str, int]]:
    """Read emission filter wheel positions for observation state and snap metadata."""
    emission: Dict[str, Union[str, int]] = {}
    if emission_filter_wheel and hasattr(emission_filter_wheel, "get_filter_wheel_position"):
        try:
            pos = emission_filter_wheel.get_filter_wheel_position()
            if isinstance(pos, dict):
                emission = {
                    str(k): int(v) if isinstance(v, (int, float)) else v for k, v in pos.items()
                }
        except Exception:
            pass
    return emission


def _pixel_format_to_optional_str(camera: Any) -> Optional[str]:
    try:
        pf = camera.get_pixel_format()
        return pf.value if hasattr(pf, "value") else str(pf)
    except Exception:
        return None


def infer_roi_centered_from_camera(camera: Any) -> bool:
    """
    Infer whether the ROI matches a centered layout (matches Camera tab "Centered" behavior).

    Uses the same even-offset rounding as the UI so the checkbox can be restored after load.
    """
    try:
        max_x, max_y = camera.get_resolution()
        ox, oy, w, h = camera.get_region_of_interest()
        ox, oy, w, h = int(ox), int(oy), int(w), int(h)
        exp_x = (max_x - w) / 2.0
        exp_y = (max_y - h) / 2.0
        exp_x = int(exp_x // 2) * 2
        exp_y = int(exp_y // 2) * 2
        return abs(ox - exp_x) <= 8 and abs(oy - exp_y) <= 8
    except Exception:
        return False


def _camera_mode_to_optional_str(mode: Any) -> Optional[str]:
    if mode is None:
        return None
    if isinstance(mode, str):
        return mode
    return getattr(mode, "value", str(mode))


# ── Camera live snapshot (unchanged) ──────────────────────────────────────────


def _collect_camera_live_snapshot(
    camera: Any,
    live_controller: Optional[Any] = None,
) -> Optional[CameraLiveSnapshot]:
    """Read exposure, gain, ROI, binning, mode, and live trigger settings from hardware."""
    try:
        exposure = float(camera.get_exposure_time())
    except Exception:
        return None
    try:
        gain = float(camera.get_analog_gain())
    except Exception:
        gain = 0.0
    try:
        mode = _camera_mode_to_optional_str(camera.get_camera_mode())
    except Exception:
        mode = None
    try:
        bx, by = camera.get_binning()
    except Exception:
        bx, by = 1, 1
    try:
        roi = camera.get_region_of_interest()
        rx, ry, rw, rh = int(roi[0]), int(roi[1]), int(roi[2]), int(roi[3])
    except Exception:
        rx, ry, rw, rh = 0, 0, 0, 0

    trigger_mode: Optional[str] = None
    trigger_fps: Optional[float] = None
    if live_controller is not None:
        try:
            tm = live_controller.get_trigger_mode()
            trigger_mode = tm if isinstance(tm, str) else getattr(tm, "value", str(tm))
        except Exception:
            trigger_mode = None
        try:
            fps = float(getattr(live_controller, "fps_trigger", 0.0) or 0.0)
            if fps > 0:
                trigger_fps = fps
        except Exception:
            trigger_fps = None

    roi_centered = infer_roi_centered_from_camera(camera)

    return CameraLiveSnapshot(
        exposure_time_ms=exposure,
        analog_gain=gain,
        pixel_format=_pixel_format_to_optional_str(camera),
        camera_mode=mode,
        binning_x=bx,
        binning_y=by,
        roi_offset_x=rx,
        roi_offset_y=ry,
        roi_width=rw,
        roi_height=rh,
        trigger_mode=trigger_mode,
        trigger_fps=trigger_fps,
        roi_centered=roi_centered,
    )


def _apply_camera_live_snapshot(
    camera: Any,
    snap: CameraLiveSnapshot,
    live_controller: Optional[Any] = None,
    *,
    apply_live_trigger_settings: bool = True,
) -> None:
    """Apply ROI/binning/mode/exposure/trigger saved with the preset to the camera and LiveController."""
    try:
        camera.set_exposure_time(snap.exposure_time_ms)
    except Exception as e:
        logger.warning("Observation State: could not set exposure: %s", e)
    try:
        camera.set_analog_gain(snap.analog_gain)
    except Exception:
        pass
    if snap.pixel_format:
        try:
            from squid.config import CameraPixelFormat

            # Stored string may match enum name or value
            pf = getattr(CameraPixelFormat, snap.pixel_format, None)
            if pf is None:
                for e in CameraPixelFormat:
                    if e.value == snap.pixel_format or e.name == snap.pixel_format:
                        pf = e
                        break
            if pf is not None:
                camera.set_pixel_format(pf)
        except Exception as e:
            logger.warning("Observation State: could not set pixel format: %s", e)
    if snap.camera_mode is not None:
        try:
            camera.set_camera_mode(snap.camera_mode)
        except Exception as e:
            logger.warning("Observation State: could not set camera mode: %s", e)
    try:
        camera.set_binning(snap.binning_x, snap.binning_y)
    except Exception as e:
        logger.warning("Observation State: could not set binning: %s", e)
    if snap.roi_width > 0 and snap.roi_height > 0:
        try:
            camera.set_region_of_interest(
                snap.roi_offset_x,
                snap.roi_offset_y,
                snap.roi_width,
                snap.roi_height,
            )
        except Exception as e:
            logger.warning("Observation State: could not set ROI: %s", e)

    if live_controller is not None and apply_live_trigger_settings:
        if snap.trigger_mode:
            try:
                live_controller.set_trigger_mode(snap.trigger_mode)
            except Exception as e:
                logger.warning("Observation State: could not set trigger mode: %s", e)
        if snap.trigger_fps is not None and snap.trigger_fps > 0:
            try:
                live_controller.set_trigger_fps(snap.trigger_fps)
            except Exception as e:
                logger.warning("Observation State: could not set trigger FPS: %s", e)


# ── New v3 helpers ────────────────────────────────────────────────────────────


def _collect_camera_settings_from_hardware(camera: Any) -> Optional[CameraSettings]:
    """Read exposure/gain/pixel_format from camera hardware into a CameraSettings."""
    try:
        exposure = float(camera.get_exposure_time())
    except Exception:
        return None
    try:
        gain = float(camera.get_analog_gain())
    except Exception:
        gain = 0.0
    pf = _pixel_format_to_optional_str(camera)
    return CameraSettings(
        exposure_time_ms=exposure,
        gain_mode=gain,
        pixel_format=pf,
    )


def _merge_illumination_hardware_into_illuminator_states(
    states: List[IlluminatorState],
    ic: Any,
) -> List[IlluminatorState]:
    """Overlay intensity/on from IlluminationController snapshot onto IlluminatorState list."""
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
        out.append(
            ist.model_copy(
                update={
                    "intensity": float(st.intensity),
                    "on": bool(st.is_on),
                }
            )
        )
    return out


def _merge_led_matrix_mode_into_illuminator_states(
    states: List[IlluminatorState],
    ic: Any,
) -> List[IlluminatorState]:
    """Overlay LED matrix mode from hardware onto matching illuminator states."""
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
        if applies:
            out.append(ist.model_copy(update={"led_matrix_mode": mode}))
        else:
            out.append(ist)
    return out


def _sync_illumination_hardware(ic: Any, illuminator_states: List[IlluminatorState]) -> None:
    """Push saved per-channel intensities and LED matrix mode to the illumination controller."""
    if ic is None:
        return
    uses_matrix = getattr(ic, "illumination_maps_to_unified_led_matrix", None)
    unified = getattr(ic, "unified_led_matrix_channel_name", lambda: None)()
    if getattr(ic, "has_unified_led_matrix", lambda: False)():
        for ist in illuminator_states:
            hw = ist.illumination_channel
            mode = ist.led_matrix_mode
            if not mode or not hw:
                continue
            applies = False
            if uses_matrix is not None:
                try:
                    applies = bool(uses_matrix(hw))
                except Exception:
                    applies = False
            if not applies and unified is not None and hw == unified:
                applies = True
            if not applies:
                continue
            try:
                ic.set_led_matrix_mode(mode)
                break
            except Exception as e:
                logger.warning("Could not apply LED matrix mode from Observation State: %s", e)
    for ist in illuminator_states:
        hw = ist.illumination_channel
        if not hw:
            continue
        try:
            ic.set_channel_intensity(hw, float(ist.intensity))
        except Exception as e:
            logger.warning("Could not set intensity for %r: %s", hw, e)


def _restore_illumination_on_off(ic: Any, illuminator_states: List[IlluminatorState]) -> None:
    """
    Restore logical illumination on/off state from IlluminatorState list.

    For LED matrix aliases, uses ``snapshot_key_for_acquisition_illumination_channel`` when available so
    the desired keys line up with ``ic.channel_names``.
    """
    if ic is None:
        return

    desired: Dict[str, bool] = {}
    for ist in illuminator_states:
        hw = ist.illumination_channel
        if not hw:
            continue
        value = bool(ist.on)

        key = hw
        if hasattr(ic, "snapshot_key_for_acquisition_illumination_channel"):
            try:
                key = ic.snapshot_key_for_acquisition_illumination_channel(hw) or hw
            except Exception:
                key = hw

        if key in desired and desired[key] != value:
            # Deterministic conflict rule: if any source indicates ON, keep ON.
            logger.warning(
                "Observation State: conflicting illumination on/off for %r (existing=%s new=%s); using ON",
                key,
                desired[key],
                value,
            )
            desired[key] = desired[key] or value
        else:
            desired[key] = value

    for name in getattr(ic, "channel_names", []):
        try:
            ic.set_channel_state(name, desired.get(name, False), force_hardware=True)
        except Exception as e:
            logger.warning("Could not restore illumination on/off state for %r: %s", name, e)


# ── Conversion helpers (internal) ────────────────────────────────────────────


def _collect_illuminator_states_from_observation_states(
    observation_states: List[ObservationState],
) -> List[IlluminatorState]:
    """Collect all unique illuminator states from a list of observation states.

    Merges illuminator states from all observation states, deduplicating by
    illumination_channel name (last write wins for intensity/on/led_matrix_mode).
    """
    seen: Dict[str, IlluminatorState] = {}
    for state in observation_states:
        for ist in state.illuminator_states:
            seen[ist.illumination_channel] = ist
    return list(seen.values())


# ── Binning/mode helpers ─────────────────────────────────────────────────────


def observation_state_binning_mode_for_metadata(
    state: Optional[ObservationState],
    camera: Optional[Any] = None,
) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """
    Resolve binning/mode for ``AcquisitionMetadata`` from observation state.

    If ``camera`` is given, fills any missing values from hardware (e.g. no profile / no state).
    """
    bx: Optional[int] = None
    by: Optional[int] = None
    cm: Optional[str] = None
    if state is not None:
        if state.camera_live is not None:
            bx = state.camera_live.binning_x
            by = state.camera_live.binning_y
            cm = _camera_mode_to_optional_str(state.camera_live.camera_mode)
    if camera is not None and (bx is None or by is None or cm is None):
        try:
            cx, cy = camera.get_binning()
            cx, cy = int(cx), int(cy)
        except Exception:
            cx, cy = 1, 1
        try:
            ccm = _camera_mode_to_optional_str(camera.get_camera_mode())
        except Exception:
            ccm = None
        if bx is None:
            bx = cx
        if by is None:
            by = cy
        if cm is None:
            cm = ccm
    return bx, by, cm


# ── Collect ───────────────────────────────────────────────────────────────────


def collect_observation_state(
    live_controller: "LiveController",
    config_repo: "ConfigRepository",
    objective_name: str,
    *,
    emission_filter_positions: Optional[Dict[str, Union[str, int]]] = None,
) -> ObservationState:
    """
    Build Observation State from the current live observation states and profile general config.

    Args:
        live_controller: Active live controller (current channel + confocal flag).
        config_repo: Repository for the active profile.
        objective_name: Current software objective (used only to read merged states; not stored).
        emission_filter_positions: Optional wheel positions from hardware/UI.
    """
    merged = live_controller.get_observation_states(objective_name)

    # Build illuminator_states from merged observation states
    illuminator_states = _collect_illuminator_states_from_observation_states(merged)

    # Merge hardware state into illuminator_states
    ic = getattr(live_controller.microscope, "illumination_controller", None)
    illuminator_states = _merge_illumination_hardware_into_illuminator_states(illuminator_states, ic)
    illuminator_states = _merge_led_matrix_mode_into_illuminator_states(illuminator_states, ic)

    # Determine the active state for z_offset, confocal_hardware_settings, display_color
    active_name: Optional[str] = None
    if live_controller.currentConfiguration is not None:
        active_name = live_controller.currentConfiguration.name
    else:
        fallback_fn = getattr(live_controller, "get_channel_name_for_contrast", None)
        if fallback_fn is not None:
            fallback = fallback_fn()
            if fallback != "default":
                active_name = fallback

    active_state: Optional[ObservationState] = None
    if active_name is not None:
        active_state = next((s for s in merged if s.name == active_name), None)
    if active_state is None and merged:
        active_state = merged[0]

    z_offset_um = active_state.z_offset_um if active_state else 0.0
    confocal_hw = active_state.confocal_hardware_settings if active_state else None
    display_color = active_state.display_color if active_state else "#FFFFFF"

    # Camera settings from hardware
    camera = live_controller.camera
    camera_settings = _collect_camera_settings_from_hardware(camera)

    # Camera live snapshot
    camera_live = _collect_camera_live_snapshot(camera, live_controller)

    auto_filter = getattr(live_controller, "enable_channel_auto_filter_switching", True)
    general = config_repo.get_general_config()
    channel_groups = list(general.channel_groups) if general else []

    return ObservationState(
        name="live",
        confocal_mode=live_controller.is_confocal_mode(),
        camera_settings=camera_settings,
        illuminator_states=illuminator_states,
        z_offset_um=z_offset_um,
        confocal_hardware_settings=confocal_hw,
        display_color=display_color,
        channel_groups=channel_groups,
        emission_filter_positions=dict(emission_filter_positions or {}),
        camera_live=camera_live,
        enable_channel_auto_filter_switching=auto_filter,
    )


# ── Apply ─────────────────────────────────────────────────────────────────────


def apply_observation_state(
    state: ObservationState,
    config_repo: "ConfigRepository",
    live_controller: "LiveController",
    objective_store: "ObjectiveStore",
    *,
    emission_filter_wheel: Optional[Any] = None,
    persist_general_to_profile: bool = True,
    apply_live_trigger_settings: bool = True,
    apply_illumination_on_off_state: bool = True,
) -> None:
    """
    Persist Observation State into general.yaml and refresh live mode.

    Args:
        persist_general_to_profile: When True (default), write preset channel rows into the profile's
            ``general.yaml``. When False, apply only to hardware and in-memory live state (no disk write).
        apply_live_trigger_settings: When True (default), restore the preset's saved live trigger mode/FPS.
            Set False for multipoint acquisitions.
        apply_illumination_on_off_state: When True (default), restore saved illumination channel on/off
            state through ``IlluminationController``. Set False for multipoint acquisitions.
    """
    profile = config_repo.current_profile
    if not profile:
        raise ValueError("No profile is set; cannot apply Observation State")

    # ── 1. Persist to general.yaml ──
    if persist_general_to_profile:
        general = GeneralObservationConfig(
            version=3,
            observation_states=[state],
            channel_groups=list(state.channel_groups),
        )
        existing = config_repo.get_general_config(profile)
        if existing is None or existing != general:
            config_repo.save_general_config(profile, general)

    # ── 2. Toggle confocal mode ──
    _t0 = time.perf_counter()
    live_controller.toggle_confocal_widefield(state.confocal_mode)
    logger.info("apply_observation_state: toggle_confocal_widefield took %.4fs", time.perf_counter() - _t0)

    # ── 3. Auto filter switching ──
    if state.enable_channel_auto_filter_switching is not None:
        try:
            live_controller.enable_channel_auto_filter_switching = bool(
                state.enable_channel_auto_filter_switching
            )
        except Exception as e:
            logger.warning("Observation State: could not set enable_channel_auto_filter_switching: %s", e)

    # ── 4. Emission filter wheel ──
    if state.emission_filter_positions and emission_filter_wheel is not None and hasattr(
        emission_filter_wheel, "set_filter_wheel_position"
    ):
        try:
            pos = {int(k): int(v) for k, v in state.emission_filter_positions.items()}
            _t0 = time.perf_counter()
            emission_filter_wheel.set_filter_wheel_position(pos)
            logger.info(
                "apply_observation_state: set_filter_wheel_position took %.4fs", time.perf_counter() - _t0
            )
        except Exception as e:
            logger.warning("Could not apply emission filter positions from Observation State: %s", e)

    # ── 5. Set camera exposure/gain from camera_settings ──
    camera = live_controller.camera
    if state.camera_settings is not None:
        _t0 = time.perf_counter()
        try:
            camera.set_exposure_time(state.camera_settings.exposure_time_ms)
        except Exception as e:
            logger.warning("Observation State: could not set exposure: %s", e)
        try:
            camera.set_analog_gain(state.camera_settings.gain_mode)
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
                    camera.set_pixel_format(pf)
            except Exception as e:
                logger.warning("Observation State: could not set pixel format: %s", e)
        logger.info("apply_observation_state: camera settings took %.4fs", time.perf_counter() - _t0)

    # ── 6. Apply camera_live snapshot (ROI, binning, trigger) ──
    if state.camera_live is not None:
        _t0 = time.perf_counter()
        _apply_camera_live_snapshot(
            camera,
            state.camera_live,
            live_controller,
            apply_live_trigger_settings=apply_live_trigger_settings,
        )
        logger.info(
            "apply_observation_state: _apply_camera_live_snapshot took %.4fs", time.perf_counter() - _t0
        )

    # ── 7. Sync illumination hardware ──
    ic = getattr(live_controller.microscope, "illumination_controller", None)

    _t0 = time.perf_counter()
    _sync_illumination_hardware(ic, state.illuminator_states)
    logger.info("apply_observation_state: _sync_illumination_hardware took %.4fs", time.perf_counter() - _t0)

    if apply_illumination_on_off_state:
        _t0 = time.perf_counter()
        _restore_illumination_on_off(ic, state.illuminator_states)
        logger.info(
            "apply_observation_state: _restore_illumination_on_off took %.4fs", time.perf_counter() - _t0
        )

    # ── 8. Update live controller's current observation state ──
    _t0 = time.perf_counter()
    live_controller.set_observation_state(state)
    logger.info("apply_observation_state: set_observation_state took %.4fs", time.perf_counter() - _t0)


# ── Preset path utilities (unchanged) ─────────────────────────────────────────


def sanitize_preset_filename(name: str) -> str:
    """User-visible preset name -> safe file stem."""
    stem = name.strip()
    if not stem:
        raise ValueError("Preset name is empty")
    if not _PRESET_FILENAME_RE.match(stem):
        raise ValueError("Preset name may only contain letters, numbers, spaces, hyphens, and underscores")
    safe = stem.replace(" ", "_")
    return safe


def observation_preset_path(
    config_repo: "ConfigRepository",
    preset_name: str,
    profile: Optional[str] = None,
) -> Path:
    """Absolute path for a named preset YAML under the given or current profile."""
    stem = sanitize_preset_filename(preset_name)
    return config_repo.get_profile_path(profile) / "observation_presets" / f"{stem}.yaml"


# ── YAML serialization ───────────────────────────────────────────────────────


def observation_state_to_yaml(
    state: ObservationState,
    *,
    camera_label: str = "camera",
) -> Dict[str, Any]:
    """
    Convert an internal ``ObservationState`` into the cleaned YAML v3 dict used by:
    - ``acquisition.yaml`` -> ``observation_states_used``
    - ``observation_presets/*.yaml``
    """
    # Camera settings
    camera_settings_dict: Optional[Dict[str, Any]] = None
    if state.camera_settings is not None:
        camera_settings_dict = state.camera_settings.model_dump(mode="json")

    # Illuminator states
    illuminator_out: List[Dict[str, Any]] = []
    for ist in state.illuminator_states:
        entry: Dict[str, Any] = {
            "illumination_channel": ist.illumination_channel,
            "intensity": ist.intensity,
            "on": ist.on,
        }
        if ist.led_matrix_mode is not None:
            entry["led_matrix_mode"] = ist.led_matrix_mode
        illuminator_out.append(entry)

    # Confocal hardware settings
    confocal_hw_dict: Optional[Dict[str, Any]] = None
    if state.confocal_hardware_settings is not None:
        confocal_hw_dict = state.confocal_hardware_settings.model_dump(mode="json")

    # Camera live snapshot
    camera_live_dict: Optional[Dict[str, Any]] = None
    if state.camera_live is not None:
        camera_live_dict = state.camera_live.model_dump(mode="json")

    # Build the camera_states block (per-camera)
    cam_state: Dict[str, Any] = {}
    if camera_settings_dict is not None:
        cam_state["camera_settings"] = camera_settings_dict
    cam_state["z_offset_um"] = state.z_offset_um
    cam_state["emission_filter_positions"] = dict(state.emission_filter_positions or {})
    if camera_live_dict is not None:
        cam_state["camera_live"] = camera_live_dict

    out: Dict[str, Any] = {
        "name": state.name,
        "version": state.version,
        "confocal_mode": bool(state.confocal_mode),
        "display_color": state.display_color,
        "illuminator_states": illuminator_out,
        "camera_states": {str(camera_label): cam_state},
        "channel_groups": [cg.model_dump(mode="json") for cg in state.channel_groups] if state.channel_groups else [],
    }

    if confocal_hw_dict is not None:
        out["confocal_hardware_settings"] = confocal_hw_dict
    if state.enable_channel_auto_filter_switching is not None:
        out["enable_channel_auto_filter_switching"] = bool(state.enable_channel_auto_filter_switching)

    return out
