# Streaming OME-Zarr to a Network Drive

For very long timelapse acquisitions (multi-day, multi-TB), the local disk on
the acquisition workstation can fill before the run finishes. This page
documents the live-upload pipeline that mirrors each timepoint to a mounted
SMB share as soon as it is written, verifies the remote copy with sha256,
and reclaims local disk space.

The feature works only with `FILE_SAVING_OPTION = ZARR_V3`. Other formats
write data in shapes that are not safe to copy incrementally.

## When and how it kicks in

1. **Per-acquisition setting.** A new triple of fields on
   `AcquisitionParameters` carries the upload config alongside
   `file_saving_option`:
   - `zarr_upload_enabled` — master switch.
   - `zarr_upload_remote_root` — UNC (`\\server\share\dest`) or POSIX
     (`/Volumes/share/dest`) path that the OS can write to.
   - `zarr_upload_delete_after_verify` — when true, local shard files are
     deleted in batches at the end of every timepoint, once every shard for
     that timepoint has been sha256-verified on the remote.

2. **UI entry point.** When `check_space_available_with_error_dialog` finds
   that the planned acquisition exceeds local free space and ZARR_V3 is the
   selected format, the disk-space dialog now offers an
   *"Enable streaming and start"* button. Pick a remote path, confirm the
   delete policy, and the acquisition continues with streaming on. The
   selected path is cached at `cache/last_streaming_path.txt` so the next
   run starts pre-filled.

3. **Headless / scripted use.** Call
   `MultiPointController.set_zarr_upload_target(enabled=True,
   remote_root="...", delete_after_verify=True)` before `run_acquisition()`.
   The values are snapshotted into `AcquisitionParameters` by
   `build_params()`.

## Architecture

```
multi_point_worker (main proc)
    │  after acquire_at_position(t, region, fov) returns:
    │
    ▼  dispatch FlushAndStageUploadJob(t, region, fov, output_path)
JobRunner subprocess (one FIFO queue per job class)
    │  the barrier runs after every preceding SaveZarrJob for the same (t, fov):
    │    writer.wait_for_pending()        ← drains TensorStore futures
    │    paths = writer.shard_paths_for_timepoint(t)
    │    upload_queue.put(UploadTask(...))
    │  returns BarrierResult(task_id, t, fov, ...) on the output queue
    │
    ▼
UploadWorker subprocess (separate Process, non-daemon)
    │  per file: stream-copy local→`<remote>.part` with running sha256,
    │            re-hash dest, os.replace(<.part>, final), append manifest
    │            (jsonl + fsync). Retry with exponential backoff on OSError.
    │  per task: push UploadResult(success, uploaded_paths, ...) → output queue
    │
    ▼
multi_point_worker (main proc)
    │  drains UploadResults each pass through _summarize_runner_outputs
    │  per-timepoint tally: when every FOV for t has reported success AND
    │  delete_after_verify is on, batched-delete every uploaded_path for t,
    │  then prune the now-empty `c/<t>/...` shard subtrees.
```

The writer subprocess never blocks on network I/O: the upload worker is its
own process with its own queue. When the network is unavailable, the queue
grows and deletions defer; acquisition keeps imaging.

## Concurrent-write safety

The metadata files are uploaded on every barrier *while the writer is still
active*. Two of them can be racing the writer at the moment we read:

- **`frame_times/c/0/0/0`** — every ``record_frame_time`` call from the
  JobRunner rewrites this single chunk (read-modify-write of the whole
  ``(T, C, Z)`` chunk to update one cell). Hit rate is on the order of one
  rewrite per frame; the upload read takes 10–100 ms; overlap probability
  during a typical run is ~10%.
- **Per-FOV `zarr.json`** — static between ``initialize()`` and
  ``finalize()``, but ``finalize()`` rewrites it with
  ``_squid.acquisition_complete = True``. The last live barrier may race
  this rewrite.

A torn read could put garbage timestamps or a broken JSON document on the
remote until the next barrier corrects it. Two safeguards keep the remote
authoritative:

