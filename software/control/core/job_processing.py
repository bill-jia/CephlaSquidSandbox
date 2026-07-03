import abc
import csv
import faulthandler
import multiprocessing
import queue
import os
import sys
import time
import json
from datetime import datetime, timezone
from contextlib import contextmanager
from typing import ClassVar, Dict, Generic, List, Optional, Set, Tuple, TypeVar, Union
from uuid import uuid4

from dataclasses import dataclass, field
from filelock import FileLock, Timeout as FileLockTimeout

import imageio as iio
import numpy as np
import tifffile

from control import _def, utils, utils_acquisition
from control._def import ZProjectionMode, DownsamplingMethod
import squid.abc
import squid.logging
from control.models.observation_state import ObservationState
from control.core import utils_ome_tiff_writer as ome_tiff_writer
from control.core.memory_profiler import (
    start_worker_monitoring,
    stop_worker_monitoring,
    set_worker_operation,
    log_memory,
)
from control.core.zarr_upload import (
    UploadTarget,
    UploadTask,
    local_to_remote_path,
)


# If the JobRunner subprocess's finalize + exit takes longer than this, a
# faulthandler watchdog dumps every thread's stack (repeatedly) to a stacks
# file, so a wedge in finalize/teardown is captured with an exact culprit
# frame instead of just the parent's "did not exit within 600s" terminate.
# Comfortably above a healthy shard-per-z finalize (seconds) and below the
# parent's JOB_RUNNER_FINALIZE_TIMEOUT_S kill budget so several dumps land
# before the kill.
JOB_RUNNER_FINALIZE_WATCHDOG_S = 90


@dataclass
class AcquisitionInfo:
    """Acquisition-wide metadata for OME-TIFF file generation.

    This class holds metadata that remains constant across all images in a
    multi-dimensional acquisition (time, z, channel). It is separate from
    CaptureInfo, which holds per-image metadata (position, timestamp, etc.).

    AcquisitionInfo is created once at acquisition start and injected into
    SaveOMETiffJob instances by JobRunner.dispatch() before job execution.

    Attributes:
        total_time_points: Number of time points in the acquisition.
        total_z_levels: Number of z-slices per stack.
        total_channels: Number of imaging channels.
        channel_names: List of channel names for OME-XML metadata.
        experiment_path: Base directory for the experiment output.
        time_increment_s: Time between timepoints in seconds (for OME-XML).
        physical_size_z_um: Z step size in micrometers (for OME-XML).
        physical_size_x_um: Pixel size in X in micrometers (for OME-XML).
        physical_size_y_um: Pixel size in Y in micrometers (for OME-XML).
    """

    total_time_points: int
    total_z_levels: int
    total_channels: int
    channel_names: List[str]
    experiment_path: Optional[str] = None
    time_increment_s: Optional[float] = None
    physical_size_z_um: Optional[float] = None
    physical_size_x_um: Optional[float] = None
    physical_size_y_um: Optional[float] = None


from .downsampled_views import (
    crop_overlap,
    downsample_tile,
    downsample_to_resolutions,
    WellTileAccumulator,
)


# NOTE(imo): We want this to be fast.  But pydantic does not support numpy serialization natively, which means
# that we need a custom serializer (which will be slow!).  So, use dataclass here instead.
@dataclass
class CaptureInfo:
    position: squid.abc.Pos
    z_index: int
    capture_time: float
    observation_state: ObservationState
    save_directory: str
    file_id: str
    region_id: int
    fov: int
    configuration_idx: int
    z_piezo_um: Optional[float] = None
    time_point: Optional[int] = None
    filename_channel_label: Optional[str] = None
    """If set, used for TIFF basename instead of observation_state.name."""
    # ── Self-describing save layout (cycles) ──
    # When ``array_key`` is None and the ``save_*`` fields are None, the legacy
    # global ``zarr_writer_info`` dims and ``(time_point, configuration_idx)``
    # coordinates are used (today's behaviour). When set, the frame fully
    # describes its own array so the save layer needs no global uniform
    # ``(T, C, Z)`` assumption — which per-region cycles and ragged counts break.
    # ``array_key`` None = dense single array per FOV; a state name = a ragged
    # single-channel per-state plate/store.
    array_key: Optional[str] = None
    save_t_index: Optional[int] = None
    save_c_index: Optional[int] = None
    save_t_size: Optional[int] = None
    save_c_size: Optional[int] = None
    # Per-array Z extent. None => the global ``zarr_writer_info.z_size`` (full
    # stack). Set to 1 for a reference-z-only capture so its array is single-z;
    # lets a ragged run mix full-z and reference-only states with different Z.
    save_z_size: Optional[int] = None
    cycle_event_index: Optional[int] = None
    state_frame_index: Optional[int] = None
    frame_suffix: Optional[str] = None
    """Disambiguating basename suffix for per-frame formats; None = no suffix."""
    array_channel_names: Optional[List[str]] = None
    array_channel_colors: Optional[List[str]] = None
    array_channel_wavelengths: Optional[List[Optional[int]]] = None
    # On-disk save format the worker selected for this acquisition. Travels with
    # the job through the multiprocessing pickle so subprocess code branches on
    # this value rather than reading the (stale) global ``_def.FILE_SAVING_OPTION``.
    file_saving_option: Optional["_def.FileSavingOption"] = None
    # Experiment root (``{base_path}/{experiment_ID}``). Used by writers that
    # consolidate output to a single root-level file (e.g. the ZARR_V3
    # acquisition_times.csv) instead of per-timepoint sidecars.
    acquisition_root: Optional[str] = None
    # Online-postprocessing routing tag. When set, this frame is an input to a
    # postprocess group (routed to the PostprocessJob runner) — its raw image is
    # NOT saved, and its save_* fields are None.
    postprocess_group: Optional[str] = None


@dataclass()
class JobImage:
    image_array: Optional[np.array]


T = TypeVar("T")


@dataclass
class Job(abc.ABC, Generic[T]):
    capture_info: CaptureInfo
    capture_image: JobImage

    job_id: str = field(default_factory=lambda: str(uuid4()))

    def image_array(self) -> np.array:
        if self.capture_image.image_array is not None:
            return self.capture_image.image_array

        raise NotImplementedError("Only np array JobImages are supported right now.")

    @abc.abstractmethod
    def run(self) -> T:
        raise NotImplementedError("You must implement run for your job type.")


@dataclass
class JobResult(Generic[T]):
    job_id: str
    result: Optional[T]
    exception: Optional[Exception]


# Timeout in seconds for acquiring file locks during OME-TIFF writing
FILE_LOCK_TIMEOUT_SECONDS = 10


def _metadata_lock_path(metadata_path: str) -> str:
    return metadata_path + ".lock"


def append_frame_acquisition_time_csv(
    info: "CaptureInfo",
    filename: str,
    *,
    channel: Optional[str] = None,
    channel_index: Optional[int] = None,
) -> None:
    """Append one row to the per-frame acquisition time CSV.

    Layout:
    - ``ZARR_V3``: single ``{acquisition_root}/acquisition_times.csv``
      consolidating all timepoints. The CSV's ``time_point`` column distinguishes
      rows. ZARR_V3 stores its image data in its own per-FOV trees, so the
      per-timepoint folder is otherwise empty in the common case.
    - All other modes: ``{save_directory}/frame_acquisition_times.csv``,
      i.e. one CSV per timepoint folder alongside the TIFFs.

    Records wall-clock time when each frame was committed for saving
    (``CaptureInfo.capture_time``). Safe across multiprocessing save workers
    via :class:`filelock.FileLock`.
    """
    _log = squid.logging.get_logger("append_frame_acquisition_time_csv")
    if info.file_saving_option == _def.FileSavingOption.ZARR_V3 and info.acquisition_root:
        path = os.path.join(info.acquisition_root, "acquisition_times.csv")
    else:
        path = os.path.join(info.save_directory, "frame_acquisition_times.csv")
    lock_path = _metadata_lock_path(path)
    fieldnames = [
        "time_point",
        "region_id",
        "fov",
        "z_level",
        "channel",
        "channel_index",
        "cycle_event_index",
        "state_frame_index",
        "filename",
        "unix_time_s",
        "utc_iso",
    ]
    ch = channel if channel is not None else (info.filename_channel_label or info.observation_state.name)
    cidx = channel_index if channel_index is not None else info.configuration_idx
    row = {
        "time_point": "" if info.time_point is None else info.time_point,
        "region_id": info.region_id,
        "fov": info.fov,
        "z_level": info.z_index,
        "channel": ch,
        "channel_index": cidx,
        # Cycle acquisition-order backbone: where this frame sat in the flat
        # per-position chain and which repeat of its state it was.
        "cycle_event_index": "" if info.cycle_event_index is None else info.cycle_event_index,
        "state_frame_index": "" if info.state_frame_index is None else info.state_frame_index,
        "filename": filename,
        "unix_time_s": f"{float(info.capture_time):.6f}",
        "utc_iso": datetime.fromtimestamp(float(info.capture_time), tz=timezone.utc).isoformat(),
    }
    try:
        with _acquire_file_lock(lock_path, context=path):
            write_header = not os.path.isfile(path) or os.path.getsize(path) == 0
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                if write_header:
                    w.writeheader()
                w.writerow(row)
    except Exception as e:
        _log.warning("Could not append frame acquisition time row to %s: %s", path, e)


@contextmanager
def _acquire_file_lock(lock_path: str, context: str = ""):
    """Acquire a file lock with timeout, providing a clear error message on failure.

    Args:
        lock_path: Path to the lock file.
        context: Optional context string (e.g., output file path) included in error messages.
    """
    lock = FileLock(lock_path, timeout=FILE_LOCK_TIMEOUT_SECONDS)
    try:
        with lock:
            yield
    except FileLockTimeout as exc:
        context_msg = f" (writing to: {context})" if context else ""
        raise TimeoutError(
            f"Failed to acquire file lock '{lock_path}' within {FILE_LOCK_TIMEOUT_SECONDS} seconds{context_msg}. "
            f"Another process may be holding the lock."
        ) from exc


