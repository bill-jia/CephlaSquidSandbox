#!/usr/bin/env python3
"""Backfill OME-Zarr experiment outputs to a network SMB share.

Parallels the live upload pipeline (``control.core.zarr_upload``) so it can
be used to:

  1. Upload a completed acquisition that wasn't streamed live.
  2. Catch up an in-progress acquisition whose remote stream is behind, or
     whose live upload was disabled when the run started.
  3. Smoke-test the upload pipeline against a known-good dataset before
     trusting it with a live run.

Layout discovery matches what ``ZarrWriterInfo`` produces:
  - HCS:     ``{exp}/plate.ome.zarr/{row}/{col}/{fov}/{level}/c/{t}/...``
  - non-HCS: ``{exp}/zarr/{region}/fov_{n}.ome.zarr/{level}/c/{t}/...``

For each FOV the script enumerates ``c/<t>/0/0/0/0`` shard files across all
pyramid levels, then groups them into per-``(t, fov)`` tasks (matching how
the live pipeline batches uploads). The same ``UploadWorker`` and
``upload_one_file`` primitive are used, so atomic rename, sha256
verification, and exponential-backoff retry are all identical to the live
path. A JSON-lines manifest is appended at
``<experiment_dir>/upload_manifest_backfill.jsonl`` — kept separate from the
live ``upload_manifest.jsonl`` so the two never collide.

Usage:
    python scripts/zarr_backfill_upload.py <experiment_dir> \
        --remote "\\\\server\\share\\dest_root" \
        [--delete-after-verify] [--in-progress] [--follow]
        [--poll-interval N] [--max-idle N] [--dry-run]

``--in-progress``  Skip the highest-numbered timepoint per FOV (assumed to
                   still be receiving writes). Also skips zarr.json files
                   that an active writer may rewrite on finalize.

``--follow``       Stay open after the initial pass; periodically rescan
                   the experiment for new timepoints. Implies
                   ``--in-progress`` while the writer is active. When every
                   FOV's ``zarr.json`` reports ``_squid.acquisition_complete
                   = true`` (set by the live writer at finalize), the script
                   runs one final pass that picks up the previously-skipped
                   highest timepoint and all metadata files, then exits.

``--poll-interval`` Seconds between rescans in ``--follow`` mode (default 30).
``--max-idle``      Exit ``--follow`` mode after this many seconds without
                    any new data appearing or any upload completing
                    (default 600; 0 = wait indefinitely).

``--delete-after-verify``  After all of a timepoint's shards verify on the
                           remote, delete the local copies. Default: off
                           (safe).
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from uuid import uuid4

# Allow running as `python scripts/zarr_backfill_upload.py` from the repo root.
_SOFTWARE_DIR = Path(__file__).resolve().parents[1]
if str(_SOFTWARE_DIR) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE_DIR))

import squid.logging  # noqa: E402

from control.core.zarr_upload import (  # noqa: E402
    UploadTarget,
    UploadTask,
    UploadResult,
    UploadWorker,
    local_to_remote_path,
)


log = squid.logging.get_logger("zarr_backfill_upload")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def find_fov_groups(experiment_dir: Path) -> List[Path]:
    """Return absolute paths of every FOV group directory in ``experiment_dir``.

    Detects both layouts:
      - non-HCS: ``zarr/<region>/fov_*.ome.zarr``
      - HCS:     ``plate.ome.zarr/<row>/<col>/<fov>``  (numeric ``fov`` dirs
                 under a 2-deep row/col tree containing ``zarr.json``)
    """
    fov_groups: List[Path] = []

    non_hcs_root = experiment_dir / "zarr"
    if non_hcs_root.is_dir():
        for region_dir in sorted(non_hcs_root.iterdir()):
            if not region_dir.is_dir():
                continue
            for fov_dir in sorted(region_dir.glob("fov_*.ome.zarr")):
                if (fov_dir / "zarr.json").is_file():
                    fov_groups.append(fov_dir)

    hcs_root = experiment_dir / "plate.ome.zarr"
    if hcs_root.is_dir():
        for row_dir in sorted(hcs_root.iterdir()):
            if not row_dir.is_dir():
                continue
            for col_dir in sorted(row_dir.iterdir()):
                if not col_dir.is_dir():
                    continue
                for fov_dir in sorted(col_dir.iterdir()):
                    if fov_dir.is_dir() and (fov_dir / "zarr.json").is_file():
                        fov_groups.append(fov_dir)

    return fov_groups


def parse_fov_identity(fov_group: Path, experiment_dir: Path) -> Tuple[str, int]:
    """Recover ``(region_id, fov_index)`` from a FOV group path."""
    rel = fov_group.relative_to(experiment_dir).as_posix()
    parts = rel.split("/")
    if parts[0] == "zarr":
        # zarr/<region>/fov_<n>.ome.zarr
        region_id = parts[1]
        stem = parts[2].split(".")[0]  # fov_<n>
        try:
            fov_idx = int(stem.split("_", 1)[1])
        except (IndexError, ValueError):
            fov_idx = 0
        return region_id, fov_idx
    if parts[0] == "plate.ome.zarr":
        # plate.ome.zarr/<row>/<col>/<fov>
        well_id = parts[1] + parts[2]
        try:
            fov_idx = int(parts[3])
        except ValueError:
            fov_idx = 0
        return well_id, fov_idx
    return "unknown", 0


def enumerate_levels(fov_group: Path) -> List[Path]:
    """Numeric subdirectories of ``fov_group``: the pyramid level dirs."""
    out: List[Path] = []
    for entry in sorted(fov_group.iterdir()):
        if entry.is_dir() and entry.name.isdigit():
            out.append(entry)
    return out


def enumerate_timepoints_for_level(level_dir: Path) -> List[int]:
    """Timepoint indices for which a shard file exists at this level."""
    c_dir = level_dir / "c"
    if not c_dir.is_dir():
        return []
    tps: List[int] = []
    for entry in sorted(c_dir.iterdir()):
        if not entry.is_dir():
            continue
        try:
            tp = int(entry.name)
        except ValueError:
            continue
        # Expect one shard file at c/<t>/0/0/0/0.
        shard_file = entry / "0" / "0" / "0" / "0"
        if shard_file.is_file():
            tps.append(tp)
    return tps


def check_fov_layout(fov_group: Path) -> Tuple[bool, str]:
    """Validate that ``fov_group``'s on-disk layout matches the assumptions
    in :func:`enumerate_timepoints_for_level` and :func:`build_tasks_for_fov`.

    Specifically: the level-0 array must be a 5D ``(T, C, Z, Y, X)`` zarr
    with an outer chunk grid of ``(1, C, Z, Y, X)`` — i.e. every chunk
    file under ``c/<t>/0/0/0/0`` must contain **all and only** the data
    for timepoint ``t``. This is what the current Squid writer produces;
    older writers (pre-commit ``5d6b34b2``) supported conditional layouts
    (FAST = no sharding, BALANCED = per-z-level sharding, 6D wellplate
    arrays) whose files live at different paths and can NOT be safely
    enumerated or deleted by this script.

    Returns ``(ok, reason)``. When ``ok=False``, ``reason`` is a short
    human-readable description; the caller should refuse to run.
    """
    level0_zj = fov_group / "0" / "zarr.json"
    if not level0_zj.is_file():
        return False, f"missing level-0 zarr.json at {level0_zj}"
    try:
        with open(level0_zj, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return False, f"unreadable {level0_zj}: {e}"

    shape = data.get("shape", [])
    if len(shape) != 5:
        return False, (
            f"unsupported array dimensionality: shape={shape} "
            f"(this script only handles 5D (T,C,Z,Y,X) arrays; "
            f"6D arrays from older writers use a different file layout)"
        )

    cg = data.get("chunk_grid", {}) or {}
    if cg.get("name") != "regular":
        return False, f"unexpected chunk_grid.name: {cg.get('name')!r}"
    chunk_shape = (cg.get("configuration", {}) or {}).get("chunk_shape", [])
    if len(chunk_shape) != 5:
        return False, f"unexpected outer chunk_shape length: {chunk_shape}"

    # The outer chunk must cover exactly one timepoint and all of C, Z, Y, X.
    # This is what makes `c/<t>/0/0/0/0` a complete per-timepoint shard.
    if chunk_shape[0] != 1 or list(chunk_shape[1:]) != list(shape[1:]):
        return False, (
            f"unexpected outer chunk_shape {chunk_shape} for shape {shape}; "
            f"this script assumes sharded layout with chunk_shape=(1, C, Z, Y, X) "
            f"so files at c/<t>/0/0/0/0 are per-timepoint shards. The dataset's "
            f"chunk grid suggests an older writer (FAST/NONE compression or "
            f"per-z-level sharding) — its data lives at different paths and "
            f"this script can NOT safely upload or delete it."
        )

    cke = data.get("chunk_key_encoding", {}) or {}
    if cke.get("name") != "default":
        return False, f"unexpected chunk_key_encoding.name: {cke.get('name')!r}"

    return True, "5D, sharded (1, C, Z, Y, X), default chunk-key encoding"


def fov_acquisition_complete(fov_group: Path) -> bool:
    """True if ``fov_group/zarr.json`` shows ``_squid.acquisition_complete``.

    Set by ``ZarrWriter.finalize()`` at the end of a live acquisition. We
    use this as the canonical "the writer is done, the dataset is stable"
    signal in ``--follow`` mode.
    """
    zj = fov_group / "zarr.json"
    if not zj.is_file():
        return False
    try:
        with open(zj, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    attrs = data.get("attributes", {}) or {}
    return bool((attrs.get("_squid", {}) or {}).get("acquisition_complete"))


def detect_acquisition_complete(fov_groups: List[Path]) -> bool:
    """True iff every FOV in the experiment reports ``acquisition_complete``."""
    if not fov_groups:
        return False
    return all(fov_acquisition_complete(fg) for fg in fov_groups)


def gather_metadata_files(fov_group: Path) -> List[Path]:
    """Return the per-FOV metadata files (group zarr.json, level zarr.jsons,
    frame_times array + chunk). These are uploaded once at the end of the
    backfill so the remote ends up with a complete, readable tree."""
    out: List[Path] = []
    group_json = fov_group / "zarr.json"
    if group_json.is_file():
        out.append(group_json)
    for level in enumerate_levels(fov_group):
        lj = level / "zarr.json"
        if lj.is_file():
            out.append(lj)
    ft = fov_group / "frame_times"
    if ft.is_dir():
        ftj = ft / "zarr.json"
        if ftj.is_file():
            out.append(ftj)
        ft_chunk = ft / "c" / "0" / "0" / "0"
        if ft_chunk.is_file():
            out.append(ft_chunk)
    return out


# ---------------------------------------------------------------------------
# Task building
# ---------------------------------------------------------------------------


def build_tasks_for_fov(
    fov_group: Path,
    experiment_dir: Path,
    target: UploadTarget,
    in_progress: bool,
    *,
    skip_log_state: Optional[Set[Tuple[str, int, int]]] = None,
) -> List[UploadTask]:
    """Build one UploadTask per timepoint for the given FOV.

    ``skip_log_state``, when provided, is a caller-owned set used to dedupe
    the "skipping in-flight t=N" INFO log. We add ``(region_id, fov_idx,
    skipped_tp)`` to it on each first encounter and skip the log on
    subsequent encounters. In ``--follow`` mode the caller owns one such
    set across the whole run, so each FOV-timepoint skip is logged exactly
    once (and a fresh INFO line appears when the writer advances to a new
    timepoint).
    """
    region_id, fov_idx = parse_fov_identity(fov_group, experiment_dir)
    level_dirs = enumerate_levels(fov_group)
    if not level_dirs:
        return []

    # Intersection of timepoints present across all levels — only treat a
    # timepoint as ready if every level has its shard. Pyramids are written
    # inline, so under normal operation this is just the level-0 set, but a
    # crashed run may have ragged levels.
    per_level_tps: List[Set[int]] = [
        set(enumerate_timepoints_for_level(L)) for L in level_dirs
    ]
    ready_tps: Set[int] = set.intersection(*per_level_tps) if per_level_tps else set()

    if in_progress and ready_tps:
        max_tp = max(ready_tps)
        ready_tps.discard(max_tp)
        key = (region_id, fov_idx, max_tp)
        if skip_log_state is None or key not in skip_log_state:
            log.info(
                f"{region_id} fov={fov_idx}: skipping in-flight t={max_tp} "
                f"(in-progress mode)"
            )
            if skip_log_state is not None:
                skip_log_state.add(key)

    tasks: List[UploadTask] = []
    for tp in sorted(ready_tps):
        files: List[Tuple[str, str]] = []
        deletable: set = set()
        for level in level_dirs:
            shard = level / "c" / str(tp) / "0" / "0" / "0" / "0"
            if shard.is_file():
                local = str(shard)
                files.append((local, local_to_remote_path(local, target.local_base, target.remote_root)))
                # Per-timepoint shards are exclusive to this (t, fov); safe
                # to delete after sha256 verify.
                deletable.add(local)
        if not files:
            continue
        tasks.append(UploadTask(
            task_id=str(uuid4()),
            time_point=tp,
            region_id=region_id,
            fov=fov_idx,
            files=files,
            deletable_local_paths=deletable,
        ))
    return tasks


def build_metadata_task(
    fov_group: Path,
    experiment_dir: Path,
    target: UploadTarget,
    in_progress: bool,
) -> Optional[UploadTask]:
    """Build a single task for the FOV's metadata files.

    Metadata files (group ``zarr.json``, per-level ``zarr.json``,
    ``frame_times`` array + chunk) are uploaded to make the remote tree
    readable but are **never** marked as deletable: a live writer needs
    them in place. In ``--in-progress`` mode we skip them entirely to avoid
    racing with the writer's potential finalize-time rewrite of
    ``zarr.json``.
    """
    if in_progress:
        return None
    files: List[Tuple[str, str]] = []
    stable_locals: set = set()
    for f in gather_metadata_files(fov_group):
        local = str(f)
        files.append((local, local_to_remote_path(local, target.local_base, target.remote_root)))
        # Metadata files may race with a concurrent live writer; force a
        # stable-read check on every one.
        stable_locals.add(local)
    if not files:
        return None
    region_id, fov_idx = parse_fov_identity(fov_group, experiment_dir)
    return UploadTask(
        task_id=str(uuid4()),
        time_point=-1,  # sentinel for metadata-only task
        region_id=region_id,
        fov=fov_idx,
        files=files,
        deletable_local_paths=set(),  # never delete shared metadata
        stable_read_paths=stable_locals,
    )


# ---------------------------------------------------------------------------
# Local deletion
# ---------------------------------------------------------------------------


def delete_verified_locals(
    result: UploadResult,
    log_,
) -> int:
    """Delete only the local files the worker tagged as safe-to-delete.

    Strictly uses ``result.deletable_uploaded_paths``: shared metadata
    files (``zarr.json``, ``frame_times``) are never touched, even on a
    completed dataset, so the local tree remains a self-consistent OME-NGFF
    pointer to whatever data has not yet been pruned.
    """
    deleted = 0
    for local_path in result.deletable_uploaded_paths:
        try:
            if os.path.isfile(local_path):
                os.remove(local_path)
                deleted += 1
        except OSError as e:
            log_.warning(f"Failed to delete {local_path}: {e}")
    return deleted


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def run_backfill(
    experiment_dir: Path,
    remote_root: str,
    delete_after_verify: bool,
    in_progress: bool,
    dry_run: bool,
) -> int:
    """Discover and upload. Returns the number of failed files."""
    if not experiment_dir.is_dir():
        log.error(f"Experiment directory not found: {experiment_dir}")
        return 1

    fov_groups = find_fov_groups(experiment_dir)
    if not fov_groups:
        log.error(f"No OME-Zarr FOV groups found under {experiment_dir}")
        return 1
    log.info(f"Found {len(fov_groups)} FOV group(s) under {experiment_dir}")

    # Refuse to run against unrecognized layouts — see ``check_fov_layout``.
    if not _validate_all_layouts(fov_groups):
        return 2

    target = UploadTarget(
        enabled=True,
        remote_root=remote_root,
        local_base=str(experiment_dir),
        delete_after_verify=delete_after_verify,
    )

    # Build the full task list up front so we know what we're committing to.
    skip_log_state: Set[Tuple[str, int, int]] = set()
    tasks: List[UploadTask] = []
    metadata_tasks: List[UploadTask] = []
    for fov_group in fov_groups:
        tasks.extend(build_tasks_for_fov(
            fov_group, experiment_dir, target, in_progress,
            skip_log_state=skip_log_state,
        ))
        md = build_metadata_task(fov_group, experiment_dir, target, in_progress)
        if md is not None:
            metadata_tasks.append(md)

    total_files = sum(len(t.files) for t in tasks) + sum(len(t.files) for t in metadata_tasks)
    total_bytes = 0
    for t in tasks + metadata_tasks:
        for local, _ in t.files:
            try:
                total_bytes += os.path.getsize(local)
            except OSError:
                pass
    log.info(
        f"Plan: {len(tasks)} shard task(s) + {len(metadata_tasks)} metadata task(s) "
        f"= {total_files} files, {total_bytes / 1e9:.2f} GB"
    )

    if dry_run:
        for t in tasks + metadata_tasks:
            log.info(f"  task t={t.time_point} region={t.region_id} fov={t.fov} "
                     f"files={len(t.files)}")
        return 0

    # Upload acquisition.yaml first (small, safe to upload immediately).
    acq_yaml = experiment_dir / "acquisition.yaml"
    if acq_yaml.is_file():
        local = str(acq_yaml)
        files = [(local, local_to_remote_path(local, target.local_base, target.remote_root))]
        metadata_tasks.insert(0, UploadTask(
            task_id=str(uuid4()),
            time_point=-1,
            region_id="",
            fov=-1,
            files=files,
        ))

    manifest_path = str(experiment_dir / "upload_manifest_backfill.jsonl")
    worker = UploadWorker(target=target, manifest_path=manifest_path)
    worker.start()
    log.info(f"UploadWorker started (pid={worker.pid}), manifest -> {manifest_path}")

    # Initial local-size snapshot — reported at the end so the operator
    # sees the net effect of the run.
    initial_size = measure_local_size_bytes(experiment_dir)
    log.info(f"local archive at start: {_format_bytes(initial_size)}")

    # Submit every task.
    for t in tasks:
        worker.submit(t)
    for t in metadata_tasks:
        worker.submit(t)

    # Drain results, applying deletion as bundles verify. Wrapped in
    # try/except/finally so KeyboardInterrupt and SystemExit both go
    # through the orderly drain+shutdown path instead of dumping a
    # traceback and leaving the UploadWorker as an orphan.
    expected_results = len(tasks) + len(metadata_tasks)
    received = 0
    failed_files = 0
    deleted_local = 0
    interrupted = False
    t_start = time.time()
    try:
        while received < expected_results:
            try:
                result: UploadResult = worker.output_queue.get(timeout=60.0)
            except queue.Empty:
                elapsed = int(time.time() - t_start)
                log.info(f"Waiting on uploads... ({received}/{expected_results} done, {elapsed}s elapsed)")
                continue
            received += 1
            if not result.success:
                failed_files += len(result.failed_paths)
                log.error(
                    f"Task {result.task_id} (t={result.time_point} region={result.region_id} "
                    f"fov={result.fov}) FAILED: {result.error}"
                )
            elif delete_after_verify:
                deleted_local += delete_verified_locals(result, log)
            if received % 10 == 0 or received == expected_results:
                log.info(f"Progress: {received}/{expected_results} task(s) processed")
    except (KeyboardInterrupt, SystemExit) as e:
        interrupted = True
        sig = "Ctrl-C" if isinstance(e, KeyboardInterrupt) else "termination signal"
        log.info(
            f"{sig} received at {received}/{expected_results} task(s); "
            f"draining in-flight uploads and exiting."
        )
    finally:
        # If we were interrupted, opportunistically drain whatever results
        # the worker managed to enqueue before its own subprocess died.
        if interrupted:
            try:
                deadline = time.time() + 60.0
                while time.time() < deadline:
                    timeout = max(0.0, min(0.5, deadline - time.time()))
                    try:
                        result = worker.output_queue.get(timeout=timeout)
                    except queue.Empty:
                        continue
                    except (OSError, ValueError):
                        break
                    received += 1
                    if not result.success:
                        failed_files += len(result.failed_paths)
                    elif delete_after_verify:
                        deleted_local += delete_verified_locals(result, log)
            except (KeyboardInterrupt, SystemExit):
                log.warning("Second interrupt during drain; bringing worker down immediately.")
        worker.shutdown()
        worker.join(timeout=30.0)
        if worker.is_alive():
            log.warning("UploadWorker did not exit cleanly; terminating")
            worker.terminate()
            worker.join(timeout=5.0)
        # Release feeder-thread resources so Python can exit (see comment in
        # ``UploadWorker.release_queue_resources``).
        worker.release_queue_resources()
        try:
            worker.close()
        except Exception:
            pass

        # Final size report so the operator sees the net effect of the run.
        try:
            final_size = measure_local_size_bytes(experiment_dir)
            log.info(
                f"local archive at exit: {_format_bytes(final_size)} "
                f"(net {_format_signed_bytes(final_size - initial_size)})"
            )
        except Exception as e:
            log.debug(f"final size measurement failed: {e}")

    log.info(
        f"Done. {received}/{expected_results} task(s) processed, "
        f"{failed_files} file(s) failed, {deleted_local} local file(s) deleted."
        + (" (INTERRUPTED)" if interrupted else "")
    )
    return failed_files


def measure_local_size_bytes(path: Path) -> int:
    """Best-effort recursive size of all files under ``path``.

    Missing files (e.g. just-deleted shards) are silently ignored — we
    only need a coarse number for the periodic "growing/shrinking" log.
    """
    total = 0
    for root, _dirs, files in os.walk(path):
        for fname in files:
            try:
                total += os.path.getsize(os.path.join(root, fname))
            except OSError:
                pass
    return total


def _format_bytes(n: float) -> str:
    """Compact human-readable size, signed (for deltas)."""
    sign = "-" if n < 0 else "+"
    a = abs(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if a < 1024 or unit == "TB":
            return f"{sign}{a:.2f} {unit}" if sign == "-" else f"{a:.2f} {unit}"
        a /= 1024
    return f"{a:.2f} TB"


def _format_signed_bytes(n: float) -> str:
    """Compact signed human-readable size (always includes +/-)."""
    sign = "-" if n < 0 else "+"
    a = abs(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if a < 1024 or unit == "TB":
            return f"{sign}{a:.2f} {unit}"
        a /= 1024
    return f"{sign}{a:.2f} TB"


def _validate_all_layouts(fov_groups: List[Path]) -> bool:
    """Run ``check_fov_layout`` over every FOV group; log and return False on any failure.

    Logs each failure with the offending FOV path so the operator can decide
    whether the dataset was produced by an older writer (pre-commit
    ``5d6b34b2``) or by something unrelated. We never proceed with deletion
    against an unrecognized layout.
    """
    bad: List[Tuple[Path, str]] = []
    for fg in fov_groups:
        ok, reason = check_fov_layout(fg)
        if not ok:
            bad.append((fg, reason))
    if bad:
        log.error(
            f"Layout check failed for {len(bad)} of {len(fov_groups)} FOV group(s). "
            f"This script ONLY supports the current Squid writer's 5D sharded layout "
            f"(chunk_grid=(1, C, Z, Y, X), default chunk-key encoding). Older datasets "
            f"(pre-commit 5d6b34b2 'Enable zarr writing') used a different layout per "
            f"compression mode and cannot be safely uploaded or deleted by this tool. "
            f"Refusing to run."
        )
        for fg, reason in bad[:5]:
            log.error(f"  {fg}: {reason}")
        if len(bad) > 5:
            log.error(f"  ... and {len(bad) - 5} more")
        return False
    return True


def run_backfill_follow(
    experiment_dir: Path,
    remote_root: str,
    delete_after_verify: bool,
    poll_interval_s: float,
    max_idle_s: float,
    drain_stall_window_s: float = 300.0,
    drain_timeout_s: float = 0.0,
) -> int:
    """Follow-mode loop: scan, submit, drain, sleep — until done.

    Stays open and rescans the experiment directory every
    ``poll_interval_s`` seconds for new timepoints. Treats the run as
    in-progress (skips the highest timepoint per FOV, skips metadata)
    while it can't yet observe ``_squid.acquisition_complete = True`` in
    every FOV's ``zarr.json``. Once the writer flips that flag, runs one
    final non-in-progress pass to pick up the previously-skipped highest
    timepoint plus every metadata file, drains, and exits.

    Returns the cumulative number of failed files.
    """
    if not experiment_dir.is_dir():
        log.error(f"Experiment directory not found: {experiment_dir}")
        return 1

    target = UploadTarget(
        enabled=True,
        remote_root=remote_root,
        local_base=str(experiment_dir),
        delete_after_verify=delete_after_verify,
    )
    # First-pass layout validation. In follow mode we may have started
    # before the writer initialized any FOVs, in which case there's nothing
    # to validate yet — the per-iteration discovery loop revalidates as
    # FOVs appear.
    initial_groups = find_fov_groups(experiment_dir)
    if initial_groups and not _validate_all_layouts(initial_groups):
        return 2
    manifest_path = str(experiment_dir / "upload_manifest_backfill.jsonl")
    worker = UploadWorker(target=target, manifest_path=manifest_path)
    worker.start()
    log.info(
        f"UploadWorker started (pid={worker.pid}), manifest -> {manifest_path}. "
        f"Follow mode: poll_interval={poll_interval_s}s, "
        f"max_idle={max_idle_s if max_idle_s > 0 else 'forever'}s"
    )

    # In-memory deduplication: which (region, fov, tp) we've already
    # submitted; which (region, fov, tp) skips we've already logged.
    submitted_shards: Set[Tuple[str, int, int]] = set()
    skip_log_state: Set[Tuple[str, int, int]] = set()
    pending_task_ids: Set[str] = set()
    completed_results = 0
    failed_files = 0
    deleted_local = 0
    final_pass_done = False

    # Progress timestamps. Idle = "no submissions AND no completed results"
    # for max_idle_s. Treating result drainage as progress means a slow but
    # active upload pipeline does NOT trip the idle timeout.
    now0 = time.time()
    last_submission_time = now0
    last_result_time = now0
    last_iteration_log_time = now0

    # Metadata refresh throttling: re-uploading 189 zarr.json files every
    # poll wastes bandwidth and pads ``pending_task_ids``. The metadata
    # changes slowly (only on writer init + finalize), so a much longer
    # cadence is fine; the post-finalize pass is the authoritative version.
    metadata_refresh_interval_s = max(5 * 60.0, poll_interval_s * 10)
    last_metadata_submit_time = 0.0  # force first submission

    # Local-disk size telemetry. Measured every ``size_report_interval_s``
    # seconds. os.walk on a multi-TB local SSD usually runs in well under
    # one poll interval; we sample sparsely to avoid I/O contention with
    # the writer.
    size_report_interval_s = max(60.0, poll_interval_s * 2)
    log.info("Measuring initial local archive size (one-shot)...")
    last_size_bytes = measure_local_size_bytes(experiment_dir)
    last_size_check_time = time.time()
    log.info(f"local archive: {_format_bytes(last_size_bytes)}")

    def drain_available(deadline: float) -> int:
        """Pull every result available before ``deadline``. Returns count."""
        nonlocal failed_files, deleted_local, completed_results, last_result_time
        n = 0
        while time.time() < deadline:
            timeout = max(0.0, min(0.5, deadline - time.time()))
            try:
                result: UploadResult = worker.output_queue.get(timeout=timeout)
            except queue.Empty:
                continue
            except (OSError, ValueError):
                break
            n += 1
            completed_results += 1
            last_result_time = time.time()
            pending_task_ids.discard(result.task_id)
            if not result.success:
                failed_files += len(result.failed_paths)
                log.error(
                    f"Task {result.task_id} (t={result.time_point} "
                    f"region={result.region_id} fov={result.fov}) FAILED: {result.error}"
                )
            elif delete_after_verify:
                deleted_local += delete_verified_locals(result, log)
        return n

    def maybe_report_local_size() -> None:
        """Log a net-growth/shrink summary every ``size_report_interval_s``."""
        nonlocal last_size_bytes, last_size_check_time
        now = time.time()
        elapsed = now - last_size_check_time
        if elapsed < size_report_interval_s:
            return
        current = measure_local_size_bytes(experiment_dir)
        delta = current - last_size_bytes
        rate = delta / max(elapsed, 1e-6)  # bytes/sec
        if delta > 0:
            direction = "growing — writer is ahead of uploader"
        elif delta < 0:
            direction = "shrinking — uploads are reclaiming space"
        else:
            direction = "steady"
        log.info(
            f"local archive: {_format_bytes(current)} "
            f"({_format_signed_bytes(delta)} over {int(elapsed)}s, "
            f"{_format_signed_bytes(rate)}/s; {direction})"
        )
        last_size_bytes = current
        last_size_check_time = now

    # Initialize before the try so an unhandled exception in the loop body
    # still lets the finally clause read it without raising NameError.
    interrupted = False
    try:
        while True:
            t_loop_start = time.time()
            fov_groups = find_fov_groups(experiment_dir)
            if not fov_groups:
                log.warning(
                    f"No OME-Zarr FOV groups under {experiment_dir} yet; "
                    f"waiting for writer to initialize..."
                )
            else:
                # Re-validate layout each iteration. New FOVs may have
                # appeared since the last pass (the writer initializes a
                # new FOV's zarr.json before the first SaveZarrJob lands).
                # Any mismatch aborts the whole follow run — we never want
                # to start uploading-then-deleting against an unrecognized
                # layout while a writer is active.
                if not _validate_all_layouts(fov_groups):
                    log.error("Aborting follow mode due to layout mismatch.")
                    break
            acq_complete = detect_acquisition_complete(fov_groups)
            in_progress = not acq_complete

            # 1. Submit new per-timepoint shard tasks.
            submitted_now = 0
            for fov_group in fov_groups:
                for task in build_tasks_for_fov(
                    fov_group, experiment_dir, target,
                    in_progress=in_progress,
                    skip_log_state=skip_log_state,
                ):
                    key = (task.region_id, task.fov, task.time_point)
                    if key in submitted_shards:
                        continue
                    worker.submit(task)
                    submitted_shards.add(key)
                    pending_task_ids.add(task.task_id)
                    submitted_now += 1
            if submitted_now:
                last_submission_time = time.time()

            # 2. Refresh metadata at a much slower cadence than the poll
            # interval. Metadata files change only at writer init/finalize,
            # so re-uploading them every 30 s wastes bandwidth and inflates
            # ``pending_task_ids``. The final post-finalize pass is the
            # authoritative version regardless.
            metadata_now = 0
            now = time.time()
            if now - last_metadata_submit_time >= metadata_refresh_interval_s:
                for fov_group in fov_groups:
                    task = build_metadata_task(
                        fov_group, experiment_dir, target, in_progress=False
                    )
                    if task is None:
                        continue
                    worker.submit(task)
                    pending_task_ids.add(task.task_id)
                    metadata_now += 1
                last_metadata_submit_time = now
                if metadata_now:
                    last_submission_time = now

            if submitted_now or metadata_now:
                log.info(
                    f"Submitted {submitted_now} shard task(s) + "
                    f"{metadata_now} metadata task(s); "
                    f"pending={len(pending_task_ids)}, "
                    f"acq_complete={acq_complete}"
                )

            # 3. Drain results for up to one poll interval. Every received
            # result updates ``last_result_time`` inside ``drain_available``,
            # which is what max_idle keys off of — so a slow-but-active
            # upload pipeline never trips the idle timeout.
            drain_deadline = t_loop_start + poll_interval_s
            drained = drain_available(drain_deadline)

            # 3b. Periodic size report.
            maybe_report_local_size()

            # 3c. Per-iteration heartbeat so the operator can see the loop
            # is alive. Logged unconditionally whenever something happened
            # this iteration, and at most once per minute when idle (so the
            # log stays informative without spamming during quiet windows).
            now = time.time()
            iter_elapsed = now - t_loop_start
            something_happened = (
                submitted_now > 0 or metadata_now > 0 or drained > 0
            )
            heartbeat_due = (now - last_iteration_log_time) >= 60.0
            if something_happened or heartbeat_due:
                log.info(
                    f"tick: iter={iter_elapsed:.1f}s "
                    f"submitted_now={submitted_now}+{metadata_now}meta "
                    f"drained={drained} pending={len(pending_task_ids)} "
                    f"results={completed_results} deleted_files={deleted_local} "
                    f"failed={failed_files} acq_complete={acq_complete}"
                )
                last_iteration_log_time = now

            # 4. Exit logic.
            if acq_complete and not pending_task_ids:
                if not final_pass_done:
                    # One final non-in-progress sweep: pick up the
                    # previously-skipped highest timepoint + final metadata
                    # snapshot post-finalize.
                    log.info(
                        "Writer reports acquisition_complete; running final pass."
                    )
                    final_count = 0
                    for fov_group in fov_groups:
                        for task in build_tasks_for_fov(
                            fov_group, experiment_dir, target,
                            in_progress=False,
                            skip_log_state=skip_log_state,
                        ):
                            key = (task.region_id, task.fov, task.time_point)
                            if key in submitted_shards:
                                continue
                            worker.submit(task)
                            submitted_shards.add(key)
                            pending_task_ids.add(task.task_id)
                            final_count += 1
                        # Metadata is rewritten by finalize; re-submit one
                        # last clean snapshot.
                        task = build_metadata_task(
                            fov_group, experiment_dir, target, in_progress=False
                        )
                        if task is not None:
                            worker.submit(task)
                            pending_task_ids.add(task.task_id)
                            final_count += 1
                    log.info(f"Final pass: submitted {final_count} task(s)")
                    final_pass_done = True
                    last_submission_time = time.time()
                    # Loop again to drain the final pass.
                    continue
                else:
                    log.info("Final pass complete; exiting follow mode.")
                    break

            # max_idle = no submissions AND no completed results for N seconds.
            # An active upload pipeline keeps last_result_time fresh, so this
            # only trips when the writer has stopped producing data AND the
            # worker has nothing left to drain.
            idle_for = now - max(last_submission_time, last_result_time)
            if max_idle_s > 0 and idle_for > max_idle_s:
                log.warning(
                    f"No submissions or completed results for {int(idle_for)}s "
                    f"(max_idle={max_idle_s}s); exiting follow mode. "
                    f"{len(pending_task_ids)} task(s) still pending."
                )
                break

    except (KeyboardInterrupt, SystemExit) as e:
        sig = "Ctrl-C" if isinstance(e, KeyboardInterrupt) else "termination signal"
        log.info(f"{sig} received; draining in-flight uploads and exiting.")
        interrupted = True
    finally:
        # Drain strategy depends on why we're shutting down:
        #   * Clean exit (acq_complete + final pass done): the worker has
        #     nothing left to do, brief drain is sufficient.
        #   * max_idle / loop-break with pending tasks: the worker MAY
        #     still be doing real work — keep draining as long as new
        #     results land. The stall-window is the only criterion that
        #     abandons work; the wallclock budget defaults to "no cap" so
        #     a large backlog at modest upload speed is allowed to finish
        #     instead of being truncated. If the user wants a hard cap
        #     they pass ``--drain-timeout``.
        #   * Ctrl-C / SIGTERM: the subprocess got the same signal and is
        #     likely already dying; do a short drain to pick up anything
        #     already enqueued, then shut down.
        if interrupted:
            stall_window_s = 30.0
            drain_budget_s = 60.0
        else:
            stall_window_s = drain_stall_window_s
            drain_budget_s = drain_timeout_s  # 0 = unbounded
        try:
            if pending_task_ids:
                budget_str = (
                    f"{drain_budget_s:.0f}s wallclock"
                    if drain_budget_s > 0
                    else "no wallclock cap"
                )
                log.info(
                    f"Draining {len(pending_task_ids)} pending task(s) "
                    f"(stall window {stall_window_s:.0f}s, {budget_str})..."
                )
            t_drain_start = time.time()
            t_last_drained = time.time()
            while pending_task_ids:
                now = time.time()
                if drain_budget_s > 0 and now - t_drain_start > drain_budget_s:
                    log.warning(
                        f"Drain budget exhausted ({drain_budget_s:.0f}s); "
                        f"{len(pending_task_ids)} task(s) abandoned. "
                        f"Re-run the script to retry; remote-side atomic renames "
                        f"mean re-uploads overwrite cleanly without torn state."
                    )
                    break
                if now - t_last_drained > stall_window_s:
                    log.warning(
                        f"No new results for {stall_window_s:.0f}s during drain; "
                        f"worker appears stalled. {len(pending_task_ids)} task(s) abandoned."
                    )
                    break
                drained_here = drain_available(now + 5.0)
                if drained_here:
                    t_last_drained = time.time()
                    log.info(
                        f"drain: results={completed_results} pending={len(pending_task_ids)} "
                        f"deleted_files={deleted_local}"
                    )
        except (KeyboardInterrupt, SystemExit):
            log.warning("Second interrupt during drain; bringing worker down immediately.")
        worker.shutdown()
        worker.join(timeout=60.0)
        if worker.is_alive():
            log.warning("UploadWorker did not exit within 60s; terminating")
            worker.terminate()
            worker.join(timeout=5.0)
        # Release feeder-thread resources so the Python interpreter can
        # exit. Without this, abandoned items in the input queue (e.g.
        # 22k tasks after a drain-budget abandonment) prevent the parent's
        # feeder thread from joining and Python hangs at shutdown.
        worker.release_queue_resources()
        try:
            # Available on Python 3.7+. Releases process handle resources;
            # silently ignore on older builds.
            worker.close()
        except Exception:
            pass

        # Final size report so the operator sees the net effect of the run.
        try:
            final_size = measure_local_size_bytes(experiment_dir)
            net_delta = final_size - last_size_bytes
            log.info(
                f"local archive at exit: {_format_bytes(final_size)} "
                f"(net {_format_signed_bytes(net_delta)} since last sample)"
            )
        except Exception as e:
            log.debug(f"final size measurement failed: {e}")

    log.info(
        f"Follow mode done. {failed_files} file(s) failed, "
        f"{deleted_local} local file(s) deleted, "
        f"{len(pending_task_ids)} task(s) abandoned in flight."
    )
    return failed_files


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("experiment_dir", type=Path, help="Local experiment directory (contains zarr/ or plate.ome.zarr/)")
    parser.add_argument("--remote", required=True, help=r"Remote destination root, e.g. \\server\share\dest")
    parser.add_argument("--delete-after-verify", action="store_true",
                        help="Delete local shard files after the remote copy is sha256-verified.")
    parser.add_argument("--in-progress", action="store_true",
                        help="Skip the highest-numbered timepoint per FOV and all per-FOV zarr.json files. "
                             "Safe to run alongside an active acquisition writer.")
    parser.add_argument("--follow", action="store_true",
                        help="Stay open and periodically rescan for new timepoints. Implies "
                             "--in-progress while the writer is active. Exits cleanly after one "
                             "final pass when every FOV reports acquisition_complete=true, or "
                             "after --max-idle seconds with no progress.")
    parser.add_argument("--poll-interval", type=float, default=30.0,
                        help="Seconds between rescans in --follow mode (default 30).")
    parser.add_argument("--max-idle", type=float, default=600.0,
                        help="Exit --follow mode after this many seconds without progress "
                             "(default 600; 0 = wait forever).")
    parser.add_argument("--drain-stall-window", type=float, default=300.0,
                        help="During shutdown drain, abandon pending uploads after this "
                             "many seconds with no new completed results (default 300; "
                             "i.e. the worker is considered stalled after 5 min of silence). "
                             "Active uploads keep this window fresh.")
    parser.add_argument("--drain-timeout", type=float, default=0.0,
                        help="Hard wallclock cap on the shutdown drain in seconds "
                             "(default 0 = no cap; the stall window is the only "
                             "criterion). Set this if you want the script to give up "
                             "on a backlog after a known time regardless of progress.")
    parser.add_argument("--dry-run", action="store_true", help="List planned uploads and exit.")
    args = parser.parse_args(argv)

    if args.follow:
        if args.dry_run:
            log.error("--dry-run is not supported with --follow")
            return 2
        return run_backfill_follow(
            experiment_dir=args.experiment_dir.resolve(),
            remote_root=args.remote,
            delete_after_verify=args.delete_after_verify,
            poll_interval_s=args.poll_interval,
            max_idle_s=args.max_idle,
            drain_stall_window_s=args.drain_stall_window,
            drain_timeout_s=args.drain_timeout,
        )

    return run_backfill(
        experiment_dir=args.experiment_dir.resolve(),
        remote_root=args.remote,
        delete_after_verify=args.delete_after_verify,
        in_progress=args.in_progress,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
