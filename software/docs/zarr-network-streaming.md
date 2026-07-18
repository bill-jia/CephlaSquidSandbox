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
     (`/Volumes/share/dest`) path that the OS can write to. **This is the
     PARENT folder**: each acquisition is mirrored into its own subfolder
     named after the experiment
     (`<selected>/<experiment_ID>/…`), exactly like the local
     `{base_path}/{experiment_ID}` layout — so the cached path can be reused
     across runs without their files intermixing. (The worker expands the
     selection at acquisition start; `UploadTarget.remote_root` and every
     on-disk record carry the already-expanded per-experiment path. The
     backfill script's `--remote` is unchanged: it takes the exact
     destination experiment directory, which is what
     `UPLOAD_INCOMPLETE.txt` records.)
   - `zarr_upload_delete_after_verify` — when true, local shard files are
     deleted in batches at the end of every timepoint, once every shard for
     that timepoint has been sha256-verified on the remote.

2. **UI entry point — inline.** In each multipoint widget (flexible + 
   wellplate), an inline row appears immediately below the "Save format"
   dropdown when `ZARR_V3` is the selected format:

   ```
   [Save format: ZARR_V3 ▾]
   [☐ Stream to network] [\\server\share\path________] [Browse...] [☑ Delete after verify]
   ```

   The row is auto-hidden for non-ZARR_V3 formats (and the enable flag is
   forcibly cleared if you toggle away from ZARR_V3 with streaming on, so
   the worker doesn't try to spawn an upload pipeline against a non-zarr
   writer). All four child widgets stay disabled until the "Stream to
   network" master checkbox is on. Edits propagate immediately to
   `MultiPointController.set_zarr_upload_target(...)`, and the chosen path
   is cached at `cache/last_streaming_path.txt` so the next session
   pre-fills it.

   Because the flexible and wellplate tabs each have their own row bound to
   the one controller, **Start re-pushes the active tab's save-format and
   streaming state** — the run always uses what the tab that started it
   displays, never a stale edit from the other tab or a previous run.
   At Start, if streaming is enabled, the target path is probed
   (timeout-bounded, off the GUI thread); an unreachable/read-only share
   raises a dialog offering "start without streaming" or cancel, instead of
   silently burning every upload against a dead share.

3. **UI entry point — disk-space fallback.** As a safety net, if you start
   an acquisition that would exceed local free space without having
   enabled streaming in the inline row,
   `check_space_available_with_error_dialog` opens a modal that lets you
   enable streaming on the spot (write-probe + path validation, both
   timeout-bounded off the GUI thread) instead of failing. Same
   `cache/last_streaming_path.txt` pre-fill. Two rules keep this honest:
   the bypass is only offered for `Nt > 1` (space is reclaimed per verified
   *timepoint*, so a single-timepoint run must fit locally regardless), and
   when streaming with delete-after-verify is already configured the space
   check compares against a ~3-timepoint local peak instead of the full run
   size.

4. **Headless / scripted use.** Call
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
    │    paths = writer.drain_unstaged_shard_paths()  ← ALL shards written
    │                                                    since the last barrier
    │    upload_queue.put(UploadTask(...))
    │  returns BarrierResult(task_id, t, fov, ...) on the output queue
    │
    ▼
UploadWorker subprocess (separate Process, non-daemon)
    │  N concurrent lanes (ThreadPoolExecutor, UPLOAD_WORKER_THREADS, default 2)
    │  per file (one lane job): stream-copy local→`<remote>.part` with running
    │            sha256, re-hash dest, os.replace(<.part>, final), append
    │            manifest (jsonl + fsync, under a lane lock). Retry with
    │            exponential backoff on OSError; a per-chunk heartbeat is stamped
    │            so the parent can tell "slow" from "wedged".
    │  per task: results aggregate as a task's last file lands, then push
    │            UploadResult(success, uploaded_paths, ...) → output queue
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
grows and deletions defer; acquisition keeps imaging. A throttled mid-run
health check (`_maybe_warn_upload_health`, every 60 s) warns via log + Slack
when the worker died, the backlog exceeds ~100 tasks, upload tasks have
failed, or no byte has moved for the stall window — previously a mid-run
wedge was completely invisible until the end-of-run drain.

