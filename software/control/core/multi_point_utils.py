from dataclasses import dataclass, field
from typing import List, Tuple, Dict, Optional, Callable, Union, TYPE_CHECKING

from control._def import FileSavingOption, ZProjectionMode, DownsamplingMethod
from control.core.job_processing import CaptureInfo
from control.core.scan_coordinates import ScanCoordinates
from squid.abc import CameraFrame

if TYPE_CHECKING:
    from control.slack_notifier import TimepointStats, AcquisitionStats
    from control.models.observation_state import ObservationState
    from control.models.acquisition_cycle import RegionPlan
    from control.models.laser_af_reference import LaserAFReference


@dataclass
class ScanPositionInformation:
    scan_region_coords_mm: List[Tuple[float, float]]
    scan_region_names: List[str]
    scan_region_fov_coords_mm: Dict[str, List[Tuple[float, float, float]]]
    # Optional per-region laser-AF focus targets, keyed by region id. Regions
    # without an entry fall back to the controller's global reference.
    scan_region_laser_af_references: Dict[str, "LaserAFReference"] = field(default_factory=dict)

    @staticmethod
    def from_scan_coordinates(scan_coordinates: ScanCoordinates):
        return ScanPositionInformation(
            scan_region_coords_mm=list(scan_coordinates.region_centers.values()),
            scan_region_names=list(scan_coordinates.region_centers.keys()),
            scan_region_fov_coords_mm=dict(scan_coordinates.region_fov_coordinates),
            scan_region_laser_af_references=dict(
                getattr(scan_coordinates, "region_laser_af_references", {})
            ),
        )


@dataclass
class AcquisitionParameters:
    experiment_ID: Optional[str]
    base_path: Optional[str]
    acquisition_start_time: float
    scan_position_information: ScanPositionInformation

    # NOTE(imo): I'm pretty sure NX and NY are broken?  They are not used in MPW anywhere.
    NX: int
    deltaX: float
    NY: int
    deltaY: float

    NZ: int
    deltaZ: float
    Nt: int
    deltat: float

    do_autofocus: bool
    do_reflection_autofocus: bool

    use_piezo: bool
    display_resolution_scaling: float

    z_stacking_config: str
    z_range: Tuple[float, float]

    use_fluidics: bool
    skip_saving: bool = False
    # On-disk format for saved images (INDIVIDUAL_IMAGES, MULTI_PAGE_TIFF, OME_TIFF, ZARR_V3).
    # Snapshotted at acquisition start so the worker doesn't depend on a mutable global.
    file_saving_option: FileSavingOption = FileSavingOption.INDIVIDUAL_IMAGES
    # Software trigger: if True, skip turn_off_illumination after each frame until channel changes
    keep_illuminators_on_between_captures: bool = False

    # Downsampled view generation parameters
    generate_downsampled_views: bool = False
    save_downsampled_well_images: bool = False  # Save individual well TIFFs (wells/A1_5um.tiff)
    downsampled_well_resolutions_um: Optional[List[float]] = None
    downsampled_plate_resolution_um: float = 10.0
    downsampled_z_projection: Union[ZProjectionMode, str] = ZProjectionMode.MIP
    downsampled_interpolation_method: Union[DownsamplingMethod, str] = DownsamplingMethod.INTER_AREA_FAST
    plate_num_rows: int = 8  # For 96-well plate
    plate_num_cols: int = 12  # For 96-well plate

    # XY mode for determining scan type
    xy_mode: str = "Current Position"  # "Current Position", "Select Wells", "Manual", "Load Coordinates"
    # Observation State preset names (profile observation_presets/). This is the
    # imaged channel axis (distinct imaged states); for cycle-driven runs it is
    # derived from the selected cycles so existing zarr/metadata/naming code
    # keeps working unchanged.
    selected_observation_state_names: List[str] = field(default_factory=list)
    # Per-region observation state override. Keys are region IDs (e.g. "R0"),
    # values are lists of preset names to acquire at that region.
    # None means all regions use selected_observation_state_names.
    region_observation_state_map: Optional[Dict[str, List[str]]] = None

    # Cycle-driven per-position acquisition plan. When `selected_cycle_names` is
    # empty the worker uses the legacy flat path (one frame per state, in
    # `selected_observation_state_names` order). `global_region_plan` is the
    # resolved plan applied to every region that has no explicit override;
    # `resolved_region_plans` holds per-region overrides (keyed by region_id).
    selected_cycle_names: List[str] = field(default_factory=list)
    region_cycle_map: Optional[Dict[str, List[str]]] = None
    global_region_plan: Optional["RegionPlan"] = None
    resolved_region_plans: Dict[str, "RegionPlan"] = field(default_factory=dict)

    # Run-only ObservationStates not backed by an on-disk preset. Used when no
    # preset is checked in the GUI: the controller snapshots the current live
    # state, names it (e.g. "live"), and passes it here. The worker seeds its
    # preset cache from this dict so disk loads are skipped, and acquisition.yaml
    # records these states under ``observation_states_used`` just like real presets.
    inline_observation_states: Dict[str, "ObservationState"] = field(default_factory=dict)

    # Laser-AF per-FOV offset table controls. See _def.LASER_AF_SEED_MODE and
    # LASER_AF_REFRESH_EVERY_N_FOVS for semantics.
    laser_af_seed_mode: str = "scan"  # "scan" | "lazy"
    laser_af_refresh_every_n_fovs: int = 10
    laser_af_consistency_threshold_um: float = 5.0
    laser_af_check_last_fov_per_region: bool = True

    # Live ZARR_V3 streaming upload to a network drive. When ``zarr_upload_enabled``
    # is True and ``file_saving_option == ZARR_V3``, the acquisition is mirrored
    # to ``<zarr_upload_remote_root>/<experiment_ID>/`` (the configured path is
    # the PARENT folder; UNC ``\\server\share\dir`` on Windows or
    # ``//server/share/dir`` POSIX) by a dedicated UploadWorker — shards stream
    # per timepoint, metadata and every sidecar follow at end of run. When
    # ``zarr_upload_delete_after_verify`` is True, local shard files are
    # deleted in batches at the end of each timepoint once every shard in that
    # timepoint has been sha256-verified on the remote. See
    # ``control.core.zarr_upload`` for details.
    zarr_upload_enabled: bool = False
    zarr_upload_remote_root: str = ""
    zarr_upload_delete_after_verify: bool = True
    # Pre-run estimate of the acquisition's total on-disk size (bytes), from
    # MultiPointController.get_estimated_acquisition_disk_storage(). The
    # worker's mid-run upload health check divides by Nt to warn when local
    # free space drops below a couple of timepoints' headroom. 0 = unknown.
    estimated_total_disk_bytes: int = 0


