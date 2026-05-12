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
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Set, Tuple

import squid.logging


# Block size for streaming copy + hash. 4 MiB balances syscall overhead against
# memory residency for parallel uploads.
COPY_CHUNK_BYTES = 4 * 1024 * 1024

# Retry budget for transient SMB failures. Backoff: 1, 2, 4, 8, 16 seconds.
MAX_UPLOAD_ATTEMPTS = 5
INITIAL_BACKOFF_S = 1.0

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


def _sha256_of_file(path: str) -> str:
    """Compute sha256 of ``path`` by streaming."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(COPY_CHUNK_BYTES)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _stream_copy_with_hash(src: str, dest: str) -> Tuple[str, int]:
    """Copy ``src`` to ``dest`` while hashing the source stream.

    Returns ``(sha256_hex, bytes_written)``. Caller is responsible for any
    pre-existing destination cleanup (we open dest with ``"wb"``).
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
    return h.hexdigest(), bytes_written


def upload_one_file(
    local_path: str,
    remote_path: str,
    *,
    log: "squid.logging.SquidLogger",
    max_attempts: int = MAX_UPLOAD_ATTEMPTS,
    initial_backoff_s: float = INITIAL_BACKOFF_S,
    stable_read: bool = False,
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
    then never touched again (``<level>/c/<t>/0/0/0/0`` after the barrier
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
                src_hash, n_bytes = _stream_copy_with_hash(local_path, part_path)
                if stable_read:
                    src_hash_post = _sha256_of_file(local_path)
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
                dest_hash = _sha256_of_file(part_path)
                if src_hash != dest_hash:
                    last_error = (
                        f"sha256 mismatch after copy: src={src_hash} dest={dest_hash}"
                    )
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
    ):
        super().__init__()
        # Non-daemon: we want explicit drain on shutdown, not silent kill.
        self.daemon = False
        self._target = target
        self._manifest_path = manifest_path
        self._max_attempts = max_attempts
        self._initial_backoff_s = initial_backoff_s
        self._input_queue: multiprocessing.Queue = multiprocessing.Queue()
        self._output_queue: multiprocessing.Queue = multiprocessing.Queue()

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

    def run(self) -> None:
        log = squid.logging.get_logger("UploadWorker")
        log.info(
            f"UploadWorker started (pid={os.getpid()}) remote_root={self._target.remote_root}"
        )

        while True:
            try:
                item = self._input_queue.get()
            except (OSError, EOFError) as e:
                log.warning(f"input queue closed: {e}")
                break

            if item == _SHUTDOWN_SENTINEL:
                log.info("shutdown sentinel received; exiting")
                break

            task: UploadTask = item
            self._process_task(task, log)

        log.info("UploadWorker exiting")

    def _process_task(self, task: UploadTask, log) -> None:
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
            )
            elapsed = time.perf_counter() - t_start
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

        result = UploadResult(
            task_id=task.task_id,
            time_point=task.time_point,
            region_id=task.region_id,
            fov=task.fov,
            success=not failed,
            uploaded_paths=uploaded,
            deletable_uploaded_paths=deletable_uploaded,
            failed_paths=failed,
            error=None if not failed else f"{len(failed)} file(s) failed",
        )
        try:
            self._output_queue.put(result)
        except (OSError, ValueError) as e:
            log.warning(f"failed to enqueue result for task {task.task_id}: {e}")


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
