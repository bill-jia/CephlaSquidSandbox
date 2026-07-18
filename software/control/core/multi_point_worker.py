import atexit
import csv
import json
import logging
import os
import queue
import threading
import time
from typing import Callable, Dict, List, NamedTuple, Optional, Set, Tuple, Type
from datetime import datetime

import imageio as iio
import numpy as np
import pandas as pd

from control._def import *
from control._def import DOWNSAMPLED_VIEW_JOB_TIMEOUT_S, DOWNSAMPLED_VIEW_IDLE_TIMEOUT_S
import control._def
from control import utils
from control.slack_notifier import TimepointStats, AcquisitionStats
from control.core.auto_focus_controller import AutoFocusController
from control.core.laser_auto_focus_controller import LaserAutofocusController
from control.core.live_controller import LiveController
from control.core.multi_point_utils import (
    AcquisitionParameters,
    MultiPointControllerFunctions,
    OverallProgressUpdate,
    RegionProgressUpdate,
    PlateViewInit,
    PlateViewUpdate,
)
from control.core.objective_store import ObjectiveStore
from control.microcontroller import Microcontroller
from control.microscope import Microscope
from control.piezo import PiezoStage
from control.models.observation_state import ObservationState
from control.core.waveform_observation_state import (
    build_pulse_waveform_for_state,
    nidaq_lines_for_state,
)
from control.core.waveform_capture import (
    apply_illumination_for_waveform_capture,
    arm_nidaq_pulse_for_capture,
)
from control.nidaq import TriggerSource
from squid.abc import AbstractCamera, CameraFrame, CameraFrameFormat
import squid.logging
import control.core.job_processing
from control.core.job_processing import ZarrWriteResult
from control.core.job_processing import (
    CaptureInfo,
    SaveImageJob,
    SaveOMETiffJob,
    SaveZarrJob,
    ZarrWriterInfo,
    AcquisitionInfo,
    Job,
    JobImage,
    JobRunner,
    JobResult,
    DownsampledViewJob,
    DownsampledViewResult,
    FlushAndStageUploadJob,
    BarrierResult,
    PostprocessJob,
    PostprocessResult,
    PostprocessWarmupJob,
    PostprocessWarmupResult,
    append_frame_acquisition_time_csv,
)
from control.core.zarr_upload import (
    UploadTarget,
    UploadTask,
    UploadWorker,
    UploadResult,
    drain_output_queue_nonblocking,
    local_to_remote_path,
)
from control.core.downsampled_views import (
    DownsampledViewManager,
    calculate_overlap_pixels,
    parse_well_id,
    ensure_plate_resolution_in_well_resolutions,
)
from control.core.backpressure import BackpressureController, BackpressureValues
from squid.config import CameraPixelFormat

# Module-level logger for static methods
_log = squid.logging.get_logger(__name__)

# Time budget for the JobRunner subprocess to flush + finalize all zarr writers
# during shutdown. The final commit of a per-FOV shard (one timepoint =
# (1, C, Z, Y, X), routinely ~1 GB for a deep z-stack) is a single TensorStore
# read-modify-write that can take tens of seconds. Terminating the subprocess
# before that commit lands leaves the previous partial shard on disk (only the
# last-written z-slices missing, and only at pyramid level 0) plus a stray
# ``*.__lock`` file. This shutdown runs in a background daemon thread, so a
# generous timeout does not block the UI or the start of the next acquisition;
# terminate() only fires if finalize genuinely wedges past this deadline.
JOB_RUNNER_FINALIZE_TIMEOUT_S = 600

# No-progress stall window for the end-of-run background upload drainer. The
# UploadWorker stamps a heartbeat on every chunk it moves, so this means "no
# byte of forward progress for N seconds while uploads are still outstanding"
# = genuinely wedged (a dead SMB handle), not merely slow. Much tighter than a
# result-only window because the heartbeat tells a slow large file apart from a
# stuck one; a wedged worker is force-terminated instead of waited out.
UPLOAD_DRAINER_STALL_WINDOW_S = 120


class SummarizeResult(NamedTuple):
    """Result from processing job output queues."""

    none_failed: bool  # True if no jobs failed (or no results to process)
    had_results: bool  # True if any results were pulled from queue


# ----------------------------------------------------------------------------
# Background upload drainer registry
# ----------------------------------------------------------------------------
#
# An acquisition's UploadWorker is handed off to a background daemon thread
# at shutdown so the controller can free up and the next acquisition can
# start. Multiple drainers can be active concurrently (one per in-flight
# acquisition). Holding strong references in a module-level list keeps the
# daemon threads from getting GC'd before they finish their backlog.

_active_upload_drainers: List["_BackgroundUploadDrainer"] = []
_active_upload_drainers_lock = threading.Lock()

# UploadWorkers that have been started but not yet handed to a drainer. From
# UploadWorker.start() until _spawn_background_upload_drainer, the worker was
# previously reachable by NO kill path — an exception in MultiPointWorker
# setup/run, or an app close mid-acquisition, left a non-daemon subprocess
# that blocked interpreter exit forever. Registering at spawn time makes
# terminate_all_upload_drainers() (close + atexit) able to reap it in every
# window of its life.
_live_upload_workers: List["UploadWorker"] = []


def register_live_upload_worker(worker: "UploadWorker") -> None:
    with _active_upload_drainers_lock:
        _live_upload_workers.append(worker)


def unregister_live_upload_worker(worker: "UploadWorker") -> None:
    """Remove a worker from the pre-drainer registry (ownership moved to a
    drainer, which has its own force-stop path)."""
    with _active_upload_drainers_lock:
        try:
            _live_upload_workers.remove(worker)
        except ValueError:
            pass


def register_active_upload_drainer(drainer: "_BackgroundUploadDrainer") -> None:
    """Add a drainer to the live registry, pruning completed ones first."""
    with _active_upload_drainers_lock:
        _active_upload_drainers[:] = [
            d for d in _active_upload_drainers if not d.is_done()
        ]
        _active_upload_drainers.append(drainer)


def active_upload_drainer_count() -> int:
    """How many background upload drainers are still running."""
    with _active_upload_drainers_lock:
        _active_upload_drainers[:] = [
            d for d in _active_upload_drainers if not d.is_done()
        ]
        return len(_active_upload_drainers)


def active_upload_drainer_summary() -> List[dict]:
    """Snapshot of every pending upload owner: drainers AND live workers.

    Useful for the GUI to surface to the user before starting a new run or
    closing the app ("3 previous acquisitions are still uploading, X tasks
    total"). Workers still owned by a running acquisition (no drainer yet)
    are included too — closing the app kills those uploads just the same.
    """
    with _active_upload_drainers_lock:
        _active_upload_drainers[:] = [
            d for d in _active_upload_drainers if not d.is_done()
        ]
        out = [d.snapshot() for d in _active_upload_drainers]
        for w in _live_upload_workers:
            out.append(
                {
                    "experiment_path": _worker_experiment_path(w),
                    "outstanding": 0,  # unknown mid-acquisition; listed for visibility
                    "alive": True,
                    "acquisition_in_progress": True,
                }
            )
        return out


def _worker_experiment_path(worker: "UploadWorker") -> str:
    """Best-effort experiment dir for a worker (manifest lives at its root)."""
    try:
        return os.path.dirname(getattr(worker, "_manifest_path", "") or "") or "unknown"
    except Exception:
        return "unknown"


def _write_orphan_incomplete_record(worker: "UploadWorker") -> None:
    """Persist UPLOAD_INCOMPLETE.txt for a worker killed before any drainer
    took ownership (app close mid-acquisition) — same contract as the
    drainer's record: no abandonment may be invisible after restart."""
    exp = _worker_experiment_path(worker)
    if not exp or exp == "unknown":
        return
    from datetime import datetime, timezone
    remote = getattr(getattr(worker, "_target", None), "remote_root", "")
    try:
        with open(os.path.join(exp, "UPLOAD_INCOMPLETE.txt"), "w", encoding="utf-8") as f:
            f.write(
                f"Streaming upload for this acquisition did NOT complete.\n\n"
                f"Reason: app closed during the acquisition (upload worker terminated)\n"
                f"Remote root: {remote}\n"
                f"Recorded at: {datetime.now(timezone.utc).isoformat()}\n\n"
                f"Local data has NOT been deleted for unverified timepoints.\n"
                f"To finish the upload, run:\n"
                f"    python scripts/zarr_backfill_upload.py \"{exp}\" --remote \"{remote}\"\n"
            )
    except OSError:
        pass


def terminate_all_upload_drainers() -> None:
    """Force-stop every active drainer's UploadWorker immediately.

    The UploadWorker is a non-daemon ``multiprocessing.Process``; Python's
    multiprocessing joins non-daemon children at interpreter exit, so a worker
    wedged in a synchronous SMB I/O wait would block the whole app from
    closing. Terminating the workers here (from ``MultiPointController.close()``
    and as an ``atexit`` handler) reaps them via ``terminate()`` — which the OS
    honors even for a thread stuck in kernel I/O — so exit is never blocked.
    Uploads abandoned this way are recoverable with the backfill script.
    """
    with _active_upload_drainers_lock:
        drainers = list(_active_upload_drainers)
        orphans = list(_live_upload_workers)
        _live_upload_workers.clear()
    for d in drainers:
        try:
            d.force_stop()
        except Exception:
            pass
    # Workers spawned but never handed to a drainer (setup crash, app close
    # mid-acquisition): terminate them too, or multiprocessing's exit join
    # blocks forever on the non-daemon child. Leave the same persistent
    # record a drainer force-stop would.
    for w in orphans:
        try:
            _write_orphan_incomplete_record(w)
        except Exception:
            pass
        try:
            w.force_stop()
        except Exception:
            pass


# atexit is LIFO, so for this handler to run BEFORE multiprocessing's
# _exit_function (which joins non-daemon children and would hang on a wedged
# worker), _exit_function must already be registered when we register ours.
# ``import multiprocessing`` alone does NOT import multiprocessing.util or
# register _exit_function — that happens lazily when the first mp primitive
# is created, which in this codebase is at RUNTIME (constructors), i.e. AFTER
# this module is imported. Import util explicitly to force its registration
# now, making the LIFO ordering deterministic.
import multiprocessing.util  # noqa: E402  (side effect: registers _exit_function)

atexit.register(terminate_all_upload_drainers)


class _BackgroundUploadDrainer:
    """Owns one acquisition's UploadWorker after the MultiPointWorker has gone.

    Created by ``MultiPointWorker._spawn_background_upload_drainer`` with a
    snapshot of all state the drain needs. Runs a daemon thread that:
      1. Submits the post-finalize metadata resync (uncontested clean read
         of every FOV's zarr.json + frame_times, since the JobRunner
         subprocess has exited and the writer has finalized).
      2. Polls the UploadWorker's output queue; for each ``UploadResult``,
         applies local deletion if ``delete_after_verify`` is on and the
         containing timepoint has fully verified.
      3. Stops when pending hits 0 or the wallclock timeout fires.
      4. Sends ``shutdown()``, joins the worker subprocess, releases its
         queue feeder threads so the host process can later exit cleanly.

    The drainer is independent of MultiPointWorker — multiple drainers
    (one per past acquisition) can be alive concurrently.
    """

    def __init__(
        self,
        *,
        upload_worker,
        upload_target,
        tasks_by_tp: Dict[int, set],
        results_by_tp: Dict[int, List],
        expected_by_tp: Dict[int, int],
        deletion_done: Set[int],
        failed_tasks: List,
        completed_task_ids: Set[str],
        results_received: int,
        zarr_writer_info,
        experiment_path: Optional[str],
        runner_output_queues: Optional[List] = None,
        runners_done_event: Optional[threading.Event] = None,
        stall_window_s: float = 120.0,
        failed_deletions: int = 0,
    ):
        self._worker = upload_worker
        self._target = upload_target
        self._tasks_by_tp = tasks_by_tp
        self._results_by_tp = results_by_tp
        self._expected_by_tp = expected_by_tp
        self._deletion_done = deletion_done
        self._failed_tasks = failed_tasks
        # Task_ids whose UploadResult has already been consumed. Guards the
        # register-after-complete race: a BarrierResult that arrives after its
        # UploadResult must not re-add a task_id nothing will ever discard.
        self._completed_task_ids = completed_task_ids
        # Count of UploadResults consumed so far (main-worker phase included).
        # outstanding = worker.tasks_submitted - results_received: authoritative
        # regardless of BarrierResult delivery.
        self._results_received = int(results_received)
        self._zarr_writer_info = zarr_writer_info
        self._experiment_path = experiment_path
        # Output queues of upload-enabled JobRunners (shutdown with
        # close_output_queue=False): late BarrierResults keep arriving on them
        # during the up-to-600s background finalize; we consume them here.
        self._runner_queues: List = list(runner_output_queues or [])
        self._runners_done = runners_done_event
        self._stall_window_s = stall_window_s
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self._done = threading.Event()
        self._stop_requested = threading.Event()
        self._last_result_time = 0.0
        self._failed_deletions = int(failed_deletions)
        self._consecutive_failed_tasks = 0
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"UploadDrain[{os.path.basename(experiment_path or 'unknown')}]",
        )

    def start(self) -> None:
        self._thread.start()

    def is_done(self) -> bool:
        return self._done.is_set()

    def snapshot(self) -> dict:
        return {
            "experiment_path": self._experiment_path,
            "outstanding": self._outstanding(),
            "alive": self._thread.is_alive(),
        }

    def force_stop(self) -> None:
        """Terminate the owned worker immediately and mark the drainer done.

        Idempotent. The drain loop (running in the daemon thread) checks
        ``_stop_requested`` each pass and exits promptly. Called by
        ``terminate_all_upload_drainers`` on app close / at interpreter exit.
        Leaves a persistent on-disk record so the abandonment is visible
        after restart (the in-memory registry does not survive one).
        """
        already = self._stop_requested.is_set()
        self._stop_requested.set()
        if not already:
            try:
                self._write_upload_incomplete_record(
                    reason="force-stopped (app close / interpreter exit)"
                )
            except Exception:
                pass
        worker = self._worker
        if worker is not None:
            try:
                worker.force_stop()
            except Exception:
                pass
        self._done.set()

    def _tasks_submitted(self) -> int:
        worker = self._worker
        if worker is None:
            return self._results_received
        try:
            return worker.tasks_submitted
        except Exception:
            return self._results_received

    def _outstanding(self) -> int:
        return max(0, self._tasks_submitted() - self._results_received)

    def _drain_available_results(self) -> int:
        """Pull every available UploadResult; apply per-timepoint deletion."""
        if self._worker is None:
            return 0
        try:
            from control.core.zarr_upload import drain_output_queue_nonblocking
            results = drain_output_queue_nonblocking(self._worker.output_queue)
        except (OSError, ValueError) as e:
            self._log.debug(f"Upload result queue read failed: {e}")
            return 0
        for result in results:
            tp = result.time_point
            self._results_received += 1
            self._completed_task_ids.add(result.task_id)
            self._tasks_by_tp.get(tp, set()).discard(result.task_id)
            self._results_by_tp.setdefault(tp, []).append(result)
            if not result.success:
                self._failed_tasks.append(result)
                self._consecutive_failed_tasks += 1
                self._log.warning(
                    f"Upload task {result.task_id} for t={tp} fov={result.fov} "
                    f"failed: {result.error}"
                )
            else:
                self._consecutive_failed_tasks = 0
            self._maybe_batched_delete(tp)
        return len(results)

    def _drain_runner_barrier_results(self) -> None:
        """Consume late JobResults from the upload-enabled JobRunners.

        Only BarrierResults (raw or embedded in PostprocessResults) matter for
        upload bookkeeping; everything else at this stage is display-only and
        is dropped. Queues that close under us are removed from the poll set.
        """
        if not self._runner_queues:
            return
        import queue as _queue
        from control.core.job_processing import BarrierResult, PostprocessResult
        dead: List = []
        for q in self._runner_queues:
            while True:
                try:
                    job_result = q.get_nowait()
                except _queue.Empty:
                    break
                except (OSError, ValueError, EOFError):
                    dead.append(q)
                    break
                result = getattr(job_result, "result", None)
                if isinstance(result, BarrierResult):
                    self._note_barrier(result)
                elif isinstance(result, PostprocessResult):
                    for br in result.barrier_results:
                        self._note_barrier(br)
        for q in dead:
            self._runner_queues.remove(q)

    def _note_barrier(self, br) -> None:
        """Late-arriving barrier bookkeeping (mirrors _handle_barrier_result)."""
        if br.submitted:
            if br.task_id not in self._completed_task_ids:
                self._tasks_by_tp.setdefault(br.time_point, set()).add(br.task_id)
        else:
            self._results_by_tp.setdefault(br.time_point, []).append(
                UploadResult(
                    task_id=br.task_id,
                    time_point=br.time_point,
                    region_id=br.region_id,
                    fov=br.fov,
                    success=True,
                    uploaded_paths=[],
                    failed_paths=[],
                    error=None,
                )
            )
            self._maybe_batched_delete(br.time_point)

    def _maybe_batched_delete(self, time_point: int) -> None:
        if time_point in self._deletion_done:
            return
        if self._target is None or not self._target.delete_after_verify:
            return
        expected = self._expected_by_tp.get(time_point)
        if expected is None:
            return
        results = self._results_by_tp.get(time_point, [])
        if len(results) < expected:
            return
        if any(not r.success for r in results):
            return
        deleted = 0
        for result in results:
            for local_path in result.deletable_uploaded_paths:
                try:
                    if os.path.isfile(local_path):
                        os.remove(local_path)
                        deleted += 1
                except OSError as e:
                    self._failed_deletions += 1
                    self._log.warning(
                        f"Failed to delete {local_path} after verified upload: {e}"
                    )
        self._prune_empty_shard_dirs(time_point)
        self._deletion_done.add(time_point)
        self._log.info(
            f"[{os.path.basename(self._experiment_path or 'unknown')}] "
            f"Reclaimed local disk: deleted {deleted} files "
            f"for verified timepoint t={time_point}"
        )

    def _prune_empty_shard_dirs(self, time_point: int) -> None:
        if self._zarr_writer_info is None:
            return
        for region_id, fov_count in self._zarr_writer_info.region_fov_counts.items():
            for fov in range(fov_count):
                group_dir = self._zarr_writer_info.get_group_path(region_id, fov)
                if not os.path.isdir(group_dir):
                    continue
                for entry in os.listdir(group_dir):
                    candidate = os.path.join(group_dir, entry, "c", str(time_point))
                    if os.path.isdir(candidate):
                        for root, dirs, files in os.walk(candidate, topdown=False):
                            if not files and not dirs:
                                try:
                                    os.rmdir(root)
                                except OSError:
                                    break

    # Files at the experiment-root that the upload pipeline produces or
    # otherwise has no business shipping to the remote.
    _EXPERIMENT_ROOT_SKIP_NAMES = frozenset({
        "upload_manifest.jsonl",
        "upload_manifest_backfill.jsonl",
        "RAW_DATA_UPLOADED.txt",
        "UPLOAD_INCOMPLETE.txt",
    })

    def _enqueue_post_finalize_metadata_resync(self) -> None:
        """Re-upload EVERY zarr metadata file in the experiment tree.

        The caller runs this only after the JobRunner subprocesses have
        exited (``runners_done``), so every ``zarr.json`` — including the
        finalize rewrite that sets ``_squid.acquisition_complete=true`` — and
        every ``frame_times`` chunk is stable on disk. Enumerating by
        filesystem walk (not through ``ZarrWriterInfo``) covers ALL plates:
        the dense plate, ragged per-state plates (``{state}.ome.zarr``,
        ``{state}_refz.ome.zarr``), derived postprocess plates
        (``{label}_{output}.ome.zarr``), their plate-root and per-well group
        ``zarr.json``, and the non-HCS ``zarr/<region>/fov_*.ome.zarr`` tree —
        the old writer-info-based enumeration silently skipped everything but
        the dense plate.
        """
        if self._worker is None or self._target is None or not self._target.enabled:
            return
        if not self._experiment_path or not os.path.isdir(self._experiment_path):
            return
        from uuid import uuid4
        from control.core.zarr_upload import UploadTask, local_to_remote_path

        meta_files: List[str] = []
        for root, dirs, files in os.walk(self._experiment_path):
            if "zarr.json" in files:
                meta_files.append(os.path.join(root, "zarr.json"))
            if os.path.basename(root) == "frame_times":
                chunk = os.path.join(root, "c", "0", "0", "0")
                if os.path.isfile(chunk):
                    meta_files.append(chunk)
        if not meta_files:
            return
        # Chunk into modest tasks so results (and watchdog progress) keep
        # flowing instead of one giant task holding everything.
        submitted = 0
        batch_size = 100
        for i in range(0, len(meta_files), batch_size):
            batch = meta_files[i : i + batch_size]
            files = [
                (
                    local,
                    local_to_remote_path(
                        local, self._target.local_base, self._target.remote_root,
                    ),
                )
                for local in batch
            ]
            task = UploadTask(
                task_id=str(uuid4()),
                time_point=-1,  # sentinel
                region_id="(metadata-resync)",
                fov=-1,
                files=files,
                deletable_local_paths=set(),
                stable_read_paths=set(batch),
            )
            self._worker.submit(task)
            self._tasks_by_tp.setdefault(-1, set()).add(task.task_id)
            self._expected_by_tp[-1] = self._expected_by_tp.get(-1, 0) + 1
            submitted += 1
        self._log.info(
            f"[{os.path.basename(self._experiment_path or 'unknown')}] "
            f"Submitted post-finalize metadata resync: {len(meta_files)} file(s) "
            f"in {submitted} task(s)"
        )

        # Also push every experiment-root file (acquisition.yaml, run logs,
        # config dumps, …) so the remote ends up with the full acquisition
        # record, not just the zarr-internal metadata.
        self._enqueue_experiment_root_resync()

    def _enqueue_experiment_root_resync(self) -> None:
        if (
            self._target is None
            or not self._target.enabled
            or not self._experiment_path
        ):
            return
        from uuid import uuid4
        from control.core.zarr_upload import UploadTask, local_to_remote_path
        try:
            entries = sorted(os.listdir(self._experiment_path))
        except OSError as e:
            self._log.warning(
                f"Could not list experiment root {self._experiment_path}: {e}"
            )
            return
        root_files: List[str] = []
        for name in entries:
            if name in self._EXPERIMENT_ROOT_SKIP_NAMES:
                continue
            full = os.path.join(self._experiment_path, name)
            if os.path.isfile(full):
                root_files.append(full)
        if not root_files:
            return
        files = [
            (
                local,
                local_to_remote_path(
                    local, self._target.local_base, self._target.remote_root,
                ),
            )
            for local in root_files
        ]
        task = UploadTask(
            task_id=str(uuid4()),
            time_point=-1,
            region_id="",
            fov=-1,
            files=files,
            deletable_local_paths=set(),
            stable_read_paths=set(root_files),
        )
        self._worker.submit(task)
        self._tasks_by_tp.setdefault(-1, set()).add(task.task_id)
        self._expected_by_tp[-1] = self._expected_by_tp.get(-1, 0) + 1
        self._log.info(
            f"[{os.path.basename(self._experiment_path or 'unknown')}] "
            f"Submitted experiment-root resync ({len(root_files)} file(s))"
        )

    def _write_upload_complete_marker(self) -> None:
        """Drop a ``RAW_DATA_UPLOADED.txt`` note in the experiment dir.

        Only called when the drain finishes cleanly (nothing remaining, no
        failed tasks) — the marker means "everything has been verified on
        the remote and only shard data was reclaimed locally; the rest of
        the directory is a valid OME-NGFF pointer."
        """
        if not self._experiment_path or self._target is None:
            return
        from datetime import datetime, timezone
        marker = os.path.join(self._experiment_path, "RAW_DATA_UPLOADED.txt")
        manifest = os.path.join(self._experiment_path, "upload_manifest.jsonl")
        if self._target.delete_after_verify:
            if self._failed_deletions:
                deletion_note = (
                    f"Per-timepoint shard data was removed from local disk after\n"
                    f"verification, EXCEPT {self._failed_deletions} file(s) that could\n"
                    f"not be deleted (locked by another process); they remain locally.\n"
                )
            else:
                deletion_note = (
                    "Local zarr metadata (zarr.json, frame_times) is preserved so this\n"
                    "directory is still a valid OME-NGFF reader pointer. Only the\n"
                    "per-timepoint shard data has been removed from local disk.\n"
                )
        else:
            deletion_note = (
                "delete-after-verify was OFF for this run: the complete local copy\n"
                "is still in place alongside the verified remote copy.\n"
            )
        content = (
            f"Raw zarr shard data for this acquisition has been uploaded to:\n"
            f"    {self._target.remote_root}\n"
            f"\n"
            f"{deletion_note}"
            f"\n"
            f"Uploaded at: {datetime.now(timezone.utc).isoformat()}\n"
            f"Upload manifest: {manifest}\n"
        )
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(content)
            # A clean completion supersedes any stale incomplete record.
            incomplete = os.path.join(self._experiment_path, "UPLOAD_INCOMPLETE.txt")
            if os.path.isfile(incomplete):
                try:
                    os.remove(incomplete)
                except OSError:
                    pass
            self._log.info(
                f"[{os.path.basename(self._experiment_path or 'unknown')}] "
                f"Wrote upload-complete marker: {marker}"
            )
        except OSError as e:
            self._log.warning(
                f"Could not write upload-complete marker {marker}: {e}"
            )

    def _write_upload_incomplete_record(self, reason: str) -> None:
        """Persist an ``UPLOAD_INCOMPLETE.txt`` next to the data.

        The in-memory drainer registry dies with the process; this file is
        the only record after an app restart that uploads were abandoned and
        the backfill script is needed. Overwritten by each abandonment;
        removed when a later clean drain writes the complete marker.
        """
        if not self._experiment_path:
            return
        from datetime import datetime, timezone
        path = os.path.join(self._experiment_path, "UPLOAD_INCOMPLETE.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(
                    f"Streaming upload for this acquisition did NOT complete.\n"
                    f"\n"
                    f"Reason: {reason}\n"
                    f"Outstanding upload task(s): {self._outstanding()}\n"
                    f"Failed upload task(s): {len(self._failed_tasks)}\n"
                    f"Remote root: {getattr(self._target, 'remote_root', '?')}\n"
                    f"Recorded at: {datetime.now(timezone.utc).isoformat()}\n"
                    f"\n"
                    f"Local data has NOT been deleted for unverified timepoints.\n"
                    f"To finish the upload, run:\n"
                    f"    python scripts/zarr_backfill_upload.py \"{self._experiment_path}\" "
                    f"--remote \"{getattr(self._target, 'remote_root', '')}\"\n"
                )
        except OSError as e:
            self._log.warning(f"Could not write UPLOAD_INCOMPLETE record: {e}")

    def _wait_for_runners_to_exit(self, tag: str) -> None:
        """Phase 1: keep bookkeeping current while the JobRunner subprocesses
        finish their queued backlog + finalize (up to the 600s budget).

        Late BarrierResults are consumed from the runner output queues here —
        the main process's last drain happened before the background shutdown,
        so without this the tasks those barriers staged would be untracked.
        """
        if self._runners_done is None:
            return
        deadline = time.time() + JOB_RUNNER_FINALIZE_TIMEOUT_S + 60.0
        last_log = 0.0
        while not self._runners_done.is_set() and not self._stop_requested.is_set():
            self._drain_runner_barrier_results()
            self._drain_available_results()
            now = time.time()
            if now > deadline:
                self._log.error(
                    f"[{tag}] JobRunners did not finish within "
                    f"{int(JOB_RUNNER_FINALIZE_TIMEOUT_S + 60)}s; proceeding with "
                    f"metadata resync against possibly-unfinalized zarr.json."
                )
                break
            if now - last_log > 30.0:
                self._log.info(
                    f"[{tag}] waiting for writer finalize before metadata resync "
                    f"({self._outstanding()} upload(s) in flight meanwhile)"
                )
                last_log = now
            self._runners_done.wait(timeout=0.5)
        # Sweep any barriers that landed right before the runners exited.
        self._drain_runner_barrier_results()

    def _worker_alive(self) -> bool:
        """is_alive() that tolerates a concurrently force-stopped (closed)
        Process handle — close() makes is_alive() raise ValueError."""
        worker = self._worker
        if worker is None:
            return False
        try:
            return worker.is_alive()
        except Exception:
            return False

    def _run(self) -> None:
        tag = os.path.basename(self._experiment_path or "unknown")
        crashed = False
        abandon_reason: Optional[str] = None
        try:
            from control.core.zarr_upload import remote_root_reachable
            # Fail fast if the share is simply gone: don't pay the stall window
            # per file when the mount is unreachable. Probe is timeout-bounded
            # so a wedged mount can't block here either.
            if (
                self._target is not None
                and getattr(self._target, "enabled", False)
                and not remote_root_reachable(self._target.remote_root, timeout_s=5.0)
            ):
                self._log.error(
                    f"[{tag}] remote {self._target.remote_root} is unreachable; "
                    f"abandoning {self._outstanding()} pending upload(s) without "
                    f"waiting. Re-run the backfill script over "
                    f"{self._experiment_path} once the share is back."
                )
                abandon_reason = "remote share unreachable at drain start"
                return

            # Phase 1: wait for the JobRunners to drain their backlog and
            # finalize the writers, consuming late BarrierResults meanwhile.
            # Only AFTER this is the metadata resync genuinely post-finalize
            # (zarr.json carries acquisition_complete=true).
            self._wait_for_runners_to_exit(tag)
            if self._stop_requested.is_set():
                return

            # Phase 2: resync every metadata file, now stable on disk.
            self._enqueue_post_finalize_metadata_resync()

            # Phase 3: drain until nothing is outstanding. Heartbeat-based
            # wedge detection, NOT a wallclock cap: the worker stamps
            # ``worker.heartbeat`` on every chunk it moves, so a healthy
            # transfer — however slow or large its backlog — keeps the idle
            # clock near zero and is never abandoned. The heartbeat is NOT
            # stamped by failing uploads, so a dead-share failure grind looks
            # idle here; we then re-probe reachability to distinguish "share
            # gone" (abandon fast with a persistent record) from "worker
            # wedged" (terminate, same record).
            stall_window_s = self._stall_window_s
            last_log = 0.0
            self._last_result_time = time.time()
            while True:
                if self._stop_requested.is_set():
                    return
                outstanding = self._outstanding()
                if outstanding == 0:
                    break
                # A crashed/terminated worker will never emit more results.
                if not self._worker_alive():
                    self._log.error(f"[{tag}] UploadWorker is not alive; stopping drain.")
                    abandon_reason = "UploadWorker process died"
                    break
                got = self._drain_available_results()
                self._drain_runner_barrier_results()
                now = time.time()
                if got:
                    self._last_result_time = now
                # Many consecutive whole-task failures usually mean the share
                # went away mid-drain: re-probe instead of grinding the whole
                # backlog through 5-attempt retry ladders.
                if self._consecutive_failed_tasks >= 5:
                    if not remote_root_reachable(self._target.remote_root, timeout_s=5.0):
                        self._log.error(
                            f"[{tag}] remote became unreachable mid-drain "
                            f"({self._consecutive_failed_tasks} consecutive task "
                            f"failures); abandoning {outstanding} upload(s)."
                        )
                        abandon_reason = "remote share became unreachable mid-drain"
                        break
                    self._consecutive_failed_tasks = 0
                hb = 0.0
                try:
                    hb = float(self._worker.heartbeat)
                except Exception:
                    pass
                last_progress = max(hb, self._last_result_time)
                idle = now - last_progress if last_progress > 0 else 0.0
                if last_progress > 0 and idle > stall_window_s:
                    if not remote_root_reachable(self._target.remote_root, timeout_s=5.0):
                        self._log.error(
                            f"[{tag}] no progress for {int(idle)}s and the remote is "
                            f"unreachable; abandoning {outstanding} upload(s). Re-run "
                            f"the backfill script once the share is back."
                        )
                        abandon_reason = "remote share unreachable (no progress)"
                    else:
                        self._log.error(
                            f"[{tag}] UploadWorker wedged: no progress for "
                            f"{int(idle)}s with {outstanding} upload(s) outstanding "
                            f"although the share is reachable; terminating. Re-run "
                            f"the backfill script over {self._experiment_path}."
                        )
                        abandon_reason = "UploadWorker wedged (no progress, share reachable)"
                    break
                if now - last_log > 30.0:
                    self._log.info(
                        f"[{tag}] upload drainer: {outstanding} in flight "
                        f"(idle {int(idle)}s / stall window {int(stall_window_s)}s)"
                    )
                    last_log = now
                time.sleep(0.5)

        except Exception:
            crashed = True
            self._log.exception(
                f"[{tag}] upload drainer crashed; worker will be torn down."
            )
        finally:
            # Teardown ALWAYS runs (a raised exception must not leak a live
            # non-daemon worker that only atexit could reap). All final
            # accounting happens AFTER teardown: results that complete during
            # the shutdown grace still count, so the marker/incomplete
            # decision reflects what actually reached the remote.
            worker_clean = False
            try:
                worker_clean = self._teardown_worker(tag)
            except Exception:
                self._log.exception(f"[{tag}] worker teardown failed")
            try:
                self._drain_available_results()
                self._drain_runner_barrier_results()
            except Exception:
                pass
            remaining = self._outstanding()
            runners_done = self._runners_done is None or self._runners_done.is_set()
            stopped = self._stop_requested.is_set()
            if remaining:
                self._log.error(
                    f"[{tag}] UploadWorker drain ended with {remaining} upload(s) "
                    f"outstanding. Run the standalone backfill script over "
                    f"{self._experiment_path} to complete the upload."
                )
            if self._failed_tasks:
                self._log.warning(
                    f"[{tag}] {len(self._failed_tasks)} upload task(s) failed during "
                    f"this run; local files for the affected timepoints have NOT "
                    f"been deleted."
                )
            # Log timepoints whose delete-after-verify never fired, so a
            # partially-reclaimed disk is explainable from the log.
            try:
                if self._target is not None and self._target.delete_after_verify:
                    undeleted = sorted(
                        tp for tp, expected in self._expected_by_tp.items()
                        if tp >= 0 and expected and tp not in self._deletion_done
                        and self._results_by_tp.get(tp)
                    )
                    if undeleted:
                        self._log.warning(
                            f"[{tag}] delete-after-verify never completed for "
                            f"timepoint(s) {undeleted}; their local shards were kept."
                        )
            except Exception:
                pass
            complete = (
                not crashed
                and not stopped
                and remaining == 0
                and not self._failed_tasks
                and worker_clean
                and runners_done
            )
            try:
                if complete:
                    self._write_upload_complete_marker()
                else:
                    if crashed:
                        reason = "drainer crashed (see log)"
                    elif stopped:
                        reason = "force-stopped (app close / interpreter exit)"
                    elif abandon_reason:
                        reason = abandon_reason
                    elif self._failed_tasks:
                        reason = f"{len(self._failed_tasks)} upload task(s) failed"
                    elif not runners_done:
                        reason = "writer finalize did not finish within its budget"
                    elif remaining:
                        reason = f"{remaining} upload(s) never completed"
                    else:
                        reason = "upload worker did not exit cleanly"
                    self._write_upload_incomplete_record(reason)
            except Exception:
                pass
            self._close_runner_queues()
            self._done.set()
            self._log.info(f"[{tag}] background upload drainer finished.")

    def _close_runner_queues(self) -> None:
        """Release the parent-side handles of the runner output queues we own
        (their shutdown ran with close_output_queue=False)."""
        for q in self._runner_queues:
            try:
                q.close()
            except Exception:
                pass
            try:
                q.cancel_join_thread()
            except Exception:
                pass
        self._runner_queues = []

    def _teardown_worker(self, tag: str) -> bool:
        """Stop the worker. Returns True iff it exited cooperatively.

        The shutdown sentinel sits BEHIND any still-queued tasks, so the wait
        is progress-aware rather than a fixed grace: as long as the worker
        keeps moving bytes (heartbeat) or emitting results it gets more time;
        only an idle-past-stall-window worker is terminated. A fixed 5s join
        used to kill workers that were still legitimately uploading.
        """
        worker = self._worker
        if worker is None:
            return True
        clean = False
        try:
            worker.shutdown()
            sentinel_sent_at = time.time()
            while True:
                worker.join(timeout=2.0)
                if not worker.is_alive():
                    clean = True
                    break
                self._drain_available_results()
                hb = 0.0
                try:
                    hb = float(worker.heartbeat)
                except Exception:
                    pass
                last_progress = max(hb, self._last_result_time, sentinel_sent_at)
                if time.time() - last_progress > self._stall_window_s:
                    self._log.warning(
                        f"[{tag}] UploadWorker idle past the stall window after "
                        f"the shutdown sentinel; terminating."
                    )
                    break
                if self._stop_requested.is_set():
                    break
        except Exception as e:
            self._log.debug(f"[{tag}] graceful worker shutdown: {e}")
        if clean:
            try:
                clean = worker.exitcode == 0
            except Exception:
                pass
        # force_stop() is a no-op if the worker already exited; otherwise it
        # terminate()s a worker still wedged in SMB I/O and releases queues.
        try:
            worker.force_stop()
        except Exception as e:
            self._log.error(f"[{tag}] Error force-stopping UploadWorker: {e}")
        return clean


