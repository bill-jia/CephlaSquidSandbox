"""Map arbitrary scan-region names onto OME-NGFF plate coordinates.

Flexible Multipoint regions are user-named (``R0``, ``tumor_edge_2``, …) and have
no plate geometry. OME-NGFF's HCS ``plate`` model, however, constrains row and
column names to ``^[A-Za-z0-9]+$`` and a well path to exactly ``{row}/{col}``
(https://ngff.openmicroscopy.org/0.5/schemas/plate.schema), so a region name can
almost never *be* a row/column name.

The resolution is a **synthetic grid**: regions are assigned deterministic
``(row, column)`` cells, and the human-readable region name is carried in a
``_squid`` annotation block alongside the ``ome`` namespace (the NGFF schemas
don't restrict ``additionalProperties``, so custom sibling keys are legal and
survive validation). Nothing about the per-FOV image data or its
``coordinateTransformations`` changes — a region-as-well plate is byte-identical
below the FOV group to what the non-HCS layout writes.

The same mapping is reimplemented verbatim in the standalone converter
``tools/flexible_to_hcs_zarr.py`` (which must run with no ``control`` imports on
a machine that has no Squid checkout).
``tests/tools/test_flexible_to_hcs_zarr.py`` asserts the two agree.
"""

import math
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

# NGFF 0.5 plate schema: rows[].name, columns[].name, and each segment of
# wells[].path must match this. well.images[].path must match it too.
NGFF_NAME_RE = re.compile(r"^[A-Za-z0-9]+$")

# A region name that already looks like a plate well ("A1", "BC12").
WELL_ID_RE = re.compile(r"^([A-Za-z]+)([0-9]+)$")

# Grid shapes. There is deliberately no "auto": Squid's own default flexible
# region names (``R0``, ``R1``, …) parse as well ids, so name-sniffing would
# silently reinterpret an ordinary flexible scan as a plate with a row "R" and a
# column "0". Keeping real well ids is opt-in via LAYOUT_PRESERVE.
LAYOUT_GRID = "grid"
LAYOUT_ROW = "row"
LAYOUT_COLUMN = "column"
LAYOUT_PRESERVE = "preserve"
LAYOUTS = (LAYOUT_GRID, LAYOUT_ROW, LAYOUT_COLUMN, LAYOUT_PRESERVE)


def row_name(index: int) -> str:
    """Spreadsheet-style row label for a 0-based index: A..Z, AA, AB, …

    Always matches ``NGFF_NAME_RE``.
    """
    if index < 0:
        raise ValueError(f"row index must be >= 0, got {index}")
    name = ""
    n = index
    while True:
        name = chr(ord("A") + (n % 26)) + name
        n = n // 26 - 1
        if n < 0:
            return name


def looks_like_well_id(name: str) -> bool:
    """True if ``name`` is already a plate well id (``A1``, ``BC12``)."""
    return bool(WELL_ID_RE.match(str(name)))


def split_well_id(well_id: str) -> Tuple[str, str]:
    """``"B12"`` -> ``("B", "12")``. Raises for a non-well-id string."""
    m = WELL_ID_RE.match(str(well_id))
    if not m:
        raise ValueError(f"{well_id!r} is not a well id")
    return m.group(1).upper(), m.group(2)


def validate_layout(region_ids: Sequence[str], layout: str) -> str:
    """Check ``layout`` is usable for ``region_ids``; return it unchanged.

    ``preserve`` is only valid when every region name is already a well id.
    """
    if layout not in LAYOUTS:
        raise ValueError(f"unknown layout {layout!r}; expected one of {LAYOUTS}")
    if layout == LAYOUT_PRESERVE:
        bad = [r for r in region_ids if not looks_like_well_id(r)]
        if bad:
            raise ValueError(
                f"layout={LAYOUT_PRESERVE!r} needs every region to be a well id "
                f"(e.g. 'A1', 'BC12'); these are not: {bad}"
            )
    return layout


def grid_shape(count: int, layout: str) -> Tuple[int, int]:
    """``(n_rows, n_cols)`` for ``count`` regions under ``layout``."""
    if count <= 0:
        return (0, 0)
    if layout == LAYOUT_ROW:
        return (1, count)
    if layout == LAYOUT_COLUMN:
        return (count, 1)
    n_cols = int(math.ceil(math.sqrt(count)))
    n_rows = int(math.ceil(count / n_cols))
    return (n_rows, n_cols)


