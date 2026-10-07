# Multipoint Acquisition Data Saving

How image data flows from camera to disk during multipoint acquisitions, covering all supported file formats and the job-based writing architecture.

## Architecture Overview

Data saving uses a three-layer pipeline that decouples image capture from disk I/O:

```
MultiPointWorker (main thread)
  │  camera trigger → frame callback → create Job
  │
  ▼
JobRunner (separate subprocess, via multiprocessing.Queue)
  │  dequeue Job → job.run() → write to disk
  │
  ▼
Format-specific writer (tifffile, TensorStore, etc.)
```

**Key files:**
- `control/core/multi_point_worker.py` — acquisition loop, job dispatch
- `control/core/job_processing.py` — Job base class, format-specific jobs, JobRunner subprocess
- `control/core/zarr_writer.py` — Zarr v3 writer (TensorStore backend)
- `control/core/utils_ome_tiff_writer.py` — per-region multi-series OME-TIFF layout, metadata and XML
- `control/core/backpressure.py` — backpressure throttling (see [acquisition-backpressure.md](development/acquisition-backpressure.md))

## Acquisition Loop Order

The multipoint worker iterates dimensions in this nesting order (innermost = fastest varying):

```
Time points
  └─ Regions (scan areas or wells)
     └─ FOVs (fields of view within a region)
        └─ Z-slices
           └─ Channels (ObservationStates)
              └─ camera trigger → _image_callback()
```

Each camera frame triggers `_image_callback()`, which:
1. Builds a `CaptureInfo` dataclass with position (x, y, z), indices (t, c, z, fov, region), and save directory
2. Creates a `Job` wrapping `CaptureInfo` + the image array
3. Calls `JobRunner.dispatch(job)`, which pickles the job across a `multiprocessing.Queue`

## File Format Selection

The format is set by the global config `FILE_SAVING_OPTION` in `control/_def.py`:

```python
class FileSavingOption(Enum):
    INDIVIDUAL_IMAGES = "INDIVIDUAL_IMAGES"   # One TIFF/PNG per frame (default)
    MULTI_PAGE_TIFF = "MULTI_PAGE_TIFF"       # One multi-page TIFF per FOV
    OME_TIFF = "OME_TIFF"                     # OME-TIFF stacks with full metadata
    ZARR_V3 = "ZARR_V3"                       # Zarr v3 with TensorStore
```

At acquisition start, `MultiPointWorker` selects which job class to register based on this setting (`multi_point_worker.py` lines 211-219):

| Setting | Job Class | Description |
|---------|-----------|-------------|
| `INDIVIDUAL_IMAGES` | `SaveImageJob` | One file per frame |
| `MULTI_PAGE_TIFF` | `SaveImageJob` | Appended multi-page TIFF per FOV |
| `OME_TIFF` | `SaveOMETiffJob` | One pre-allocated multi-series OME-TIFF per region (series = FOV) |
| `ZARR_V3` | `SaveZarrJob` | Zarr v3 via TensorStore |

There is no automatic format switching — the user selects the format before acquisition via Settings > Preferences > File Saving Format.

## Format Details

### Individual Images (Default)

**Job:** `SaveImageJob` in `job_processing.py`

**Output structure:**
```
{experiment}/
└── {timepoint:04d}/
    ├── {region}_{fov:04d}_{z:04d}_{channel_name}.tiff
    ├── {region}_{fov:04d}_{z:04d}_{channel_name}.tiff
    ├── coordinates.csv
    └── frame_acquisition_times.csv
```

Each frame is saved as a separate TIFF (or PNG, depending on `IMAGE_FORMAT`). Metadata is encoded in the filename. This is the simplest format and works with any downstream tool, but produces many small files.

A `metadata.json` sidecar is written in each timepoint directory with acquisition-wide context:

```json
{
  "channel_names": ["DAPI", "GFP"],
  "num_time_points": 10,
  "num_z_levels": 5,
  "num_channels": 2,
  "pixel_size_um": 0.5,
  "z_step_um": 1.0,
  "time_increment_s": 60.0,
  "file_saving_option": "INDIVIDUAL_IMAGES"
}
```

This makes per-timepoint folders self-describing without parsing filenames.

### Multi-Page TIFF

**Job:** `SaveImageJob` (same class, different code path)

**Output structure:**
```
{experiment}/
└── {timepoint:04d}/
    └── {region}_{fov:04d}_stack.tiff     # All z/channel frames appended
```

