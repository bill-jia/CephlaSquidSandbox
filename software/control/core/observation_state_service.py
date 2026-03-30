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

from control.models import AcquisitionChannel, GeneralChannelConfig
from control.models.observation_state import CameraLiveSnapshot, ObservationState

if TYPE_CHECKING:
    from control.core.config.repository import ConfigRepository
    from control.core.live_controller import LiveController
    from control.core.objective_store import ObjectiveStore

logger = logging.getLogger(__name__)

_PRESET_FILENAME_RE = re.compile(r"^[\w\- ]+$")


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


def _camera_mode_to_optional_str(mode: Any) -> Optional[str]:
    if mode is None:
        return None
    if isinstance(mode, str):
        return mode
    return getattr(mode, "value", str(mode))


def _read_binning_and_mode_from_camera(camera: Any) -> tuple[int, int, Optional[str]]:
    """Fallback when ``camera_live`` could not be built (e.g. exposure read failed)."""
    try:
        bx, by = camera.get_binning()
        bx, by = int(bx), int(by)
    except Exception:
        bx, by = 1, 1
    try:
        mode = _camera_mode_to_optional_str(camera.get_camera_mode())
    except Exception:
        mode = None
    return bx, by, mode


def _top_level_binning_mode_from_camera(
    camera: Any, camera_live: Optional[CameraLiveSnapshot]
) -> tuple[int, int, Optional[str]]:
    """Values stored on ``ObservationState`` for presets and metadata (mirrors ``camera_live`` when present)."""
    if camera_live is not None:
        return (
            camera_live.binning_x,
            camera_live.binning_y,
            _camera_mode_to_optional_str(camera_live.camera_mode),
        )
    return _read_binning_and_mode_from_camera(camera)


def observation_state_binning_mode_for_metadata(
    state: Optional[ObservationState],
    camera: Optional[Any] = None,
) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """
    Resolve binning/mode for ``AcquisitionMetadata`` from observation state (top-level or nested).

    If ``camera`` is given, fills any missing values from hardware (e.g. no profile / no state).
    """
    bx: Optional[int] = None
    by: Optional[int] = None
    cm: Optional[str] = None
    if state is not None:
        if state.binning_x is not None and state.binning_y is not None:
            bx, by = state.binning_x, state.binning_y
            cm = _camera_mode_to_optional_str(state.camera_mode)
        elif state.camera_live is not None:
            bx = state.camera_live.binning_x
            by = state.camera_live.binning_y
            cm = _camera_mode_to_optional_str(state.camera_live.camera_mode)
        else:
            cm = _camera_mode_to_optional_str(state.camera_mode)
    if camera is not None and (bx is None or by is None or cm is None):
        cx, cy, ccm = _read_binning_and_mode_from_camera(camera)
        if bx is None:
            bx = cx
        if by is None:
            by = cy
        if cm is None:
            cm = ccm
    return bx, by, cm


def _apply_top_level_binning_mode_if_needed(
    camera: Any,
    state: ObservationState,
    camera_live_applied: bool,
) -> None:
    """If there is no ``camera_live`` snapshot, apply top-level binning/mode when present."""
    if camera_live_applied:
        return
    if state.binning_x is not None and state.binning_y is not None:
        try:
            camera.set_binning(state.binning_x, state.binning_y)
        except Exception as e:
            logger.warning("Observation State: could not set binning from top-level fields: %s", e)
    if state.camera_mode is not None:
        try:
            camera.set_camera_mode(_camera_mode_to_optional_str(state.camera_mode))
        except Exception as e:
            logger.warning("Observation State: could not set camera mode from top-level field: %s", e)