1. **Stable-read with retry.** ``upload_one_file(..., stable_read=True)``
   hashes the source while copying *and* re-hashes the source after the
   copy. If the two hashes disagree, the source was being written during
   our read — the copy is therefore torn, and the upload is retried.
   ``FlushAndStageUploadJob.run()`` marks every metadata path as
   ``stable_read``; shard files (no concurrent writes possible after the
   barrier's ``wait_for_pending()``) are uploaded with ``stable_read=False``
   to avoid the extra hash.
2. **Post-finalize metadata resync.** After the JobRunner subprocess exits
   (which is when ``finalize_all_writers()`` runs in the subprocess and
   rewrites every FOV's ``zarr.json`` with ``acquisition_complete = True``),
   ``_drain_upload_worker_on_shutdown`` enqueues one final metadata-only
   upload per FOV. This pass reads files that no writer can be touching, so
   the upload is guaranteed clean. The remote ends up reflecting the
   finalized local state even if every intermediate barrier upload of
   ``frame_times`` had been torn.

In the manifest, each record carries:
- ``"deletable": true|false`` — whether the file was eligible for local
  deletion after verify.
- A ``stable-read mismatch`` warning is logged for any attempt that
  detected a concurrent rewrite.

## Deletion safety invariant

The pipeline distinguishes between two classes of files. **Only the first
class is ever deleted locally** — even when `--delete-after-verify` is set:

| Class | Files | Lifecycle | Deletable after verify? |
|---|---|---|---|
| Per-timepoint shards | `<level>/c/<t>/0/0/0/0` | Written once during `(t, fov)` acquisition, never touched again | **Yes** |
| Shared metadata | group `zarr.json`, per-level `zarr.json`, `frame_times/zarr.json`, `frame_times/c/0/0/0` | Group + level `zarr.json` rewritten at finalize; `frame_times/c/0/0/0` rewritten by every `record_frame_time` call (one cell per call) | **No** |

The split is enforced at the upload-pipeline boundary: `UploadTask` carries
an explicit `deletable_local_paths: Set[str]` whitelist;
`UploadResult.deletable_uploaded_paths` is the strict subset of verified
uploads that the caller is allowed to delete. The live pipeline's
`_maybe_batched_delete` and the backfill's `delete_verified_locals` both
iterate only that subset. There is no code path through which a metadata
file can be deleted while uploads are in flight.

Shared metadata is still **uploaded** on every barrier, so the remote tree
stays continuously readable as a valid OME-NGFF — but the local copies
remain in place until the writer has finalized, which is what the running
acquisition needs.

## File-level guarantees

Per shard file:
1. Stream-copy local → `<remote>.part`, computing sha256 of the source bytes.
2. Re-read `<remote>.part` and compute its sha256.
3. If they match, `os.replace(<.part>, <final>)` — atomic on Windows and
   POSIX SMB.
4. On `OSError` or sha256 mismatch, retry with backoff 1s, 2s, 4s, 8s, 16s
   (5 attempts). On final failure, leave the `.part` file in place, push
   `UploadResult(success=False)`, and **never** delete the local copy.

Per acquisition:
- A JSON-lines manifest is appended at
  `{experiment_dir}/upload_manifest.jsonl`, one record per successfully
  verified shard, fsynced before the next record:
  ```json
  {"time_point":3,"region_id":"A1","fov":0,"local_path":"...","remote_path":"...","sha256":"...","bytes":1342177280,"elapsed_s":7.412,"verified_utc":"2026-..."}
  ```
- The shard ordering inside each `(t, fov)` task is: group `zarr.json`, then
  every pyramid level's `zarr.json` + shard file, then `frame_times` array
  metadata + chunk. Metadata files are small; re-uploading them every
  timepoint keeps the remote tree continuously readable.

## Backfill: applying upload to an existing dataset

`software/scripts/zarr_backfill_upload.py` exposes the same primitives as
the live pipeline. Use it to:
- Stream a completed acquisition to the network after the fact.
- Catch up an acquisition that was started without streaming enabled.
- Smoke-test the upload pipeline against a known-good dataset before
  trusting a live run.

```
python scripts/zarr_backfill_upload.py /path/to/experiment_dir \
    --remote "\\server\share\dest_root" \
    [--delete-after-verify] [--in-progress] [--dry-run]
```

- `--in-progress` — safe to run alongside an active acquisition writer.
  Skips the highest-numbered timepoint per FOV (it may still be receiving
  writes) and the FOV `zarr.json` files (the writer rewrites them at
  finalize).
- `--follow` — stay open after the initial pass and rescan the experiment
  every `--poll-interval` seconds (default 30 s). New timepoints are
  uploaded as they land. The script keeps treating the run as in-progress
  (skips the highest timepoint per FOV) until every FOV's `zarr.json`
  shows `_squid.acquisition_complete = true` (which `ZarrWriter.finalize()`
  sets at the end of the live run); at that point one final non-in-progress
  pass picks up the last timepoint plus a clean post-finalize metadata
  snapshot, the drain finishes, and the script exits. Exits early after
  `--max-idle` seconds of no progress (default 600; 0 = forever).

  The "skipping in-flight t=N" log line is deduplicated per
  `(region, fov, t)` tuple, so each FOV-timepoint skip is logged exactly
  once. When the writer advances to a new timepoint, you'll see one fresh
  INFO line per FOV — and silence in between.

  Every `max(60s, 2 × poll-interval)`, the script reports the local
  archive's total size and the signed delta since the last report, e.g.
  `local archive: 421.3 GB (-12.4 GB over 60s, -211.6 MB/s; shrinking
  — uploads are reclaiming space)`. Negative deltas mean uploads are
  outpacing the writer; positive deltas mean the writer is ahead.

### Termination

Both `--in-progress` (one-shot) and `--follow` now use the same orderly
cleanup path: `try/except (KeyboardInterrupt, SystemExit) / finally`. On
Ctrl-C or SIGTERM the script logs the signal, opportunistically drains
any results the UploadWorker already produced (up to 60 s), sends the
shutdown sentinel, joins the worker (up to 60 s more), and terminates the
subprocess if it didn't exit on its own. A final local-size report is
printed so the operator sees the net effect of the partial run. The
shutdown path is itself signal-safe — a second Ctrl-C during the drain
logs and skips ahead to the join rather than re-raising. File-level
atomicity (`.part`+`os.replace`) and manifest fsync mean that every
upload visible on the remote at exit time is verified and durable; any
in-flight `.part` files are auto-cleared on the next run's first retry.
- The backfill manifest is `upload_manifest_backfill.jsonl` so it never
  collides with a concurrent live `upload_manifest.jsonl`.

### Layout compatibility

The backfill script assumes the **current Squid writer's layout** (5D
arrays of shape `(T, C, Z, Y, X)`, outer chunk grid `(1, C, Z, Y, X)`,
zarr-v3 `default` chunk-key encoding, so each timepoint occupies exactly
one shard file at `c/<t>/0/0/0/0`). Before this layout became the only
mode (commit `5d6b34b2` "Enable zarr writing"), the writer supported
conditional layouts that vary by compression mode:

| Mode | Outer chunk grid | Shard file path |
|---|---|---|
| `BALANCED` / `BEST` (current default, sharded) | `(1, C, Z, Y, X)` | `c/<t>/0/0/0/0` ✓ |
| `FAST` / `NONE` (no sharding) | `(1, 1, 1, Y, X)` | `c/<t>/<c>/<z>/0/0` ✗ |
| Older `BALANCED` (per-z-level) | `(1, C, 1, Y, X)` | `c/<t>/0/<z>/0/0` ✗ |
| Older 6D wellplate | `(1, 1, 1, 1, Y, X)` | `c/<fov>/<t>/<c>/<z>/0/0` ✗ |

To prevent silent data loss against older datasets, the script reads the
level-0 `zarr.json` of every FOV before submitting any uploads and aborts
with a clear error if the outer chunk grid is not `(1, C, Z, Y, X)`. The
check is re-run every iteration in `--follow` mode so a newly-appearing
FOV with a mismatched layout (e.g. someone pointed it at the wrong
directory) still triggers the same abort. The live upload pipeline is
not affected by this — it is tightly coupled to the current writer and
cannot run against older outputs by construction.

## Caveats and current limitations

- **Only ZARR_V3.** Streaming for OME-TIFF / multi-page TIFF / individual
  PNGs is not wired up; the same approach is feasible but the shard-sealing
  signal is harder to derive.
- **No throttling.** The user chose "continue and accumulate locally" for
  network outages. If uploads fall behind for hours, local disk usage will
  trend back upward and a partial timepoint may force the run to stop.
- **Delete is per-timepoint, not per-FOV.** A single FOV upload failure
  defers deletion of the whole timepoint's local data until the failure
  resolves.
- **Aborts.** On user abort, in-flight uploads continue draining for up to
  30 minutes. Re-running the backfill against the same experiment dir is
  the canonical recovery path if the drain timed out.

## Key files

- `software/control/core/zarr_upload.py` — `UploadTarget`, `UploadTask`,
  `UploadResult`, `UploadWorker`, `upload_one_file`, manifest helpers.
- `software/control/core/job_processing.py` — `FlushAndStageUploadJob`,
  `BarrierResult`, `JobRunner(upload_target=..., upload_input_queue=...)`.
- `software/control/core/zarr_writer.py` — `shard_paths_for_timepoint`.
- `software/control/core/multi_point_worker.py` — UploadWorker lifecycle,
  barrier dispatch, batched-delete, drain on shutdown.
- `software/control/core/multi_point_utils.py` — new fields on
  `AcquisitionParameters`.
- `software/control/core/multi_point_controller.py` —
  `set_zarr_upload_target`.
- `software/gui/widgets/common.py` — `prompt_enable_network_streaming` UI.
- `software/scripts/zarr_backfill_upload.py` — backfill CLI.
