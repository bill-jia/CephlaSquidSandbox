# Acquisition and upload pipeline diagnostics

These measurements distinguish capture backpressure caused by reconstruction,
local writing, flushes, or synchronous upload submission from a backlog in the
separate Upload Manager. They observe existing work; they do not change queue
limits, transfer concurrency, verification, or deletion rules.

## Where to look

Restart **Squid and the separate Upload Manager** to load the instrumentation.
Restarting Squid alone does not update an already-running manager. Finish or
stop the current acquisition before restarting; use the manager's normal exit
and relaunch flow for its durable queue.

| Output | Location | Cadence |
| --- | --- | --- |
| Acquisition summaries and throttle durations | Main Squid log, normally `C:\Microscope_Data\logs\main_hcs.log`; search `[PIPELINE]` | About every 60 seconds while capturing, at throttle entry/exit, capture finish, and writer shutdown |
| Acquisition details | `pipeline_diagnostics\acquisition-<pid>-<unique>.jsonl` beside the main log | One JSON record per summary event |
| Upload summaries | `%LOCALAPPDATA%\Squid\UploadManager\upload_manager.log` (or the manager's custom state directory); search `[PIPELINE]` | About every 60 seconds while its scheduler runs, plus shutdown |
| Upload details | `%LOCALAPPDATA%\Squid\UploadManager\pipeline_diagnostics\uploads-<pid>-<unique>.jsonl` (or `pipeline_diagnostics` beside a custom manager database) | Each transfer result, local deletion attempt, and summary |

Both processes announce the exact detail filename in their log. Detailed files
contain metadata and timings, not images. Files accumulate across sessions and
are not automatically rotated. If a detail file cannot be written, the full
record falls back to that process's log with a warning.

## Acquisition summaries

Each worker reports queued jobs, the oldest queued job's age, its current job
and stage (with elapsed stage time), the stage consuming most time in the last
reporting interval, and lifetime mean/maximum queue wait. Raw-save workers use
`job_run` when no narrower stage was recorded. Throttle exit reports the limit
reached (`jobs`, `bytes`, or both), elapsed wait, and whether capacity returned
or the wait timed out.
`capacity_changed` means jobs completed between the initial throttle check and
the diagnostic snapshot, so that snapshot no longer identifies the reached limit.

The JSON records include all workers' pending and queued counts, queued image
MiB, active stage, age of the oldest pending/queued job, and stage totals:

| Stage | Work measured |
| --- | --- |
| `queue_wait` | Dispatch to worker execution, including serialization, transport, deserialization, and waiting behind earlier jobs |
| `job_run` | Entire job execution; includes nested stages below |
| `reconstruction` | Postprocessing computation |
| `derived_write` | Writing/submitting derived images to the writer |
| `zarr_flush` | Upload barrier waiting for pending Zarr writes |
| `upload_submit` | Synchronous batch submission to the persistent Upload Manager |
| `preview_prepare` | Preparing the postprocessed preview image, excluding GUI rendering |
| `finalize` | Zarr and OME-TIFF writer finalization during worker shutdown |

For each stage, cumulative count, total, maximum, mean, and exception count
are retained; interval count and total are also recorded. A finalizer returning
an unsuccessful status is still reported by its existing error log; the timing
exception count only counts raised exceptions. `completed_sequence` is a FIFO
tracking identifier, not a completed-job count (failed dispatches can leave gaps).

Diagnostic queue bytes are **not total process RAM**. They exclude retained postprocessing
stacks, reconstruction buffers, writer caches, and other allocations. Pending
includes running jobs; queued excludes them. These diagnostic snapshots are
best-effort observations and can differ briefly from the authoritative
backpressure counters as jobs finish.
The authoritative byte counter also holds incomplete postprocessing input groups
until computation begins, so it can exceed the diagnostic queued-image bytes.

## Upload summaries and details

Each summary reports finished transfers (including failures), verified MB and
MB/s, deleted MB, occupied/available lanes, oldest active transfer age, pending
files/bytes, ready files, retry waits, and the largest measured stage. The JSON
backlog additionally groups counts/bytes by dataset and file state, exposing
paused datasets and datasets needing attention. Backlog reads query the local
inventory only; they do not probe the share.

Per-transfer details separate source existence checks, stale remote temporary
file cleanup, remote directory creation, copying plus source hashing, optional
source rechecking, remote readback hashing (or size checking when configured),
remote rename, retry backoff, and configured rate-limit sleeps. Historical
manifest revalidation has its own timing. Repeated attempts accumulate stage
counts and time; exception counts indicate raised exceptions, while the final
transfer result and existing log describe hash mismatches and other failures.

`worker_elapsed_s` covers the transfer function. `observed_elapsed_s` spans
process launch to result collection. Their difference, `outside_worker_s`,
includes Windows process startup/imports and result-queue polling delays; it
does not isolate those costs individually. `result_bookkeeping_s` covers
joining the child, manifest updates, and database recording. Deletion records
measure the local removal/check and its database/manifest bookkeeping.

Interpret verified MB/s as end-to-end completion throughput, not network link
speed. `copied_bytes` is the existing successful-copy accounting: it does not
count partial failed writes, repeated attempts, or verification read traffic.
Historical revalidation can verify bytes without copying them again. Deleted
bytes reflect inventory reclamation, including reconciliation when a file is
already absent; they are not a measurement of physical free-space change.

## Reading the evidence

- A growing postprocessing queue with long `reconstruction` time points to
  computation; long `derived_write` or `zarr_flush` time points to writer work
  or waiting for pending writes.
- Long `upload_submit` times show that communication with the manager is
  delaying the acquisition worker. An upload backlog alone does not prove this.
- Occupied upload lanes and long copy/readback times identify transfer or
  verification costs. Large `outside_worker_s` highlights process startup or
  result collection overhead. Long remote cleanup/rename times expose remote
  filesystem overhead that a copy-only timer would miss.
- Pending files with no ready files are explained by retry waits and dataset
  states in the detail record; they need not mean the link is saturated.

Stage durations are wall-clock measurements, not CPU/GPU utilization. Copying
and hashing remain combined. Nested timings overlap: `job_run` contains its
sub-stages, and rate-limit sleeps are included in copy/verification times.
Concurrent transfer lanes also overlap. Do not sum all stage times as elapsed
acquisition time. Interval totals are charged when a stage or transfer finishes,
so a long operation can contribute more than the latest reporting window.

Acquisition periodic reports run at frame boundaries, so a blocked acquisition
thread can delay them. Active worker stage/age helps identify ongoing work at a
snapshot, but upload sub-stage timings arrive only when that transfer returns.
A stuck live transfer has an increasing lane age, not a live sub-stage trace.
These measurements narrow the bottleneck; they cannot alone distinguish OS
scheduling, local disk contention, SMB/server latency, and physical link limits.

For comparison, retain the main log, manager log, and both diagnostic JSONL
files for the same acquisition. The acquisition path, process IDs, dataset/file
IDs, and timestamps connect the records.