class MultiPointWorker:
    def __init__(
        self,
        scope: Microscope,
        live_controller: LiveController,
        auto_focus_controller: Optional[AutoFocusController],
        laser_auto_focus_controller: Optional[LaserAutofocusController],
        objective_store: ObjectiveStore,
        acquisition_parameters: AcquisitionParameters,
        callbacks: MultiPointControllerFunctions,
        abort_requested_fn: Callable[[], bool],
        request_abort_fn: Callable[[], None],
        extra_job_classes: list[type[Job]] | None = None,
        abort_on_failed_jobs: bool = True,
        alignment_widget=None,
        slack_notifier=None,
        prewarmed_job_runner: Optional[JobRunner] = None,
        prewarmed_bp_values: Optional["BackpressureValues"] = None,
    ):
        self._log = squid.logging.get_logger(__class__.__name__)
        self._timing = utils.TimingManager("MultiPointWorker Timer Manager")
        self._alignment_widget = alignment_widget  # Optional AlignmentWidget for coordinate offset
        self._slack_notifier = slack_notifier  # Optional SlackNotifier for notifications

        # Slack notification tracking counters
        self._timepoint_image_count = 0
        self._timepoint_fov_count = 0
        self._timepoint_start_time = 0.0
        self._acquisition_error_count = 0
        self._laser_af_successes = 0
        self._laser_af_failures = 0
        self.microscope: Microscope = scope
        self.camera: AbstractCamera = scope.camera
        self.microcontroller: Microcontroller = scope.low_level_drivers.microcontroller
        self.stage: squid.abc.AbstractStage = scope.stage
        self.piezo: Optional[PiezoStage] = scope.addons.piezo_stage
        self.liveController = live_controller
        self.autofocusController: Optional[AutoFocusController] = auto_focus_controller
        self.laser_auto_focus_controller: Optional[LaserAutofocusController] = laser_auto_focus_controller
        self.objectiveStore: ObjectiveStore = objective_store
        self.fluidics = scope.addons.fluidics
        self.use_fluidics = acquisition_parameters.use_fluidics
        self.keep_illuminators_on_between_captures = (
            acquisition_parameters.keep_illuminators_on_between_captures
        )

        self.callbacks: MultiPointControllerFunctions = callbacks
        self.abort_requested_fn: Callable[[], bool] = abort_requested_fn
        self.request_abort_fn: Callable[[], None] = request_abort_fn
        self.NZ = acquisition_parameters.NZ
        self.deltaZ = acquisition_parameters.deltaZ

        self.Nt = acquisition_parameters.Nt
        self.dt = acquisition_parameters.deltat

        self.do_autofocus = acquisition_parameters.do_autofocus
        self.do_reflection_af = acquisition_parameters.do_reflection_autofocus
        self.use_piezo = acquisition_parameters.use_piezo
        self.display_resolution_scaling = acquisition_parameters.display_resolution_scaling

        self.experiment_ID = acquisition_parameters.experiment_ID
        self.base_path = acquisition_parameters.base_path
        self.experiment_path = os.path.join(self.base_path or "", self.experiment_ID or "")
        self.observation_state_names = list(acquisition_parameters.selected_observation_state_names or [])
        self._use_observation_presets = bool(self.observation_state_names)
        self.region_observation_state_map = acquisition_parameters.region_observation_state_map
        self._emission_filter_wheel = getattr(scope.addons, "emission_filter_wheel", None)

        # Resolved per-position acquisition plan (cycles). A flat/legacy
        # selection arrives as a global plan of 1-frame-per-state events, so the
        # worker has a single iteration path. `_global_plan` applies to any
        # region without an explicit override in `_region_plans`.
        from control.models.acquisition_cycle import RegionPlan, _index_events

        self._global_plan = acquisition_parameters.global_region_plan
        if self._global_plan is None:
            # Defensive fallback (e.g. direct worker construction in tests): treat
            # the channel axis as a 1-frame-per-state chain. _index_events takes
            # tagged raw events, so wrap each name as a ("state", (name, az)) event;
            # a flat selection is always a full z-stack (az=True).
            self._global_plan = RegionPlan.from_events(
                _index_events([("state", (n, True)) for n in self.observation_state_names])
            )
        self._region_plans = dict(acquisition_parameters.resolved_region_plans or {})
        # True if any plan carries online-postprocessing assignments (adds a
        # PostprocessJob runner + routes those frames away from the save jobs).
        self._has_postprocess = any(
            p is not None and p.postprocess_groups
            for p in [self._global_plan, *self._region_plans.values()]
        )
        # Size of the most recent frame sent to the live display; postprocess
        # output previews are only displayed when they match it (the shared
        # napari/contrast viewer holds one image size across all channels).
        self._last_raw_display_shape: Optional[Tuple[int, int]] = None
        # Imaged channel axis (C order) and per-channel display metadata, used for
        # zarr/omero naming. Resolved lazily so a None pixel/illumination config
        # in tests doesn't break construction.
        self._channel_meta_cache: Dict[str, Tuple[str, Optional[int]]] = {}

        # Pre-compute acquisition metadata that remains constant throughout the run.
        try:
            pixel_factor = self.objectiveStore.get_pixel_size_factor()
            sensor_pixel_um = self.camera.get_pixel_size_binned_um()
            if pixel_factor is not None and sensor_pixel_um is not None:
                self._pixel_size_um = float(pixel_factor) * float(sensor_pixel_um)
            else:
                self._pixel_size_um = None
        except Exception:
            self._pixel_size_um = None
        self._time_increment_s = self.dt if self.Nt > 1 and self.dt > 0 else None
        self._physical_size_z_um = self.deltaZ if self.NZ > 1 else None
        self.timestamp_acquisition_started = acquisition_parameters.acquisition_start_time
        self.timestamp_prev_timepoint_started = None

        _channel_display_names = list(self.observation_state_names)
        _n_channels = len(_channel_display_names)

        self.acquisition_info = AcquisitionInfo(
            total_time_points=self.Nt,
            total_z_levels=self.NZ,
            total_channels=_n_channels,
            channel_names=_channel_display_names,
            experiment_path=self.experiment_path,
            time_increment_s=self._time_increment_s,
            physical_size_z_um=self._physical_size_z_um,
            physical_size_x_um=self._pixel_size_um,
            physical_size_y_um=self._pixel_size_um,
        )

        self.time_point = 0
        self._first_fov_pre_moved = False
        self.af_fov_count = 0
        self.num_fovs = 0
        self.total_scans = 0
        self._z_pos_proposal = {}
        self.scan_region_fov_coords_mm = (
            acquisition_parameters.scan_position_information.scan_region_fov_coords_mm.copy()
        )
        self.scan_region_coords_mm = acquisition_parameters.scan_position_information.scan_region_coords_mm
        self.scan_region_names = acquisition_parameters.scan_position_information.scan_region_names
        # Per-region laser-AF focus targets. Regions without an entry fall back to
        # `_base_laser_af_reference` — the global reference loaded in the controller
        # at worker construction — so a region that follows one with a distinct
        # reference still corrects to the right target, independent of scan order.
        self._region_laser_af_references = dict(
            acquisition_parameters.scan_position_information.scan_region_laser_af_references
        )
        self._base_laser_af_reference = (
            self.laser_auto_focus_controller.get_active_reference()
            if self.laser_auto_focus_controller is not None
            else None
        )
        self.z_stacking_config = acquisition_parameters.z_stacking_config  # default 'from bottom'
        self.z_range = acquisition_parameters.z_range

        self.t_dpc = []
        self.t_inf = []
        self.t_over = []

        self.count = 0

        self.merged_image = None
        self.image_count = 0

        # This is for keeping track of whether or not we have the last image we tried to capture.
        # NOTE(imo): Once we do overlapping triggering, we'll want to keep a queue of images we are expecting.
        # For now, this is an improvement over blocking immediately while waiting for the next image!
        self._ready_for_next_trigger = threading.Event()
        # Set this to true so that the first frame capture can proceed.
        self._ready_for_next_trigger.set()
        # This is cleared while ANY frame's image_callback (decode → job
        # dispatch) is still in flight. With deferred decode, multiple frames
        # can overlap, so we count outstanding frames under _outstanding_lock
        # and only set the idle event when the count drops back to zero.
        self._image_callback_idle = threading.Event()
        self._image_callback_idle.set()
        self._outstanding_frames = 0
        self._outstanding_lock = threading.Lock()
        # Frame-id-keyed hand-off of CaptureInfo between _on_frame_arrived
        # (SDK thread, snapshots the info before the next trigger overwrites
        # _current_capture_info) and _image_callback (decode thread, reads
        # the info when dispatching jobs). Without this, deferred decode
        # would read the NEXT capture's info instead of its own.
        self._pending_capture_info_by_frame_id: Dict[int, CaptureInfo] = {}
        # Per-capture timing breakdown populated by acquire_camera_image and
        # _image_callback; read after the wait returns so we can split the
        # "exposure_time_done_sleep_hw or wait_for_image_sw" window into
        # sub-timers. Reset before each send_trigger.
        self._capture_ts: dict = {}
        # Stage-move pipelining scaffolding (inactive by default — currently
        # move_to_coordinate blocks synchronously because MCU serial contention
        # between stage motion and shutter commands made async moves a wash).
        # Kept live so a future change can flip move_to_coordinate to fire both
        # axes non-blocking + set _pending_move_settle=True, and the existing
        # _wait_for_move_settled() call sites (perform_autofocus top,
        # acquire_camera_image before trigger) will join motion / run the
        # stabilization sleep automatically. Primary use case: rigs where
        # illumination is on NIDAQ (independent bus from the MCU stage motor),
        # so apply_observation_state / illuminate_for_capture can run in
        # parallel with in-flight motion.
        self._pending_move_settle: bool = False
        self._pending_move_stabilization_s: float = 0.0
        # ObservationState preset cache — populated during prewarm, consulted by
        # _apply_observation_state. Avoids re-parsing the same YAML 27+ times
        # during a multipoint scan (2 presets × 27 FOVs = 54 redundant loads).
        # Seeded here with any inline (run-only) states supplied by the controller
        # so disk loads are skipped for synthetic states like "live".
        self._observation_preset_cache: Dict[str, ObservationState] = dict(
            acquisition_parameters.inline_observation_states or {}
        )
        # This is protected by the threading event above (aka set after clear, take copy before set)
        self._current_capture_info: Optional[CaptureInfo] = None
        self._last_illumination_config_name: Optional[str] = None
        # This is only touched via the image callback path.  Don't touch it outside of there!
        self._current_round_images = {}

        # Laser-AF per-FOV offset table. `_fov_z_map[(region_id, fov_index)]` is the
        # absolute Z (mm) measured at that FOV, populated by `_seed_fov_z_map()` (scan
        # mode) or lazily in `perform_autofocus` (lazy mode). Per-region anchors track
        # the latest laser-AF measurement so we can correct for rigid-body drift via
        # offsets from the static table.
        self._laser_af_seed_mode = acquisition_parameters.laser_af_seed_mode
        self._laser_af_refresh_every_n_fovs = max(1, int(acquisition_parameters.laser_af_refresh_every_n_fovs))
        self._laser_af_consistency_threshold_um = float(acquisition_parameters.laser_af_consistency_threshold_um)
        self._laser_af_check_last_fov_per_region = bool(acquisition_parameters.laser_af_check_last_fov_per_region)
        self._fov_z_map: dict[tuple[str, int], float] = {}
        self._fov_z_delta_map: dict[tuple[str, int], float] = {}
        self._region_anchor_z_current: dict[str, float] = {}
        self._region_anchor_fov: dict[str, int] = {}
        self._fovs_since_refresh: dict[str, int] = {}
        # Tracks transitions between regions so we can force an anchor refresh
        # and reset counters on each new region entry (e.g. well) within a
        # timepoint, not just the first time a region is ever seen.
        self._last_region_id: Optional[str] = None
        # Refreshes completed in the current region entry. Resets on region
        # transition. Drives consistency checks (>=2 = compare new measurement
        # vs table prediction) and end-of-region logic (==1 = no mid-region
        # refresh fired, optionally take a verification displacement).
        self._region_refresh_count_this_entry: int = 0

        self.skip_saving = acquisition_parameters.skip_saving
        self.file_saving_option = acquisition_parameters.file_saving_option
        # Tracks whether the most recent run_single_time_point created a per-timepoint
        # folder (so we know whether to drop a per-timepoint .done marker into it).
        self._wrote_per_timepoint_folder = False
        job_classes = []
        use_ome_tiff = self.file_saving_option == FileSavingOption.OME_TIFF
        use_zarr_v3 = self.file_saving_option == FileSavingOption.ZARR_V3
        if not self.skip_saving:
            if use_ome_tiff:
                job_classes.append(SaveOMETiffJob)
            elif use_zarr_v3:
                job_classes.append(SaveZarrJob)
            else:
                job_classes.append(SaveImageJob)

        if extra_job_classes:
            job_classes.extend(extra_job_classes)

        # Online postprocessing runs in its own runner (created only when a plan
        # uses it). It writes derived plates via inline SaveZarrJob / direct TIFF
        # and needs the zarr writer info + upload pipeline like the zarr runner.
        if self._has_postprocess and not self.skip_saving:
            job_classes.append(PostprocessJob)

        # Downsampled view generation setup
        # Only generate downsampled views for well-based acquisitions
        is_select_wells = acquisition_parameters.xy_mode == "Select Wells"
        is_loaded_wells = acquisition_parameters.xy_mode == "Load Coordinates" and self._is_well_based_acquisition()
        self._generate_downsampled_views = acquisition_parameters.generate_downsampled_views and (
            is_select_wells or is_loaded_wells
        )
        self._downsampled_view_manager: Optional[DownsampledViewManager] = None
        self._downsampled_well_resolutions_um = acquisition_parameters.downsampled_well_resolutions_um or [
            5.0,
            10.0,
            20.0,
        ]
        self._downsampled_plate_resolution_um = acquisition_parameters.downsampled_plate_resolution_um
        self._downsampled_z_projection = acquisition_parameters.downsampled_z_projection
        self._downsampled_interpolation_method = acquisition_parameters.downsampled_interpolation_method
        self._save_downsampled_well_images = acquisition_parameters.save_downsampled_well_images
        self._plate_num_rows = acquisition_parameters.plate_num_rows
        self._plate_num_cols = acquisition_parameters.plate_num_cols
        self._overlap_pixels: Optional[Tuple[int, int, int, int]] = None
        self._region_fov_counts: Dict[str, int] = {}  # Track total FOVs per region

        if self._generate_downsampled_views:
            # Ensure plate resolution is in well resolutions
            self._downsampled_well_resolutions_um = ensure_plate_resolution_in_well_resolutions(
                self._downsampled_well_resolutions_um,
                self._downsampled_plate_resolution_um,
            )
            # Add DownsampledViewJob to job classes
            job_classes.append(DownsampledViewJob)
            # Pre-calculate FOV counts per region
            for region_id, coords in self.scan_region_fov_coords_mm.items():
                self._region_fov_counts[region_id] = len(coords)
            mode = "Select Wells" if is_select_wells else "Load Coordinates (auto-detected)"
            self._log.info(
                f"Downsampled view generation enabled ({mode}). Resolutions: {self._downsampled_well_resolutions_um} um"
            )

        # Initialize backpressure controller for throttling acquisition when queue fills up.
        # If pre-warmed values are provided, use them for consistent tracking with the
        # pre-warmed job runner. Otherwise, BackpressureController creates its own values.
        bp_kwargs = {
            "max_jobs": control._def.ACQUISITION_MAX_PENDING_JOBS,
            "max_mb": control._def.ACQUISITION_MAX_PENDING_MB,
            "timeout_s": control._def.ACQUISITION_THROTTLE_TIMEOUT_S,
            "enabled": control._def.ACQUISITION_THROTTLING_ENABLED,
        }
        if prewarmed_bp_values is not None:
            bp_kwargs["bp_values"] = prewarmed_bp_values
        self._backpressure = BackpressureController(**bp_kwargs)

        # For now, use 1 runner per job class.  There's no real reason/rationale behind this, though.  The runners
        # can all run any job type.  But 1 per is a reasonable arbitrary arrangement while we don't have a lot
        # of job types.  If we have a lot of custom jobs, this could cause problems via resource hogging.
        self._job_runners: List[Tuple[Type[Job], JobRunner]] = []
        self._log.info(f"Acquisition.USE_MULTIPROCESSING = {Acquisition.USE_MULTIPROCESSING}")

        # Get the current log file path to share with subprocess workers
        log_file_path = squid.logging.get_current_log_file_path()

        # Build ZarrWriterInfo if using ZARR_V3 format.
        # Output is always OME-NGFF 5D per FOV:
        # - HCS:     {experiment_path}/plate.ome.zarr/{row}/{col}/{fov}/0
        # - Non-HCS: {experiment_path}/zarr/{region}/fov_{n}.ome.zarr/0
        zarr_writer_info = None
        if use_zarr_v3:
            # HCS = well-based acquisition (Select Wells or Load Coordinates with well IDs).
            is_hcs = is_select_wells or is_loaded_wells

            # Per-region FOV counts (drives OME-NGFF well fields list).
            # Per-region per-FOV (y_um, x_um) translations (drives OME-NGFF multiscales translation).
            region_fov_counts: Dict[str, int] = {}
            fov_translations_um: Dict[str, Dict[int, Tuple[float, float]]] = {}
            for region_id, coords in self.scan_region_fov_coords_mm.items():
                region_key = str(region_id)
                region_fov_counts[region_key] = len(coords)
                fov_translations_um[region_key] = {
                    fov_idx: (coord[1] * 1000.0, coord[0] * 1000.0)  # (y_um, x_um) from (x_mm, y_mm, z_mm)
                    for fov_idx, coord in enumerate(coords)
                }

            # Channel axis for the dense layout = the global plan's imaged states
            # (stimulus-only states produce no frame, so they're excluded). For
            # ragged runs each frame self-describes its single-channel array via
            # CaptureInfo.save_*, so these global dims are the dense fallback.
            channel_names = list(self._global_plan.channel_order)
            channel_colors = []
            channel_wavelengths = []
            for pname in channel_names:
                color, wl = self._channel_display_meta(pname)
                channel_colors.append(color)
                channel_wavelengths.append(wl)
            # Dense T expands by frames-per-state; ragged overrides per-array.
            dense_t_size = self.Nt * (self._global_plan.frames_per_position // max(1, len(channel_names))) \
                if (self._global_plan.dense and channel_names) else self.Nt

            zarr_writer_info = ZarrWriterInfo(
                base_path=self.experiment_path,
                t_size=dense_t_size,
                c_size=len(channel_names),
                z_size=self.NZ,
                is_hcs=is_hcs,
                region_fov_counts=region_fov_counts,
                fov_translations_um=fov_translations_um,
                pixel_size_um=self._pixel_size_um,
                z_step_um=self._physical_size_z_um,
                time_increment_s=self._time_increment_s,
                channel_names=channel_names,
                channel_colors=channel_colors,
                channel_wavelengths=channel_wavelengths,
            )
            mode_str = "HCS plate hierarchy" if is_hcs else "per-FOV 5D (OME-NGFF compliant)"
            self._log.info(f"ZARR_V3 output: {mode_str}, base path: {self.experiment_path}")

        # Live ZARR_V3 upload (per-acquisition setting). When enabled, spawn a
        # dedicated UploadWorker process and wire its input queue into the
        # SaveZarrJob runner. Acquisition continues even if the network is
        # down — uploads queue, deletions defer.
        self._upload_target: Optional[UploadTarget] = None
        self._upload_worker: Optional[UploadWorker] = None
        self._save_zarr_runner: Optional[JobRunner] = None
        self._upload_tasks_by_tp: Dict[int, set] = {}
        self._upload_results_by_tp: Dict[int, List[UploadResult]] = {}
        self._upload_expected_count_by_tp: Dict[int, int] = {}
        self._upload_deletion_done: Set[int] = set()
        self._upload_failed_tasks: List[UploadResult] = []
        # Order-independent completion tracking: count of UploadResults
        # consumed, plus the task_ids they carried (guards the case where a
        # result arrives before its BarrierResult registers the task).
        self._upload_results_received = 0
        self._upload_completed_task_ids: Set[str] = set()
        self._upload_failed_deletions = 0
        self._upload_health_last_check = 0.0
        self._upload_health_last_warn = 0.0
        if (
            use_zarr_v3
            and getattr(acquisition_parameters, "zarr_upload_enabled", False)
            and getattr(acquisition_parameters, "zarr_upload_remote_root", "")
        ):
            self._upload_target = UploadTarget(
                enabled=True,
                remote_root=acquisition_parameters.zarr_upload_remote_root,
                local_base=self.experiment_path,
                delete_after_verify=acquisition_parameters.zarr_upload_delete_after_verify,
            )
            manifest_path = os.path.join(self.experiment_path, "upload_manifest.jsonl")
            self._upload_worker = UploadWorker(
                target=self._upload_target, manifest_path=manifest_path
            )
            self._upload_worker.start()
            # Reachable by the app-close/atexit kill path from the moment it
            # exists — NOT only once the end-of-run drainer takes ownership.
            register_live_upload_worker(self._upload_worker)
            self._log.info(
                f"Started UploadWorker (pid={self._upload_worker.pid}) "
                f"-> {self._upload_target.remote_root}, "
                f"delete_after_verify={self._upload_target.delete_after_verify}"
            )
            # Each FOV visit emits one FlushAndStageUploadJob (⇒ one BarrierResult
            # ⇒ one UploadResult) PER on-disk plate key, not one per FOV: a ragged
            # run has len(array_keys) raw plates, and postprocessing adds one per
            # derived output. Seeding to bare FOV count made _maybe_batched_delete
            # fire after only 1/N barriers for ragged/postprocess runs (deletion
            # would then never reclaim later shards). Count keys per FOV per region.
            barriers_per_tp = 0
            for region_id, coords in self.scan_region_fov_coords_mm.items():
                plan = self._get_region_plan(region_id)
                raw_keys = 1 if plan.dense else len(plan.array_keys)
                keys_per_fov = raw_keys + len(plan.derived_output_keys)
                barriers_per_tp += len(coords) * keys_per_fov
            for tp in range(self.Nt):
                self._upload_expected_count_by_tp[tp] = barriers_per_tp
                self._upload_tasks_by_tp[tp] = set()
                self._upload_results_by_tp[tp] = []

        # Use pre-warmed job runner if available, otherwise create new ones.
        # IMPORTANT: Only use pre-warmed runner if BOTH runner AND backpressure values
        # are available. Using a runner without matching backpressure values would cause
        # the BackpressureController to track different counters than the JobRunner.
        # Also: the upload pipeline must be installed at subprocess startup (the input
        # queue is class-installed in JobRunner.run()), so a pre-warmed runner started
        # before the upload config was known cannot satisfy an upload-enabled run.
        can_use_prewarmed = (
            prewarmed_job_runner is not None
            and prewarmed_bp_values is not None
            and self._upload_target is None
        )
        used_prewarmed = False
        upload_queue_for_zarr_runner = (
            self._upload_worker.input_queue if self._upload_worker is not None else None
        )
        for job_class in job_classes:
            job_runner = None
            if Acquisition.USE_MULTIPROCESSING:
                # Try to use pre-warmed runner for the first job class
                if can_use_prewarmed and not used_prewarmed:
                    if prewarmed_job_runner.is_ready():
                        self._log.info(f"Using pre-warmed job runner for {job_class.__name__} jobs")
                        job_runner = prewarmed_job_runner
                        # Configure it with current acquisition settings
                        job_runner.set_acquisition_info(self.acquisition_info)
                        if zarr_writer_info:
                            job_runner.set_zarr_writer_info(zarr_writer_info)
                        used_prewarmed = True
                    else:
                        self._log.warning(
                            f"Pre-warmed job runner not ready (possibly hung during warmup), "
                            f"shutting it down and creating new one for {job_class.__name__}"
                        )
                        # Shutdown the hung pre-warmed runner to avoid resource leak
                        try:
                            prewarmed_job_runner.shutdown(timeout_s=1.0)
                        except Exception as e:
                            self._log.error(f"Error shutting down hung pre-warmed runner: {e}")
                        # Don't try to use pre-warmed runner again for subsequent job classes
                        can_use_prewarmed = False

                if job_runner is None:
                    self._log.info(f"Creating job runner for {job_class.__name__} jobs")
                    # Only the SaveZarrJob runner needs the upload pipeline — barriers
                    # are dispatched onto its queue so they FIFO-order behind preceding
                    # SaveZarrJobs for the same (t, fov).
                    # The zarr runner AND the postprocess runner both flush+stage
                    # uploads (the latter for its derived plates, via inline
                    # FlushAndStageUploadJob), so both need the upload pipeline.
                    needs_upload = job_class in (SaveZarrJob, PostprocessJob)
                    runner_upload_target = self._upload_target if needs_upload else None
                    runner_upload_queue = upload_queue_for_zarr_runner if needs_upload else None
                    runner_upload_counter = (
                        self._upload_worker.tasks_submitted_value
                        if needs_upload and self._upload_worker is not None
                        else None
                    )
                    job_runner = control.core.job_processing.JobRunner(
                        self.acquisition_info,
                        cleanup_stale_ome_files=use_ome_tiff,
                        log_file_path=log_file_path,
                        # Pass backpressure shared values for cross-process tracking
                        bp_pending_jobs=self._backpressure.pending_jobs_value,
                        bp_pending_bytes=self._backpressure.pending_bytes_value,
                        bp_capacity_event=self._backpressure.capacity_event,
                        # Pass zarr writer info for ZARR_V3 format
                        zarr_writer_info=zarr_writer_info,
                        upload_target=runner_upload_target,
                        upload_input_queue=runner_upload_queue,
                        upload_tasks_submitted=runner_upload_counter,
                    )
                    job_runner.start()
                    # Subprocess starts warming up in background - don't block here

            self._job_runners.append((job_class, job_runner))
            if job_class is SaveZarrJob:
                self._save_zarr_runner = job_runner

        # Cache zarr_writer_info so barrier dispatch can resolve writer keys
        # (HCS vs per-FOV layout) without re-deriving the path.
        self._zarr_writer_info: Optional[ZarrWriterInfo] = zarr_writer_info
        self._abort_on_failed_job = abort_on_failed_jobs
        self._first_job_dispatched = False  # Track if we've waited for subprocess warmup

    def update_use_piezo(self, value):
        self.use_piezo = value
        self._log.info(f"MultiPointWorker: updated use_piezo to {value}")

    def _is_well_based_acquisition(self) -> bool:
        """Check if regions represent a valid well-based acquisition.

        Returns True if:
        - All region names are valid well IDs (A1, B2, etc.)
        - All regions have the same FOV grid pattern (same distinct X and Y counts)
        """
        if not self.scan_region_names:
            self._log.debug(
                "_is_well_based_acquisition: no scan_region_names defined; treating as non well-based acquisition"
            )
            return False

        # Check all region names are valid well IDs using parse_well_id
        for region_id in self.scan_region_names:
            if not region_id:
                self._log.debug(
                    "_is_well_based_acquisition: encountered empty region_id in scan_region_names; "
                    "treating as invalid well-based acquisition"
                )
                return False
            try:
                parse_well_id(region_id)
            except ValueError as exc:
                self._log.debug(
                    "_is_well_based_acquisition: region_id '%s' is not a valid well ID: %s; "
                    "treating as invalid well-based acquisition",
                    region_id,
                    exc,
                )
                return False

        # Check all wells have same grid size
        grid_sizes = set()
        for region_id, coords in self.scan_region_fov_coords_mm.items():
            if not coords:
                self._log.debug(
                    "_is_well_based_acquisition: region '%s' has no FOV coordinates; skipping in grid-size check",
                    region_id,
                )
                continue
            x_positions = set(round(c[0], 4) for c in coords)  # Round to avoid float precision issues
            y_positions = set(round(c[1], 4) for c in coords)
            grid_sizes.add((len(x_positions), len(y_positions)))

        # All wells should have the same grid pattern
        if not grid_sizes:
            self._log.debug(
                "_is_well_based_acquisition: no valid FOV coordinates found for any region; "
                "treating as non well-based acquisition"
            )
            return False

        if len(grid_sizes) > 1:
            self._log.debug(
                "_is_well_based_acquisition: inconsistent FOV grid sizes detected across wells: %s; "
                "treating as non well-based acquisition",
                grid_sizes,
            )
            return False

        self._log.debug(
            "_is_well_based_acquisition: valid well-based acquisition detected with grid size %s",
            next(iter(grid_sizes)),
        )
        return True

    def _channel_step_count(self) -> int:
        return len(self.observation_state_names)

    def _get_region_plan(self, region_id):
        """Return the resolved RegionPlan for a region (its override or global)."""
        return self._region_plans.get(str(region_id), self._global_plan)

    def _get_observation_states_for_region(self, region_id: str) -> list:
        """Imaged channel axis (C order) active for this region."""
        return list(self._get_region_plan(region_id).channel_order)

    def _channel_display_meta(self, name: str) -> Tuple[str, Optional[int]]:
        """(display_color, wavelength_nm) for an imaged observation state, cached."""
        if name in self._channel_meta_cache:
            return self._channel_meta_cache[name]
        color, wavelength = "#FFFFFF", None
        try:
            repo = self.microscope.config_repo
            st = self._observation_preset_cache.get(name) or repo.load_observation_preset(name)
            if st is not None and st.illuminator_states:
                color = st.display_color
                active = st.active_illuminator_states
                ist = active[0] if active else st.illuminator_states[0]
                illum = repo.get_illumination_config()
                if illum and ist.illumination_channel:
                    ch_def = illum.get_channel_by_name(ist.illumination_channel)
                    wavelength = ch_def.wavelength_nm if ch_def else None
        except Exception:
            pass
        self._channel_meta_cache[name] = (color, wavelength)
        return self._channel_meta_cache[name]

    def _build_save_layout(self, region_plan, event):
        """Build the self-describing SaveLayout for one imaged event at the
        current timepoint, using the dense/ragged layout from the region plan."""
        from control.models.acquisition_cycle import frame_coord, SaveLayout, array_key_for

        coord = frame_coord(region_plan, self.Nt, self.time_point, event)
        if coord.array_key is None:
            # Dense: one array, all imaged channels.
            names = list(region_plan.channel_order)
        else:
            # Ragged: a single-channel per-(state, z-mode) array.
            names = [event.observation_state]
        colors, wavelengths = [], []
        for n in names:
            c, w = self._channel_display_meta(n)
            colors.append(c)
            wavelengths.append(w)
        # Only disambiguate filenames when this (state, z-mode) repeats within a
        # position; key by the array group so a ref-z step is counted correctly.
        group_key = array_key_for(event.observation_state, event.acquire_z_stack)
        repeats = region_plan.frame_counts.get(group_key, 1) > 1
        frame_suffix = f"f{event.state_frame_index:0{FILE_ID_PADDING}}" if repeats else None
        # Z extent of this frame's array: full stack for a normal step, 1 for a
        # reference-z-only capture (whose single frame is written at z=0).
        z_size = self.NZ if event.acquire_z_stack else 1
        return SaveLayout(
            array_key=coord.array_key,
            t_index=coord.t_index,
            c_index=coord.c_index,
            t_size=coord.t_size,
            c_size=coord.c_size,
            cycle_event_index=event.cycle_event_index,
            state_frame_index=event.state_frame_index,
            channel_names=names,
            channel_colors=colors,
            channel_wavelengths=wavelengths,
            frame_suffix=frame_suffix,
            z_size=z_size,
        )

    def _seed_camera_for_first_observation_state(self) -> None:
        """Apply the first observation state's camera_live snapshot once before streaming.

        Per-FOV applies skip the camera_live block (ROI/binning/camera_mode/pixel_format/
        trigger) for performance and to avoid re-asserting stale fields between channel
        switches. That block still has to run *once* so streaming starts in the mode the
        first observation state actually requires; otherwise the camera carries over
        whatever the live controller left it in.
        """
        if not self.observation_state_names:
            return
        first_name = self.observation_state_names[0]
        state = self._observation_preset_cache.get(first_name)
        if state is None:
            try:
                state = self.microscope.config_repo.load_observation_preset(first_name)
            except Exception as exc:
                self._log.warning("Could not load first observation state %r: %s", first_name, exc)
                return
            if state is None:
                return
            self._observation_preset_cache[first_name] = state
        if state.camera_live is None:
            return
        obs_controller = self.liveController.obs_controller
        try:
            obs_controller._apply_camera_live_snapshot(
                state.camera_live,
                apply_trigger_settings=False,  # multipoint already configured SOFTWARE trigger; preset's saved trigger (e.g. Continuous) would cause auto-fired frames here.
            )
        except Exception as exc:
            self._log.warning("Could not seed camera_live snapshot from %r: %s", first_name, exc)

    def _apply_observation_state(self, preset_name: str) -> ObservationState:
        state = self._observation_preset_cache.get(preset_name)
        if state is None:
            with self._timing.get_timer("load_observation_preset"):
                state = self.microscope.config_repo.load_observation_preset(preset_name)
            if state is None:
                raise ValueError(f"observation state not found: {preset_name!r}")
            self._observation_preset_cache[preset_name] = state
        with self._timing.get_timer("apply_observation_state_to_hardware"):
            self.liveController.obs_controller.apply_observation_state_preset(
                state,
                emission_filter_wheel=self._emission_filter_wheel,
                apply_camera_live_snapshot=False,  # ROI/binning/camera_mode/trigger were set up before acquisition started; re-applying them per channel switch is wasteful and risks dragging in stale fields (e.g. a "default" camera_mode saved by a different camera class).
            )
        return state

    def _apply_current_illumination_state_to_hardware(self) -> None:
        """Assert the illumination controller's current logical on/off state on hardware.

        Uses the batched path (``set_channel_states_batch``) when available so
        the shutter off→on transition between two channels on the same MCU is
        a single ``wait_till_operation_is_completed`` rather than one wait per
        channel — cuts per-capture cost from ~2× MCU round-trip to ~1×.
        """
        ic = self.microscope.illumination_controller
        with self._timing.get_timer("get_shutter_state"):
            try:
                logical_states = ic.get_shutter_state()
            except Exception as e:
                self._log.warning("Could not read illumination logical state for capture: %s", e)
                return
        with self._timing.get_timer("apply_shutter_state_to_hardware"):
            try:
                ic.set_channel_states_batch(logical_states, force_hardware=True)
            except Exception as e:
                self._log.warning("Could not apply illumination state batch: %s", e)

    def _turn_off_capture_illumination_preserving_logical_state(self) -> None:
        """Clear hardware illumination after a snap without losing the saved logical state."""
        try:
            self.microscope.illumination_controller.turn_off_all(preserve_logical_state=True)
        except Exception as e:
            self._log.warning("Could not turn off capture illumination after snap: %s", e)

    def _apply_illumination_for_waveform_capture(self, config: ObservationState) -> None:
        """Apply illumination for a waveform-driven capture (DC on, timed gates LOW).

        Thin delegator to :func:`control.core.waveform_capture.apply_illumination_for_waveform_capture`
        so multipoint and live preview drive the gated pulse identically.
        """
        apply_illumination_for_waveform_capture(self.microscope, config, self._log)

    def _arm_nidaq_pulse_for_capture(self, config: ObservationState) -> Optional[Callable[[], None]]:
        """Arm the per-frame NIDAQ pulse for a waveform-driven capture.

        Thin delegator to :func:`control.core.waveform_capture.arm_nidaq_pulse_for_capture`.
        On a wait-timeout the failure handler logs once and aborts the run, so the
        user fixes the wiring rather than watching every FOV produce dark frames.
        """
        return arm_nidaq_pulse_for_capture(
            self.microscope,
            config,
            log=self._log,
            get_timer=self._timing.get_timer,
            on_wait_failure=self._on_nidaq_pulse_wait_failure,
        )

    def _on_nidaq_pulse_wait_failure(self, terminal, timeout_s, name, error) -> None:
        """Abort the acquisition (logging once) when the per-frame pulse never fired."""
        if not getattr(self, "_nidaq_pulse_failure_logged", False):
            self._log.error(
                "NIDAQ pulse waveform did not fire for state '%s' within %.2fs. "
                "Check that the camera frame-signal terminal (%s) is wired to the "
                "camera's exposure-active / frame-signal output on this rig. "
                "Aborting acquisition to avoid producing dark frames on every FOV.",
                name, timeout_s, terminal,
            )
            self._log.debug("NIDAQ wait_until_done error detail: %s", error)
            self._nidaq_pulse_failure_logged = True
        try:
            self.request_abort_fn()
        except Exception:
            pass

    def _run_nidaq_stimulus(self, config: ObservationState) -> None:
        """Fire an NIDAQ pulse-comb stimulus step (no camera capture).

        Builds the same kind of ``WaveformData`` the capture-pulse path uses,
        but arms with ``TriggerSource.SOFTWARE`` and a total task length of
        ``config.stimulus_duration_ms``. ``start_trigger()`` fires the comb
        immediately; this method blocks until the comb completes (or aborts
        with a clear error if it doesn't). Designed to share the per-FOV
        channel loop with capture steps — the per-FOV ``active_step`` counter
        still increments, so the AF guard fires on the first active step
        regardless of whether that step is a capture or a stimulus.
        """
        nidaq = getattr(self.microscope.addons, "nidaq", None)
        if nidaq is None:
            self._log.warning(
                "Stimulus-only state '%s' selected but no NIDAQ is configured; skipping",
                config.name,
            )
            return

        if config.stimulus_duration_ms is None or config.stimulus_duration_ms <= 0:
            self._log.error(
                "Stimulus-only state '%s' has no positive stimulus_duration_ms; skipping",
                config.name,
            )
            return

        ic = self.microscope.illumination_controller
        sample_rate_hz = float(control._def.NIDAQ_PULSE_SAMPLE_RATE_HZ)

        # DC intensity / serial laser power / NL5+CellX must be live before the
        # NIDAQ comb fires the gate. Standard (non-timed) active illuminators in
        # the same state get gated ON normally; timed channels stay LOW until
        # the per-step waveform drives them.
        self._apply_illumination_for_waveform_capture(config)

        try:
            waveform = build_pulse_waveform_for_state(
                config, ic, sample_rate_hz=sample_rate_hz,
            )
            do_lines = nidaq_lines_for_state(config, ic)
        except ValueError:
            self._log.exception("Failed to build NIDAQ stimulus waveform for state '%s'", config.name)
            # Make sure illumination doesn't stay on if the build failed mid-way.
            self._turn_off_capture_illumination_preserving_logical_state()
            raise

        prev_sample_rate_hz = float(getattr(nidaq, "sample_rate_hz", sample_rate_hz))
        prev_samples_per_channel = int(getattr(nidaq, "samples_per_channel", 0))
        prev_trigger_source = getattr(nidaq, "trigger_source", TriggerSource.SOFTWARE)
        prev_external_terminal = getattr(nidaq, "external_trigger_terminal", "")

        per_step_samples = next(iter(waveform.digital_output.values())).size
        stimulus_duration_s = float(config.stimulus_duration_ms) / 1000.0
        timeout_s = stimulus_duration_s + 0.5

        armed_ok = False
        with self._timing.get_timer("nidaq_stimulus_arm"):
            try:
                nidaq.sample_rate_hz = sample_rate_hz
                nidaq.samples_per_channel = per_step_samples
                nidaq.trigger_source = TriggerSource.SOFTWARE
                nidaq.configure_task_io(
                    ao_channels=[],
                    do_lines=do_lines,
                    di_lines=[],
                    ai_channels=[],
                )
                nidaq.prepare_for_acquisition()
                nidaq.set_waveforms(waveform)
                nidaq.arm()
                nidaq.start_trigger()  # SOFTWARE trigger -> fires immediately
                armed_ok = True
            except Exception:
                self._log.exception("Failed to arm NIDAQ stimulus for '%s'", config.name)
                try:
                    nidaq.release_tasks()
                except Exception:
                    pass
                try:
                    restore_fn = getattr(nidaq, "restore_after_acquisition", None)
                    if callable(restore_fn):
                        restore_fn()
                except Exception:
                    pass

        try:
            if armed_ok:
                with self._timing.get_timer("nidaq_stimulus_wait"):
                    try:
                        nidaq.wait_until_done(timeout_s=timeout_s)
                    except Exception as e:
                        self._log.error(
                            "NIDAQ stimulus '%s' did not complete within %.2fs: %s",
                            config.name, timeout_s, e,
                        )
                try:
                    nidaq.release_tasks()
                except Exception as e:
                    self._log.warning("NIDAQ release_tasks failed for stimulus '%s': %s", config.name, e)
                # Drive each timed DO line LOW before the live-output restore.
                # A FINITE DAQmx output task holds its last written sample after
                # release, so combs whose final pulse runs to the window boundary
                # leave the gate HIGH. Lines that were never live (typical for
                # stimulus channels) aren't in the prepare/restore snapshot, so
                # restore_after_acquisition can't undo that — this explicit write
                # does. start_live_output also seeds the persistent DO task so
                # later operations see the line in a known state.
                try:
                    nidaq.start_live_output(do_values={line: False for line in do_lines})
                except Exception as e:
                    self._log.warning(
                        "Post-stimulus DO clear failed for '%s' on lines %s: %s",
                        config.name, do_lines, e,
                    )
                try:
                    restore_fn = getattr(nidaq, "restore_after_acquisition", None)
                    if callable(restore_fn):
                        restore_fn()
                except Exception as e:
                    self._log.warning(
                        "NIDAQ restore_after_acquisition failed for stimulus '%s': %s",
                        config.name, e,
                    )
        finally:
            # Restore knob snapshot so the next NIDAQ user (capture pulse,
            # fast acquisition, DAQ-only widget) sees the device unchanged.
            try:
                nidaq.sample_rate_hz = prev_sample_rate_hz
                if prev_samples_per_channel:
                    nidaq.samples_per_channel = prev_samples_per_channel
                nidaq.trigger_source = prev_trigger_source
                nidaq.external_trigger_terminal = prev_external_terminal
            except Exception as e:
                self._log.warning("NIDAQ knob restore failed after stimulus '%s': %s", config.name, e)

            # Per-capture illumination off invariant — applies to stimulus steps too.
            self._turn_off_capture_illumination_preserving_logical_state()

    def run(self):
        this_image_callback_id = None
        self._last_illumination_config_name = None
        # Share the timing manager with the observation state controller so
        # apply_observation_state_preset's sub-steps contribute to the report.
        obs_controller = self.liveController.obs_controller
        obs_controller._timing = self._timing
        # Same wiring for the laser autofocus controller so move_to_target's
        # sub-steps (laser toggles, frame capture, spot detection, cross-corr)
        # contribute to the report.
        laser_af = self.laser_auto_focus_controller
        if laser_af is not None:
            laser_af._timing = self._timing
        try:
            first_region, first_region_coords = list(self.scan_region_fov_coords_mm.items())[0]
            first_coords_mm = first_region_coords[0]
            self._log.info(f"Moving to first region '{first_region}' first FOV coordinates {first_coords_mm} mm to start acquisition")
            start_time = time.perf_counter_ns()
            # Force a clean stop→start so any streaming state left by live mode (queued
            # frames, stale trigger config) is discarded before acquisition begins.
            self.camera.stop_streaming()
            # One-time apply of the first observation state's camera_live snapshot
            # (ROI, binning, camera_mode, pixel_format, trigger) while streaming is
            # stopped — Tucsen camera mode switches while streaming have caused
            # issues. Per-FOV applies skip this block (apply_camera_live_snapshot=
            # False below); without this seed, streaming would start in whatever
            # mode live mode left the camera in.
            self._seed_camera_for_first_observation_state()
            self.camera.start_streaming()
            # self._log.info(f"Camera acquisition mode {self.camera.get_acquisition_mode()}, trigger mode {self.camera._capture_mode_genicam}")
            this_image_callback_id = self.camera.add_frame_callback(self._image_callback)
            # Deferred-decode cameras (Tucsen SDK-callback path) fire this as
            # soon as raw bytes arrive, before decode. Snapshots capture_info
            # and signals _ready_for_next_trigger so the worker can issue the
            # next trigger in parallel with the still-running decode. Cameras
            # without this API fall back to the synchronous path inside
            # _image_callback.
            self._use_deferred_decode_callback = hasattr(self.camera, "add_frame_arrived_callback")
            if self._use_deferred_decode_callback:
                self.camera.add_frame_arrived_callback(self._on_frame_arrived)
            sleep_time = min(self.dt / 20.0, 0.5)

            # Send Slack acquisition start notification
            if self._slack_notifier is not None:
                try:
                    self._slack_notifier.notify_acquisition_start(
                        experiment_id=self.experiment_ID or "unknown",
                        num_regions=len(self.scan_region_names) if self.scan_region_names else 0,
                        num_timepoints=self.Nt,
                        num_channels=self._channel_step_count(),
                        num_z_levels=self.NZ,
                    )
                except Exception as e:
                    self._log.warning(f"Failed to send Slack acquisition start notification: {e}")

            # Pre-acquisition laser-AF seed scan. Populates `_fov_z_map` so later
            # timepoints use the table-and-anchor path in perform_autofocus rather
            # than a full laser AF at every FOV. Lazy mode skips this and seeds
            # on first visit during normal acquisition instead.
            if (
                self.do_reflection_af
                and self._laser_af_seed_mode == "scan"
                and self.laser_auto_focus_controller is not None
            ):
                # Suppress laser-AF sub-timers (af:*) during the one-time seed
                # scan so they reflect only per-FOV acquisition costs. The outer
                # laser_af_seed_scan bucket still captures the full seed-scan
                # wall time for a complete breakdown. Matches the
                # obs_controller._timing=None pattern used by prewarm.
                saved_af_timing = self.laser_auto_focus_controller._timing
                self.laser_auto_focus_controller._timing = None
                try:
                    with self._timing.get_timer("laser_af_seed_scan"):
                        self._seed_fov_z_map()
                finally:
                    self.laser_auto_focus_controller._timing = saved_af_timing

            while self.time_point < self.Nt:
                # check if abort acquisition has been requested
                if self.abort_requested_fn():
                    self._log.debug("In run, abort_acquisition_requested=True")
                    break

                if self.fluidics and self.use_fluidics:
                    self.fluidics.update_port(self.time_point)  # use the port in PORT_LIST
                    # For MERFISH, before imaging, run the first 3 sequences (Add probe, wash buffer, imaging buffer)
                    self.fluidics.run_before_imaging()
                    self.fluidics.wait_for_completion()
                    # Check for abort after fluidics completes (user may have stopped during fluidics)
                    if self.abort_requested_fn():
                        self._log.debug("Abort requested after fluidics, skipping imaging")
                        break

                with self._timing.get_timer("run_single_time_point"):
                    self.timestamp_prev_timepoint_started = time.time()
                    self.run_single_time_point()

                if self.fluidics and self.use_fluidics:
                    # For MERFISH, after imaging, run the following 2 sequences (Cleavage buffer, SSC rinse)
                    self.fluidics.run_after_imaging()
                    self.fluidics.wait_for_completion()

                self.time_point = self.time_point + 1
                if self.dt == 0:  # continous acquisition
                    pass
                else:  # timed acquisition

                    # check if the acquisition has taken longer than dt or integer multiples of dt, if so immediately start the next time point without waiting to catch up (but still check for abort request to allow user to stop if acquisition is running too long)
                    if time.time() > self.timestamp_prev_timepoint_started + self.dt:
                        self._log.info(f"Acquisition is running behind schedule (time since last time point start: {time.time() - self.timestamp_prev_timepoint_started} [s])")
                        if self.abort_requested_fn():
                            self._log.debug("In run wait loop, abort_acquisition_requested=True")
                            break
                        pass

                    # check if it has reached Nt
                    if self.time_point == self.Nt:
                        break  # no waiting after taking the last time point

                    if time.time() < self.timestamp_prev_timepoint_started + self.dt:
                        self._log.info("Waiting for next time point (%.2f [s] until next time point start)", (self.timestamp_prev_timepoint_started + self.dt) - time.time())
                        self.move_to_coordinate(first_coords_mm, first_region, 0)  # Move to the first coordinate of the first region while waiting for the next time point to start, to save time on stage movement and allow for any necessary settling to occur during the wait
                        self._first_fov_pre_moved = True

                    # wait until it's time to do the next acquisition
                    while time.time() < self.timestamp_prev_timepoint_started + self.dt:
                        if self.abort_requested_fn():
                            self._log.debug("In run wait loop, abort_acquisition_requested=True")
                            break
                        self._sleep(sleep_time)

            elapsed_time = time.perf_counter_ns() - start_time
            self._log.info("Time taken for acquisition: " + str(elapsed_time / 10**9))

            # Since we use callback based acquisition, make sure to wait for any final images to come in
            self._wait_for_outstanding_callback_images()
            self._log.info(f"Time taken for acquisition/processing: {(time.perf_counter_ns() - start_time) / 1e9} [s]")
        except TimeoutError as te:
            origin = None
            tb = te.__traceback__
            this_file = os.path.abspath(__file__)
            while tb is not None:
                if os.path.abspath(tb.tb_frame.f_code.co_filename) == this_file:
                    origin = (tb.tb_frame.f_code.co_name, tb.tb_lineno)
                tb = tb.tb_next
            if origin:
                self._log.error(f"Operation timed out during acquisition at {origin[0]}() (multi_point_worker.py:{origin[1]}), aborting acquisition!")
            else:
                self._log.error(f"Operation timed out during acquisition, aborting acquisition!")
            self._log.error(te)
            self.request_abort_fn()
        # except Exception as e:
        #     self._log.exception(e)
        #     raise
        finally:
            # We do this above, but there are some paths that skip the proper end of the acquisition so make
            # sure to always wait for final images here before removing our callback.
            self._wait_for_outstanding_callback_images()
            # Timing collection and report emission are gated by TimingManager's
            # class-level switch (SQUID_TIMING_REPORT=1 env var). When disabled,
            # get_timer() returns a null context manager so every `with ...`
            # site across the worker/obs_controller/laser_af path is a no-op,
            # and get_report() returns a short "disabled" line. When enabled,
            # the full report surfaces at INFO.
            _timing_level = logging.INFO if utils.TimingManager.is_enabled() else logging.DEBUG
            self._log.log(_timing_level, self._timing.get_report())
            self._log.log(_timing_level, self._timing.get_report(sort=True))
            # Detach the timing manager from the obs controller so live-mode
            # calls from widgets don't keep writing into the acquisition's
            # timing report.
            obs_controller._timing = None
            if laser_af is not None:
                laser_af._timing = None
            if this_image_callback_id:
                # Guard each SDK call: a raising/wedging camera teardown here
                # used to skip _finish_jobs entirely — no writer finalize, no
                # upload-drainer handoff, GUI stuck "Finalizing…".
                try:
                    self.camera.stop_streaming()  # Stop streaming to prevent any more frames from coming in after we remove the callback
                except Exception:
                    self._log.exception("camera.stop_streaming failed at end of acquisition")
                try:
                    self.camera.remove_frame_callback(this_image_callback_id)
                except Exception:
                    self._log.exception("camera.remove_frame_callback failed at end of acquisition")

            self._finish_jobs()

            # Send Slack acquisition finished notification via callback (ensures ordering with timepoint notifications)
            if self._slack_notifier is not None:
                try:
                    total_duration = time.time() - self.timestamp_acquisition_started
                    stats = AcquisitionStats(
                        total_images=self.image_count,
                        total_timepoints=self.time_point,
                        total_duration_seconds=total_duration,
                        errors_encountered=self._acquisition_error_count,
                        experiment_id=self.experiment_ID or "unknown",
                    )
                    self.callbacks.signal_slack_acquisition_finished(stats)
                except Exception as e:
                    self._log.warning(f"Failed to send Slack acquisition finished notification: {e}")

            self.callbacks.signal_acquisition_finished()

    def _wait_for_outstanding_callback_images(self):
        # If there are outstanding frames, wait for them to come in.
        self._log.info("Waiting for any outstanding frames.")
        if not self._ready_for_next_trigger.wait(self._frame_wait_timeout_s()):
            self._log.warning("Timed out waiting for the last outstanding frames at end of acquisition!")

        if not self._image_callback_idle.wait(self._frame_wait_timeout_s()):
            self._log.warning("Timed out waiting for the last image to process!")

        # No matter what, set the flags so things can continue
        self._ready_for_next_trigger.set()
        self._image_callback_idle.set()

    def _finish_jobs(self, timeout_s=10):
        # Drain and summarize all currently available job results before waiting for completion
        self._summarize_runner_outputs(drain_all=True)

        active_runners = [
            (job_class, job_runner) for job_class, job_runner in self._job_runners if job_runner is not None
        ]

        self._log.info(f"Waiting for jobs to finish on {len(active_runners)} job runners before shutting them down...")
        timeout_time = time.time() + timeout_s

        def timed_out():
            return time.time() > timeout_time

        # Wait for all pending jobs across all runners (round-robin to avoid blocking on one)
        while not timed_out():
            any_pending = False
            for job_class, job_runner in active_runners:
                if job_runner.has_pending():
                    any_pending = True
                    break
            if not any_pending:
                break
            # Process any available results while waiting
            self._summarize_runner_outputs(drain_all=True)
            time.sleep(0.1)
        else:
            # Drain budget exhausted, but DO NOT kill the subprocess here. A
            # still-pending job may be a FlushAndStageUploadJob whose
            # ``wait_for_pending()`` is midway through committing a large shard,
            # or a SaveZarrJob whose level-0 write has not yet been flushed.
            # Killing the process would corrupt that FOV (the previous partial
            # shard would be left in place). The graceful shutdown below sends
            # the stop sentinel, which the run loop only honors *between* jobs,
            # so the in-flight job finishes and every writer is finalized before
            # the subprocess exits.
            for job_class, job_runner in active_runners:
                if job_runner.has_pending():
                    self._log.warning(
                        f"Jobs still pending for {job_class.__name__} after {timeout_s} [s]; "
                        f"they will finish and finalize during shutdown "
                        f"(up to {JOB_RUNNER_FINALIZE_TIMEOUT_S} [s])."
                    )

        # Drain results before shutdown
        self._summarize_runner_outputs(drain_all=True)

        # Shut down all job runners in parallel (in background to avoid blocking on subprocess termination).
        # Using daemon threads is safe here because:
        # 1. The subprocess drains any remaining queued jobs and runs
        #    finalize_all_writers() (the data-durability flush) before exiting;
        #    shutdown() waits JOB_RUNNER_FINALIZE_TIMEOUT_S for that to finish.
        # 2. Beyond that flush, subprocess termination is best-effort cleanup only
        # 3. If app exits before threads complete, OS will terminate subprocesses anyway
        # 4. Running in the background prevents the (possibly slow) flush + termination
        #    from blocking acquisition completion
        log = self._log  # Capture for closure

        def shutdown_runner(job_runner, timeout, close_output_queue=True):
            try:
                job_runner.shutdown(timeout, close_output_queue=close_output_queue)
            except Exception as e:
                log.error(f"Error shutting down job runner in background: {e}")

        self._log.info("Shutting down job runners (non-blocking)...")
        # Give the background shutdown the full finalize budget — NOT the
        # leftover of the short drain timeout above. ``shutdown()`` sends the
        # stop sentinel and then ``join()``s for this long before resorting to
        # terminate(), which is exactly the window the subprocess needs to run
        # its remaining queued jobs plus ``finalize_all_writers()``. Because
        # this runs in a daemon thread, the long budget does not block the
        # controller or the next acquisition.
        #
        # Upload-enabled runners keep their output queues OPEN
        # (close_output_queue=False): BarrierResults keep arriving on them for
        # the whole background shutdown, and the upload drainer — not this
        # method's final drain — is what consumes those. The drainer owns
        # closing the queues when it finishes.
        upload_runner_queues = []
        runners_exited_event = threading.Event()
        shutdown_threads = []
        for job_class, job_runner in active_runners:
            keep_queue_open = job_runner.has_upload_pipeline()
            if keep_queue_open:
                upload_runner_queues.append(job_runner.output_queue())
            t = threading.Thread(
                target=shutdown_runner,
                args=(job_runner, JOB_RUNNER_FINALIZE_TIMEOUT_S),
                kwargs={"close_output_queue": not keep_queue_open},
                daemon=True,
            )
            t.start()
            shutdown_threads.append(t)

        # Fire signal_data_writing_complete once every runner has finished
        # finalizing (writers flushed, zarr.json marked complete, subprocess
        # exited) — the point at which the local dataset is safe to move/copy.
        # Runs in its own daemon thread so the controller is not blocked; the
        # callback always fires (the joined shutdowns each have their own
        # internal timeout+terminate) so the GUI can never get stuck in the
        # "Finalizing..." state.
        signal_writing_complete = self.callbacks.signal_data_writing_complete

        def _announce_writeback_complete(threads):
            for th in threads:
                th.join()
            # The upload drainer waits on this before its "post-finalize"
            # metadata resync — only now is every zarr.json actually final.
            runners_exited_event.set()
            try:
                signal_writing_complete()
            except Exception as e:
                log.error(f"signal_data_writing_complete callback failed: {e}")
            else:
                log.info("Data writing complete (all zarr writers finalized).")

        threading.Thread(
            target=_announce_writeback_complete, args=(shutdown_threads,), daemon=True
        ).start()

        # Final drain of all output queues (should be empty, but check anyway)
        self._summarize_runner_outputs(drain_all=True)

        # Hand the UploadWorker drain off to a background daemon thread.
        # The controller is free to start the next acquisition immediately;
        # the previous run's uploads finish independently. Multiple drainers
        # can run concurrently — see ``active_upload_drainer_count()`` for
        # operator visibility.
        # Stall window, not a deadline: the drainer keeps running as long as
        # the worker makes byte-progress (heartbeat advances); it only gives up
        # after this many seconds of a genuinely wedged worker.
        self._spawn_background_upload_drainer(
            stall_window_s=UPLOAD_DRAINER_STALL_WINDOW_S,
            runner_output_queues=upload_runner_queues,
            runners_done_event=runners_exited_event,
        )

        # Release backpressure resources now that all jobs are complete
        try:
            self._backpressure.close()
        except Exception as e:
            self._log.error(f"Error closing backpressure controller: {e}")

    def _spawn_background_upload_drainer(
        self,
        stall_window_s: float,
        runner_output_queues: Optional[List] = None,
        runners_done_event: Optional[threading.Event] = None,
    ) -> None:
        """Detach the UploadWorker drain to a background thread.

        The thread takes over ownership of:
          - the UploadWorker subprocess,
          - the per-timepoint pending/result/expected tracking dicts,
          - the local-deletion application and shard-dir pruning,
          - the post-finalize metadata resync,
          - the eventual ``shutdown()``+``join()``+``release_queue_resources()``
            of the worker.

        After this returns, ``MultiPointWorker`` may exit and a new
        acquisition may start — each acquisition gets its own UploadWorker
        and previously-spawned drainer threads continue independently.
        """
        if self._upload_worker is None:
            return

        # Capture every piece of state the drainer needs into a fresh
        # _BackgroundUploadDrainer so the daemon thread does not depend on
        # MultiPointWorker still being alive.
        drainer = _BackgroundUploadDrainer(
            upload_worker=self._upload_worker,
            upload_target=self._upload_target,
            tasks_by_tp=self._upload_tasks_by_tp,
            results_by_tp=self._upload_results_by_tp,
            expected_by_tp=self._upload_expected_count_by_tp,
            deletion_done=self._upload_deletion_done,
            failed_tasks=self._upload_failed_tasks,
            completed_task_ids=self._upload_completed_task_ids,
            results_received=self._upload_results_received,
            zarr_writer_info=self._zarr_writer_info,
            experiment_path=self.experiment_path,
            runner_output_queues=runner_output_queues,
            runners_done_event=runners_done_event,
            stall_window_s=stall_window_s,
            failed_deletions=self._upload_failed_deletions,
        )
        register_active_upload_drainer(drainer)
        # The drainer's force_stop path now covers the worker; drop it from
        # the pre-drainer orphan registry.
        unregister_live_upload_worker(self._upload_worker)
        drainer.start()
        active = active_upload_drainer_count()
        self._log.info(
            f"UploadWorker drain handed off to background thread "
            f"(experiment={self.experiment_path}). "
            f"{active} background drainer(s) now active; "
            f"acquisition controller is free to start the next run."
        )

        # Detach the upload state from the MultiPointWorker so nothing
        # else can accidentally mutate or shutdown the now-owned worker.
        self._upload_worker = None
        self._upload_target = None
        self._upload_tasks_by_tp = {}
        self._upload_results_by_tp = {}
        self._upload_expected_count_by_tp = {}
        self._upload_deletion_done = set()
        self._upload_failed_tasks = []
        self._upload_completed_task_ids = set()
        self._upload_results_received = 0

    def wait_till_operation_is_completed(self):
        self.microcontroller.wait_till_operation_is_completed()

    def run_single_time_point(self):
        try:
            start = time.time()
            self._timepoint_start_time = start
            self._timepoint_image_count = 0
            self._timepoint_fov_count = 0
            self._laser_af_successes = 0
            self._laser_af_failures = 0
            self.microcontroller.enable_joystick(False)

            self._log.info("multipoint acquisition - time point " + str(self.time_point + 1))

            # Notify listeners (napari views flush their per-timepoint caches on
            # this signal so peak RAM tracks a single timepoint, not the whole run).
            self.callbacks.signal_new_time_point(self.time_point)

            # For each time point, create a per-timepoint folder *only if* something
            # actually lands in it. ZARR_V3 streams images to its own per-FOV trees and
            # consolidates the per-frame timing CSV at the experiment root, so the
            # timepoint folder is otherwise empty in the common case. We still create
            # it when downsampled views or laser-AF characterization debug images need it.
            with self._timing.get_timer("create_new_timepoint"):
                if self.experiment_path:
                    utils.ensure_directory_exists(str(self.experiment_path))
                if self._needs_per_timepoint_folder():
                    current_path = os.path.join(self.experiment_path, f"{self.time_point:0{FILE_ID_PADDING}}")
                    utils.ensure_directory_exists(str(current_path))
                    self._wrote_per_timepoint_folder = True
                else:
                    current_path = self.experiment_path
                    self._wrote_per_timepoint_folder = False

                # Write acquisition metadata sidecar for individual TIFF saving modes.
                # This makes per-timepoint folders self-describing without parsing filenames.
                if (
                    not self.skip_saving
                    and self.file_saving_option in (FileSavingOption.INDIVIDUAL_IMAGES, FileSavingOption.MULTI_PAGE_TIFF)
                ):
                    metadata_path = os.path.join(current_path, "metadata.json")
                    if not os.path.exists(metadata_path):
                        sidecar = {
                            "channel_names": list(self.observation_state_names),
                            "num_time_points": self.Nt,
                            "num_z_levels": self.NZ,
                            "num_channels": len(self.observation_state_names),
                            "pixel_size_um": self._pixel_size_um,
                            "z_step_um": self._physical_size_z_um,
                            "time_increment_s": self._time_increment_s,
                            "file_saving_option": self.file_saving_option.value,
                        }
                        try:
                            with open(metadata_path, "w") as f:
                                json.dump(sidecar, f, indent=2)
                        except OSError as e:
                            self._log.warning(f"Failed to write metadata sidecar: {e}")
            # create a dataframe to save coordinates
            with self._timing.get_timer("initialize_coordinates_dataframe"):
                self.initialize_coordinates_dataframe()

            # init z parameters, z range
            with self._timing.get_timer("initialize_z_stack"):
                if self.NZ > 1:
                    self.initialize_z_stack()

            with self._timing.get_timer("run_coordinate_acquisition"):
                self.run_coordinate_acquisition(current_path)

            # Save plate view for this timepoint
            with self._timing.get_timer("save_plate_view"):
                if self._generate_downsampled_views and self._downsampled_view_manager is not None:
                    # Wait for pending downsampled view jobs to complete
                    self._wait_for_downsampled_view_jobs()
                    # Save plate view
                    plate_resolution = int(self._downsampled_plate_resolution_um)
                    plate_view_path = os.path.join(current_path, "downsampled", f"plate_{plate_resolution}um.tiff")
                    self.save_plate_view(plate_view_path)
                    self._log.info(f"Saved plate view for timepoint {self.time_point} to {plate_view_path}")
                    # Clear plate view for next timepoint
                    self._downsampled_view_manager.clear()

            # finished region scan. Skip the per-timepoint coordinates.csv for ZARR_V3,
            # since the controller already wrote {exp}/coordinates.csv with the same data.
            if self.file_saving_option != FileSavingOption.ZARR_V3:
                with self._timing.get_timer("save_coordinates_csv"):
                    self.coordinates_pd.to_csv(
                        os.path.join(current_path, "coordinates.csv"), index=False, header=True
                    )

            # Send Slack timepoint notification via callback (allows main thread to capture screenshot)
            if self._slack_notifier is not None:
                try:
                    elapsed = time.time() - self.timestamp_acquisition_started
                    timepoint_duration = time.time() - self._timepoint_start_time
                    self._slack_notifier.record_timepoint_duration(timepoint_duration)
                    estimated_remaining = self._slack_notifier.estimate_remaining_time(self.time_point + 1, self.Nt)
                    stats = TimepointStats(
                        timepoint=self.time_point + 1,
                        total_timepoints=self.Nt,
                        elapsed_seconds=elapsed,
                        estimated_remaining_seconds=estimated_remaining,
                        images_captured=self._timepoint_image_count,
                        fovs_captured=self._timepoint_fov_count,
                        laser_af_successes=self._laser_af_successes,
                        laser_af_failures=self._laser_af_failures,
                        laser_af_failure_reasons=[],
                    )
                    # Use callback to allow main thread to capture screenshot before sending
                    self.callbacks.signal_slack_timepoint_notification(stats)
                except Exception as e:
                    self._log.warning(f"Failed to send Slack timepoint notification: {e}")

            # Per-timepoint .done marker only when we actually have a per-timepoint folder.
            # Acquisition-level completion is marked separately at experiment root by the
            # controller in _restore_state_after_acquisition.
            if self._wrote_per_timepoint_folder:
                utils.create_done_file(current_path)
            self._log.debug(f"Single time point took: {time.time() - start} [s]")
        finally:
            # Backstop: drive every illuminator OFF before the inter-timepoint wait.
            # Patterned-pulse stimulus channels don't go through the regular
            # set_channel_state path, so _hardware_asserted never tracks them and
            # turn_off_all_hardware_preserving_state skips them. That leaves the
            # NIDAQ DO line in its post-pulse state, which can be HIGH when the
            # last sample of the waveform happens to be HIGH (e.g. comb extending
            # to the end of the window). Calling the device-level turn_off_all
            # forces every channel OFF regardless of the cache.
            try:
                self.microscope.illumination_controller.turn_off_all()
            except Exception:
                self._log.exception("end-of-timepoint illumination backstop failed")
            self.microcontroller.enable_joystick(True)

    def _needs_per_timepoint_folder(self) -> bool:
        """True when something will write into ``{exp}/{timepoint}/``.

        ZARR_V3 alone does not — its image data lives under ``plate.ome.zarr``
        / ``zarr/`` and the per-frame CSV is consolidated at the experiment root.
        We keep the folder when:

        * skip_saving is off and we're using a TIFF mode (images land here)
        * downsampled views are enabled (``plate_<r>um.tiff`` lands here per timepoint)
        * laser-AF characterization mode is on (debug bmps land here)
        """
        if self.skip_saving:
            tiff_mode_writes = False
        else:
            tiff_mode_writes = self.file_saving_option != FileSavingOption.ZARR_V3
        if tiff_mode_writes:
            return True
        if self._generate_downsampled_views:
            return True
        if (
            self.laser_auto_focus_controller is not None
            and getattr(self.laser_auto_focus_controller, "characterization_mode", False)
        ):
            return True
        return False

    def initialize_z_stack(self):
        # z stacking config
        if self.z_stacking_config == "FROM TOP":
            self.deltaZ = -abs(self.deltaZ)
            self.move_to_z_level(self.z_range[1])
        else:
            self.move_to_z_level(self.z_range[0])

        self.z_pos = self.stage.get_pos().z_mm  # zpos at the beginning of the scan

    def initialize_coordinates_dataframe(self):
        self._coordinate_rows: list[dict] = []

    def update_coordinates_dataframe(self, region_id, z_level, pos: squid.abc.Pos, fov=None):
        row = {
            "region": region_id,
            "fov": fov,
            "z_level": z_level,
            "x (mm)": pos.x_mm,
            "y (mm)": pos.y_mm,
            "z (um)": pos.z_mm * 1000,
            "time": datetime.now().strftime("%Y-%m-%d_%H-%M-%S.%f"),
        }
        if self.use_piezo:
            row["z_piezo (um)"] = self.z_piezo_um
        self._coordinate_rows.append(row)

    @property
    def coordinates_pd(self) -> pd.DataFrame:
        return pd.DataFrame(self._coordinate_rows)

    def move_to_coordinate(self, coordinate_mm, region_id, fov):
        curr_pos = self.stage.get_pos()
        x_mm = coordinate_mm[0]
        y_mm = coordinate_mm[1]
        delta_x = abs(curr_pos.x_mm-x_mm)
        delta_y = abs(curr_pos.y_mm-y_mm)

        if self._alignment_widget is not None and self._alignment_widget.has_offset:
            x_mm, y_mm = self._alignment_widget.apply_offset(x_mm, y_mm)
            self._log.debug(
                f"moving to coordinate ({x_mm:.4f}, {y_mm:.4f}) "
                f"[original: ({coordinate_mm[0]:.4f}, {coordinate_mm[1]:.4f}), offset applied]"
            )
        else:
            self._log.debug(f"moving to coordinate {coordinate_mm}")

        # Pick the Z source. On subsequent timepoints with AF enabled, prefer
        # the focused Z cached from the previous timepoint over the
        # coordinate's nominal Z. The X/Y move below must still run — only
        # the Z source changes here.
        if (self.do_reflection_af or self.do_autofocus) and self.time_point > 0:
            if (region_id, fov) in self._z_pos_proposal:
                last_z_mm = self._z_pos_proposal[(region_id, fov)]
                self.move_to_z_level(last_z_mm, blocking=False)
                self._log.debug(f"Moved to last z position {last_z_mm} [mm]")
            else:
                self._log.warning(f"No last z position found for region {region_id}, fov {fov}")
        elif len(coordinate_mm) == 3:
            z_mm = coordinate_mm[2]
            self.move_to_z_level(z_mm, blocking=False)

        # Blocking-longer-axis move. Shorter axis fires non-blocking; the
        # longer axis blocks until motion is complete. Stabilization sleep
        # runs inline (the rig's MCU serializes motor + shutter commands, so
        # there is no benefit to deferring the sleep — see the
        # _pending_move_settle scaffolding in __init__ for the future async
        # path that _wait_for_move_settled will unlock).
        if delta_x > delta_y:
            self.stage.move_y_to(y_mm, blocking=False)
            self.stage.move_x_to(x_mm)
            self._sleep(SCAN_STABILIZATION_TIME_MS_X / 1000)
        else:
            self.stage.move_x_to(x_mm, blocking=False)
            self.stage.move_y_to(y_mm)
            self._sleep(SCAN_STABILIZATION_TIME_MS_Y / 1000)

    def _wait_for_move_settled(self, timeout_s: float = 30.0) -> None:
        """Join the in-flight stage motion issued by move_to_coordinate and
        run the post-motion stabilization sleep. Idempotent — no-op if no
        pending move. Called by any op that requires the stage physically
        settled (perform_autofocus, acquire_camera_image right before the
        camera trigger).
        """
        if not self._pending_move_settle:
            return
        with self._timing.get_timer("wait_for_move_settled"):
            try:
                self.stage.wait_for_idle(timeout_s)
            except Exception:
                self._log.exception("Timed out waiting for stage to settle")
            if self._pending_move_stabilization_s > 0:
                self._sleep(self._pending_move_stabilization_s)
        self._pending_move_settle = False
        self._pending_move_stabilization_s = 0.0



    def move_to_z_level(self, z_mm, blocking=True):
        self._log.debug("moving z")
        self.stage.move_z_to(z_mm, blocking=blocking)
        self._sleep(SCAN_STABILIZATION_TIME_MS_Z / 1000)

    def _summarize_runner_outputs(self, drain_all: bool = False) -> SummarizeResult:
        """Process job results from output queues.

        Args:
            drain_all: If True, process ALL available results. If False, process at most one per queue.

        Returns:
            SummarizeResult with none_failed and had_results.
        """
        none_failed = True
        had_results = False
        for job_class, job_runner in self._job_runners:
            if job_runner is None:
                continue
            out_queue = job_runner.output_queue()
            if out_queue is None:
                # Queue was cleared during shutdown
                continue
            while True:
                try:
                    job_result: JobResult = out_queue.get_nowait()
                    none_failed = none_failed and self._summarize_job_result(job_result)
                    had_results = True
                    if not drain_all:
                        break  # Only process one result per queue if not draining
                except queue.Empty:
                    break
                except ValueError:
                    # Queue was closed during shutdown - nothing more to drain
                    break

        # Drain the upload worker's output queue and reconcile against the
        # per-timepoint completion bookkeeping. Non-blocking — uploads that
        # are still in flight will be picked up on the next pass.
        self._drain_upload_results()

        return SummarizeResult(none_failed=none_failed, had_results=had_results)

    def _drain_upload_results(self) -> None:
        """Pull every available ``UploadResult`` from the UploadWorker.

        Each result corresponds to one ``(t, fov)`` bundle. When every bundle
        for a timepoint has reported (and ``delete_after_verify`` is on) we
        delete the local shard files for that timepoint in one batch.
        """
        if self._upload_worker is None:
            return
        try:
            results = drain_output_queue_nonblocking(self._upload_worker.output_queue)
        except (OSError, ValueError) as e:
            self._log.debug(f"Upload result queue read failed: {e}")
            return
        for result in results:
            tp = result.time_point
            self._upload_results_received += 1
            # Remember completion by task_id: if the matching BarrierResult
            # has not been consumed yet (independent queues, different drain
            # rates), _handle_barrier_result must NOT re-register this task —
            # nothing would ever discard it again and the end-of-run drain
            # would wait on a phantom.
            self._upload_completed_task_ids.add(result.task_id)
            self._upload_tasks_by_tp.get(tp, set()).discard(result.task_id)
            self._upload_results_by_tp.setdefault(tp, []).append(result)
            if not result.success:
                self._upload_failed_tasks.append(result)
                self._log.warning(
                    f"Upload task {result.task_id} for t={tp} fov={result.fov} "
                    f"failed: {result.error}"
                )
            self._maybe_batched_delete(tp)
        self._maybe_warn_upload_health()

    # Mid-run upload supervision (previously the heartbeat had no consumer
    # until the end-of-run drainer): throttled check that the worker process
    # is alive and not silently drowning. Log + Slack only — the acquisition
    # itself deliberately never blocks on upload state.
    _UPLOAD_HEALTH_CHECK_INTERVAL_S = 60.0
    _UPLOAD_HEALTH_WARN_INTERVAL_S = 300.0
    _UPLOAD_BACKLOG_WARN_TASKS = 100

    def _maybe_warn_upload_health(self) -> None:
        if self._upload_worker is None:
            return
        now = time.time()
        if now - self._upload_health_last_check < self._UPLOAD_HEALTH_CHECK_INTERVAL_S:
            return
        self._upload_health_last_check = now
        problems = []
        try:
            if not self._upload_worker.is_alive():
                problems.append("UploadWorker process is not alive")
        except Exception:
            return
        backlog = max(
            0, self._upload_worker.tasks_submitted - self._upload_results_received
        )
        if backlog >= self._UPLOAD_BACKLOG_WARN_TASKS:
            problems.append(
                f"upload backlog is {backlog} task(s) — the share is slower than "
                f"the acquisition writes; local disk keeps filling until it catches up"
            )
        hb = float(self._upload_worker.heartbeat or 0.0)
        if backlog and hb and now - hb > UPLOAD_DRAINER_STALL_WINDOW_S:
            problems.append(
                f"no upload progress for {int(now - hb)}s with {backlog} task(s) queued"
            )
        if self._upload_failed_tasks:
            problems.append(f"{len(self._upload_failed_tasks)} upload task(s) failed so far")
        if not problems:
            return
        if now - self._upload_health_last_warn < self._UPLOAD_HEALTH_WARN_INTERVAL_S:
            return
        self._upload_health_last_warn = now
        msg = "Streaming upload health: " + "; ".join(problems)
        self._log.warning(msg)
        if self._slack_notifier is not None:
            try:
                self._slack_notifier.notify_error(msg, {"experiment": self.experiment_ID})
            except Exception:
                pass

    def _maybe_batched_delete(self, time_point: int) -> None:
        """If every FOV in ``time_point`` has uploaded successfully, delete
        the local shard files for that timepoint in one batch.

        We require *all* expected FOVs to be present (so a partial timepoint
        with one failure does not silently drop the remainder), *and* every
        result to be ``success=True``. A single failure defers deletion for
        that timepoint until either a retry succeeds or the user intervenes.
        """
        if time_point in self._upload_deletion_done:
            return
        if self._upload_target is None or not self._upload_target.delete_after_verify:
            return
        expected = self._upload_expected_count_by_tp.get(time_point)
        if expected is None:
            return
        results = self._upload_results_by_tp.get(time_point, [])
        if len(results) < expected:
            return
        if any(not r.success for r in results):
            return
        # All bundles for this timepoint are verified on the remote. Delete
        # only the per-timepoint shard files that the upload worker tagged
        # as deletable — never the shared metadata (zarr.json / frame_times),
        # which the live writer is still using.
        deleted = 0
        for result in results:
            for local_path in result.deletable_uploaded_paths:
                try:
                    if os.path.isfile(local_path):
                        os.remove(local_path)
                        deleted += 1
                except OSError as e:
                    self._upload_failed_deletions += 1
                    self._log.warning(
                        f"Failed to delete {local_path} after verified upload: {e}"
                    )
        # Best-effort prune of the now-empty `c/<t>/...` shard subtrees so
        # the local directory listing stays tidy.
        self._prune_empty_shard_dirs(time_point)
        self._upload_deletion_done.add(time_point)
        self._log.info(
            f"Reclaimed local disk: deleted {deleted} files for verified timepoint t={time_point}"
        )

    def _prune_empty_shard_dirs(self, time_point: int) -> None:
        """Remove ``<level_dir>/c/<t>`` if empty after batched delete.

        Only descends into ``c/<t>`` and prunes upward to the level dir; never
        touches sibling timepoints' shard files or array metadata.
        """
        if self._zarr_writer_info is None:
            return
        for region_id, fov_count in self._zarr_writer_info.region_fov_counts.items():
            for fov in range(fov_count):
                group_dir = self._zarr_writer_info.get_group_path(region_id, fov)
                # Sweep across all level dirs (0/, 1/, ...) plus frame_times.
                if not os.path.isdir(group_dir):
                    continue
                for entry in os.listdir(group_dir):
                    candidate = os.path.join(group_dir, entry, "c", str(time_point))
                    if os.path.isdir(candidate):
                        # rmtree any empty subtree under it.
                        for root, dirs, files in os.walk(candidate, topdown=False):
                            if not files and not dirs:
                                try:
                                    os.rmdir(root)
                                except OSError:
                                    break

    def _summarize_job_result(self, job_result: JobResult) -> bool:
        """
        Prints a summary, then returns True if the result was successful or False otherwise.
        """
        if job_result.exception is not None:
            self._log.error(f"Error while running job {job_result.job_id}: {job_result.exception}")
            self._acquisition_error_count += 1

            # Send Slack error notification
            if self._slack_notifier is not None:
                try:
                    context = {"job_id": job_result.job_id}
                    self._slack_notifier.notify_error(
                        str(job_result.exception),
                        context,
                    )
                except Exception as e:
                    self._log.warning(f"Failed to send Slack error notification: {e}")
            return False
        else:
            # self._log.debug(f"Got result for job {job_result.job_id}, it completed!")
            # Handle DownsampledViewResult - update plate view
            if isinstance(job_result.result, DownsampledViewResult) and job_result.result.well_images:
                self._handle_downsampled_view_result(job_result.result)
            # Handle ZarrWriteResult - notify viewer that frame is written
            elif isinstance(job_result.result, ZarrWriteResult):
                r = job_result.result
                self.callbacks.signal_zarr_frame_written(r.fov, r.time_point, r.z_index, r.channel_name, r.region_idx)
            # Handle BarrierResult - the upload barrier has flushed this (t, fov)
            # and enqueued the upload task; track its task_id so we can match
            # the matching UploadResult later.
            elif isinstance(job_result.result, BarrierResult):
                self._handle_barrier_result(job_result.result)
            # Handle PostprocessResult - derived plates were written; feed each
            # embedded upload barrier through the same accounting as raw plates
            # and push the output previews to the live display.
            elif isinstance(job_result.result, PostprocessResult):
                pr = job_result.result
                if pr.error is not None:
                    self._log.error(
                        "Postprocess group %s failed at t=%d region=%s fov=%d: %s",
                        pr.group_key, pr.time_point, pr.region_id, pr.fov, pr.error,
                    )
                    self._acquisition_error_count += 1
                    if self._slack_notifier is not None:
                        try:
                            self._slack_notifier.notify_error(
                                f"Postprocess {pr.group_key} failed: {pr.error}",
                                {"time_point": pr.time_point, "region_id": pr.region_id, "fov": pr.fov},
                            )
                        except Exception as e:
                            self._log.warning(f"Failed to send Slack error notification: {e}")
                for br in pr.barrier_results:
                    self._handle_barrier_result(br)
                self._emit_postprocess_display(pr)
                return pr.error is None
            # Pre-acquisition routine warmup finished (non-fatal if it failed).
            elif isinstance(job_result.result, PostprocessWarmupResult):
                wr = job_result.result
                if wr.ok:
                    self._log.info(f"Postprocess routine warmup complete: {wr.label}")
                else:
                    self._log.warning(f"Postprocess routine warmup failed for {wr.label}: {wr.error}")
            return True

    def _handle_barrier_result(self, br: "BarrierResult") -> None:
        """Track an upload barrier: register its task_id (submitted) or account it
        as completed-non-uploading (no writer/shards) so the timepoint tally can
        still close. Shared by the raw-plate and derived-plate (postprocess) paths."""
        if br.submitted:
            if br.task_id in self._upload_completed_task_ids:
                # Its UploadResult already arrived and was consumed —
                # registering now would create a task_id nothing discards.
                return
            self._upload_tasks_by_tp.setdefault(br.time_point, set()).add(br.task_id)
        elif self._upload_target is not None:
            self._upload_results_by_tp.setdefault(br.time_point, []).append(
                UploadResult(
                    task_id=br.task_id,
                    time_point=br.time_point,
                    region_id=br.region_id,
                    fov=br.fov,
                    success=True,
                    uploaded_paths=[],
                    failed_paths=[],
                    error=None,
                )
            )
            self._maybe_batched_delete(br.time_point)

    def _emit_postprocess_display(self, pr: "PostprocessResult") -> None:
        """Push each derived output preview to the live image display.

        Synthesizes a CameraFrame + CaptureInfo (labelled with the output key) so
        the derived image flows through the same signal_new_image path as raw
        frames. Best-effort — display must never break the acquisition.
        """
        src = pr.source_capture_info
        if src is None or not pr.display_images:
            return
        import dataclasses
        import squid.abc
        from squid.config import CameraPixelFormat

        for out_key, image in pr.display_images.items():
            # The live display shares one image size + integer dtype across all
            # channels; a preview that doesn't match the current raw-frame size
            # would thrash the viewer (re-init every frame). Skip mismatches —
            # the output is still saved to its plate.
            if self._last_raw_display_shape is not None and image.shape[:2] != self._last_raw_display_shape:
                self._log.debug(
                    "Skipping live display of %s: shape %s != live frame %s",
                    out_key, image.shape[:2], self._last_raw_display_shape,
                )
                continue
            try:
                os_copy = src.observation_state.model_copy(update={"name": out_key})
                info = dataclasses.replace(
                    src,
                    observation_state=os_copy,
                    filename_channel_label=out_key,
                    postprocess_group=None,
                    array_key=None,
                    save_t_index=None,
                    save_c_index=None,
                    save_t_size=None,
                    save_c_size=None,
                    save_z_size=None,
                )
                frame = squid.abc.CameraFrame(
                    frame_id=0,
                    timestamp=src.capture_time,
                    frame=image,
                    frame_format=squid.abc.CameraFrameFormat.RAW,
                    frame_pixel_format=CameraPixelFormat.MONO16,
                )
                self.callbacks.signal_new_image(frame, info)
            except Exception as e:
                self._log.debug(f"Could not display postprocess output {out_key}: {e}")

    def _handle_downsampled_view_result(self, result: DownsampledViewResult) -> None:
        """Update plate view with completed well image."""
        t_start = time.perf_counter()

        if self._downsampled_view_manager is None:
            return
        try:
            self._downsampled_view_manager.update_well(
                result.well_row,
                result.well_col,
                result.well_images,
            )
            t_update = time.perf_counter()

            self._log.debug(
                f"Updated plate view for well {result.well_id} at ({result.well_row}, {result.well_col}) "
                f"with {len(result.well_images)} channels"
            )

            # Emit plate view update for each channel
            for ch_idx, plate_image in enumerate(self._downsampled_view_manager.plate_view):
                channel_name = (
                    self._downsampled_view_manager.channel_names[ch_idx]
                    if ch_idx < len(self._downsampled_view_manager.channel_names)
                    else f"Channel_{ch_idx}"
                )
                self.callbacks.signal_plate_view_update(
                    PlateViewUpdate(
                        channel_idx=ch_idx,
                        channel_name=channel_name,
                        plate_image=plate_image.copy(),
                    )
                )

            t_signal = time.perf_counter()
            self._log.debug(
                f"[PERF] _handle_downsampled_view_result {result.well_id}: "
                f"update_well={t_update - t_start:.3f}s, signals={t_signal - t_update:.3f}s, "
                f"TOTAL={t_signal - t_start:.3f}s"
            )
        except Exception as e:
            self._log.exception(
                f"Failed to update plate view for well {result.well_id} "
                f"at ({result.well_row}, {result.well_col}): {e}"
            )

    def _create_job(self, job_class: Type[Job], info: CaptureInfo, image: np.ndarray) -> Optional[Job]:
        """Create a job instance for the given job class.

        Returns None if the job should be skipped for this frame. Postprocessed
        frames (``info.postprocess_group`` set) go ONLY to the PostprocessJob
        runner — their raw image is never saved and must not feed the save jobs
        or the downsampled-view accumulators. Non-postprocessed frames skip the
        PostprocessJob runner.
        """
        is_postprocessed = info.postprocess_group is not None
        if job_class is PostprocessJob:
            return self._create_postprocess_job(info, image) if is_postprocessed else None
        if is_postprocessed:
            return None  # raw frame not saved / not downsampled
        if job_class == DownsampledViewJob:
            return self._create_downsampled_view_job(info, image)
        return job_class(capture_info=info, capture_image=JobImage(image_array=image))

    def _create_postprocess_job(self, info: CaptureInfo, image: np.ndarray) -> Optional[PostprocessJob]:
        """Build a PostprocessJob for one accumulated frame of a postprocess group."""
        plan = self._get_region_plan(info.region_id)
        group = plan.postprocess_groups.get(info.postprocess_group)
        if group is None:
            self._log.error(
                "Postprocess frame for unknown group %r in region %s; skipping.",
                info.postprocess_group,
                info.region_id,
            )
            return None
        expected = 0
        for s in group.input_states.values():
            expected += s.frames_per_visit * (self.NZ if s.acquire_z_stack else 1)
        ctx_meta = self._postprocess_ctx_meta(group)
        output_specs = [
            {
                "name": o.name,
                "z_size": o.z_size,
                "dtype": o.dtype,
                "channel_color": o.channel_color,
                "wavelength_nm": o.wavelength_nm,
            }
            for o in group.outputs
        ]
        input_state_specs = {
            name: {"acquire_z_stack": s.acquire_z_stack, "frames_per_visit": s.frames_per_visit}
            for name, s in group.input_states.items()
        }
        return PostprocessJob(
            capture_info=info,
            capture_image=JobImage(image_array=image),
            group_key=info.postprocess_group,
            label=group.label,
            spec_dict=group.spec.model_dump(mode="json"),
            expected_frames=expected,
            output_specs=output_specs,
            input_state_specs=input_state_specs,
            ctx_meta=ctx_meta,
        )

    def _postprocess_frame_shape(self):
        """Expected camera frame (Y, X) for this run, or None if unknown.

        Known before the first capture (from the configured ROI + software crop),
        so routine warmup can precompute frame-shape-dependent state.
        """
        try:
            w, h = self.camera.get_crop_size()
            return (int(h), int(w))
        except Exception:
            return None

    def _postprocess_ctx_meta(self, group) -> dict:
        """Run geometry + per-group state metadata handed to a routine's context.
        Shared by the per-FOV job and the pre-acquisition warmup so their cache
        keys match (first FOV is then a cache hit)."""
        state_meta = {}
        for name in group.input_states:
            _color, wl = self._channel_display_meta(name)
            state_meta[name] = {"wavelength_nm": wl}
        return {
            "pixel_size_um": self._pixel_size_um,
            # self.deltaZ is the mechanical z step in MILLIMETRES (stage units);
            # convert to micrometres for the routine geometry.
            "dz_um": (self.deltaZ * 1000.0) if self.NZ > 1 else None,
            "nz": self.NZ,
            "nt": self.Nt,
            "state_meta": state_meta,
            "yx_shape": self._postprocess_frame_shape(),
        }

    def _prewarm_postprocess_routines(self) -> None:
        """Precompute FOV-shared routine state (e.g. transfer functions) BEFORE
        the first hardware trigger, so the first FOV's compute is a cache hit and
        never stalls saving/display or backpressures the run.

        Dispatches one PostprocessWarmupJob per distinct routine (deduped by
        routine identity) to the postprocess runner and waits for them (bounded).
        A warmup failure is non-fatal — the routine falls back to lazy compute.
        """
        if not self._has_postprocess:
            return
        runner = None
        for job_class, jr in self._job_runners:
            if job_class is PostprocessJob:
                runner = jr
                break
        # Distinct warmup jobs across the global + per-region plans, deduped by
        # routine identity so an identical routine+params is warmed only once.
        from control.core.job_processing import postprocess_routine_key

        seen = set()
        jobs = []
        for plan in [self._global_plan, *self._region_plans.values()]:
            if plan is None:
                continue
            for group in plan.postprocess_groups.values():
                spec_dict = group.spec.model_dump(mode="json")
                key = postprocess_routine_key(spec_dict)
                if key in seen:
                    continue
                seen.add(key)
                jobs.append(
                    PostprocessWarmupJob(
                        label=group.label,
                        spec_dict=spec_dict,
                        input_state_specs={
                            name: {"acquire_z_stack": s.acquire_z_stack, "frames_per_visit": s.frames_per_visit}
                            for name, s in group.input_states.items()
                        },
                        ctx_meta=self._postprocess_ctx_meta(group),
                    )
                )
        if not jobs:
            return
        self._log.info(f"Pre-computing {len(jobs)} postprocess routine(s) before acquisition...")
        if runner is None:
            # No multiprocessing runner (e.g. USE_MULTIPROCESSING False): run inline.
            for job in jobs:
                job.run()
            return
        # Make sure the subprocess is up before dispatching (bounded).
        runner.wait_ready(timeout_s=15.0)
        for job in jobs:
            runner.dispatch(job)
        # Wait for warmups to finish so the first FOV is a cache hit. Bounded so a
        # hung warmup can't wedge the run — on timeout we proceed (lazy compute).
        deadline = time.time() + 180.0
        while runner.has_pending() and time.time() < deadline:
            if self.abort_requested_fn():
                break
            self._summarize_runner_outputs()
            time.sleep(0.05)
        self._summarize_runner_outputs()
        if runner.has_pending():
            self._log.warning("Postprocess warmup did not finish within timeout; routines will compute lazily.")

    def _create_downsampled_view_job(self, info: CaptureInfo, image: np.ndarray) -> Optional[DownsampledViewJob]:
        """Create a DownsampledViewJob for the given capture.

        Returns None if downsampled views are disabled or not applicable.
        """
        if not self._generate_downsampled_views:
            return None

        # Calculate overlap first (needed for plate view manager initialization)
        if self._overlap_pixels is None:
            self._calculate_overlap_pixels(image)

        # Initialize plate view manager on first image (we need image dimensions)
        if self._downsampled_view_manager is None:
            self._initialize_downsampled_view_manager(image)

        # Get well info from region_id
        region_id = str(info.region_id)
        try:
            well_row, well_col = parse_well_id(region_id)
        except (ValueError, IndexError):
            # Region ID is not a valid well ID (e.g., "R0", "manual")
            # Region ID is not a valid well ID (e.g., "R0", "manual", custom names).
            # Use region index as a fallback. This is expected for non-plate acquisitions.
            self._log.debug(f"Region {region_id} is not a well ID, using fallback positioning")
            if not self._plate_num_rows or not self._plate_num_cols:
                self._log.warning(
                    f"Plate dimensions not set (rows={self._plate_num_rows}, cols={self._plate_num_cols}); "
                    "using (0, 0) for well position"
                )
                well_row, well_col = 0, 0
            else:
                region_idx = self.scan_region_names.index(region_id) if region_id in self.scan_region_names else 0
                well_row = region_idx // self._plate_num_cols
                well_col = region_idx % self._plate_num_cols
                # Warn if region index exceeds plate capacity (data will be overwritten)
                max_slots = self._plate_num_rows * self._plate_num_cols
                if region_idx >= max_slots:
                    self._log.warning(
                        f"Region index {region_idx} exceeds plate capacity ({max_slots} slots); "
                        f"well position will be clamped and may overwrite existing data"
                    )
                # Clamp to plate bounds
                well_row = min(well_row, self._plate_num_rows - 1)
                well_col = min(well_col, self._plate_num_cols - 1)

        # Get FOV position within well
        total_fovs = self._region_fov_counts.get(region_id, 1)
        fov_index = info.fov

        # Get the first FOV position for this region to calculate relative position
        region_coords = self.scan_region_fov_coords_mm.get(region_id, [])
        if region_coords and fov_index < len(region_coords):
            first_fov = region_coords[0]
            current_fov = region_coords[fov_index]
            # Relative position in mm from first FOV
            fov_position = (current_fov[0] - first_fov[0], current_fov[1] - first_fov[1])
        else:
            fov_position = (0.0, 0.0)

        # Determine output directory
        output_dir = os.path.join(self.experiment_path, str(self.time_point), "downsampled")

        # Get channel info
        channel_idx = info.configuration_idx
        total_channels = self._channel_step_count()
        channel_name = info.observation_state.name if info.observation_state else f"Channel_{channel_idx}"
        channel_names = list(self.observation_state_names)

        return DownsampledViewJob(
            capture_info=info,
            capture_image=JobImage(image_array=image),
            well_id=region_id,
            well_row=well_row,
            well_col=well_col,
            fov_index=fov_index,
            total_fovs_in_well=total_fovs,
            channel_idx=channel_idx,
            total_channels=total_channels,
            channel_name=channel_name,
            fov_position_in_well=fov_position,
            overlap_pixels=self._overlap_pixels,
            pixel_size_um=self._pixel_size_um or 1.0,
            target_resolutions_um=self._downsampled_well_resolutions_um,
            plate_resolution_um=self._downsampled_plate_resolution_um,
            output_dir=output_dir,
            channel_names=channel_names,
            z_index=info.z_index,
            total_z_levels=self.NZ,
            z_projection_mode=self._downsampled_z_projection,
            interpolation_method=self._downsampled_interpolation_method,
            skip_saving=self.skip_saving
            or not self._save_downsampled_well_images
            or control._def.SIMULATED_DISK_IO_ENABLED,
        )

    def _initialize_downsampled_view_manager(self, image: np.ndarray) -> None:
        """Initialize the plate view manager based on image dimensions and FOV grid."""
        height, width = image.shape[:2]
        pixel_size_um = self._pixel_size_um or 1.0

        # Calculate downsample factor (must match downsample_tile's rounding)
        downsample_factor = int(round(self._downsampled_plate_resolution_um / pixel_size_um))
        if downsample_factor < 1:
            downsample_factor = 1

        # Calculate cropped tile dimensions (after overlap removal)
        # This matches what stitch_tiles receives
        if self._overlap_pixels:
            top, bottom, left, right = self._overlap_pixels
            cropped_width = width - left - right
            cropped_height = height - top - bottom
        else:
            cropped_width = width
            cropped_height = height

        cropped_tile_width_mm = cropped_width * pixel_size_um / 1000.0
        cropped_tile_height_mm = cropped_height * pixel_size_um / 1000.0

        # Calculate expected stitched well size using same logic as stitch_tiles:
        # canvas_size = (max_coord - min_coord) + tile_size
        well_extent_x_mm = 0.0
        well_extent_y_mm = 0.0

        for region_id, coords in self.scan_region_fov_coords_mm.items():
            if len(coords) >= 1:
                # Find extent of FOV positions within this well
                x_coords = [c[0] for c in coords]
                y_coords = [c[1] for c in coords]
                # Match stitch_tiles logic: extent = (max - min) + cropped_tile_size
                extent_x = max(x_coords) - min(x_coords) + cropped_tile_width_mm
                extent_y = max(y_coords) - min(y_coords) + cropped_tile_height_mm
                well_extent_x_mm = max(well_extent_x_mm, extent_x)
                well_extent_y_mm = max(well_extent_y_mm, extent_y)

        # Convert to pixels at native resolution (matching stitch_tiles)
        well_width_pixels = int(round(well_extent_x_mm * 1000.0 / pixel_size_um))
        well_height_pixels = int(round(well_extent_y_mm * 1000.0 / pixel_size_um))

        # Apply downsampling to get final slot size (matching downsample_tile)
        well_slot_width = well_width_pixels // downsample_factor
        well_slot_height = well_height_pixels // downsample_factor

        # Ensure minimum size (single cropped FOV downsampled)
        min_slot_width = cropped_width // downsample_factor
        min_slot_height = cropped_height // downsample_factor
        well_slot_width = max(well_slot_width, min_slot_width)
        well_slot_height = max(well_slot_height, min_slot_height)

        # Get channel info
        num_channels = self._channel_step_count()
        channel_names = list(self.observation_state_names)

        self._downsampled_view_manager = DownsampledViewManager(
            num_rows=self._plate_num_rows,
            num_cols=self._plate_num_cols,
            well_slot_shape=(well_slot_height, well_slot_width),
            num_channels=num_channels,
            channel_names=channel_names,
            dtype=image.dtype,
        )
        self._log.info(
            f"Initialized downsampled view manager: {self._plate_num_rows}x{self._plate_num_cols} wells, "
            f"{num_channels} channels, slot shape ({well_slot_height}, {well_slot_width}), "
            f"well extent ({well_extent_x_mm:.2f}x{well_extent_y_mm:.2f} mm)"
        )

        # Calculate FOV grid shape for click coordinate mapping
        # Determine from the first region that has multiple FOVs
        fov_grid_shape = (1, 1)
        for region_id, coords in self.scan_region_fov_coords_mm.items():
            if len(coords) >= 1:
                x_positions = set(round(c[0], 4) for c in coords)
                y_positions = set(round(c[1], 4) for c in coords)
                fov_grid_shape = (len(y_positions), len(x_positions))
                break

        # Emit plate view init signal
        self.callbacks.signal_plate_view_init(
            PlateViewInit(
                num_rows=self._plate_num_rows,
                num_cols=self._plate_num_cols,
                well_slot_shape=(well_slot_height, well_slot_width),
                fov_grid_shape=fov_grid_shape,
                channel_names=channel_names,
            )
        )

    def _calculate_overlap_pixels(self, image: np.ndarray) -> None:
        """Calculate overlap pixels based on acquisition parameters."""
        height, width = image.shape[:2]
        pixel_size_um = self._pixel_size_um or 1.0

        # Find step size from FOV coordinates by grouping FOVs into rows
        dx_mm = 0.0
        dy_mm = 0.0

        try:
            for coords in self.scan_region_fov_coords_mm.values():
                if len(coords) < 2:
                    continue

                # Group FOVs by Y coordinate to find rows
                # Rounding to 4 decimal places (0.1 µm precision) assumes stage positioning
                # is accurate to within 0.1 µm, which is typical for microscope stages.
                rows: Dict[float, List[float]] = {}
                for coord in coords:
                    x, y = coord[0], coord[1]
                    y_key = round(y, 4)
                    if y_key not in rows:
                        rows[y_key] = []
                    rows[y_key].append(x)

                # Find X step from first row with 2+ FOVs
                for y_key in sorted(rows.keys()):
                    x_coords = rows[y_key]
                    if len(x_coords) >= 2:
                        x_sorted = sorted(x_coords)
                        dx_mm = x_sorted[1] - x_sorted[0]
                        break

                # Find Y step from two adjacent rows
                y_keys = sorted(rows.keys())
                if len(y_keys) >= 2:
                    dy_mm = y_keys[1] - y_keys[0]

                if dx_mm > 0 or dy_mm > 0:
                    break
        except Exception as e:
            self._log.warning(f"Could not calculate step size from coordinates: {e}")
            dx_mm = 0
            dy_mm = 0

        # If only one direction has steps, assume same step in both directions (square grid)
        if dx_mm > 0 and dy_mm == 0:
            dy_mm = dx_mm
        elif dy_mm > 0 and dx_mm == 0:
            dx_mm = dy_mm

        if dx_mm == 0 and dy_mm == 0:
            # No overlap or single FOV per well - don't crop anything
            self._overlap_pixels = (0, 0, 0, 0)
            self._log.info("Single FOV per well or cannot determine step size, no overlap cropping")
        else:
            self._overlap_pixels = calculate_overlap_pixels(width, height, dx_mm, dy_mm, pixel_size_um)
            self._log.info(f"Calculated overlap pixels: {self._overlap_pixels} (dx={dx_mm}mm, dy={dy_mm}mm)")

    def _wait_for_downsampled_view_jobs(self, timeout_s: Optional[float] = None) -> None:
        """Wait for all pending downsampled view jobs to complete and process results.

        Args:
            timeout_s: Maximum time to wait for jobs to complete. If None, uses
                      DOWNSAMPLED_VIEW_JOB_TIMEOUT_S from _def.py.
        """
        from control.core.job_processing import DownsampledViewJob

        if timeout_s is None:
            timeout_s = DOWNSAMPLED_VIEW_JOB_TIMEOUT_S
        timeout_time = time.time() + timeout_s
        timed_out = False

        for job_class, job_runner in self._job_runners:
            if job_runner is None or job_class != DownsampledViewJob:
                continue

            # Wait for input queue to empty
            while job_runner.has_pending():
                self._summarize_runner_outputs(drain_all=True)
                if time.time() > timeout_time:
                    self._log.warning(
                        f"Timeout ({timeout_s}s) waiting for downsampled view jobs - "
                        f"some wells may not appear in plate view"
                    )
                    timed_out = True
                    break
                time.sleep(0.1)

            if timed_out:
                break

            # After input queue is empty, the last job may still be running
            # Keep polling for results until we get no new results for a while
            last_result_time = time.time()
            while time.time() < timeout_time:
                result = self._summarize_runner_outputs(drain_all=True)
                if result.had_results:
                    last_result_time = time.time()
                # If no results for DOWNSAMPLED_VIEW_IDLE_TIMEOUT_S, assume all jobs are done
                if time.time() - last_result_time > DOWNSAMPLED_VIEW_IDLE_TIMEOUT_S:
                    break
                time.sleep(0.1)

            # Final drain of results
            self._summarize_runner_outputs(drain_all=True)

    def get_plate_view(self) -> Optional[np.ndarray]:
        """Get a copy of the current plate view array."""
        if self._downsampled_view_manager is None:
            return None
        return self._downsampled_view_manager.get_plate_view()

    def save_plate_view(self, path: str) -> None:
        """Save the plate view to disk."""
        if self._downsampled_view_manager is not None:
            self._downsampled_view_manager.save_plate_view(path)

    def _prewarm_observation_states(self) -> None:
        """Apply each distinct observation state preset once outside the
        FOV loop so the one-time mode-switch / init costs land in an
        ``init:prewarm_observation_states`` timer instead of polluting
        per-capture stats.

        Suppresses the observation-state controller's own sub-timers
        (``obs:cls:*`` / ``obs:preset:*``) during prewarm by temporarily
        detaching ``obs_controller._timing`` — those are no-ops when
        ``_timing`` is None (see ObservationStateController._time). After
        the main scan loop runs, those sub-timers' ``max`` values reflect
        real per-capture work, not the one-off init spike.
        """
        if not self.observation_state_names:
            return
        obs_controller = self.liveController.obs_controller
        repo = self.microscope.config_repo

        # Determine which presets are actually used by this acquisition.
        # (Skip presets that are referenced but inactive for every region.)
        active_states_union: set = set()
        for region_id in self.scan_region_fov_coords_mm:
            active_states_union |= set(self._get_observation_states_for_region(region_id))
        to_warm = [name for name in self.observation_state_names if name in active_states_union]
        if not to_warm:
            return

        with self._timing.get_timer("init:prewarm_observation_states"):
            saved_obs_timing = obs_controller._timing
            obs_controller._timing = None  # suppress sub-timer noise during prewarm
            try:
                seen: set = set()
                for preset_name in to_warm:
                    if preset_name in seen:
                        continue
                    seen.add(preset_name)
                    try:
                        state = repo.load_observation_preset(preset_name)
                        if state is None:
                            continue
                        # Cache so _apply_observation_state can skip the YAML
                        # load on every FOV (~2.3 ms/capture × 54 captures ≈ 120 ms saved).
                        self._observation_preset_cache[preset_name] = state
                        obs_controller.apply_observation_state_preset(
                            state,
                            emission_filter_wheel=self._emission_filter_wheel,
                            apply_camera_live_snapshot=False,
                        )
                    except Exception as exc:
                        # Prewarm is an optimization, not a correctness step —
                        # log and keep going; the actual apply in the FOV loop
                        # will surface any real issue.
                        self._log.warning(
                            "Prewarm of observation state %r failed (non-fatal): %s",
                            preset_name, exc,
                        )
            finally:
                obs_controller._timing = saved_obs_timing

    def run_coordinate_acquisition(self, current_path):
        # Reset backpressure counters at acquisition start
        # IMPORTANT: Must be before any camera triggers
        self._backpressure.reset()

        # Pre-warm every distinct observation state preset once BEFORE the
        # FOV loop. Absorbs the first-call one-off costs (most notably the
        # ~1 s GenICam SensorOperationMode write + Cap_Start that
        # set_camera_mode pays the first time it actually switches modes)
        # into a dedicated init timer instead of polluting the first FOV's
        # per-capture stats. Amortizes to ~zero over long runs.
        self._prewarm_observation_states()

        # Precompute FOV-shared postprocessing state (e.g. transfer functions)
        # before any hardware fires, so the first FOV's compute is a cache hit
        # and never stalls the first save/display.
        self._prewarm_postprocess_routines()

        n_regions = len(self.scan_region_coords_mm)

        for region_index, (region_id, coordinates) in enumerate(self.scan_region_fov_coords_mm.items()):
            self.callbacks.signal_overall_progress(
                OverallProgressUpdate(
                    current_region=region_index + 1,
                    total_regions=n_regions,
                    current_timepoint=self.time_point,
                    total_timepoints=self.Nt,
                )
            )
            self.num_fovs = len(coordinates)
            # Count imaged frames per position (cycles capture several frames per
            # state), not just distinct channels.
            frames_per_pos = self._get_region_plan(region_id).frames_per_position
            self.total_scans = self.num_fovs * self.NZ * frames_per_pos

            for fov, coordinate_mm in enumerate(coordinates):
                # Just so the job result queues don't get too big, check and print a summary of intermediate results here
                with self._timing.get_timer("job result summaries"):
                    result = self._summarize_runner_outputs()
                    if not result.none_failed and self._abort_on_failed_job:
                        self._log.error("Some jobs failed, aborting acquisition because abort_on_failed_job=True")
                        self.request_abort_fn()
                        return

                # Skip the inter-timepoint pre-move's destination; re-issuing it forces a
                # 0-distance wait that hits the 3 s floor in _calc_move_timeout, and that wait
                # races against the routine multi-second MCU read-thread lag at TP starts.
                if region_index == 0 and fov == 0 and self._first_fov_pre_moved:
                    self._first_fov_pre_moved = False
                else:
                    with self._timing.get_timer("move_to_coordinate"):
                        self.move_to_coordinate(coordinate_mm, region_id, fov)
                with self._timing.get_timer("acquire_at_position"):
                    self.acquire_at_position(region_id, current_path, fov)

                # Barrier: after every SaveZarrJob for this (t, region, fov)
                # has been dispatched, queue a FlushAndStageUploadJob behind
                # them. The job runs FIFO in the SaveZarrJob runner so by the
                # time its run() begins, every preceding zarr write has been
                # processed; it then waits on TensorStore pending futures and
                # hands the resulting shard paths to the UploadWorker.
                if (
                    self._upload_target is not None
                    and self._save_zarr_runner is not None
                    and self._zarr_writer_info is not None
                ):
                    try:
                        # Dense -> one array per FOV (array_key=None); ragged ->
                        # one single-channel plate per imaged state, so flush each.
                        region_plan = self._get_region_plan(region_id)
                        # Ragged plate keys carry the _refz suffix, so use array_keys
                        # (not channel_order, which is bare state names for the C axis).
                        array_keys = [None] if region_plan.dense else list(region_plan.array_keys)
                        for array_key in array_keys:
                            output_path = self._zarr_writer_info.get_output_path(
                                str(region_id), fov, array_key
                            )
                            barrier = FlushAndStageUploadJob(
                                time_point=self.time_point,
                                region_id=str(region_id),
                                fov=fov,
                                output_path=output_path,
                            )
                            self._save_zarr_runner.dispatch(barrier)
                    except Exception as e:
                        self._log.exception(
                            f"Failed to dispatch upload barrier for "
                            f"t={self.time_point} region={region_id} fov={fov}: {e}"
                        )

                if self.abort_requested_fn():
                    self.handle_acquisition_abort(current_path)
                    return

    def acquire_at_position(self, region_id, current_path, fov):
        # Autofocus once at the FOV's nominal plane to establish the focal
        # (reference) plane BEFORE the z-stack is positioned around it. The
        # stacking mode then decides whether that plane becomes the bottom,
        # center, or top slice (see prepare_z_stack). Also records the AF event
        # (target vs. corrected Z) to autofocus_log.csv.
        self._autofocus_and_record(region_id, fov, current_path)

        if self.NZ > 1:
            self.prepare_z_stack()

        if self.use_piezo:
            self.z_piezo_um = self.piezo.position

        # Z-plane index of the focus/reference plane (where reference-z-only steps
        # capture their single frame): the first acquired plane for From Bottom/Top,
        # the middle plane for From Center. See _reference_z_level.
        ref_z_level = self._reference_z_level()

        for z_level in range(self.NZ):
            file_ID = f"{region_id}_{fov:0{FILE_ID_PADDING}}_{z_level:0{FILE_ID_PADDING}}"

            acquire_pos = self.stage.get_pos()
            metadata = {"x": acquire_pos.x_mm, "y": acquire_pos.y_mm, "z": acquire_pos.z_mm}
            self._log.debug(f"Acquiring image: ID={file_ID}, Metadata={metadata}")

            # Iterate the resolved per-position plan (cycles). A flat selection is
            # just a 1-frame-per-state plan, so this single path serves both. The
            # plan's ordered events preserve interleave / chain order; imaged
            # events capture a frame, stimulus events fire an NIDAQ pulse comb.
            region_plan = self._get_region_plan(region_id)
            # Captured (not just saved) frames per (FOV, z): postprocessed events
            # are captured too, so include them so imaged_step stays aligned.
            frames_per_pos = region_plan.captured_frames_per_position
            if region_plan.events:
                imaged_step = 0  # per-z imaged-frame counter (for progress + AF guard)
                for event in region_plan.events:
                    if event.is_wait:
                        # Timed delay between events — no frame, no AF, no progress
                        # tick. Sleep in short slices so an abort interrupts it.
                        with self._timing.get_timer("cycle_wait"):
                            self._interruptible_sleep(event.wait_ms / 1000.0)
                        continue
                    # Reference-z-only step/sweep: capture a single frame at the
                    # focus/reference plane and skip it at every other z-level.
                    # Stimulus events are unaffected (they fire at every z as before).
                    if (not event.acquire_z_stack) and (not event.is_stimulus) and (z_level != ref_z_level):
                        continue
                    preset_name = event.observation_state
                    try:
                        with self._timing.get_timer("apply_observation_state"):
                            config = self._apply_observation_state(preset_name)
                    except Exception as e:
                        self._log.error("Failed to apply observation states %s: %s", preset_name, e, exc_info=True)
                        self.request_abort_fn()
                        return
                    # Source-coded FPM darkfield frame: override the LED matrix to
                    # this multiplexed pattern (base state supplies exposure/gain/
                    # color; the matrix channel must be ON in that base state — this
                    # is enforced pre-flight in validate_acquisition_settings). The
                    # override switches the device to 'mux' mode, so the capture
                    # path's re-fire lights exactly this LED set. A False return
                    # means no SciMicroscopy array is available, which would
                    # silently capture the wrong pattern — abort instead.
                    if event.multiplexed_leds is not None:
                        ok = False
                        try:
                            ok = self.microscope.illumination_controller.set_led_matrix_multiplexed_indices(
                                event.multiplexed_leds
                            )
                        except Exception as e:
                            self._log.error("FPM: applying multiplexed LED set failed: %s", e, exc_info=True)
                        if not ok:
                            self._log.error(
                                "FPM: could not light multiplexed darkfield pattern for base state %r "
                                "(no SciMicroscopy LED array / unified matrix unavailable). Aborting.",
                                preset_name,
                            )
                            self.request_abort_fn()
                            return
                    if self.NZ == 1:  # TODO: handle z offset for z stack
                        self.handle_z_offset(config, True)

                    # (Autofocus now runs once per FOV in _autofocus_and_record,
                    # before the z-stack is positioned — see acquire_at_position.)

                    if event.is_stimulus or config.is_stimulus_only:
                        with self._timing.get_timer("run_nidaq_stimulus"):
                            self._run_nidaq_stimulus(config)
                        if self.NZ == 1:
                            self.handle_z_offset(config, False)
                        continue  # no frame, no progress tick

                    # Postprocessed frames are routed to the PostprocessJob runner
                    # (raw not saved), so they carry no save layout — the group id
                    # tags the frame for accumulation. A ref-z-only postprocessed
                    # step still passes NZ frames? No: it is captured only at the
                    # reference plane like any ref-z step (skip handled above), so
                    # eff_z_index follows the same rule.
                    if event.postprocess is not None:
                        save_layout = None
                        config_idx = 0
                    else:
                        save_layout = self._build_save_layout(region_plan, event)
                        config_idx = save_layout.c_index
                    # A reference-z-only frame lives at z=0 of its Z=1 array; a
                    # normal frame at its stack level.
                    eff_z_index = z_level if event.acquire_z_stack else 0
                    with self._timing.get_timer("acquire_camera_image"):
                        with self._timing.get_timer("acquire_camera_image_inner"):
                            self.acquire_camera_image(
                                config,
                                file_ID,
                                current_path,
                                eff_z_index,
                                region_id=region_id,
                                fov=fov,
                                config_idx=config_idx,
                                filename_channel_label=preset_name,
                                save_layout=save_layout,
                                postprocess_group=event.postprocess_group,
                            )

                    if self.NZ == 1:
                        self.handle_z_offset(config, False)

                    current_image = fov * self.NZ * frames_per_pos + z_level * frames_per_pos + imaged_step + 1
                    imaged_step += 1
                    self.callbacks.signal_region_progress(
                        RegionProgressUpdate(current_fov=current_image, region_fovs=self.total_scans)
                    )
            else:
                raise ValueError("No observation states selected for acquisition.")

            # updates coordinates df
            self.update_coordinates_dataframe(region_id, z_level, acquire_pos, fov)
            self.callbacks.signal_current_fov(acquire_pos.x_mm, acquire_pos.y_mm)

            # check if the acquisition should be aborted
            if self.abort_requested_fn():
                self.handle_acquisition_abort(current_path)

            # update FOV counter
            self.af_fov_count = self.af_fov_count + 1

            if z_level < self.NZ - 1:
                self.move_z_for_stack()

        if self.NZ > 1:
            self.move_z_back_after_stack()

        # Increment FOV counter for Slack notification stats
        self._timepoint_fov_count += 1

    def _select_config(self, config):
        """Apply an ObservationState to hardware before capture."""
        self.callbacks.signal_current_configuration(config)
        self.liveController.obs_controller.apply_full_observation_state(config)
        self.wait_till_operation_is_completed()

    def _seed_fov_z_map(self):
        """Visit every (region, fov) and record its absolute Z via laser AF.

        Populates `self._fov_z_map`. Keys already present are skipped, so an
        aborted seed can be resumed by calling the method again. Runs before
        the first timepoint capture when `laser_af_seed_mode == "scan"`.
        """
        if self.laser_auto_focus_controller is None:
            self._log.warning("Laser AF seed-scan requested but laser_auto_focus_controller is None; skipping")
            return

        total = sum(len(coords) for coords in self.scan_region_fov_coords_mm.values())
        self._log.info(f"Laser-AF seed scan: {total} FOVs across {len(self.scan_region_fov_coords_mm)} region(s)")

        seeded = 0
        failed = 0
        for region_id, coords in self.scan_region_fov_coords_mm.items():
            # Each region's seed measurements must correct to that region's own
            # focus target, so load it before stepping through the region's FOVs.
            self._apply_region_laser_af_reference(region_id)
            for fov_idx, coord in enumerate(coords):
                if self.abort_requested_fn():
                    self._log.info("Abort requested during laser-AF seed scan")
                    return
                if (region_id, fov_idx) in self._fov_z_map:
                    continue  # resumable — already seeded

                x_mm, y_mm = coord[0], coord[1]
                if self._alignment_widget is not None and self._alignment_widget.has_offset:
                    x_mm, y_mm = self._alignment_widget.apply_offset(x_mm, y_mm)

                self.stage.move_x_to(x_mm)
                self._sleep(SCAN_STABILIZATION_TIME_MS_X / 1000)
                self.stage.move_y_to(y_mm)
                self._sleep(SCAN_STABILIZATION_TIME_MS_Y / 1000)

                try:
                    with self._timing.get_timer("af:seed_event"):
                        ok = self.laser_auto_focus_controller.move_to_target(0)
                    if ok:
                        self._fov_z_map[(region_id, fov_idx)] = self.stage.get_pos().z_mm
                        seeded += 1
                    else:
                        failed += 1
                        self._log.warning(f"Laser AF failed during seed at region={region_id} fov={fov_idx}")
                except Exception:
                    failed += 1
                    self._log.exception(f"Laser AF exception during seed at region={region_id} fov={fov_idx}")

        self._log.info(f"Laser-AF seed scan complete: seeded={seeded} failed={failed} total={total}")

        # Initialize the delta map + proposals from the freshly seeded absolute-Z
        # table. Use FOV 0 of each region as the provisional anchor — the first
        # runtime visit to that region will also refresh on FOV 0 (new-region-
        # entry rule), which will re-seat the anchor and recompute with the
        # runtime measurement. Pre-populating here gives us valid fallback
        # values if a refresh fails before any successful one in a region.
        for region_id in self.scan_region_fov_coords_mm:
            if (region_id, 0) in self._fov_z_map:
                self._recompute_region_proposals(region_id, anchor_fov=0)

    def _recompute_region_proposals(self, region_id: str, anchor_fov: int) -> None:
        """Refresh `_fov_z_delta_map` and `_z_pos_proposal` for every seeded
        FOV in `region_id`, using `anchor_fov` as the reference.

        Called after the seed scan (with `anchor_fov=0`) and after every
        successful laser-AF refresh (with `anchor_fov=<refreshed FOV>`). The
        delta map stays in sync with whichever FOV is the current anchor so
        the fallback path can trust `_fov_z_delta_map` directly.
        """
        anchor_key = (region_id, anchor_fov)
        if anchor_key not in self._fov_z_map:
            return
        anchor_seed_z = self._fov_z_map[anchor_key]
        # Before the first runtime refresh, fall back to the seed anchor z so
        # proposals are still populated (they'll be overwritten on first refresh).
        anchor_z_current = self._region_anchor_z_current.get(region_id, anchor_seed_z)
        for fov_idx in range(len(self.scan_region_fov_coords_mm.get(region_id, ()))):
            key = (region_id, fov_idx)
            if key not in self._fov_z_map:
                continue
            delta = self._fov_z_map[key] - anchor_seed_z
            self._fov_z_delta_map[key] = delta
            self._z_pos_proposal[key] = anchor_z_current + delta

    def _resolve_region_laser_af_reference(self, region_id):
        """Return the effective laser-AF reference for ``region_id``, or ``None``.

        - No per-region reference -> the global reference (the controller's
          reference snapshotted at worker construction, before this worker began
          switching references around).
        - A per-region reference WITH a crop -> used as-is.
        - A per-region reference carrying only a spot position (no crop, e.g. a
          spot-only CSV import) -> the region's x_reference but the global crop,
          so cross-correlation verification still has a valid template. This
          merge is done here (not in apply_reference) because the controller's
          currently-active crop is order-dependent and must not leak in.
        """
        region_ref = self._region_laser_af_references.get(region_id)
        base = self._base_laser_af_reference
        if region_ref is None:
            return base
        if region_ref.reference_image is None and base is not None:
            return base.model_copy(update={"x_reference": region_ref.x_reference})
        return region_ref

    def _apply_region_laser_af_reference(self, region_id) -> None:
        """Make ``region_id``'s effective laser-AF target active on the controller.

        No-op when laser AF is unavailable or no reference resolves (the latter
        is caught earlier by validate_acquisition_settings).
        """
        if self.laser_auto_focus_controller is None:
            return
        reference = self._resolve_region_laser_af_reference(region_id)
        if reference is not None:
            self.laser_auto_focus_controller.apply_reference(reference)

    def perform_autofocus(self, region_id, fov):
        # Phase F: the stage move that brought us to this FOV was fired async
        # by move_to_coordinate. When AF will actually touch hardware below,
        # join the motion here so the AF measurement happens on a settled
        # stage. When AF will no-op (disabled / skipped this FOV), skip the
        # wait so apply_observation_state + illuminate_for_capture at the
        # first capture can continue to run in parallel with motion — the
        # trigger-site _wait_for_move_settled() in acquire_camera_image is
        # the final gate.
        if self.do_reflection_af or self.do_autofocus:
            self._wait_for_move_settled()
        if not self.do_reflection_af:
            # Contrast-based AF. Runs for any z-stacking mode: AF establishes the
            # focal/reference plane and acquire_at_position/prepare_z_stack then
            # position the stack around it (bottom/center/top). Cadence-gated by
            # NUMBER_OF_FOVS_PER_AF.
            if (
                (self.do_autofocus)
                and (self.af_fov_count % Acquisition.NUMBER_OF_FOVS_PER_AF == 0)
            ):
                configuration_name_AF = MULTIPOINT_AUTOFOCUS_CHANNEL
                config_AF = self.liveController.get_channel_by_name(
                    self.objectiveStore.current_objective, configuration_name_AF
                )
                self._select_config(config_AF)
                if (
                    self.af_fov_count % Acquisition.NUMBER_OF_FOVS_PER_AF == 0
                ) or self.autofocusController.use_focus_map:
                    self.autofocusController.autofocus()
                    self.autofocusController.wait_till_autofocus_has_completed()
        else:
            # Laser-AF path. Decide between a full laser-AF "refresh" or a
            # table-only Z move, then run consistency checks where possible.
            # Load this region's focus target before ANY measurement below. Done
            # every FOV (cheap — just sets x_reference + crop, no hardware I/O)
            # so correctness never depends on the reference persisting across
            # FOVs/timepoints or on _last_region_id bookkeeping.
            self._apply_region_laser_af_reference(region_id)
            new_region_entry = self._last_region_id != region_id
            if new_region_entry:
                # Reset per-region-entry counters. Refreshes completed in earlier
                # visits to this region (e.g. previous timepoints) must not count
                # toward the new entry's cadence; the first FOV here always AFs.
                self._region_refresh_count_this_entry = 0
                self._fovs_since_refresh[region_id] = 0

            counter_due = self._fovs_since_refresh.get(region_id, 0) >= self._laser_af_refresh_every_n_fovs
            unseeded = (region_id, fov) not in self._fov_z_map
            is_refresh = new_region_entry or counter_due or unseeded

            if is_refresh:
                # Capture pre-refresh state so we can compare the new measurement
                # against what the table would have predicted. Only meaningful
                # when this is a mid-region refresh (>=2 in this region entry).
                prior_anchor_z = self._region_anchor_z_current.get(region_id)
                prior_anchor_fov = self._region_anchor_fov.get(region_id)
                prior_fov_z = self._fov_z_map.get((region_id, fov))

                with self._timing.get_timer("af:refresh"):
                    ok = self._run_laser_af_refresh(region_id, fov)
                if not ok:
                    self._last_region_id = region_id
                    return False

                # Mid-region consistency check: if this is the 2nd+ refresh in
                # the current region entry and we had table data for this FOV
                # coming in, compare the fresh measurement to the table's
                # prediction.
                if (
                    self._region_refresh_count_this_entry >= 1
                    and prior_anchor_z is not None
                    and prior_anchor_fov is not None
                    and prior_fov_z is not None
                    and (region_id, prior_anchor_fov) in self._fov_z_map
                ):
                    predicted_z = prior_anchor_z + (
                        prior_fov_z - self._fov_z_map[(region_id, prior_anchor_fov)]
                    )
                    measured_z = self._region_anchor_z_current[region_id]
                    diff_um = abs(predicted_z - measured_z) * 1000.0
                    if diff_um > self._laser_af_consistency_threshold_um:
                        self._log.warning(
                            f"Laser-AF consistency: table predicted z={predicted_z:.4f} mm, "
                            f"measured {measured_z:.4f} mm (diff={diff_um:.1f} µm) "
                            f"at region={region_id} fov={fov}"
                        )
                    else:
                        self._log.debug(
                            f"Laser-AF consistency OK: diff={diff_um:.1f} µm at region={region_id} fov={fov}"
                        )

                self._region_refresh_count_this_entry += 1
            else:
                # Already taken care of in initial move to pos
                # with self._timing.get_timer("af:table_move"):
                #     anchor_fov = self._region_anchor_fov[region_id]
                #     delta = self._fov_z_map[(region_id, fov)] - self._fov_z_map[(region_id, anchor_fov)]
                #     target_z = self._region_anchor_z_current[region_id] + delta
                #     self.stage.move_z_to(target_z)
                #     self._sleep(SCAN_STABILIZATION_TIME_MS_Z / 1000)
                self._fovs_since_refresh[region_id] = self._fovs_since_refresh.get(region_id, 0) + 1

                # TEMPORARY audit: measure laser-AF displacement at every
                # non-anchor FOV without correcting, to gauge how accurate the
                # table + anchor estimate remains vs a live measurement.
                # Remove once the approach is validated.
                # self._check_table_path_displacement(region_id, fov)

            self._last_region_id = region_id

            # End-of-region verification: when a region is too small to hit the
            # refresh cadence, only the initial refresh ever fires. Take one
            # extra displacement measurement at the last FOV to catch stale
            # anchor drift that the cadence would otherwise have exposed.
            region_coords = self.scan_region_fov_coords_mm.get(region_id, ())
            is_last_fov_in_region = len(region_coords) > 0 and fov == len(region_coords) - 1
            if (
                self._laser_af_check_last_fov_per_region
                and is_last_fov_in_region
                and self._region_refresh_count_this_entry == 1
            ):
                self._check_last_fov_displacement(region_id, fov)
        return True

    # TEMPORARY: header for the per-FOV table-path audit CSV. Columns cover
    # the before/after state around a full laser-AF correction so we can
    # directly compare the table+anchor estimate to a live focus measurement.
    _TABLE_PATH_AUDIT_HEADER = [
        "timestamp",
        "time_point",
        "region_id",
        "fov",
        "z_before_mm",
        "displacement_before_um",
        "correlation_before",
        "cc_ok_before",
        "z_after_mm",
        "displacement_after_um",
        "correlation_after",
        "cc_ok_after",
    ]

    def _check_table_path_displacement(self, region_id, fov):
        """TEMPORARY: compare the table+anchor Z estimate against a full AF
        correction at the same FOV.

        Sequence at each non-anchor FOV:
          1. Measure displacement + cross-correlation at the table-predicted Z.
          2. Run `move_to_target(0)` — the full laser-AF adjustment.
          3. Measure displacement + cross-correlation after the correction.
          4. Append the before/after pair to
             `{experiment_path}/table_path_audit.csv` for offline analysis.

        Remove once the approach is validated.
        """
        controller = self.laser_auto_focus_controller
        if controller is None:
            return

        # 1. Pre-correction audit — current stage is at the table-predicted Z.
        z_before = self.stage.get_pos().z_mm
        before = self._audit_laser_af_state(region_id, fov, phase="before")

        # 2. Full laser-AF correction. Errors are caught so the audit
        #    continues even if the measurement path misfires on one FOV.
        try:
            with self._timing.get_timer("af:table_path_audit_full_af"):
                controller.move_to_target(0)
        except Exception:
            self._log.exception(
                f"Table-path audit: move_to_target raised at region={region_id} fov={fov}"
            )

        # 3. Post-correction audit — current stage is at the focus-optimal Z.
        z_after = self.stage.get_pos().z_mm
        after = self._audit_laser_af_state(region_id, fov, phase="after")

        # 4. Append row.
        self._append_table_path_audit_row(region_id, fov, z_before, before, z_after, after)

    def _audit_laser_af_state(self, region_id, fov, phase):
        """Return {displacement_um, correlation, cc_ok} for the laser-AF
        view of the current Z. Used by the before/after table-path audit.
        """
        result = {"displacement_um": float("nan"), "correlation": float("nan"), "cc_ok": False}
        controller = self.laser_auto_focus_controller
        if controller is None:
            return result
        try:
            with self._timing.get_timer(f"af:table_path_audit_disp_{phase}"):
                result["displacement_um"] = float(controller.measure_displacement())
        except Exception:
            self._log.exception(
                f"Table-path audit ({phase}): measure_displacement raised at region={region_id} fov={fov}"
            )
        if getattr(controller, "reference_crop", None) is not None:
            try:
                with self._timing.get_timer(f"af:table_path_audit_cc_{phase}"):
                    cc_ok, correlation = controller._verify_spot_alignment()
                result["correlation"] = float(correlation) if correlation is not None else float("nan")
                result["cc_ok"] = bool(cc_ok)
            except Exception:
                self._log.exception(
                    f"Table-path audit ({phase}): _verify_spot_alignment raised at region={region_id} fov={fov}"
                )
        return result

    def _append_table_path_audit_row(self, region_id, fov, z_before, before, z_after, after):
        """Append one before/after audit row to the experiment's CSV."""
        if not self.experiment_path:
            return
        path = os.path.join(self.experiment_path, "table_path_audit.csv")
        try:
            file_exists = os.path.exists(path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", newline="") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(self._TABLE_PATH_AUDIT_HEADER)
                writer.writerow([
                    datetime.now().isoformat(timespec="seconds"),
                    self.time_point,
                    region_id,
                    fov,
                    f"{z_before:.6f}",
                    f"{before['displacement_um']:.4f}",
                    f"{before['correlation']:.4f}",
                    int(bool(before["cc_ok"])),
                    f"{z_after:.6f}",
                    f"{after['displacement_um']:.4f}",
                    f"{after['correlation']:.4f}",
                    int(bool(after["cc_ok"])),
                ])
        except Exception:
            self._log.exception(
                f"Table-path audit: failed to append CSV row for region={region_id} fov={fov}"
            )

    def _check_last_fov_displacement(self, region_id, fov):
        """Measure laser-AF displacement at the last FOV of a short region and
        warn if it exceeds the consistency threshold. Pure measurement — no Z
        move, no correction. Skipped if AF controller is missing or untrained.
        """
        controller = self.laser_auto_focus_controller
        if controller is None:
            return
        try:
            with self._timing.get_timer("af:last_fov_check"):
                displacement_um = controller.measure_displacement()
        except Exception:
            self._log.exception(
                f"Last-FOV laser-AF check raised at region={region_id} fov={fov}"
            )
            return
        if np.isnan(displacement_um):
            self._log.warning(
                f"Last-FOV laser-AF check: NaN displacement at region={region_id} fov={fov}"
            )
            return
        if abs(displacement_um) > self._laser_af_consistency_threshold_um:
            self._log.warning(
                f"Last-FOV laser-AF check: displacement {displacement_um:.1f} µm at "
                f"region={region_id} fov={fov} exceeds threshold "
                f"({self._laser_af_consistency_threshold_um:.1f} µm) — sample may have drifted"
            )
        else:
            self._log.debug(
                f"Last-FOV laser-AF check OK: displacement {displacement_um:.1f} µm at "
                f"region={region_id} fov={fov}"
            )

    def _run_laser_af_refresh(self, region_id, fov) -> bool:
        """Run a full laser AF and update the per-region anchor on success.

        On success: records `fov_z_map[(region, fov)]` if it's this FOV's first
        encounter, and updates `anchor_z_current` / `anchor_fov` / resets the
        refresh counter. On failure: logs and returns False only if there's no
        prior anchor AND no table entry to fall back on (mirrors legacy
        "log and continue" behavior otherwise).
        """
        self._log.debug(f"laser AF refresh (region={region_id} fov={fov})")
        measured_ok = False
        try:
            measured_ok = self.laser_auto_focus_controller.move_to_target(0)
        except Exception as e:
            file_ID = f"{region_id}_focus_camera.bmp"
            saving_path = os.path.join(self.base_path, self.experiment_ID, str(self.time_point), file_ID)
            iio.imwrite(saving_path, self.laser_auto_focus_controller.image)
            self._log.error(
                f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! laser AF failed at region={region_id} fov={fov} !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
                exc_info=e,
            )

        if measured_ok:
            measured_z = self.stage.get_pos().z_mm
            if (region_id, fov) not in self._fov_z_map:
                # Lazy-mode seeding, or a scan-mode FOV whose seed failed.
                self._fov_z_map[(region_id, fov)] = measured_z
            self._region_anchor_z_current[region_id] = measured_z
            self._region_anchor_fov[region_id] = fov
            self._fovs_since_refresh[region_id] = 0
            self._laser_af_successes += 1
            # Re-seat the delta map + proposals against the new anchor. Deltas
            # are shape differences between FOVs, so they're determined by the
            # static seed values — but re-parameterizing them against the live
            # anchor keeps both the fallback path and `move_to_coordinate`'s
            # non-blocking Z pre-move consistent.
            self._recompute_region_proposals(region_id, anchor_fov=fov)
            return True

        # Refresh failed (exception caught above or move_to_target returned False).
        self._laser_af_failures += 1

        # If we have a prior anchor and this FOV is in the table, silently apply
        # the table offset relative to the stale anchor and continue. Better than
        # leaving Z at whatever move_to_target left it (post-rollback or similar).
        if region_id in self._region_anchor_z_current and (region_id, fov) in self._fov_z_map:
            delta = self._fov_z_delta_map[(region_id, fov)]
            target_z = self._region_anchor_z_current[region_id] + delta
            self.stage.move_z_to(target_z)
            self._sleep(SCAN_STABILIZATION_TIME_MS_Z / 1000)
            self._fovs_since_refresh[region_id] = self._fovs_since_refresh.get(region_id, 0) + 1
            self._log.warning(f"Laser-AF refresh failed at region={region_id} fov={fov}; using stale anchor + table offset")
            return True

        # No prior anchor and no table entry — can't set Z. Match legacy failure path.
        return False

    # Columns for the per-acquisition autofocus log sidecar. position_index is
    # the FOV/position index; (x, y) disambiguate across regions. z_expected is
    # the pre-AF target Z; z_actual is the Z after correction (or after a failed
    # AF). af_status is "ok" or "failed".
    _AUTOFOCUS_LOG_HEADER = [
        "position_index",
        "t_index",
        "x",
        "y",
        "z_expected",
        "z_actual",
        "af_status",
    ]

    def _autofocus_and_record(self, region_id, fov, current_path):
        """Run autofocus once at the FOV's nominal plane and log the result.

        This establishes the focal/reference plane that the z-stack is built
        around (see :meth:`prepare_z_stack`). For laser AF the per-region
        reference captured by the "Update Ref" button is what defines that plane
        (the worker already loads it inside :meth:`perform_autofocus`), so the
        secondary AF offset is honored automatically. No-op when no AF is enabled.

        Records the pre-AF (expected/target) and post-AF (after-correction, or
        after-failure) absolute Z to ``autofocus_log.csv``.
        """
        if not (self.do_reflection_af or self.do_autofocus):
            return

        pos_before = self.stage.get_pos()
        z_expected_mm = pos_before.z_mm

        # Cache the pre-AF Z for cross-timepoint tracking exactly as before; the
        # laser-AF focus-map path may overwrite it with a table estimate inside
        # perform_autofocus.
        if self.Nt > 1:
            self._z_pos_proposal[(region_id, fov)] = z_expected_mm

        with self._timing.get_timer("perform_autofocus"):
            af_ok = self.perform_autofocus(region_id, fov)
        if not af_ok:
            self._log.error(
                f"Autofocus failed at region={region_id} fov={fov}. Continuing to acquire "
                f"anyway using the current z position (z={self.stage.get_pos().z_mm} [mm])"
            )

        # Laser-AF characterization debug image (unchanged behavior).
        if self.laser_auto_focus_controller and getattr(
            self.laser_auto_focus_controller, "characterization_mode", False
        ):
            try:
                image = self.laser_auto_focus_controller.get_image()
                file_ID = f"{region_id}_{fov:0{FILE_ID_PADDING}}_{0:0{FILE_ID_PADDING}}"
                iio.imwrite(os.path.join(current_path, file_ID + "_laser af camera" + ".bmp"), image)
            except Exception as e:
                self._log.warning(f"Failed to save laser-AF characterization image: {e}")

        pos_after = self.stage.get_pos()
        self._record_autofocus_event(
            position_index=fov,
            x_mm=pos_after.x_mm,
            y_mm=pos_after.y_mm,
            z_expected_mm=z_expected_mm,
            z_actual_mm=pos_after.z_mm,
            ok=bool(af_ok),
        )

    def _record_autofocus_event(self, position_index, x_mm, y_mm, z_expected_mm, z_actual_mm, ok):
        """Append one AF row to ``{experiment_path}/autofocus_log.csv``.

        Best-effort: a logging failure must never interrupt the acquisition.
        """
        if not self.experiment_path:
            return
        path = os.path.join(self.experiment_path, "autofocus_log.csv")
        try:
            file_exists = os.path.exists(path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", newline="") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(self._AUTOFOCUS_LOG_HEADER)
                writer.writerow(
                    [
                        position_index,
                        self.time_point,
                        f"{x_mm:.6f}",
                        f"{y_mm:.6f}",
                        f"{z_expected_mm:.6f}",
                        f"{z_actual_mm:.6f}",
                        "ok" if ok else "failed",
                    ]
                )
        except Exception as e:
            self._log.warning(f"Failed to append autofocus_log.csv: {e}")

    def _reference_z_level(self) -> int:
        """Z-plane index of the focus/reference plane within the stack.

        This is where a reference-z-only step/sweep captures its single frame.
        It matches the plane autofocus lands on (see _autofocus_and_record /
        prepare_z_stack): the first acquired plane (z_level 0) for From Bottom and
        From Top, and the middle plane for From Center. Clamped to [0, NZ-1].
        """
        if self.NZ <= 1:
            return 0
        if self.z_stacking_config == "FROM CENTER":
            return max(0, min(self.NZ - 1, int(round((self.NZ - 1) / 2))))
        return 0

    def prepare_z_stack(self):
        # Position the stage at the START slice of the stack, relative to the
        # focal plane established by _autofocus_and_record (which already ran).
        # FROM CENTER: step down half the stack so the AF plane is the center.
        # FROM BOTTOM/TOP: the AF plane is already the bottom/top slice (deltaZ's
        # sign, set in initialize_z_stack, carries the sweep direction), so no
        # extra offset is needed here.
        if self.z_stacking_config == "FROM CENTER":
            self.stage.move_z(-self.deltaZ * round((self.NZ - 1) / 2.0))
            self._sleep(SCAN_STABILIZATION_TIME_MS_Z / 1000)
        self._sleep(SCAN_STABILIZATION_TIME_MS_Z / 1000)

    def handle_z_offset(self, config, not_offset):
        z_offset = config.z_offset_um if isinstance(config, ObservationState) else getattr(config, "z_offset", None)
        if z_offset is not None and z_offset != 0.0:
            direction = 1 if not_offset else -1
            self._log.debug("Moving Z offset" + str(z_offset * direction))
            self.stage.move_z(z_offset / 1000 * direction)
            self.wait_till_operation_is_completed()
            self._sleep(SCAN_STABILIZATION_TIME_MS_Z / 1000)

    def _on_frame_arrived(self, frame_id: int) -> None:
        """Fired on the camera's delivery thread the moment raw bytes arrive,
        BEFORE decode. Runs once per frame and must stay fast — it's on the
        pacing-critical path the worker is blocked on.

        Responsibilities:
          * Snapshot _current_capture_info under `frame_id` so the later (off-
            thread) _image_callback can correlate. Must happen here, not in
            _image_callback, because acquire_camera_image sets
            _current_capture_info for the NEXT capture as soon as the worker
            wakes.
          * Bump the in-flight counter and clear _image_callback_idle so the
            end-of-acquisition wait can't complete until every frame's dispatch
            has drained.
          * Record the `ic_entry` / `ic_event_set` sub-timer boundaries.
          * Set _ready_for_next_trigger so the worker can proceed to the next
            capture while this frame's decode + dispatch continue in parallel.
        """
        ic_entry = time.perf_counter()
        info = self._current_capture_info
        self._current_capture_info = None
        # Only track outstanding for frames we actually expect _image_callback
        # to dispatch. A None info means the frame arrived without a matching
        # CaptureInfo — that's an error condition; _image_callback will log+abort,
        # and we don't want the missing-info case to leak a phantom outstanding
        # count into the end-of-acquisition wait.
        if info is not None:
            self._pending_capture_info_by_frame_id[frame_id] = info
            with self._outstanding_lock:
                self._outstanding_frames += 1
                self._image_callback_idle.clear()
        self._capture_ts["ic_entry"] = ic_entry
        self._ready_for_next_trigger.set()
        self._capture_ts["ic_event_set"] = time.perf_counter()

    def _image_callback(self, camera_frame: CameraFrame):
        # Deferred path (Tucsen SDK callback): _on_frame_arrived already
        # snapshotted the CaptureInfo under this frame_id, signalled the
        # worker, and bumped the outstanding counter. We just pop the info
        # and run dispatch. Missing key means the frame arrived without a
        # matching _on_frame_arrived call (bug or race — log+abort).
        #
        # Synchronous path (non-deferred cameras — simulated, thread-poll
        # cameras without arrived callbacks): _on_frame_arrived wasn't fired
        # so we do the snapshot + event set + counter bump here, just like
        # the original single-callback flow.
        if getattr(self, "_use_deferred_decode_callback", False):
            if camera_frame.frame_id not in self._pending_capture_info_by_frame_id:
                self._log.error(
                    "Deferred image callback fired without a pending CaptureInfo for frame_id=%d. Aborting.",
                    camera_frame.frame_id,
                )
                self.request_abort_fn()
                return
            info = self._pending_capture_info_by_frame_id.pop(camera_frame.frame_id)
        else:
            if self._ready_for_next_trigger.is_set():
                self._log.warning(
                    "Got an image in the image callback, but we didn't send a trigger. "
                    "Ignoring the image."
                )
                return
            ic_entry = time.perf_counter()
            info = self._current_capture_info
            self._current_capture_info = None
            self._ready_for_next_trigger.set()
            with self._outstanding_lock:
                self._outstanding_frames += 1
                self._image_callback_idle.clear()
            self._capture_ts["ic_entry"] = ic_entry
            self._capture_ts["ic_event_set"] = time.perf_counter()
        try:
            with self._timing.get_timer("_image_callback"):
                self._log.debug(f"In Image callback for frame_id={camera_frame.frame_id}")
                if not info:
                    self._log.error("In image callback, no current capture info! Something is wrong. Aborting.")
                    self.request_abort_fn()
                    return

                image = camera_frame.frame
                if not camera_frame or image is None:
                    self._log.warning("image in frame callback is None. Something is really wrong, aborting!")
                    self.request_abort_fn()
                    return

                # Increment image counter for Slack notification stats
                self._timepoint_image_count += 1
                self.image_count += 1

                with self._timing.get_timer("job creation and dispatch"):
                    # Wait for subprocess to be ready before first dispatch.
                    # Skip the PostprocessJob runner here: it only accumulates
                    # frames (compute happens at group completion), so blocking
                    # the first capture on its ~1-2s warmup would stall the run
                    # with illumination on. Its jobs queue fine before it's ready.
                    if not self._first_job_dispatched:
                        for job_class, job_runner in self._job_runners:
                            if job_class is PostprocessJob:
                                continue
                            if job_runner is not None:
                                t_wait_start = time.perf_counter()
                                if job_runner.wait_ready(timeout_s=10.0):
                                    t_wait_end = time.perf_counter()
                                    wait_ms = (t_wait_end - t_wait_start) * 1000
                                    if wait_ms > 10:  # Only log if we actually had to wait
                                        self._log.info(f"Job runner ready (waited {wait_ms:.0f}ms for subprocess)")
                                else:
                                    self._log.warning(f"Job runner for {job_class.__name__} not ready after 10s")
                        self._first_job_dispatched = True

                    for job_class, job_runner in self._job_runners:
                        job = self._create_job(job_class, info, image)
                        if job is None:
                            continue  # Skip if job creation returns None (e.g., downsampled views disabled for this image)
                        if job_runner is not None:
                            if not job_runner.dispatch(job):
                                self._log.error("Failed to dispatch multiprocessing job!")
                                self.request_abort_fn()
                                return
                        else:
                            try:
                                # NOTE(imo): We don't have any way of people using results, so for now just
                                # grab and ignore it.
                                result = job.run()
                            except Exception:
                                self._log.exception("Failed to execute job, abandoning acquisition!")
                                self.request_abort_fn()
                                return

                height, width = image.shape[:2]
                # with self._timing.get_timer("crop_image"):
                #     image_to_display = utils.crop_image(
                #         image,
                #         round(width * self.display_resolution_scaling),
                #         round(height * self.display_resolution_scaling),
                #     )
                with self._timing.get_timer("image_to_display*.emit"):
                    # Remember the live-display frame size: the shared napari /
                    # contrast display holds one image size across all channels,
                    # so a postprocess output preview is only safe to display when
                    # it matches this (see _emit_postprocess_display).
                    self._last_raw_display_shape = image.shape[:2]
                    self.callbacks.signal_new_image(camera_frame, info)

        finally:
            with self._outstanding_lock:
                self._outstanding_frames = max(0, self._outstanding_frames - 1)
                if self._outstanding_frames == 0:
                    self._image_callback_idle.set()

    def _frame_wait_timeout_s(self):
        return (self.camera.get_total_frame_time() / 1e3) + 10

    def acquire_camera_image(
        self,
        config,
        file_ID: str,
        current_path: str,
        k: int,
        region_id: int,
        fov: int,
        config_idx: int,
        *,
        filename_channel_label: Optional[str] = None,
        save_layout=None,
        postprocess_group: Optional[str] = None,
    ):
        # When keeping illuminators on between captures, turn off the previous channel
        # before switching currentConfiguration (software trigger only).
        if (
            self.liveController.trigger_mode == TriggerMode.SOFTWARE
            and self.keep_illuminators_on_between_captures
            and self._last_illumination_config_name is not None
            and self._last_illumination_config_name != config.name
        ):
            with self._timing.get_timer("turn_off_prev_channel_illumination"):
                self.liveController.obs_controller.turn_off_illumination()

        # trigger acquisition (including turning on the illumination) and read frame
        camera_illumination_time = self.camera.get_exposure_time()
        using_preset_obs_state = self._use_observation_presets
        is_waveform_driven = bool(getattr(config, "is_waveform_driven", False))
        with self._timing.get_timer("illuminate_for_capture"):
            if is_waveform_driven:
                # DC intensities ON, digital gating left to the NIDAQ pulse.
                self._apply_illumination_for_waveform_capture(config)
            elif using_preset_obs_state:
                self._apply_current_illumination_state_to_hardware()
            else:
                self.liveController.obs_controller.turn_on_illumination()
            # Note: no outer `wait_till_operation_is_completed()` — both paths above
            # route through `IlluminationController.set_channel_state`, which waits
            # per-device (shutter_ep.wait() for NI-DAQ endpoints, or the MCU's own
            # wait_till_operation_is_completed for MCU-gated channels) before
            # returning. An outer wait was redundant and cost ~5 ms/capture in
            # CV-lock/wait_for overhead.
        # Give the LED shutter time to reach stable brightness before the camera
        # begins integrating. Needed on rolling-shutter sensors — without this the
        # top rows start exposing on the shutter's rising edge and show a
        # top-bright gradient. Configured per-rig via
        # software.acquisition.illumination_settle_ms in the machine config.
        settle_ms = control._def.Acquisition.ILLUMINATION_SETTLE_MS
        # self._log.info(f"Acquisition settle ms: {settle_ms}")
        if settle_ms > 0:
            with self._timing.get_timer("illumination_settle"):
                self._sleep(settle_ms / 1000.0)
        # This is some large timeout that we use just so as to not block forever
        with self._timing.get_timer("_ready_for_next_trigger.wait"):
            if not self._ready_for_next_trigger.wait(self._frame_wait_timeout_s()):
                self._log.error("Frame callback never set _have_last_triggered_image callback! Aborting acquisition.")
                self.request_abort_fn()
                return

        # Backpressure check AFTER previous frame dispatched, BEFORE next trigger
        # This is when we know the previous image's jobs have been dispatched (and counters incremented)
        if self._backpressure.should_throttle():
            with self._timing.get_timer("backpressure.wait_for_capacity"):
                got_capacity = self._backpressure.wait_for_capacity()
                if not got_capacity:
                    self._log.error(
                        f"Backpressure timeout - disk I/O cannot keep up. Stats: {self._backpressure.get_stats()}"
                    )

        # The prior-frame `_ready_for_next_trigger.wait` above already guarantees
        # the camera is ready for the next trigger in SW/HW mode. The former
        # get_ready_for_trigger re-check loop here was a no-op on the hot path
        # (its own comment said so) but added a 1 ms sleep and another timer.
        self._ready_for_next_trigger.clear()
        with self._timing.get_timer("current_capture_info ="):
            # Even though the capture time will be slightly after this, we need to capture and set the capture info
            # before the trigger to be 100% sure the callback doesn't stomp on it.
            # NOTE(imo): One level up from acquire_camera_image, we have acquire_pos.  We're careful to use that as
            # much as we can, but don't use it here because we'd rather take the position as close as possible to the
            # real capture time for the image info.  Ideally we'd use this position for the caller's acquire_pos as well.
            current_capture_info = CaptureInfo(
                position=self.stage.get_pos(),
                z_index=k,
                capture_time=time.time(),
                z_piezo_um=(self.z_piezo_um if self.use_piezo else None),
                observation_state=config,
                save_directory=current_path,
                file_id=file_ID,
                region_id=region_id,
                fov=fov,
                configuration_idx=config_idx,
                time_point=self.time_point,
                filename_channel_label=filename_channel_label,
                file_saving_option=self.file_saving_option,
                acquisition_root=self.experiment_path,
                array_key=(save_layout.array_key if save_layout else None),
                save_t_index=(save_layout.t_index if save_layout else None),
                save_c_index=(save_layout.c_index if save_layout else None),
                save_t_size=(save_layout.t_size if save_layout else None),
                save_c_size=(save_layout.c_size if save_layout else None),
                save_z_size=(save_layout.z_size if save_layout else None),
                cycle_event_index=(save_layout.cycle_event_index if save_layout else None),
                state_frame_index=(save_layout.state_frame_index if save_layout else None),
                frame_suffix=(save_layout.frame_suffix if save_layout else None),
                array_channel_names=(list(save_layout.channel_names) if save_layout else None),
                array_channel_colors=(list(save_layout.channel_colors) if save_layout else None),
                array_channel_wavelengths=(list(save_layout.channel_wavelengths) if save_layout else None),
                postprocess_group=postprocess_group,
            )
            self._current_capture_info = current_capture_info
        # Hot path — demoted to debug so formatting CaptureInfo (dataclass with Pos and
        # ObservationState) doesn't cost ~10–15 ms per capture when info handlers are attached.
        # self._log.debug(
        #     "Triggering camera for capture: %s, position=%s, z_index=%d",
        #     current_capture_info.observation_state.name,
        #     current_capture_info.position,
        #     k,
        # )
        if self.liveController.trigger_mode != TriggerMode.CONTINUOUS:
            # Block until the camera reports it's ready for the next trigger
            # before we actually send one. _ready_for_next_trigger above is a
            # worker-side flag set by _image_callback — it tells us the previous
            # frame arrived, but not that the camera's own _trigger_sent flag has
            # been cleared (those get updated by different lines in the SDK
            # callback). Poll get_ready_for_trigger() here so the trigger never
            # races the flag update. Timeout = frame_time + generous slack;
            # past that the camera is genuinely stuck and we abort.
            if self.liveController.trigger_mode == TriggerMode.SOFTWARE:
                with self._timing.get_timer("wait_camera_ready_for_trigger"):
                    wait_timeout_s = self.camera.get_total_frame_time() / 1e3 + 0.1
                    ready_deadline = time.time() + wait_timeout_s
                    while not self.camera.get_ready_for_trigger():
                        if time.time() >= ready_deadline:
                            self._log.error(
                                f"Camera never became ready for next trigger within {wait_timeout_s:.3f}s. Aborting."
                            )
                            self.request_abort_fn()
                            return
                        time.sleep(0.0005)
            # Reset per-capture timestamps before trigger so stale values from
            # the previous capture can't contaminate the sub-timer breakdown.
            self._capture_ts = {}
            # Phase F: if an async stage move is still in flight (common at
            # the first capture of a new FOV — we skipped the blocking wait
            # in move_to_coordinate so apply_observation_state and
            # illuminate_for_capture could run in parallel with motion),
            # join it now. For subsequent captures at the same FOV this is
            # a no-op since the wait already cleared the flag.
            self._wait_for_move_settled()

            # Arm the per-frame NIDAQ pulse waveform before firing the camera so
            # the task is already waiting for the camera's exposure-active edge
            # by the time send_trigger returns.
            nidaq_pulse_cleanup = (
                self._arm_nidaq_pulse_for_capture(config) if is_waveform_driven else None
            )

            try:
                with self._timing.get_timer("send_trigger"):
                    self.camera.send_trigger(illumination_time=camera_illumination_time)
                self._capture_ts["post_trigger"] = time.perf_counter()
            except Exception:
                if nidaq_pulse_cleanup is not None:
                    try:
                        nidaq_pulse_cleanup()
                    except Exception:
                        self._log.exception("NIDAQ cleanup failed after send_trigger error")
                raise
        else:
            nidaq_pulse_cleanup = None

        try:
            with self._timing.get_timer("exposure_time_done_sleep_hw or wait_for_image_sw"):
                if self.liveController.trigger_mode == TriggerMode.HARDWARE:
                    # Per-capture, so keep at debug to avoid log-format overhead on the hot path.
                    self._log.debug("Waiting %.3f [s] for exposure to complete", self.camera.get_total_frame_time() / 1e3)
                    exposure_done_time = time.time() + self.camera.get_total_frame_time() / 1e3
                    # Even though we can do overlapping triggers, we want to make sure that we don't move before our exposure
                    # is done.  So we still need to at least sleep for the total frame time corresponding to this exposure.
                    self._sleep(max(0.0, exposure_done_time - time.time()))
                else:
                    # In SW trigger mode (or anything not HARDWARE mode), there's indeterminism in the trigger timing.
                    # To overcome this, just wait until the frame for this capture actually comes into the image
                    # callback.  That way we know we have it.  This also helps by making sure the illumination for this
                    # frame is on from before the trigger until after we get the frame (which guarantees it will be on
                    # for the full exposure).
                    #
                    # If we wait for longer than 5x the exposure + 2 seconds, abort the acquisition because something is
                    # wrong.
                    non_hw_frame_timeout = 5 * self.camera.get_total_frame_time() / 1e3 + 2
                    if not self._ready_for_next_trigger.wait(non_hw_frame_timeout):
                        self._log.error(f"Timed out waiting {non_hw_frame_timeout} [s] for a frame, aborting acquisition.")
                        self.request_abort_fn()
                        # Let this fall through so we still turn off illumination.  Let the caller actually break out
                        # of the acquisition.
        finally:
            if nidaq_pulse_cleanup is not None:
                nidaq_pulse_cleanup()
        # Break the wait window into sub-intervals so we can see where the
        # ~140 ms/capture goes. Camera-side stamps come from
        # camera._last_capture_ts (populated by _on_sdk_trigger_frame or
        # _wait_for_frame). Worker-side stamps come from _image_callback.
        # Any path that didn't populate a key just skips that sub-timer.
        self._record_capture_sub_timings()

        # Turn off capture illumination after a one-frame snap unless the user explicitly keeps it on.
        if not self.keep_illuminators_on_between_captures:
            with self._timing.get_timer("turn_off_capture_illumination"):
                self._turn_off_capture_illumination_preserving_logical_state()
        self._last_illumination_config_name = config.name

    def _record_capture_sub_timings(self) -> None:
        """Break the per-capture "wait for image" window into named sub-timers.

        Timeline (SW trigger, deferred-decode or thread-poll):
            post_trigger       <- worker: after camera.send_trigger returns
            sdk_entry          <- camera: top of _on_sdk_trigger_frame / read-thread frame-handle block
            sdk_cleared        <- camera: after _trigger_sent.clear (before decode / callback fire)
            ic_entry           <- worker _on_frame_arrived top (sync from sdk_cleared via fire_arrived_callbacks)
            ic_event_set       <- worker: after _ready_for_next_trigger.set
            (wait returns)     <- the "exposure_time_done_sleep_hw or wait_for_image_sw" timer stops here
            sdk_decoded        <- camera: after decode + CameraFrame build (off-critical-path,
                                   available only AFTER the worker wait returns — set by the
                                   decode thread in callback mode, inline in thread-poll mode)

        Sub-timers (``wait:*`` are on the worker's critical path):
            wait:trigger_to_sdk       — camera exposure + readout + SDK dispatch
            wait:sdk_entry_to_clear   — SDK-callback bookkeeping up to _trigger_sent.clear
            wait:sdk_clear_to_ic      — _trigger_sent.clear → worker _on_frame_arrived entry
            wait:ic_event_set         — _on_frame_arrived entry → _ready_for_next_trigger.set
            wait:event_to_wake        — Event.set → .wait return in worker thread
        """
        ts = dict(self._capture_ts)  # snapshot
        # Pull camera-side stamps now that the wait has returned. sdk_decoded
        # is intentionally excluded: it's populated on the decode thread AFTER
        # the wait returns, so reading it here is racy and meaningless on the
        # critical-path report. A separate decode timer (future) should own it.
        cam_ts = getattr(self.camera, "_last_capture_ts", None) or {}
        for key in ("sdk_entry", "sdk_cleared"):
            if key in cam_ts:
                ts[key] = cam_ts[key]
        ts["post_wait"] = time.perf_counter()

        def pair(name: str, a: str, b: str) -> None:
            if a in ts and b in ts and ts[b] >= ts[a]:
                self._timing.get_timer(name).record(ts[a], ts[b])

        pair("wait:trigger_to_sdk",     "post_trigger", "sdk_entry")
        pair("wait:sdk_entry_to_clear", "sdk_entry",    "sdk_cleared")
        pair("wait:sdk_clear_to_ic",    "sdk_cleared",  "ic_entry")
        pair("wait:ic_event_set",       "ic_entry",     "ic_event_set")
        pair("wait:event_to_wake",      "ic_event_set", "post_wait")

    def _sleep(self, sec):
        time_to_sleep = max(sec, 1e-6)
        # self._log.debug(f"Sleeping for {time_to_sleep} [s]")
        time.sleep(time_to_sleep)

    def _interruptible_sleep(self, sec, slice_s: float = 0.05):
        """Sleep up to ``sec`` seconds, returning early if an abort is requested.

        Used for cycle wait periods, which may be long — a plain time.sleep would
        block abort until it elapsed.
        """
        if sec <= 0:
            return
        deadline = time.time() + sec
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            if self.abort_requested_fn():
                return
            time.sleep(min(slice_s, remaining))

    def handle_acquisition_abort(self, current_path):
        # Save coordinates.csv (skip for ZARR_V3 — the controller's root copy is canonical
        # and the per-timepoint folder may not exist).
        if self.file_saving_option != FileSavingOption.ZARR_V3:
            self.coordinates_pd.to_csv(os.path.join(current_path, "coordinates.csv"), index=False, header=True)
        self.microcontroller.enable_joystick(True)

        self._wait_for_outstanding_callback_images()

    def move_z_for_stack(self):
        if self.use_piezo:
            self.z_piezo_um += self.deltaZ * 1000
            self.piezo.move_to(self.z_piezo_um)
            if (
                self.liveController.trigger_mode == TriggerMode.SOFTWARE
            ):  # for hardware trigger, delay is in waiting for the last row to start exposure
                self._sleep(MULTIPOINT_PIEZO_DELAY_MS / 1000)
        else:
            self.stage.move_z(self.deltaZ)
            self._sleep(SCAN_STABILIZATION_TIME_MS_Z / 1000)

    def move_z_back_after_stack(self):
        if self.use_piezo:
            self.z_piezo_um = self.z_piezo_um - self.deltaZ * 1000 * (self.NZ - 1)
            self.piezo.move_to(self.z_piezo_um)
            if (
                self.liveController.trigger_mode == TriggerMode.SOFTWARE
            ):  # for hardware trigger, delay is in waiting for the last row to start exposure
                self._sleep(MULTIPOINT_PIEZO_DELAY_MS / 1000)
        else:
            if self.z_stacking_config == "FROM CENTER":
                rel_z_to_start = -self.deltaZ * (self.NZ - 1) + self.deltaZ * round((self.NZ - 1) / 2)
            else:
                rel_z_to_start = -self.deltaZ * (self.NZ - 1)

            self.stage.move_z(rel_z_to_start)
