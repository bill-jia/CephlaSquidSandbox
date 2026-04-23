#!/usr/bin/env python3
"""Repackage an existing INDIVIDUAL_IMAGES Squid acquisition into the current
OME-NGFF ZARR_V3 layout.

Reads per-timepoint TIFF folders of the form::

    {experiment_dir}/
        acquisition.yaml
        coordinates.csv
        {timepoint}/
            {region}_{fov}_{z}_{channel}.tiff
            coordinates.csv           (per-timepoint, optional)
            frame_acquisition_times.csv (optional, written by newer runs)

and writes a sibling ``plate.ome.zarr`` (HCS) or ``zarr/`` (non-HCS) tree
matching what a fresh ZARR_V3 acquisition would produce:

* One 5D ``(T, C, Z, Y, X)`` zarr per FOV with shard ``(1, C, Z, Y, X)``.
* Streaming multiscale pyramid (via ``ZarrWriter``).
* OME-NGFF multiscales + omero + ``_squid.manifest_path`` metadata.
* HCS plate + well metadata when the layout is HCS.
* Per-frame timestamps as a ``frame_times`` zarr array inside each FOV.

Missing frames are zero-filled by default and logged to a
``missing_frames.csv`` file in the output root.

Usage::

    python software/tools/repackage_tiffs_to_zarr.py \\
        --input <exp_dir> \\
        [--output <exp_dir>/repackaged] \\
        [--compression balanced|fast|best|none] \\
        [--jobs N] \\
        [--on-missing zero-fill|skip-fov|fail] \\
        [--layout auto|hcs|flat] \\
        [--trim-to-last-observed-t] \\
        [--force] \\
        [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import shutil
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import tifffile
import yaml

# Make this script runnable with `python software/tools/repackage_tiffs_to_zarr.py`
# from the repo root OR with `python tools/repackage_tiffs_to_zarr.py` from software/.
_SOFTWARE_DIR = Path(__file__).resolve().parent.parent
if str(_SOFTWARE_DIR) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE_DIR))

from control import _def, utils  # noqa: E402
from control._def import ZarrCompression  # noqa: E402
from control.core.zarr_writer import (  # noqa: E402
    ZarrAcquisitionConfig,
    ZarrWriter,
    write_plate_metadata,
    write_well_metadata,
)


log = logging.getLogger("repackage_tiffs_to_zarr")


# -- Data models ---------------------------------------------------------------


@dataclass
class AcquisitionLayout:
    """Denormalized acquisition layout, either scraped from acquisition.yaml
    or reconstructed from filesystem + coordinates.csv."""

    experiment_dir: Path
    widget_type: str  # "wellplate" or "flexible"
    t_size: int
    z_size: int
    channel_names: List[str]
    pixel_size_um: float
    z_step_um: Optional[float]
    time_increment_s: Optional[float]
    region_names: List[str]
    region_fov_count: Dict[str, int]
    # (region, fov) -> (y_um, x_um)
    fov_translations_um: Dict[str, Dict[int, Tuple[float, float]]]
    is_hcs: bool

    def __post_init__(self):
        if not self.channel_names:
            raise ValueError("No channels discovered in acquisition layout.")


@dataclass
class FovTask:
    """One per FOV."""

    region_id: str
    fov: int
    t_size: int
    c_size: int
    z_size: int
    channel_names: List[str]
    pixel_size_um: float
    z_step_um: Optional[float]
    time_increment_s: Optional[float]
    translation_um: Tuple[float, float]
    is_hcs: bool
    input_dir: Path
    output_root: Path
    manifest_path: str
    # dict indexed by (t, z, channel_idx) -> (tiff_path, ext)
    tiff_index: Dict[Tuple[int, int, int], Path]
    frame_time_index: Dict[Tuple[int, int, int], float]
    compression: ZarrCompression
    on_missing: str  # "zero-fill" | "skip-fov" | "fail"
    trim_to_last_observed_t: bool


@dataclass
class FovResult:
    region_id: str
    fov: int
    frames_found: int = 0
    frames_missing: int = 0
    skipped: bool = False
    error: Optional[str] = None
    missing_cells: List[Tuple[int, int, int, int]] = field(
        default_factory=list
    )  # (t, c, z, 1-if-missing)  -- note: c indexes the channel_names list on this FOV's parent task


# -- Filename / manifest scanning ----------------------------------------------


_IMAGE_EXTS = ("tiff", "tif", "png", "bmp")


def _iter_timepoint_dirs(experiment_dir: Path) -> List[Tuple[int, Path]]:
    """List ``(time_point, dir)`` pairs. Time point folders are 0-padded decimal ints."""
    results = []
    for entry in experiment_dir.iterdir():
        if not entry.is_dir():
            continue
        try:
            t = int(entry.name)
        except ValueError:
            continue
        results.append((t, entry))
    results.sort(key=lambda x: x[0])
    return results


def _parse_tiff_name(
    filename: str,
    region_names: List[str],
    channel_names: List[str],
) -> Optional[Tuple[str, int, int, str, str]]:
    """Parse ``{region}_{fov}_{z}_{channel}.{ext}`` using the known region and
    channel lists (both of which can contain underscores).

    Returns ``(region, fov, z, channel, ext)`` or ``None`` if the filename does
    not match any known combination.
    """
    # Sort region names longest first to avoid prefix collisions (e.g. "A1" vs "A10").
    sorted_regions = sorted(region_names, key=len, reverse=True)
    sorted_channels = sorted(channel_names, key=len, reverse=True)

    for region in sorted_regions:
        prefix = f"{region}_"
        if not filename.startswith(prefix):
            continue
        remainder = filename[len(prefix):]
        for channel in sorted_channels:
            channel_safe = channel.replace(" ", "_")
            for ext in _IMAGE_EXTS:
                suffix = f"_{channel_safe}.{ext}"
                if remainder.endswith(suffix):
                    middle = remainder[: -len(suffix)]
                    parts = middle.split("_")
                    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                        return region, int(parts[0]), int(parts[1]), channel, ext
                    return None
    return None


def _scan_tiffs(
    experiment_dir: Path, region_names: List[str], channel_names: List[str]
) -> Tuple[
    Dict[Tuple[int, str, int, int, int], Path],
    Dict[str, int],
    int,
    int,
    int,
]:
    """Walk per-timepoint folders and build a manifest.

    Returns:
        tiff_index: ``(t, region, fov, z, channel_idx) -> Path``.
        region_fov_max: ``region -> max(fov)+1``.
        t_max_plus_one, z_max_plus_one, c_max_plus_one: inferred bounds.
    """
    channel_idx = {name: i for i, name in enumerate(channel_names)}
    tiff_index: Dict[Tuple[int, str, int, int, int], Path] = {}
    region_fov_max: Dict[str, int] = {}
    t_max = -1
    z_max = -1
    c_max = -1
    unparseable = 0

    for t, tp_dir in _iter_timepoint_dirs(experiment_dir):
        for entry in tp_dir.iterdir():
            if not entry.is_file():
                continue
            name = entry.name
            # Skip known sidecars
            if name.endswith(".csv") or name.endswith(".json") or name.endswith(".yaml"):
                continue
            parsed = _parse_tiff_name(name, region_names, channel_names)
            if parsed is None:
                unparseable += 1
                continue
            region, fov, z, channel, _ext = parsed
            if channel not in channel_idx:
                continue
            c_idx = channel_idx[channel]
            tiff_index[(t, region, fov, z, c_idx)] = entry
            region_fov_max[region] = max(region_fov_max.get(region, -1), fov) + 0  # keep track
            # record actual max
            region_fov_max[region] = max(region_fov_max.get(region, -1), fov)
            t_max = max(t_max, t)
            z_max = max(z_max, z)
            c_max = max(c_max, c_idx)

    if unparseable:
        log.warning("Ignored %d unparseable filenames under %s", unparseable, experiment_dir)

    fov_counts = {r: n + 1 for r, n in region_fov_max.items()}
    return tiff_index, fov_counts, t_max + 1, z_max + 1, c_max + 1


# -- Coordinates / layout discovery --------------------------------------------


def _read_coordinates_csv(path: Path) -> List[Tuple[str, float, float, float]]:
    rows: List[Tuple[str, float, float, float]] = []
    with path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                region = str(row["region"])
                x_mm = float(row["x (mm)"])
                y_mm = float(row["y (mm)"])
                z_mm = float(row.get("z (mm)", 0.0) or 0.0)
            except (KeyError, ValueError):
                continue
            rows.append((region, x_mm, y_mm, z_mm))
    return rows


def _build_translations_from_coordinates(
    coords_path: Path,
) -> Dict[str, Dict[int, Tuple[float, float]]]:
    """Derive per-region per-FOV (y_um, x_um) from a top-level coordinates.csv.

    Row order within a region is taken as the FOV index.
    """
    result: Dict[str, Dict[int, Tuple[float, float]]] = {}
    per_region_counter: Dict[str, int] = {}
    for region, x_mm, y_mm, _z_mm in _read_coordinates_csv(coords_path):
        fov_idx = per_region_counter.get(region, 0)
        region_map = result.setdefault(region, {})
        region_map[fov_idx] = (y_mm * 1000.0, x_mm * 1000.0)
        per_region_counter[region] = fov_idx + 1
    return result


def _detect_hcs(region_names: List[str], widget_type: Optional[str], override: str) -> bool:
    """Decide HCS vs flat layout. HCS requires well-ID-shaped region names."""
    if override == "hcs":
        return True
    if override == "flat":
        return False
    well_pattern = re.compile(r"^[A-Z]\d+$")
    all_look_like_wells = all(well_pattern.match(r) for r in region_names) if region_names else False
    if widget_type == "wellplate":
        return all_look_like_wells
    return False


def _load_frame_acquisition_times(experiment_dir: Path, channel_names: List[str]) -> Dict[Tuple[int, str, int, int, int], float]:
    """Build ``(t, region, fov, z, channel_idx) -> unix_time_s`` from per-timepoint CSVs, if present."""
    channel_idx = {name: i for i, name in enumerate(channel_names)}
    out: Dict[Tuple[int, str, int, int, int], float] = {}
    for t, tp_dir in _iter_timepoint_dirs(experiment_dir):
        csv_path = tp_dir / "frame_acquisition_times.csv"
        if not csv_path.is_file():
            continue
        try:
            with csv_path.open("r", newline="") as f:
                for row in csv.DictReader(f):
                    try:
                        time_point = int(row.get("time_point") or t)
                        region = str(row["region_id"])
                        fov = int(row["fov"])
                        z = int(row["z_level"])
                        ch = row["channel"]
                        unix_s = float(row["unix_time_s"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    c_idx = channel_idx.get(ch)
                    if c_idx is None:
                        continue
                    out[(time_point, region, fov, z, c_idx)] = unix_s
        except OSError:
            continue
    return out


# -- Layout resolution ---------------------------------------------------------


def _load_acquisition_yaml(experiment_dir: Path) -> Optional[dict]:
    path = experiment_dir / "acquisition.yaml"
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        log.warning("Could not parse acquisition.yaml: %s", e)
        return None


def _resolve_layout(
    experiment_dir: Path,
    layout_override: str,
) -> AcquisitionLayout:
    """Pull T/Z/C/pixel_size/regions from acquisition.yaml with filesystem fallback."""
    yaml_dict = _load_acquisition_yaml(experiment_dir)

    region_names: List[str] = []
    region_fov_count: Dict[str, int] = {}
    channel_names: List[str] = []
    t_size = 0
    z_size = 0
    pixel_size_um = 1.0
    z_step_um: Optional[float] = None
    time_increment_s: Optional[float] = None
    widget_type = "flexible"

    if yaml_dict:
        acq = yaml_dict.get("acquisition", {}) or {}
        widget_type = acq.get("widget_type", "flexible")
        objective = yaml_dict.get("objective", {}) or {}
        pixel_size_um = float(objective.get("pixel_size_um", 1.0) or 1.0)
        z_stack = yaml_dict.get("z_stack", {}) or {}
        z_size = int(z_stack.get("nz", 1) or 1)
        delta_z_mm = z_stack.get("delta_z_mm")
        z_step_um = float(delta_z_mm) * 1000.0 if delta_z_mm and z_size > 1 else None
        ts = yaml_dict.get("time_series", {}) or {}
        t_size = int(ts.get("nt", 1) or 1)
        delta_t_s = ts.get("delta_t_s")
        time_increment_s = float(delta_t_s) if delta_t_s and t_size > 1 else None
        ch = yaml_dict.get("channels", {}) or {}
        channel_names = list(ch.get("observation_state_names", []) or [])
        if widget_type == "wellplate":
            scan = yaml_dict.get("wellplate_scan", {}) or {}
            regions = scan.get("regions", []) or []
        else:
            scan = yaml_dict.get("flexible_scan", {}) or {}
            regions = scan.get("positions", []) or []
        region_names = [str(r.get("name")) for r in regions if r.get("name") is not None]

    # Fallback: scan filesystem to infer channels + dims if yaml is missing or empty.
    if not channel_names or not region_names:
        log.info("acquisition.yaml missing or incomplete; scanning filesystem for layout")
        channel_names, region_names = _infer_channels_and_regions_from_fs(experiment_dir)

    # Build tiff index to infer T/Z if yaml didn't say, and as a fallback FOV count source.
    tiff_index, fs_fov_counts, fs_t, fs_z, fs_c = _scan_tiffs(experiment_dir, region_names, channel_names)
    region_fov_count = dict(fs_fov_counts)
    t_size = max(t_size, fs_t)
    z_size = max(z_size, fs_z)

    if not channel_names:
        raise RuntimeError("No channels discovered — cannot proceed.")
    if not region_names:
        raise RuntimeError("No regions discovered — cannot proceed.")
    if t_size < 1:
        t_size = 1
    if z_size < 1:
        z_size = 1

    # FOV translations: prefer top-level coordinates.csv.
    top_coords = experiment_dir / "coordinates.csv"
    if top_coords.is_file():
        fov_translations = _build_translations_from_coordinates(top_coords)
    else:
        # Fall back to the first timepoint's coordinates.csv.
        fov_translations = {}
        for t, tp_dir in _iter_timepoint_dirs(experiment_dir):
            csv_path = tp_dir / "coordinates.csv"
            if csv_path.is_file():
                fov_translations = _build_translations_from_coordinates(csv_path)
                break

    # coordinates.csv is the authoritative FOV count source — it lists planned FOVs
    # independent of which TIFFs ended up on disk. This keeps skipped/missing FOVs
    # enumerated as tasks so --on-missing=skip-fov etc. can account for them.
    for region_id, region_map in fov_translations.items():
        if region_map:
            region_fov_count[region_id] = max(region_map.keys()) + 1

    is_hcs = _detect_hcs(region_names, widget_type, layout_override)

    return AcquisitionLayout(
        experiment_dir=experiment_dir,
        widget_type=widget_type,
        t_size=t_size,
        z_size=z_size,
        channel_names=channel_names,
        pixel_size_um=pixel_size_um,
        z_step_um=z_step_um,
        time_increment_s=time_increment_s,
        region_names=region_names,
        region_fov_count=region_fov_count,
        fov_translations_um=fov_translations,
        is_hcs=is_hcs,
    )


def _infer_channels_and_regions_from_fs(experiment_dir: Path) -> Tuple[List[str], List[str]]:
    """Last-resort heuristic: scan filenames for ``_{channel_safe}.{ext}`` suffixes.

    Splits each filename on ``_`` and enumerates all observed suffixes. This
    can over-segment channel names with underscores, so prefer acquisition.yaml.
    """
    channel_set = set()
    region_set = set()
    for _t, tp_dir in _iter_timepoint_dirs(experiment_dir):
        for entry in tp_dir.iterdir():
            if not entry.is_file():
                continue
            name = entry.name
            stem, dot, ext = name.rpartition(".")
            if ext.lower() not in _IMAGE_EXTS:
                continue
            parts = stem.split("_")
            if len(parts) < 4:
                continue
            # best-effort: region = first part; fov+z = next two numeric parts; channel = rest
            region = parts[0]
            region_set.add(region)
            # walk forward while numeric
            i = 1
            num_count = 0
            while i < len(parts) and parts[i].isdigit() and num_count < 2:
                i += 1
                num_count += 1
            channel_pieces = parts[i:]
            if channel_pieces:
                channel_set.add("_".join(channel_pieces).replace("_", " "))
    return sorted(channel_set), sorted(region_set)


# -- Per-FOV worker ------------------------------------------------------------


def _output_group_dir(output_root: Path, is_hcs: bool, region_id: str, fov: int) -> Path:
    if is_hcs:
        return Path(utils.build_hcs_zarr_fov_path(str(output_root), region_id, fov))
    return Path(utils.build_per_fov_zarr_path(str(output_root), region_id, fov))


def _output_array_path(output_root: Path, is_hcs: bool, region_id: str, fov: int) -> Path:
    return _output_group_dir(output_root, is_hcs, region_id, fov) / "0"


def _manifest_rel_path(group_dir: Path, manifest_abs: Path) -> str:
    try:
        return os.path.relpath(str(manifest_abs), str(group_dir)).replace("\\", "/")
    except ValueError:
        return str(manifest_abs)


def _process_one_fov(task: FovTask) -> FovResult:
    """Worker body: write all planes for one FOV into a fresh zarr store."""
    # Stdlib logging doesn't follow into forked/spawned workers — bootstrap here.
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(name)s: %(message)s")
    worker_log = logging.getLogger(f"repackage.{task.region_id}.{task.fov}")
    result = FovResult(region_id=task.region_id, fov=task.fov)

    # Probe one existing TIFF to get (Y, X) and dtype
    probe_path = next(iter(task.tiff_index.values()), None)
    if probe_path is None:
        if task.on_missing == "skip-fov":
            worker_log.warning("FOV %s / %d has no TIFFs at all — skipping", task.region_id, task.fov)
            result.skipped = True
            return result
        if task.on_missing == "fail":
            raise RuntimeError(f"No TIFFs found for FOV {task.region_id}/{task.fov}")
        # zero-fill mode: cannot create a zarr with no shape — skip this FOV but note it
        worker_log.warning("FOV %s / %d has no TIFFs; cannot infer shape — skipping", task.region_id, task.fov)
        result.skipped = True
        return result

    try:
        probe = tifffile.imread(str(probe_path))
    except Exception as e:
        result.error = f"Probe read failed: {e}"
        worker_log.error("Probe TIFF read failed for %s: %s", probe_path, e)
        if task.on_missing == "fail":
            raise
        result.skipped = True
        return result

    if probe.ndim != 2:
        result.error = f"Unexpected probe shape {probe.shape} (expected 2D)"
        worker_log.error(result.error)
        if task.on_missing == "fail":
            raise RuntimeError(result.error)
        result.skipped = True
        return result

    y, x = probe.shape
    dtype = probe.dtype

    # Determine effective T size (trim if requested)
    if task.trim_to_last_observed_t:
        max_t = max((key[0] for key in task.tiff_index.keys()), default=-1)
        effective_t = max_t + 1 if max_t >= 0 else 0
        if effective_t == 0:
            worker_log.warning("Trim requested but FOV has no data — skipping")
            result.skipped = True
            return result
    else:
        effective_t = task.t_size

    out_array_path = _output_array_path(task.output_root, task.is_hcs, task.region_id, task.fov)
    group_dir = out_array_path.parent

    config = ZarrAcquisitionConfig(
        output_path=str(out_array_path),
        shape=(effective_t, task.c_size, task.z_size, y, x),
        dtype=dtype,
        pixel_size_um=task.pixel_size_um or 1.0,
        z_step_um=task.z_step_um,
        time_increment_s=task.time_increment_s,
        channel_names=task.channel_names,
        compression=task.compression,
        translation_um=task.translation_um,
        manifest_path=task.manifest_path,
    )
    writer = ZarrWriter(config)
    try:
        writer.initialize()
    except Exception as e:
        result.error = f"ZarrWriter.initialize failed: {e}"
        worker_log.error(result.error)
        if task.on_missing == "fail":
            raise
        return result

    try:
        zero_plane = np.zeros((y, x), dtype=dtype)
        for t in range(effective_t):
            for c in range(task.c_size):
                for z in range(task.z_size):
                    key = (t, z, c)  # per-(t, z, channel_idx)
                    tiff_path = task.tiff_index.get(key)
                    unix_s = task.frame_time_index.get(key, 0.0)
                    if tiff_path is None:
                        if task.on_missing == "fail":
                            raise RuntimeError(
                                f"Missing frame t={t} c={c} z={z} for FOV {task.region_id}/{task.fov}"
                            )
                        result.frames_missing += 1
                        result.missing_cells.append((t, c, z, 1))
                        if task.on_missing == "skip-fov":
                            # don't write anything more for this FOV
                            writer.abort()
                            result.skipped = True
                            return result
                        # zero-fill
                        writer.write_frame(zero_plane, t=t, c=c, z=z)
                        continue
                    try:
                        img = tifffile.imread(str(tiff_path))
                    except Exception as e:
                        worker_log.warning("TIFF read failed for %s: %s", tiff_path, e)
                        result.frames_missing += 1
                        result.missing_cells.append((t, c, z, 1))
                        if task.on_missing == "fail":
                            raise
                        if task.on_missing == "skip-fov":
                            writer.abort()
                            result.skipped = True
                            return result
                        writer.write_frame(zero_plane, t=t, c=c, z=z)
                        continue
                    if img.shape != (y, x):
                        worker_log.warning(
                            "Shape mismatch for %s: got %s expected %s — treating as missing",
                            tiff_path,
                            img.shape,
                            (y, x),
                        )
                        result.frames_missing += 1
                        result.missing_cells.append((t, c, z, 1))
                        writer.write_frame(zero_plane, t=t, c=c, z=z)
                        continue
                    writer.write_frame(img, t=t, c=c, z=z)
                    writer.record_frame_time(
                        t=t, c=c, z=z, unix_time_s=unix_s, channel_name=task.channel_names[c]
                    )
                    result.frames_found += 1
        writer.finalize()
    except Exception:
        worker_log.exception("Fatal error writing FOV %s/%d", task.region_id, task.fov)
        try:
            writer.abort()
        except Exception:
            pass
        result.error = traceback.format_exc(limit=3)
        if task.on_missing == "fail":
            raise
        return result

    # Log the group dir path for the summary
    worker_log.info(
        "FOV %s/%d: %d frames found, %d missing, wrote %s",
        task.region_id,
        task.fov,
        result.frames_found,
        result.frames_missing,
        group_dir,
    )
    return result


# -- Main pipeline -------------------------------------------------------------


def _build_fov_tasks(
    layout: AcquisitionLayout,
    tiff_index: Dict[Tuple[int, str, int, int, int], Path],
    frame_time_index: Dict[Tuple[int, str, int, int, int], float],
    output_root: Path,
    compression: ZarrCompression,
    on_missing: str,
    trim_to_last_observed_t: bool,
) -> List[FovTask]:
    tasks: List[FovTask] = []
    manifest_abs = layout.experiment_dir / "acquisition.yaml"
    c_size = len(layout.channel_names)
    for region_id in layout.region_names:
        fov_count = layout.region_fov_count.get(region_id, 0)
        region_translations = layout.fov_translations_um.get(region_id, {})
        for fov in range(fov_count):
            # Subset the tiff_index for this FOV (drop the region + fov keys)
            fov_tiff: Dict[Tuple[int, int, int], Path] = {}
            fov_times: Dict[Tuple[int, int, int], float] = {}
            for (t, region, f, z, c), path in tiff_index.items():
                if region == region_id and f == fov:
                    fov_tiff[(t, z, c)] = path
            for (t, region, f, z, c), ts in frame_time_index.items():
                if region == region_id and f == fov:
                    fov_times[(t, z, c)] = ts

            translation_um = region_translations.get(fov, (0.0, 0.0))
            group_dir = _output_group_dir(output_root, layout.is_hcs, region_id, fov)
            manifest_rel = _manifest_rel_path(group_dir, manifest_abs)

            tasks.append(
                FovTask(
                    region_id=region_id,
                    fov=fov,
                    t_size=layout.t_size,
                    c_size=c_size,
                    z_size=layout.z_size,
                    channel_names=layout.channel_names,
                    pixel_size_um=layout.pixel_size_um,
                    z_step_um=layout.z_step_um,
                    time_increment_s=layout.time_increment_s,
                    translation_um=translation_um,
                    is_hcs=layout.is_hcs,
                    input_dir=layout.experiment_dir,
                    output_root=output_root,
                    manifest_path=manifest_rel,
                    tiff_index=fov_tiff,
                    frame_time_index=fov_times,
                    compression=compression,
                    on_missing=on_missing,
                    trim_to_last_observed_t=trim_to_last_observed_t,
                )
            )
    return tasks


def _write_hcs_metadata(layout: AcquisitionLayout, output_root: Path) -> None:
    """Write plate + well metadata mirroring SaveZarrJob._write_hcs_metadata_if_needed."""
    if not layout.is_hcs:
        return
    rows_set = set()
    cols_set = set()
    wells: List[Tuple[str, int]] = []
    for well_id in layout.region_names:
        row, col = utils.parse_well_id(well_id)
        rows_set.add(row)
        cols_set.add(int(col))
        wells.append((row, int(col)))
    rows = sorted(rows_set)
    cols = sorted(cols_set)
    plate_path = os.path.join(str(output_root), "plate.ome.zarr")
    write_plate_metadata(plate_path, rows, cols, wells, plate_name="plate")
    for well_id in layout.region_names:
        row, col = utils.parse_well_id(well_id)
        well_path = os.path.join(str(output_root), "plate.ome.zarr", row, col)
        fov_count = layout.region_fov_count.get(well_id, 0)
        write_well_metadata(well_path, list(range(fov_count)))


def _write_missing_frames_csv(
    path: Path, missing: List[Tuple[str, int, int, int, int, int]]
) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["region", "fov", "t", "channel_index", "z", "missing"])
        for row in missing:
            w.writerow(row)


def _write_report(output_root: Path, report: dict) -> None:
    with (output_root / "repackage_report.json").open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)


# -- CLI -----------------------------------------------------------------------


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True, help="Experiment directory (contains acquisition.yaml).")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output root (default: {input}/repackaged).",
    )
    parser.add_argument(
        "--compression",
        choices=[c.value for c in ZarrCompression],
        default=ZarrCompression.BALANCED.value,
        help="Zarr compression preset (default: balanced).",
    )
    parser.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1), help="Parallel FOV workers.")
    parser.add_argument(
        "--on-missing",
        choices=["zero-fill", "skip-fov", "fail"],
        default="zero-fill",
        help="What to do when a per-(t,c,z) TIFF is missing (default: zero-fill).",
    )
    parser.add_argument(
        "--layout",
        choices=["auto", "hcs", "flat"],
        default="auto",
        help="Force HCS plate layout or flat layout; default auto-detects from acquisition.yaml.",
    )
    parser.add_argument(
        "--trim-to-last-observed-t",
        action="store_true",
        help="Set T in the output zarr to the last timepoint that has any data (default: use declared T).",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite an existing output root.")
    parser.add_argument("--dry-run", action="store_true", help="Walk inputs and print a summary; do not write anything.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable DEBUG logging.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    )

    input_dir = args.input.resolve()
    if not input_dir.is_dir():
        log.error("Input directory does not exist: %s", input_dir)
        return 2

    output_root = (args.output or (input_dir / "repackaged")).resolve()

    if output_root.exists():
        if not args.force and not args.dry_run:
            log.error(
                "Output root already exists: %s  (use --force to overwrite, or choose a different --output).",
                output_root,
            )
            return 2
        if args.force and not args.dry_run:
            log.info("Removing existing output root: %s", output_root)
            shutil.rmtree(output_root)

    log.info("Input:  %s", input_dir)
    log.info("Output: %s", output_root)

    compression = ZarrCompression(args.compression)
    log.info("Compression: %s", compression.value)

    t0 = time.perf_counter()
    layout = _resolve_layout(input_dir, args.layout)
    log.info(
        "Layout: %s, T=%d, Z=%d, channels=%d (%s), regions=%d, FOVs=%d, pixel_size=%.4f um",
        "HCS plate" if layout.is_hcs else "flat per-FOV",
        layout.t_size,
        layout.z_size,
        len(layout.channel_names),
        layout.channel_names,
        len(layout.region_names),
        sum(layout.region_fov_count.values()),
        layout.pixel_size_um,
    )

    # Build the exhaustive TIFF index once (cheaper than re-walking per FOV)
    tiff_index, _, _, _, _ = _scan_tiffs(input_dir, layout.region_names, layout.channel_names)
    frame_time_index = _load_frame_acquisition_times(input_dir, layout.channel_names)

    tasks = _build_fov_tasks(
        layout=layout,
        tiff_index=tiff_index,
        frame_time_index=frame_time_index,
        output_root=output_root,
        compression=compression,
        on_missing=args.on_missing,
        trim_to_last_observed_t=args.trim_to_last_observed_t,
    )
    log.info("Prepared %d FOV tasks (%d expected frames total)", len(tasks), sum(len(t.tiff_index) for t in tasks))

    if args.dry_run:
        log.info("Dry run: not writing zarr output. Exiting.")
        return 0

    output_root.mkdir(parents=True, exist_ok=True)

    # Copy acquisition.yaml into the output root so the _squid.manifest_path pointer resolves.
    src_yaml = input_dir / "acquisition.yaml"
    if src_yaml.is_file():
        shutil.copy2(src_yaml, output_root / "acquisition.yaml")

    _write_hcs_metadata(layout, output_root)

    results: List[FovResult] = []
    if args.jobs <= 1:
        for task in tasks:
            results.append(_process_one_fov(task))
    else:
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(_process_one_fov, t): t for t in tasks}
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception as e:
                    task = futures[fut]
                    log.exception("FOV worker crashed for %s/%d: %s", task.region_id, task.fov, e)
                    results.append(
                        FovResult(
                            region_id=task.region_id,
                            fov=task.fov,
                            error=str(e),
                            skipped=True,
                        )
                    )

    duration_s = time.perf_counter() - t0

    # Aggregate missing cells for CSV
    missing_rows: List[Tuple[str, int, int, int, int, int]] = []
    for r in results:
        for (t, c, z, m) in r.missing_cells:
            missing_rows.append((r.region_id, r.fov, t, c, z, m))
    if missing_rows:
        _write_missing_frames_csv(output_root / "missing_frames.csv", missing_rows)

    frames_found = sum(r.frames_found for r in results)
    frames_missing = sum(r.frames_missing for r in results)
    fovs_processed = sum(1 for r in results if not r.skipped and not r.error)
    fovs_skipped = sum(1 for r in results if r.skipped)
    fovs_failed = sum(1 for r in results if r.error and not r.skipped)

    report = {
        "input": str(input_dir),
        "output": str(output_root),
        "layout": "hcs" if layout.is_hcs else "flat",
        "t_size": layout.t_size,
        "z_size": layout.z_size,
        "channels": layout.channel_names,
        "regions": layout.region_names,
        "total_fovs": len(tasks),
        "fovs_processed": fovs_processed,
        "fovs_skipped": fovs_skipped,
        "fovs_failed": fovs_failed,
        "frames_found": frames_found,
        "frames_missing": frames_missing,
        "duration_s": duration_s,
        "compression": compression.value,
    }
    _write_report(output_root, report)

    log.info(
        "Done in %.1fs: %d FOVs processed, %d skipped, %d failed; %d frames written, %d missing",
        duration_s,
        fovs_processed,
        fovs_skipped,
        fovs_failed,
        frames_found,
        frames_missing,
    )

    return 0 if fovs_failed == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
