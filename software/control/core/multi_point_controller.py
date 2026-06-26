"""
Multi-point acquisition controller.

This module handles automated multi-position image acquisition, which is the core
functionality for high-content screening (HCS) applications. It coordinates:

- Stage movement to multiple positions (X, Y grid, Z-stack, time-lapse)
- Image acquisition at each position
- Autofocus at each position (optional)
- Channel switching (multiple wavelengths/filters)
- Z-stack acquisition (3D imaging)
- Time-lapse acquisition (4D imaging)
- Focus map generation and application

The controller manages the complete acquisition workflow:
1. Generate scan coordinates (grid of positions)
2. For each position:
   a. Move stage to position
   b. Perform autofocus (if enabled)
   c. For each channel:
      - Switch illumination/filters
      - Acquire image(s)
      - Save to disk
   d. Move to next Z position (if Z-stack)
3. Move to next time point (if time-lapse)

All acquisition is performed in a background thread to keep the GUI responsive.
"""

import dataclasses
import math
import os
import time
import yaml
from datetime import datetime
from enum import Enum
from threading import Thread
from typing import Any, Dict, Optional, Tuple

from control.models.observation_state import ObservationState

import numpy as np
import pandas as pd

from control import utils
import control._def
from control.core.auto_focus_controller import AutoFocusController
from control.core.multi_point_utils import MultiPointControllerFunctions, ScanPositionInformation, AcquisitionParameters
from control.core.scan_coordinates import ScanCoordinates
from control.core.laser_auto_focus_controller import LaserAutofocusController
from control.core.live_controller import LiveController
from control.microscope import Microscope
from control.core.multi_point_worker import MultiPointWorker
from control.core.objective_store import ObjectiveStore
from control.core.memory_profiler import MemoryMonitor, log_memory
from control.microcontroller import Microcontroller
from control.piezo import PiezoStage
from control.core.config.repository import ConfigRepository
from squid.abc import CameraFrame, AbstractCamera, AbstractStage
import squid.logging


# Approximate on-disk compression ratios for the zarr blosc presets (see ZarrCompression in
# _def for the documented encode ratios). Used only for the disk-space estimate.
_ZARR_COMPRESSION_RATIOS = {
    control._def.ZarrCompression.NONE: 1.0,
    control._def.ZarrCompression.FAST: 2.0,
    control._def.ZarrCompression.BALANCED: 3.5,
    control._def.ZarrCompression.BEST: 4.0,
}
# Pyramid levels add geometric overhead on top of level 0: 1 + 1/4 + 1/16 + ... -> ~4/3.
_ZARR_PYRAMID_OVERHEAD = 4.0 / 3.0


# No-op callbacks for cases where callbacks are not needed
NoOpCallbacks = MultiPointControllerFunctions(
    signal_acquisition_start=lambda *a, **kw: None,
    signal_acquisition_finished=lambda *a, **kw: None,
    signal_new_image=lambda *a, **kw: None,
    signal_current_configuration=lambda *a, **kw: None,
    signal_current_fov=lambda *a, **kw: None,
    signal_overall_progress=lambda *a, **kw: None,
    signal_region_progress=lambda *a, **kw: None,
)


