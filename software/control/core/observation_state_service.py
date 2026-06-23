"""
Observation State utilities.

Hardware collection, application, and serialization logic lives in
``ObservationStateController``.  This module retains only the stateless helpers
that are shared across the codebase:

- ``collect_emission_filter_positions`` — read emission filter wheel hardware
- ``infer_roi_centered_from_camera`` — used by ObservationStateController
- ``observation_state_binning_mode_for_metadata`` — for AcquisitionMetadata
- ``observation_state_to_yaml`` — serialise ObservationState to YAML dict
- ``sanitize_preset_filename`` / ``observation_preset_path`` — preset path helpers
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

import squid.logging

from control.models.observation_state import (
    IlluminatorState,
    ObservationState,
)

if TYPE_CHECKING:
    from control.core.config.repository import ConfigRepository

logger = squid.logging.get_logger(__name__)

_PRESET_FILENAME_RE = re.compile(r"^[\w\- ]+$")


# ── Hardware reads ────────────────────────────────────────────────────────────


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


# ── FOV / ROI tiling consistency ─────────────────────────────────────────────


def observation_state_fov_mm(
    state: ObservationState, camera: Any, pixel_size_factor: float
) -> Optional[tuple[float, float]]:
    """Sample-frame FOV (width_mm, height_mm) that ``state`` will actually save.

    Derived from the state's recorded camera ROI/binning, matching the saved-frame
    geometry that ``AbstractCamera.get_crop_size`` produces: the delivered frame is the
    ROI, clamped to the configured software crop. ROI/crop are converted to unbinned
    sensor pixels so binning cancels and two states with the same physical ROI compare
    equal regardless of their binning. Returns ``None`` if the FOV can't be determined.
    """
    snap = state.camera_live
    try:
        sensor_um = float(camera.get_pixel_size_unbinned_um())
    except Exception:
        return None

    if snap is None or snap.roi_width <= 0 or snap.roi_height <= 0:
        # No explicit ROI recorded — fall back to the camera's current FOV.
        try:
            w_mm, h_mm = camera.get_fov_size_mm()
            return pixel_size_factor * w_mm, pixel_size_factor * h_mm
        except Exception:
            return None

    bx = max(1, int(snap.binning_x or 1))
    by = max(1, int(snap.binning_y or 1))
    # get_region_of_interest reports binned pixels; * binning -> unbinned sensor pixels.
    roi_w_unbinned = int(snap.roi_width) * bx
    roi_h_unbinned = int(snap.roi_height) * by

    cfg = getattr(camera, "_config", None)
    crop_w = getattr(cfg, "crop_width", None) if cfg is not None else None
    crop_h = getattr(cfg, "crop_height", None) if cfg is not None else None
    frame_w = min(roi_w_unbinned, int(crop_w)) if crop_w else roi_w_unbinned
    frame_h = min(roi_h_unbinned, int(crop_h)) if crop_h else roi_h_unbinned

    return frame_w * sensor_um * pixel_size_factor / 1000.0, frame_h * sensor_um * pixel_size_factor / 1000.0


def observation_state_roi_report(
    states: List[tuple[str, ObservationState]],
    camera: Any,
    pixel_size_factor: float,
    tolerance_mm: float = 1e-4,
) -> Dict[str, Any]:
    """Compare the FOVs of observation states sharing one acquisition.

    Tiling overlap is computed for the *largest* FOV so the most complete channel is
    fully covered; smaller-ROI states then under-sample (intentional or not), which is
    flagged via ``mismatch`` so the UI can require user approval.

    Returns a dict with:
      - ``entries``: ``[{"name", "fov_mm"}]`` per state (fov_mm may be None)
      - ``tiling_fov_mm``: (w, h) of the largest FOV, or None
      - ``largest_name``: name of the state with the largest FOV
      - ``mismatch_names``: states whose FOV differs from the largest
      - ``mismatch``: True if any state differs beyond ``tolerance_mm``
    """
    entries = [{"name": name, "fov_mm": observation_state_fov_mm(st, camera, pixel_size_factor)} for name, st in states]
    valid = [e for e in entries if e["fov_mm"] is not None]
    if not valid:
        return {"entries": entries, "tiling_fov_mm": None, "largest_name": None, "mismatch_names": [], "mismatch": False}

    largest = max(valid, key=lambda e: e["fov_mm"][0] * e["fov_mm"][1])
    tiling = largest["fov_mm"]
    mismatch_names = [
        e["name"]
        for e in valid
        if abs(e["fov_mm"][0] - tiling[0]) > tolerance_mm or abs(e["fov_mm"][1] - tiling[1]) > tolerance_mm
    ]
    return {
        "entries": entries,
        "tiling_fov_mm": tiling,
        "largest_name": largest["name"],
        "mismatch_names": mismatch_names,
        "mismatch": bool(mismatch_names),
    }


# ── Binning/mode helpers ─────────────────────────────────────────────────────


def _camera_mode_to_optional_str(mode: Any) -> Optional[str]:
    if mode is None:
        return None
    if isinstance(mode, str):
        return mode
    return getattr(mode, "value", str(mode))


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


# ── Preset path utilities ─────────────────────────────────────────────────────


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


def acquisition_cycle_path(
    config_repo: "ConfigRepository",
    cycle_name: str,
    profile: Optional[str] = None,
) -> Path:
    """Absolute path for a named acquisition-cycle YAML under the given profile."""
    stem = sanitize_preset_filename(cycle_name)
    return config_repo.get_profile_path(profile) / "cycles" / f"{stem}.yaml"


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
        for _fld in (
            "led_matrix_bf_na",
            "led_matrix_df_na",
            "led_matrix_lowna_na",
            "led_matrix_dpc_na",
            "led_matrix_inner_na",
            "led_matrix_outer_na",
            "led_matrix_single_led_index",
            "led_matrix_color",
        ):
            _val = getattr(ist, _fld)
            if _val is not None:
                entry[_fld] = _val
        if ist.timing is not None:
            entry["timing"] = {
                "start_offset_ms": ist.timing.start_offset_ms,
                "pulse_width_ms": ist.timing.pulse_width_ms,
                "period_ms": ist.timing.period_ms,
                "num_pulses": ist.timing.num_pulses,
            }
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

    if state.is_stimulus_only:
        out["is_stimulus_only"] = True
        if state.stimulus_duration_ms is not None:
            out["stimulus_duration_ms"] = float(state.stimulus_duration_ms)

    return out