Frames are appended to a single TIFF file per FOV. Each page includes:
- `ImageDescription`: JSON with z_level, channel, position, time
- `PageName` (TIFF tag 285): channel name

### OME-TIFF

**Job:** `SaveOMETiffJob` in `job_processing.py`

**Output structure — one multi-series file per region:**
```
{experiment}/
├── ome_tiff/
│   ├── {region}.ome.tiff                 # dense layout
│   ├── {region}__{array_key}.ome.tiff    # ragged cycle layout (one per state)
│   ├── {region}__{label}_{output}.ome.tiff  # derived — one per postprocess output
│   └── .{stem}.meta.json                 # in-progress bookkeeping (removed on finalize)
├── acquisition_times.csv                 # per-frame timestamps, all timepoints
└── acquired_positions.csv                # measured stage positions, all timepoints
```

Nothing is written per timepoint, so — like ZARR_V3 — **no `{exp}/{timepoint}/`
folder is created** (see [Per-Frame Metadata](#per-frame-metadata)). The
`time_point` column of the two root CSVs carries what the folder name used to.

A region file holds **one OME `Image` / TIFF series per FOV**: series *i* is FOV
*i*, axes `TZCYX`. The separator before `array_key` is a **double** underscore
because region ids are user-editable names that may contain single underscores;
readers take the region from the first series' `Image Name` (`"{region}:{fov}"`)
and treat whatever follows `__` in the filename as the array key.

**Write mechanism:**
1. On the region's first frame, **every** FOV series is pre-allocated in one
   `tifffile.TiffWriter(..., ome=True)` pass — one `write(data=None, shape=...)`
   per FOV, each carrying its own `Name`, channel names, physical sizes and
   `TimeIncrement`. The FOV count comes from `AcquisitionInfo.fovs_per_region`
   (built from the scan coordinates), and the BigTIFF flag is computed up front
   from the total size of all series (a classic TIFF cannot be upgraded later).
2. Each frame writes its plane through `tifffile.memmap(path, series=fov, mode="r+")`:
   `stack[t, z, c, :, :] = image`. The runner keeps the open memmap and the
   bookkeeping in memory for the FOV it is filling, so a frame costs one plane
   write — no reopen, no per-frame JSON rewrite.
3. A JSON sidecar next to the data (`ome_tiff/.{stem}.meta.json`, with a
   `.lock`) records per-series plane metadata. It is flushed when the FOV changes
   and at finalisation, i.e. at most once per FOV visit.
4. **Finalisation** rewrites the OME-XML comment with each `Image`'s
   `AcquisitionDate` and full `Plane` list, then deletes the sidecar. It happens
   as soon as a region's expected plane count is reached, and — crucially — for
   every still-open region file when the `JobRunner` subprocess exits, so an
   **aborted** acquisition still gets its positions and timestamps. Only a hard
   kill of the whole process can leave a sidecar behind; because it sits beside
   the data it is discoverable (and harmless).

**Metadata includes:** per-`Image` channel names, pixel size (X/Y/Z), time
increment, per-plane positions (X/Y in mm, Z in µm) and per-plane `DeltaT`
relative to that FOV's first frame.

**One acquisition can produce several region files.** The file a frame lands in
is chosen by its `CaptureInfo.array_key`, exactly as the zarr store is:

| `array_key` | Region file | Produced by |
|---|---|---|
| `None` | `{region}.ome.tiff` — dense `TZCYX` | plain channel selection, or a dense cycle plan |
| `{state}` / `{state}_refz` | `{region}__{array_key}.ome.tiff` — single-channel | a ragged [acquisition cycle](acquisition-cycles.md) |
| `{label}_{output}` | `{region}__{label}_{output}.ome.tiff` — single-channel, derived | [online postprocessing](online-postprocessing.md) |

Every file takes its `T`/`Z`/`C` from the frame's own `save_t_size` /
`save_z_size` / `save_c_size` when those are set, falling back to the
acquisition-wide `AcquisitionInfo` totals otherwise. That is what lets a
reference-z-only state, or a postprocess output that collapses a z-stack to one
plane, allocate `Z = 1` inside a run whose `NZ` is 11.

The registry of open region files is process-local, so the rule is **exactly one
process writes a given file**. Raw frames are handled by a single
single-threaded `SaveOMETiffJob` runner; derived postprocess outputs are written
by inline `SaveOMETiffJob`s inside the *separate* `PostprocessJob` runner, which
is safe because the two processes can never target the same path (a
postprocessed step saves no raw frames, so no raw `array_key` equals a derived
`{label}_{output}`). Both runners call `SaveOMETiffJob.finalize_all_writers()` on
their way out — the hook is not gated on job class. The sidecar lock is kept only
as a cheap guard on the JSON itself.

### Zarr V3

**Job:** `SaveZarrJob` in `job_processing.py`  
**Writer:** `ZarrWriter` in `zarr_writer.py` (TensorStore backend)

See [zarr-v3-format.md](zarr-v3-format.md) for full details on output structure, metadata, and configuration.

**Write mechanism:**
1. On first frame for a given FOV/region, a `ZarrWriter` is lazily initialized with a `ZarrAcquisitionConfig`
2. TensorStore creates the zarr v3 dataset. The inner chunk is one plane `(1, 1, 1, Y, X)`; the shard (on-disk file unit) defaults to one z-slice `(1, C, 1, Y, X)` (`ZARR_SHARD_PER_Z=True`), or the legacy whole-FOV `(1, C, Z, Y, X)` when set False
3. All pyramid levels (`/1`..`/5`) are also opened up-front as sibling arrays, and their entries are registered in `multiscales.datasets` at this point
4. OME-NGFF 0.5 metadata is written to `zarr.json`, including per-level `scale` and per-FOV `translation` transforms and a `_squid.manifest_path` pointer back to `acquisition.yaml`
5. In per-z mode (default) each plane is buffered until its z-slice has all channels, then the whole `(1, C, 1, Y, X)` shard (all channels + all pyramid levels for that z) is written in a single non-blocking TensorStore pass — one commit per shard, no read-modify-write. A slice still missing channels is **never** committed mid-run: the whole-shard write would store fill value for the absent channels, and a later commit of the same cell would then erase whatever the first one stored (TensorStore omits fill-value chunks). It stays buffered until its last channel lands — see [zarr-network-streaming.md](zarr-network-streaming.md#ordering-a-shard-cell-is-committed-exactly-once). (Legacy mode writes each plane as its own chunk into the big per-FOV shard and cascades pyramids per frame.) Futures are pipelined and drained when more than 32 are in flight
6. Per-frame timestamps are written directly into a `(T, C, Z)` float64 zarr array named `frame_times` alongside the resolution levels
7. At acquisition end, each writer `finalize()` just flushes pending writes and flips `_squid.acquisition_complete = True` — there is no read-back or post-hoc pyramid pass

**Key configuration:**
- Compression: NONE, FAST (LZ4), BALANCED (Zstd-3, default), BEST (Zstd-9)
- Shard granularity: `ZARR_SHARD_PER_Z` (default True = one shard per z-slice, ~30× faster writeback; False = legacy one shard per FOV)
- Layout: HCS (wellplate) or per-FOV (flexible). Both are OME-NGFF v0.5 5D.

**One acquisition can produce several stores.** The store a frame lands in is
chosen by its `CaptureInfo.array_key`:

| `array_key` | Store | Produced by |
|---|---|---|
| `None` | dense — `plate.ome.zarr` / `zarr/{region}/` | plain channel selection, or a dense cycle plan |
| `{state}` / `{state}_refz` | ragged — one single-channel store per (state, z-mode) | a ragged [acquisition cycle](acquisition-cycles.md) |
| `{label}_{output}` | derived — one store per postprocess output | [online postprocessing](online-postprocessing.md) |

Whether those stores use the HCS plate hierarchy or the flat `zarr/` tree is a
separate axis: wellplate scans always use HCS, and flexible scans do too unless
`FLEXIBLE_MULTIPOINT_AS_HCS` is turned off — their regions are mapped to
synthetic wells with the names kept as `_squid` annotations
([flexible-to-hcs-conversion.md](flexible-to-hcs-conversion.md)).

`SaveZarrJob` keys its process-local writer dict on the resolved level-0 path,
so each store gets its own `ZarrWriter`, its own HCS plate/well metadata, its
own pyramid and its own `frame_times`. See
[zarr-v3-format.md](zarr-v3-format.md#store-inventory) for exact paths, shapes,
and the channel/sequence coordinate rules.

**Writeback status in the GUI:** capture ending (`acquisition_finished`) is *not* the same as data being on disk. The per-FOV writers finalize in a background thread; the controller fires `data_writing_complete` once they finish, and the multipoint widgets keep the Start button disabled with the progress bar showing "Finalizing…" until then (with a safety timeout so it can never get stuck). Only after "✓ Data writing complete" is it safe to move/copy the dataset.

## Live Viewing (NDViewer) per Saving Mode

The embedded [NDViewer tab](ndviewer-tab.md) is fed by `QtMultiPointController`
(`gui/gui_hcs/qt_controllers.py`). Which push API it uses is decided at
acquisition start from `AcquisitionParameters.file_saving_option`, and recorded
in `NDViewerMode`. The rule behind the table: **a frame may only be announced to
the viewer once its pixels are readable**, which for the out-of-process writers
means "when the subprocess says so", not "when the camera returned it".

| Saving mode | `NDViewerMode` | Start signal | Per-frame signal | Fires when |
|---|---|---|---|---|
| `INDIVIDUAL_IMAGES` | `TIFF` | `ndviewer_start_acquisition` | `ndviewer_register_image(t, fov, z, ch, filepath)` | on the GUI thread, right after the synchronous per-frame write |
| `MULTI_PAGE_TIFF` | `TIFF` | same | same | ⚠️ same, but the registered path is wrong — see below |
| `OME_TIFF` | `OME_TIFF` | `ndviewer_start_ome_tiff_acquisition` | `ndviewer_notify_ome_tiff_frame(t, fov, z, ch)` | when `SaveOMETiffJob` reports the plane written |
| `ZARR_V3` | `ZARR_5D` | `ndviewer_start_zarr_acquisition` | `ndviewer_notify_zarr_frame(t, fov, z, ch, region)` | when `SaveZarrJob` reports the frame written |

**Frame-written path (OME_TIFF and ZARR_V3).** Both save jobs return a
`FrameWriteResult(fov, time_point, z_index, channel_name, region_idx)` from
`run()`. The `JobRunner` subprocess puts it on the output queue; the worker's
`_summarize_job_result()` (`multi_point_worker.py`) turns it into
`callbacks.signal_frame_written(...)`; the Qt controller's
`_signal_frame_written_fn` converts the region-local FOV index to the flat one
and emits the signal for the active mode. `fov`/`time_point` are the *save*
coordinates (`save_t_index` when a cycle sets one), so they address the array the
viewer reads.

**OME_TIFF specifics.** There is no per-frame file, so nothing can be registered
by path. At start the controller computes, per flat FOV, the region file and the
series index inside it via `utils_ome_tiff_writer.ome_region_file_path()` — the
same helper `ome_output_path()` (the writer) is built on, so the predicted path
cannot drift from the written one. Only the dense `{region}.ome.tiff` path is
announced; the ragged `{region}__{array_key}` files are discovered by the viewer
as they appear, matching each channel by name.

The viewer reads planes through a read-only `tifffile.memmap` of the series
rather than through `TiffFile`, because the writer keeps its own memmap of the
same file open for a whole FOV visit and a buffered reader can return bytes from
before the write (measured: a small plane reads stale; a 512 KB plane does not).
Flushing the writer's memmap does **not** fix that — the stale copy lives in the
reader's own buffer — so no per-frame flush was added.

**⚠️ `MULTI_PAGE_TIFF` is still wrong.** It goes down the `TIFF` branch, which
registers `{save_directory}/{file_id}_{channel}.tiff`, but the job actually
appends into `{save_directory}/{region}_{fov}_stack.tiff`. The viewer is handed
paths that do not exist and shows nothing. Fixing it needs a page-indexed reader
(the appended pages have no stable (t, z, c) address), so it is left as is.

## Job Processing Subprocess

All save jobs run in a single `JobRunner` subprocess (`multiprocessing.Process`). This design:

- **Decouples** the acquisition thread from disk I/O — the camera keeps triggering while writes happen in the background
- **Isolates** I/O failures from the acquisition loop
- **Serializes** writes within the subprocess — one job at a time, sequentially

### Job Lifecycle

```
Main Process                          Worker Subprocess (JobRunner)
─────────────                         ───────────────────────────────
_image_callback()
  ├─ create Job(CaptureInfo, image)
  ├─ JobRunner.dispatch(job)
  │   ├─ inject metadata ──────────▶  input_queue.get()
  │   │   (AcquisitionInfo for          │
  │   │    OME-TIFF, ZarrWriterInfo     ├─ job.run()
  │   │    for Zarr)                    │   └─ write to disk
  │   ├─ increment backpressure         │
  │   └─ put on input_queue             ├─ decrement backpressure
  │                                     └─ put result on output_queue
  ├─ backpressure check
  │   (block if queue too full)
  └─ next camera trigger
```

### Backpressure

When the camera produces frames faster than the disk can write them, the backpressure system prevents unbounded memory growth. Before each camera trigger, the worker checks:

- **Pending job count** vs `ACQUISITION_MAX_PENDING_JOBS`
- **Pending bytes** vs `ACQUISITION_MAX_PENDING_MB`

If either limit is exceeded, acquisition pauses until the subprocess drains enough jobs. See [acquisition-backpressure.md](development/acquisition-backpressure.md) for details.

### Shutdown and finalize durability

At acquisition end the worker calls `_finish_jobs()`, which drains queued job
results and then shuts each `JobRunner` down in a **background daemon thread** so
the controller can return and the next acquisition can start immediately.

The subprocess's exit path runs `SaveZarrJob.finalize_all_writers()`, and that is
where any **remaining shard commits are flushed**. With the default per-z layout
this is cheap — each z-slice shard was already committed during the stream, so
finalize only awaits the last one or two. In the legacy per-FOV layout (or with
upload disabled and a deep stack) the single `(1, C, Z, Y, X)` shard — routinely
~1 GB — is written here as one temp-file (`*.__lock`) + atomic rename, which can
take tens of seconds.

Two rules keep that commit from being killed mid-write (which would leave a
partial shard whose last z-slices read back as zeros **only at level 0**, plus a
stray `*.__lock` file and `_squid.acquisition_complete = False`):

1. `_finish_jobs()` never `kill()`s the runner when its drain budget expires — a
   still-pending job may be a flush/commit in progress. It logs a warning and
   lets the graceful stop sentinel (honored only *between* jobs) finish the
   in-flight job and finalize.
2. The background shutdown is given `JOB_RUNNER_FINALIZE_TIMEOUT_S` (10 min), not
   the leftover of the short drain budget, before `JobRunner.shutdown()` resorts
   to `terminate()`. Because it runs in a daemon thread, this generous budget
   does not block the UI. Reaching `terminate()` now logs an error — it means
   finalize genuinely wedged and data may be incomplete.

## Per-Frame Metadata

Every format writes a per-frame timing CSV with the same column schema:

| Column | Description |
|--------|-------------|
| `time_point` | Time point index |
| `region_id` | Region/well identifier |
| `fov` | Field of view index |
| `z_level` | Z-slice index |
| `channel` | Channel name |
| `channel_index` | Channel index |
| `filename` | Relative path to saved file |
| `unix_time_s` | Unix timestamp of capture |
| `utc_iso` | UTC ISO 8601 timestamp |

Layout differs by save mode:

- **`INDIVIDUAL_IMAGES`, `MULTI_PAGE_TIFF`**: one CSV per timepoint at `{exp}/{timepoint}/frame_acquisition_times.csv`, alongside the image files. `filename` is relative to that folder.
- **`OME_TIFF`, `ZARR_V3`**: a single consolidated CSV at `{exp}/acquisition_times.csv`. The `time_point` column distinguishes rows and `filename` is relative to the experiment root (`ome_tiff/A1.ome.tiff`, `plate.ome.zarr/A/1/0/0`). Neither mode writes images into the per-timepoint folder — OME-TIFF has one multi-series file per region under `ome_tiff/`, Zarr has its own per-FOV trees under `plate.ome.zarr/` (the default for both wellplate and flexible scans) or `zarr/` (when `FLEXIBLE_MULTIPOINT_AS_HCS = False`) — so the folder would be empty and is *not created* at all unless downsampled views or laser-AF characterization need it (`MultiPointWorker._needs_per_timepoint_folder`).

### Planned vs. Measured Positions

Two different tables, and they are not copies of each other:

- **Planned** — `{exp}/coordinates.csv`, written once by `MultiPointController` before the run: the FOV grid it *intends* to visit (`region`, `x (mm)`, `y (mm)`, `z (mm)`). Written for every saving mode.
- **Measured** — where the stage actually was, one row per FOV **and z level**, with a wall-clock `time` (`region`, `fov`, `z_level`, `x (mm)`, `y (mm)`, `z (um)`, `time`, plus `z_piezo (um)` when the piezo is in use). Autofocus, the focus map and piezo motion all move the measured z away from the planned one, so this is the table to use for stitching or for reconstructing the real focus surface.

The measured table lands where the mode has room for it:

| Saving mode | Measured positions |
|---|---|
| `INDIVIDUAL_IMAGES`, `MULTI_PAGE_TIFF` | `{exp}/{timepoint}/coordinates.csv` — beside the images that folder already holds, one file per timepoint |
| `OME_TIFF`, `ZARR_V3` | `{exp}/acquired_positions.csv` — one file for the whole run, appended once per timepoint (header written once) with a leading `time_point` column |

### Region Names

`region_id` is a well id (`A1`) for wellplate scans, and on the Flexible Multipoint tab an auto-assigned `R{n}` that the user may rename to anything meaningful (see [the user guide](user_guides/multipoint.md#naming-regions)). It is not just a label — it is a path component (`zarr/{region_id}/`, `plate.ome.zarr/{row}/{col}/`) and the image filename prefix, so it must be a unique, filesystem-safe string. `control.core.scan_coordinates.validate_region_name` defines the rules; the GUI enforces them on every edit/import, and `MultiPointController.validate_acquisition_settings` re-checks the whole set at acquisition start so headless and SiLA entry points are covered too.

Renaming goes through `ScanCoordinates.rename_region`, which rekeys **every** per-region map in place. Order matters: dict insertion order is the scan order, and a stale `region_generation_params` key would make the acquisition-start re-tile (`regenerate_for_fov`) resurrect the region under its old name and scan it twice.

Note that `ScanCoordinates` is *derived* state for flexible scans, not durable state: it is cleared and rebuilt wholesale by `FlexibleMultiPointWidget.update_fov_positions` (on any tile-geometry change) and by `MainWindow.onTabChanged`. Anything per-region that the user authored must therefore be owned by the widget and re-applied after each rebuild — that is what `_region_laser_af_references` / `_restore_region_references` do for the per-region laser-AF targets. Adding a new per-region user-authored value means giving it the same treatment.

### Zarr-Embedded Timestamps

For Zarr V3 format, per-frame timestamps are also written as a `frame_times` zarr array inside each FOV group (shape `(T, C, Z)`, dtype `float64`, Unix seconds). This makes the zarr store fully self-describing — downstream consumers can read timestamps with the same stack of tools that reads the image data. The root-level `acquisition_times.csv` covers the same ground in human-readable form.

## Fast Acquisition (Separate Path)

For continuous fast acquisition (not multipoint), a separate `FastAcquisitionWriter` (`control/core/fast_acquisition_writer.py`) uses a two-stage approach:

1. **During capture**: raw bytes are streamed to `frames.raw` with per-frame metadata in `frame_metadata.jsonl` (minimal CPU overhead)
2. **Post-capture**: raw data is converted to the final format (TIFF stack, BigTIFF, Zarr, or HDF5)

When writes are deferred until stop *and* the target is BigTIFF/Zarr/HDF5, the writer skips `frames.raw` entirely and decodes the RAM ring straight into the output file (direct-encode, ~1× disk I/O instead of ~3×).

Its **Zarr output is a different format from the multipoint one** — zarr v2, no OME metadata, no pyramid, no sharding:

```
{run}/
├── frames.zarr/
│   ├── frames/        # (N, Y, X), chunks (100, Y, X), blosc-lz4 clevel 5
│   ├── frame_ids/     # (N,) int64
│   └── timestamps/    # (N,) float64
├── frames/frame_metadata.jsonl
├── metadata.json                 # frame counts/drops, DIO lines, camera settings, DAQ settings
└── acquisition_metadata.yaml     # microscope/objective/illumination sidecar
```

The stream is a flat frame sequence — there is no T/C/Z structure and no channel metadata, because a fast-acquisition burst is one observation state by construction. All acquisition context lives in the two sidecars, not in the store.

This path is separate from the job-based multipoint pipeline.

## Related Documentation

- [Zarr v3 Format](zarr-v3-format.md) — detailed Zarr output structure, metadata, and reading instructions
- [Acquisition Backpressure](development/acquisition-backpressure.md) — throttling mechanism details
- [Downsampled Plate View](downsampled-plate-view.md) — overview visualization for wellplate acquisitions
- [Simulated Disk I/O](development/simulated-disk-io.md) — testing write performance without actual disk writes
- [NDViewer Tab](ndviewer-tab.md) — live viewing during acquisition