class SaveImageJob(Job):
    _log: ClassVar = squid.logging.get_logger("SaveImageJob")

    def run(self) -> bool:
        from control.core.io_simulation import is_simulation_enabled, simulated_tiff_write

        image = self.image_array()

        # Simulated disk I/O mode - encode to buffer, throttle, discard
        if is_simulation_enabled():
            bytes_written = simulated_tiff_write(image)
            self._log.debug(
                f"SaveImageJob {self.job_id}: simulated write of {bytes_written} bytes " f"(image shape={image.shape})"
            )
            return True

        is_color = len(image.shape) > 2
        return self.save_image(image, self.capture_info, is_color)

    def save_image(self, image: np.array, info: CaptureInfo, is_color: bool):
        # NOTE(imo): We silently fall back to individual image saving here.  We should warn or do something.
        # Prefer the per-acquisition snapshot on the CaptureInfo; fall back to the global only if a job
        # was constructed outside the worker (e.g. tests).
        save_format = info.file_saving_option if info.file_saving_option is not None else _def.FILE_SAVING_OPTION
        if save_format == _def.FileSavingOption.MULTI_PAGE_TIFF:
            _ch_label = info.filename_channel_label or info.observation_state.name
            metadata = {
                "z_level": info.z_index,
                "channel": _ch_label,
                "channel_index": info.configuration_idx,
                "region_id": info.region_id,
                "fov": info.fov,
                "x_mm": info.position.x_mm,
                "y_mm": info.position.y_mm,
                "z_mm": info.position.z_mm,
            }
            # Add requested fields: human-readable time and optional piezo position
            try:
                metadata["time"] = datetime.fromtimestamp(info.capture_time).strftime("%Y-%m-%d %H:%M:%S.%f")
            except Exception:
                metadata["time"] = info.capture_time
            if info.z_piezo_um is not None:
                metadata["z_piezo (um)"] = info.z_piezo_um
            output_path = os.path.join(
                info.save_directory, f"{info.region_id}_{info.fov:0{_def.FILE_ID_PADDING}}_stack.tiff"
            )
            # Ensure channel information is preserved across common TIFF readers by:
            # - embedding full metadata as JSON in ImageDescription (description=)
            # - setting PageName (tag 285) to the channel name via extratags
            description = json.dumps(metadata)
            page_name = str(info.observation_state.name)

            # extratags format: (code, dtype, count, value, writeonce)
            # PageName (285) expects ASCII; dtype 's' denotes a null-terminated string in tifffile
            extratags = [(285, "s", 0, page_name, False)]

            with tifffile.TiffWriter(output_path, append=True) as tiff_writer:
                tiff_writer.write(
                    image,
                    metadata=metadata,
                    description=description,
                    extratags=extratags,
                )
            append_frame_acquisition_time_csv(info, os.path.basename(output_path))
        else:
            # Disambiguate repeated frames of the same state at one position
            # (cycles) by folding the frame suffix into the basename's channel
            # label. Kept out of info.filename_channel_label so the channel
            # identity used by zarr/CSV stays clean.
            _base_label = info.filename_channel_label or info.observation_state.name
            _disambiguated = f"{_base_label}_{info.frame_suffix}" if info.frame_suffix else _base_label
            saved_image = utils_acquisition.save_image(
                image=image,
                file_id=info.file_id,
                save_directory=info.save_directory,
                config=info.observation_state,
                is_color=is_color,
                filename_channel_label=_disambiguated,
            )
            _written = utils_acquisition.get_image_filepath(
                info.save_directory, info.file_id, _disambiguated, image.dtype
            )
            append_frame_acquisition_time_csv(info, os.path.basename(_written))

            if _def.MERGE_CHANNELS:
                # TODO(imo): Add this back in
                raise NotImplementedError("Image merging not supported yet")

        return True


@dataclass
class SaveOMETiffJob(Job):
    """Job for saving images to OME-TIFF format.

    The acquisition_info field is injected by JobRunner.dispatch() before the job runs.
    """

    _log: ClassVar = squid.logging.get_logger("SaveOMETiffJob")
    acquisition_info: Optional[AcquisitionInfo] = field(default=None)

    def run(self) -> bool:
        if self.acquisition_info is None:
            raise ValueError(
                "SaveOMETiffJob.run() requires acquisition_info but it is None. "
                "This job must be dispatched via JobRunner.dispatch(), which injects acquisition_info. "
                "If running directly, set job.acquisition_info before calling run()."
            )

        from control.core.io_simulation import is_simulation_enabled, simulated_ome_tiff_write

        image = self.image_array()

        # Simulated disk I/O mode - encode to buffer, throttle, discard
        if is_simulation_enabled():
            # Build stack key from output path
            ome_folder = ome_tiff_writer.ome_output_folder(self.acquisition_info, self.capture_info)
            base_name = ome_tiff_writer.ome_base_name(self.capture_info)
            stack_key = os.path.join(ome_folder, base_name)

            # Determine 5D shape (T, Z, C, Y, X), preferring cycle self-describing dims
            _t_sim, _c_sim = ome_tiff_writer.ome_plane_indices(self.capture_info)
            shape = (
                ome_tiff_writer._ome_t_size(self.acquisition_info, self.capture_info),
                self.acquisition_info.total_z_levels,
                ome_tiff_writer._ome_c_size(self.acquisition_info, self.capture_info),
                image.shape[0],
                image.shape[1],
            )

            bytes_written = simulated_ome_tiff_write(
                image=image,
                stack_key=stack_key,
                shape=shape,
                time_point=_t_sim,
                z_index=self.capture_info.z_index,
                channel_index=_c_sim,
            )
            self._log.debug(
                f"SaveOMETiffJob {self.job_id}: simulated write of {bytes_written} bytes "
                f"(image shape={image.shape})"
            )
            return True

        self._save_ome_tiff(image, self.capture_info)
        return True

    def _save_ome_tiff(self, image: np.ndarray, info: CaptureInfo) -> None:
        # with reference to Talley's https://github.com/pymmcore-plus/pymmcore-plus/blob/main/src/pymmcore_plus/mda/handlers/_ome_tiff_writer.py and Christoph's https://forum.image.sc/t/how-to-create-an-image-series-ome-tiff-from-python/42730/7
        ome_tiff_writer.validate_capture_info(info, self.acquisition_info, image)

        ome_folder = ome_tiff_writer.ome_output_folder(self.acquisition_info, info)
        ome_tiff_writer.ensure_output_directory(ome_folder)

        base_name = ome_tiff_writer.ome_base_name(info)
        output_path = os.path.join(ome_folder, base_name + ".ome.tiff")
        metadata_path = ome_tiff_writer.metadata_temp_path(self.acquisition_info, info, base_name)
        lock_path = _metadata_lock_path(metadata_path)

        with _acquire_file_lock(lock_path, context=output_path):
            metadata = ome_tiff_writer.load_metadata(metadata_path)
            if metadata is None:
                metadata = ome_tiff_writer.initialize_metadata(self.acquisition_info, info, image)
                target_dtype = np.dtype(metadata[ome_tiff_writer.DTYPE_KEY])
                if os.path.exists(output_path):
                    os.remove(output_path)
                tifffile.imwrite(
                    output_path,
                    shape=tuple(metadata[ome_tiff_writer.SHAPE_KEY]),
                    dtype=target_dtype,
                    metadata=ome_tiff_writer.metadata_for_imwrite(metadata),
                    ome=True,
                )
            else:
                expected_shape = tuple(metadata[ome_tiff_writer.SHAPE_KEY])
                if expected_shape[-2:] != image.shape[-2:]:
                    raise ValueError("Image dimensions do not match existing OME memmap stack")
                # acquisition_info is guaranteed non-None here (validated in run())
                if not metadata.get(ome_tiff_writer.CHANNEL_NAMES_KEY) and self.acquisition_info.channel_names:
                    metadata[ome_tiff_writer.CHANNEL_NAMES_KEY] = self.acquisition_info.channel_names

            target_dtype = np.dtype(metadata[ome_tiff_writer.DTYPE_KEY])
            image_to_store = image if image.dtype == target_dtype else image.astype(target_dtype)

            time_point, channel_index = ome_tiff_writer.ome_plane_indices(info)
            z_index = int(info.z_index)
            shape = tuple(metadata[ome_tiff_writer.SHAPE_KEY])
            if not (0 <= time_point < shape[0]):
                raise ValueError("Time point index out of range for OME stack")
            if not (0 <= z_index < shape[1]):
                raise ValueError("Z index out of range for OME stack")
            if not (0 <= channel_index < shape[2]):
                raise ValueError("Channel index out of range for OME stack")

            stack = tifffile.memmap(output_path, dtype=target_dtype, mode="r+")
            if stack.shape != shape:
                stack.shape = shape
            try:
                stack[time_point, z_index, channel_index, :, :] = image_to_store
                stack.flush()
            finally:
                del stack

            try:
                _rel_ome = os.path.relpath(output_path, info.save_directory)
            except ValueError:
                _rel_ome = os.path.basename(output_path)
            append_frame_acquisition_time_csv(info, _rel_ome.replace("\\", "/"))

            metadata = ome_tiff_writer.update_plane_metadata(metadata, info)
            index_key = f"{time_point}-{channel_index}-{z_index}"
            if index_key not in metadata[ome_tiff_writer.WRITTEN_INDICES_KEY]:
                metadata[ome_tiff_writer.WRITTEN_INDICES_KEY].append(index_key)
                metadata[ome_tiff_writer.SAVED_COUNT_KEY] = len(metadata[ome_tiff_writer.WRITTEN_INDICES_KEY])

            # Check if all images have been saved
            is_complete = metadata[ome_tiff_writer.SAVED_COUNT_KEY] >= metadata[ome_tiff_writer.EXPECTED_COUNT_KEY]
            if is_complete:
                metadata[ome_tiff_writer.COMPLETED_KEY] = True

            # Write metadata (includes completed flag if acquisition is done)
            ome_tiff_writer.write_metadata(metadata_path, metadata)

            if is_complete:
                # Finalize OME-XML and clean up temporary files
                with tifffile.TiffFile(output_path) as tif:
                    current_xml = tif.ome_metadata
                ome_xml = ome_tiff_writer.augment_ome_xml(current_xml, metadata)
                tifffile.tiffcomment(output_path, ome_xml.encode("utf-8"))
                if os.path.exists(metadata_path):
                    os.remove(metadata_path)

        # Clean up lock file after lock is released (only when acquisition completed).
        # Race condition note: Between releasing the lock and this cleanup, another process
        # could theoretically acquire the same lock path. However:
        # 1. We only attempt removal if metadata_path is gone (acquisition completed)
        # 2. If another process holds the lock, os.remove fails with OSError (caught below)
        # 3. This is best-effort cleanup; stale locks are also cleaned by cleanup_stale_metadata_files
        try:
            if not os.path.exists(metadata_path):
                os.remove(lock_path)
        except OSError:
            pass  # Lock held by another process, already removed, or platform-specific issue


