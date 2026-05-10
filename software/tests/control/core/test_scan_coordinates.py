from types import SimpleNamespace
from unittest.mock import MagicMock

import tests.control.gui_test_stubs as gts
import squid.stage
from control.core.scan_coordinates import (
    ScanCoordinates,
    ScanCoordinatesUpdate,
    AddScanCoordinateRegion,
    RemovedScanCoordinateRegion,
    ClearedScanCoordinates,
)
from control.microscope import Microscope


def _make_scan_coordinates(fov_w_mm: float = 1.0, fov_h_mm: float = 1.0) -> ScanCoordinates:
    """Build a ScanCoordinates without booting the full Microscope (no hardware required)."""
    objective_store = MagicMock()
    objective_store.get_pixel_size_factor.return_value = 1.0

    camera = MagicMock()
    camera.get_fov_size_mm.return_value = (fov_w_mm, fov_h_mm)

    stage = MagicMock()
    stage.get_pos.return_value = SimpleNamespace(x_mm=0.0, y_mm=0.0, z_mm=0.0)

    return ScanCoordinates(objective_store, stage, camera)


def test_scan_coordinates_basic_operation():
    # The scope creates a scan config, but just for sanity/clarity we'll create our own below.
    scope = Microscope.build_from_global_config(simulated=True)

    add_count = 0
    remove_count = 0
    clear_count = 0
    update_count = 0

    def test_callback(update: ScanCoordinatesUpdate):
        nonlocal add_count, remove_count, clear_count, update_count
        if isinstance(update, AddScanCoordinateRegion):
            add_count += 1
        elif isinstance(update, RemovedScanCoordinateRegion):
            remove_count += 1
        elif isinstance(update, ClearedScanCoordinates):
            clear_count += 1
        else:
            raise ValueError(f"Unknown update case in scan coordinates test: {update.__class__}")
        update_count += 1

    scan_coordinates = ScanCoordinates(scope.objective_store, scope.stage, scope.camera, update_callback=test_callback)

    single_fov_center = (6.0, 7.0, 3.0)
    flexible_center = (8.0, 9.0, 0.5)
    well_center = (6.5, 8.5, scope.stage.get_pos().z_mm)
    scan_coordinates.add_single_fov_region("single_fov", *single_fov_center)
    scan_coordinates.add_flexible_region("flexible_region", *flexible_center, 2, 2, 10)
    scan_coordinates.add_region("well_region", well_center[0], well_center[1], 4, 10, "Circle")

    assert add_count == 3
    assert remove_count == 0
    assert clear_count == 0
    assert update_count == 3

    assert set(scan_coordinates.region_centers.keys()) == {"single_fov", "flexible_region", "well_region"}
    assert set([tuple(c) for c in scan_coordinates.region_centers.values()]) == {
        single_fov_center,
        flexible_center,
        well_center,
    }

    scan_coordinates.remove_region("single_fov")
    assert add_count == 3
    assert remove_count == 1
    assert clear_count == 0
    assert update_count == 4

    assert set(scan_coordinates.region_centers.keys()) == {"flexible_region", "well_region"}
    assert set([tuple(c) for c in scan_coordinates.region_centers.values()]) == {flexible_center, well_center}

    scan_coordinates.remove_region("well_region")
    assert add_count == 3
    assert remove_count == 2
    assert clear_count == 0
    assert update_count == 5

    assert set(scan_coordinates.region_centers.keys()) == {"flexible_region"}
    assert set([tuple(c) for c in scan_coordinates.region_centers.values()]) == {flexible_center}

    scan_coordinates.clear_regions()
    assert add_count == 3
    assert remove_count == 2
    assert clear_count == 1
    assert update_count == 6

    assert len(scan_coordinates.region_centers.keys()) == 0
    assert len(scan_coordinates.region_centers.values()) == 0


