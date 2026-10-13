# Persistent Upload Manager: proposed design

Status: first-release implementation is present in `control/core/upload_manager.py`,
`control/core/upload_manager_client.py`, and `upload_manager.py`. Hardware and
real Windows SMB qualification remain release acceptance work.

Confirmed requirement: successive operators restart Squid under the same
Windows login. Surviving Windows sign-out is outside the requested scope.

The Upload Manager should be a separate, detached Qt application that owns
upload scheduling, verification, deletion, durable state, and a small status
window. Start it when uploading is first enabled, not during Squid shutdown.
The microscope GUI and the backfill CLI become clients of that manager.

## Existing behavior and integration points

- `control/core/multi_point_worker.py` starts `UploadWorker` and later hands it
  to `_BackgroundUploadDrainer`, which is a thread inside the main application.
- `MultiPointController.close()` and an `atexit` handler call
  `terminate_all_upload_drainers()`. Simply removing these calls could make
  Python wait indefinitely for its non-daemon children on exit.
- `FlushAndStageUploadJob` submits immutable shards after local writes finish.
  Writer finalization stages partial stacks and triggers final metadata resync.
- `scripts/zarr_backfill_upload.py` discovers files, resumes matching historical
  uploads, and deletes verified tasks. Its log rate measures verified bytes,
  including reverified historical files; that is not network upload throughput.
- The JSONL upload manifests persist verification history but do not provide
  a durable pending inventory or confirmed deletion history.

## Process ownership

One Upload Manager instance per Windows login owns the queue for all datasets.
Launch using the configured Python environment and an absolute entry-point
path through `QProcess.startDetached`; redirect logs independently of Squid's
console. Use the existing PyQt5/qtpy stack. The manager imports no camera,
stage, or microscope controllers.

The manager owns its own `UploadWorker` subprocesses. Workers do blocking SMB
I/O; the manager's GUI remains responsive if a worker stalls. The main Squid
process owns neither those workers nor their multiprocessing queues.

A user-scoped local IPC endpoint provides versioned JSON requests:
`register_dataset`, `submit_batch`, `seal_dataset`, `get_status`, `show_window`,
`pause`, and `resume`. Use `QLocalServer`/`QLocalSocket` with user-only access.
Use a single-instance lock and startup handshake; concurrent Squid launches
must connect to the same manager. A connection loss does not cancel a job.

Create workers inside the manager, never pickle workers from a different
Squid process. Record protocol and application versions. Reject incompatible
new requests while allowing existing work to finish; deploy manager code as
an immutable version while its workers are active. This avoids mixing old
parent instances with newly imported worker code.

## Durable state and safe ownership

Store SQLite state locally under `%LOCALAPPDATA%/Squid/UploadManager`, not on
the remote share. The manager is the only database writer. Keep the existing
append-only per-experiment JSONL manifests for verification audit and legacy
recovery; add explicit deletion events and transfer/reverification counters.

Persist dataset identity, local root, exact remote experiment root, deletion
policy, producer state, and each submitted file's relative path, size,
generation, task membership, checksum, and processing state. A new destination
creates a separate job; it cannot silently retarget existing deletion authority.

Commit queued work before acknowledging submission. Producers retain a small
local outbox until acknowledgement, so a manager restart cannot lose a batch
between writer completion and queue insertion. Submissions have stable IDs
and are idempotent.

File states are `pending`, `uploading`, `verified`, `delete_pending`, `deleted`,
or `failed`. Dataset states include `receiving`, `draining`, `waiting_for_share`,
`paused`, `needs_attention`, and `complete`.

Only one owner may write/delete for a local dataset at a time. Use an OS-backed
ownership lock and a persisted job identity; PID alone is insufficient. The
backfill CLI submits to the manager when it owns a dataset. Legacy standalone
uploaders must stop before adopting their work. Do not run a recovery scanner
and the live uploader concurrently over the same files.

Reuse existing temporary-copy, verification, atomic rename, and exact remote
destination checks. Historical records remain usable after revalidating local
and remote bytes. Persist verification before deletion eligibility. Preserve
the current policies: core uploads reclaim individually verified shards;
backfill waits for its whole FOV/timepoint task to succeed. Never delete shared
metadata or timestamps. Persist delete intent before unlink and completion
afterwards; reconcile interrupted deletion on restart against verified state.

Deduplicate by dataset, destination, and file generation. Progress must not
increase twice for retries, duplicate submissions, or reverified history.
Metadata rewrites are separate generations and final metadata is required
before declaring a sealed dataset complete.

## Closing and reopening Squid

