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
- `control/core/utils_ome_tiff_writer.py` — OME-TIFF metadata utilities
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
| `OME_TIFF` | `SaveOMETiffJob` | Pre-allocated 5D OME-TIFF stacks |
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

**Output structure:**
```
{experiment}/
└── ome_tiff/
    └── {region}_{fov:04d}.ome.tiff       # 5D stack (T, Z, C, Y, X)
```

**Write mechanism:**
1. On first frame for a given FOV, a pre-allocated memmap TIFF is created with the full 5D shape using `tifffile.imwrite()`
2. Subsequent frames write directly to their index via `tifffile.memmap()`: `stack[t, z, c, :, :] = image`
3. A temporary JSON metadata file (with `FileLock`) tracks which planes have been written
4. When all planes are written, the OME-XML header is finalized with full metadata (channels, pixel sizes, plane positions, timestamps)

**Metadata includes:** channel names, pixel size (X/Y/Z), time increment, per-plane positions (X, Y, Z in mm), and per-plane timestamps.

### Zarr V3

**Job:** `SaveZarrJob` in `job_processing.py`  
**Writer:** `ZarrWriter` in `zarr_writer.py` (TensorStore backend)

See [zarr-v3-format.md](zarr-v3-format.md) for full details on output structure, metadata, and configuration.

**Write mechanism:**
1. On first frame for a given FOV/region, a `ZarrWriter` is lazily initialized with a `ZarrAcquisitionConfig`
2. TensorStore creates the zarr v3 dataset with configured chunks, sharding, and compression
3. OME-NGFF 0.5 metadata is written to `zarr.json`
4. Each frame is submitted as a non-blocking TensorStore async write. Futures are pipelined and drained periodically (up to 32 in-flight) for throughput
5. Per-frame timestamps are recorded in memory alongside each write
6. At acquisition end, all writers are finalized:
   - Pending writes are flushed
   - Multiscale pyramid levels (2x, 4x downsampled) are generated and written as sibling arrays (`/1`, `/2`)
   - Per-frame timestamps are written as `frame_timestamps.json` in the zarr group
   - The `multiscales` metadata is updated with all pyramid levels
   - Completion flag is set in `_squid` metadata

**Key configuration:**
- Compression: NONE, FAST (LZ4), BALANCED (Zstd-3), BEST (Zstd-9)
- Chunks: FULL_FRAME, TILED_512, TILED_256
- Sharding: automatic based on compression level
- Modes: HCS (wellplate), per-FOV, or 6D (all FOVs in one array)

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

## Per-Frame Metadata

Every format writes a `frame_acquisition_times.csv` in each timepoint directory:

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

Additionally, `coordinates.csv` records the stage position for each FOV.

### Zarr-Embedded Timestamps

For Zarr V3 format, per-frame timestamps are also written as `frame_timestamps.json` inside each zarr group (next to `zarr.json`). This makes the zarr store fully self-describing — downstream consumers can read timestamps without locating a separate CSV. Each entry contains `t`, `c`, `z`, `unix_time_s`, `channel_name`, and optionally `fov` (for 6D mode).

## Fast Acquisition (Separate Path)

For continuous fast acquisition (not multipoint), a separate `FastAcquisitionWriter` (`control/core/fast_acquisition_writer.py`) uses a two-stage approach:

1. **During capture**: raw bytes are streamed to `frames.raw` with per-frame metadata in `frame_metadata.jsonl` (minimal CPU overhead)
2. **Post-capture**: raw data is converted to the final format (TIFF stack, Zarr, or HDF5)

This path is separate from the job-based multipoint pipeline.

## Related Documentation

- [Zarr v3 Format](zarr-v3-format.md) — detailed Zarr output structure, metadata, and reading instructions
- [Acquisition Backpressure](development/acquisition-backpressure.md) — throttling mechanism details
- [Downsampled Plate View](downsampled-plate-view.md) — overview visualization for wellplate acquisitions
- [Simulated Disk I/O](development/simulated-disk-io.md) — testing write performance without actual disk writes
- [NDViewer Tab](ndviewer-tab.md) — live viewing during acquisition