def test_sort_coordinates_manual_regions_preserve_drawing_order():
    """Manual regions stay in drawing order and come before wells. The well sweep
    starts at the corner closest to the last manual exit, so a manual region in
    the bottom-right makes the well order start there too."""
    sc = _make_scan_coordinates()
    sc.acquisition_pattern = "S-Pattern"

    # Set up regions directly (bypass coordinate validation)
    sc.region_centers = {
        "A1": [10.0, 10.0],
        "manual1": [99.0, 99.0],  # Drawn second, far position (bottom-right)
        "B1": [10.0, 20.0],
        "manual0": [10.0, 10.0],  # Drawn first, same position as A1
        "B2": [20.0, 20.0],
        "A2": [20.0, 10.0],
    }
    sc.region_fov_coordinates = {k: [(v[0], v[1], 0.0)] for k, v in sc.region_centers.items()}

    sc.sort_coordinates()

    keys = list(sc.region_centers.keys())
    # Manuals first (drawing order). Wells: closest corner to manual1's exit
    # (99, 99) is B2 (20, 20), so the optimizer picks bottom-right row-major.
    assert keys == ["manual0", "manual1", "B2", "B1", "A1", "A2"]


def test_sort_coordinates_picks_column_major_for_tall_narrow_selection():
    """When wells are stacked tall-and-narrow with column spacing >> row spacing,
    sweeping each column top-to-bottom and snaking back is much shorter than
    crossing the long horizontal gap on every row."""
    sc = _make_scan_coordinates()
    sc.acquisition_pattern = "S-Pattern"

    # Two columns 100mm apart, 4 rows 10mm apart. Row-major would cross the 100mm
    # gap 4 times; col-major crosses it once and walks short rungs in between.
    cols = (10.0, 110.0)
    rows = (0.0, 10.0, 20.0, 30.0)
    for r_idx, y in enumerate(rows):
        for c_idx, x in enumerate(cols):
            wid = f"{chr(ord('A') + r_idx)}{c_idx + 1}"
            sc.region_centers[wid] = [x, y]
            sc.region_fov_coordinates[wid] = [(x, y, 0.0)]

    sc.sort_coordinates()

    keys = list(sc.region_centers.keys())
    # Col-major TL: col 1 top->bottom, then col 2 bottom->top.
    expected = ["A1", "B1", "C1", "D1", "D2", "C2", "B2", "A2"]
    assert keys == expected


def test_snake_from_rows_four_corners():
    """_snake_from_rows produces a boustrophedon starting at each of the 4 corners."""
    rows = [
        [("a0", 0, 0), ("a1", 1, 0), ("a2", 2, 0)],
        [("b0", 0, 1), ("b1", 1, 1), ("b2", 2, 1)],
        [("c0", 0, 2), ("c1", 1, 2), ("c2", 2, 2)],
    ]

    def ids(path):
        return [p[0] for p in path]

    # Top-left: row 0 LTR, row 1 RTL, row 2 LTR
    assert ids(ScanCoordinates._snake_from_rows(rows, True, True)) == [
        "a0", "a1", "a2", "b2", "b1", "b0", "c0", "c1", "c2"
    ]
    # Top-right: row 0 RTL, row 1 LTR, row 2 RTL
    assert ids(ScanCoordinates._snake_from_rows(rows, True, False)) == [
        "a2", "a1", "a0", "b0", "b1", "b2", "c2", "c1", "c0"
    ]
    # Bottom-left: row 2 LTR, row 1 RTL, row 0 LTR
    assert ids(ScanCoordinates._snake_from_rows(rows, False, True)) == [
        "c0", "c1", "c2", "b2", "b1", "b0", "a0", "a1", "a2"
    ]
    # Bottom-right: row 2 RTL, row 1 LTR, row 0 RTL
    assert ids(ScanCoordinates._snake_from_rows(rows, False, False)) == [
        "c2", "c1", "c0", "b0", "b1", "b2", "a2", "a1", "a0"
    ]


