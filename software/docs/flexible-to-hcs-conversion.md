# Converting Flexible Multipoint acquisitions to an OME-NGFF HCS plate

Flexible Multipoint writes a non-HCS zarr tree (`zarr/{region}/fov_n.ome.zarr`)
because its regions are user-drawn areas with user-chosen names, not plate wells.
That tree is valid OME-NGFF *image* data, but it carries **no plate grouping
metadata**, so plate-aware tools (napari-ome-zarr's plate reader, OMERO,
BiaFlows, most HCS pipelines) see a pile of unrelated images.

**As of `FLEXIBLE_MULTIPOINT_AS_HCS = True` (the default), new flexible
acquisitions save as a plate.** This document covers how regions are mapped onto
wells, and the standalone tool that converts acquisitions recorded before the
change.

## Is it standards-legal? Yes — with one constraint

Per the [NGFF 0.5 schemas](https://ngff.openmicroscopy.org/0.5/schemas/):

| Field | Constraint |
|---|---|
| `plate.rows[].name`, `plate.columns[].name` | `^[A-Za-z0-9]+$`, unique, ≥1 |
| `plate.wells[].path` | `^[A-Za-z0-9]+/[A-Za-z0-9]+$` — exactly `{row}/{col}` |
| `plate.wells[]` | requires `path`, `rowIndex`, `columnIndex` |
| `well.images[].path` | `^[A-Za-z0-9]+$` — the field subgroup name |
| `plate.field_count`, `plate.name`, `plate.acquisitions` | optional |
| `additionalProperties` | **not restricted** anywhere in the plate/well schemas |

Two consequences decide the design:

1. **A region name can almost never be a row or column name.** `tumor_edge_2`,
   `cortex slice`, `R-3` all fail `^[A-Za-z0-9]+$`. So regions get a
   **synthetic grid** of cells, and the real name is stored as an annotation.
2. **Custom keys are legal.** Because the schemas don't restrict
   `additionalProperties`, a `_squid` block sitting next to `ome` validates
   cleanly and round-trips through readers that ignore it. Squid already relies
   on this for `_squid.manifest_path`.

"HCS supports multiple tiles per well" maps exactly onto what we need: a region's
FOVs become that well's **fields** (`well.images[]`), which is the same thing
Squid already does for wellplate scans.

### What does *not* change

Below the FOV group, the non-HCS and HCS layouts are **byte-identical**: same
`(T, C, Z, Y, X)` arrays, same shards, same pyramid levels, same `frame_times`,
same `multiscales`/`omero` metadata. In particular **every FOV's stage position
is already stored** as the OME-NGFF `translation` transform on each resolution
level, so per-FOV coordinates survive the conversion for free — no rewriting, no
loss of precision, no dependence on a sidecar.

That is why conversion is a directory relocation plus a few small JSON files,
not a data rewrite.

## The region → well mapping

```
regions (scan order)      plate cells        annotation
──────────────────────────────────────────────────────────────────────
R0                   ->   A/1          _squid.regions[0].name = "R0"
cortex slice         ->   A/2          _squid.regions[1].name = "cortex slice"
R2                   ->   A/3          ...
organoid_7           ->   B/1
R4                   ->   B/2
```

Layouts (`--layout`):

| Value | Shape | Use when |
|---|---|---|
| `grid` (default) | square-ish, row-major — `ceil(sqrt(N))` columns | general case; renders compactly in plate viewers |
| `row` | one row, `N` columns | few regions, want them in a strip |
| `column` | one column, `N` rows | same, transposed |
| `preserve` | keep the region names as well ids | the "flexible" scan was really a plate (every region named `A1`, `B12`, …) |

`preserve` is deliberately **opt-in**. Squid's own default flexible region names
(`R0`, `R1`, …) also parse as well ids, so auto-detection would silently emit a
nonsense plate with a row `R` and a column `0`. The tool errors out if you ask
for `preserve` and any region isn't a well id.

Assignment order (`--order`):

- `scan` (default) — the acquisition order, read from `coordinates.csv`. Not the
  directory listing, which is alphabetical and would shuffle wells relative to
  the run.
- `name` — alphabetical.
- `spatial` — by mean stage position (y then x), so the plate view roughly
  mirrors the physical layout on the slide.

## Where the annotations live

Everything needed to recover "which well was which region, and where was every
field on the stage" is written in four places — three inside the zarr, one
beside it:

| Location | Content |
|---|---|
| `{plate}.ome.zarr/zarr.json` → `_squid.regions` | the full region ↔ well table: `name`, `path`, `row`, `column`, `rowIndex`, `columnIndex`, `field_count`, `layout` |
| `{plate}.ome.zarr/{row}/{col}/zarr.json` → `_squid.region` | this well's region name + field count |
| `.../{row}/{col}/{field}/zarr.json` → `_squid` | `region`, `fov_index`, `source_path`, `stage_position_um` (mirrors the NGFF translation), `stage_position_mm` (from `coordinates.csv`, **includes z**) |
| `{output}/region_map.json`, `{output}/fov_coordinates.csv` | the same tables as plain sidecars, for tools that don't read zarr attributes |

`stage_position_mm` matters because the 5D `(t, c, z, y, x)` translation
transform has no axis for the *stage* z — `z` there is the z-stack axis. The
per-FOV focus height only exists in `coordinates.csv`, so the converter carries
it into the zarr.

The tool also rewrites each FOV's `_squid.manifest_path`: it is a *relative*
pointer to `acquisition.yaml`, and the HCS layout is one directory level deeper
than the flexible one, so the original `../../../acquisition.yaml` would dangle.

## Ragged and derived stores

An [acquisition-cycle](acquisition-cycles.md) run with a ragged frame plan, or
one with [online postprocessing](online-postprocessing.md), writes extra
namespaced trees (`zarr/{array_key}/{region}/fov_n.ome.zarr`). Each becomes its
own sibling plate — `{array_key}.ome.zarr` — using the **same** region → well
mapping, so `A/2` is the same physical region in every plate of the run. This is
exactly the layout a wellplate cycle run already produces.

## Protocol: converting an existing acquisition

The converter is `software/tools/flexible_to_hcs_zarr.py`. It is
**standard-library only** — copy that one file to any machine with Python 3.8+.
It does not import `control`, `zarr`, `tensorstore` or `numpy`, and it never
decodes image data.

### 1. Copy the tool and dry-run

```bash
# on the analysis machine
python flexible_to_hcs_zarr.py /data/exp_2026_07_31 --dry-run
```

Prints the discovered stores, the region → well assignment, and the plate grid.
Nothing is written. **Check the region → well table here** — this is the
decision the rest of the conversion is built on.

Try `--order spatial` or `--layout row` and re-run the dry run until the plate
layout is what you want.

### 2. Convert

Pick a mode based on how much disk you have and whether you want the original
tree kept:

```bash
# A. Safest: write a separate copy (needs ~2x the dataset size)
python flexible_to_hcs_zarr.py /data/exp --output /data/exp_hcs --mode copy

# B. Recommended for large datasets: hardlink in place.
#    Instant, no extra disk, both layouts readable, deleting one keeps the other.
#    Requires one filesystem that supports hardlinks (NTFS/ext4/xfs/APFS).
python flexible_to_hcs_zarr.py /data/exp --mode link

# C. Fastest and destructive: move the FOV groups, prune the empty zarr/ tree.
python flexible_to_hcs_zarr.py /data/exp --mode move
```

Every mode ends with an automatic validation pass against the NGFF 0.5
plate/well constraints and prints `Validation: OK` (or a list of problems and a
non-zero exit code).

### 3. Verify

```bash
python flexible_to_hcs_zarr.py /data/exp_hcs --validate-only
```

and open it in a plate-aware viewer:

```bash
napari --plugin napari-ome-zarr /data/exp_hcs/plate.ome.zarr
```

or from Python:

```python
import zarr
root = zarr.open_group("/data/exp_hcs/plate.ome.zarr", mode="r")
print([w["path"] for w in root.attrs["ome"]["plate"]["wells"]])
for entry in root.attrs["_squid"]["regions"]:
    print(entry["name"], "->", entry["path"])

fov = zarr.open_group("/data/exp_hcs/plate.ome.zarr/B/1/0", mode="r")
print(fov["0"].shape)                      # (T, C, Z, Y, X)
print(fov.attrs["_squid"]["region"])       # original region name
print(fov.attrs["_squid"]["stage_position_mm"])
```

### 4. Records the tool leaves behind

At the output root:

- `region_map.json` — region ↔ well table.
- `fov_coordinates.csv` — one row per field: plate, array_key, region, well,
  row, column, field, `stage_y_um`/`stage_x_um` (from the NGFF transform),
  `x_mm`/`y_mm`/`z_mm` (from `coordinates.csv`), and the path.
- `hcs_conversion_manifest.json` — every source → destination pair plus the run
  parameters. This is what makes a `--mode move` reversible by hand.

When `--output` differs from the input directory, all experiment-root files
(`acquisition.yaml`, `coordinates.csv`, `acquisition_times.csv`,
`cycles_manifest.yaml`, logs) are copied across so the converted tree is
self-describing and every `_squid.manifest_path` resolves.

### Re-running

The tool refuses to overwrite an existing plate directory unless `--force` is
given. After a `--mode move` run there is nothing left to convert; after
`--mode link` or `--mode copy` the source tree is still there and re-running
with `--force` is safe.

## Acquisition writes HCS directly (default)

New flexible ZARR_V3 acquisitions **already save as a plate** — the converter is
only needed for datasets recorded before this change, or with the flag off.

```python
# control/_def.py
FLEXIBLE_MULTIPOINT_AS_HCS = True      # default; False = legacy zarr/{region}/ layout
FLEXIBLE_MULTIPOINT_HCS_LAYOUT = "grid"  # "grid" | "row" | "column"
```

`MultiPointWorker` resolves the region → well map **once, at acquisition
start**, from `scan_region_fov_coords_mm` — the point where the region set and
their FOV counts are frozen. It must not be computed any earlier: flexible
regions can be renamed and re-tiled right up to the moment Start is pressed, and
`ScanCoordinates` is rebuilt wholesale on any geometry change. The resolved map
travels to the save subprocess on `ZarrWriterInfo.region_well_ids`, and the
acquisition log records the assignment:

```
Flexible multipoint -> HCS plate (grid layout): R0->A1, cortex slice->A2, R2->B1
```

Ordering is the scan order (`scan_region_fov_coords_mm` insertion order), which
is the same order `coordinates.csv` records — so a run and its converted
equivalent land on the same cells. Wellplate scans (Select Wells / Load
Coordinates) are untouched: their `region_id` already *is* the well id, no map
is built, and no `_squid.regions` annotation is written.

Switching `FLEXIBLE_MULTIPOINT_AS_HCS = False` restores the flat
`zarr/{region}/fov_n.ome.zarr` layout with no other code change, which is the
rollback if a downstream script needs the old paths.

### Consequences for downstream tools

Writing HCS **closes** two non-HCS gaps for new flexible runs rather than
opening new ones: both `NDViewer.discover_zarr_v3_fovs()` and
`scripts/zarr_backfill_upload.py` already handle HCS plates (they look for
`*.ome.zarr` with an `ome.plate` attribute), while their non-HCS branches are
the ones that miss ragged/derived namespaces. Anything that hardcodes
`zarr/{region}/fov_n.ome.zarr` for *new* data needs updating — or point it at
`plate.ome.zarr/{row}/{col}/{field}` and read `_squid.regions` to recover names.

## Related documentation

- [Zarr v3 Output Format](zarr-v3-format.md) — the store inventory, per-FOV
  metadata, and known gaps.
- [Multipoint Data Saving](multipoint-data-saving.md) — how the stores are
  written during acquisition.
- [Acquisition Cycles](acquisition-cycles.md) — where ragged namespaces come from.
- [Multipoint user guide](user_guides/multipoint.md) — naming flexible regions.
