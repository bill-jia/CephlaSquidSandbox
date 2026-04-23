# Zarr v3 Output Format

Squid writes acquisitions as OME-NGFF v0.5 Zarr v3 stores when
`FILE_SAVING_OPTION = ZARR_V3`. The layout is always 5D per FOV with plane-level
chunks and per-FOV sharding, optimized for tile-scan timelapse workloads that
feed downstream stitching / segmentation / tracking pipelines.

## At a glance

| Axis | Value |
|------|-------|
| Array shape | `(T, C, Z, Y, X)` per FOV |
| Inner chunk | `(1, 1, 1, Y, X)` — one image plane |
| Outer shard | `(1, C, Z, Y, X)` — one FOV-timepoint bundle |
| Compression | blosc-zstd clevel 3 + bitshuffle (default, `BALANCED`) |
| Pyramid | up to 5 extra levels, written inline per frame |
| Per-frame timestamps | `frame_times` zarr array (shape `T×C×Z`, float64) |
| Metadata | OME-NGFF `multiscales` + `omero` + `_squid.manifest_path` |

File count per FOV ≈ `T × (num_pyramid_levels + 1)`: shards are written once per
`(FOV, timepoint)` and never reopened.

## Output layouts

### HCS (wellplate)

When the xy layout resolves to well IDs (`A1`, `B12`, …), Squid writes the
OME-NGFF HCS plate hierarchy:

```
{experiment}/
└── plate.ome.zarr/
    ├── zarr.json              # ome.plate
    ├── A/
    │   └── 1/
    │       ├── zarr.json      # ome.well
    │       ├── 0/             # FOV 0
    │       │   ├── zarr.json  # ome.multiscales + ome.omero + _squid.manifest_path
    │       │   ├── 0/         # resolution level 0 (full resolution)
    │       │   ├── 1/         # resolution level 1 (2× down)
    │       │   ├── ...
    │       │   └── frame_times/  # (T, C, Z) float64 unix timestamps
    │       └── 1/             # FOV 1
    └── B/
        └── ...
```

### Non-HCS (flexible / large-area)

For non-well xy layouts (custom regions, single-area tile scans, etc.):

```
{experiment}/
└── zarr/
    └── {region}/
        ├── fov_0.ome.zarr/
        │   ├── zarr.json
        │   ├── 0/ .. 5/       # resolution levels
        │   └── frame_times/
        └── fov_1.ome.zarr/
```

Each FOV is its own OME-NGFF image group. Both layouts share the same per-FOV
structure below the FOV group.

## Array structure

- **Shape**: `(T, C, Z, Y, X)`.
- **Dtype**: whatever the camera produces (usually `uint16`).
- **Inner chunk** (`chunks` inside the sharding codec): `(1, 1, 1, Y, X)` — a single image plane. Enables per-`(t, c, z)` random access.
- **Outer shard** (the zarr-v3 chunk-grid chunk, containing the inner chunks): `(1, C, Z, Y, X)`. One shard per `(FOV, timepoint)`: the stitching pattern downstream reads one whole FOV per tile and only needs a single file open per tile. Acquisition writes each shard once and never reopens it.

## Compression

`ZARR_COMPRESSION` (project setting) selects the blosc preset:

| Value | Codec | Typical ratio | Typical encode |
|-------|-------|---------------|----------------|
| `NONE` | no codec | 1× | disk-bound |
| `FAST` | blosc-lz4 clevel 1, byte shuffle | ~2× | ~1 GB/s |
| `BALANCED` (default) | blosc-zstd clevel 3, bitshuffle | ~3–5× on 16-bit fluorescence | ~500 MB/s |
| `BEST` | blosc-zstd clevel 9, bitshuffle | ~5–7× | ~100 MB/s |

Sharding is always on regardless of compression; the decisive factor is just
the codec choice.

## Multiscale pyramid (streaming)

Resolution levels `/1` ... `/N` are opened at `ZarrWriter.initialize()` and
populated inline on every `write_frame(image, t, c, z)` call via a cascade of
`cv2.pyrDown`. Each level's shape is `((Y+1)//2, (X+1)//2)` relative to the
previous. Generation stops when `min(Y, X) < 128` or after 5 extra levels
(defaults on `ZarrAcquisitionConfig`).

There is **no serial post-hoc pyramid pass** — `finalize()` does not read back
or compute anything; it only flushes pending TensorStore writes and flips the
`_squid.acquisition_complete` flag.

## OME-NGFF metadata

At each FOV group's `zarr.json` (schema v0.5):

```json
{
  "zarr_format": 3,
  "node_type": "group",
  "attributes": {
    "ome": {
      "version": "0.5",
      "multiscales": [{
        "version": "0.5",
        "name": "0",
        "axes": [
          {"name": "t", "type": "time",    "unit": "second"},
          {"name": "c", "type": "channel"},
          {"name": "z", "type": "space",   "unit": "micrometer"},
          {"name": "y", "type": "space",   "unit": "micrometer"},
          {"name": "x", "type": "space",   "unit": "micrometer"}
        ],
        "datasets": [
          {
            "path": "0",
            "coordinateTransformations": [
              {"type": "scale",       "scale":       [dt_s, 1, dz_um, px_um, px_um]},
              {"type": "translation", "translation": [0, 0, 0, stage_y_um, stage_x_um]}
            ]
          },
          { "path": "1", "coordinateTransformations": [ ... scale doubles at each level ... ] }
        ],
        "coordinateTransformations": [{"type": "identity"}]
      }],
      "omero": {
        "version": "0.5",
        "channels": [
          {"label": "BF", "active": true, "color": "FFFFFF", "window": {...}},
          {"label": "GFP", "active": true, "color": "00FF00", "emission_wavelength": {"value": 488, "unit": "nanometer"}, "window": {...}}
        ]
      }
    },
    "_squid": {
      "manifest_path": "../../../../acquisition.yaml",
      "acquisition_complete": true
    }
  }
}
```