def test_snake_from_rows_skips_empty_rows():
    """Empty rows (e.g., Circle filtering removing a whole row) don't break alternation."""
    rows = [
        [],
        [("a0", 0, 0), ("a1", 1, 0)],
        [],
        [("b0", 0, 1), ("b1", 1, 1)],
    ]

    path = ScanCoordinates._snake_from_rows(rows, True, True)
    ids = [p[0] for p in path]
    # Only non-empty rows participate; alternation is on surviving rows
    assert ids == ["a0", "a1", "b1", "b0"]


def test_apply_snake_continuity_wellplate_grid():
    """DP-optimal corners for 2x2 wells in a serpentine region order.

    Each well has a 2x2 FOV grid (even-row snake: entry and exit are on the same
    column). Because the DP is forward-looking it picks A1's starting corner with
    knowledge that A2 sits to the east — greedy TL at A1 would force a longer
    A1→A2 jump than BR at A1 does.
    """
    sc = _make_scan_coordinates()
    sc.fov_pattern = "S-Pattern"
    sc.acquisition_pattern = "S-Pattern"

    def make_rows(cx, cy):
        return [
            [(cx - 0.5, cy - 0.5), (cx + 0.5, cy - 0.5)],
            [(cx - 0.5, cy + 0.5), (cx + 0.5, cy + 0.5)],
        ]

    wells = {"A1": (0, 0), "A2": (10, 0), "B1": (0, 10), "B2": (10, 10)}
    for wid, (cx, cy) in wells.items():
        rows = make_rows(cx, cy)
        sc.region_centers[wid] = [float(cx), float(cy), 0.0]
        sc.region_fov_rows[wid] = rows
        sc.region_fov_coordinates[wid] = sc._snake_from_rows(rows, True, True)

    sc.sort_coordinates()

    assert list(sc.region_centers.keys()) == ["A1", "A2", "B2", "B1"]

    # DP solution (total squared inter-region travel = 3*81 = 243):
    #   A1 BR → exit (0.5, -0.5) → A2 TL (9.5, -0.5)
    #   A2 exit (9.5,  0.5) → B2 TL (9.5, 9.5)
    #   B2 exit (9.5, 10.5) → B1 BR (0.5, 10.5)
    a1 = sc.region_fov_coordinates["A1"]
    assert a1[0] == (0.5, 0.5)
    assert a1[-1] == (0.5, -0.5)

    a2 = sc.region_fov_coordinates["A2"]
    assert a2[0] == (9.5, -0.5)
    assert a2[-1] == (9.5, 0.5)

    b2 = sc.region_fov_coordinates["B2"]
    assert b2[0] == (9.5, 9.5)
    assert b2[-1] == (9.5, 10.5)

    b1 = sc.region_fov_coordinates["B1"]
    assert b1[0] == (0.5, 10.5)
    assert b1[-1] == (0.5, 9.5)


