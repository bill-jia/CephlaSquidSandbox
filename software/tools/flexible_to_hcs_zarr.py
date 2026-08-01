#!/usr/bin/env python3
"""Convert a Flexible-Multipoint (non-HCS) Squid OME-Zarr acquisition into an
OME-NGFF 0.5 **HCS plate**, mapping each arbitrary scan region to a well.

SELF-CONTAINED: standard library only. Copy this single file to any machine with
Python 3.8+ and run it against an experiment directory. It does not import
``control``, ``zarr``, ``tensorstore``, ``numpy``, or anything else.

------------------------------------------------------------------------------
What it does
------------------------------------------------------------------------------
Squid's flexible (non-wellplate) ZARR_V3 output looks like::

    {experiment}/
        acquisition.yaml
        coordinates.csv
        zarr/
            {region}/fov_0.ome.zarr/{zarr.json, 0/, 1/, ..., frame_times/}
            {region}/fov_1.ome.zarr/...
        zarr/{array_key}/{region}/fov_0.ome.zarr/...   # ragged-cycle / postprocess stores

Its HCS output looks like::

    {experiment}/
        plate.ome.zarr/{row}/{col}/{field}/{zarr.json, 0/, 1/, ..., frame_times/}
        {array_key}.ome.zarr/{row}/{col}/{field}/...

**Below the FOV group the two are identical.** So this converter only has to
(a) relocate FOV groups, (b) write plate + well group metadata, and (c) patch a
small ``_squid`` annotation block inside each FOV's ``zarr.json``. No image data
is decoded, re-encoded, or rewritten — with ``--mode link`` or ``--mode move``
the conversion is effectively instantaneous regardless of dataset size.

------------------------------------------------------------------------------
Region -> well mapping
------------------------------------------------------------------------------
The NGFF plate schema constrains row and column names to ``^[A-Za-z0-9]+$`` and
a well path to exactly ``{row}/{col}``, so an arbitrary region name such as
``tumor_edge_2`` cannot itself be a row or column. Regions are therefore laid
out on a **synthetic grid** (default: square-ish, row-major) and the original
names are preserved as annotations:

  * ``{plate}.ome.zarr/zarr.json``      -> ``_squid.regions`` : full region <-> well table
  * ``{plate}.ome.zarr/{row}/{col}/zarr.json`` -> ``_squid.region`` : this well's region
  * ``.../{field}/zarr.json``           -> ``_squid.region``, ``_squid.fov_index``,
                                           ``_squid.stage_position_um``, ``_squid.source_path``
  * ``{output}/region_map.json``        -> the same table as a plain sidecar
  * ``{output}/fov_coordinates.csv``    -> one row per field with stage coordinates

The NGFF 0.5 schemas do not restrict ``additionalProperties``, so a ``_squid``
key sitting next to ``ome`` is spec-legal and survives validation. Per-FOV stage
coordinates were already stored by Squid as the OME-NGFF ``translation``
transform on every resolution level and are copied through untouched — the
``_squid``/CSV copies are redundant conveniences, not the source of truth.

If a "flexible" scan was really a plate — every region already named ``A1``,
``BC12`` — ``--layout preserve`` keeps those exact wells instead of inventing a
grid. It is opt-in on purpose: Squid's own default region names (``R0``, ``R1``,
…) also parse as well ids, and sniffing them would silently produce a nonsense
plate with a row ``R`` and a column ``0``.

------------------------------------------------------------------------------
Usage
------------------------------------------------------------------------------
    # Inspect the plan without touching anything
    python flexible_to_hcs_zarr.py /data/exp_001 --dry-run

    # Convert in place, hardlinking data (instant, no extra disk, originals kept)
    python flexible_to_hcs_zarr.py /data/exp_001 --mode link

    # Convert into a fresh directory, copying (safest, needs 2x disk)
    python flexible_to_hcs_zarr.py /data/exp_001 --output /data/exp_001_hcs --mode copy

    # Convert in place, moving (fastest, destructive - originals are consumed)
    python flexible_to_hcs_zarr.py /data/exp_001 --mode move

    # Check an already-converted plate against the NGFF 0.5 constraints
    python flexible_to_hcs_zarr.py /data/exp_001_hcs --validate-only

Every run writes ``hcs_conversion_manifest.json`` at the output root recording
each source -> destination pair and the region map, so a ``--mode move`` run can
be reversed by hand if needed.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import sys
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# NGFF 0.5 constraints (https://ngff.openmicroscopy.org/0.5/schemas/)
# ---------------------------------------------------------------------------

NGFF_NAME_RE = re.compile(r"^[A-Za-z0-9]+$")       # rows[].name, columns[].name, well image path
NGFF_WELL_PATH_RE = re.compile(r"^[A-Za-z0-9]+/[A-Za-z0-9]+$")
WELL_ID_RE = re.compile(r"^([A-Za-z]+)([0-9]+)$")
NGFF_VERSION = "0.5"

FOV_DIR_RE = re.compile(r"^fov_(\d+)\.ome\.zarr$")
MANIFEST_NAME = "hcs_conversion_manifest.json"
REGION_MAP_NAME = "region_map.json"
FOV_CSV_NAME = "fov_coordinates.csv"

LAYOUT_GRID, LAYOUT_ROW, LAYOUT_COLUMN, LAYOUT_PRESERVE = "grid", "row", "column", "preserve"
LAYOUTS = (LAYOUT_GRID, LAYOUT_ROW, LAYOUT_COLUMN, LAYOUT_PRESERVE)
ORDERS = ("scan", "name", "spatial")
MODES = ("copy", "link", "move")


# ---------------------------------------------------------------------------
# Region -> well mapping
# (mirrors control/core/hcs_region_mapping.py; kept duplicated so this file
#  stands alone. tests/tools/test_flexible_to_hcs_zarr.py asserts they agree.)
# ---------------------------------------------------------------------------


def row_name(index):
    """Spreadsheet-style row label for a 0-based index: A..Z, AA, AB, ..."""
    if index < 0:
        raise ValueError("row index must be >= 0, got %d" % index)
    name = ""
    n = index
    while True:
        name = chr(ord("A") + (n % 26)) + name
        n = n // 26 - 1
        if n < 0:
            return name


def looks_like_well_id(name):
    return bool(WELL_ID_RE.match(str(name)))


def split_well_id(well_id):
    m = WELL_ID_RE.match(str(well_id))
    if not m:
        raise ValueError("%r is not a well id" % (well_id,))
    return m.group(1).upper(), m.group(2)


def validate_layout(region_ids, layout):
    """Check ``layout`` is usable for ``region_ids``; return it unchanged."""
    if layout not in LAYOUTS:
        raise ValueError("unknown layout %r; expected one of %s" % (layout, LAYOUTS))
    if layout == LAYOUT_PRESERVE:
        bad = [r for r in region_ids if not looks_like_well_id(r)]
        if bad:
            raise ValueError(
                "layout='preserve' needs every region to be a well id "
                "(e.g. 'A1', 'BC12'); these are not: %s" % bad
            )
    return layout


def grid_shape(count, layout):
    if count <= 0:
        return (0, 0)
    if layout == LAYOUT_ROW:
        return (1, count)
    if layout == LAYOUT_COLUMN:
        return (count, 1)
    n_cols = int(math.ceil(math.sqrt(count)))
    n_rows = int(math.ceil(count / n_cols))
    return (n_rows, n_cols)


def region_well_map(region_ids, layout=LAYOUT_GRID):
    """region id -> (row_name, column_name). Input order is assignment order."""
    ids = [str(r) for r in region_ids]
    if len(set(ids)) != len(ids):
        dupes = sorted(set(r for r in ids if ids.count(r) > 1))
        raise ValueError("duplicate region ids cannot map to distinct wells: %s" % dupes)

    resolved = validate_layout(ids, layout)
    out = {}

    if resolved == LAYOUT_PRESERVE:
        for region in ids:
            out[region] = split_well_id(region)
        cells = list(out.values())
        if len(set(cells)) != len(cells):
            raise ValueError("region names collide as well ids (e.g. 'A1' and 'a1')")
        return out

    _n_rows, n_cols = grid_shape(len(ids), resolved)
    for i, region in enumerate(ids):
        out[region] = (row_name(i // n_cols), str(i % n_cols + 1))
    return out


def plate_axes(well_map):
    rows = sorted(set(r for r, _c in well_map.values()), key=lambda s: (len(s), s))
    cols = set(c for _r, c in well_map.values())
    if all(c.isdigit() for c in cols):
        columns = sorted(cols, key=int)
    else:
        columns = sorted(cols)
    return rows, columns


def region_annotations(well_map, fov_counts=None, layout=None):
    rows, columns = plate_axes(well_map)
    out = []
    for region, (row, col) in well_map.items():
        entry = {
            "name": region,
            "path": "%s/%s" % (row, col),
            "row": row,
            "column": col,
            "rowIndex": rows.index(row),
            "columnIndex": columns.index(col),
        }
        if fov_counts is not None and region in fov_counts:
            entry["field_count"] = int(fov_counts[region])
        if layout is not None:
            entry["layout"] = layout
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def _is_fov_group(path):
    return os.path.isdir(path) and os.path.isfile(os.path.join(path, "zarr.json"))


def _fov_dirs(region_dir):
    """[(fov_index, abs_path)] for fov_*.ome.zarr groups directly inside."""
    out = []
    try:
        entries = sorted(os.listdir(region_dir))
    except OSError:
        return out
    for entry in entries:
        m = FOV_DIR_RE.match(entry)
        if not m:
            continue
        path = os.path.join(region_dir, entry)
        if _is_fov_group(path):
            out.append((int(m.group(1)), path))
    out.sort()
    return out


def discover(experiment_dir):
    """Find every non-HCS store.

    Returns ``{array_key_or_None: {region_id: [(fov_index, path), ...]}}``.

    A directory directly under ``zarr/`` is a *region* if it holds
    ``fov_*.ome.zarr`` groups, and an *array-key namespace* if its children do.
    That is exactly how Squid distinguishes the dense layout
    (``zarr/{region}/fov_n``) from the ragged / postprocess-derived layout
    (``zarr/{array_key}/{region}/fov_n``).
    """
    root = os.path.join(experiment_dir, "zarr")
    found = {}
    if not os.path.isdir(root):
        return found

    for entry in sorted(os.listdir(root)):
        entry_path = os.path.join(root, entry)
        if not os.path.isdir(entry_path) or entry.startswith("."):
            continue

        direct = _fov_dirs(entry_path)
        if direct:
            found.setdefault(None, {})[entry] = direct
            continue

        for sub in sorted(os.listdir(entry_path)):
            sub_path = os.path.join(entry_path, sub)
            if not os.path.isdir(sub_path) or sub.startswith("."):
                continue
            fovs = _fov_dirs(sub_path)
            if fovs:
                found.setdefault(entry, {})[sub] = fovs
    return found


def read_scan_coordinates(experiment_dir):
    """Parse ``coordinates.csv`` -> ``{region: [(x_mm, y_mm, z_mm), ...]}``.

    Key order is the acquisition's real **scan order** (the controller writes the
    regions in the order it visits them), which a directory listing does not
    preserve. Per-region list order is the FOV order, so index *i* is
    ``fov_i``. ``z_mm`` is ``None`` when the column is absent/blank; it is the
    only stage axis not already embedded in the zarr metadata.
    """
    path = os.path.join(experiment_dir, "coordinates.csv")
    if not os.path.isfile(path):
        return {}
    out = {}
    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                region = (row.get("region") or "").strip()
                if not region:
                    continue

                def _num(key):
                    raw = (row.get(key) or "").strip()
                    try:
                        return float(raw)
                    except ValueError:
                        return None

                out.setdefault(region, []).append((_num("x (mm)"), _num("y (mm)"), _num("z (mm)")))
    except (OSError, csv.Error):
        return {}
    return out


def fov_stage_position_um(fov_group):
    """``(y_um, x_um)`` from the FOV's OME-NGFF level-0 translation transform."""
    try:
        attrs = _read_json(os.path.join(fov_group, "zarr.json")).get("attributes", {}) or {}
        ms = ((attrs.get("ome", {}) or {}).get("multiscales") or [])[0]
        for ct in ms["datasets"][0].get("coordinateTransformations", []):
            if ct.get("type") == "translation":
                tr = ct["translation"]
                return (float(tr[3]), float(tr[4]))
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        pass
    return (None, None)