### What's in each transform

- `scale` embeds the physical size of a voxel in µm on spatial axes, the time
  delta in seconds on `t`, and leaves `c = 1` (channel has no physical scale).
  For pyramid level `L`, the `y`/`x` scale is multiplied by `2^L`.
- `translation` embeds the FOV's stage origin in µm on `y` and `x`. Downstream
  stitchers read this directly from each FOV's zarr; the top-level
  `coordinates.csv` is kept for human inspection but is no longer the source of
  truth for positions.

### `_squid` pointer

`_squid.manifest_path` is a relative path from the FOV group back to the
experiment-root `acquisition.yaml`, which holds full provenance (objective,
wellplate format, channel presets, instrument manifest, etc.). The zarr
intentionally does not duplicate the manifest.

## Per-frame timestamps

Alongside the resolution-level arrays, each FOV group contains a
`frame_times` zarr array of shape `(T, C, Z)` dtype `float64` holding unix
timestamps. Values are written via `ZarrWriter.record_frame_time(t, c, z,
unix_time_s, channel_name=...)`. This replaces the older
`frame_timestamps.json` sidecar and scales cleanly to long timelapses.

A single human-friendly `acquisition_times.csv` is written at the experiment
root (`{experiment}/acquisition_times.csv`) consolidating per-frame timestamps
across every timepoint, region, FOV, channel, and z. Each row carries a
`time_point` column so all data live in one file rather than scattered across
per-timepoint folders. (Other save modes — `INDIVIDUAL_IMAGES`,
`MULTI_PAGE_TIFF`, `OME_TIFF` — keep their per-timepoint
`{experiment}/{timepoint}/frame_acquisition_times.csv` since their image data
is also organised per timepoint.)

### No empty per-timepoint folders

Because ZARR_V3 streams image data to its own per-FOV trees and consolidates
the per-frame CSV at the root, the per-timepoint folder
(`{experiment}/{timepoint:04d}/`) is **not created** for pure ZARR_V3
acquisitions. The folder is only created when something else needs it:

- Downsampled views are enabled (`SAVE_DOWNSAMPLED_WELL_IMAGES` or
  `DISPLAY_PLATE_VIEW`) — `plate_<r>um.tiff` lands per timepoint.
- Laser-AF characterization mode (`LASER_AF_CHARACTERIZATION_MODE = True`) —
  per-FOV debug bmps land per timepoint.

Acquisition-level completion is marked by a single `.done` file at
`{experiment}/.done` written by the controller when the run finishes.

## HCS plate + well metadata

For HCS acquisitions, two extra group-level `zarr.json` files are written:

- `plate.ome.zarr/zarr.json` — OME-NGFF `ome.plate` (rows, columns, wells).
- `plate.ome.zarr/{row}/{col}/zarr.json` — OME-NGFF `ome.well` (fields list).

Both are written once per acquisition when the first writer for that
plate / well initializes (see `SaveZarrJob._write_hcs_metadata_if_needed`).

## Pipelined writes

`ZarrWriter` accumulates up to `MAX_PENDING_WRITES = 32` in-flight TensorStore
futures before draining completed ones. Every `write_frame` also dispatches
the same number of pyramid-level writes, so the drain threshold is the
effective pipeline depth. `finalize()` waits for all outstanding futures.

## Configuration

| Setting | Values | Description |
|---------|--------|-------------|
| `FILE_SAVING_OPTION` | `ZARR_V3` | Enable Zarr v3 output |
| `ZARR_COMPRESSION` | `none`, `fast`, `balanced`, `best` | Compression preset |

Chunk/shard shape and pyramid depth are fixed by the implementation; they are
not user-configurable.

## Reading the output

### TensorStore (Python, any version)

```python
import tensorstore as ts

spec = {
    "driver": "zarr3",
    "kvstore": {"driver": "file", "path": "exp/plate.ome.zarr/A/1/0/0"},
}
store = ts.open(spec, read=True).result()
print(store.shape)                   # (T, C, Z, Y, X)
plane = store[0, 0, 0, :, :].read().result()
```

For stage positions, read the parent group's `zarr.json` and pull the
`translation` out of `multiscales[0].datasets[0].coordinateTransformations`.

### zarr-python ≥ 3

```python
import zarr
grp = zarr.open_group("exp/plate.ome.zarr/A/1/0", mode="r")
arr = grp["0"]
plane = arr[0, 0, 0, :, :]
```

### napari / OMERO / BiaFlows

Any OME-NGFF v0.5 HCS-plate reader works with the output directory. Pyramid
levels make scrolling at plate scale practical.

## Repackaging legacy INDIVIDUAL_IMAGES acquisitions

`software/tools/repackage_tiffs_to_zarr.py` converts an existing
`INDIVIDUAL_IMAGES` acquisition (per-frame TIFFs under per-timepoint folders)
into this exact layout, including per-FOV pyramids and translation metadata.
Missing frames are zero-filled by default and logged to `missing_frames.csv`.

```bash
python software/tools/repackage_tiffs_to_zarr.py \
    --input /path/to/experiment \
    --output /path/to/experiment/repackaged \
    --compression balanced \
    --jobs 4
```

See the script `--help` for the full flag set (`--on-missing`,
`--trim-to-last-observed-t`, `--force`, `--dry-run`).

## Related documentation

- [NDViewer Tab](ndviewer-tab.md) — live viewing during acquisition.
- [Downsampled Plate View](downsampled-plate-view.md) — overview tile images
  for wellplate scans (independent of the zarr output).