def _merge_illumination_hardware_into_channels(
    channels: List[AcquisitionChannel],
    ill_snapshot: Any,
    ic: Any,
) -> List[AcquisitionChannel]:
    """Overlay intensity from IlluminationController.snapshot() onto each channel row."""
    if ill_snapshot is None or not getattr(ill_snapshot, "channel_states", None):
        return channels
    states = ill_snapshot.channel_states
    out: List[AcquisitionChannel] = []
    for ch in channels:
        hw = ch.illumination_settings.illumination_channel
        if not hw:
            out.append(ch)
            continue
        snap_key = hw
        if ic is not None and hasattr(ic, "snapshot_key_for_acquisition_illumination_channel"):
            try:
                snap_key = ic.snapshot_key_for_acquisition_illumination_channel(hw) or hw
            except Exception:
                snap_key = hw
        if snap_key not in states:
            out.append(ch)
            continue
        st = states[snap_key]
        new_ill = ch.illumination_settings.model_copy(
            update={"intensity": float(st.intensity), "on": bool(st.is_on)}
        )
        out.append(ch.model_copy(update={"illumination_settings": new_ill}))
    return out


def _merge_led_matrix_mode_into_channels(
    channels: List[AcquisitionChannel],
    ic: Any,
) -> List[AcquisitionChannel]:
    if ic is None or not getattr(ic, "has_unified_led_matrix", lambda: False)():
        return channels
    mode = ic.get_led_matrix_mode()
    if mode is None:
        return channels
    uses_matrix = getattr(ic, "illumination_maps_to_unified_led_matrix", None)
    unified = getattr(ic, "unified_led_matrix_channel_name", lambda: None)()
    out: List[AcquisitionChannel] = []
    for ch in channels:
        hw = ch.illumination_settings.illumination_channel
        applies = False
        if hw and uses_matrix is not None:
            try:
                applies = bool(uses_matrix(hw))
            except Exception:
                applies = False
        if not applies and unified is not None and hw == unified:
            applies = True
        if applies:
            new_ill = ch.illumination_settings.model_copy(update={"led_matrix_mode": mode})
            out.append(ch.model_copy(update={"illumination_settings": new_ill}))
        else:
            out.append(ch)
    return out


def _merge_active_channel_camera_from_hardware(
    channels: List[AcquisitionChannel],
    camera: Any,
) -> List[AcquisitionChannel]:
    """Copy live camera exposure/gain/pixel format onto the active acquisition channel row."""
    if not channels:
        return channels
    if target is None:
        target = channels[0].name
    try:
        exp = float(camera.get_exposure_time())
        gain = float(camera.get_analog_gain())
    except Exception:
        return channels
    pf = _pixel_format_to_optional_str(camera)
    out: List[AcquisitionChannel] = []
    for ch in channels:
        if ch.name != target:
            out.append(ch)
            continue
        new_cs = ch.camera_settings.model_copy(
            update={
                "exposure_time_ms": exp,
                "gain_mode": gain,
                "pixel_format": pf,
            }
        )
        out.append(ch.model_copy(update={"camera_settings": new_cs}))
    return out


def _overlay_preset_channels_onto_merged(
    merged: List[AcquisitionChannel],
    preset: List[AcquisitionChannel],
) -> List[AcquisitionChannel]:
    """
    After get_channels(), overlay tunables from the saved preset.

    Objective YAML can override general.yaml on merge; the preset must still win
    for illumination intensity, LED matrix mode, camera settings, and filter position.
    """
    preset_by_name = {c.name: c for c in preset}
    out: List[AcquisitionChannel] = []
    for ch in merged:
        p = preset_by_name.get(ch.name)
        if p is None:
            out.append(ch)
            continue
        new_ill = ch.illumination_settings.model_copy(
            update={
                "intensity": p.illumination_settings.intensity,
                "led_matrix_mode": p.illumination_settings.led_matrix_mode,
                "on": p.illumination_settings.on,
            }
        )
        new_cam = p.camera_settings.model_copy()
        fp = p.filter_position if p.filter_position is not None else ch.filter_position
        out.append(
            ch.model_copy(
                update={
                    "illumination_settings": new_ill,
                    "camera_settings": new_cam,
                    "filter_position": fp,
                }
            )
        )
    return out