def order_regions(regions, stores, order, scan_coords=None):
    """Order region ids for grid assignment.

    ``scan`` prefers ``coordinates.csv``'s region order (the true acquisition
    order); regions absent from it keep directory order, appended after.
    """
    if order == "name":
        return sorted(regions)
    if order == "scan":
        known = [r for r in (scan_coords or {}) if r in regions]
        rest = [r for r in regions if r not in known]
        return known + rest
    # spatial: mean FOV stage position, row-major (y then x)
    def centroid(region):
        pts = []
        for _ak, by_region in stores.items():
            for fov_index, path in by_region.get(region, []):
                y, x = fov_stage_position_um(path)
                if y is not None:
                    pts.append((y, x))
        if not pts:
            return (float("inf"), float("inf"))
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))

    return sorted(regions, key=lambda r: centroid(r) + (r,))


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


def plate_dir_name(array_key):
    return "plate.ome.zarr" if array_key is None else "%s.ome.zarr" % array_key


def _link_tree(src, dst):
    """Recreate ``src`` at ``dst`` with hardlinked files."""
    os.makedirs(dst, exist_ok=True)
    for entry in os.listdir(src):
        s = os.path.join(src, entry)
        d = os.path.join(dst, entry)
        if os.path.isdir(s):
            _link_tree(s, d)
        else:
            if os.path.exists(d):
                os.remove(d)
            os.link(s, d)