@dataclass
class OverallProgressUpdate:
    current_region: int
    total_regions: int

    current_timepoint: int
    total_timepoints: int


@dataclass
class RegionProgressUpdate:
    current_fov: int
    region_fovs: int


@dataclass
class PlateViewUpdate:
    """Data for plate view channel update."""

    channel_idx: int
    channel_name: str
    plate_image: "np.ndarray"  # Forward reference


@dataclass
class PlateViewInit:
    """Data for plate view initialization."""

    num_rows: int
    num_cols: int
    well_slot_shape: Tuple[int, int]
    fov_grid_shape: Tuple[int, int]
    channel_names: List[str]


@dataclass
class MultiPointControllerFunctions:
    signal_acquisition_start: Callable[[AcquisitionParameters], None]
    signal_acquisition_finished: Callable[[], None]
    signal_new_image: Callable[[CameraFrame, CaptureInfo], None]
    signal_current_configuration: Callable
    signal_current_fov: Callable[[float, float], None]
    signal_overall_progress: Callable[[OverallProgressUpdate], None]
    signal_region_progress: Callable[[RegionProgressUpdate], None]
    # Optional plate view callbacks. Default no-op lambdas avoid None checks at every call site.
    # Unlike mutable defaults (lists/dicts), lambdas are safe as defaults since they're not modified.
    signal_plate_view_init: Callable[[PlateViewInit], None] = lambda *a, **kw: None
    signal_plate_view_update: Callable[[PlateViewUpdate], None] = lambda *a, **kw: None
    # Optional Slack notification callbacks (allows main thread to capture screenshot and maintain ordering)
    signal_slack_timepoint_notification: Callable[["TimepointStats"], None] = lambda *a, **kw: None
    signal_slack_acquisition_finished: Callable[["AcquisitionStats"], None] = lambda *a, **kw: None
    # Zarr frame written callback - called when subprocess completes writing a frame
    # Args: (fov, time_point, z_index, channel_name, region_idx)
    signal_zarr_frame_written: Callable[[int, int, int, str, int], None] = lambda *a, **kw: None
    # Fires at the start of each timepoint (arg: time_point index). Used by napari
    # views to flush per-timepoint state so peak RAM tracks a single timepoint
    # rather than accumulating across the run.
    signal_new_time_point: Callable[[int], None] = lambda *a, **kw: None
    # Fires once the background job-runner shutdown has finished, i.e. every zarr
    # writer has been finalized and all data is durably on disk. This lands AFTER
    # signal_acquisition_finished (which only signals that capture ended); it is
    # the point at which it is safe to move/copy the dataset. Used by the GUI to
    # keep the progress bar up as "Finalizing..." until writeback truly completes.
    signal_data_writing_complete: Callable[[], None] = lambda *a, **kw: None