@dataclass
class ZarrWriterInfo:
    """Info for Zarr v3 saving, injected by JobRunner.

    Output is always 5D per FOV under OME-NGFF:
    - HCS mode:     {base_path}/plate.ome.zarr/{row}/{col}/{fov}/0
    - Non-HCS:      {base_path}/zarr/{region_id}/fov_{n}.ome.zarr/0

    Attributes:
        base_path: Experiment directory where ``acquisition.yaml`` lives.
        t_size: Total time points.
        c_size: Total channels.
        z_size: Total z levels.
        is_hcs: True for wellplate (HCS) acquisitions.
        region_fov_counts: Map of ``region_id`` -> number of FOVs (used to
            enumerate fields for OME-NGFF well metadata and to derive plate
            row/column layout).
        fov_translations_um: Per-region per-FOV ``(y_um, x_um)`` stage positions
            of the FOV origin, embedded as OME-NGFF ``translation`` transforms.
        pixel_size_um: Physical pixel size in micrometers.
        z_step_um: Z step size in micrometers (optional).
        time_increment_s: Time between timepoints in seconds (optional).
        channel_names: Channel names for the omero metadata block.
        channel_colors: Hex colors per channel (e.g. ``"#FF0000"``).
        channel_wavelengths: Emission wavelengths in nm (None for brightfield).
    """

    base_path: str
    t_size: int
    c_size: int
    z_size: int
    is_hcs: bool = False
    region_fov_counts: Dict[str, int] = field(default_factory=dict)
    fov_translations_um: Dict[str, Dict[int, Tuple[float, float]]] = field(default_factory=dict)
    pixel_size_um: Optional[float] = None
    z_step_um: Optional[float] = None
    time_increment_s: Optional[float] = None
    channel_names: List[str] = field(default_factory=list)
    channel_colors: List[str] = field(default_factory=list)
    channel_wavelengths: List[Optional[int]] = field(default_factory=list)

    def get_output_path(self, region_id: str, fov: int, array_key: Optional[str] = None) -> str:
        """Resolution-0 array path for a given ``(region_id, fov)``.

        ``array_key`` (a channel name) selects a ragged per-state plate/store; None
        is the dense single-array layout.
        """
        return os.path.join(self.get_group_path(region_id, fov, array_key), "0")

    def get_group_path(self, region_id: str, fov: int, array_key: Optional[str] = None) -> str:
        """FOV group directory (parent of the resolution levels)."""
        if self.is_hcs:
            return utils.build_hcs_zarr_fov_path(self.base_path, region_id, fov, array_key)
        return utils.build_per_fov_zarr_path(self.base_path, region_id, fov, array_key)

    def get_fov_count(self, region_id: str) -> int:
        """Number of FOVs in a region (for HCS well fields metadata)."""
        return self.region_fov_counts.get(str(region_id), 1)

    def get_plate_path(self, array_key: Optional[str] = None) -> str:
        """Path to the plate store (HCS mode). ``array_key`` selects a ragged
        per-channel plate (``{array_key}.ome.zarr``)."""
        plate = "plate.ome.zarr" if array_key is None else f"{array_key}.ome.zarr"
        return os.path.join(self.base_path, plate)

    def get_well_path(self, well_id: str, array_key: Optional[str] = None) -> str:
        """Path to a well directory (HCS mode only)."""
        row_letter, col_num = utils.parse_well_id(well_id)
        return os.path.join(self.get_plate_path(array_key), row_letter, col_num)

    def get_hcs_structure(self) -> Tuple[List[str], List[int], List[Tuple[str, int]]]:
        """Return ``(rows, cols, wells)`` for HCS plate metadata."""
        rows_set = set()
        cols_set = set()
        wells = []
        for well_id in self.region_fov_counts.keys():
            row_letter, col_num = utils.parse_well_id(well_id)
            rows_set.add(row_letter)
            cols_set.add(int(col_num))
            wells.append((row_letter, int(col_num)))
        return sorted(rows_set), sorted(cols_set), wells

    def get_fov_translation_um(self, region_id: str, fov: int) -> Tuple[float, float]:
        """Stage position (y_um, x_um) for the FOV; (0, 0) if unknown."""
        region_map = self.fov_translations_um.get(str(region_id), {})
        return region_map.get(int(fov), (0.0, 0.0))

    def get_manifest_path(self, region_id: str, fov: int) -> str:
        """Relative path from the FOV group to the experiment's acquisition.yaml.

        Used for ``_squid.manifest_path`` inside the FOV's zarr.json.
        """
        group_dir = self.get_group_path(region_id, fov)
        manifest_abs = os.path.join(self.base_path, "acquisition.yaml")
        try:
            rel = os.path.relpath(manifest_abs, group_dir)
        except ValueError:
            rel = manifest_abs
        return rel.replace("\\", "/")


@dataclass
class ZarrWriteResult:
    """Result from a SaveZarrJob, containing frame info for viewer notification."""

    fov: int
    time_point: int
    z_index: int
    channel_name: str
    region_idx: int = 0


@dataclass
class SaveZarrJob(Job):
    """Job for saving images to Zarr v3 format using TensorStore.

    Uses a process-local ZarrWriter that is initialized lazily on first write.
    The zarr_writer_info field is injected by JobRunner.dispatch() before the job runs.
    """

    _log: ClassVar = squid.logging.get_logger("SaveZarrJob")
    zarr_writer_info: Optional[ZarrWriterInfo] = field(default=None)

    # Class-level writer storage keyed by output_path.
    # SAFETY: JobRunner runs in a multiprocessing.Process (not threads), so each
    # worker process has its own independent copy of this class variable.
    # WARNING: This dict is NOT thread-safe. DO NOT use SaveZarrJob with threading
    # (e.g., ThreadPoolExecutor) - it will cause race conditions and data corruption.
    _zarr_writers: ClassVar[Dict[str, "ZarrWriter"]] = {}

    # Track HCS metadata that has been written (plate path -> True, well path -> True)
    _hcs_plate_written: ClassVar[Set[str]] = set()
    _hcs_wells_written: ClassVar[Set[str]] = set()

    @classmethod
    def clear_writers(cls) -> None:
        """Clear all zarr writers, aborting any that are still active.

        Call at start of new acquisition to ensure clean state.
        Uses try-finally to guarantee dictionaries are cleared even if abort fails.
        """
        try:
            for writer in list(cls._zarr_writers.values()):
                if writer.is_initialized and not writer.is_finalized:
                    try:
                        writer.abort()
                    except Exception as e:
                        cls._log.warning(f"Error aborting writer during clear: {e}")
        finally:
            # Always clear dictionaries, even if abort loop fails
            cls._zarr_writers.clear()
            cls._hcs_plate_written.clear()
            cls._hcs_wells_written.clear()

    @classmethod
    def finalize_all_writers(cls) -> bool:
        """Finalize all active zarr writers.

        Call at end of acquisition to ensure all data is written.

        Returns:
            True if all writers finalized successfully, False if any failed.
        """
        failed_paths = []
        for path, writer in list(cls._zarr_writers.items()):
            if writer.is_initialized and not writer.is_finalized:
                try:
                    writer.finalize()
                    cls._log.info(f"Finalized zarr writer: {path}")
                except Exception as e:
                    cls._log.error(f"Error finalizing writer {path}: {e}")
                    failed_paths.append(path)
        cls._zarr_writers.clear()
        if failed_paths:
            cls._log.error(f"Failed to finalize {len(failed_paths)} zarr writers: {failed_paths}")
            return False
        return True

    def _write_hcs_metadata_if_needed(self, region_id: str, fov: int, array_key: Optional[str] = None) -> None:
        """Write HCS plate and well metadata if not already written.

        Called when a new writer is initialized for an HCS acquisition.
        Uses class-level sets to track which plate/well metadata has been written.
        In the ragged cycle layout each imaged channel is its own single-channel
        plate (``array_key``), so plate/well metadata is written once per plate.

        Args:
            region_id: Well ID (e.g., "A1", "B12")
            fov: Field of view index
            array_key: Per-channel plate namespace (ragged), or None (dense).
        """
        from control.core.zarr_writer import write_plate_metadata, write_well_metadata

        info = self.zarr_writer_info

        # Write plate metadata (once per plate)
        plate_path = info.get_plate_path(array_key)
        if plate_path not in self._hcs_plate_written:
            rows, cols, wells = info.get_hcs_structure()
            plate_name = "plate" if array_key is None else str(array_key)
            write_plate_metadata(plate_path, rows, cols, wells, plate_name=plate_name)
            self._hcs_plate_written.add(plate_path)
            self._log.info(f"Wrote HCS plate metadata ({plate_name}): {len(wells)} wells")

        # Write well metadata (once per well per plate)
        well_path = info.get_well_path(region_id, array_key)
        if well_path not in self._hcs_wells_written:
            # Get FOV count for this well
            fov_count = info.get_fov_count(region_id)
            fields = list(range(fov_count))
            write_well_metadata(well_path, fields)
            self._hcs_wells_written.add(well_path)
            self._log.debug(f"Wrote HCS well metadata for {region_id}: {fov_count} fields")

    def run(self) -> ZarrWriteResult:
        if self.zarr_writer_info is None:
            raise ValueError(
                "SaveZarrJob.run() requires zarr_writer_info but it is None. "
                "This job must be dispatched via JobRunner.dispatch(), which injects zarr_writer_info. "
                "If running directly, set job.zarr_writer_info before calling run()."
            )

        from control.core.io_simulation import is_simulation_enabled, simulated_zarr_write

        image = self.image_array()
        info = self.capture_info

        region_id = str(info.region_id) if info.region_id is not None else "0"
        fov = info.fov if info.fov is not None else 0
        ak, t, c, t_size, c_size, z_size = self._effective_save_coords(info)
        output_path = self.zarr_writer_info.get_output_path(region_id, fov, ak)

        region_names = list(self.zarr_writer_info.region_fov_counts.keys())
        result = ZarrWriteResult(
            fov=fov,
            time_point=t,
            z_index=info.z_index,
            channel_name=info.observation_state.name,
            region_idx=region_names.index(region_id) if region_id in region_names else 0,
        )

        # Always 5D: (T, C, Z, Y, X) per FOV (dense) or per (FOV, channel) (ragged).
        # Z is per-array (1 for a reference-z-only state, NZ for full-z).
        shape = (
            t_size,
            c_size,
            z_size,
            image.shape[0],
            image.shape[1],
        )

        if is_simulation_enabled():
            bytes_written = simulated_zarr_write(
                image=image,
                stack_key=output_path,
                shape=shape,
                time_point=t,
                z_index=info.z_index,
                channel_index=c,
            )
            self._log.debug(
                f"SaveZarrJob {self.job_id}: simulated write of {bytes_written} bytes "
                f"to {output_path} (image shape={image.shape})"
            )
            return result

        self._save_zarr(image, info, output_path)
        try:
            _rel_z = os.path.relpath(output_path, info.save_directory)
        except ValueError:
            _rel_z = output_path
        append_frame_acquisition_time_csv(info, _rel_z.replace("\\", "/"))
        return result

    def _effective_save_coords(self, info: CaptureInfo):
        """Resolve (array_key, t, c, t_size, c_size, z_size) for a frame.

        Uses the self-describing ``save_*`` fields when present (cycle layout),
        else falls back to today's global ``(time_point, configuration_idx)`` and
        the uniform ``zarr_writer_info`` dims. ``z_size`` is per-array so a
        reference-z-only state can be single-z alongside full-z states.
        """
        zwi = self.zarr_writer_info
        ak = info.array_key
        t = info.save_t_index if info.save_t_index is not None else (info.time_point or 0)
        c = info.save_c_index if info.save_c_index is not None else info.configuration_idx
        t_size = info.save_t_size if info.save_t_size is not None else zwi.t_size
        c_size = info.save_c_size if info.save_c_size is not None else zwi.c_size
        z_size = info.save_z_size if info.save_z_size is not None else zwi.z_size
        return ak, t, c, t_size, c_size, z_size

    def _save_zarr(self, image: np.ndarray, info: CaptureInfo, output_path: str) -> None:
        """Write one plane to the per-FOV zarr (level 0 + pyramid via ZarrWriter)."""
        from control.core.zarr_writer import ZarrWriter, ZarrAcquisitionConfig
        from control import _def

        region_id = str(info.region_id) if info.region_id is not None else "0"
        fov = info.fov if info.fov is not None else 0
        ak, t, c, t_size, c_size, z_size = self._effective_save_coords(info)
        writer_key = output_path  # One writer per (FOV[, channel/z-mode] for ragged)

        zwi = self.zarr_writer_info
        if writer_key not in self._zarr_writers:
            shape = (
                t_size,
                c_size,
                z_size,
                image.shape[0],
                image.shape[1],
            )
            translation_um = zwi.get_fov_translation_um(region_id, fov)
            manifest_path = zwi.get_manifest_path(region_id, fov)
            # Per-array channel metadata (ragged = single channel) falls back to global.
            channel_names = info.array_channel_names if info.array_channel_names is not None else zwi.channel_names
            channel_colors = info.array_channel_colors if info.array_channel_colors is not None else zwi.channel_colors
            channel_wavelengths = (
                info.array_channel_wavelengths if info.array_channel_wavelengths is not None else zwi.channel_wavelengths
            )

            config = ZarrAcquisitionConfig(
                output_path=output_path,
                shape=shape,
                dtype=image.dtype,
                pixel_size_um=zwi.pixel_size_um or 1.0,
                z_step_um=zwi.z_step_um,
                time_increment_s=zwi.time_increment_s,
                channel_names=channel_names,
                channel_colors=channel_colors,
                channel_wavelengths=channel_wavelengths,
                compression=_def.ZARR_COMPRESSION,
                translation_um=translation_um,
                manifest_path=manifest_path,
                shard_per_z=_def.ZARR_SHARD_PER_Z,
            )
            try:
                writer = ZarrWriter(config)
                writer.initialize()
            except Exception as e:
                self._log.error(f"Failed to initialize zarr writer for {output_path}: {e}")
                raise
            self._zarr_writers[writer_key] = writer
            if zwi.is_hcs:
                self._write_hcs_metadata_if_needed(region_id, fov, ak)
            mode_str = "HCS" if zwi.is_hcs else "per-FOV"
            self._log.info(f"Initialized zarr writer ({mode_str}): {output_path}")

        writer = self._zarr_writers[writer_key]
        z = info.z_index
        channel_name = info.filename_channel_label or info.observation_state.name
        writer.write_frame(image, t=t, c=c, z=z)
        writer.record_frame_time(t=t, c=c, z=z, unix_time_s=info.capture_time, channel_name=channel_name)
        self._log.debug(f"Wrote frame t={t}, c={c}, z={z} to {output_path} (array_key={ak})")