def place_fov(src, dst, mode):
    if os.path.exists(dst):
        shutil.rmtree(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if mode == "copy":
        shutil.copytree(src, dst)
    elif mode == "move":
        shutil.move(src, dst)
    elif mode == "link":
        try:
            _link_tree(src, dst)
        except OSError as e:
            raise SystemExit(
                "hardlinking failed (%s).\n"
                "  --mode link requires source and destination on the same volume and a\n"
                "  filesystem that supports hardlinks (NTFS, ext4, xfs, APFS...).\n"
                "  Re-run with --mode copy (needs 2x disk) or --mode move." % e
            )
    else:
        raise ValueError("unknown mode %r" % mode)


def patch_fov_metadata(fov_group, output_root, region, fov_index, source_rel, stage_mm=None):
    """Fix ``_squid`` on a relocated FOV group; return its stage position.

    ``manifest_path`` is a *relative* pointer back to the experiment's
    ``acquisition.yaml``, so relocating the group (3 levels deep -> 4) invalidates
    it. It is recomputed from the new location.

    Coordinates are recorded twice, deliberately: ``stage_position_um`` mirrors
    the OME-NGFF ``translation`` transform already on every resolution level (the
    source of truth, untouched by this tool), and ``stage_position_mm`` carries
    the ``coordinates.csv`` row including **z**, which the 5D transform has no
    axis for.
    """
    zj_path = os.path.join(fov_group, "zarr.json")
    doc = _read_json(zj_path)
    attrs = doc.setdefault("attributes", {})
    squid = attrs.setdefault("_squid", {})

    manifest_abs = os.path.join(output_root, "acquisition.yaml")
    squid["manifest_path"] = os.path.relpath(manifest_abs, fov_group).replace(os.sep, "/")
    squid["region"] = region
    squid["fov_index"] = int(fov_index)
    squid["source_path"] = source_rel

    y_um, x_um = fov_stage_position_um(fov_group)
    if y_um is not None:
        squid["stage_position_um"] = {"y": y_um, "x": x_um}
    if stage_mm is not None:
        x_mm, y_mm, z_mm = stage_mm
        entry = {"x": x_mm, "y": y_mm}
        if z_mm is not None:
            entry["z"] = z_mm
        squid["stage_position_mm"] = entry

    _write_json(zj_path, doc)
    return (y_um, x_um)


def write_plate_metadata(plate_path, well_map, ordered_regions, fov_counts, layout, plate_name, extra):
    rows, columns = plate_axes(well_map)
    wells = []
    for region in ordered_regions:
        row, col = well_map[region]
        wells.append(
            {
                "path": "%s/%s" % (row, col),
                "rowIndex": rows.index(row),
                "columnIndex": columns.index(col),
            }
        )
    plate = {
        "version": NGFF_VERSION,
        "name": plate_name,
        "rows": [{"name": r} for r in rows],
        "columns": [{"name": c} for c in columns],
        "wells": wells,
    }
    max_fields = max(fov_counts.values()) if fov_counts else 0
    if max_fields > 0:
        plate["field_count"] = int(max_fields)

    attributes = {
        "ome": {"version": NGFF_VERSION, "plate": plate},
        "_squid": {
            "source_layout": "flexible_multipoint_non_hcs",
            "region_layout": layout,
            "converted_by": os.path.basename(__file__),
            "converted_utc": datetime.now(timezone.utc).isoformat(),
            "regions": region_annotations(
                {r: well_map[r] for r in ordered_regions}, fov_counts=fov_counts, layout=layout
            ),
        },
    }
    attributes["_squid"].update(extra or {})
    _write_json(
        os.path.join(plate_path, "zarr.json"),
        {"zarr_format": 3, "node_type": "group", "attributes": attributes},
    )


def write_well_metadata(well_path, region, fov_indices):
    attributes = {
        "ome": {
            "version": NGFF_VERSION,
            "well": {"images": [{"path": str(i)} for i in fov_indices]},
        },
        "_squid": {"region": region, "field_count": len(fov_indices)},
    }
    _write_json(
        os.path.join(well_path, "zarr.json"),
        {"zarr_format": 3, "node_type": "group", "attributes": attributes},
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_plate(plate_path):
    """Check one plate tree against the NGFF 0.5 plate/well constraints.

    Returns a list of human-readable problems (empty == valid).
    """
    problems = []
    root_json = os.path.join(plate_path, "zarr.json")
    if not os.path.isfile(root_json):
        return ["%s: missing zarr.json" % plate_path]
    try:
        attrs = _read_json(root_json).get("attributes", {}) or {}
    except ValueError as e:
        return ["%s: unreadable zarr.json (%s)" % (plate_path, e)]

    ome = attrs.get("ome", {}) or {}
    plate = ome.get("plate")
    if not plate:
        return ["%s: attributes.ome.plate missing" % plate_path]
    if ome.get("version") != NGFF_VERSION:
        problems.append("%s: ome.version is %r, expected %r" % (plate_path, ome.get("version"), NGFF_VERSION))

    rows = [r.get("name") for r in plate.get("rows", [])]
    cols = [c.get("name") for c in plate.get("columns", [])]
    for label, names in (("row", rows), ("column", cols)):
        if not names:
            problems.append("%s: plate.%ss is empty (minItems 1)" % (plate_path, label))
        for n in names:
            if not (isinstance(n, str) and NGFF_NAME_RE.match(n)):
                problems.append("%s: %s name %r violates ^[A-Za-z0-9]+$" % (plate_path, label, n))
        if len(set(names)) != len(names):
            problems.append("%s: duplicate %s names" % (plate_path, label))

    wells = plate.get("wells", [])
    if not wells:
        problems.append("%s: plate.wells is empty (minItems 1)" % plate_path)
    for w in wells:
        path = w.get("path")
        if not (isinstance(path, str) and NGFF_WELL_PATH_RE.match(path)):
            problems.append("%s: well path %r violates ^[A-Za-z0-9]+/[A-Za-z0-9]+$" % (plate_path, path))
            continue
        row, col = path.split("/")
        for key, name, table in (("rowIndex", row, rows), ("columnIndex", col, cols)):
            idx = w.get(key)
            if not isinstance(idx, int) or idx < 0:
                problems.append("%s: well %s has invalid %s %r" % (plate_path, path, key, idx))
            elif idx >= len(table) or table[idx] != name:
                problems.append(
                    "%s: well %s %s=%r does not point at %r" % (plate_path, path, key, idx, name)
                )
        well_dir = os.path.join(plate_path, row, col)
        well_json = os.path.join(well_dir, "zarr.json")
        if not os.path.isfile(well_json):
            problems.append("%s: well %s has no zarr.json" % (plate_path, path))
            continue
        try:
            wattrs = _read_json(well_json).get("attributes", {}) or {}
        except ValueError as e:
            problems.append("%s: well %s zarr.json unreadable (%s)" % (plate_path, path, e))
            continue
        images = ((wattrs.get("ome", {}) or {}).get("well", {}) or {}).get("images")
        if not images:
            problems.append("%s: well %s has no ome.well.images" % (plate_path, path))
            continue
        for img in images:
            p = img.get("path")
            if not (isinstance(p, str) and NGFF_NAME_RE.match(p)):
                problems.append("%s: well %s image path %r violates ^[A-Za-z0-9]+$" % (plate_path, path, p))
                continue
            fov_dir = os.path.join(well_dir, p)
            if not _is_fov_group(fov_dir):
                problems.append("%s: well %s field %s missing or has no zarr.json" % (plate_path, path, p))
                continue
            if not os.path.isfile(os.path.join(fov_dir, "0", "zarr.json")):
                problems.append("%s: well %s field %s has no level-0 array" % (plate_path, path, p))

    squid_regions = (attrs.get("_squid", {}) or {}).get("regions")
    if squid_regions is None:
        problems.append("%s: _squid.regions annotation missing (region names not recoverable)" % plate_path)
    else:
        well_paths = set(w.get("path") for w in wells)
        for entry in squid_regions:
            if entry.get("path") not in well_paths:
                problems.append(
                    "%s: _squid.regions entry %r points at unknown well %r"
                    % (plate_path, entry.get("name"), entry.get("path"))
                )
    return problems


def find_plates(root):
    out = []
    if not os.path.isdir(root):
        return out
    for entry in sorted(os.listdir(root)):
        if not entry.endswith(".ome.zarr"):
            continue
        path = os.path.join(root, entry)
        zj = os.path.join(path, "zarr.json")
        if not os.path.isfile(zj):
            continue
        try:
            attrs = _read_json(zj).get("attributes", {}) or {}
        except ValueError:
            continue
        if "plate" in (attrs.get("ome", {}) or {}):
            out.append(path)
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def convert(args):
    experiment_dir = os.path.abspath(args.experiment_dir)
    output_root = os.path.abspath(args.output) if args.output else experiment_dir
    in_place = os.path.normcase(output_root) == os.path.normcase(experiment_dir)

    if not os.path.isdir(experiment_dir):
        raise SystemExit("not a directory: %s" % experiment_dir)

    stores = discover(experiment_dir)
    if not stores:
        raise SystemExit(
            "no non-HCS zarr stores found under %s\n"
            "  Expected {experiment}/zarr/{region}/fov_N.ome.zarr\n"
            "  (an already-HCS acquisition needs no conversion; run --validate-only to check it)"
            % os.path.join(experiment_dir, "zarr")
        )

    # Region universe, in first-seen order across the dense store then the others.
    regions = []
    for array_key in [None] + sorted(k for k in stores if k is not None):
        for region in stores.get(array_key, {}):
            if region not in regions:
                regions.append(region)
    scan_coords = read_scan_coordinates(experiment_dir)
    ordered_regions = order_regions(regions, stores, args.order, scan_coords)
    resolved_layout = validate_layout(ordered_regions, args.layout)
    well_map = region_well_map(ordered_regions, resolved_layout)

    fov_counts = {}
    for region in ordered_regions:
        counts = [len(by_region.get(region, [])) for by_region in stores.values()]
        fov_counts[region] = max(counts) if counts else 0

    print("Experiment : %s" % experiment_dir)
    print("Output     : %s%s" % (output_root, "  (in place)" if in_place else ""))
    print("Mode       : %s" % args.mode)
    print("Layout     : %s (order=%s)" % (resolved_layout, args.order))
    print("Stores     : %s" % ", ".join(plate_dir_name(k) for k in sorted(stores, key=lambda k: (k is not None, k))))
    print("Regions    : %d" % len(ordered_regions))
    if resolved_layout != LAYOUT_PRESERVE:
        n_rows, n_cols = grid_shape(len(ordered_regions), resolved_layout)
        print("Plate grid : %d rows x %d columns" % (n_rows, n_cols))
    print("")
    for region in ordered_regions:
        row, col = well_map[region]
        print("  %-28s -> %s/%s   (%d fields)" % (region, row, col, fov_counts[region]))
    print("")

    if args.dry_run:
        total = sum(len(f) for by_region in stores.values() for f in by_region.values())
        print("DRY RUN: would relocate %d FOV groups into %d plate(s). Nothing written." % (total, len(stores)))
        return 0

    for array_key in stores:
        plate_path = os.path.join(output_root, plate_dir_name(array_key))
        if os.path.exists(plate_path) and not args.force:
            raise SystemExit(
                "refusing to overwrite existing %s (pass --force to replace it)" % plate_path
            )

    manifest = {
        "tool": os.path.basename(__file__),
        "converted_utc": datetime.now(timezone.utc).isoformat(),
        "experiment_dir": experiment_dir,
        "output_root": output_root,
        "mode": args.mode,
        "layout": resolved_layout,
        "order": args.order,
        "region_map": region_annotations(well_map, fov_counts=fov_counts, layout=resolved_layout),
        "moves": [],
    }
    fov_rows = []

    for array_key in sorted(stores, key=lambda k: (k is not None, k)):
        by_region = stores[array_key]
        plate_path = os.path.join(output_root, plate_dir_name(array_key))
        if os.path.exists(plate_path) and args.force:
            shutil.rmtree(plate_path)
        os.makedirs(plate_path, exist_ok=True)

        present = [r for r in ordered_regions if r in by_region]
        plate_fov_counts = {r: len(by_region[r]) for r in present}

        for region in present:
            row, col = well_map[region]
            well_path = os.path.join(plate_path, row, col)
            os.makedirs(well_path, exist_ok=True)
            fov_indices = []
            for fov_index, src in by_region[region]:
                dst = os.path.join(well_path, str(fov_index))
                source_rel = os.path.relpath(src, experiment_dir).replace(os.sep, "/")
                region_coords = scan_coords.get(region, [])
                stage_mm = region_coords[fov_index] if fov_index < len(region_coords) else None
                place_fov(src, dst, args.mode)
                y_um, x_um = patch_fov_metadata(
                    dst, output_root, region, fov_index, source_rel, stage_mm
                )
                fov_indices.append(fov_index)
                manifest["moves"].append(
                    {
                        "plate": plate_dir_name(array_key),
                        "array_key": array_key,
                        "region": region,
                        "well": "%s/%s" % (row, col),
                        "field": fov_index,
                        "source": source_rel,
                        "destination": os.path.relpath(dst, output_root).replace(os.sep, "/"),
                    }
                )
                def _mm(i):
                    if stage_mm is None or stage_mm[i] is None:
                        return ""
                    return "%.6f" % stage_mm[i]

                fov_rows.append(
                    {
                        "plate": plate_dir_name(array_key),
                        "array_key": "" if array_key is None else array_key,
                        "region": region,
                        "well": "%s/%s" % (row, col),
                        "row": row,
                        "column": col,
                        "field": fov_index,
                        "stage_y_um": "" if y_um is None else "%.3f" % y_um,
                        "stage_x_um": "" if x_um is None else "%.3f" % x_um,
                        "x_mm": _mm(0),
                        "y_mm": _mm(1),
                        "z_mm": _mm(2),
                        "path": os.path.relpath(dst, output_root).replace(os.sep, "/"),
                    }
                )
            write_well_metadata(well_path, region, sorted(fov_indices))

        write_plate_metadata(
            plate_path,
            {r: well_map[r] for r in present},
            present,
            plate_fov_counts,
            resolved_layout,
            plate_name=(os.path.basename(experiment_dir) if array_key is None else str(array_key)),
            extra={"array_key": array_key} if array_key is not None else {},
        )
        print("Wrote plate %s (%d wells)" % (plate_dir_name(array_key), len(present)))

    # Experiment-root records: copy them along when writing to a new directory so
    # the converted tree is self-describing (acquisition.yaml is what every FOV's
    # _squid.manifest_path points at).
    if not in_place:
        os.makedirs(output_root, exist_ok=True)
        for entry in sorted(os.listdir(experiment_dir)):
            src = os.path.join(experiment_dir, entry)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(output_root, entry))

    _write_json(os.path.join(output_root, REGION_MAP_NAME), manifest["region_map"])
    _write_json(os.path.join(output_root, MANIFEST_NAME), manifest)

    csv_path = os.path.join(output_root, FOV_CSV_NAME)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        fields = ["plate", "array_key", "region", "well", "row", "column", "field",
                  "stage_y_um", "stage_x_um", "x_mm", "y_mm", "z_mm", "path"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in fov_rows:
            writer.writerow(r)

    # Prune the emptied source tree after a move.
    if args.mode == "move":
        zarr_root = os.path.join(experiment_dir, "zarr")
        # topdown=False yields deepest-first, which is exactly the order rmdir
        # needs. Do NOT sort these — sorting reorders parents before children.
        for dirpath, _dirnames, _filenames in os.walk(zarr_root, topdown=False):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass

    print("")
    print("Wrote %s, %s, %s" % (REGION_MAP_NAME, FOV_CSV_NAME, MANIFEST_NAME))

    problems = []
    for plate_path in find_plates(output_root):
        problems.extend(validate_plate(plate_path))
    if problems:
        print("\nVALIDATION FAILED:")
        for p in problems:
            print("  - %s" % p)
        return 1
    print("Validation: OK (%d plate(s) conform to NGFF %s)" % (len(find_plates(output_root)), NGFF_VERSION))
    return 0


def validate_only(args):
    root = os.path.abspath(args.experiment_dir)
    plates = find_plates(root)
    if not plates:
        raise SystemExit("no OME-NGFF plates (*.ome.zarr with ome.plate) found in %s" % root)
    problems = []
    for plate_path in plates:
        plate_problems = validate_plate(plate_path)
        status = "OK" if not plate_problems else "%d problem(s)" % len(plate_problems)
        print("%-40s %s" % (os.path.basename(plate_path), status))
        problems.extend(plate_problems)
    if problems:
        print("")
        for p in problems:
            print("  - %s" % p)
        return 1
    print("\nAll %d plate(s) conform to NGFF %s." % (len(plates), NGFF_VERSION))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Convert a Flexible-Multipoint (non-HCS) Squid OME-Zarr acquisition "
                    "into an OME-NGFF 0.5 HCS plate, mapping regions to wells.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage")[-1],
    )
    parser.add_argument("experiment_dir", help="Experiment directory (contains zarr/ and acquisition.yaml)")
    parser.add_argument("--output", default=None,
                        help="Destination root (default: convert in place, next to zarr/)")
    parser.add_argument("--mode", choices=MODES, default="copy",
                        help="copy (safe, 2x disk) | link (hardlink, instant, same volume only) | "
                             "move (fastest, consumes the source). Default: copy")
    parser.add_argument("--layout", choices=LAYOUTS, default=LAYOUT_GRID,
                        help="Plate grid shape. grid = square-ish synthetic grid (default), "
                             "row / column = single strip, preserve = keep the region names as "
                             "well ids (only valid when every region is already named like 'A1')")
    parser.add_argument("--order", choices=ORDERS, default="scan",
                        help="Region -> cell assignment order. scan = acquisition order from "
                             "coordinates.csv (directory order if absent), name = alphabetical, "
                             "spatial = by mean stage position (y then x). Default: scan")
    parser.add_argument("--force", action="store_true", help="Replace existing plate directories")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan and exit")
    parser.add_argument("--validate-only", action="store_true",
                        help="Validate plates already present in experiment_dir and exit")
    args = parser.parse_args(argv)

    if args.validate_only:
        return validate_only(args)
    return convert(args)


if __name__ == "__main__":
    sys.exit(main())
