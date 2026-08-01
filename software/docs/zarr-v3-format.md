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
| Outer shard | `(1, C, 1, Y, X)` — one z-slice (default, `ZARR_SHARD_PER_Z=True`); `(1, C, Z, Y, X)` — one FOV-timepoint bundle (legacy) |
| Compression | blosc-zstd clevel 3 + bitshuffle (default, `BALANCED`) |
| Pyramid | up to 5 extra levels, written inline per z-slice |
| Per-frame timestamps | `frame_times` zarr array (shape `T×C×Z`, float64) |
| Metadata | OME-NGFF `multiscales` + `omero` + `_squid.manifest_path` |

A **shard is the unit written to disk as one file**, so it must be committed in
one pass — TensorStore cannot cheaply append a chunk to an existing shard
(doing so rewrites the whole file). The default `(1, C, 1, Y, X)` shard matches
the z-outer/channel-inner acquisition loop: the writer buffers a z-slice's
channels and commits that shard once the slice is fully captured, synchronously
with acquisition and with no per-frame read-modify-write. This is ~30× faster
writeback on deep stacks than writing plane-by-plane into one giant per-FOV
shard. The inner *chunk* is one plane in both layouts, so reads (scrolling z,
switching channels) are identical. Set `ZARR_SHARD_PER_Z = False` in `_def.py`
to fall back to the legacy one-shard-per-FOV layout.

File count per FOV ≈ `T × Z × (num_pyramid_levels + 1)` (per-z, default) or
`T × (num_pyramid_levels + 1)` (legacy). Each shard is written once and never
reopened.

### Shard file paths

With zarr-v3 `default` chunk-key encoding, a shard file's path *is* its grid
coordinate: `{level}/c/{t}/{c_grid}/{z_grid}/{y_grid}/{x_grid}`. Because the
shard spans all channels and the full `Y`,`X`, only `t` and `z` vary:

| Layout | Shard grid | Shard file |
|---|---|---|
| per-z (default, `ZARR_SHARD_PER_Z=True`) | `(1, C, 1, Y, X)` | `c/{t}/0/{z}/0/0` — **one per z** |
| legacy (`ZARR_SHARD_PER_Z=False`) | `(1, C, Z, Y, X)` | `c/{t}/0/0/0/0` — one per timepoint |