def region_well_map(region_ids: Sequence[str], layout: str = LAYOUT_GRID) -> Dict[str, Tuple[str, str]]:
    """Map each region id to its ``(row_name, column_name)`` plate cell.

    ``region_ids`` order *is* the assignment order — pass them in scan order, or
    pre-sorted by stage position if a spatially meaningful plate view is wanted.
    Duplicate ids are rejected: two regions cannot share a well.

    The returned names always satisfy ``NGFF_NAME_RE``, so ``{row}/{col}``
    always satisfies the plate schema's well-path pattern.
    """
    ids = [str(r) for r in region_ids]
    if len(set(ids)) != len(ids):
        dupes = sorted({r for r in ids if ids.count(r) > 1})
        raise ValueError(f"duplicate region ids cannot map to distinct wells: {dupes}")

    resolved = validate_layout(ids, layout)
    out: Dict[str, Tuple[str, str]] = {}

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


def plate_axes(well_map: Dict[str, Tuple[str, str]]) -> Tuple[List[str], List[str]]:
    """``(rows, columns)`` for the plate metadata, in plate order.

    Columns sort numerically when they are all numeric (so ``10`` follows ``9``),
    lexicographically otherwise.
    """
    rows = sorted({r for r, _c in well_map.values()}, key=lambda s: (len(s), s))
    cols = {c for _r, c in well_map.values()}
    if all(c.isdigit() for c in cols):
        columns = sorted(cols, key=int)
    else:
        columns = sorted(cols)
    return rows, columns


def is_wellplate_acquisition(xy_mode: str, region_fov_coords_mm: Dict[str, Sequence]) -> bool:
    """True when the scan's regions are **real** plate wells.

    Mirrors the acquisition-time rule: *Select Wells* always is; *Load
    Coordinates* is only if every region name is a well id **and** every region
    has the same FOV grid shape. Everything else (Flexible Multipoint, Current
    Position, …) is not.

    Shared by ``MultiPointWorker`` (which decides the on-disk layout) and the
    GUI's live-view path builder, so the two can never disagree about where the
    writer is putting frames.
    """
    if xy_mode == "Select Wells":
        return True
    if xy_mode != "Load Coordinates":
        return False
    if not region_fov_coords_mm:
        return False
    if not all(looks_like_well_id(r) for r in region_fov_coords_mm):
        return False
    grid_sizes = set()
    for coords in region_fov_coords_mm.values():
        if not coords:
            continue
        grid_sizes.add((len({round(c[0], 4) for c in coords}), len({round(c[1], 4) for c in coords})))
    return len(grid_sizes) == 1


@dataclass(frozen=True)
class PlateMapping:
    """How one acquisition's regions land on a plate.

    ``region_well_ids`` is empty for a real wellplate scan (the region id *is*
    the well id); populated for a flexible scan mapped onto a synthetic plate.
    """

    is_hcs: bool
    region_well_ids: Dict[str, str]
    layout: Optional[str]

    def well_id_for(self, region_id: str) -> str:
        return self.region_well_ids.get(str(region_id), str(region_id))


def resolve_plate_mapping(
    region_ids: Sequence[str],
    *,
    is_wellplate: bool,
    flexible_as_hcs: bool,
    layout: str = LAYOUT_GRID,
) -> PlateMapping:
    """Decide the plate layout for a run. The single source of this decision.

    Real wellplate scans pass through unchanged. A flexible scan becomes an HCS
    plate with a synthetic region -> well map when ``flexible_as_hcs`` is set;
    otherwise it stays on the flat non-HCS layout.
    """
    if is_wellplate:
        return PlateMapping(is_hcs=True, region_well_ids={}, layout=None)
    ids = [str(r) for r in region_ids]
    if not (flexible_as_hcs and ids):
        return PlateMapping(is_hcs=False, region_well_ids={}, layout=None)
    mapping = region_well_map(ids, layout)
    return PlateMapping(
        is_hcs=True,
        region_well_ids={r: f"{row}{col}" for r, (row, col) in mapping.items()},
        layout=layout,
    )


def region_annotations(
    well_map: Dict[str, Tuple[str, str]],
    *,
    fov_counts: Optional[Dict[str, int]] = None,
    layout: Optional[str] = None,
) -> List[dict]:
    """The ``_squid.regions`` annotation list written at the plate root.

    This is the record that makes the synthetic grid reversible: it is the only
    place the original region name is bound to its well cell.
    """
    rows, columns = plate_axes(well_map)
    out: List[dict] = []
    for region, (row, col) in well_map.items():
        entry = {
            "name": region,
            "path": f"{row}/{col}",
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