def test_apply_snake_continuity_odd_row_parity():
    """Odd row counts exit diagonally; DP picks entry corners accordingly.

    With two 3x3 wells side by side, A1's right-side-entry snake exits diagonally
    at its own left side (odd parity), which the DP exploits so that A2's entry
    lies closer to A1's exit than a naive same-column choice.
    """
    sc = _make_scan_coordinates()
    sc.fov_pattern = "S-Pattern"
    sc.acquisition_pattern = "S-Pattern"

    def make_rows(cx, cy):
        # 3x3 grid, unit step, centered on (cx, cy). Row 0 is y=cy-1, row 2 is y=cy+1.
        return [
            [(cx - 1.0, cy + dy), (cx + 0.0, cy + dy), (cx + 1.0, cy + dy)]
            for dy in (-1.0, 0.0, 1.0)
        ]

    for wid, (cx, cy) in {"A1": (0.0, 0.0), "A2": (10.0, 0.0)}.items():
        rows = make_rows(cx, cy)
        sc.region_centers[wid] = [cx, cy, 0.0]
        sc.region_fov_rows[wid] = rows
        sc.region_fov_coordinates[wid] = sc._snake_from_rows(rows, True, True)

    sc.sort_coordinates()

    # A1 optimal entry = TR (1, -1): exits diagonally at BL (-1, 1) is wrong;
    # odd parity means TR exits at (- to the other corner). Concretely, TR entry
    # row 0 RTL → row 1 LTR → row 2 RTL, ending at rows[2][0] = (-1, 1).
    # But the DP will pick the start corner that gets A1's exit CLOSEST to A2.
    # A2 is to the east, so A1's exit should be on the east side. With odd parity,
    # the east-side exit comes from a west-side entry: BL entry (-1, 1) exits at
    # rows[0][-1] = (1, -1), or TL entry (-1, -1) exits at rows[-1][-1] = (1, 1).
    a1 = sc.region_fov_coordinates["A1"]
    assert a1[-1][0] == 1.0  # exit on east side of A1
    # A2's entry is closest to A1's exit (east side of A1 → west side of A2).
    a2 = sc.region_fov_coordinates["A2"]
    assert a2[0][0] == 9.0  # entry on west side of A2
    # A1 exit and A2 entry have same y (diagonal snake preserves y parity of column),
    # so the inter-region jump is purely horizontal.
    assert a1[-1][1] == a2[0][1]


def test_apply_snake_continuity_noop_when_unidirectional():
    """When fov_pattern is not S-Pattern, continuity is a no-op."""
    sc = _make_scan_coordinates()
    sc.fov_pattern = "Unidirectional"

    rows_a = [[(0.0, 0.0), (1.0, 0.0)], [(0.0, 1.0), (1.0, 1.0)]]
    rows_b = [[(10.0, 0.0), (11.0, 0.0)], [(10.0, 1.0), (11.0, 1.0)]]
    sc.region_centers["A1"] = [0.5, 0.5, 0.0]
    sc.region_centers["A2"] = [10.5, 0.5, 0.0]
    sc.region_fov_rows = {"A1": rows_a, "A2": rows_b}
    sc.region_fov_coordinates = {
        "A1": [fov for row in rows_a for fov in row],
        "A2": [fov for row in rows_b for fov in row],
    }

    before_a2 = list(sc.region_fov_coordinates["A2"])
    sc.sort_coordinates()
    assert sc.region_fov_coordinates["A2"] == before_a2


def test_apply_snake_continuity_skips_regions_without_rows():
    """Manual/single-FOV regions (no region_fov_rows entry) stay in their order,
    but their trailing FOV still seeds the next region's corner pick."""
    sc = _make_scan_coordinates()
    sc.fov_pattern = "S-Pattern"
    sc.acquisition_pattern = "S-Pattern"

    # Manual region: drawn shape, no row grid, flat ordered FOV list. Last FOV is far to
    # the right so the nearest A1 corner is unambiguous.
    manual_fovs = [(100.0, 0.0), (100.0, 1.0), (101.0, 1.0), (105.0, 0.0)]
    sc.region_centers["manual0"] = [100.5, 0.5]
    sc.region_fov_coordinates["manual0"] = manual_fovs
    # Intentionally no region_fov_rows["manual0"]

    # Well region near where the manual region ends.
    well_rows = [
        [(100.5, 10.0), (101.5, 10.0)],
        [(100.5, 11.0), (101.5, 11.0)],
    ]
    sc.region_centers["A1"] = [101.0, 10.5, 0.0]
    sc.region_fov_rows["A1"] = well_rows
    sc.region_fov_coordinates["A1"] = sc._snake_from_rows(well_rows, True, True)

    sc.sort_coordinates()

    # Manual region unchanged.
    assert sc.region_fov_coordinates["manual0"] == manual_fovs

    # A1 entry corner chosen based on manual region's trailing FOV (105.0, 0.0):
    # closest corner of A1 is top-right (101.5, 10.0).
    a1 = sc.region_fov_coordinates["A1"]
    assert a1[0] == (101.5, 10.0)
