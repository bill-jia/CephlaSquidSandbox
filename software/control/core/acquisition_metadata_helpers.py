"""
Shared construction of AcquisitionMetadata for snap, multipoint, and fast acquisition.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import yaml

from control.models.acquisition_metadata import AcquisitionMetadata
from control.core.observation_state_service import observation_state_to_yaml
from control.core.config import ConfigRepository

if TYPE_CHECKING:
    from control.core.live_controller import LiveController
    from control.core.objective_store import ObjectiveStore
    from control.models.observation_state import ObservationState


def build_acquisition_metadata(
    *,
    experiment_id: str,
    recording_start_time: Optional[float] = None,
    objective_store: "ObjectiveStore",
    live_controller: "LiveController",
    camera: Any,
    scan_parameters: Dict[str, Any],
    observation_state: Optional["ObservationState"] = None,
    selected_channel_names: Optional[List[str]] = None,
    selected_observation_state_names: Optional[List[str]] = None,
) -> AcquisitionMetadata:
    """Build canonical AcquisitionMetadata using the same rules as live snap."""
    from control._def import TUBE_LENS_MM
    from control.core.observation_state_service import observation_state_binning_mode_for_metadata

    t0 = recording_start_time if recording_start_time is not None else time.time()
    current_objective = objective_store.current_objective
    objective_details: Dict[str, Any] = {}
    try:
        objective_details = dict(objective_store.objectives_dict.get(current_objective, {}))
        objective_details["name"] = current_objective
    except (AttributeError, KeyError, TypeError):
        objective_details = {"name": current_objective}

    try:
        trigger_mode = str(live_controller.get_trigger_mode())
    except Exception:
        trigger_mode = None

    ch_names = list(selected_channel_names) if selected_channel_names is not None else []
    os_names = list(selected_observation_state_names) if selected_observation_state_names is not None else []

    try:
        sensor_px = float(camera.get_pixel_size_binned_um())
    except Exception:
        sensor_px = None

    bx, by, cm = observation_state_binning_mode_for_metadata(observation_state, camera)

    return AcquisitionMetadata(
        experiment_id=experiment_id,
        recording_start_time=t0,
        objective=current_objective,
        objective_details=objective_details,
        confocal_mode=live_controller.obs_controller.is_confocal_mode() if live_controller.obs_controller else False,
        sensor_pixel_size_um=sensor_px,
        tube_lens_mm=TUBE_LENS_MM,
        trigger_mode=trigger_mode,
        binning_x=bx,
        binning_y=by,
        camera_mode=cm,
        selected_channel_names=ch_names,
        selected_observation_state_names=os_names,
        scan_parameters=dict(scan_parameters),
        observation_state=observation_state,
    )


def multipoint_legacy_acquisition_parameters_dict(
    *,
    delta_x: float,
    nx: int,
    delta_y: float,
    ny: int,
    delta_z_mm: float,
    nz: int,
    deltat: float,
    nt: int,
    do_autofocus: bool,
    do_reflection_af: bool,
    use_manual_focus_map: bool,
    objective_block: Optional[Dict[str, Any]],
    sensor_pixel_size_um: Optional[float],
    tube_lens_mm: float,
    confocal_mode: bool,
) -> Dict[str, Any]:
    """Legacy flat dict shape (formerly ``acquisition parameters.json``); tooling may rebuild this from ``acquisition.yaml``."""
    acquisition_parameters: Dict[str, Any] = {
        "dx(mm)": delta_x,
        "Nx": nx,
        "dy(mm)": delta_y,
        "Ny": ny,
        "dz(um)": delta_z_mm * 1000 if delta_z_mm != 0 else 1,
        "Nz": nz,
        "dt(s)": deltat,
        "Nt": nt,
        "with AF": do_autofocus,
        "with laser AF": do_reflection_af,
        "with manual focus map": use_manual_focus_map,
    }
    if objective_block is not None:
        acquisition_parameters["objective"] = dict(objective_block)
    acquisition_parameters["sensor_pixel_size_um"] = sensor_pixel_size_um
    acquisition_parameters["tube_lens_mm"] = tube_lens_mm
    acquisition_parameters["confocal_mode"] = confocal_mode
    return acquisition_parameters


def augment_multipoint_acquisition_yaml_dict(
    base_yaml: Dict[str, Any],
    *,
    experiment_id: str,
    recording_start_time: float,
    repo: ConfigRepository,
    objective_store: Any,
    live_controller: Any,
    camera: Any,
    selected_configurations: List[Any],
    obs_names: List[str],
    logger: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Merge layout YAML (grid, objective, z-stack, …) with a non-duplicative ``manifest`` slice
    and ``observation_states_used`` (full preset snapshots for selected names only).
    """
    from control.core.observation_state_service import collect_emission_filter_positions, collect_observation_state

    _log = logger or logging.getLogger(__name__)
    uses_presets = bool(obs_names)

    obs_state: Optional[Any] = None
    if not uses_presets and repo.current_profile:
        try:
            wheel = getattr(live_controller.microscope, "emission_filter_wheel", None)
            emission = collect_emission_filter_positions(wheel)
            obs_state = collect_observation_state(
                live_controller,
                repo,
                emission_filter_positions=emission or None,
            )
        except Exception as e:
            _log.warning("Could not collect observation state for multipoint metadata: %s", e)

    resolved_channel_names: List[str] = []
    if uses_presets:
        for pname in obs_names:
            st = repo.load_observation_preset(pname)
            if st and st.illuminator_states:
                active = st.active_illuminator_states
                ist = active[0] if active else st.illuminator_states[0]
                resolved_channel_names.append(ist.illumination_channel)
    else:
        resolved_channel_names = [c.name for c in selected_configurations]

    acquisition_metadata = build_acquisition_metadata(
        experiment_id=experiment_id,
        recording_start_time=recording_start_time,
        objective_store=objective_store,
        live_controller=live_controller,
        camera=camera,
        scan_parameters={},
        observation_state=obs_state if not uses_presets else None,
        selected_channel_names=resolved_channel_names,
        selected_observation_state_names=list(obs_names) if uses_presets else None,
    )
    manifest = acquisition_metadata.model_dump(mode="json", exclude_none=True)
    for k in ("experiment_id", "objective", "objective_details", "scan_parameters"):
        manifest.pop(k, None)
    obj_block = base_yaml.get("objective") or {}
    if obj_block.get("sensor_pixel_size_um") is not None:
        manifest.pop("sensor_pixel_size_um", None)
    if uses_presets:
        manifest.pop("selected_channel_names", None)
        manifest.pop("selected_observation_state_names", None)

    observation_states_used: Dict[str, Any] = {}
    if uses_presets:

        camera_label = getattr(camera, "name", None) or getattr(camera, "serial_number", None) or type(camera).__name__ or "camera"
        for pname in obs_names:
            st = repo.load_observation_preset(pname)
            if st is not None:
                observation_states_used[pname] = observation_state_to_yaml(st, camera_label=str(camera_label))

    out: Dict[str, Any] = {"schema_version": 2}
    out.update(base_yaml)
    out["manifest"] = manifest
    if observation_states_used:
        out["observation_states_used"] = observation_states_used
    return out