def _sync_illumination_hardware_from_channels(ic: Any, channels: List[AcquisitionChannel]) -> None:
    """Push saved per-channel intensities and LED matrix mode to the illumination controller."""
    if ic is None:
        return
    uses_matrix = getattr(ic, "illumination_maps_to_unified_led_matrix", None)
    unified = getattr(ic, "unified_led_matrix_channel_name", lambda: None)()
    if getattr(ic, "has_unified_led_matrix", lambda: False)():
        for ch in channels:
            hw = ch.illumination_settings.illumination_channel
            mode = ch.illumination_settings.led_matrix_mode
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
    for ch in channels:
        hw = ch.illumination_settings.illumination_channel
        if not hw:
            continue
        try:
            ic.set_channel_intensity(hw, float(ch.illumination_intensity))
        except Exception as e:
            logger.warning("Could not set intensity for %r: %s", hw, e)


def _restore_illumination_on_off_from_channels(ic: Any, channels: List[AcquisitionChannel]) -> None:
    """
    Restore logical illumination on/off state from per-channel `illumination_settings.on`.

    For LED matrix aliases, uses `snapshot_key_for_acquisition_illumination_channel` when available so
    the desired keys line up with `ic.channel_names`.
    """
    if ic is None:
        return

    desired: Dict[str, bool] = {}
    for ch in channels:
        hw = ch.illumination_settings.illumination_channel
        if not hw:
            continue
        value = bool(ch.illumination_settings.on)

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
            if desired.get(name, False):
                ic.turn_on_channel(name)
            else:
                ic.turn_off_channel(name)
        except Exception as e:
            logger.warning("Could not restore illumination on/off state for %r: %s", name, e)


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


def project_merged_channels_for_observation_preset(channels: List[AcquisitionChannel]) -> List[AcquisitionChannel]:
    """
    Strip objective-specific confocal override blocks from merged channels for a general-layer preset.

    Preserves current tunables (intensity, exposure, etc.) while avoiding stale
    objective-only confocal_override when the preset is applied under another objective.
    """
    out: List[AcquisitionChannel] = []
    for ch in channels:
        out.append(ch.model_copy(update={"confocal_override": None}))
    return out


def collect_observation_state(
    live_controller: "LiveController",
    config_repo: "ConfigRepository",
    objective_name: str,
    *,
    emission_filter_positions: Optional[Dict[str, Union[str, int]]] = None,
) -> ObservationState:
    """
    Build Observation State from the current live merged channels and profile general config.

    Args:
        live_controller: Active live controller (current channel + confocal flag).
        config_repo: Repository for the active profile.
        objective_name: Current software objective (used only to read merged channels; not stored).
        emission_filter_positions: Optional wheel positions from hardware/UI.
    """
    merged = live_controller.get_channels(objective_name)
    channels = project_merged_channels_for_observation_preset(merged)
    ic = getattr(live_controller.microscope, "illumination_controller", None)
    try:
        snap = ic.snapshot() if ic is not None else None
    except Exception:
        snap = None
    channels = _merge_illumination_hardware_into_channels(channels, snap, ic)
    channels = _merge_led_matrix_mode_into_channels(channels, ic)
    # Same logical channel as Live Control / Napari (set via set_active_channel_reference or set_microscope_mode).
    active: Optional[str] = None
    if live_controller.currentConfiguration is not None:
        active = live_controller.currentConfiguration.name
    else:
        fallback = live_controller.get_channel_name_for_contrast()
        if fallback != "default":
            active = fallback
    camera = live_controller.camera
    channels = _merge_active_channel_camera_from_hardware(channels, active, camera)
    camera_live = _collect_camera_live_snapshot(camera, live_controller)
    bx, by, cmode = _top_level_binning_mode_from_camera(camera, camera_live)
    auto_filter = getattr(live_controller, "enable_channel_auto_filter_switching", True)
    general = config_repo.get_general_config()
    channel_groups = list(general.channel_groups) if general else []
    return ObservationState(
        name="live",
        confocal_mode=live_controller.is_confocal_mode(),
        channels=channels,
        channel_groups=channel_groups,
        emission_filter_positions=dict(emission_filter_positions or {}),
        camera_live=camera_live,
        binning_x=bx,
        binning_y=by,
        camera_mode=cmode,
        enable_channel_auto_filter_switching=auto_filter,
    )