@dataclass
class BarrierResult:
    """Return value of ``FlushAndStageUploadJob`` consumed by ``multi_point_worker``.

    Tells the main process: "I have flushed FOV ``fov`` for timepoint ``t`` to
    disk and enqueued ``file_count`` files onto the upload worker. Track
    ``task_id`` until the matching ``UploadResult`` arrives, then it is safe
    to batched-delete this FOV's local shard."
    """

    task_id: str
    time_point: int
    region_id: str
    fov: int
    file_count: int
    submitted: bool  # False means upload disabled or writer missing — no UploadResult will ever arrive


@dataclass
class FlushAndStageUploadJob:
    """Barrier job: flush one ``(t, fov)``'s zarr writes, then stage upload.

    Runs in the ``JobRunner`` subprocess. Because the JobRunner pulls jobs
    FIFO from a single queue, by the time this job runs every preceding
    ``SaveZarrJob`` for ``(t, fov)`` has already been processed. We then call
    ``writer.wait_for_pending()`` on the per-FOV ``ZarrWriter`` so all
    outstanding TensorStore futures resolve, collect the resulting shard
    paths via :meth:`ZarrWriter.drain_unstaged_shard_paths`, build local→remote
    pairs, and push one :class:`UploadTask` onto the ``UploadWorker``'s input
    queue. Network I/O happens in the upload worker, not here.

    NOTE: this class does NOT inherit from :class:`Job` because the parent
    requires a real ``CaptureInfo``/``JobImage`` per-frame. The
    ``JobRunner`` machinery only needs ``job_id``, ``run()``, and a
    ``capture_image`` with ``image_array`` (for the backpressure accountant)
    — all of which are provided here directly.
    """

    _log: ClassVar = squid.logging.get_logger("FlushAndStageUploadJob")

    time_point: int = 0
    region_id: str = ""
    fov: int = 0
    output_path: str = ""
    # Injected by ``JobRunner.dispatch`` from its ``self._upload_target``.
    upload_target: Optional[UploadTarget] = field(default=None)

    job_id: str = field(default_factory=lambda: str(uuid4()))
    # Empty JobImage keeps the backpressure byte accountant happy without
    # actually moving image data through the queue.
    capture_image: JobImage = field(default_factory=lambda: JobImage(image_array=None))

    # Set in each ``JobRunner`` subprocess by ``JobRunner.run()`` so the job
    # can reach the upload worker without pickling the queue per-job.
    _upload_input_queue: ClassVar[Optional[multiprocessing.Queue]] = None

    def run(self) -> BarrierResult:
        from uuid import uuid4

        task_id = str(uuid4())

        writer = SaveZarrJob._zarr_writers.get(self.output_path)
        if writer is None:
            # Either no writes happened for this FOV (test scaffold), or the
            # writer was cleared by an abort. Either way there is nothing to
            # upload — return submitted=False so the main worker doesn't
            # wait on a phantom UploadResult.
            self._log.warning(
                f"No writer for output_path={self.output_path}; "
                f"skipping upload for t={self.time_point} fov={self.fov}"
            )
            return BarrierResult(
                task_id=task_id,
                time_point=self.time_point,
                region_id=self.region_id,
                fov=self.fov,
                file_count=0,
                submitted=False,
            )

        # Drain every outstanding TensorStore future tied to this writer. After
        # this returns, every shard file for this FOV is fully on disk and
        # safe to read for upload.
        writer.wait_for_pending()

        if not (self.upload_target and self.upload_target.enabled):
            return BarrierResult(
                task_id=task_id,
                time_point=self.time_point,
                region_id=self.region_id,
                fov=self.fov,
                file_count=0,
                submitted=False,
            )

        shard_paths = writer.drain_unstaged_shard_paths()
        metadata_paths = writer.metadata_paths()
        if not shard_paths and not metadata_paths:
            self._log.warning(
                f"No shard or metadata paths found for t={self.time_point} "
                f"fov={self.fov} at {self.output_path}"
            )
            return BarrierResult(
                task_id=task_id,
                time_point=self.time_point,
                region_id=self.region_id,
                fov=self.fov,
                file_count=0,
                submitted=False,
            )

        # Compose ``files`` with metadata first (so the remote tree becomes
        # readable as soon as the shards land) and mark only shards as
        # deletable: metadata files are shared across timepoints and the
        # writer continues to update ``frame_times`` and (at finalize)
        # ``zarr.json``. Deleting them locally would break the live writer.
        #
        # Metadata files are also marked ``stable_read`` so the UploadWorker
        # re-hashes the source after the copy and retries if it detects a
        # concurrent writer rewrite (``record_frame_time`` hits
        # ``frame_times/c/0/0/0`` every frame; finalize rewrites the
        # per-FOV ``zarr.json``).
        all_locals = list(metadata_paths) + list(shard_paths)
        files = [
            (
                local,
                local_to_remote_path(
                    local,
                    self.upload_target.local_base,
                    self.upload_target.remote_root,
                ),
            )
            for local in all_locals
        ]
        deletable = set(shard_paths)
        stable_read = set(metadata_paths)

        if FlushAndStageUploadJob._upload_input_queue is None:
            self._log.error(
                "Upload enabled but UploadWorker queue not initialized in this "
                "JobRunner subprocess — dropping task. Check JobRunner.run()."
            )
            return BarrierResult(
                task_id=task_id,
                time_point=self.time_point,
                region_id=self.region_id,
                fov=self.fov,
                file_count=len(files),
                submitted=False,
            )

        task = UploadTask(
            task_id=task_id,
            time_point=self.time_point,
            region_id=self.region_id,
            fov=self.fov,
            files=files,
            deletable_local_paths=deletable,
            stable_read_paths=stable_read,
        )
        FlushAndStageUploadJob._upload_input_queue.put(task)
        self._log.debug(
            f"Staged upload task {task_id} for t={self.time_point} fov={self.fov} "
            f"({len(files)} files)"
        )
        return BarrierResult(
            task_id=task_id,
            time_point=self.time_point,
            region_id=self.region_id,
            fov=self.fov,
            file_count=len(files),
            submitted=True,
        )


@dataclass
class _PPSpecView:
    """Minimal spec object for ``registry.load_routine`` in the subprocess
    (reconstructed from the pickled ``spec_dict``)."""

    routine: str
    script_path: Optional[str]
    params: Dict


def postprocess_routine_key(spec_dict: dict) -> str:
    """Stable identity key for a routine (routine + script path + params).

    Used to key the process-local routine instance + its persistent cache so
    (a) warmup and per-FOV compute share one cache, (b) two groups/regions with
    an identical routine+params share the (expensive) transfer function, and
    (c) different routines under the same ``group_key`` don't collide."""
    payload = {
        "routine": spec_dict.get("routine"),
        "script_path": spec_dict.get("script_path"),
        "params": spec_dict.get("params", {}),
    }
    import hashlib

    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


@dataclass
class PostprocessWarmupResult:
    """Result of a pre-acquisition routine warmup (see ``PostprocessWarmupJob``)."""

    routine_key: str
    label: str
    ok: bool
    error: Optional[str] = None


@dataclass
class PostprocessResult:
    """Result of a completed postprocess group invocation for one FOV visit.

    Carries the derived-plate upload barriers (so the main worker's upload
    accounting is identical to the raw-plate path) and small display previews
    (so the main process can show each output live). ``error`` is set (and the
    numeric fields zeroed) when the routine or a write failed.
    """

    group_key: str
    time_point: int
    region_id: str
    fov: int
    outputs_written: int
    barrier_results: List[BarrierResult] = field(default_factory=list)
    display_images: Dict[str, np.ndarray] = field(default_factory=dict)  # output_key -> small preview
    # The last input frame's CaptureInfo — lets the main process synthesize a
    # CameraFrame/CaptureInfo pair for the live-display signal (position, region…).
    source_capture_info: Optional[CaptureInfo] = None
    error: Optional[str] = None