def legacy_flat_multipoint_from_acquisition_yaml_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild the legacy flat dict (formerly ``acquisition parameters.json``) from ``acquisition.yaml``."""
    from control._def import TUBE_LENS_MM

    manifest = data.get("manifest") or {}
    acq = data.get("acquisition", {})
    obj_yaml = data.get("objective", {})
    z_stack = data.get("z_stack", {})
    time_series = data.get("time_series", {})
    flex = data.get("flexible_scan", {})
    wp = data.get("wellplate_scan", {})
    af = data.get("autofocus", {})

    dz_mm = z_stack.get("delta_z_mm")
    if dz_mm is None:
        dz_um = 1.0
    else:
        dz_f = float(dz_mm)
        dz_um = dz_f * 1000.0 if dz_f != 0 else 1.0

    if flex:
        dx = float(flex.get("delta_x_mm", 0.9))
        dy = float(flex.get("delta_y_mm", 0.9))
        nx = int(flex.get("nx", 1))
        ny = int(flex.get("ny", 1))
    elif wp:
        dx = float(wp.get("delta_x_mm", 0.9))
        dy = float(wp.get("delta_y_mm", 0.9))
        nx = int(wp.get("nx", 1))
        ny = int(wp.get("ny", 1))
    else:
        dx, dy, nx, ny = 0.9, 0.9, 1, 1

    objective = dict(obj_yaml)
    if "tube_lens_f_mm" not in objective:
        od = manifest.get("objective_details") if isinstance(manifest, dict) else {}
        if isinstance(od, dict) and "tube_lens_f_mm" in od:
            objective["tube_lens_f_mm"] = od["tube_lens_f_mm"]
        else:
            objective.setdefault("tube_lens_f_mm", 180.0)

    sensor_pixel_size_um = obj_yaml.get("sensor_pixel_size_um")
    if sensor_pixel_size_um is None and isinstance(manifest, dict):
        sensor_pixel_size_um = manifest.get("sensor_pixel_size_um")

    tube_lens_mm = TUBE_LENS_MM
    if isinstance(manifest, dict) and manifest.get("tube_lens_mm") is not None:
        tube_lens_mm = manifest["tube_lens_mm"]

    flat: Dict[str, Any] = {
        "dx(mm)": dx,
        "dy(mm)": dy,
        "Nx": nx,
        "Ny": ny,
        "dz(um)": dz_um,
        "Nz": z_stack.get("nz", 1),
        "dt(s)": time_series.get("delta_t_s", 0.0),
        "Nt": time_series.get("nt", 1),
        "with AF": bool(af.get("contrast_af")),
        "with laser AF": bool(af.get("laser_af")),
        "with manual focus map": bool(acq.get("use_manual_focus_map", False)),
        "objective": objective,
        "sensor_pixel_size_um": sensor_pixel_size_um,
        "tube_lens_mm": tube_lens_mm,
        "confocal_mode": bool(manifest.get("confocal_mode", False)) if isinstance(manifest, dict) else False,
    }
    ch = data.get("channels", [])
    if isinstance(ch, dict) and ch.get("observation_state_names"):
        flat["observation_state_names"] = list(ch["observation_state_names"])
    return flat


def load_legacy_acquisition_parameters_flat(experiment_dir: str) -> Dict[str, Any]:
    """Load legacy flat acquisition dict: prefer JSON if present, else derive from ``acquisition.yaml``."""
    json_path = os.path.join(experiment_dir, "acquisition parameters.json")
    if os.path.isfile(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    yaml_path = os.path.join(experiment_dir, "acquisition.yaml")
    if os.path.isfile(yaml_path):
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError(f"Invalid acquisition YAML: {yaml_path}")
        return legacy_flat_multipoint_from_acquisition_yaml_dict(data)
    raise FileNotFoundError(
        f"No acquisition parameters found in {experiment_dir!r} (expected acquisition.yaml or legacy JSON)."
    )
