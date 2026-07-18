"""SMB streaming uploads for live OME-Zarr acquisitions.

Owns the upload pipeline that runs alongside an active acquisition:
- ``UploadTarget`` carries the remote SMB root + delete-local policy through
  ``AcquisitionParameters`` and ``ZarrWriterInfo``.
- ``UploadTask`` / ``UploadResult`` are the queue payloads.
- ``UploadWorker`` is a dedicated ``multiprocessing.Process`` that copies each
  file to the SMB share with ``.part``+atomic-rename, verifies a sha256 across
  source and remote, appends a manifest record with ``fsync``, and reports
  back through an output queue.
- ``upload_one_file`` is the file-level primitive, factored out so the
  standalone backfill script (``software/scripts/zarr_backfill_upload.py``)
  can reuse the exact same copy/verify/manifest semantics on quiescent
  experiment directories.

The writer thread never blocks on network I/O: the worker is a separate
process with an unbounded input queue. When the network is unavailable, the
queue grows; deletions defer; acquisition keeps imaging.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import queue
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional, Set, Tuple

import squid.logging


# Block size for streaming copy + hash. 4 MiB balances syscall overhead against
# memory residency for parallel uploads.
COPY_CHUNK_BYTES = 4 * 1024 * 1024

# Retry budget for transient SMB failures. Backoff: 1, 2, 4, 8, 16 seconds.
MAX_UPLOAD_ATTEMPTS = 5
INITIAL_BACKOFF_S = 1.0

# --- Pipelined worker tunables -------------------------------------------
# The worker runs N file uploads concurrently. Each upload writes the file up
# (send direction) then reads it all back to sha256-verify (receive
# direction); 1 GbE is full-duplex, so with >=2 lanes one file's read-back
# overlaps another's write and the verification cost is largely hidden behind
# the next upload instead of serializing after it. N also hides per-file SMB
# round-trip latency on the long tail of small pyramid/metadata files. It does
# NOT (and cannot) push past the link's bandwidth ceiling — for large shards a
# single stream already saturates the pipe.
UPLOAD_PIPELINED = True          # False -> legacy strictly-sequential path (rollback)
UPLOAD_WORKER_THREADS = 2        # concurrent copy/verify lanes
UPLOAD_MAX_INFLIGHT_FILES = 8    # cap submitted-but-unfinished files (bounds RAM)
UPLOAD_VERIFY_READBACK = True    # read the remote file back and sha256-verify it

# Sentinel on the input queue tells the worker to drain and exit.
_SHUTDOWN_SENTINEL = "__upload_worker_shutdown__"


@dataclass
class UploadTarget:
    """Per-acquisition upload configuration.

    Attached to ``ZarrWriterInfo`` and ``JobRunner`` so writer subprocesses
    can spawn an ``UploadWorker`` on startup. Travels through the
    multiprocessing pickle, so all fields must be picklable.

    Attributes:
        enabled: Master switch. When False, no upload worker is spawned and
            ``FlushAndStageUploadJob`` is a no-op.
        remote_root: Remote root directory (UNC ``\\\\server\\share\\dir`` on
            Windows or POSIX ``//server/share/dir``). Local experiment paths
            are mirrored under this root by prefix swap on ``local_base``.
        local_base: Local experiment root (``{base_path}/{experiment_ID}``).
            Used to compute ``remote_path = remote_root + (local_path - local_base)``.
        delete_after_verify: When True, ``multi_point_worker`` deletes local
            shard files for a timepoint once every shard in that timepoint
            verifies on the remote.
    """

    enabled: bool = False
    remote_root: str = ""
    local_base: str = ""
    delete_after_verify: bool = True


@dataclass
class UploadTask:
    """One unit of work for ``UploadWorker``.

    Groups all files for a single ``(time_point, region_id, fov)`` bundle so
    the main worker can correlate task completion with batched deletion.

    ``deletable_local_paths`` is the **whitelist** of local files that the
    consumer is allowed to delete after a successful remote verify. Files
    in ``files`` whose ``local_path`` is not in this set are shared metadata
    that the active writer needs in place (group ``zarr.json``, per-level
    ``zarr.json``, ``frame_times/c/0/0/0``); they are uploaded fresh every
    barrier so the remote tree stays readable but the **local** copy must
    remain in place until acquisition finalizes. Empty set means "upload
    only, do not delete anything from this task".
    """

    task_id: str
    time_point: int
    region_id: str
    fov: int
    # (local_path, remote_path) pairs.
    files: List[Tuple[str, str]] = field(default_factory=list)
    # Subset of local paths (from files) that are safe to delete locally
    # after a sha256-verified upload. Anything not in this set is treated
    # as shared/never-delete metadata.
    deletable_local_paths: Set[str] = field(default_factory=set)
    # Subset of local paths whose source may be rewritten by another process
    # during our upload (currently: metadata files — group/per-level
    # ``zarr.json``, ``frame_times/c/0/0/0``). ``UploadWorker`` runs these
    # through ``upload_one_file(..., stable_read=True)`` so a torn read is
    # detected via a post-copy source re-hash and the upload is retried.
    stable_read_paths: Set[str] = field(default_factory=set)


@dataclass
class UploadResult:
    """Report emitted by ``UploadWorker`` once a task has been attempted.

    ``uploaded_paths`` records every file successfully verified on the
    remote (used by the manifest accountant). ``deletable_uploaded_paths``
    is the strict subset that the caller may delete locally — i.e. paths
    that were both in ``UploadTask.deletable_local_paths`` AND verified.
    Splitting these prevents the live pipeline from ever rm'ing a shared
    metadata file out from under a still-running writer.
    """

    task_id: str
    time_point: int
    region_id: str
    fov: int
    success: bool
    uploaded_paths: List[str] = field(default_factory=list)
    deletable_uploaded_paths: List[str] = field(default_factory=list)
    failed_paths: List[Tuple[str, str]] = field(default_factory=list)  # (local, error)
    error: Optional[str] = None


def is_smb_path(path: str) -> bool:
    """True if ``path`` looks like a UNC or ``//`` SMB path."""
    if not path:
        return False
    return path.startswith("\\\\") or path.startswith("//")


def local_to_remote_path(local_path: str, local_base: str, remote_base: str) -> str:
    """Map a local file path to its remote counterpart under ``remote_base``.

    Uses **native separators** throughout the result so OS APIs accept the
    path: backslashes on Windows (including ``\\\\server\\share`` UNCs and
    mapped drives like ``Z:\\``), forward slashes on POSIX. Accepts mixed
    input — a remote root typed as ``//srv/share/dir`` is normalized to
    ``\\\\srv\\share\\dir`` on Windows, and oddities like ``Z://path`` or
    ``Z://a//b`` are collapsed to ``Z:\\path`` / ``Z:\\a\\b`` (Windows
    treats ``\\\\`` after a drive letter as an invalid UNC, so we have to
    collapse rather than just substitute separators).

    ``local_path`` must live under ``local_base``. Comparison is
    case-insensitive on Windows (where ``C:\\Foo`` and ``c:/foo`` refer to
    the same path) and case-sensitive elsewhere.
    """
    abs_local = os.path.normpath(local_path)
    abs_base = os.path.normpath(local_base)
    if os.name == "nt":
        prefix_ok = abs_local.lower().startswith(abs_base.lower())
    else:
        prefix_ok = abs_local.startswith(abs_base)
    if not prefix_ok:
        # Caller responsibility; we don't want to silently misroute.
        raise ValueError(
            f"local_path {local_path!r} is not under local_base {local_base!r}"
        )
    rel = os.path.relpath(abs_local, abs_base)
    # Normalize remote_base end-to-end. On Windows, ``os.path.normpath``
    # collapses redundant separators (``Z://path`` → ``Z:\\path``), turns
    # forward slashes into backslashes, and crucially preserves the
    # leading ``\\\\`` of UNC paths. On POSIX it collapses ``//`` to ``/``.
    # We strip a trailing separator after normalization so the join below
    # doesn't double-up (``os.path.normpath('Z:/')`` returns ``'Z:\\'``,
    # which would compose to ``'Z:\\\\rel'`` if not stripped).
    remote_base_clean = os.path.normpath(remote_base).rstrip("/\\")
    if os.name == "nt":
        rel = rel.replace("/", "\\")
    else:
        remote_base_clean = remote_base_clean.replace("\\", "/")
        rel = rel.replace("\\", "/")
    return remote_base_clean + os.sep + rel


def _sha256_of_file(path: str, heartbeat: Optional[Callable[[int], None]] = None) -> str:
    """Compute sha256 of ``path`` by streaming.

    ``heartbeat`` (if given) is called with each chunk's byte count so a
    parent watchdog can tell forward progress (read bytes flowing) from a
    genuinely wedged SMB handle (no bytes for a long window).
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(COPY_CHUNK_BYTES)
            if not chunk:
                break
            h.update(chunk)
            if heartbeat is not None:
                heartbeat(len(chunk))
    return h.hexdigest()


def _stream_copy_with_hash(
    src: str, dest: str, heartbeat: Optional[Callable[[int], None]] = None
) -> Tuple[str, int]:
    """Copy ``src`` to ``dest`` while hashing the source stream.

    Returns ``(sha256_hex, bytes_written)``. Caller is responsible for any
    pre-existing destination cleanup (we open dest with ``"wb"``).
    ``heartbeat`` (if given) is called with each chunk's byte count.
    """
    h = hashlib.sha256()
    bytes_written = 0
    with open(src, "rb") as fsrc, open(dest, "wb") as fdest:
        while True:
            chunk = fsrc.read(COPY_CHUNK_BYTES)
            if not chunk:
                break
            fdest.write(chunk)
            h.update(chunk)
            bytes_written += len(chunk)
            if heartbeat is not None:
                heartbeat(len(chunk))
    return h.hexdigest(), bytes_written


def upload_one_file(
    local_path: str,
    remote_path: str,
    *,
    log: "squid.logging.SquidLogger",
    max_attempts: int = MAX_UPLOAD_ATTEMPTS,
    initial_backoff_s: float = INITIAL_BACKOFF_S,
    stable_read: bool = False,
    verify_readback: bool = UPLOAD_VERIFY_READBACK,
    heartbeat: Optional[Callable[[int], None]] = None,
) -> Tuple[bool, Optional[str], Optional[int], Optional[str]]:
    """Copy ``local_path`` to ``remote_path`` with retry, sha256 verification,
    and atomic rename.

    Steps per attempt:
        1. Stream-copy local → ``remote_path + ".part"``, hashing the source
           as it is read (``src_hash``).
        2. If ``stable_read`` is set, re-hash the source after the copy
           (``src_hash_post``). If it does not match ``src_hash``, the source
           file was being written by another process during our read — the
           copy is therefore torn. Retry.
        3. Read back the ``.part`` and hash the destination.
        4. If all digests match, ``os.replace`` to the final remote path.
        5. On any ``OSError`` / digest mismatch, wait exponential backoff and
           retry. Stale ``.part`` from the previous attempt is best-effort
           cleared before the next try.

    ``stable_read`` should be set for files that may be concurrently rewritten
    by an active acquisition writer — specifically the per-FOV ``zarr.json``
    metadata (rewritten at finalize) and ``frame_times/c/0/0/0`` (rewritten
    on every ``record_frame_time`` call). For shard files written once and
    then never touched again (``<level>/c/<t>/0/<z>/0/0`` per z-slice, or
    ``<level>/c/<t>/0/0/0/0`` in the legacy per-FOV layout, after the barrier
    drains pending TensorStore futures), ``stable_read=False`` is correct
    and avoids the extra source re-hash.

    Returns ``(success, sha256_hex, bytes_written, error)``. On failure,
    ``sha256_hex`` and ``bytes_written`` are ``None``; ``error`` carries the
    last exception's string for the manifest/log.
    """
    if not os.path.exists(local_path):
        return False, None, None, f"source missing: {local_path}"

    remote_dir = os.path.dirname(remote_path)
    part_path = remote_path + ".part"

    last_error: Optional[str] = None
    for attempt in range(max_attempts):
        try:
            if remote_dir:
                os.makedirs(remote_dir, exist_ok=True)
        except OSError as e:
            last_error = f"mkdir {remote_dir}: {e}"
            log.warning(last_error)
        else:
            try:
                # Best-effort clear of any stale .part from a previous attempt.
                if os.path.exists(part_path):
                    try:
                        os.remove(part_path)
                    except OSError:
                        pass
                src_hash, n_bytes = _stream_copy_with_hash(
                    local_path, part_path, heartbeat=heartbeat
                )
                if stable_read:
                    src_hash_post = _sha256_of_file(local_path, heartbeat=heartbeat)
                    if src_hash_post != src_hash:
                        last_error = (
                            f"source changed during copy "
                            f"(during-copy sha256={src_hash} != post-copy sha256={src_hash_post}); "
                            f"another process is writing"
                        )
                        log.warning(
                            f"{last_error} ({local_path} -> {remote_path}); retrying"
                        )
                        # Fall through to retry/backoff path.
                        if attempt < max_attempts - 1:
                            wait = initial_backoff_s * (2 ** attempt)
                            time.sleep(wait)
                        continue
                if verify_readback:
                    # Read the bytes back off the remote and hash them. There is
                    # no server-side hash op over plain SMB, so end-to-end
                    # verification means reading the stored file back down — the
                    # cost is hidden by running this lane concurrently with other
                    # files' uploads on the full-duplex link, not by skipping it.
                    dest_hash = _sha256_of_file(part_path, heartbeat=heartbeat)
                    verified = src_hash == dest_hash
                    mismatch_err = (
                        f"sha256 mismatch after copy: src={src_hash} dest={dest_hash}"
                    )
                else:
                    # Weaker guarantee: trust the source hash + the write, confirm
                    # only that the remote size matches. Catches truncation/short
                    # writes, not silent corruption. Off by default.
                    dest_size = os.path.getsize(part_path)
                    verified = dest_size == n_bytes
                    mismatch_err = (
                        f"size mismatch after copy: src={n_bytes} dest={dest_size}"
                    )
                if not verified:
                    last_error = mismatch_err
                    log.warning(f"{last_error} ({local_path} -> {remote_path})")
                else:
                    # Atomic on Windows and POSIX. SMB honors rename on the same share.
                    os.replace(part_path, remote_path)
                    return True, src_hash, n_bytes, None
            except OSError as e:
                last_error = f"{type(e).__name__}: {e}"
                log.warning(
                    f"upload attempt {attempt + 1}/{max_attempts} failed for "
                    f"{local_path} -> {remote_path}: {last_error}"
                )

        if attempt < max_attempts - 1:
            wait = initial_backoff_s * (2 ** attempt)
            time.sleep(wait)

    return False, None, None, last_error


def append_manifest_record(
    manifest_path: str,
    record: dict,
) -> None:
    """Append one JSON-lines record to ``manifest_path`` with fsync.

    Caller composes the record dict; we only own the durability story.
    Failures are logged but do not abort the upload — the on-disk file is
    still valid even if our own bookkeeping is stale.
    """
    line = json.dumps(record, sort_keys=True) + "\n"
    try:
        os.makedirs(os.path.dirname(manifest_path) or ".", exist_ok=True)
        with open(manifest_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        # Logging at warning rather than raising: manifest loss costs us
        # recovery info but does not corrupt the remote dataset.
        squid.logging.get_logger("zarr_upload").warning(
            f"Failed to append upload manifest record to {manifest_path}: {e}"
        )


def read_manifest(manifest_path: str) -> List[dict]:
    """Read all records from a JSON-lines manifest. Skips malformed lines."""
    out: List[dict] = []
    if not os.path.isfile(manifest_path):
        return out
    with open(manifest_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


class UploadWorker(multiprocessing.Process):
    """Dedicated subprocess that drains ``UploadTask``s onto the SMB share.

    Designed so a stalled SMB connection never blocks the zarr writer:
    - Lives in its own process with its own queue.
    - Non-daemon so SIGTERM during shutdown lets us drain the backlog.
    - Reports per-task success/failure on an output queue; the main worker
      consumes those to decide when a timepoint is safe to delete locally.
    """

    def __init__(
        self,
        target: UploadTarget,
        manifest_path: str,
        max_attempts: int = MAX_UPLOAD_ATTEMPTS,
        initial_backoff_s: float = INITIAL_BACKOFF_S,
        pipelined: bool = UPLOAD_PIPELINED,
        threads: int = UPLOAD_WORKER_THREADS,
        max_inflight_files: int = UPLOAD_MAX_INFLIGHT_FILES,
        verify_readback: bool = UPLOAD_VERIFY_READBACK,
    ):
        super().__init__()
        # Non-daemon: explicit drain on shutdown, not silent kill. The owning
        # drainer also force-terminates us on a watchdog stall or at app exit
        # (_BackgroundUploadDrainer / terminate_all_upload_drainers), so a
        # wedged SMB handle can never block interpreter shutdown.
        self.daemon = False
        self._target = target
        self._manifest_path = manifest_path
        self._max_attempts = max_attempts
        self._initial_backoff_s = initial_backoff_s
        self._pipelined = pipelined
        self._threads = max(1, threads)
        self._max_inflight_files = max(self._threads, max_inflight_files)
        self._verify_readback = verify_readback
        self._input_queue: multiprocessing.Queue = multiprocessing.Queue()
        self._output_queue: multiprocessing.Queue = multiprocessing.Queue()
        # Wall-clock (time.time()) of the worker's last byte of forward
        # progress, shared with the parent so it can tell "slow but alive"
        # from "wedged" in seconds instead of waiting out a coarse no-result
        # window. lock=False: a single double read approximately; races benign.
        self._heartbeat = multiprocessing.Value("d", 0.0, lock=False)

    @property
    def heartbeat(self) -> float:
        """Wall-clock of the worker's last forward progress (0.0 until run()).

        The parent compares ``time.time() - heartbeat`` against a stall window
        only while tasks are still outstanding — see ``_BackgroundUploadDrainer``.
        """
        try:
            return float(self._heartbeat.value)
        except Exception:
            return 0.0

    @property
    def input_queue(self) -> multiprocessing.Queue:
        return self._input_queue

    @property
    def output_queue(self) -> multiprocessing.Queue:
        return self._output_queue

    def submit(self, task: UploadTask) -> None:
        self._input_queue.put(task)

    def shutdown(self) -> None:
        """Send the drain-and-exit sentinel."""
        try:
            self._input_queue.put(_SHUTDOWN_SENTINEL)
        except (OSError, ValueError):
            pass

    def release_queue_resources(self) -> None:
        """Release the parent-side feeder threads of both queues.

        ``multiprocessing.Queue`` has a background pickling/feeder thread in
        the parent process that pushes buffered items down a pipe to the
        subprocess. After we terminate the worker (e.g. on drain timeout
        with a large backlog), those items have nowhere to go but the
        feeder thread still tries to flush them — and Python's interpreter
        shutdown will block waiting on that thread, leaving the script
        unable to exit. Calling ``close()`` + ``cancel_join_thread()`` on
        each queue says "abandon any buffered items at exit", which lets
        the interpreter terminate promptly.
        """
        for q in (self._input_queue, self._output_queue):
            try:
                q.close()
            except Exception:
                pass
            try:
                q.cancel_join_thread()
            except Exception:
                pass

    def force_stop(self, join_timeout: float = 2.0) -> None:
        """Forcibly terminate the worker and abandon its queue buffers.

        terminate() reliably reaps even a process wedged in a synchronous SMB
        I/O wait (the OS cancels the pending I/O during process teardown),
        unlike the cooperative shutdown() sentinel which a wedged worker never
        reads. Used by the drainer's watchdog and at app exit so a stuck
        upload can never block interpreter shutdown.
        """
        try:
            if self.is_alive():
                self.terminate()
                self.join(timeout=join_timeout)
        except Exception:
            pass
        self.release_queue_resources()
        try:
            self.close()
        except Exception:
            pass

    def run(self) -> None:
        log = squid.logging.get_logger("UploadWorker")
        self._heartbeat.value = time.time()
        log.info(
            f"UploadWorker started (pid={os.getpid()}) remote_root={self._target.remote_root} "
            f"pipelined={self._pipelined} threads={self._threads} "
            f"verify_readback={self._verify_readback}"
        )
        try:
            if self._pipelined:
                self._run_pipelined(log)
            else:
                self._run_sequential(log)
        except Exception:
            log.exception("UploadWorker crashed")
        log.info("UploadWorker exiting")

    def _emit_result(self, st: dict, log) -> None:
        """Build and enqueue the ``UploadResult`` for one finished task."""
        failed = st["failed"]
        result = UploadResult(
            task_id=st["task_id"],
            time_point=st["time_point"],
            region_id=st["region_id"],
            fov=st["fov"],
            success=not failed,
            uploaded_paths=st["uploaded"],
            deletable_uploaded_paths=st["deletable"],
            failed_paths=failed,
            error=None if not failed else f"{len(failed)} file(s) failed",
        )
        try:
            self._output_queue.put(result)
        except (OSError, ValueError) as e:
            log.warning(f"failed to enqueue result for task {st['task_id']}: {e}")

    def _do_upload(
        self, local_path, remote_path, stable_read, task_fields, deletable,
        manifest_lock, heartbeat, log,
    ) -> Tuple[bool, Optional[str]]:
        """Upload one file and append its manifest record. Runs on a lane
        thread in the pipelined path. Returns ``(ok, error)``."""
        t_start = time.perf_counter()
        ok, sha, n_bytes, err = upload_one_file(
            local_path,
            remote_path,
            log=log,
            max_attempts=self._max_attempts,
            initial_backoff_s=self._initial_backoff_s,
            stable_read=stable_read,
            verify_readback=self._verify_readback,
            heartbeat=heartbeat,
        )
        elapsed = time.perf_counter() - t_start
        heartbeat(0)
        if ok:
            # Serialize manifest appends across lanes: one fsynced record at a
            # time keeps the durability ordering the recovery path relies on.
            with manifest_lock:
                append_manifest_record(
                    self._manifest_path,
                    {
                        "time_point": task_fields[0],
                        "region_id": task_fields[1],
                        "fov": task_fields[2],
                        "local_path": local_path,
                        "remote_path": remote_path,
                        "sha256": sha,
                        "bytes": n_bytes,
                        "elapsed_s": round(elapsed, 3),
                        "verified_utc": datetime.now(timezone.utc).isoformat(),
                        "deletable": deletable,
                    },
                )
        else:
            log.error(
                f"giving up after {self._max_attempts} attempts: "
                f"{local_path} -> {remote_path} ({err})"
            )
        return ok, err

    def _run_pipelined(self, log) -> None:
        """Process tasks with ``threads`` concurrent copy/verify lanes.

        Each file is one lane job (``upload_one_file`` + manifest append).
        Per-task results are aggregated as lane jobs complete and emitted the
        moment a task's last file finishes — so results keep flowing (no
        head-of-line blocking on a slow FOV) and, on a full-duplex link, one
        file's read-back verify overlaps another's upload.
        """
        manifest_lock = threading.Lock()
        state_lock = threading.Lock()
        pending: dict = {}                       # task_id -> aggregation state
        inflight = threading.Semaphore(self._max_inflight_files)
        executor = ThreadPoolExecutor(
            max_workers=self._threads, thread_name_prefix="upload-lane"
        )

        def touch(_n: int = 0) -> None:
            self._heartbeat.value = time.time()

        def on_done(task_id, local_path, deletable, fut) -> None:
            # release() in finally so a bug here can never leak a slot and
            # deadlock the main loop on inflight.acquire().
            try:
                try:
                    ok, err = fut.result()
                except Exception as e:
                    ok, err = False, f"{type(e).__name__}: {e}"
                touch()
                finished = None
                with state_lock:
                    st = pending.get(task_id)
                    if st is not None:
                        if ok:
                            st["uploaded"].append(local_path)
                            if deletable:
                                st["deletable"].append(local_path)
                        else:
                            st["failed"].append((local_path, err or "unknown error"))
                        st["remaining"] -= 1
                        if st["remaining"] == 0:
                            finished = pending.pop(task_id)
                if finished is not None:
                    self._emit_result(finished, log)
            finally:
                inflight.release()

        try:
            while True:
                try:
                    item = self._input_queue.get()
                except (OSError, EOFError) as e:
                    log.warning(f"input queue closed: {e}")
                    break
                if item == _SHUTDOWN_SENTINEL:
                    log.info("shutdown sentinel received; finishing in-flight uploads")
                    break
                touch()
                task: UploadTask = item
                base = {
                    "task_id": task.task_id,
                    "time_point": task.time_point,
                    "region_id": task.region_id,
                    "fov": task.fov,
                    "uploaded": [],
                    "deletable": [],
                    "failed": [],
                }
                if not task.files:
                    self._emit_result(base, log)
                    continue
                base["remaining"] = len(task.files)
                with state_lock:
                    pending[task.task_id] = base
                task_fields = (task.time_point, task.region_id, task.fov)
                for local_path, remote_path in task.files:
                    inflight.acquire()
                    stable_read = local_path in task.stable_read_paths
                    deletable = local_path in task.deletable_local_paths
                    fut = executor.submit(
                        self._do_upload,
                        local_path, remote_path, stable_read, task_fields,
                        deletable, manifest_lock, touch, log,
                    )
                    fut.add_done_callback(
                        lambda f, tid=task.task_id, lp=local_path, dl=deletable: on_done(
                            tid, lp, dl, f
                        )
                    )
        finally:
            # Let every submitted lane job finish (their callbacks emit results
            # and release inflight slots), then flush any straggler tasks.
            executor.shutdown(wait=True)
            with state_lock:
                leftover = list(pending.values())
                pending.clear()
            for st in leftover:
                self._emit_result(st, log)

    def _run_sequential(self, log) -> None:
        """Legacy strictly-sequential path (UPLOAD_PIPELINED=False) — one task,
        one file at a time. Kept as an instant rollback for the pipeline."""
        def touch(_n: int = 0) -> None:
            self._heartbeat.value = time.time()

        while True:
            try:
                item = self._input_queue.get()
            except (OSError, EOFError) as e:
                log.warning(f"input queue closed: {e}")
                break
            if item == _SHUTDOWN_SENTINEL:
                log.info("shutdown sentinel received; exiting")
                break
            touch()
            self._process_task(item, log, touch)

    def _process_task(self, task: UploadTask, log, heartbeat=None) -> None:
        uploaded: List[str] = []
        deletable_uploaded: List[str] = []
        failed: List[Tuple[str, str]] = []
        for local_path, remote_path in task.files:
            t_start = time.perf_counter()
            stable_read = local_path in task.stable_read_paths
            ok, sha, n_bytes, err = upload_one_file(
                local_path,
                remote_path,
                log=log,
                max_attempts=self._max_attempts,
                initial_backoff_s=self._initial_backoff_s,
                stable_read=stable_read,
                verify_readback=self._verify_readback,
                heartbeat=heartbeat,
            )
            elapsed = time.perf_counter() - t_start
            if heartbeat is not None:
                heartbeat(0)
            if ok:
                uploaded.append(local_path)
                if local_path in task.deletable_local_paths:
                    deletable_uploaded.append(local_path)
                append_manifest_record(
                    self._manifest_path,
                    {
                        "time_point": task.time_point,
                        "region_id": task.region_id,
                        "fov": task.fov,
                        "local_path": local_path,
                        "remote_path": remote_path,
                        "sha256": sha,
                        "bytes": n_bytes,
                        "elapsed_s": round(elapsed, 3),
                        "verified_utc": datetime.now(timezone.utc).isoformat(),
                        "deletable": local_path in task.deletable_local_paths,
                    },
                )
            else:
                failed.append((local_path, err or "unknown error"))
                log.error(
                    f"giving up after {self._max_attempts} attempts: "
                    f"{local_path} -> {remote_path} ({err})"
                )

        self._emit_result(
            {
                "task_id": task.task_id,
                "time_point": task.time_point,
                "region_id": task.region_id,
                "fov": task.fov,
                "uploaded": uploaded,
                "deletable": deletable_uploaded,
                "failed": failed,
            },
            log,
        )


def drain_output_queue_nonblocking(
    out_queue: multiprocessing.Queue,
) -> List[UploadResult]:
    """Pull every immediately-available ``UploadResult`` from the queue."""
    results: List[UploadResult] = []
    while True:
        try:
            results.append(out_queue.get_nowait())
        except queue.Empty:
            break
    return results


def remote_root_reachable(remote_root: str, timeout_s: float = 5.0) -> bool:
    """Best-effort: is ``remote_root`` a reachable directory within timeout_s?

    The ``os.path.isdir`` probe runs in a daemon thread so a wedged SMB mount
    cannot block the caller — if the probe itself does not return in time we
    treat the share as unreachable (False) rather than hanging on it. Used to
    fail an end-of-run drain fast instead of waiting out the stall window when
    the share is simply gone.
    """
    if not remote_root:
        return False
    result = {"ok": False, "done": False}

    def _probe():
        try:
            result["ok"] = os.path.isdir(remote_root)
        except Exception:
            result["ok"] = False
        finally:
            result["done"] = True

    t = threading.Thread(target=_probe, daemon=True, name="smb-reach-probe")
    t.start()
    t.join(timeout_s)
    return bool(result["done"] and result["ok"])