@dataclass
class PostprocessJob(Job):
    """Accumulate one postprocess group's frames per FOV visit, then compute.

    Frames of postprocessed cycle events are routed here (not to the save jobs),
    so their raw images are never written. This job accumulates them in a
    process-local ClassVar (the ``DownsampledViewJob`` pattern), and on the last
    expected frame runs the routine, writes each declared output as its own
    single-channel plate via an inline ``SaveZarrJob`` (ZARR_V3) or a direct
    float-safe TIFF write (INDIVIDUAL_IMAGES), and — for ZARR_V3 with upload
    enabled — runs the derived-plate upload barrier inline (so it deterministically
    follows the writes in this same subprocess, avoiding the cross-thread
    ordering race the worker-dispatched barrier would have).

    WARNING: like ``SaveZarrJob`` / ``DownsampledViewJob``, the ClassVars are
    process-local and only safe because each ``JobRunner`` is its own process.
    """

    _log: ClassVar = squid.logging.get_logger("PostprocessJob")

    group_key: str = ""
    label: str = ""
    spec_dict: Dict = field(default_factory=dict)
    expected_frames: int = 1
    output_specs: List[Dict] = field(default_factory=list)  # {name,z_size,dtype,channel_color,wavelength_nm}
    input_state_specs: Dict[str, Dict] = field(default_factory=dict)  # state -> {acquire_z_stack, frames_per_visit}
    ctx_meta: Dict = field(default_factory=dict)  # pixel_size_um, dz_um, nz, nt, state_meta
    # Injected by JobRunner.dispatch (like SaveZarrJob / FlushAndStageUploadJob).
    zarr_writer_info: Optional[ZarrWriterInfo] = field(default=None)
    acquisition_info: Optional[AcquisitionInfo] = field(default=None)
    upload_target: Optional[UploadTarget] = field(default=None)

    # ── process-local state ──
    _accumulators: ClassVar[Dict[tuple, dict]] = {}
    _routines: ClassVar[Dict[str, object]] = {}          # group_key -> routine instance
    _routine_caches: ClassVar[Dict[str, dict]] = {}      # group_key -> ctx.cache dict (TF cache lives here)
    # Installed by JobRunner.run() so held accumulator bytes stay counted for backpressure.
    _bp_pending_bytes: ClassVar[Optional[multiprocessing.Value]] = None
    _bp_capacity_event: ClassVar[Optional[multiprocessing.Event]] = None

    @classmethod
    def clear_accumulators(cls) -> None:
        cls._accumulators.clear()
        cls._routines.clear()
        cls._routine_caches.clear()

    def _acc_key(self) -> tuple:
        info = self.capture_info
        return (info.time_point, str(info.region_id), info.fov, self.group_key)

    def run(self) -> Optional[PostprocessResult]:
        info = self.capture_info
        image = self.image_array()

        # Ground-truth timestamp for the (unsaved) raw frame.
        try:
            append_frame_acquisition_time_csv(info, f"postprocess/{self.group_key}")
        except Exception as e:
            self._log.debug(f"Could not record postprocess input frame time: {e}")

        key = self._acc_key()
        acc = self._accumulators.get(key)
        if acc is None:
            acc = {"frames": [], "held_bytes": 0}
            self._accumulators[key] = acc
        acc["frames"].append(
            {
                "state": info.observation_state.name,
                "state_frame_index": info.state_frame_index or 0,
                "z_index": info.z_index,
                "image": image,
                "capture_time": info.capture_time,
            }
        )
        # Hold the frame's bytes against backpressure: the JobRunner's finally
        # block will subtract this job's image bytes, so re-adding here keeps the
        # stored frame counted until the group completes.
        if self._bp_pending_bytes is not None and image is not None:
            with self._bp_pending_bytes.get_lock():
                self._bp_pending_bytes.value += image.nbytes
            acc["held_bytes"] += image.nbytes

        if len(acc["frames"]) < self.expected_frames:
            return None

        # Group complete — release held bytes and compute.
        self._accumulators.pop(key, None)
        if self._bp_pending_bytes is not None and acc["held_bytes"]:
            with self._bp_pending_bytes.get_lock():
                self._bp_pending_bytes.value = max(0, self._bp_pending_bytes.value - acc["held_bytes"])
            if self._bp_capacity_event is not None:
                self._bp_capacity_event.set()

        t_scan = info.time_point or 0
        try:
            outputs = self._compute(acc["frames"])
        except Exception as e:
            self._log.exception(f"Postprocess group {self.group_key} failed for t={t_scan} fov={info.fov}")
            return PostprocessResult(
                group_key=self.group_key,
                time_point=t_scan,
                region_id=str(info.region_id),
                fov=info.fov,
                outputs_written=0,
                error=str(e),
            )

        barrier_results: List[BarrierResult] = []
        display_images: Dict[str, np.ndarray] = {}
        written = 0
        last_capture_time = acc["frames"][-1]["capture_time"]
        for spec in self.output_specs:
            out_key = f"{self.label}_{spec['name']}"
            arr = outputs.get(spec["name"])
            if arr is None:
                self._log.error(f"Routine {self.group_key} did not return declared output {spec['name']!r}")
                continue
            arr = self._coerce_output(arr, spec, acc["frames"])
            written += self._write_output(arr, out_key, spec, t_scan, last_capture_time)
            barrier = self._maybe_barrier(out_key)
            if barrier is not None:
                barrier_results.append(barrier)
            display_images[out_key] = self._prepare_display_image(arr)

        return PostprocessResult(
            group_key=self.group_key,
            time_point=t_scan,
            region_id=str(info.region_id),
            fov=info.fov,
            outputs_written=written,
            barrier_results=barrier_results,
            display_images=display_images,
            source_capture_info=info,
        )

    @classmethod
    def ensure_routine(cls, spec_dict: dict):
        """Load (once per process) the routine + its persistent cache, keyed by
        routine identity (routine/script/params) — so warmup and every FOV of
        every region that share the same routine also share one instance and its
        TF cache. Returns ``(routine, cache_dict)``."""
        from control.postprocessing.registry import load_routine

        key = postprocess_routine_key(spec_dict)
        routine = cls._routines.get(key)
        if routine is None:
            routine = load_routine(
                _PPSpecView(
                    routine=spec_dict.get("routine"),
                    script_path=spec_dict.get("script_path"),
                    params=spec_dict.get("params", {}),
                )
            )
            cls._routines[key] = routine
            cls._routine_caches[key] = {}
        return routine, cls._routine_caches[key]

    @staticmethod
    def build_context(ctx_meta: dict, cache: dict, logger) -> "PostprocessContext":
        from control.postprocessing.base import PostprocessContext

        return PostprocessContext(
            cache=cache,
            logger=logger,
            pixel_size_um=ctx_meta.get("pixel_size_um"),
            dz_um=ctx_meta.get("dz_um"),
            nz=int(ctx_meta.get("nz", 1)),
            nt=int(ctx_meta.get("nt", 1)),
            z_positions_um=ctx_meta.get("z_positions_um"),
            state_meta=ctx_meta.get("state_meta", {}),
            yx_shape=tuple(ctx_meta["yx_shape"]) if ctx_meta.get("yx_shape") else None,
        )

    def _compute(self, frames: List[dict]) -> Dict[str, np.ndarray]:
        routine, cache = self.ensure_routine(self.spec_dict)

        # Assemble inputs[state] = (F, Z, Y, X), ordered by (state_frame_index, z_index).
        inputs: Dict[str, np.ndarray] = {}
        for state, sspec in self.input_state_specs.items():
            f = int(sspec["frames_per_visit"])
            z = int(self.ctx_meta["nz"]) if sspec["acquire_z_stack"] else 1
            state_frames = sorted(
                (fr for fr in frames if fr["state"] == state),
                key=lambda fr: (fr["state_frame_index"], fr["z_index"]),
            )
            if len(state_frames) != f * z:
                raise ValueError(
                    f"postprocess group {self.group_key}: expected {f * z} frames of {state!r} "
                    f"(F={f} × Z={z}), got {len(state_frames)}"
                )
            stack = np.stack([fr["image"] for fr in state_frames], axis=0)
            inputs[state] = stack.reshape((f, z) + stack.shape[1:])

        ctx = self.build_context(self.ctx_meta, cache, self._log)
        return routine.process(inputs, ctx, dict(self.spec_dict.get("params", {})))

    def _coerce_output(self, arr: np.ndarray, spec: dict, frames: List[dict]) -> np.ndarray:
        from control.postprocessing.base import DTYPE_INPUT

        arr = np.asarray(arr)
        if arr.ndim == 2:
            arr = arr[np.newaxis, ...]  # (Y,X) -> (1,Y,X)
        if arr.shape[0] != spec["z_size"]:
            raise ValueError(
                f"output {spec['name']!r} declared z_size={spec['z_size']} but returned {arr.shape[0]} planes"
            )
        dtype = spec["dtype"]
        if dtype == DTYPE_INPUT:
            dtype = frames[0]["image"].dtype
        return arr.astype(dtype, copy=False)

    def _write_output(self, arr: np.ndarray, out_key: str, spec: dict, t_scan: int, capture_time: float) -> int:
        info = self.capture_info
        save_format = info.file_saving_option if info.file_saving_option is not None else _def.FILE_SAVING_OPTION
        z_size = int(spec["z_size"])
        nt = int(self.ctx_meta.get("nt", 1))
        wavelength = spec.get("wavelength_nm")
        color = spec.get("channel_color", "#FFFFFF")

        if save_format == _def.FileSavingOption.ZARR_V3:
            for zi in range(z_size):
                ci = CaptureInfo(
                    position=info.position,
                    z_index=zi,
                    capture_time=capture_time,
                    observation_state=info.observation_state,
                    save_directory=info.save_directory,
                    file_id=info.file_id,
                    region_id=info.region_id,
                    fov=info.fov,
                    configuration_idx=0,
                    time_point=info.time_point,
                    filename_channel_label=out_key,
                    array_key=out_key,
                    save_t_index=t_scan,
                    save_c_index=0,
                    save_t_size=nt,
                    save_c_size=1,
                    save_z_size=z_size,
                    array_channel_names=[out_key],
                    array_channel_colors=[color],
                    array_channel_wavelengths=[wavelength],
                    file_saving_option=save_format,
                    acquisition_root=info.acquisition_root,
                )
                sub = SaveZarrJob(
                    capture_info=ci,
                    capture_image=JobImage(image_array=arr[zi]),
                    zarr_writer_info=self.zarr_writer_info,
                )
                sub.run()
            return 1

        # INDIVIDUAL_IMAGES: float-safe direct TIFF write (avoids save_image's
        # uint/pseudo-color handling). One file per output (+ z suffix if 3D).
        from control.core.io_simulation import is_simulation_enabled

        if is_simulation_enabled():
            return 1
        pad = _def.FILE_ID_PADDING
        for zi in range(z_size):
            zsuffix = "" if z_size == 1 else f"_z{zi:03d}"
            fname = f"{info.region_id}_{info.fov:0{pad}}_{out_key}{zsuffix}.tiff"
            out_path = os.path.join(info.save_directory, fname)
            try:
                tifffile.imwrite(out_path, arr[zi])
                append_frame_acquisition_time_csv(info, fname, channel=out_key, channel_index=0)
            except Exception as e:
                self._log.error(f"Failed to write postprocess output {out_path}: {e}")
        return 1

    def _maybe_barrier(self, out_key: str) -> Optional[BarrierResult]:
        info = self.capture_info
        save_format = info.file_saving_option if info.file_saving_option is not None else _def.FILE_SAVING_OPTION
        if save_format != _def.FileSavingOption.ZARR_V3:
            return None
        if not (self.upload_target and self.upload_target.enabled):
            return None
        output_path = self.zarr_writer_info.get_output_path(str(info.region_id), info.fov, out_key)
        barrier = FlushAndStageUploadJob(
            time_point=info.time_point or 0,
            region_id=str(info.region_id),
            fov=info.fov,
            output_path=output_path,
            upload_target=self.upload_target,
        )
        return barrier.run()

    def _prepare_display_image(self, arr: np.ndarray) -> np.ndarray:
        """Produce a display-safe preview plane for the live viewer.

        The live napari/contrast display assumes a single integer dtype and one
        image size shared across all channels (it re-inits the whole canvas when
        either changes). So a float output must be normalized to the raw camera
        dtype (uint16) and kept at its native resolution (which, for image-in →
        image-out routines, matches the camera frame) — otherwise it thrashes the
        viewer every frame. Integer outputs pass through unchanged.
        """
        plane = arr[arr.shape[0] // 2] if arr.ndim == 3 else arr
        if np.issubdtype(plane.dtype, np.integer):
            return np.ascontiguousarray(plane.astype(np.uint16, copy=False))
        finite = plane[np.isfinite(plane)]
        lo = float(finite.min()) if finite.size else 0.0
        hi = float(finite.max()) if finite.size else 1.0
        if hi <= lo:
            return np.zeros(plane.shape, dtype=np.uint16)
        scaled = (np.clip(plane, lo, hi) - lo) / (hi - lo) * 65535.0
        return np.ascontiguousarray(scaled.astype(np.uint16))


@dataclass
class PostprocessWarmupJob:
    """Precompute a routine's FOV-shared state before the first hardware trigger.

    Runs in the PostprocessJob runner subprocess (so it populates the same
    process-local routine cache the per-FOV ``PostprocessJob``s read). Calls
    ``routine.warmup(...)`` with the run geometry (incl. camera ``yx_shape``), so
    e.g. waveorder's transfer function is factorized up front and the first FOV's
    reconstruction is a cache hit instead of a multi-second stall.

    Like ``FlushAndStageUploadJob`` this is not a frame ``Job``: the ``JobRunner``
    only needs ``job_id``, ``run()``, and an (empty) ``capture_image``.
    """

    _log: ClassVar = squid.logging.get_logger("PostprocessWarmupJob")

    label: str = ""
    spec_dict: Dict = field(default_factory=dict)
    input_state_specs: Dict[str, Dict] = field(default_factory=dict)
    ctx_meta: Dict = field(default_factory=dict)

    job_id: str = field(default_factory=lambda: str(uuid4()))
    capture_image: JobImage = field(default_factory=lambda: JobImage(image_array=None))

    def run(self) -> PostprocessWarmupResult:
        from control.postprocessing.base import InputStateSpec

        key = postprocess_routine_key(self.spec_dict)
        try:
            routine, cache = PostprocessJob.ensure_routine(self.spec_dict)
            ctx = PostprocessJob.build_context(self.ctx_meta, cache, self._log)
            input_states = {
                name: InputStateSpec(
                    state=name,
                    acquire_z_stack=bool(s["acquire_z_stack"]),
                    frames_per_visit=int(s["frames_per_visit"]),
                )
                for name, s in self.input_state_specs.items()
            }
            routine.warmup(input_states, ctx, dict(self.spec_dict.get("params", {})))
            return PostprocessWarmupResult(routine_key=key, label=self.label, ok=True)
        except Exception as e:
            # Non-fatal: fall back to lazy compute on the first FOV.
            self._log.warning(f"Postprocess warmup for {self.label!r} failed (will compute lazily): {e}")
            return PostprocessWarmupResult(routine_key=key, label=self.label, ok=False, error=str(e))


# These are debugging jobs - they should not be used in normal usage!
class HangForeverJob(Job):
    def run(self) -> bool:
        while True:
            time.sleep(1)

        return True  # noqa


class ThrowImmediatelyJobException(RuntimeError):
    pass


class ThrowImmediatelyJob(Job):
    def run(self) -> bool:
        raise ThrowImmediatelyJobException("ThrowImmediatelyJob threw")


@dataclass
class DownsampledViewResult:
    """Result from DownsampledViewJob containing well images for plate view update."""

    well_id: str
    well_row: int
    well_col: int
    well_images: Dict[int, np.ndarray]  # channel_idx -> downsampled image
    channel_names: List[str]


@dataclass
class DownsampledViewJob(Job):
    """Job to generate downsampled well images and contribute to plate view.

    This job:
    1. Crops overlap from the tile
    2. Accumulates tiles for the well (using class-level storage per process)
    3. When all FOVs for all channels are received, stitches and saves as multipage TIFF
    4. Returns the first channel 10um image via queue for plate view update in main process

    Warning:
        This class uses a mutable class-level accumulator (_well_accumulators) that is
        only safe because each JobRunner runs in its own *process* (via multiprocessing).
        Each worker has its own independent copy of this attribute.

        Do NOT use DownsampledViewJob in a threading context (e.g., with
        ThreadPoolExecutor or other in-process thread runners) without adding
        proper synchronization or refactoring to avoid shared mutable class
        state, as that would lead to race conditions and data corruption.
    """

    # All fields must have defaults because parent class Job has job_id with default
    well_id: str = ""
    well_row: int = 0
    well_col: int = 0
    fov_index: int = 0
    total_fovs_in_well: int = 1
    channel_idx: int = 0
    total_channels: int = 1
    channel_name: str = ""
    fov_position_in_well: Tuple[float, float] = (0.0, 0.0)  # (x_mm, y_mm) relative to well origin
    overlap_pixels: Tuple[int, int, int, int] = field(default=(0, 0, 0, 0))  # (top, bottom, left, right)
    pixel_size_um: float = 1.0
    target_resolutions_um: List[float] = field(default_factory=lambda: [5.0, 10.0, 20.0])
    plate_resolution_um: float = 10.0
    output_dir: str = ""
    channel_names: List[str] = field(default_factory=list)
    z_index: int = 0
    total_z_levels: int = 1
    z_projection_mode: Union[ZProjectionMode, str] = ZProjectionMode.MIP
    interpolation_method: Union[DownsamplingMethod, str] = DownsamplingMethod.INTER_AREA_FAST
    skip_saving: bool = False  # Skip TIFF file saving (just generate for display)

    # Class-level accumulator storage keyed by well_id.
    # Note: This runs inside JobRunner (a multiprocessing.Process), so each worker
    # process has its own copy of this class variable. It is process-local and
    # safe to mutate without cross-process synchronization.
    _well_accumulators: ClassVar[Dict[str, WellTileAccumulator]] = {}
    # Track wells that encountered errors during processing
    _failed_wells: ClassVar[Dict[str, str]] = {}  # well_id -> error message

    @classmethod
    def clear_accumulators(cls) -> None:
        """Clear all accumulated well data and error tracking.

        Call this at the start of a new acquisition to ensure no stale state
        from previous (potentially aborted) acquisitions remains.

        This method is safe to call even if no accumulators exist.
        Performance: O(1) - just clears the dictionaries.
        """
        cls._well_accumulators.clear()
        cls._failed_wells.clear()

    @classmethod
    def get_accumulator_count(cls) -> int:
        """Get the number of wells currently being accumulated.

        Useful for monitoring memory pressure during acquisition.
        """
        return len(cls._well_accumulators)

    @classmethod
    def get_failed_wells(cls) -> Dict[str, str]:
        """Get a copy of the failed wells dictionary.

        Returns:
            Dict mapping well_id to error message for wells that failed processing.
        """
        return cls._failed_wells.copy()

    def run(self) -> Optional[DownsampledViewResult]:
        log = squid.logging.get_logger(self.__class__.__name__)

        t_start = time.perf_counter()

        # Get image array (may involve unpickling)
        tile = self.image_array()
        t_get_image = time.perf_counter()

        # Crop overlap from tile
        cropped = crop_overlap(tile, self.overlap_pixels)

        t_crop = time.perf_counter()

        # Get or create accumulator for this well
        if self.well_id not in self._well_accumulators:
            self._well_accumulators[self.well_id] = WellTileAccumulator(
                well_id=self.well_id,
                total_fovs=self.total_fovs_in_well,
                total_channels=self.total_channels,
                pixel_size_um=self.pixel_size_um,
                channel_names=self.channel_names if self.channel_names else None,
                total_z_levels=self.total_z_levels,
                z_projection_mode=self.z_projection_mode,
            )

        accumulator = self._well_accumulators[self.well_id]
        accumulator.add_tile(
            cropped,
            self.fov_position_in_well,
            self.channel_idx,
            fov_idx=self.fov_index,
            z_index=self.z_index,
        )

        t_accumulate = time.perf_counter()

        # If not all FOVs for all channels received yet, return None
        if not accumulator.is_complete():
            t_intermediate = time.perf_counter()
            z_info = f" z {self.z_index + 1}/{self.total_z_levels}" if self.total_z_levels > 1 else ""
            log.debug(
                f"Well {self.well_id}: channel {self.channel_idx} FOV {self.fov_index + 1}/{self.total_fovs_in_well}{z_info}, "
                f"channels: {accumulator.get_channel_count()}/{self.total_channels} | "
                f"tile={tile.shape}, get_img={t_get_image - t_start:.3f}s, crop={t_crop - t_get_image:.3f}s, "
                f"accum={t_accumulate - t_crop:.3f}s, total={t_intermediate - t_start:.3f}s"
            )
            return None

        # All FOVs for all channels (and z-levels for MIP) received - stitch and save
        z_info = f" x {self.total_z_levels} z-levels ({self.z_projection_mode})" if self.total_z_levels > 1 else ""
        log.info(
            f"Well {self.well_id}: all {self.total_fovs_in_well} FOVs x {self.total_channels} channels{z_info} received, stitching..."
        )

        try:
            t_stitch_start = time.perf_counter()

            # Memory tracking: stitching is memory-intensive
            set_worker_operation(f"STITCH_{self.well_id}")

            # Stitch all channels
            stitched_channels = accumulator.stitch_all_channels()

            t_stitch_end = time.perf_counter()

            # Get channel names for metadata
            channel_names = accumulator.channel_names

            # Convert interpolation_method to enum if string
            interp_method = (
                DownsamplingMethod.convert_to_enum(self.interpolation_method)
                if isinstance(self.interpolation_method, str)
                else self.interpolation_method
            )

            # Memory tracking: downsampling phase
            set_worker_operation(f"DOWNSAMPLE_{self.well_id}")

            # Generate plate view images first (at plate resolution only)
            t_downsample_plate_start = time.perf_counter()
            well_images_for_plate: Dict[int, np.ndarray] = {}
            for ch_idx in sorted(stitched_channels.keys()):
                downsampled = downsample_tile(
                    stitched_channels[ch_idx], self.pixel_size_um, self.plate_resolution_um, interp_method
                )
                well_images_for_plate[ch_idx] = downsampled
            t_downsample_plate_end = time.perf_counter()

            # Memory tracking: save phase
            set_worker_operation(f"SAVE_{self.well_id}")

            # Save TIFFs only if not skipping
            t_save_start = time.perf_counter()
            if not self.skip_saving:
                wells_dir = os.path.join(self.output_dir, "wells")
                os.makedirs(wells_dir, exist_ok=True)

                # Downsample each channel to all target resolutions
                # downsample_to_resolutions handles cascading for INTER_AREA
                # Initialize resolution stacks before the loop to avoid UnboundLocalError if stitched_channels is empty
                resolution_stacks: Dict[float, List[np.ndarray]] = {r: [] for r in self.target_resolutions_um}
                for ch_idx in sorted(stitched_channels.keys()):
                    # Get all resolutions for this channel (may include plate_resolution)
                    resolutions_to_compute = [r for r in self.target_resolutions_um if r != self.plate_resolution_um]
                    downsampled_images = downsample_to_resolutions(
                        stitched_channels[ch_idx], self.pixel_size_um, resolutions_to_compute, interp_method
                    )
                    # Add already-computed plate resolution
                    downsampled_images[self.plate_resolution_um] = well_images_for_plate[ch_idx]

                    # Store for stacking
                    for resolution in self.target_resolutions_um:
                        resolution_stacks[resolution].append(downsampled_images[resolution])

                # Save each resolution as multipage TIFF
                for resolution in self.target_resolutions_um:
                    downsampled_stack = resolution_stacks[resolution]
                    if not downsampled_stack:
                        continue

                    # Stack channels into multipage array (C, H, W)
                    stacked = np.stack(downsampled_stack, axis=0)

                    filename = f"{self.well_id}_{int(resolution)}um.tiff"
                    filepath = os.path.join(wells_dir, filename)

                    # Save as multipage TIFF with channel metadata
                    tifffile.imwrite(
                        filepath,
                        stacked,
                        metadata={
                            "axes": "CYX",
                            "Channel": {"Name": channel_names[: len(downsampled_stack)]},
                        },
                    )
                    log.debug(f"Saved {filepath} with shape {stacked.shape} ({len(downsampled_stack)} channels)")

            t_save_end = time.perf_counter()

            # Log timing summary for performance analysis
            t_total = t_save_end - t_start
            stitched_shape = list(stitched_channels.values())[0].shape if stitched_channels else (0, 0)
            plate_shape = list(well_images_for_plate.values())[0].shape if well_images_for_plate else (0, 0)
            log.debug(
                f"[PERF] Well {self.well_id} complete: "
                f"get_img={t_get_image - t_start:.3f}s, crop={t_crop - t_get_image:.3f}s, "
                f"accum={t_accumulate - t_crop:.3f}s, stitch={t_stitch_end - t_stitch_start:.3f}s, "
                f"downsample_plate={t_downsample_plate_end - t_downsample_plate_start:.3f}s, "
                f"save={t_save_end - t_save_start:.3f}s, "
                f"TOTAL={t_total:.3f}s | "
                f"tile={tile.shape}, stitched={stitched_shape}, plate={plate_shape}, "
                f"channels={len(stitched_channels)}, skip_saving={self.skip_saving}"
            )

            return DownsampledViewResult(
                well_id=self.well_id,
                well_row=self.well_row,
                well_col=self.well_col,
                well_images=well_images_for_plate,
                channel_names=channel_names,
            )

        except Exception as e:
            log.exception(f"Error processing well {self.well_id}: {e}")
            # Track failed well for reporting
            self._failed_wells[self.well_id] = str(e)
            raise
        finally:
            # Ensure accumulator is always cleaned up after processing a complete well
            self._well_accumulators.pop(self.well_id, None)


# TODO: For Zarr with FULL_FRAME chunks, writes to different FOVs/regions are
# independent.  A future optimization is to run N JobRunner processes partitioned
# by FOV or region, giving linear throughput scaling when disk bandwidth allows.
# The backpressure counters already use shared multiprocessing.Value and would
# work across multiple workers without changes.
class JobRunner(multiprocessing.Process):
    def __init__(
        self,
        acquisition_info: Optional[AcquisitionInfo] = None,
        cleanup_stale_ome_files: bool = False,
        log_file_path: Optional[str] = None,
        # Backpressure shared values (from BackpressureController)
        bp_pending_jobs: Optional[multiprocessing.Value] = None,
        bp_pending_bytes: Optional[multiprocessing.Value] = None,
        bp_capacity_event: Optional[multiprocessing.Event] = None,
        # Zarr writer info (for ZARR_V3 saving)
        zarr_writer_info: Optional[ZarrWriterInfo] = None,
        # Remote upload target for ZARR_V3 streaming (None disables upload).
        # ``upload_input_queue`` is the input queue of an ``UploadWorker``
        # owned by the main process; this subprocess only writes to it.
        upload_target: Optional[UploadTarget] = None,
        upload_input_queue: Optional[multiprocessing.Queue] = None,
    ):
        super().__init__()
        # Daemon processes are terminated when the main process exits, ensuring
        # cleanup even if the main process crashes. Note: forceful termination
        # means the shutdown cleanup code (releasing incomplete well bytes) may
        # be skipped - see the cleanup block after the main while loop in run().
        self.daemon = True
        self._log = squid.logging.get_logger(__class__.__name__)
        self._acquisition_info = acquisition_info
        self._zarr_writer_info = zarr_writer_info
        self._upload_target = upload_target
        self._upload_input_queue = upload_input_queue
        self._log_file_path = log_file_path  # Will be used in subprocess to set up file logging

        self._input_queue: multiprocessing.Queue = multiprocessing.Queue()
        self._input_timeout = 1.0
        self._output_queue: multiprocessing.Queue = multiprocessing.Queue()
        self._shutdown_event: multiprocessing.Event = multiprocessing.Event()
        self._ready_event: multiprocessing.Event = multiprocessing.Event()  # Signals subprocess is ready
        # Track jobs in flight (dispatched but not yet completed)
        self._pending_count = multiprocessing.Value("i", 0)

        # Backpressure tracking (shared with BackpressureController)
        self._bp_pending_jobs = bp_pending_jobs
        self._bp_pending_bytes = bp_pending_bytes
        self._bp_capacity_event = bp_capacity_event

        # Clean up stale metadata files from previous crashed acquisitions
        # Only run when explicitly requested (i.e., when OME-TIFF saving is being used)
        if cleanup_stale_ome_files:
            removed = ome_tiff_writer.cleanup_stale_metadata_files()
            if removed:
                self._log.info(f"Cleaned up {len(removed)} stale OME-TIFF metadata files")

    def dispatch(self, job: Job):
        # Inject acquisition_info into SaveOMETiffJob instances before serialization.
        # The job object is pickled when placed in the queue, so injection must happen here.
        if isinstance(job, SaveOMETiffJob):
            if self._acquisition_info is None:
                raise ValueError(
                    "Cannot dispatch SaveOMETiffJob: JobRunner was initialized without acquisition_info. "
                    "When using OME-TIFF saving, initialize JobRunner with an AcquisitionInfo instance."
                )
            job.acquisition_info = self._acquisition_info

        # Inject zarr_writer_info into SaveZarrJob instances before serialization.
        if isinstance(job, SaveZarrJob):
            if self._zarr_writer_info is None:
                raise ValueError(
                    "Cannot dispatch SaveZarrJob: JobRunner was initialized without zarr_writer_info. "
                    "When using ZARR_V3 saving, initialize JobRunner with a ZarrWriterInfo instance."
                )
            job.zarr_writer_info = self._zarr_writer_info

        # Inject upload_target into FlushAndStageUploadJob instances before serialization.
        # ``upload_input_queue`` is installed on the class in the subprocess at startup.
        if isinstance(job, FlushAndStageUploadJob):
            job.upload_target = self._upload_target

        # PostprocessJob writes derived zarr plates + runs its own upload barriers
        # inline, so it needs the same injected context as the zarr runner.
        if isinstance(job, PostprocessJob):
            job.zarr_writer_info = self._zarr_writer_info
            job.acquisition_info = self._acquisition_info
            job.upload_target = self._upload_target

        # Calculate image bytes for backpressure tracking
        image_bytes = 0
        if self._bp_pending_jobs is not None:
            if job.capture_image and job.capture_image.image_array is not None:
                image_bytes = job.capture_image.image_array.nbytes

        # Increment counters BEFORE putting job in queue to prevent race condition
        # where worker processes job before counter is incremented, causing
        # has_pending() to return False while job is still in flight.
        with self._pending_count.get_lock():
            self._pending_count.value += 1
        if self._bp_pending_jobs is not None:
            with self._bp_pending_jobs.get_lock():
                self._bp_pending_jobs.value += 1
            with self._bp_pending_bytes.get_lock():
                self._bp_pending_bytes.value += image_bytes

        try:
            self._input_queue.put_nowait(job)
        except Exception as original_exc:
            # Roll back ALL counters if enqueue fails
            try:
                with self._pending_count.get_lock():
                    self._pending_count.value -= 1
                if self._bp_pending_jobs is not None:
                    with self._bp_pending_jobs.get_lock():
                        self._bp_pending_jobs.value = max(0, self._bp_pending_jobs.value - 1)
                    with self._bp_pending_bytes.get_lock():
                        self._bp_pending_bytes.value = max(0, self._bp_pending_bytes.value - image_bytes)
            except Exception as rollback_exc:
                self._log.error(
                    f"Failed to rollback counters after dispatch failure: {rollback_exc}. "
                    f"Counters may be inconsistent. Original error: {original_exc}"
                )
            raise original_exc
        return True

    def output_queue(self) -> multiprocessing.Queue:
        return self._output_queue

    def has_pending(self):
        with self._pending_count.get_lock():
            return self._pending_count.value > 0

    def wait_ready(self, timeout_s: float = 5.0) -> bool:
        """Wait for the subprocess to signal it's ready to process jobs.

        Args:
            timeout_s: Maximum time to wait in seconds.

        Returns:
            True if subprocess is ready, False if timed out.
        """
        return self._ready_event.wait(timeout=timeout_s)

    def set_acquisition_info(self, acquisition_info: AcquisitionInfo):
        """Set acquisition info for OME-TIFF saving.

        Thread safety: This method and dispatch() are NOT synchronized. The caller
        must ensure this method completes BEFORE any dispatch() calls that need
        the acquisition_info. In practice, this is called during worker init
        before the acquisition loop starts, so no synchronization is needed.
        """
        self._acquisition_info = acquisition_info

    def has_upload_pipeline(self) -> bool:
        """True iff this runner was constructed with an upload target + queue.

        Used by ``multi_point_worker`` to decide whether the pre-warmed runner
        can be reused: the upload queue must be installed at subprocess
        startup, so a pre-warmed runner created before the upload config was
        known cannot satisfy an upload-enabled acquisition.
        """
        return (
            self._upload_target is not None
            and self._upload_target.enabled
            and self._upload_input_queue is not None
        )

    def set_zarr_writer_info(self, zarr_writer_info: "ZarrWriterInfo"):
        """Set zarr writer info for ZARR_V3 saving.

        Thread safety: This method and dispatch() are NOT synchronized. The caller
        must ensure this method completes BEFORE any dispatch() calls that need
        the zarr_writer_info. In practice, this is called during worker init
        before the acquisition loop starts, so no synchronization is needed.
        """
        self._zarr_writer_info = zarr_writer_info

    def is_ready(self) -> bool:
        """Check if the subprocess is ready without blocking."""
        return self._ready_event.is_set()

    def shutdown(self, timeout_s=1.0):
        # Guard against double shutdown
        if self._shutdown_event is None:
            return
        self._shutdown_event.set()
        # Send sentinel to wake up worker blocked on queue.get()
        try:
            self._input_queue.put_nowait(None)
        except (queue.Full, OSError, ValueError) as e:
            # queue.Full: Queue is at capacity (unlikely for sentinel)
            # OSError: Queue's underlying pipe/semaphore closed
            # ValueError: Queue has been closed
            self._log.debug(f"Could not send shutdown sentinel to worker: {e}")
        self.join(timeout=timeout_s)
        # If process is still alive after timeout, terminate it. This is a
        # last-resort kill: the subprocess runs finalize_all_writers() on its
        # way out, so terminating here can interrupt an in-flight TensorStore
        # shard commit and leave a half-written shard (last z-slices missing at
        # level 0) plus a stray ``*.__lock`` file. Callers must pass a timeout
        # generous enough for finalize to complete (see
        # JOB_RUNNER_FINALIZE_TIMEOUT_S); reaching this branch means finalize
        # genuinely wedged.
        if self.is_alive():
            if getattr(_def, "ZARR_SHARD_PER_Z", False):
                # Per-z sharding commits each z-slice during acquisition, so the
                # wedge is almost always in finalize *teardown* AFTER the data +
                # completion flags are already durable — not a data-loss event.
                detail = (
                    "With per-z sharding, image data is committed to disk during "
                    "acquisition, so already-finalized FOVs are durable; at worst a "
                    "single FOV still mid-finalize at this instant could miss its "
                    "last z-slice(s). Audit the dataset before assuming any loss."
                )
            else:
                detail = (
                    "With per-FOV sharding the final level-0 commit happens during "
                    "finalize, so the most recent FOV(s) may be missing their last "
                    "z-slices at pyramid level 0."
                )
            self._log.error(
                f"JobRunner subprocess (PID={self.pid}) did not exit within {timeout_s} [s] of "
                f"shutdown; terminating. Finalization wedged in finalize or teardown. {detail} "
                f"A faulthandler stack dump of the wedge should be in the '*_finalize_stacks' "
                f"file next to the worker log."
            )
            self.terminate()
            self.join(timeout=1.0)
        # Clean up multiprocessing primitives to avoid semaphore leaks
        self._input_queue.close()
        self._input_queue.join_thread()
        self._output_queue.close()
        self._output_queue.join_thread()
        # Clear references to allow garbage collection of Event and Value semaphores
        self._input_queue = None
        self._output_queue = None
        self._shutdown_event = None
        self._pending_count = None

    def run(self):
        import logging

        # Configure logging in subprocess - the squid.logging module sets up console logging
        # on import, but we need to ensure it's properly initialized in this process.
        # Default to INFO for stdout in the worker, and allow overriding via
        # the SQUID_WORKER_LOG_LEVEL environment variable (e.g. "DEBUG").
        stdout_level = logging.INFO
        env_level = os.environ.get("SQUID_WORKER_LOG_LEVEL")
        if env_level:
            env_level_upper = env_level.upper()
            if hasattr(logging, env_level_upper):
                stdout_level = getattr(logging, env_level_upper)
        squid.logging.set_stdout_log_level(stdout_level)

        # Set up file logging if a log file path was provided
        # Use a separate file for the worker to avoid multiprocess file write conflicts
        worker_log_path = None
        if self._log_file_path:
            base, ext = os.path.splitext(self._log_file_path)
            worker_log_path = f"{base}_worker{ext}"
            squid.logging.add_file_handler(worker_log_path, replace_existing=True, level=logging.DEBUG)

        self._log = squid.logging.get_logger(self.__class__.__name__)
        worker_log_msg = f", worker_log={worker_log_path}" if worker_log_path else ""
        self._log.info(f"JobRunner subprocess started (PID={os.getpid()}{worker_log_msg})")

        # Start memory monitoring for the worker process
        start_worker_monitoring(sample_interval_ms=200)
        log_memory("WORKER_START", include_children=False)

        # Install the upload worker's input queue on the class so that
        # ``FlushAndStageUploadJob.run()`` instances in this subprocess can
        # reach the upload pipeline. The main process owns/started the
        # ``UploadWorker``; we are only a producer here.
        if self._upload_input_queue is not None:
            FlushAndStageUploadJob._upload_input_queue = self._upload_input_queue
            self._log.info("Upload input queue installed on FlushAndStageUploadJob")

        # PostprocessJob holds accumulated frame bytes across job completions, so
        # give it the shared backpressure Value/Event to keep them accounted for.
        PostprocessJob._bp_pending_bytes = self._bp_pending_bytes
        PostprocessJob._bp_capacity_event = self._bp_capacity_event

        # Signal to main process that we're ready to receive jobs
        self._ready_event.set()

        while not self._shutdown_event.is_set():
            job = None
            try:
                t_wait_start = time.perf_counter()
                job = self._input_queue.get(timeout=self._input_timeout)
                t_got_job = time.perf_counter()

                # None is a shutdown sentinel - skip processing and check shutdown flag
                if job is None:
                    continue

                self._log.debug(f"Running job {job.job_id} (waited {(t_got_job - t_wait_start)*1000:.1f}ms in queue)...")

                # Set operation context for memory tracking
                if isinstance(job, DownsampledViewJob):
                    set_worker_operation(f"DOWNSAMPLE_{job.well_id}")
                else:
                    set_worker_operation(job.__class__.__name__)

                t_run_start = time.perf_counter()
                result = job.run()
                t_run_end = time.perf_counter()

                # Only queue non-None results (DownsampledViewJob returns None for intermediate FOVs)
                if result is not None:
                    self._log.debug(
                        f"Job {job.job_id} returned in {(t_run_end - t_run_start)*1000:.1f}ms. "
                        f"Sending result to output queue."
                    )
                    self._output_queue.put_nowait(JobResult(job_id=job.job_id, result=result, exception=None))
                    self._log.debug(f"Result for {job.job_id} is on output queue.")
                else:
                    self._log.warning(
                        f"Job {job.job_id} returned None in {(t_run_end - t_run_start)*1000:.1f}ms, not queuing."
                    )
            except queue.Empty:
                pass
            except Exception as e:
                if job:
                    self._log.exception(f"Job {job.job_id} failed! Returning exception result.")
                    self._output_queue.put_nowait(JobResult(job_id=job.job_id, result=None, exception=e))
            finally:
                # Clear operation context after job completes
                set_worker_operation("")
                # Decrement pending count when job completes (success, None result, or exception)
                if job is not None:
                    with self._pending_count.get_lock():
                        self._pending_count.value -= 1

                    # Backpressure tracking: decrement counters immediately when job completes.
                    # Note: For DownsampledViewJob, the image data moves to subprocess memory
                    # (the accumulator) when the job is processed. Backpressure tracks queue
                    # memory, not subprocess memory, so it's correct to release bytes here
                    # rather than waiting for well completion.
                    if self._bp_pending_jobs is not None:
                        with self._bp_pending_jobs.get_lock():
                            self._bp_pending_jobs.value = max(0, self._bp_pending_jobs.value - 1)

                        # Decrement image bytes
                        if job.capture_image and job.capture_image.image_array is not None:
                            image_bytes = job.capture_image.image_array.nbytes
                            with self._bp_pending_bytes.get_lock():
                                self._bp_pending_bytes.value = max(0, self._bp_pending_bytes.value - image_bytes)

                        # Signal capacity available for all job completions
                        if self._bp_capacity_event is not None:
                            self._bp_capacity_event.set()

        # Arm a faulthandler watchdog over finalize + process teardown. If the
        # subprocess wedges on its way out — a TensorStore context/thread that
        # won't join, or a multiprocessing queue feeder with buffered items —
        # this dumps EVERY thread's stack to a stacks file every
        # JOB_RUNNER_FINALIZE_WATCHDOG_S, capturing the exact culprit frame
        # instead of only the parent's opaque "did not exit within 600s"
        # terminate. Best-effort; diagnostics must never break finalize. Left
        # armed (not disarmed): a healthy exit (seconds) beats the timeout, and
        # staying armed also covers the post-run() interpreter teardown.
        stacks_path = None
        try:
            if worker_log_path:
                _sb, _se = os.path.splitext(worker_log_path)
                stacks_path = f"{_sb}_finalize_stacks{_se}"
            else:
                stacks_path = os.path.join(
                    squid.logging.get_default_log_directory(), "jobrunner_finalize_stacks.log"
                )
            os.makedirs(os.path.dirname(stacks_path) or ".", exist_ok=True)
            self._finalize_stacks_file = open(stacks_path, "a", buffering=1)
            self._finalize_stacks_file.write(
                f"\n===== JobRunner PID={os.getpid()} finalize watchdog armed @ "
                f"{datetime.now(timezone.utc).isoformat()} "
                f"(dumps all thread stacks every {JOB_RUNNER_FINALIZE_WATCHDOG_S}s while wedged) =====\n"
            )
            self._finalize_stacks_file.flush()
            faulthandler.dump_traceback_later(
                JOB_RUNNER_FINALIZE_WATCHDOG_S, repeat=True, file=self._finalize_stacks_file
            )
            self._log.info(f"Finalize watchdog armed; wedge stack dumps -> {stacks_path}")
        except Exception as e:
            self._log.warning(f"Could not arm finalize watchdog: {e}")

        # Finalize any zarr writers that are still open
        t_finalize_start = time.perf_counter()
        try:
            success = SaveZarrJob.finalize_all_writers()
            if not success:
                self._log.error("ZARR FINALIZATION INCOMPLETE - Some data may not be saved correctly")
        except Exception as e:
            self._log.error(f"Error finalizing zarr writers during shutdown: {e}")
        self._log.info(
            f"finalize_all_writers() completed in {time.perf_counter() - t_finalize_start:.1f}s"
        )

        # Flush the upload queue feeder thread so any UploadTask items that
        # FlushAndStageUploadJob.run() put() onto the queue actually reach
        # the UploadWorker before this subprocess exits.
        #
        # multiprocessing.Queue.put() returns as soon as the item is
        # buffered into the per-process Python-side feeder thread, NOT when
        # it has been written to the underlying pipe. If we exit the
        # subprocess without flushing, the feeder is killed and any buffered
        # items are silently lost — even though the BarrierResult we
        # already sent back through the JobRunner's OUTPUT queue has the
        # main process tracking those tasks as "in flight". Symptom:
        # ``pending_task_ids`` stays positive forever because the
        # UploadWorker never receives the lost tasks and never emits
        # matching UploadResults.
        if self._upload_input_queue is not None:
            try:
                self._upload_input_queue.close()
                self._upload_input_queue.join_thread()
            except Exception as e:
                self._log.error(
                    f"Error flushing upload queue feeder on shutdown: {e}"
                )

        # Stop memory monitoring and log final report
        log_memory("WORKER_SHUTDOWN", include_children=False)
        stop_worker_monitoring()

        # Let the subprocess exit promptly even if JobResults remain buffered in
        # the output queue's feeder thread. Data durability is guaranteed by
        # finalize_all_writers() above (not by this status queue), and the parent
        # has already drained the results it needs and tracks completion via the
        # shared pending counter. Without this, an unflushed output item makes
        # the feeder block at interpreter exit and the whole subprocess hangs
        # until the parent's terminate() fires — the leading suspect for the
        # observed 600s finalize wedge. The watchdog above confirms this (no
        # dump => exit is now clean) or reveals a different teardown culprit.
        try:
            self._output_queue.cancel_join_thread()
        except Exception as e:
            self._log.debug(f"output_queue.cancel_join_thread on exit: {e}")

        self._log.info("Shutdown request received, exiting run (interpreter teardown begins).")