def _serialize_for_yaml(obj):
    """Recursively serialize objects to YAML-compatible types."""
    if obj is None:
        return None
    elif isinstance(obj, Enum):
        return obj.value
    # Handle numpy types - convert to native Python types
    elif isinstance(obj, np.ndarray):
        return [_serialize_for_yaml(item) for item in obj.tolist()]
    elif isinstance(obj, (np.integer, np.floating)):
        return obj.item()  # Convert numpy scalar to Python scalar
    elif isinstance(obj, np.bool_):
        return bool(obj)
    elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _serialize_for_yaml(v) for k, v in dataclasses.asdict(obj).items()}
    elif hasattr(obj, "model_dump"):
        return _serialize_for_yaml(obj.model_dump())
    elif isinstance(obj, dict):
        return {k: _serialize_for_yaml(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_serialize_for_yaml(item) for item in obj]
    else:
        return obj


def _save_region_observation_state_csv(experiment_path, region_observation_state_map, observation_state_names, logger=None):
    """Write a CSV recording the per-region observation state matrix.

    Rows = regions, columns = observation state preset names.
    Cell value is 1 (acquire) or 0 (skip).
    Only written when region_observation_state_map is not None (i.e. the user customised it).
    """
    if region_observation_state_map is None:
        return
    rows = []
    for region_id, active_names in region_observation_state_map.items():
        active_set = set(active_names)
        row = {"region": region_id}
        for obs_name in observation_state_names:
            row[obs_name] = 1 if obs_name in active_set else 0
        rows.append(row)
    df = pd.DataFrame(rows)
    csv_path = os.path.join(experiment_path, "region_observation_states.csv")
    try:
        df.to_csv(csv_path, index=False)
    except Exception as e:
        if logger:
            logger.error("Failed to write region_observation_states.csv: %s", e, exc_info=True)


def _save_region_laser_af_references(experiment_path, region_laser_af_references, logger=None):
    """Record the per-region laser-AF focus targets used for the run.

    Writes ``region_laser_af_references.csv`` (region id, spot x_reference, and
    whether a verification crop was stored). Only written when at least one
    region carried a per-region reference. The crop images themselves are part
    of the exported coordinate sidecar, not this reproducibility summary.
    """
    if not region_laser_af_references:
        return
    rows = []
    for region_id, reference in region_laser_af_references.items():
        rows.append(
            {
                "region": region_id,
                "x_reference": getattr(reference, "x_reference", ""),
                "has_reference_image": int(getattr(reference, "reference_image", None) is not None),
            }
        )
    csv_path = os.path.join(experiment_path, "region_laser_af_references.csv")
    try:
        pd.DataFrame(rows).to_csv(csv_path, index=False)
    except Exception as e:
        if logger:
            logger.error("Failed to write region_laser_af_references.csv: %s", e, exc_info=True)


def _save_cycle_manifest(experiment_path, params, repo, logger=None):
    """Write the resolved cycle/acquisition-order manifest (the ground truth).

    Records, per region: the dense/ragged layout, per-state frame counts, the
    imaged channel order, and the flat ordered list of events (so the exact
    interleave of imaged frames and stimulus pulses is reconstructable
    regardless of the on-disk array layout). Also embeds the selected cycle
    definitions. Skipped when no cycles were selected (flat acquisition).
    """
    if not getattr(params, "selected_cycle_names", None):
        return

    def _plan_dict(plan):
        if plan is None:
            return None
        return {
            "dense": plan.dense,
            # frame_counts and array_keys are keyed by (state, z-mode): a
            # reference-z-only capture appears under "{state}_refz" (its own
            # single-z array), so the full per-array structure is reconstructable.
            "frame_counts": dict(plan.frame_counts),
            "channel_order": list(plan.channel_order),
            "array_keys": list(plan.array_keys),
            "events": [
                {
                    "observation_state": ev.observation_state,
                    "is_stimulus": ev.is_stimulus,
                    "is_wait": ev.is_wait,
                    "wait_ms": ev.wait_ms,
                    "state_frame_index": ev.state_frame_index,
                    "cycle_event_index": ev.cycle_event_index,
                    # False => captured only at the reference/focus plane (single z).
                    "acquire_z_stack": ev.acquire_z_stack,
                    # Source-coded FPM: the exact LED indices lit for this frame,
                    # so the reconstruction can recover each multiplexed pattern.
                    "multiplexed_leds": list(ev.multiplexed_leds) if ev.multiplexed_leds else None,
                }
                for ev in plan.events
            ],
        }

    cycle_defs = {}
    names = list(params.selected_cycle_names)
    for region_names in (params.region_cycle_map or {}).values():
        names.extend(region_names)
    for name in dict.fromkeys(names):
        try:
            cyc = repo.load_acquisition_cycle(name)
            if cyc is not None:
                cycle_defs[name] = cyc.model_dump(mode="json")
        except Exception:
            pass

    manifest = {
        "selected_cycle_names": list(params.selected_cycle_names),
        "region_cycle_map": params.region_cycle_map,
        "cycle_definitions": cycle_defs,
        "global_plan": _plan_dict(params.global_region_plan),
        "region_plans": {rid: _plan_dict(p) for rid, p in (params.resolved_region_plans or {}).items()},
    }
    path = os.path.join(experiment_path, "cycles_manifest.yaml")
    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(manifest, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    except Exception as e:
        if logger:
            logger.error("Failed to write cycles_manifest.yaml: %s", e, exc_info=True)


def _save_fpm_pattern_positions(experiment_path, params, repo, objective_na, logger=None):
    """Write per-pattern LED indices + NA positions for source-coded FPM runs.

    Reconstruction needs each multiplexed darkfield frame's LED NA coordinates;
    the cycles manifest records only the indices, so this resolves them against the
    cached dome geometry and writes a self-contained ``fpm_patterns.yaml`` to the
    acquisition root. No-op when the run has no FPM darkfield frames.
    """
    # Distinct (base state, LED set) frames in first-seen order, across the global
    # plan and any per-region plans. The base state distinguishes brightfield from
    # darkfield in the full routine; for the source-coded routine all share one.
    plans = []
    if getattr(params, "global_region_plan", None) is not None:
        plans.append(params.global_region_plan)
    plans.extend((getattr(params, "resolved_region_plans", None) or {}).values())
    seen = set()
    records = []  # (observation_state, leds_tuple)
    for plan in plans:
        if plan is None:
            continue
        for ev in plan.events:
            if not ev.multiplexed_leds:
                continue
            key = (ev.observation_state, tuple(ev.multiplexed_leds))
            if key in seen:
                continue
            seen.add(key)
            records.append(key)
    if not records:
        return

    try:
        from control import fpm_led_geometry as fpm

        table = fpm.load_na_table()
    except Exception as e:
        if logger:
            logger.warning("FPM: could not load LED NA table to write fpm_patterns.yaml: %s", e)
        return

    distance_mm = None
    try:
        entry = repo.get_machine_config().get_device("led_matrix")
        if entry is not None:
            distance_mm = entry.config.get("distance")
    except Exception:
        pass

    def _centroid(leds):
        pts = [table[j] for j in leds if j in table]
        if not pts:
            return [0.0, 0.0]
        return [round(sum(p[0] for p in pts) / len(pts), 6), round(sum(p[1] for p in pts) / len(pts), 6)]

    out = {
        "objective_na": float(objective_na) if objective_na else None,
        "array_distance_mm": distance_mm,
        "na_table": str(fpm.DEFAULT_NA_TABLE_PATH),
        "n_patterns": len(records),
        "patterns": [
            {
                "index": i,
                "observation_state": name,
                "led_indices": [int(j) for j in leds],
                "led_na": [[round(table[j][0], 6), round(table[j][1], 6)] for j in leds if j in table],
                "centroid_na": _centroid(leds),
            }
            for i, (name, leds) in enumerate(records)
        ],
    }
    path = os.path.join(experiment_path, "fpm_patterns.yaml")
    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(out, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        if logger:
            logger.info("FPM: wrote %d pattern positions to %s", len(records), path)
    except Exception as e:
        if logger:
            logger.error("Failed to write fpm_patterns.yaml: %s", e, exc_info=True)


def _save_unified_multipoint_acquisition_yaml(
    params: "AcquisitionParameters",
    experiment_path: str,
    region_shapes: dict = None,
    widget_type: str = "wellplate",
    objective_info: dict = None,
    wellplate_format: str = None,
    scan_size_mm: float = 0.0,
    overlap_percent: float = 10.0,
    *,
    repo: ConfigRepository,
    live_controller: "LiveController",
    camera: Any,
    objective_store: "ObjectiveStore",
    recording_start_time: float,
    selected_observation_state_names: list,
    use_manual_focus_map: bool,
    logger: Any,
) -> None:
    """Write a single ``acquisition.yaml`` (schema v2): layout + manifest + used observation states."""
    from control.core.acquisition_metadata_helpers import augment_multipoint_acquisition_yaml_dict

    # Build common sections
    yaml_dict = {
        "acquisition": {
            "experiment_id": params.experiment_ID,
            "start_time": params.acquisition_start_time,
            "widget_type": widget_type,
            "xy_mode": params.xy_mode,
            "skip_saving": params.skip_saving,
            "use_manual_focus_map": use_manual_focus_map,
            "keep_illuminators_on_between_captures": params.keep_illuminators_on_between_captures,
        },
        "objective": objective_info or {},
        "sample": {
            "wellplate_format": wellplate_format,
        },
        "z_stack": {
            "nz": params.NZ,
            "delta_z_mm": params.deltaZ,
            "config": params.z_stacking_config,
            "z_range_mm": _serialize_for_yaml(params.z_range) if params.z_range else None,
            "use_piezo": params.use_piezo,
        },
        "time_series": {
            "nt": params.Nt,
            "delta_t_s": params.deltat,
        },
        "autofocus": {
            "contrast_af": params.do_autofocus,
            "laser_af": params.do_reflection_autofocus,
        },
        "channels": {
            "observation_state_names": list(params.selected_observation_state_names),
        },
    }

    # Add widget-specific scan section
    if widget_type == "wellplate":
        yaml_dict["wellplate_scan"] = {
            "scan_size_mm": scan_size_mm,
            "overlap_percent": overlap_percent,
            "nx": params.NX,
            "ny": params.NY,
            "delta_x_mm": params.deltaX,
            "delta_y_mm": params.deltaY,
            "regions": [
                {
                    "name": name,
                    "center_mm": _serialize_for_yaml(center),
                    "shape": region_shapes.get(name) if region_shapes else None,
                }
                for name, center in zip(
                    params.scan_position_information.scan_region_names,
                    params.scan_position_information.scan_region_coords_mm,
                )
            ],
        }
    else:  # flexible
        yaml_dict["flexible_scan"] = {
            "nx": params.NX,
            "ny": params.NY,
            "delta_x_mm": params.deltaX,
            "delta_y_mm": params.deltaY,
            "overlap_percent": overlap_percent,
            "positions": [
                {
                    "name": name,
                    "center_mm": _serialize_for_yaml(center),
                }
                for name, center in zip(
                    params.scan_position_information.scan_region_names,
                    params.scan_position_information.scan_region_coords_mm,
                )
            ],
        }

    # Add remaining common sections
    yaml_dict["downsampled_views"] = {
        "enabled": params.generate_downsampled_views,
        "save_well_images": params.save_downsampled_well_images,
        "well_resolutions_um": _serialize_for_yaml(params.downsampled_well_resolutions_um),
        "plate_resolution_um": params.downsampled_plate_resolution_um,
        "z_projection": _serialize_for_yaml(params.downsampled_z_projection),
        "interpolation_method": _serialize_for_yaml(params.downsampled_interpolation_method),
    }
    yaml_dict["plate"] = {
        "num_rows": params.plate_num_rows,
        "num_cols": params.plate_num_cols,
    }
    yaml_dict["fluidics"] = {
        "enabled": params.use_fluidics,
    }

    unified = augment_multipoint_acquisition_yaml_dict(
        yaml_dict,
        experiment_id=params.experiment_ID or "",
        recording_start_time=recording_start_time,
        repo=repo,
        objective_store=objective_store,
        live_controller=live_controller,
        camera=camera,
        selected_configurations=[],
        obs_names=list(selected_observation_state_names or []),
        inline_observation_states=dict(params.inline_observation_states or {}),
        logger=logger,
    )

    yaml_path = os.path.join(experiment_path, "acquisition.yaml")
    try:
        with open(yaml_path, "w", encoding="utf-8") as f:
            f.write(
                f"# Unified multipoint acquisition record (schema_version=2, experiment_id={params.experiment_ID}).\n"
                f"# Layout, instrument manifest, and observation_states_used (selected presets only).\n"
                f"# Per-frame wall-clock acquisition times: <timepoint>/frame_acquisition_times.csv (UTC + unix).\n\n"
            )
            yaml.dump(unified, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    except (OSError, yaml.YAMLError) as exc:
        _log = squid.logging.get_logger(__name__)
        _log.error("Failed to write acquisition YAML file '%s': %s", yaml_path, exc)


class MultiPointController:
    """
    Controller for automated multi-point image acquisition.
    
    This class orchestrates the complete acquisition workflow:
    - Generates scan coordinates (grid of field-of-view positions)
    - Moves stage to each position
    - Performs autofocus (optional)
    - Acquires images for each channel
    - Saves images to disk with proper metadata
    
    Supports:
    - 2D grids (X, Y)
    - Z-stacks (3D imaging)
    - Time-lapse (4D imaging)
    - Multiple channels (wavelengths/filters)
    - Focus maps (pre-computed focus positions)
    """
    def __init__(
        self,
        microscope: Microscope,
        live_controller: LiveController,
        autofocus_controller: AutoFocusController,
        objective_store: ObjectiveStore,
        callbacks: MultiPointControllerFunctions,
        scan_coordinates: Optional[ScanCoordinates] = None,
        laser_autofocus_controller: Optional[LaserAutofocusController] = None,
        alignment_widget=None,
    ):
        super().__init__()
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self._alignment_widget = alignment_widget  # Optional AlignmentWidget for coordinate offset
        self.microscope: Microscope = microscope
        self.camera: AbstractCamera = microscope.camera
        self.stage: AbstractStage = microscope.stage
        self.piezo: Optional[PiezoStage] = microscope.addons.piezo_stage
        self.microcontroller: Microcontroller = microscope.low_level_drivers.microcontroller
        self.liveController: LiveController = live_controller
        self.autofocusController: AutoFocusController = autofocus_controller
        self.laserAutoFocusController: LaserAutofocusController = laser_autofocus_controller
        self.objectiveStore: ObjectiveStore = objective_store
        self.callbacks: MultiPointControllerFunctions = callbacks
        self.multiPointWorker: Optional[MultiPointWorker] = None
        self.fluidics: Optional[Any] = microscope.addons.fluidics
        self.thread: Optional[Thread] = None
        self._per_acq_log_handler = None
        self._memory_monitor: Optional[MemoryMonitor] = None
        self._slack_notifier = None  # Optional SlackNotifier for notifications

        # Pre-warm job runner subprocess at controller init (reduces acquisition start delay)
        # Backpressure values (tuple) are created here and shared with both the pre-warmed runner
        # and the BackpressureController in the worker, ensuring consistent tracking.
        self._prewarmed_job_runner: Optional["JobRunner"] = None
        self._prewarmed_bp_values: Optional["BackpressureValues"] = None
        if control._def.Acquisition.USE_MULTIPROCESSING:
            self._start_prewarmed_job_runner()

        # Acquisition grid parameters
        self.NX = 1  # Number of positions in X direction
        self.deltaX = control._def.Acquisition.DX  # Spacing between positions in X (mm)
        self.NY = 1  # Number of positions in Y direction
        self.deltaY = control._def.Acquisition.DY  # Spacing between positions in Y (mm)
        self.NZ = 1  # Number of Z positions (for Z-stacks)
        # TODO(imo): Switch all to consistent mm units
        self.deltaZ = control._def.Acquisition.DZ / 1000  # Z step size (mm, converted from um)
        self.Nt = 1  # Number of time points (for time-lapse)
        self.deltat = 0  # Time interval between time points (seconds)

        self.do_autofocus = False
        self.do_reflection_af = False
        self.laser_af_seed_mode = control._def.LASER_AF_SEED_MODE
        self.laser_af_refresh_every_n_fovs = control._def.LASER_AF_REFRESH_EVERY_N_FOVS
        self.laser_af_consistency_threshold_um = control._def.LASER_AF_CONSISTENCY_THRESHOLD_UM
        self.laser_af_check_last_fov_per_region = control._def.LASER_AF_CHECK_LAST_FOV_PER_REGION
        # Override laser-AF fast-mode defaults with whatever the user last
        # configured via the settings dialog. Load is widget-agnostic so the
        # values carry regardless of which multipoint tab is in use.
        self._load_laser_af_settings_from_cache()
        self.display_resolution_scaling = control._def.Acquisition.IMAGE_DISPLAY_SCALING_FACTOR
        self.use_piezo = control._def.MULTIPOINT_USE_PIEZO_FOR_ZSTACKS
        self.experiment_ID = None
        self.use_manual_focus_map = False
        self.base_path = None
        self.use_fluidics = False
        self.skip_saving = False
        self.file_saving_option = control._def.FILE_SAVING_OPTION
        self.keep_illuminators_on_between_captures = False
        # Live ZARR_V3 upload settings (off by default; configured per-acquisition).
        self.zarr_upload_enabled = False
        self.zarr_upload_remote_root = ""
        self.zarr_upload_delete_after_verify = True
        self.xy_mode = "Current Position"
        self.widget_type = "wellplate"  # "wellplate" or "flexible"
        self.scan_size_mm = 0.0  # For wellplate mode: size of scan area per region
        self.overlap_percent = 10.0  # FOV overlap percentage

        self.focus_map = None
        self.gen_focus_map = False
        self.focus_map_storage = []
        self.already_using_fmap = False
        self.selected_configurations = []
        self.selected_observation_state_names = []
        self.region_observation_state_map = None
        # Cycle-driven selection. When `selected_cycle_names` is non-empty the
        # per-position plan comes from resolving those cycles; otherwise the
        # legacy flat path over `selected_observation_state_names` (one frame per
        # state) is used unchanged — so the old path stays an instant rollback.
        self.selected_cycle_names = []
        self.region_cycle_map = None
        self._frames_per_position = 1
        self.scanCoordinates = scan_coordinates
        self._log.debug(f"Initializing coordinates with scan coordinates: {self.scanCoordinates}, format: {self.scanCoordinates.format}")
        
        # Display settings
        self.old_images_per_page = 1
        self.z_stacking_config = control._def.Z_STACKING_CONFIG

        self._start_position: Optional[squid.abc.Pos] = None

    def _start_prewarmed_job_runner(self):
        """Start a job runner subprocess that warms up in the background.

        This reduces acquisition start delay by having the subprocess already
        running when the user clicks 'Start Acquisition'.

        Also creates backpressure values that will be used by both the
        pre-warmed runner and the BackpressureController in the worker.

        Known limitation: Pre-warming for the NEXT acquisition is started when
        the CURRENT acquisition begins (i.e., when ``get_prewarmed_job_runner()``
        is called). If the user starts another acquisition before pre-warming
        finishes (~1.2s), the worker will wait for the subprocess. This only
        affects rapid-fire manual clicking; real workloads (full plate scans,
        time-lapse with intervals >2s) are unaffected.
        """
        from control.core.job_processing import JobRunner
        from control.core.backpressure import create_backpressure_values

        self._log.info("Pre-warming job runner subprocess...")
        # Create shared backpressure values for cross-process tracking
        self._prewarmed_bp_values = create_backpressure_values()

        self._prewarmed_job_runner = JobRunner(
            bp_pending_jobs=self._prewarmed_bp_values[0],
            bp_pending_bytes=self._prewarmed_bp_values[1],
            bp_capacity_event=self._prewarmed_bp_values[2],
        )
        self._prewarmed_job_runner.start()

    def _cleanup_prewarmed_runner(
        self,
        runner: Optional["JobRunner"],
        timeout_s: float = 1.0,
        context: str = "",
    ) -> None:
        """Shutdown a pre-warmed job runner.

        Args:
            runner: JobRunner to shutdown, or None
            timeout_s: Timeout for runner shutdown
            context: Context string for error messages (e.g., "during close")
        """
        if runner is not None:
            try:
                runner.shutdown(timeout_s=timeout_s)
            except Exception as e:
                self._log.error(f"Error shutting down pre-warmed runner {context}: {e}")

    def get_prewarmed_job_runner(self) -> Tuple[Optional["JobRunner"], Optional["BackpressureValues"]]:
        """Get the pre-warmed job runner and its shared backpressure values.

        Returns:
            Tuple of (runner, bp_values) where:
            - runner: JobRunner instance or None if not available
            - bp_values: BackpressureValues tuple or None

        The runner and values are cleared (so they're only used once).

        Usage:
            runner, bp_values = controller.get_prewarmed_job_runner()
            worker = MultiPointWorker(..., prewarmed_job_runner=runner,
                                      prewarmed_bp_values=bp_values)
        """
        runner = self._prewarmed_job_runner
        bp_values = self._prewarmed_bp_values

        # Clear references (so they're only used once)
        self._prewarmed_job_runner = None
        self._prewarmed_bp_values = None

        # Start warming up a new one for the next acquisition
        if control._def.Acquisition.USE_MULTIPROCESSING:
            self._start_prewarmed_job_runner()

        return runner, bp_values

    def set_alignment_widget(self, alignment_widget):
        """Set the alignment widget for coordinate offset during acquisitions."""
        self._alignment_widget = alignment_widget

    def set_slack_notifier(self, slack_notifier):
        """Set the Slack notifier for acquisition notifications."""
        self._slack_notifier = slack_notifier

    def _start_per_acquisition_log(self) -> None:
        if not control._def.ENABLE_PER_ACQUISITION_LOG:
            return
        if self._per_acq_log_handler is not None:
            return
        if not self.base_path or not self.experiment_ID:
            return

        acq_dir = os.path.join(self.base_path, self.experiment_ID)
        log_path = os.path.join(acq_dir, "acquisition.log")
        try:
            self._per_acq_log_handler = squid.logging.add_file_handler(
                log_path, replace_existing=True, level=squid.logging.py_logging.DEBUG
            )
        except Exception:
            self._log.exception("Failed to start per-acquisition logging")
            self._per_acq_log_handler = None

    def _stop_per_acquisition_log(self) -> None:
        if self._per_acq_log_handler is None:
            return
        try:
            squid.logging.remove_handler(self._per_acq_log_handler)
        except Exception:
            self._log.exception("Failed to stop per-acquisition logging")
        finally:
            self._per_acq_log_handler = None

    def acquisition_in_progress(self):
        if self.thread and self.thread.is_alive() and self.multiPointWorker:
            return True
        return False

    def set_use_piezo(self, checked):
        if checked and self.piezo is None:
            raise ValueError("Cannot enable piezo - no piezo stage configured")
        self.use_piezo = checked
        # TODO(imo): Why do we only allow runtime updates of use_piezo (not all the other params?)
        if self.multiPointWorker:
            self.multiPointWorker.update_use_piezo(checked)

    def set_z_stacking_config(self, z_stacking_config_index):
        if z_stacking_config_index in control._def.Z_STACKING_CONFIG_MAP:
            self.z_stacking_config = control._def.Z_STACKING_CONFIG_MAP[z_stacking_config_index]
        self._log.info(f"z-stacking configuration set to {self.z_stacking_config}")

    def set_z_range(self, minZ, maxZ):
        self.z_range = [minZ, maxZ]

    def set_NX(self, N):
        self.NX = N

    def set_NY(self, N):
        self.NY = N

    def set_NZ(self, N):
        self.NZ = N

    def set_Nt(self, N):
        self.Nt = N

    def set_deltaX(self, delta):
        self.deltaX = delta

    def set_deltaY(self, delta):
        self.deltaY = delta

    def set_deltaZ(self, delta_um):
        self.deltaZ = delta_um / 1000

    def set_deltat(self, delta):
        self.deltat = delta

    def set_af_flag(self, flag):
        self.do_autofocus = flag

    def set_reflection_af_flag(self, flag):
        self.do_reflection_af = flag

    def set_laser_af_seed_mode(self, mode: str):
        """Set laser-AF seed mode: "scan" runs a pre-acquisition pass that
        laser-AFs every FOV; "lazy" seeds on first visit during acquisition."""
        if mode not in ("scan", "lazy"):
            raise ValueError(f"laser_af_seed_mode must be 'scan' or 'lazy', got {mode!r}")
        self.laser_af_seed_mode = mode
        self._save_laser_af_settings_to_cache()

    def set_laser_af_refresh_every_n_fovs(self, n: int):
        """Max FOVs per region between anchor refreshes (1 = AF every FOV)."""
        self.laser_af_refresh_every_n_fovs = max(1, int(n))
        self._save_laser_af_settings_to_cache()

    def set_laser_af_consistency_threshold_um(self, threshold_um: float):
        """µm disagreement above which consistency checks emit a warning."""
        self.laser_af_consistency_threshold_um = float(threshold_um)
        self._save_laser_af_settings_to_cache()

    def set_laser_af_check_last_fov_per_region(self, flag: bool):
        """Enable/disable the end-of-region displacement check for short regions."""
        self.laser_af_check_last_fov_per_region = bool(flag)
        self._save_laser_af_settings_to_cache()

    # Dedicated cache for laser-AF fast-mode settings. Lives outside the
    # wellplate-widget-specific multipoint_widget_config.yaml so that changes
    # made via the dialog from *any* multipoint widget (Flexible, Wellplate,
    # Fluidics) persist across restarts.
    _LASER_AF_SETTINGS_CACHE_PATH = "cache/laser_af_settings.yaml"

    def _save_laser_af_settings_to_cache(self):
        try:
            os.makedirs(os.path.dirname(self._LASER_AF_SETTINGS_CACHE_PATH), exist_ok=True)
            data = {
                "laser_af_seed_mode": self.laser_af_seed_mode,
                "laser_af_refresh_every_n_fovs": self.laser_af_refresh_every_n_fovs,
                "laser_af_consistency_threshold_um": self.laser_af_consistency_threshold_um,
                "laser_af_check_last_fov_per_region": self.laser_af_check_last_fov_per_region,
            }
            with open(self._LASER_AF_SETTINGS_CACHE_PATH, "w") as f:
                yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
        except Exception as e:
            self._log.warning(f"Failed to persist laser-AF settings: {e}")

    def _load_laser_af_settings_from_cache(self):
        path = self._LASER_AF_SETTINGS_CACHE_PATH
        if not os.path.exists(path):
            return
        try:
            with open(path, "r") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            self._log.warning(f"Failed to read laser-AF settings cache ({path}): {e}")
            return
        seed_mode = data.get("laser_af_seed_mode")
        if seed_mode in ("scan", "lazy"):
            self.laser_af_seed_mode = seed_mode
        if "laser_af_refresh_every_n_fovs" in data:
            try:
                self.laser_af_refresh_every_n_fovs = max(1, int(data["laser_af_refresh_every_n_fovs"]))
            except (TypeError, ValueError):
                pass
        if "laser_af_consistency_threshold_um" in data:
            try:
                self.laser_af_consistency_threshold_um = float(data["laser_af_consistency_threshold_um"])
            except (TypeError, ValueError):
                pass
        if "laser_af_check_last_fov_per_region" in data:
            self.laser_af_check_last_fov_per_region = bool(data["laser_af_check_last_fov_per_region"])

    def set_manual_focus_map_flag(self, flag):
        self.use_manual_focus_map = flag

    def set_gen_focus_map_flag(self, flag):
        self.gen_focus_map = flag
        if not flag:
            self.autofocusController.set_focus_map_use(False)

    def set_focus_map(self, focusMap):
        self.focus_map = focusMap  # None if dont use focusMap

    def set_base_path(self, path):
        self.base_path = path

    def set_use_fluidics(self, use_fluidics):
        self.use_fluidics = use_fluidics

    def set_skip_saving(self, skip_saving):
        self.skip_saving = skip_saving

    def set_file_saving_option(self, option):
        """Set the on-disk save format for the next acquisition.

        ``option`` may be a ``FileSavingOption`` enum or its string name.
        Snapshotted into ``AcquisitionParameters`` in ``build_params()``;
        no global state changes.
        """
        self.file_saving_option = control._def.FileSavingOption.convert_to_enum(option)

    def set_keep_illuminators_on_between_captures(self, keep_on: bool):
        self.keep_illuminators_on_between_captures = bool(keep_on)

    def set_zarr_upload_target(
        self,
        enabled: bool,
        remote_root: str = "",
        delete_after_verify: bool = True,
    ) -> None:
        """Configure live ZARR_V3 upload to a network share for the next acquisition.

        ``remote_root`` must be a writable directory on a mounted SMB share
        (e.g. ``\\\\server\\share\\bills_acquisitions``). Snapshotted into
        ``AcquisitionParameters`` in ``build_params()``.
        """
        self.zarr_upload_enabled = bool(enabled)
        self.zarr_upload_remote_root = remote_root or ""
        self.zarr_upload_delete_after_verify = bool(delete_after_verify)

    def set_xy_mode(self, xy_mode):
        self.xy_mode = xy_mode

    def set_widget_type(self, widget_type: str):
        self.widget_type = widget_type

    def set_scan_size(self, scan_size_mm: float):
        self.scan_size_mm = scan_size_mm

    def set_overlap_percent(self, overlap_percent: float):
        self.overlap_percent = overlap_percent

    def start_new_experiment(self, experiment_ID):  # @@@ to do: change name to prepare_folder_for_new_experiment
        # generate unique experiment ID
        self.experiment_ID = experiment_ID.replace(" ", "_") + "_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S.%f")
        self.recording_start_time = time.time()
        # create a new folder (unified acquisition.yaml is written when the run starts in run_acquisition)
        experiment_dir = os.path.join(self.base_path, self.experiment_ID)
        utils.ensure_directory_exists(experiment_dir)

    def set_selected_configurations(self, selected_configurations_name):
        """Legacy flat selection: acquire these observation states, one frame each.

        Clears any cycle selection so the worker takes the flat path.
        """
        repo = self.liveController.microscope.config_repo
        preset_set = set(repo.list_observation_presets())
        self.selected_configurations = []
        self.selected_observation_state_names = []
        self.selected_cycle_names = []
        for name in selected_configurations_name:
            if name in preset_set:
                self.selected_observation_state_names.append(name)
            else:
                self._log.warning("Channel '%s' not found in observation presets, skipping", name)
        self._frames_per_position = len(self.selected_observation_state_names)

    def set_selected_cycles(self, selected_cycle_names):
        """Cycle-driven selection: run these cycles (in order) at each position.

        Derives the imaged channel axis (distinct imaged states) and
        frames-per-position so existing disk/image-count/zarr-naming helpers,
        which read ``selected_observation_state_names``, keep working.
        """
        repo = self.liveController.microscope.config_repo
        cycle_set = set(repo.list_acquisition_cycles())
        self.selected_configurations = []
        self.selected_cycle_names = []
        for name in selected_cycle_names:
            if name in cycle_set:
                self.selected_cycle_names.append(name)
            else:
                self._log.warning("Cycle '%s' not found, skipping", name)
        from control.models.acquisition_cycle import all_states_in_order

        plan = self._resolve_plan(self.selected_cycle_names, None)
        # Metadata/disk/image-count helpers read this; keep stimulus states in the
        # record. The zarr C axis (imaged-only) is taken from the plan separately.
        self.selected_observation_state_names = all_states_in_order(plan.events)
        self._frames_per_position = plan.frames_per_position

    def set_region_observation_state_map(self, mapping):
        """Set per-region observation state overrides. None means all regions use the global list."""
        self.region_observation_state_map = mapping

    def set_region_cycle_map(self, mapping):
        """Set per-region cycle overrides (region_id -> list of cycle names).

        None means all regions run the global selected cycles.
        """
        self.region_cycle_map = mapping

    def _resolve_run_observation_states(self):
        """[(name, ObservationState)] for every distinct observation state in this run.

        Cycles already expand into ``selected_observation_state_names`` (see
        set_selected_cycles), so iterating that list covers both the flat and cycle
        paths. Inline live-snapshot states take precedence over saved presets.
        """
        repo = self.liveController.microscope.config_repo
        inline = getattr(self, "_inline_observation_states_for_run", None) or {}
        out = []
        seen = set()
        for name in self.selected_observation_state_names:
            if name in seen:
                continue
            seen.add(name)
            st = inline.get(name)
            if st is None:
                st = repo.load_observation_preset(name)
            if st is not None:
                out.append((name, st))
        return out

    def build_roi_consistency_report(self):
        """Report each selected observation state's FOV and whether their ROIs match.

        Used by the GUI to warn (and require approval) when states in one acquisition
        have mismatched ROIs, and to derive the tiling FOV (largest). See
        observation_state_roi_report.
        """
        from control.core.observation_state_service import observation_state_roi_report

        states = self._resolve_run_observation_states()
        factor = self.objectiveStore.get_pixel_size_factor()
        return observation_state_roi_report(states, self.camera, factor)

    def apply_observation_state_tiling(self, scan_coordinates=None):
        """Regenerate the region tile grids for the largest observation-state ROI.

        Guarantees the saved overlap matches the user's intent regardless of the
        camera state when the regions were drawn. Idempotent: re-running it for the
        same observation states reproduces the same coordinates, so it is safe to call
        once from the GUI (so disk/RAM estimates see the final tile count) and again
        from run_acquisition (so headless/SiLA paths are covered). Defaults to the
        controller's own scan coordinates.
        """
        if scan_coordinates is None:
            scan_coordinates = self.scanCoordinates
        try:
            report = self.build_roi_consistency_report()
            tiling_fov = report.get("tiling_fov_mm")
            if tiling_fov is None:
                # No explicit observation states (live-snapshot fallback): tile for the
                # current live camera FOV so a post-definition ROI change is still honored.
                w_mm, h_mm = self.camera.get_fov_size_mm()
                factor = self.objectiveStore.get_pixel_size_factor()
                tiling_fov = (factor * w_mm, factor * h_mm)
            if scan_coordinates.regenerate_for_fov(tiling_fov[0], tiling_fov[1]):
                self._log.info(
                    f"Tiled regions for largest observation-state FOV "
                    f"{tiling_fov[0]:.4f} x {tiling_fov[1]:.4f} mm (state '{report.get('largest_name')}')."
                )
                if report.get("mismatch"):
                    self._log.warning(
                        "Observation states have mismatched ROIs; smaller-ROI states "
                        f"will under-sample: {report.get('mismatch_names')}"
                    )
        except Exception:
            self._log.exception("Could not apply observation-state tiling FOV; using regions as defined.")

    def _is_stimulus_predicate(self):
        """Return a cached predicate: is this observation state stimulus-only?

        Consults inline (live-snapshot) states first, then saved presets.
        """
        repo = self.liveController.microscope.config_repo
        inline = getattr(self, "_inline_observation_states_for_run", None) or {}
        cache = {}

        def is_stim(name):
            if name in cache:
                return cache[name]
            st = inline.get(name)
            if st is None:
                st = repo.load_observation_preset(name)
            result = bool(getattr(st, "is_stimulus_only", False)) if st is not None else False
            cache[name] = result
            return result

        return is_stim

    def _fpm_pattern_provider(self):
        """Build the FPM frame generator for the cycle resolver.

        Closes over the current objective NA + the cached LED NA table so the pure
        resolver can expand an FPM item into concrete frames. Returns a callable
        ``item -> List[(observation_state_name, led_indices)]`` (one camera frame
        each). Handles ``CycleFPMDarkfield`` (random multiplexed darkfield),
        ``CycleFPMBrightfield`` (single-LED BF sweep) and
        ``CycleFPMClusteredDarkfield`` (angle-clustered DF sweep). On any failure
        (no NA table, unknown objective NA) it logs and returns ``[]`` so the
        acquisition degrades rather than crashing.
        """
        from control import fpm_led_geometry as fpm
        from control.models.acquisition_cycle import (
            CycleFPMBrightfield,
            CycleFPMClusteredDarkfield,
            CycleFPMDarkfield,
        )

        def _context():
            table = fpm.load_na_table()
            obj_na = float(self.objectiveStore.get_current_objective_info().get("NA", 0.0))
            return table, obj_na

        def provider(item):
            try:
                table, obj_na = _context()
            except Exception as e:
                self._log.warning("FPM: geometry/objective NA unavailable (%s); no FPM frames generated", e)
                return []
            if obj_na <= 0:
                self._log.warning("FPM: objective NA is %.3f (<=0); no FPM frames generated", obj_na)
                return []

            if isinstance(item, CycleFPMDarkfield):
                patterns, report = fpm.build_fpm_darkfield_patterns(
                    table,
                    objective_na=obj_na,
                    outer_na=item.outer_na,
                    inner_na=item.inner_na,
                    min_overlap=item.min_overlap,
                    leds_per_pattern=item.leds_per_pattern,
                    seed=item.seed,
                )
                inner = item.inner_na if item.inner_na is not None else obj_na
                if report.n_candidates == 0 or not patterns:
                    self._log.warning(
                        "FPM darkfield: NO LEDs in region[%.3f,%.3f] for obj_NA=%.3f; 0 frames.",
                        inner, item.outer_na, obj_na,
                    )
                    return []
                m_used = max((len(p) for p in patterns), default=0)
                self._log.info(
                    "FPM darkfield: obj_NA=%.3f region[%.3f,%.3f] %d/%d LEDs -> %d patterns of <=%d%s (overlap %.2f)",
                    obj_na, inner, item.outer_na, report.n_selected, report.n_candidates,
                    len(patterns), m_used, " [auto]" if item.leds_per_pattern <= 0 else "",
                    report.min_achieved_overlap,
                )
                return [(item.observation_state, tuple(g)) for g in patterns]

            if isinstance(item, CycleFPMBrightfield):
                bf_all = fpm.brightfield_leds(table, obj_na)
                bf = bf_all
                sampled = item.n_leds and item.n_leds > 0 and item.n_leds < len(bf_all)
                if sampled:
                    bf = fpm.pseudorandom_sample(bf_all, item.n_leds, seed=item.seed)
                self._log.info(
                    "FPM brightfield: obj_NA=%.3f -> %d single-LED frames%s (of %d LEDs with NA <= %.3f)",
                    obj_na, len(bf), f" [pseudorandom n={item.n_leds} seed={item.seed}]" if sampled else "",
                    len(bf_all), obj_na,
                )
                if not bf:
                    self._log.warning("FPM brightfield: no LEDs with NA <= %.3f; 0 frames.", obj_na)
                    return []
                return [(item.observation_state, (led,)) for led in bf]

            if isinstance(item, CycleFPMClusteredDarkfield):
                inner = item.inner_na if item.inner_na is not None else obj_na
                cells, report = fpm.cluster_darkfield_leds(
                    table,
                    inner_na=inner,
                    outer_na=item.outer_na,
                    pupil_radius_na=obj_na,
                    min_overlap=item.min_overlap,
                )
                self._log.info(
                    "FPM clustered darkfield: obj_NA=%.3f region[%.3f,%.3f] %d LEDs -> %d cells (overlap %.2f)",
                    obj_na, inner, item.outer_na, report.n_candidates, len(cells), report.min_achieved_overlap,
                )
                if not cells:
                    self._log.warning(
                        "FPM clustered darkfield: NO LEDs in region[%.3f,%.3f] for obj_NA=%.3f; 0 frames.",
                        inner, item.outer_na, obj_na,
                    )
                    return []
                return [(item.observation_state, tuple(c.indices)) for c in cells]

            return []

        return provider

    def _resolve_plan(self, cycle_names, region_state_names):
        """Resolve a RegionPlan from cycle names (preferred) or bare state names.

        ``cycle_names`` non-empty -> resolve those cycles into a chain.
        Otherwise ``region_state_names`` (falling back to the global
        ``selected_observation_state_names``) is treated as a chain of 1-frame
        events — today's flat behaviour.
        """
        from control.models.acquisition_cycle import RegionPlan, resolve_chain, _index_events

        repo = self.liveController.microscope.config_repo
        is_stim = self._is_stimulus_predicate()
        if cycle_names:
            events = resolve_chain(
                list(cycle_names), repo.load_acquisition_cycle, is_stim, self._fpm_pattern_provider()
            )
        else:
            names = region_state_names if region_state_names is not None else self.selected_observation_state_names
            # _index_events takes tagged raw events; a flat selection is one ("state", name)
            # event per checked state (1 frame each) — today's flat behaviour.
            events = _index_events([("state", n) for n in names], is_stim)
        return RegionPlan.from_events(events)

    def _build_region_plans(self, scan_region_names):
        """Build (global_plan, {region_id: RegionPlan}) for an acquisition.

        Per-region plans are produced only for regions that have an explicit
        override (cycle map or legacy state map); other regions fall back to the
        global plan at run time.
        """
        global_plan = self._resolve_plan(self.selected_cycle_names, None)
        region_plans = {}
        if self.region_cycle_map is not None:
            for region_id, names in self.region_cycle_map.items():
                region_plans[region_id] = self._resolve_plan(names, None)
        elif self.region_observation_state_map is not None:
            for region_id, names in self.region_observation_state_map.items():
                region_plans[region_id] = self._resolve_plan([], names)
        return global_plan, region_plans

    def get_acquisition_image_count(self):
        """
        Given the current settings on this controller, return how many images an acquisition will
        capture and save to disk.

        NOTE: This does not cover debug images (eg: auto focus) or user created images (eg: custom scripts).

        NOTE: This does attempt to include the "merged" image if that config is enabled.

        Raises a ValueError if the class is not configured for a valid acquisition.
        """
        try:
            # We have Nt timepoints.  For each timepoint, we capture images at all the regions.  Each
            # region has a list of coordinates that we capture at, and at each coordinate we need to
            # do a capture for each requested camera + lighting + other configuration selected.  So
            # total image count is:
            coords_per_region = [
                len(region_coords) for (region_id, region_coords) in self.scanCoordinates.region_fov_coordinates.items()
            ]
            all_regions_coord_count = sum(coords_per_region)

            # Resolve the per-position plan so cycles (multiple frames per state)
            # and stimulus-only states (no camera frame) are both accounted for:
            # frames_per_position already excludes stimulus events.
            if self.selected_observation_state_names or self.selected_cycle_names:
                n_ch = self._resolve_plan(self.selected_cycle_names, None).frames_per_position
            else:
                n_ch = len(self.selected_configurations)
            non_merged_images = self.Nt * self.NZ * all_regions_coord_count * n_ch
            # When capturing merged images, we capture 1 per fov (where all the configurations are merged)
            merged_images = self.Nt * self.NZ * all_regions_coord_count if control._def.MERGE_CHANNELS else 0

            return non_merged_images + merged_images
        except AttributeError:
            # We don't init all fields in __init__, so it's easy to get attribute errors.  We consider
            # this "not configured" and want it to be a ValueError.
            raise ValueError("Not properly configured for an acquisition, cannot calculate image count.")

    def _raw_bytes_per_image(self) -> int:
        """Uncompressed bytes for a single captured frame at the current crop / pixel format.

        Worst-case assumptions matching the save pipeline: 24-bit color (3 bytes/px) or
        16-bit grayscale (2 bytes/px). Grayscale saved as pseudo-color expands to 3 samples.
        """
        width, height = self.camera.get_crop_size()
        is_color = squid.abc.CameraPixelFormat.is_color_format(self.camera.get_pixel_format())
        if is_color:
            bytes_per_pixel = 3
        elif control._def.SAVE_IN_PSEUDO_COLOR:
            bytes_per_pixel = 6  # uint16 grayscale promoted to 3-sample RGB
        else:
            bytes_per_pixel = 2
        return width * height * bytes_per_pixel

    def _format_size_factor(self) -> float:
        """Multiplier on raw (uncompressed) image bytes for the selected save format.

        INDIVIDUAL_IMAGES and OME_TIFF store planes uncompressed (~1.0). ZARR_V3 applies a
        blosc codec (per ``ZARR_COMPRESSION``) and writes a downsampled pyramid on top of
        level 0, so the on-disk size is ``pyramid_overhead / compression_ratio`` of raw.
        """
        if self.file_saving_option == control._def.FileSavingOption.ZARR_V3:
            ratio = _ZARR_COMPRESSION_RATIOS.get(control._def.ZARR_COMPRESSION, 1.0)
            return _ZARR_PYRAMID_OVERHEAD / ratio
        return 1.0

    def estimate_acquisition_disk_bytes(self) -> int:
        """Fast, format-aware estimate of the image bytes this acquisition will write.

        Pure arithmetic (no camera capture, no temp save), so it is cheap enough to call
        live as the user edits settings. Accounts for:

          * cycles / ragged plans (frames per position) via ``get_acquisition_image_count``,
          * the selected ``file_saving_option`` (zarr compression + pyramid overhead).

        Returns 0 if nothing would be captured. Raises ValueError if the controller is not
        configured for a valid acquisition.
        """
        image_count = self.get_acquisition_image_count()
        if image_count == 0:
            return 0
        return int(self._raw_bytes_per_image() * self._format_size_factor() * image_count)

    def get_estimated_acquisition_disk_storage(self):
        """
        This does its best to return the number of bytes needed to store the currently
        configured acquisition on disk.  If you don't have at least this amount of disk space
        available when starting this acquisition, it is likely it will fail with an
        "out of disk space" error.

        Note: for ZARR_V3 the byte estimate assumes a typical compression ratio for the
        selected preset; the real size is data-dependent and usually smaller.
        """
        # Add in 100kB for non-image files.  This is normally more like 10k total, so this gives us extra.
        non_image_file_size = 100 * 1024

        return self.estimate_acquisition_disk_bytes() + non_image_file_size

    def get_estimated_mosaic_ram_bytes(self) -> int:
        """
        Estimate the RAM (in bytes) required to hold the mosaic view in memory.

        The estimate is based on:

        * The mosaic scan bounds in stage space (mm) derived from ``self.scanCoordinates``.
        * The effective camera pixel size at the sample, computed from the objective
          magnification factor and the binned camera pixel size in microns.
        * A downsampling factor chosen so that the effective mosaic pixel size is at
          least ``control._def.MOSAIC_VIEW_TARGET_PIXEL_SIZE_UM`` (in µm). The scan
          extents are divided by this downsampled pixel size to obtain the mosaic width
          and height in pixels.

        Assumptions:

        * Each mosaic pixel is stored as a 16‑bit unsigned integer (2 bytes per pixel).
        * The returned value includes memory for all mosaic channel layers, by
          multiplying by ``len(self.selected_configurations)``.
        * The estimate only applies when ``control._def.USE_NAPARI_FOR_MOSAIC_DISPLAY``
          is enabled and when valid scan coordinates with regions are available;
          otherwise, it returns 0.
        """
        if not control._def.USE_NAPARI_FOR_MOSAIC_DISPLAY:
            return 0

        if not self.scanCoordinates or not self.scanCoordinates.has_regions():
            return 0

        bounds = self.scanCoordinates.get_scan_bounds()
        if not bounds:
            return 0

        # Calculate scan extents in mm
        width_mm = bounds["x"][1] - bounds["x"][0]
        height_mm = bounds["y"][1] - bounds["y"][0]

        # Get effective pixel size (with downsampling)
        pixel_size_um = self.objectiveStore.get_pixel_size_factor() * self.camera.get_pixel_size_binned_um()
        downsample_factor = max(1, int(control._def.MOSAIC_VIEW_TARGET_PIXEL_SIZE_UM / pixel_size_um))
        viewer_pixel_size_mm = (pixel_size_um * downsample_factor) / 1000

        # Calculate mosaic dimensions in pixels
        mosaic_width = int(math.ceil(width_mm / viewer_pixel_size_mm))
        mosaic_height = int(math.ceil(height_mm / viewer_pixel_size_mm))

        # Assume 2 bytes per pixel component (uint16), adjust for color and multiply by number of channels
        bytes_per_pixel = 2

        # If the camera provides color images (e.g. RGB), account for multiple components per pixel.
        # Mirror the logic used in get_estimated_acquisition_disk_storage to keep estimates consistent.
        try:
            # Common patterns: a boolean property or a zero-arg method named "is_color"
            is_color_attr = getattr(self.camera, "is_color", None)
            if callable(is_color_attr):
                if is_color_attr():
                    bytes_per_pixel *= 3
            elif isinstance(is_color_attr, bool) and is_color_attr:
                bytes_per_pixel *= 3
        except Exception:
            # If color information isn't available, fall back to the monochrome assumption.
            pass
        num_channels = (
            len(self.selected_observation_state_names)
            if self.selected_observation_state_names
            else len(self.selected_configurations)
        )
        if num_channels == 0:
            # No channels selected; this is likely an invalid acquisition state.
            # Log a warning (similar to disk storage estimation) and return 0 as a sentinel.
            squid.logging.get_logger(__name__).warning(
                "Estimated mosaic RAM is 0 because no channel configurations are selected."
            )
            return 0

        return mosaic_width * mosaic_height * bytes_per_pixel * num_channels

    def run_acquisition(self, acquire_current_fov=False):
        if not self.validate_acquisition_settings():
            # emit acquisition finished signal to re-enable the UI
            self.callbacks.signal_acquisition_finished()
            return
        self._start_per_acquisition_log()

        # Start memory monitoring for the acquisition (if enabled)
        if control._def.ENABLE_MEMORY_PROFILING:
            self._memory_monitor = MemoryMonitor(
                sample_interval_ms=200,
                process_name="main",
                track_children=True,
                log_interval_s=30.0,  # Log every 30 seconds during acquisition
            )
            self._memory_monitor.start("ACQUISITION_START")
            log_memory("ACQUISITION START", include_children=True)

        thread_started = False
        try:
            self._log.info("start multipoint")
            self._start_position = self.stage.get_pos()

            if self.z_range is None:
                self.z_range = (self._start_position.z_mm, self._start_position.z_mm + self.deltaZ * (self.NZ - 1))

            acquisition_scan_coordinates = self.scanCoordinates
            self.run_acquisition_current_fov = False
            if acquire_current_fov:
                pos = self.stage.get_pos()
                # No callback - we don't want to clobber existing info with this one off fov acquisition
                acquisition_scan_coordinates = ScanCoordinates(
                    objectiveStore=self.scanCoordinates.objectiveStore,
                    stage=self.scanCoordinates.stage,
                    camera=self.scanCoordinates.camera,
                )
                acquisition_scan_coordinates.clear_regions()
                acquisition_scan_coordinates.add_single_fov_region(
                    "current", center_x=pos.x_mm, center_y=pos.y_mm, center_z=pos.z_mm
                )
                self.run_acquisition_current_fov = True
            else:
                # Re-tile every region for the FOV the acquisition will actually image
                # (the largest observation-state ROI), so overlap is honored even if the
                # camera state changed since the regions were drawn.
                self.apply_observation_state_tiling(acquisition_scan_coordinates)

            scan_position_information = ScanPositionInformation.from_scan_coordinates(acquisition_scan_coordinates)

            # Save coordinates to CSV in top level folder
            coordinates_df = pd.DataFrame(columns=["region", "x (mm)", "y (mm)", "z (mm)"])
            for region_id, coords_list in scan_position_information.scan_region_fov_coords_mm.items():
                for coord in coords_list:
                    row = {"region": region_id, "x (mm)": coord[0], "y (mm)": coord[1]}
                    # Add z coordinate if available
                    if len(coord) > 2:
                        row["z (mm)"] = coord[2]
                    coordinates_df = pd.concat([coordinates_df, pd.DataFrame([row])], ignore_index=True)
            coordinates_df.to_csv(os.path.join(self.base_path, self.experiment_ID, "coordinates.csv"), index=False)

            self._log.info(
                f"num fovs: {sum(len(coords) for coords in scan_position_information.scan_region_fov_coords_mm.values())}"
            )
            self._log.info(f"num regions: {len(scan_position_information.scan_region_coords_mm)}")
            # self._log.info(f"region ids: {scan_position_information.scan_region_names}")
            # self._log.info(f"region centers: {scan_position_information.scan_region_coords_mm}")

            self.abort_acqusition_requested = False

            self.configuration_before_running_multipoint = self.liveController.obs_controller.current_observation_state

            # Snapshot the live camera geometry/mode (binning, ROI, pixel_format,
            # camera_mode) so it can be restored after the run. The acquisition
            # seeds the camera from the first observation preset's camera_live
            # snapshot (_seed_camera_for_first_observation_state), which can change
            # binning/ROI; apply_full_observation_state at the end only restores
            # exposure/gain/illumination, so without this the camera would be left
            # in the preset's resolution/ROI (e.g. defaulting back to 2x2 with a
            # cropped ROI) instead of the live state the user had before the run.
            self._camera_live_snapshot_before_multipoint = (
                self.liveController.obs_controller._collect_camera_live_snapshot()
            )

            # Edge case: nothing checked in the GUI → use the current live-controller
            # state as a single synthetic observation state for this run only.
            # Populated via build_params/AcquisitionParameters; NOT written to the
            # user's profile observation_presets/. Restored at the end of the run.
            self._inline_observation_states_for_run: Dict[str, ObservationState] = {}
            self._selected_observation_state_names_before_run = list(self.selected_observation_state_names)
            if not self.selected_observation_state_names:
                from control.core.observation_state_service import collect_emission_filter_positions
                wheel = getattr(self.liveController.microscope.addons, "emission_filter_wheel", None)
                try:
                    emission = collect_emission_filter_positions(wheel)
                except Exception:
                    emission = None
                live_state = self.liveController.obs_controller.collect_observation_state(
                    emission_filter_positions=emission or None,
                )
                live_state = live_state.model_copy(update={"name": "live"})
                self._inline_observation_states_for_run = {"live": live_state}
                self.selected_observation_state_names = ["live"]
                self._log.info("No observation states selected; using current live-controller state as 'live'.")

            # Snapshot illumination state before acquisition so it can be restored afterwards
            _illum_ctrl = getattr(self.liveController.microscope, "illumination_controller", None)
            self._illumination_snapshot_before_acquisition = (
                _illum_ctrl.snapshot() if _illum_ctrl is not None else None
            )

            # stop live
            if self.liveController.is_live:
                self.liveController_was_live_before_multipoint = True
                self.liveController.stop_live()  # @@@ to do: also uncheck the live button
            else:
                self.liveController_was_live_before_multipoint = False
            self.camera.stop_streaming()  # Ensure streaming is stopped before acquisition (important for some camera models)

            # TODO: Multipoint acquisition only supports software trigger for now (hardware trigger TBD).
            # Snapshot the prior mode so it can be restored when the acquisition completes.
            self._trigger_mode_before_multipoint = self.liveController.trigger_mode
            if self._trigger_mode_before_multipoint != control._def.TriggerMode.SOFTWARE:
                self.liveController.set_trigger_mode(control._def.TriggerMode.SOFTWARE)

            # Ensure all channels are off before acquisition begins
            if _illum_ctrl is not None:
                try:
                    _illum_ctrl.turn_off_all()
                except Exception as e:
                    self._log.warning(f"Failed to turn off all illumination before acquisition: {e}")

            self.camera_callback_was_enabled_before_multipoint = self.camera.get_callbacks_enabled()
            # We need callbacks, because we trigger and then use callbacks for image processing.  This
            # lets us do overlapping triggering (soon).
            self.camera.enable_callbacks(True)

            # run the acquisition
            self.timestamp_acquisition_started = time.time()
            if self.focus_map:
                self._log.info("Using focus surface for Z interpolation")
                for region_id in scan_position_information.scan_region_names:
                    region_fov_coords = scan_position_information.scan_region_fov_coords_mm[region_id]
                    # Convert each tuple to list for modification
                    for i, coords in enumerate(region_fov_coords):
                        x, y = coords[:2]  # This handles both (x,y) and (x,y,z) formats
                        z = self.focus_map.interpolate(x, y, region_id)
                        # Modify the list directly
                        region_fov_coords[i] = (x, y, z)
                        self.scanCoordinates.update_fov_z_level(region_id, i, z)

            elif self.gen_focus_map and not self.do_reflection_af:
                self._log.info("Generating autofocus plane for multipoint grid")
                bounds = self.scanCoordinates.get_scan_bounds()
                if not bounds:
                    return
                x_min, x_max = bounds["x"]
                y_min, y_max = bounds["y"]

                # Calculate scan dimensions and center
                x_span = abs(x_max - x_min)
                y_span = abs(y_max - y_min)
                x_center = (x_max + x_min) / 2
                y_center = (y_max + y_min) / 2

                # Determine grid size based on scan dimensions
                if x_span < self.deltaX:
                    fmap_Nx = 2
                    fmap_dx = self.deltaX  # Force deltaX spacing for small scans
                else:
                    fmap_Nx = min(4, max(2, int(x_span / self.deltaX) + 1))
                    fmap_dx = max(self.deltaX, x_span / (fmap_Nx - 1))

                if y_span < self.deltaY:
                    fmap_Ny = 2
                    fmap_dy = self.deltaY  # Force deltaY spacing for small scans
                else:
                    fmap_Ny = min(4, max(2, int(y_span / self.deltaY) + 1))
                    fmap_dy = max(self.deltaY, y_span / (fmap_Ny - 1))

                # Calculate starting corner position (top-left of the AF map grid)
                starting_x_mm = x_center - (fmap_Nx - 1) * fmap_dx / 2
                starting_y_mm = y_center - (fmap_Ny - 1) * fmap_dy / 2
                # TODO(sm): af map should be a grid mapped to a surface, instead of just corners mapped to a plane
                try:
                    # Store existing AF map if any
                    self.focus_map_storage = []
                    self.already_using_fmap = self.autofocusController.use_focus_map
                    for x, y, z in self.autofocusController.focus_map_coords:
                        self.focus_map_storage.append((x, y, z))

                    # Define grid corners for AF map
                    coord1 = (starting_x_mm, starting_y_mm)  # Starting corner
                    coord2 = (
                        starting_x_mm + (fmap_Nx - 1) * fmap_dx,
                        starting_y_mm,
                    )  # X-axis corner
                    coord3 = (
                        starting_x_mm,
                        starting_y_mm + (fmap_Ny - 1) * fmap_dy,
                    )  # Y-axis corner

                    self._log.info(f"Generating AF Map: Nx={fmap_Nx}, Ny={fmap_Ny}")
                    self._log.info(f"Spacing: dx={fmap_dx:.3f}mm, dy={fmap_dy:.3f}mm")
                    self._log.info(f"Center:  x=({x_center:.3f}mm, y={y_center:.3f}mm)")

                    # Generate and enable the AF map
                    self.autofocusController.gen_focus_map(coord1, coord2, coord3)
                    self.autofocusController.set_focus_map_use(True)

                    # Return to center position
                    self.stage.move_x_to(x_center)
                    self.stage.move_y_to(y_center)

                except ValueError:
                    self._log.exception("Invalid coordinates for autofocus plane, aborting.")
                    return

            def finish_fn():
                try:
                    self._on_acquisition_completed()
                    # Note: signal_acquisition_finished is called inside _on_acquisition_completed()
                finally:
                    self._stop_per_acquisition_log()

            updated_callbacks = dataclasses.replace(self.callbacks, signal_acquisition_finished=finish_fn)

            acquisition_params = self.build_params(scan_position_information=scan_position_information)

            # Gather objective and camera info for YAML
            current_objective = self.objectiveStore.current_objective
            objective_dict = self.objectiveStore.objectives_dict.get(current_objective, {})
            pixel_size_um = self.objectiveStore.get_pixel_size_factor() * self.camera.get_pixel_size_binned_um()
            objective_info = {
                "name": current_objective,
                "magnification": objective_dict.get("magnification"),
                "NA": objective_dict.get("NA"),
                "pixel_size_um": pixel_size_um,
                "camera_binning": list(self.camera.get_binning()) if hasattr(self.camera, "get_binning") else None,
                "sensor_pixel_size_um": self.camera.get_pixel_size_binned_um(),
            }
            if "tube_lens_f_mm" in objective_dict:
                objective_info["tube_lens_f_mm"] = objective_dict["tube_lens_f_mm"]

            # Get wellplate format if available
            wellplate_format = getattr(self.scanCoordinates, "format", None)

            # Save acquisition parameters to YAML
            experiment_path = os.path.join(self.base_path, self.experiment_ID)
            region_shapes = getattr(self.scanCoordinates, "region_shapes", None)
            _save_unified_multipoint_acquisition_yaml(
                acquisition_params,
                experiment_path,
                region_shapes,
                self.widget_type,
                objective_info,
                wellplate_format,
                self.scan_size_mm,
                self.overlap_percent,
                repo=self.liveController.microscope.config_repo,
                live_controller=self.liveController,
                camera=self.camera,
                objective_store=self.objectiveStore,
                recording_start_time=self.recording_start_time,
                selected_observation_state_names=self.selected_observation_state_names,
                use_manual_focus_map=self.use_manual_focus_map,
                logger=self._log,
            )

            # Save per-region observation state matrix CSV if customised
            _save_region_observation_state_csv(
                experiment_path,
                acquisition_params.region_observation_state_map,
                acquisition_params.selected_observation_state_names,
                logger=self._log,
            )

            # Save per-region laser-AF reference summary (laser AF runs only)
            _save_region_laser_af_references(
                experiment_path,
                acquisition_params.scan_position_information.scan_region_laser_af_references,
                logger=self._log,
            )

            # Save the resolved cycle/acquisition-order manifest (cycle runs only)
            _save_cycle_manifest(
                experiment_path,
                acquisition_params,
                self.liveController.microscope.config_repo,
                logger=self._log,
            )

            # Save per-pattern LED indices + NA positions for source-coded FPM runs
            # (self-contained geometry for offline reconstruction).
            _fpm_obj_na = None
            try:
                _fpm_obj_na = float(self.objectiveStore.get_current_objective_info().get("NA"))
            except Exception:
                pass
            _save_fpm_pattern_positions(
                experiment_path,
                acquisition_params,
                self.liveController.microscope.config_repo,
                _fpm_obj_na,
                logger=self._log,
            )

            # Get pre-warmed job runner and its shared backpressure values
            # (starts a new one warming for next acquisition)
            prewarmed_runner, prewarmed_bp_values = self.get_prewarmed_job_runner()

            # Worker creation can fail - ensure runner is cleaned up on error
            try:
                self.multiPointWorker = MultiPointWorker(
                    scope=self.microscope,
                    live_controller=self.liveController,
                    auto_focus_controller=self.autofocusController,
                    laser_auto_focus_controller=self.laserAutoFocusController,
                    objective_store=self.objectiveStore,
                    acquisition_parameters=acquisition_params,
                    callbacks=updated_callbacks,
                    abort_requested_fn=lambda: self.abort_acqusition_requested,
                    request_abort_fn=self.request_abort_acquisition,
                    extra_job_classes=[],
                    alignment_widget=self._alignment_widget,
                    slack_notifier=self._slack_notifier,
                    prewarmed_job_runner=prewarmed_runner,
                    prewarmed_bp_values=prewarmed_bp_values,
                )
            except Exception:
                # Clean up pre-warmed runner if worker creation failed.
                # Note: get_prewarmed_job_runner() already started a NEW pre-warmed runner,
                # so we're cleaning up the one that was handed off to us.
                self._cleanup_prewarmed_runner(
                    prewarmed_runner,
                    context="after worker creation failure",
                )
                raise

            # Signal after worker creation so backpressure_controller is available
            self.callbacks.signal_acquisition_start(acquisition_params)

            self.thread = Thread(target=self.multiPointWorker.run, name="Acquisition thread", daemon=True)
            thread_started = True
            self.thread.start()
        finally:
            if not thread_started:
                self._stop_per_acquisition_log()
                # Stop memory monitor if acquisition setup failed
                if self._memory_monitor is not None:
                    self._memory_monitor.stop()
                    self._memory_monitor = None
                # If we mutated selected_observation_state_names with a synthetic
                # "live" entry but never started the thread, restore now —
                # _on_acquisition_completed (which normally restores) won't fire.
                prior_selection = getattr(self, "_selected_observation_state_names_before_run", None)
                if prior_selection is not None:
                    self.selected_observation_state_names = list(prior_selection)
                    self._selected_observation_state_names_before_run = None
                self._inline_observation_states_for_run = {}

    def build_params(self, scan_position_information: ScanPositionInformation) -> AcquisitionParameters:
        # Determine plate dimensions from wellplate format if available
        plate_num_rows = 8  # Default for 96-well
        plate_num_cols = 12
        if hasattr(self.scanCoordinates, "format") and self.scanCoordinates.format:
            format_settings = control._def.get_wellplate_settings(self.scanCoordinates.format)
            if format_settings:
                plate_num_rows = format_settings.get("rows", 8)
                plate_num_cols = format_settings.get("cols", 12)
            else:
                self._log.debug(
                    f"Unknown wellplate format '{self.scanCoordinates.format}', using default 96-well dimensions"
                )

        global_plan, region_plans = self._build_region_plans(scan_position_information.scan_region_names)

        return AcquisitionParameters(
            experiment_ID=self.experiment_ID,
            base_path=self.base_path,
            acquisition_start_time=self.timestamp_acquisition_started,
            scan_position_information=scan_position_information,
            NX=self.NX,
            deltaX=self.deltaX,
            NY=self.NY,
            deltaY=self.deltaY,
            NZ=self.NZ,
            deltaZ=self.deltaZ,
            Nt=self.Nt,
            deltat=self.deltat,
            do_autofocus=self.do_autofocus,
            do_reflection_autofocus=self.do_reflection_af,
            use_piezo=self.use_piezo,
            display_resolution_scaling=self.display_resolution_scaling,
            z_stacking_config=self.z_stacking_config,
            z_range=self.z_range,
            use_fluidics=self.use_fluidics,
            skip_saving=self.skip_saving,
            file_saving_option=self.file_saving_option,
            keep_illuminators_on_between_captures=self.keep_illuminators_on_between_captures,
            # Downsampled view generation parameters
            generate_downsampled_views=control._def.SAVE_DOWNSAMPLED_WELL_IMAGES or control._def.DISPLAY_PLATE_VIEW,
            save_downsampled_well_images=control._def.SAVE_DOWNSAMPLED_WELL_IMAGES,
            downsampled_well_resolutions_um=control._def.DOWNSAMPLED_WELL_RESOLUTIONS_UM,
            downsampled_plate_resolution_um=control._def.DOWNSAMPLED_PLATE_RESOLUTION_UM,
            downsampled_z_projection=control._def.DOWNSAMPLED_Z_PROJECTION,
            downsampled_interpolation_method=control._def.DOWNSAMPLED_INTERPOLATION_METHOD,
            plate_num_rows=plate_num_rows,
            plate_num_cols=plate_num_cols,
            xy_mode=self.xy_mode,
            selected_observation_state_names=self.selected_observation_state_names,
            region_observation_state_map=self.region_observation_state_map,
            selected_cycle_names=list(self.selected_cycle_names),
            region_cycle_map=self.region_cycle_map,
            global_region_plan=global_plan,
            resolved_region_plans=region_plans,
            inline_observation_states=dict(getattr(self, "_inline_observation_states_for_run", {})),
            laser_af_seed_mode=self.laser_af_seed_mode,
            laser_af_refresh_every_n_fovs=self.laser_af_refresh_every_n_fovs,
            laser_af_consistency_threshold_um=self.laser_af_consistency_threshold_um,
            laser_af_check_last_fov_per_region=self.laser_af_check_last_fov_per_region,
            zarr_upload_enabled=self.zarr_upload_enabled,
            zarr_upload_remote_root=self.zarr_upload_remote_root,
            zarr_upload_delete_after_verify=self.zarr_upload_delete_after_verify,
        )

    def _on_acquisition_completed(self):
        self._log.debug("MultiPointController._on_acquisition_completed called")
        # Note: Plate views are saved per timepoint in the worker's run_single_time_point method

        try:
            self._restore_state_after_acquisition()
        finally:
            # Always notify the UI that the acquisition is done, even if restoration
            # raised — otherwise the UI hangs waiting for the finished signal.
            try:
                self.callbacks.signal_acquisition_finished()
            except Exception:
                self._log.exception("Failed to emit signal_acquisition_finished")

    def _restore_state_after_acquisition(self):
        # If we synthesized a "live" preset for this run, undo the selection
        # injection so subsequent runs (or the GUI) see the original list.
        prior_selection = getattr(self, "_selected_observation_state_names_before_run", None)
        if prior_selection is not None:
            self.selected_observation_state_names = list(prior_selection)
            self._selected_observation_state_names_before_run = None
        self._inline_observation_states_for_run = {}

        # restore the previous selected mode
        if self.gen_focus_map:
            self.autofocusController.clear_focus_map()
            for x, y, z in self.focus_map_storage:
                self.autofocusController.focus_map_coords.append((x, y, z))
            self.autofocusController.use_focus_map = self.already_using_fmap

        # Only restore prior observation state if one was actually selected at acquisition start.
        # The Qt signal for signal_current_configuration is typed as Signal(ObservationState)
        # and will reject None.
        prior_config = self.configuration_before_running_multipoint
        if prior_config is not None:
            self.callbacks.signal_current_configuration(prior_config)
            self.liveController.obs_controller.apply_full_observation_state(prior_config)

        # Restore the live camera geometry/mode captured at acquisition start.
        # apply_full_observation_state above only restores exposure/gain/illumination,
        # so binning/ROI/pixel_format/camera_mode must be re-applied here to undo the
        # first-preset seeding (otherwise live returns at the preset's resolution/ROI).
        # Trigger settings are restored separately below, so skip them here.
        camera_snapshot = getattr(self, "_camera_live_snapshot_before_multipoint", None)
        if camera_snapshot is not None:
            try:
                self.liveController.obs_controller._apply_camera_live_snapshot(
                    camera_snapshot, apply_trigger_settings=False
                )
            except Exception as e:
                self._log.warning(f"Failed to restore camera geometry after acquisition: {e}")
            self._camera_live_snapshot_before_multipoint = None

        # Restore illumination state that was active before the acquisition
        _illum_snapshot = getattr(self, "_illumination_snapshot_before_acquisition", None)
        if _illum_snapshot is not None:
            _illum_ctrl = getattr(self.liveController.microscope, "illumination_controller", None)
            if _illum_ctrl is not None:
                try:
                    _illum_ctrl.restore(_illum_snapshot)
                except Exception as e:
                    self._log.warning(f"Failed to restore illumination state after acquisition: {e}")
            self._illumination_snapshot_before_acquisition = None

        # Restore callbacks to pre-acquisition state
        self.camera.enable_callbacks(self.camera_callback_was_enabled_before_multipoint)

        # Restore trigger mode that was active before the acquisition
        prior_trigger_mode = getattr(self, "_trigger_mode_before_multipoint", None)
        if prior_trigger_mode is not None and prior_trigger_mode != self.liveController.trigger_mode:
            try:
                self.liveController.set_trigger_mode(prior_trigger_mode)
            except Exception as e:
                self._log.warning(f"Failed to restore trigger mode after acquisition: {e}")
        self._trigger_mode_before_multipoint = None

        # re-enable live if it's previously on
        if self.liveController_was_live_before_multipoint and control._def.RESUME_LIVE_AFTER_ACQUISITION:
            self.liveController.start_live()

        # Stop memory monitoring and log final report (in background to not delay acquisition finish)
        if self._memory_monitor is not None:
            monitor = self._memory_monitor
            self._memory_monitor = None

            def _stop_monitor_background():
                try:
                    if control._def.ENABLE_MEMORY_PROFILING:
                        log_memory("ACQUISITION COMPLETE", include_children=True)
                    monitor.stop()
                except Exception as e:
                    self._log.error(f"Error stopping memory monitor in background: {e}")

            Thread(target=_stop_monitor_background, daemon=True).start()

        self._log.info(f"total time for acquisition + processing + reset: {time.time() - self.recording_start_time}")
        utils.create_done_file(os.path.join(self.base_path, self.experiment_ID))

        if self.run_acquisition_current_fov:
            self.run_acquisition_current_fov = False

        if self._start_position:
            x_mm = self._start_position.x_mm
            y_mm = self._start_position.y_mm
            z_mm = self._start_position.z_mm
            self._log.info(f"Moving back to start position: (x,y,z) [mm] = ({x_mm}, {y_mm}, {z_mm})")
            self.stage.move_x_to(x_mm)
            self.stage.move_y_to(y_mm)
            self.stage.move_z_to(z_mm)
            self._start_position = None

        ending_pos = self.stage.get_pos()
        self.callbacks.signal_current_fov(ending_pos.x_mm, ending_pos.y_mm)

    def request_abort_acquisition(self):
        self.abort_acqusition_requested = True

    def validate_acquisition_settings(self) -> bool:
        """Validate settings before starting acquisition"""
        if self.do_reflection_af:
            # Acceptable when a global reference is set (regions without their own
            # reference fall back to it) OR every region carries a per-region
            # reference (no global needed). Otherwise some region would have no
            # focus target.
            has_global = self.laserAutoFocusController.laser_af_properties.has_reference
            region_refs = getattr(self.scanCoordinates, "region_laser_af_references", {}) or {}
            region_ids = list(getattr(self.scanCoordinates, "region_centers", {}).keys())
            all_regions_have_refs = bool(region_ids) and all(rid in region_refs for rid in region_ids)
            if not has_global and not all_regions_have_refs:
                self._log.error(
                    "Laser Autofocus Not Ready - set the laser autofocus reference position "
                    "(global) or capture a per-region reference for every region before "
                    "starting acquisition with laser AF enabled."
                )
                return False

        # When any selected observation state has timed illuminators (capture-
        # window or stimulus-only), the worker arms an NIDAQ pulse waveform.
        # Without an NIDAQ on this rig those steps cannot run.
        repo = self.liveController.microscope.config_repo
        ic = self.liveController.microscope.illumination_controller
        for name in self.selected_observation_state_names or []:
            preset = repo.load_observation_preset(name)
            if preset is None:
                continue
            is_stimulus = bool(getattr(preset, "is_stimulus_only", False))
            if not preset.is_waveform_driven and not is_stimulus:
                continue
            if getattr(self.microscope.addons, "nidaq", None) is None:
                self._log.error(
                    "Observation state '%s' uses NIDAQ pulse timing but no NIDAQ is configured on this microscope.",
                    name,
                )
                return False
            # Capture-window pulses require the camera frame-signal terminal.
            # Stimulus-only steps fire on SOFTWARE trigger, so the terminal is
            # irrelevant for them.
            if not is_stimulus and not control._def.NIDAQ_FRAME_SIGNAL_TERMINAL:
                self._log.error(
                    "Observation state '%s' uses NIDAQ pulse timing but NIDAQ_FRAME_SIGNAL_TERMINAL is not set.",
                    name,
                )
                return False
            if is_stimulus:
                if not preset.stimulus_duration_ms or preset.stimulus_duration_ms <= 0:
                    self._log.error(
                        "Observation state '%s' is_stimulus_only but stimulus_duration_ms is unset or non-positive.",
                        name,
                    )
                    return False
                window_ms = float(preset.stimulus_duration_ms)
                has_timed = False
                for ist in preset.illuminator_states:
                    if not ist.on or ist.timing is None:
                        continue
                    has_timed = True
                    if ist.timing.end_ms > window_ms + 1e-6:
                        self._log.error(
                            "Observation state '%s': illuminator '%s' comb end (%.2f ms) exceeds stimulus_duration_ms (%.2f ms).",
                            name, ist.illumination_channel, ist.timing.end_ms, window_ms,
                        )
                        return False
                if not has_timed:
                    self._log.error(
                        "Observation state '%s' is_stimulus_only but no active illuminator has a timing comb.",
                        name,
                    )
                    return False
            for ist in preset.illuminator_states:
                if not ist.on or ist.timing is None:
                    continue
                if ic.get_nidaq_do_line_for_channel(ist.illumination_channel) is None:
                    self._log.error(
                        "Observation state '%s': illuminator '%s' has pulse timing but no NIDAQ digital-output line is wired for it.",
                        name,
                        ist.illumination_channel,
                    )
                    return False

        # FPM items (brightfield sweep, clustered darkfield, or source-coded
        # darkfield) each need (a) a SciMicroscopy unified LED matrix and (b) a base
        # observation state whose LED-matrix channel is ON (the mux override only
        # lights an already-on matrix). Validate up front so a misconfiguration
        # fails clearly here instead of silently capturing dark/wrong frames mid-run.
        from control.models.acquisition_cycle import (
            CycleFPMBrightfield,
            CycleFPMClusteredDarkfield,
            CycleFPMDarkfield,
        )

        fpm_types = (CycleFPMBrightfield, CycleFPMClusteredDarkfield, CycleFPMDarkfield)
        cycle_names = list(self.selected_cycle_names or [])
        for names in (self.region_cycle_map or {}).values():
            cycle_names.extend(names or [])
        fpm_base_states = []
        for cname in dict.fromkeys(cycle_names):
            cyc = repo.load_acquisition_cycle(cname)
            if cyc is None:
                continue
            for it in cyc.items:
                if isinstance(it, fpm_types):
                    fpm_base_states.append(it.observation_state)
        if fpm_base_states:
            matrix_name = ic.unified_led_matrix_channel_name()
            if matrix_name is None:
                self._log.error(
                    "An FPM cycle is selected but no SciMicroscopy unified LED matrix is configured."
                )
                return False
            for state_name in dict.fromkeys(fpm_base_states):
                preset = repo.load_observation_preset(state_name)
                if preset is None:
                    self._log.error("FPM base observation state '%s' not found.", state_name)
                    return False
                if not any(ist.illumination_channel == matrix_name for ist in preset.active_illuminator_states):
                    self._log.error(
                        "FPM base observation state '%s' must have the LED matrix channel ('%s') ON.",
                        state_name,
                        matrix_name,
                    )
                    return False
        return True

    def get_plate_view(self) -> np.ndarray:
        """Get the current plate view array from the acquisition.

        Returns:
            Copy of the plate view array, or None if not available.
        """
        if self.multiPointWorker is not None:
            return self.multiPointWorker.get_plate_view()
        return None

    @property
    def backpressure_controller(self) -> Optional["BackpressureController"]:
        """Get the backpressure controller from the current worker.

        Returns:
            BackpressureController if worker exists, None otherwise.
        """
        if self.multiPointWorker is not None:
            return getattr(self.multiPointWorker, "_backpressure", None)
        return None

    _PROCESS_TERMINATE_TIMEOUT_S = 1.0

    def close(self, timeout_s: float = 5.0) -> None:
        """Clean up resources on application shutdown.

        Aborts any running acquisition and waits for cleanup to complete.
        If job runner processes do not terminate gracefully, they are forcefully
        terminated (SIGTERM) then killed (SIGKILL). This may result in incomplete
        well images or unsaved data.

        Args:
            timeout_s: Maximum time to wait for acquisition thread to finish.
                      Job runner processes have a separate timeout defined by
                      _PROCESS_TERMINATE_TIMEOUT_S.
        """
        # Clean up pre-warmed job runner if it exists
        if self._prewarmed_job_runner is not None:
            self._log.info("Shutting down pre-warmed job runner...")
        self._cleanup_prewarmed_runner(
            self._prewarmed_job_runner,
            timeout_s=self._PROCESS_TERMINATE_TIMEOUT_S,
            context="during close",
        )
        self._prewarmed_job_runner = None
        self._prewarmed_bp_values = None

        # Abort any running acquisition
        try:
            if self.acquisition_in_progress():
                self.request_abort_acquisition()
                if self.thread is not None:
                    self.thread.join(timeout=timeout_s)
                    if self.thread.is_alive():
                        self._log.warning(f"Acquisition thread did not stop within {timeout_s}s")
        except Exception:
            self._log.exception("Error aborting acquisition during close")

        # Stop memory monitor if running
        try:
            if self._memory_monitor is not None:
                self._memory_monitor.stop()
                self._memory_monitor = None
        except Exception:
            self._log.exception("Error stopping memory monitor during close")

        # Forcefully terminate any remaining job runner processes
        if self.multiPointWorker is not None:
            job_runners = getattr(self.multiPointWorker, "_job_runners", [])
            for job_class, job_runner in job_runners:
                try:
                    if job_runner is not None and job_runner.is_alive():
                        self._log.warning(f"Terminating {job_class.__name__} job runner (abnormal shutdown)")
                        job_runner.terminate()
                        job_runner.join(timeout=self._PROCESS_TERMINATE_TIMEOUT_S)
                        # If still alive after terminate, force kill
                        if job_runner.is_alive():
                            self._log.warning(f"Force killing {job_class.__name__} job runner")
                            job_runner.kill()
                            job_runner.join(timeout=self._PROCESS_TERMINATE_TIMEOUT_S)
                            # Final check - warn if zombie process remains
                            if job_runner.is_alive():
                                self._log.error(
                                    f"{job_class.__name__} job runner could not be terminated - "
                                    "zombie process may remain"
                                )
                except Exception:
                    self._log.exception(f"Error terminating {job_class.__name__} job runner")

            # Release backpressure controller resources to prevent semaphore leaks
            try:
                backpressure = getattr(self.multiPointWorker, "_backpressure", None)
                if backpressure is not None:
                    backpressure.close()
            except Exception:
                self._log.exception("Error closing backpressure controller during shutdown")

        # Clear worker reference
        self.multiPointWorker = None
        self.thread = None