**Completion accounting is counter-based, not task-id-based.** Every producer
increments a shared `tasks_submitted` `multiprocessing.Value`
(`UploadWorker.tasks_submitted_value`) immediately before putting a task on
the input queue — the barrier does it inside the JobRunner subprocess, the
resync/root uploads via `UploadWorker.submit()`. Outstanding work is then
simply `tasks_submitted − UploadResults consumed`. This is immune to the two
failure modes that used to corrupt the old registration scheme
(`BarrierResult` task_ids matched against `UploadResult` task_ids across two
independent queues):

- an `UploadResult` arriving *before* its `BarrierResult` had been consumed
  left a phantom task_id registered forever → the drainer waited out its
  stall window on nothing, declared a healthy worker "wedged", terminated
  it, and withheld the completion marker;
- `BarrierResult`s produced during the backgrounded JobRunner shutdown were
  never consumed at all → the drainer *undercounted*, tore the worker down
  mid-backlog, and could write a **false** `RAW_DATA_UPLOADED.txt`.

The per-task_id maps still exist, but only as bookkeeping for the
per-timepoint deletion tally and the registry snapshot; a completed-task_id
set guards the late-registration race there too.

**Why concurrent lanes (and why only a few).** Each lane writes a file up
(send direction) then reads it all back to sha256-verify (receive direction).
1 GbE is full-duplex, so with ≥2 lanes one file's verify-read overlaps
another's upload-write instead of serializing after it — the read-back's cost
is largely hidden rather than doubling wall-clock. Extra lanes also hide
per-file SMB round-trip latency on the long tail of small pyramid/metadata
files. Lanes do **not** push past the link's bandwidth ceiling: for large
shards a single stream already saturates the pipe, so this is latency/duplex
hiding, not parallelism-beats-bandwidth. `UPLOAD_PIPELINED=False` restores the
strictly-sequential one-file-at-a-time path as an instant rollback.

**Verification is end-to-end and unavoidable over plain SMB.** There is no
server-side hash op on an SMB share, so confirming the stored bytes means
reading them back down — that is what the re-hash does. (`verify_readback=
False` downgrades to a remote-size check only — catches truncation, not silent
corruption — and is off by default.) Caveat: the SMB client may serve the
read-back from its own write cache, so the check is as strong as the client's
cache coherency, not a guaranteed fresh fetch from the server.

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
2. **Post-finalize metadata resync.** The background drainer first **waits
   for every JobRunner shutdown to complete** (an event set by
   ``_finish_jobs``'s writeback-complete watcher, capped at the 600 s
   finalize budget + 60 s) — that is the moment ``finalize_all_writers()``
   has rewritten every FOV's ``zarr.json`` with
   ``acquisition_complete = True``. Only then does it enqueue the resync,
   so the pass reads files no writer can be touching and the remote is
   guaranteed to end up reflecting the *finalized* local state. (It used to
   fire at drain start, racing a finalize that could still be minutes away —
   the remote could permanently miss ``acquisition_complete``.) While
   waiting, the drainer keeps consuming late ``BarrierResult``s from the
   runners' still-open output queues. The resync enumerates by **filesystem
   walk**: every ``zarr.json`` under the experiment dir (per-FOV group +
   level metadata, plate roots, per-well groups — for the dense plate,
   ragged per-state plates, ``*_refz`` plates and derived postprocess plates
   alike) plus every ``frame_times`` chunk, batched ~100 files per task, all
   non-deletable; then the experiment-root files
   (``_enqueue_experiment_root_resync``).

### Staging every shard a FOV visit writes

The barrier does **not** key on the scan ``time_point``. A dense or ragged
[acquisition cycle](acquisition-cycles.md) visit folds many frames into a
*contiguous block* of array-``t`` indices (``T = Nt × frames/visit``), so one
visit seals several shards. ``ZarrWriter`` records every array-``t`` it writes
in ``_unstaged_t_indices`` (in ``write_frame``); ``drain_unstaged_shard_paths``
returns the shard files for **all** of them across every pyramid level.
Because callers run ``wait_for_pending()`` first, a level file absent at
drain time is *permanently* absent (TensorStore elides all-fill-value
shards — e.g. a pyramid level that downsampled to exactly zero), so the
drain stages whatever level files exist and clears the cell as long as its
level-0 shard is present; only a cell with no level-0 file stays pending
(its write may land before a later barrier, or the frame was all-fill and
no file will ever exist). Demanding all levels used to strand real level-0
data in the pending set forever. Keying on the scan ``time_point`` instead
would stage one shard per visit and silently skip the rest.

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
1. Stream-copy local → `<remote>.part-<uuid>`, computing sha256 of the source
   bytes. The temp name is unique **per call**: shared metadata files recur in
   every barrier task and the lanes run across task boundaries, so a fixed
   `.part` name let two lanes truncate/delete each other's temp file. Lanes
   additionally hold a per-remote-path lock, so one destination is never
   written by two lanes at once.