def apply_observation_state(
    state: ObservationState,
    config_repo: "ConfigRepository",
    live_controller: "LiveController",
    objective_store: "ObjectiveStore",
    *,
    emission_filter_wheel: Optional[Any] = None,
    persist_general_to_profile: bool = True,
    apply_live_trigger_settings: bool = True,
    apply_illumination_on_off_state: bool = True
) -> None:
    """
    Persist Observation State into general.yaml and refresh live mode.

    Merges with the current objective's objective.yaml when resolving channels via LiveController.

    Args:
        persist_general_to_profile: When True (default), write preset channel rows into the profile's
            ``general.yaml``. When False, apply only to hardware and in-memory live state (no disk write).
            Use False when cycling multiple presets during one acquisition so the on-disk profile is not
            rewritten at every step.
        apply_live_trigger_settings: When True (default), restore the preset's saved live trigger mode/FPS.
            Set False for multipoint acquisitions, which manage trigger behavior separately and should not
            inherit live-view continuous/streaming settings from an Observation State.
        apply_illumination_on_off_state: When True (default), restore saved illumination channel on/off
            state through ``IlluminationController``. Set False for multipoint acquisitions, which manage
            illumination per capture and should not inherit manual live-view on/off state.
    """
    profile = config_repo.current_profile
    if not profile:
        raise ValueError("No profile is set; cannot apply Observation State")

    general = GeneralChannelConfig(
        version=state.version,
        channels=list(state.channels),
        channel_groups=list(state.channel_groups),
    )
    if persist_general_to_profile:
        existing = config_repo.get_general_config(profile)
        if existing is None or existing != general:
            config_repo.save_general_config(profile, general)

    _t0 = time.perf_counter()
    live_controller.toggle_confocal_widefield(state.confocal_mode)
    logger.info("apply_observation_state: toggle_confocal_widefield took %.4fs", time.perf_counter() - _t0)

    if state.enable_channel_auto_filter_switching is not None:
        try:
            live_controller.enable_channel_auto_filter_switching = bool(
                state.enable_channel_auto_filter_switching
            )
        except Exception as e:
            logger.warning("Observation State: could not set enable_channel_auto_filter_switching: %s", e)

    if state.emission_filter_positions and emission_filter_wheel is not None and hasattr(
        emission_filter_wheel, "set_filter_wheel_position"
    ):
        try:
            pos = {int(k): int(v) for k, v in state.emission_filter_positions.items()}
            _t0 = time.perf_counter()
            emission_filter_wheel.set_filter_wheel_position(pos)
            logger.info("apply_observation_state: set_filter_wheel_position took %.4fs", time.perf_counter() - _t0)
        except Exception as e:
            logger.warning("Could not apply emission filter positions from Observation State: %s", e)

    objective = objective_store.current_objective
    merged = live_controller.get_channels(objective)
    if not merged:
        logger.warning("apply_observation_state: no channels available for objective %r", objective)
        return

    channels = _overlay_preset_channels_onto_merged(merged, state.channels)
    ic = getattr(live_controller.microscope, "illumination_controller", None)

    # There is no canonical "active channel" in the YAML v2 view.
    # Select any microscope mode row based on which illumination channels are enabled.
    match = next((c for c in channels if bool(getattr(c.illumination_settings, "on", False))), None)
    if match is None:
        match = channels[0] if channels else None

    _t0 = time.perf_counter()
    if match is not None:
        live_controller.set_microscope_mode(match)
    else:
        logger.warning(
            "apply_observation_state: no enabled illumination channel found for objective %r",
            objective,
        )
        live_controller.set_microscope_mode(channels[0])
    logger.info("apply_observation_state: set_microscope_mode took %.4fs", time.perf_counter() - _t0)

    camera_live_applied = False
    if state.camera_live is not None:
        _t0 = time.perf_counter()
        _apply_camera_live_snapshot(
            live_controller.camera,
            state.camera_live,
            live_controller,
            apply_live_trigger_settings=apply_live_trigger_settings,
        )
        logger.info("apply_observation_state: _apply_camera_live_snapshot took %.4fs", time.perf_counter() - _t0)
        camera_live_applied = True

    _apply_top_level_binning_mode_if_needed(live_controller.camera, state, camera_live_applied)

    _t0 = time.perf_counter()
    _sync_illumination_hardware_from_channels(ic, channels)
    logger.info("apply_observation_state: _sync_illumination_hardware took %.4fs", time.perf_counter() - _t0)
    if apply_illumination_on_off_state:
        _t0 = time.perf_counter()
        _restore_illumination_on_off_from_channels(ic, channels)
        logger.info("apply_observation_state: _restore_illumination_on_off took %.4fs", time.perf_counter() - _t0)