1. Normal upload submission already goes to the independent manager.
2. Closing Squid ends any acquisition through its normal abort/finalize path.
   Stop image production, finish local writes, publish final shard batches and
   metadata, then send a durable completion/aborted seal. Remote transfers do
   not delay closure; local writes and durable submission still must finish.
3. If writers cannot finalize, record `needs_attention`; do not mark unfinished
   shards immutable or report the dataset complete. Already acknowledged files
   can continue uploading. A disconnected producer is not proof of finalization.
4. Disconnect the client, release hardware, and close the microscope GUI.
   Its shutdown handlers must never terminate manager-owned workers. Keep old
   termination paths only for any remaining legacy workers during rollout.
5. Leave the Upload Manager window visible. A fresh Squid instance reconnects,
   shows the existing upload count, and can start a new acquisition immediately
   after normal hardware initialization.

On manager failure, preserve the queue and local files. A restarted manager
reconciles in-flight work with manifests and resumes. The next Squid launch or
the manager shortcut can restart it; automatic recovery while Squid is closed
would require a separate watchdog and is not promised by the first version.

## Status window

Use one compact window with a row per acquisition and an aggregate footer.
Show acquisition name/start time, local and remote folders, state, uploaded
percentage, deleted percentage, upload rate, and approximate completion time.
Selected-row details show byte counts, file counts, retry reason, and last
successful verification. Refresh counters about once per second without
rescanning directories.

| Display | Definition |
| --- | --- |
| Uploaded | Unique verified bytes / inventoried upload bytes; include successful history without double counting. |
| Deleted | Confirmed reclaimed bytes / total inventoried bytes eligible for deletion; metadata excluded. Show `Disabled` when deletion is off. |
| Upload rate | Bytes actually written remotely per second, smoothed over about 30 seconds; revalidation reads do not count as uploads. |
| Verification rate | Optional detail showing readback/revalidation throughput separately. |
| Completion estimate | Remaining work divided by recent end-to-end processing throughput, including verification and cleanup; show approximate clock time plus duration. |

Freeze the denominator after dataset sealing/final discovery. While acquisition
continues, label progress `of data written so far`, show backlog bytes, and
withhold an acquisition-wide completion time. Do not show a finite estimate
while paused, stalled, still indexing, or lacking a stable processing rate.
When upload is 100% but cleanup remains, display `Cleaning up`.

Import legacy manifests together with local discovery. Historical local files
that are absent are not automatically counted as deleted: they may have been
moved. Show unknown historical reclamation separately until it can be proven.

Controls: Pause/Resume, Retry failed, Open local folder, Open remote folder,
and View log. Closing the status window hides it to the tray and keeps uploads
running; an explicit Stop uploads and exit action checkpoints and stops work.
Completed rows remain available in history. Squid offers Show uploads to
reopen the window.

Start with a global limit of two concurrent transfer lanes across all datasets,
configurable in the manager. Schedule fairly so adding acquisitions does not
multiply SMB concurrency. Expose a rate cap and source-volume free-space
indicator to help the next acquisition coexist with older uploads.

## Implementation sequence and acceptance checks

1. Extract shared backfill discovery/cleanup into a reusable engine; add the
   durable inventory, progress events, deletion receipts, and recovery tests.
2. Build the detached manager, local IPC client, ownership locks, standalone
   window/tray, and CLI submission. Verify close/reopen with local destinations.
3. Route core staging/finalization to the manager and remove main-process
   ownership for this path. Update close/restart messaging and queue accounting.
4. Exercise real Windows SMB behavior, disconnects, process termination, and
   two successive acquisitions before enabling by default.

Required tests: close Squid mid-transfer and reopen while the same job advances;
metadata/tail shards survive orderly abort/finalize; duplicate launches and
duplicate submissions never create duplicate owners; blocked SMB does not freeze
either GUI; network reconnect resumes; manager death between copy, verify,
manifest append, unlink, and receipt commit remains recoverable; destination
mismatch prevents deletion; revised progress never double-counts retries or
history; GUI close only hides the window; a new acquisition and older backlog
share the configured global transfer budget.

Scope of the first release is application close/restart under one Windows
login. Windows sign-out or reboot is not uninterrupted execution. Persisted
jobs can resume after relaunch. If transfers must survive sign-out or separate
Windows accounts, host the engine in a Windows service with a per-session
status GUI and explicit network credentials/UNC destinations; mapped drive
letters are tied to logon sessions.

References: [Qt 5 detached process lifecycle](https://doc.qt.io/archives/qt-5.15/qprocess.html#startDetached),
[Microsoft: services and redirected drives](https://learn.microsoft.com/en-us/windows/win32/services/services-and-redirected-drives).