2. Re-read the temp file and compute its sha256.
3. If they match, `os.replace(<.part-uuid>, <final>)` — atomic on Windows and
   POSIX SMB.
4. On `OSError` or sha256 mismatch, retry with backoff 1s, 2s, 4s, 8s, 16s
   (5 attempts). On final failure, best-effort remove the temp file, push
   `UploadResult(success=False)`, and **never** delete the local copy.

   The retry loop only fires when a syscall *returns* an error. A genuine SMB
   *hang* (a dead/half-open session where `write()` never returns) bypasses it
   entirely — so the parent watches the worker's per-chunk heartbeat and
   `terminate()`s a wedged worker rather than waiting forever (see the
   stall-window section). A terminated worker leaves only `.part` files
   (never a half-written final file, thanks to the atomic rename), so the
   remote is never corrupted and the backfill script can finish the job.

Per acquisition:
- A JSON-lines manifest is appended at
  `{experiment_dir}/upload_manifest.jsonl`, one record per successfully
  verified shard, fsynced before the next record:
  ```json
  {"time_point":3,"region_id":"A1","fov":0,"local_path":"...","remote_path":"...","sha256":"...","bytes":1342177280,"elapsed_s":7.412,"verified_utc":"2026-..."}
  ```
  With concurrent lanes the appends are serialized under a per-worker lock, so
  the "one fsynced record at a time" durability ordering is preserved.
- The files inside each `(t, fov)` task are: group `zarr.json`, then every
  pyramid level's `zarr.json` + shard file, then `frame_times` array metadata
  + chunk. With concurrent lanes these upload out of order, but a task's
  `UploadResult` is only emitted once **all** its files have landed, so the
  per-timepoint deletion tally is unaffected. Metadata files are small;
  re-uploading them every timepoint keeps the remote tree continuously
  readable.

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

The script discovers FOV groups in three layouts: non-HCS
`zarr/<region>/fov_*.ome.zarr`; HCS `<plate>.ome.zarr/<row>/<col>/<fov>` for
**any** top-level `*.ome.zarr` whose root `zarr.json` carries an `ome.plate`
attribute (a ragged [acquisition cycle](acquisition-cycles.md) writes one
plate per imaged state — `{state}.ome.zarr` — so there can be several; the
legacy single `plate.ome.zarr` is just the one-plate case); and a top-level
`*.ome.zarr` that is itself a multiscales group. Region IDs are namespaced by
plate stem so wells from different per-state plates don't collide. Plate-root
and per-well group `zarr.json` are uploaded alongside the per-FOV metadata so
the remote opens as a valid HCS plate.

To prevent silent data loss against older datasets, the script reads the
level-0 `zarr.json` of every FOV before submitting any uploads and aborts
with a clear error if the outer chunk grid is not `(1, C, Z, Y, X)`. The
check is re-run every iteration in `--follow` mode so a newly-appearing
FOV with a mismatched layout (e.g. someone pointed it at the wrong
directory) still triggers the same abort. The live upload pipeline is
not affected by this — it is tightly coupled to the current writer and
cannot run against older outputs by construction.

## End-of-acquisition cleanup

Both the live pipeline (via `_BackgroundUploadDrainer`) and the standalone
backfill script perform these steps once every shard + every metadata
file has been verified on the remote:

1. **Empty per-timepoint shard directories are pruned.** `c/<t>/0/0/0/`,
   `c/<t>/0/0/`, … `c/<t>/` are removed bottom-up after each timepoint's
   `delete_after_verify` pass. The parent `c/`, the level `<n>/`
   (containing `zarr.json`), the FOV group dir, and the `frame_times/`
   subtree all stay so the directory remains a valid OME-NGFF reader.