def sanitize_preset_filename(name: str) -> str:
    """User-visible preset name → safe file stem."""
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


def observation_state_to_yaml(
    state: ObservationState,
    *,
    camera_label: str = "camera",
) -> Dict[str, Any]:
    """
    Convert an internal `ObservationState` into the cleaned YAML v2 "view" used by:
    - `acquisition.yaml` -> `observation_states_used`
    - `observation_presets/*.yaml`

    This function is intentionally *not* a 1:1 mapping to the internal model:
    - removes `active_channel_name`
    - strips camera/filter/z_offset/display fields from per-illumination channel entries
    - moves camera + emission filter wheel + z offset into top-level `camera_states`
    """
    if state.channels:
        on_channels = [ch for ch in state.channels if bool(ch.illumination_settings.on)]
        canonical = on_channels[0] if on_channels else state.channels[0]
    else:
        canonical = None

    camera_settings: Optional[Dict[str, Any]] = None
    z_offset_um: Optional[float] = None
    if canonical is not None:
        z_offset_um = float(canonical.z_offset_um)

    # Derive camera settings from the live snapshot (camera hardware state),
    # not from any particular illumination channel row.
    if state.camera_live is not None:
        camera_settings = {
            "exposure_time_ms": float(state.camera_live.exposure_time_ms),
            "gain_mode": float(state.camera_live.analog_gain),
            "pixel_format": state.camera_live.pixel_format,
        }
    elif canonical is not None:
        camera_settings = canonical.camera_settings.model_dump(mode="json")

    # Per internal logic, excitation filter wheel info isn't consistently represented today.
    # Keep it as an optional/empty map in the view.
    excitation_filter_positions: Dict[str, Union[str, int]] = {}
    camera_emission_filter_positions: Dict[str, Union[str, int]] = dict(state.emission_filter_positions or {})

    # If we couldn't collect wheel positions at save-time, fall back to the channel's
    # configured filter_position (many systems effectively treat this as the wheel position).
    if not camera_emission_filter_positions and canonical is not None and canonical.filter_position is not None:
        camera_emission_filter_positions = {"1": int(canonical.filter_position)}

    channel_out: List[Dict[str, Any]] = []
    for ch in state.channels:
        out: Dict[str, Any] = {
            "name": ch.name,
            "enabled": bool(ch.enabled),
            "illumination_settings": ch.illumination_settings.model_dump(mode="json"),
        }
        if ch.confocal_hardware_settings is not None:
            out["confocal_hardware_settings"] = ch.confocal_hardware_settings.model_dump(mode="json")
        channel_out.append(out)

    cam_state: Dict[str, Any] = {}
    if camera_settings is not None:
        cam_state["camera_settings"] = camera_settings
    if z_offset_um is not None:
        cam_state["z_offset_um"] = z_offset_um
    cam_state["emission_filter_positions"] = camera_emission_filter_positions
    if state.camera_live is not None:
        cam_state["camera_live"] = state.camera_live.model_dump(mode="json")

    out: Dict[str, Any] = {
        "name": state.name,
        "version": state.version,
        "confocal_mode": bool(state.confocal_mode),
        "channels": channel_out,
        "channel_groups": [cg.model_dump(mode="json") for cg in state.channel_groups] if state.channel_groups else [],
        "camera_states": {str(camera_label): cam_state},
    }

    if excitation_filter_positions:
        out["excitation_filter_positions"] = excitation_filter_positions
    if state.enable_channel_auto_filter_switching is not None:
        out["enable_channel_auto_filter_switching"] = bool(state.enable_channel_auto_filter_switching)

    return out