Any external tool that enumerates shards by path must handle the `{z}` axis —
see [Known gaps](#known-gaps-and-limitations).

## Store inventory

A single acquisition can produce **more than one** zarr store. Which stores
appear depends on the xy layout (HCS vs flexible) and on whether the run uses
[acquisition cycles](acquisition-cycles.md) with a ragged frame plan or
[online postprocessing](online-postprocessing.md).

Whether a run uses the HCS or the non-HCS column below is decided by
`FLEXIBLE_MULTIPOINT_AS_HCS` (default `True` ⇒ HCS for every layout); the
row is decided by the cycle plan.

| Store kind | When | HCS path | Non-HCS path (opt-out) | Array shape |
|---|---|---|---|---|
| **Dense** (one multichannel array per FOV) | Plain channel selection, or a cycle plan where every imaged state has the same frame count *and* one z-mode | `plate.ome.zarr/{row}/{col}/{fov}/` | `zarr/{region}/fov_{n}.ome.zarr/` | `(Nt × frames_per_state, C, NZ, Y, X)` |
| **Ragged** (one single-channel array per *(state, z-mode)*) | Cycle plan with unequal per-state frame counts, or mixed `acquire_z_stack` | `{state}.ome.zarr/{row}/{col}/{fov}/` | `zarr/{state}/{region}/fov_{n}.ome.zarr/` | `(Nt × that_state's_count, 1, NZ, Y, X)` |
| **Ragged, reference-z only** | A cycle step with **Full z-stack** unchecked | `{state}_refz.ome.zarr/...` | `zarr/{state}_refz/{region}/...` | `(Nt × count, 1, **1**, Y, X)` |
| **Derived** (postprocess output) | A step/group with a Postprocess routine assigned | `{label}_{output}.ome.zarr/...` | `zarr/{label}_{output}/{region}/...` | `(Nt, 1, z_size, Y, X)` |

The path-building rules live in `control/utils.py`
(`build_hcs_zarr_fov_path` / `build_per_fov_zarr_path`); the `array_key` that
selects a namespace is `None` for dense, `array_key_for(state, acquire_z_stack)`
for ragged (`control/models/acquisition_cycle.py`), and `{label}_{output}` for
derived plates (`PostprocessJob._write_output`).

Key consequences:

- **Every store is structurally identical below the FOV group** — same
  `(T, C, Z, Y, X)` arrays, same pyramid, same `frame_times`, same OME-NGFF
  metadata. Only the extents and the channel list differ.
- **In HCS mode each namespace is a full, independent OME-NGFF plate**, with its
  own `ome.plate` root and `ome.well` groups (written once per plate by
  `SaveZarrJob._write_hcs_metadata_if_needed`). A ragged 3-state run with one
  DPC output produces four sibling `*.ome.zarr` plates in the experiment dir.
- **A ragged/derived store's `C` axis is always 1**, and its `omero.channels`
  has exactly that one entry (the state name, or `{label}_{output}`). The
  channel axis carries no information in ragged mode — the *store name* does.
- **Raw frames consumed by a postprocess routine are never written.** Only the
  routine's declared outputs land on disk.
- **Per-region channel subsets** (Per-Point Channels) are handled by each frame
  self-describing its array, so two regions in the same run can legitimately have
  different `C` and different channel names in the *same* dense plate hierarchy.

### Channel and sequence semantics

Where a frame lands is computed by `frame_coord()`:

| | Dense | Ragged |
|---|---|---|
| Store | one per FOV | one per *(state, z-mode)* per FOV |
| `c` index | position of the state in the region's channel order | always 0 |
| `t` index | `t_scan × frames_per_state + state_frame_index` | `t_scan × that_state's_count + state_frame_index` |
| `T` size | `Nt × frames_per_state` | `Nt × that_state's_count` |

So a per-position cycle's repeats are **folded into `T`**, with the scan-level
timelapse blocking on top of it — timepoint `t_scan` owns the contiguous `T`
range `[t_scan × count, (t_scan+1) × count)`. The original acquisition order is
recoverable from `acquisition_times.csv` (`cycle_event_index`,
`state_frame_index`) and `cycles_manifest.yaml`.

## Output layouts

### HCS (plate) — the default for every layout

Squid writes the OME-NGFF HCS plate hierarchy when the xy layout resolves to
well IDs (`A1`, `B12`, …) **and** — since `FLEXIBLE_MULTIPOINT_AS_HCS = True` —
for Flexible Multipoint scans, whose arbitrary regions are mapped onto synthetic
plate cells with their names preserved as `_squid` annotations (see
[flexible-to-hcs-conversion.md](flexible-to-hcs-conversion.md)).

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

### Non-HCS (opt-out)

Only written when `FLEXIBLE_MULTIPOINT_AS_HCS = False`, or by acquisitions
recorded before that setting existed:

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
structure below the FOV group. There is **no plate/well grouping metadata** in
the non-HCS layout — the directory names are the only index, so plate-aware
readers see unrelated images. `tools/flexible_to_hcs_zarr.py` converts such a
tree into a real HCS plate by mapping each region to a well; see
[flexible-to-hcs-conversion.md](flexible-to-hcs-conversion.md).

### Ragged / derived namespaces

A ragged cycle run or a postprocess output inserts one namespace level:

```
{experiment}/
├── BF.ome.zarr/           # HCS: sibling plates, one per (state, z-mode) or output
├── BF_refz.ome.zarr/
├── dpc_phase.ome.zarr/
└── zarr/                  # non-HCS: one subtree per namespace
    ├── BF/{region}/fov_0.ome.zarr/
    ├── BF_refz/{region}/fov_0.ome.zarr/
    └── dpc_phase/{region}/fov_0.ome.zarr/
```

Note the asymmetry: in HCS mode the namespace replaces `plate` in the store
name at the *same depth*; in non-HCS mode it is an *extra* directory level
between `zarr/` and `{region}/`. Tools that walk these trees by path must
account for both — see [Known gaps](#known-gaps-and-limitations).

## Array structure

- **Shape**: `(T, C, Z, Y, X)`.
- **Dtype**: whatever the camera produces (usually `uint16`).
- **Inner chunk** (`chunks` inside the sharding codec): `(1, 1, 1, Y, X)` — a single image plane. Enables per-`(t, c, z)` random access.
- **Outer shard** (the zarr-v3 chunk-grid chunk, containing the inner chunks): `(1, C, 1, Y, X)` by default — one z-slice (all channels) per file, committed once that slice is fully captured. Set `ZARR_SHARD_PER_Z = False` for the legacy `(1, C, Z, Y, X)` one-shard-per-`(FOV, timepoint)` layout. Either way acquisition writes each shard exactly once and never reopens it; the per-z default avoids the per-frame read-modify-write of a multi-hundred-MB shard that made plane-by-plane writeback ~30× slower.

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
populated as each z-slice is committed (per-z layout) or per frame (legacy),
via a cascade of `cv2.pyrDown` applied to every channel. Each level's shape is
`((Y+1)//2, (X+1)//2)` relative to the previous. Generation stops when
`min(Y, X) < 128` or after 5 extra levels (defaults on `ZarrAcquisitionConfig`).

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

### `_squid` block

A custom `_squid` key sits beside `ome` in the group attributes. The NGFF 0.5
schemas do not restrict `additionalProperties`, so this validates cleanly and
readers that don't know about it ignore it.

On a **FOV group**:

| Key | Meaning |
|---|---|
| `manifest_path` | Relative path back to the experiment-root `acquisition.yaml`, which holds full provenance (objective, wellplate format, channel presets, instrument manifest). The zarr intentionally does not duplicate the manifest. |
| `acquisition_complete` | Flipped to `true` by `ZarrWriter.finalize()`. |
| `region` | Originating region/well id. |
| `fov_index` | Index of this field within its region. |
| `well`, `stage_position_um` | Only for a synthetic plate (flexible regions mapped to wells): the assigned well and the FOV's stage origin, so the image is self-describing without a plate-root lookup. |

On a **synthetic plate root**: `source_layout`, `region_layout`, and `regions` —
the region ↔ well table that binds each well back to its user-given name. On a
**synthetic well group**: `region` and `field_count`. Neither is written for a
real wellplate scan, where the well id already *is* the region name.

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

- `{plate}.ome.zarr/zarr.json` — OME-NGFF `ome.plate` (rows, columns, wells).
- `{plate}.ome.zarr/{row}/{col}/zarr.json` — OME-NGFF `ome.well` (fields list).

Both are written once **per plate** when the first writer for that plate / well
initializes (see `SaveZarrJob._write_hcs_metadata_if_needed`). Ragged and
derived namespaces each get their own pair, with `plate.name` set to the
`array_key` (`BF`, `BF_refz`, `dpc_phase`, …) rather than the literal `"plate"`.
Rows/columns/wells are identical across the plates of one run — they come from
the same `region_fov_counts` map — so the plates are aligned well-for-well.

The non-HCS layout writes no equivalent grouping metadata.

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

## Known gaps and limitations

Audited 2026-07-31. These are **current behaviours to be aware of**, not
aspirations — each is a place where a consumer of the output can be surprised.

1. ~~**`_squid.manifest_path` is wrong for non-HCS ragged/derived stores.**~~
   **Fixed.** `get_manifest_path()` now takes the `array_key`, so the pointer
   accounts for the extra directory level a ragged/derived non-HCS store sits
   at. Datasets written before the fix have `../../../acquisition.yaml` in those
   stores, which resolves to `{experiment}/zarr/` — read `acquisition.yaml` from
   the experiment root directly for those.
2. ~~**`scripts/zarr_backfill_upload.py` does not support the default per-z
   shard layout.**~~ **Fixed.** It now enumerates a timepoint's shards by
   walking `c/{t}/` instead of constructing `c/{t}/0/0/0/0`, and its guard only
   requires that a shard not span two timepoints (5D array, `chunk_shape[0] ==
   1`). That covers per-z, legacy per-FOV, and unsharded layouts alike;
   `--follow` also dedupes per file so a part-written timepoint isn't stranded.
3. **The post-finalize upload metadata resync only covers dense stores.**
   `MultiPointWorker._collect_metadata_paths_for_fov()` resolves the group path
   with `array_key=None`, so for ragged/derived plates the *final* `zarr.json`
   (the one carrying `acquisition_complete: true`) and the last `frame_times`
   chunk are never re-pushed. Those files were uploaded by the per-FOV barriers
   during the run, so the remote data is complete, but the remote copy's
   `_squid.acquisition_complete` stays `false`. Anything keying on that flag
   (including the backfill script's `--follow` exit condition) will not see the
   run as finished.
4. **The NDViewer's offline discovery only finds the dense store.**
   `discover_zarr_v3_fovs()` looks for `plate.ome.zarr` and
   `zarr/{region}/fov_*.ome.zarr`. Ragged plates (`{state}.ome.zarr`) and the
   extra non-HCS namespace level are not enumerated, so a ragged run opened from
   disk shows only its dense store (nothing at all if the run is entirely
   ragged). Flexible runs are no longer affected for the dense case — they now
   land in `plate.ome.zarr`, which is the first thing it checks. Live push-mode
   viewing during acquisition is unaffected.
5. **The stitcher cannot consume ZARR_V3 output.** `control/stitcher.py` reads
   per-timepoint TIFF/BMP folders plus per-timepoint `coordinates.csv`; it has
   no zarr reader. Its own `.ome.zarr` output is a *different* format
   (zarr v2 via `ome-zarr`/`aicsimageio`, OME-NGFF 0.4-era), written to
   `{input}/{t}_stitched/` and `*_complete_acquisition.ome.zarr`. Stitching a
   ZARR_V3 acquisition requires exporting to TIFF first.
6. **Local shard-directory pruning after verified upload skips ragged stores**
   (`_prune_empty_shard_dirs` also resolves with `array_key=None`). Cosmetic —
   empty `c/{t}/…` directories are left behind; no data is affected.

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

Substitute the store name for a ragged or derived namespace — e.g.
`exp/BF_refz.ome.zarr/A/1/0/0`, or `exp/zarr/dpc_phase/R0/fov_0.ome.zarr/0`.
Because ragged stores are single-channel, index them as `[t, 0, z, :, :]`.

### napari / OMERO / BiaFlows

Any OME-NGFF v0.5 HCS-plate reader works with the output directory. Pyramid
levels make scrolling at plate scale practical. A ragged run is *n* separate
plates — open each one; they are not merged into a single multi-channel view.

## Other zarr writers in this codebase

These produce zarr, but **not** this format. Don't confuse them with acquisition
output.

| Producer | Format | Layout |
|---|---|---|
| `FastAcquisitionWriter` (fast NIDAQ capture, `file_format="zarr"`) | **zarr v2**, blosc-lz4, no OME metadata | `{run}/frames.zarr` with flat `frames (N, Y, X)`, `frame_ids (N,)`, `timestamps (N,)` datasets; chunks `(100, Y, X)`. Context lives in sibling `metadata.json` + `acquisition_metadata.yaml` + `frames/frame_metadata.jsonl`, not in the store. |
| `control/stitcher.py` | **zarr v2** OME-NGFF (via `ome-zarr` / `aicsimageio`) | `{input}/{t}_stitched/{region}_{name}.ome.zarr`, then a merged `*_complete_acquisition.ome.zarr` (plain or HCS). Input is TIFF acquisitions only. |
| `tools/script_create_zarr_from_acquisition.py` | zarr v2 via `ome-zarr` | Legacy one-off converter driven by the old `configurations.xml`; superseded by the repackage tool below. |
| `control/core/io_simulation.py` | none (no files written) | Simulated-write benchmarking path; accounts bytes and throttles only. |

## Repackaging legacy INDIVIDUAL_IMAGES acquisitions

`software/tools/repackage_tiffs_to_zarr.py` converts an existing
`INDIVIDUAL_IMAGES` acquisition (per-frame TIFFs under per-timepoint folders)
into this exact layout, including per-FOV pyramids and translation metadata.
Missing frames are zero-filled by default and logged to `missing_frames.csv`.
It writes the **dense** layout only (one multichannel array per FOV) with the
current default per-z shard granularity, and does not populate omero channel
colors / wavelengths.

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

- [Multipoint Data Saving](multipoint-data-saving.md) — the job pipeline that
  feeds this writer, and the other save formats.
- [Acquisition Cycles](acquisition-cycles.md) — where the dense/ragged decision
  and the `_refz` namespaces come from.
- [Online Postprocessing](online-postprocessing.md) — the derived plates.
- [Streaming OME-Zarr to a Network Drive](zarr-network-streaming.md) — live
  upload, deletion safety, and the backfill script.
- [Flexible → HCS conversion](flexible-to-hcs-conversion.md) — turning a
  flexible-region acquisition into an OME-NGFF plate.
- [NDViewer Tab](ndviewer-tab.md) — live viewing during acquisition.
- [Downsampled Plate View](downsampled-plate-view.md) — overview tile images
  for wellplate scans (independent of the zarr output).