2. **Every sidecar is mirrored, recursively.** The final pass
   (`_enqueue_sidecar_resync`, via `collect_sidecar_files`) walks the whole
   experiment dir and uploads every non-zarr file — `acquisition.yaml`, run
   logs, config dumps, coordinate CSVs, AND sidecar subdirectories such as
   the per-timepoint folders (downsampled well views, laser-AF debug
   images). Zarr plate subtrees (`*.ome.zarr`) are pruned from the walk —
   their shards streamed live and their metadata has its own resync — so
   nothing is uploaded twice. `upload_manifest.jsonl`,
   `upload_manifest_backfill.jsonl`, `RAW_DATA_UPLOADED.txt`, and
   `UPLOAD_INCOMPLETE.txt` are excluded at the root (upload-pipeline
   outputs). Net effect: the remote experiment folder is a complete mirror
   of the local one.
3. **A `RAW_DATA_UPLOADED.txt` marker** is dropped into the local
   experiment dir when *and only when* the drain finished with zero failed
   tasks, zero outstanding tasks, **and the worker exited cooperatively with
   exit code 0** — the marker is written *after* worker teardown, never
   before, so a worker that had to be terminated can no longer leave a
   marker claiming success. The marker carries the remote root URL, an
   ISO-8601 UTC timestamp, and a pointer to the manifest; its deletion note
   is accurate to the run (delete-after-verify off → says the local copy is
   intact; N files survived deletion due to Windows file locks → says so).

4. **An `UPLOAD_INCOMPLETE.txt` record** is written whenever uploads are
   abandoned — drain stall, share unreachable, drainer crash, app close /
   `atexit` force-stop. It carries the reason, the outstanding/failed task
   counts, the remote root, and the exact backfill command to finish the
   job. This is the only record that survives an app restart (the drainer
   registry is in-memory), so an abandoned upload is no longer silent. A
   later clean completion removes it.

A re-run that picks up where an earlier (incomplete) run stopped will
write the marker once *its* run is clean. The marker is overwritten
atomically each time, so accidental partial-success markers are
self-correcting.

## Concurrency across acquisitions

Each acquisition gets its **own** UploadWorker subprocess. When the
acquisition finishes (or the user aborts it), the worker's drain is
handed off to a daemon thread (`_BackgroundUploadDrainer` in
`multi_point_worker.py`) and the acquisition controller is freed
immediately — the user can start a new run without waiting for the
previous one's uploads to finish. Several drainers can be active
concurrently, one per past-but-not-yet-finished acquisition. The module
exposes `active_upload_drainer_count()` and
`active_upload_drainer_summary()`; the multipoint Start buttons surface
these in a pre-start "N previous acquisitions still uploading" warning
(`check_system_load_and_pending_uploads_with_dialog`, alongside high
CPU/RAM and tight-disk checks) so the operator can choose not to pile a
new run onto a still-draining one.

Each acquisition writes to a unique `experiment_path`, so concurrent
drainers' local files, remote paths, and manifests don't collide.
Bandwidth contention on a shared SMB share is the only real overlap;
that's an accepted cost of the concurrent design.

**Clean exit, even with a wedged worker.** The UploadWorker is a
non-daemon `multiprocessing.Process`, which Python joins at interpreter
exit — so a worker stuck in a synchronous SMB I/O wait would otherwise
hang the whole app on close. `MultiPointController.close()` calls
`terminate_all_upload_drainers()` (also registered as an `atexit`
handler), which `terminate()`s every active worker — the OS reaps even a
kernel-I/O-blocked process — and releases the queue feeder threads. Three
details make this hold in every window:

- the atexit ordering is forced by an explicit `import multiprocessing.util`
  immediately before our `atexit.register` — plain `import multiprocessing`
  does *not* register multiprocessing's child-join `_exit_function`, so
  without the forced import the LIFO ordering was luck-of-first-Queue-
  construction and our handler could run *after* the hang it exists to
  prevent;
- workers are registered in a module-level live-worker list **at spawn
  time** (`register_live_upload_worker`), not only when the end-of-run
  drainer takes ownership — a setup crash or app close mid-acquisition can
  no longer strand an unreachable worker;
- abandoning uploads this way writes `UPLOAD_INCOMPLETE.txt` next to the
  data, and the GUI warns before both app exit and restart when drainers
  are still active ("N uploads still pending — exit anyway?").

Uploads abandoned this way are recoverable with the backfill script.

### JobRunner shutdown correctness

Three related fixes keep the runner honest at end of run:

- **Queued jobs run before finalize.** The subprocess loop exit is
  sentinel-driven: after `shutdown()` it keeps draining the input queue and
  executes every job dispatched before the sentinel. The old
  `while not shutdown_event` form exited after the in-flight job and
  silently dropped the whole queued backlog — frames vanished (their pixels
  lived in the dropped queue items), upload barriers never staged, and
  `finalize_all_writers()` stamped `acquisition_complete=true` over the
  truncated data. The parent's 600 s join + `terminate()` remains the only
  hard stop.
- **Upload-queue feeder flush** (`close()` + `join_thread()`) before the
  subprocess exits, so `UploadTask`s buffered in the feeder actually reach
  the UploadWorker. With uploads enabled the *output* queue feeder is
  flushed too (the drainer is still reading BarrierResults from it);
  without uploads it is abandoned (`cancel_join_thread`) so an unread
  status item can never wedge subprocess exit.
- **No feeder join after terminate.** When the parent had to `terminate()`
  the subprocess, `shutdown()` abandons the input-queue feeder instead of
  joining it — the consumer is dead, so a feeder still holding items would
  block that join forever (this exact hang was reproducible in
  `test_shutdown_with_pending_jobs`, and in production would wedge the
  background shutdown thread and with it the "data writing complete"
  signal).

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
- **Aborts / end-of-run drain.** When an acquisition ends, the background
  drainer keeps uploading and only gives up after a **stall window**
  (`UPLOAD_DRAINER_STALL_WINDOW_S`, default 120 s) of *no forward progress* —
  not a fixed wallclock deadline. Progress is measured by the worker's
  per-chunk **heartbeat**, so a healthy worker that is merely slow (large
  backlog, slow share, one big file) keeps the idle clock near zero and is
  never abandoned. The heartbeat is stamped **only by byte movement and
  verified completions** — failing uploads do not refresh it, so a
  dead-share failure grind looks idle instead of keeping the drain alive for
  hours of guaranteed failures. When the watchdog fires (or ≥5 consecutive
  tasks fail), the drainer re-probes the remote root: share gone → abandon
  fast with an `UPLOAD_INCOMPLETE.txt` record; share reachable but no bytes
  moving → genuinely wedged worker, `terminate()`d, same record. Worker
  teardown after a clean drain is progress-aware too: the shutdown sentinel
  queues behind any remaining tasks, and the join keeps extending while
  bytes move (the old flat 5 s join could kill a worker mid-upload).
  Whenever the drain reports outstanding uploads, re-running the backfill
  against the same experiment dir is the canonical recovery path — the
  `UPLOAD_INCOMPLETE.txt` record contains the exact command.
- **Throughput knobs.** `UPLOAD_WORKER_THREADS` (default 2) sets the number of
  concurrent copy/verify lanes; `UPLOAD_PIPELINED=False` reverts to the
  strictly-sequential path; `UPLOAD_VERIFY_READBACK=False` trades end-to-end
  re-hash for a remote-size check. All live in `zarr_upload.py`.

## Key files

- `software/control/core/zarr_upload.py` — `UploadTarget`, `UploadTask`,
  `UploadResult`, `UploadWorker` (pipelined lanes + heartbeat + `force_stop`),
  `upload_one_file`, `remote_root_reachable`, manifest helpers, and the
  `UPLOAD_PIPELINED` / `UPLOAD_WORKER_THREADS` / `UPLOAD_VERIFY_READBACK` knobs.
- `software/control/core/job_processing.py` — `FlushAndStageUploadJob`,
  `BarrierResult`, `JobRunner(upload_target=..., upload_input_queue=...)`.
- `software/control/core/zarr_writer.py` — `drain_unstaged_shard_paths`
  (stages every shard written since the last barrier).
- `software/control/core/multi_point_worker.py` — UploadWorker lifecycle,
  barrier dispatch, batched-delete, `_BackgroundUploadDrainer` (heartbeat
  watchdog), `terminate_all_upload_drainers` (+ atexit), `active_upload_
  drainer_count/summary`, `UPLOAD_DRAINER_STALL_WINDOW_S`.
- `software/control/core/multi_point_utils.py` — new fields on
  `AcquisitionParameters`.
- `software/control/core/multi_point_controller.py` —
  `set_zarr_upload_target`.
- `software/gui/widgets/common.py` — `prompt_enable_network_streaming` UI and
  `check_system_load_and_pending_uploads_with_dialog` (pre-start warning).
- `software/scripts/zarr_backfill_upload.py` — backfill CLI.
