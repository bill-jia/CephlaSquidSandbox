"""Per-region state on the Flexible Multipoint tab: renaming, and the laser-AF
references that have to survive a region rebuild.

The GUI classes are far too entangled to build here without hardware, so these tests
exercise the widget methods against a light harness that provides exactly the state
they touch: the real ``QTableWidget``/``QComboBox`` they edit, a real
``ScanCoordinates`` (backed by mocks), and the widget's own bookkeeping.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import numpy as np
import pandas as pd
import pytest
from qtpy.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDoubleSpinBox,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
)

import control._def
from control.core.multi_point_utils import ScanPositionInformation
from control.core.scan_coordinates import ScanCoordinates
from gui.widgets.multipoint import FlexibleMultiPointWidget, _push_tiling_grid_to_controller


class _RegionHarness:
    """Just enough of FlexibleMultiPointWidget to drive its per-region bookkeeping."""

    # The table's column order is part of the widget's contract; take it from the
    # widget rather than restating it, so a reorder can't silently desync the harness.
    _COL_X = FlexibleMultiPointWidget._COL_X
    _COL_Y = FlexibleMultiPointWidget._COL_Y
    _COL_Z = FlexibleMultiPointWidget._COL_Z
    _COL_AF_REF = FlexibleMultiPointWidget._COL_AF_REF
    _COL_NAME = FlexibleMultiPointWidget._COL_NAME
    _COL_COUNT = FlexibleMultiPointWidget._COL_COUNT

    _position_cells = staticmethod(FlexibleMultiPointWidget._position_cells)
    _refresh_position_cells = FlexibleMultiPointWidget._refresh_position_cells
    _selected_row = FlexibleMultiPointWidget._selected_row
    _select_row = FlexibleMultiPointWidget._select_row
    _set_name_cell = FlexibleMultiPointWidget._set_name_cell
    _rename_region_from_cell = FlexibleMultiPointWidget._rename_region_from_cell
    cell_was_changed = FlexibleMultiPointWidget.cell_was_changed
    cell_was_clicked = FlexibleMultiPointWidget.cell_was_clicked
    go_to = FlexibleMultiPointWidget.go_to
    _move_stage_to_position = FlexibleMultiPointWidget._move_stage_to_position
    next = FlexibleMultiPointWidget.next
    prev = FlexibleMultiPointWidget.prev
    _af_ref_item = FlexibleMultiPointWidget._af_ref_item
    _set_af_ref_cell_for_region = FlexibleMultiPointWidget._set_af_ref_cell_for_region
    _store_region_reference = FlexibleMultiPointWidget._store_region_reference
    _restore_region_references = FlexibleMultiPointWidget._restore_region_references
    update_fov_positions = FlexibleMultiPointWidget.update_fov_positions

    def __init__(self, scan_coordinates, names, coords):
        self.scanCoordinates = scan_coordinates
        self.location_ids = np.array(names, dtype=object)
        self.location_list = np.array(coords, dtype=float)
        self._region_obs_state_map = None
        self._region_laser_af_references = {}
        self._log = MagicMock()
        self.navigationViewer = MagicMock()
        self.focusMapWidget = MagicMock()
        self.multipointController = MagicMock()
        self.multipointController.acquisition_in_progress.return_value = False
        self.stage = MagicMock()

        # The two stage-motion toggles, at their shipped defaults.
        self.checkbox_moveStageOnClick = QCheckBox("Move stage on click")
        self.checkbox_moveStageOnClick.setChecked(True)
        self.checkbox_retractZBetweenRegions = QCheckBox("Retract Z for XY moves")
        self.checkbox_retractZBetweenRegions.setChecked(True)

        # update_fov_positions reads these; the tile geometry itself is not under test.
        self.use_overlap = True
        self.entry_NX = SimpleNamespace(value=lambda: 2)
        self.entry_NY = SimpleNamespace(value=lambda: 2)
        self.entry_overlap = SimpleNamespace(value=lambda: 0)

        self.table_location_list = QTableWidget(len(names), self._COL_COUNT)
        self.table_location_list.setSelectionBehavior(QAbstractItemView.SelectRows)
        for row, (name, (x, y, z)) in enumerate(zip(names, coords)):
            for col, item in self._position_cells(x, y, z).items():
                self.table_location_list.setItem(row, col, item)
            self.table_location_list.setItem(row, self._COL_NAME, QTableWidgetItem(name))
            self.table_location_list.setItem(row, self._COL_AF_REF, self._af_ref_item(None))

    def type_name(self, row, text):
        """Simulate the user editing the Region Name cell and pressing Enter."""
        self.table_location_list.blockSignals(True)
        self.table_location_list.setItem(row, self._COL_NAME, QTableWidgetItem(text))
        self.table_location_list.blockSignals(False)
        self.cell_was_changed(row, self._COL_NAME)

    def name_cell(self, row):
        return self.table_location_list.item(row, self._COL_NAME).text()

    def af_ref_cell(self, row):
        return self.table_location_list.item(row, self._COL_AF_REF).text()

    def isVisible(self):
        return True  # update_fov_positions bails out on a hidden widget


@pytest.fixture
def harness(qtbot):
    objective_store = MagicMock()
    objective_store.get_pixel_size_factor.return_value = 1.0
    camera = MagicMock()
    camera.get_fov_size_mm.return_value = (1.0, 1.0)
    stage = MagicMock()
    stage.get_pos.return_value = SimpleNamespace(x_mm=0.0, y_mm=0.0, z_mm=0.0)

    sc = ScanCoordinates(objective_store, stage, camera)
    names = ["R0", "R1", "R2"]
    coords = [(10.0, 10.0, 0.5), (20.0, 20.0, 0.5), (30.0, 30.0, 0.5)]
    for name, (x, y, z) in zip(names, coords):
        sc.add_flexible_region(name, x, y, z, 2, 2, overlap_percent=0)

    h = _RegionHarness(sc, names, coords)
    qtbot.addWidget(h.table_location_list)
    return h


def test_rename_updates_ids_scan_coordinates_and_cell(harness):
    harness.type_name(1, "liver section")

    assert list(harness.location_ids) == ["R0", "liver section", "R2"]
    assert list(harness.scanCoordinates.region_centers.keys()) == ["R0", "liver section", "R2"]
    assert harness.name_cell(1) == "liver section"


def test_clicking_a_row_selects_it_and_moves_the_stage(harness):
    """The table is the selection model now that it lives in the tab, so a click has
    to do what picking from the old dropdown did."""
    harness.cell_was_clicked(2, 0)

    assert harness._selected_row() == 2
    harness.stage.move_x_to.assert_called_once_with(30.0)
    harness.stage.move_y_to.assert_called_once_with(30.0)


def test_clicking_a_row_with_move_on_click_off_only_selects(harness):
    """"Move stage on click" off makes the table a selection list: the user can pick a
    row to rename it, or to set its Z, without the objective going anywhere."""
    harness.checkbox_moveStageOnClick.setChecked(False)

    harness.cell_was_clicked(2, 0)

    assert harness._selected_row() == 2
    assert harness.stage.mock_calls == []


def test_go_to_retracts_z_before_the_xy_move(harness):
    """With the retract on, Z goes up *first* and blocking - an XY move issued
    alongside a Z move would sweep the objective across the sample at working height."""
    harness.go_to(1)

    assert harness.stage.mock_calls == [
        call.move_z_to(control._def.OBJECTIVE_RETRACTED_POS_MM),
        call.move_x_to(20.0),
        call.move_y_to(20.0),
        call.move_z_to(0.5),
    ]


def test_go_to_without_the_retract_moves_xy_then_z(harness):
    harness.checkbox_retractZBetweenRegions.setChecked(False)

    harness.go_to(1)

    assert harness.stage.mock_calls == [
        call.move_x_to(20.0),
        call.move_y_to(20.0),
        call.move_z_to(0.5),
    ]


def test_next_wraps_around_the_selection(harness):
    harness._select_row(2)
    harness.next()

    assert harness._selected_row() == 0
    harness.stage.move_x_to.assert_called_once_with(10.0)


def test_prev_wraps_around_the_selection(harness):
    harness._select_row(0)
    harness.prev()

    assert harness._selected_row() == 2
    harness.stage.move_x_to.assert_called_once_with(30.0)


def test_prev_with_no_selection_goes_to_the_last_row(harness):
    """Next starts at the top of an unselected list, so Prev starts at the bottom."""
    assert harness._selected_row() == -1
    harness.prev()

    assert harness._selected_row() == 2
    harness.stage.move_x_to.assert_called_once_with(30.0)


def test_af_ref_cell_tracks_the_stored_reference(harness):
    """The AF Ref column sits between z and the name; writing it by the wrong index
    would overwrite a region's name with a focus-spot position."""
    assert harness.af_ref_cell(1) == "—"

    harness._set_af_ref_cell_for_region("R1", SimpleNamespace(x_reference=222.0))

    assert harness.af_ref_cell(1) == "222.0"
    assert harness.name_cell(1) == "R1", "the name column must be untouched"


def test_actions_are_no_ops_without_a_selection(harness):
    """currentRow() is -1 until something is selected; the old dropdown could never
    report that, and an unguarded -1 index silently addressed the *last* position."""
    assert harness._selected_row() == -1
    harness.go_to(-1)
    harness.stage.move_x_to.assert_not_called()


def test_rename_longer_than_twenty_chars_is_not_truncated(harness):
    """location_ids used to be a "<U20" array, which silently clipped the name and
    de-synchronised the table from the scanCoordinates keys."""
    long_name = "cortex_slice_replicate_04_left_hemisphere"
    harness.type_name(0, long_name)

    assert harness.location_ids[0] == long_name
    assert long_name in harness.scanCoordinates.region_centers


def test_rename_retags_focus_points(harness):
    """A "By Region" focus-map fit refuses to run unless the focus points' region tags
    match the scan regions exactly, so the rename has to reach them too."""
    harness.type_name(1, "middle")

    harness.focusMapWidget.rename_region.assert_called_once_with("R1", "middle")


def test_rename_carries_the_per_point_channel_map(harness):
    harness._region_obs_state_map = {"R0": ["BF"], "R1": ["BF", "GFP"], "R2": ["GFP"]}

    harness.type_name(1, "middle")

    # The renamed region keeps its own channel subset instead of falling back to the
    # global list, and no stale key is left describing a region that no longer exists.
    assert harness._region_obs_state_map == {"R0": ["BF"], "middle": ["BF", "GFP"], "R2": ["GFP"]}


@pytest.mark.parametrize("bad_name", ["R2", "r2", "", "   ", "sub/dir", "NUL", "x" * 60])
def test_invalid_rename_is_rejected_and_reverted(harness, bad_name):
    with patch("gui.widgets.multipoint.QMessageBox.warning") as warn:
        harness.type_name(1, bad_name)

    warn.assert_called_once()
    assert harness.name_cell(1) == "R1", "cell must revert to the previous name"
    assert list(harness.location_ids) == ["R0", "R1", "R2"]
    assert list(harness.scanCoordinates.region_centers.keys()) == ["R0", "R1", "R2"]


def test_rename_survives_the_acquisition_start_retile(harness):
    """The re-tile that runs at acquisition start replays region_generation_params;
    a half-applied rename would make it scan the same spot under both names."""
    harness.type_name(1, "middle")
    harness.scanCoordinates.regenerate_for_fov(0.5, 0.5)

    assert list(harness.scanCoordinates.region_centers.keys()) == ["R0", "middle", "R2"]
    assert list(harness.scanCoordinates.region_fov_coordinates.keys()) == ["R0", "middle", "R2"]


def test_renamed_region_reaches_the_acquisition_sidecars(tmp_path, harness):
    """The name the user typed is what lands in coordinates.csv, acquisition.yaml's
    position list and region_laser_af_references.csv — all of which are built from the
    ScanPositionInformation snapshot taken at acquisition start."""
    harness.scanCoordinates.set_region_laser_af_reference("R1", SimpleNamespace(x_reference=123.4, z_reference=None))
    harness.type_name(1, "liver section")

    info = ScanPositionInformation.from_scan_coordinates(harness.scanCoordinates)
    assert info.scan_region_names == ["R0", "liver section", "R2"]
    assert "liver section" in info.scan_region_fov_coords_mm
    assert "liver section" in info.scan_region_laser_af_references
    assert "R1" not in info.scan_region_laser_af_references

    # coordinates.csv is written straight from scan_region_fov_coords_mm.
    rows = [
        {"region": region_id, "x (mm)": c[0], "y (mm)": c[1]}
        for region_id, coords in info.scan_region_fov_coords_mm.items()
        for c in coords
    ]
    csv_path = tmp_path / "coordinates.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False)
    written = pd.read_csv(csv_path)
    assert list(written["region"].unique()) == ["R0", "liver section", "R2"]


def test_whitespace_only_edit_is_normalized_not_renamed(harness):
    harness.type_name(1, "  R1  ")

    assert harness.name_cell(1) == "R1"
    assert list(harness.scanCoordinates.region_centers.keys()) == ["R0", "R1", "R2"]


def test_rename_rejected_while_an_acquisition_is_running(harness):
    """The worker snapshots region names at start, so a mid-run rename would only
    desync the GUI from the folder names actually being written."""
    harness.multipointController.acquisition_in_progress.return_value = True

    with patch("gui.widgets.multipoint.QMessageBox.warning") as warn:
        harness.type_name(1, "middle")

    warn.assert_called_once()
    assert harness.name_cell(1) == "R1"
    assert list(harness.scanCoordinates.region_centers.keys()) == ["R0", "R1", "R2"]


def test_rename_rejected_when_row_is_not_a_registered_region(harness):
    """Guards the template widget, whose table rows don't key regions of their own."""
    harness.location_ids[1] = "not-a-region"

    with patch("gui.widgets.multipoint.QMessageBox.warning") as warn:
        harness.type_name(1, "whatever")

    warn.assert_called_once()
    assert "whatever" not in harness.scanCoordinates.region_centers


# --------------------------------------------------------------------------------------
# Per-region laser-AF references must survive a region rebuild
# --------------------------------------------------------------------------------------


def _ref(x):
    return SimpleNamespace(x_reference=x, z_reference=None)


def test_references_survive_a_tile_rebuild(harness):
    """Changing Nx/Ny/overlap (or the objective) rebuilds every region through
    clear_regions, which drops scanCoordinates' copy of the laser-AF targets."""
    harness._store_region_reference("R0", _ref(100.0))
    harness._store_region_reference("R2", _ref(300.0))

    harness.update_fov_positions()

    assert harness.scanCoordinates.get_region_laser_af_reference("R0").x_reference == 100.0
    assert harness.scanCoordinates.get_region_laser_af_reference("R2").x_reference == 300.0
    assert harness.scanCoordinates.get_region_laser_af_reference("R1") is None


def test_references_survive_a_tab_switch(harness):
    """MainWindow.onTabChanged clears every region before asking the widget to rebuild,
    so the references are already gone by the time update_fov_positions runs."""
    harness._store_region_reference("R1", _ref(222.0))

    harness.scanCoordinates.clear_regions()  # what onTabChanged does first
    assert harness.scanCoordinates.get_region_laser_af_reference("R1") is None
    harness.update_fov_positions()

    assert harness.scanCoordinates.get_region_laser_af_reference("R1").x_reference == 222.0


def test_restore_prunes_references_for_deleted_positions(harness):
    harness._store_region_reference("R0", _ref(100.0))
    harness._store_region_reference("R1", _ref(200.0))

    # Position R1 removed from the list (as remove_location does).
    harness.location_ids = np.array(["R0", "R2"], dtype=object)
    harness.location_list = np.array([(10.0, 10.0, 0.5), (30.0, 30.0, 0.5)], dtype=float)
    harness.scanCoordinates.remove_region("R1")
    harness.update_fov_positions()

    assert set(harness._region_laser_af_references) == {"R0"}
    assert "R1" not in harness.scanCoordinates.region_laser_af_references


def test_renamed_region_keeps_its_reference_across_a_rebuild(harness):
    """The rename moves scanCoordinates' copy, but the durable store is what gets
    re-applied on the next rebuild — so it has to be rekeyed too."""
    harness._store_region_reference("R1", _ref(222.0))

    harness.type_name(1, "liver section")
    harness.update_fov_positions()

    assert harness._region_laser_af_references["liver section"].x_reference == 222.0
    assert "R1" not in harness._region_laser_af_references
    assert harness.scanCoordinates.get_region_laser_af_reference("liver section").x_reference == 222.0


def test_restored_references_reach_the_acquisition_snapshot(harness):
    """The worker reads the references off ScanPositionInformation, and the pre-flight
    "every region has a reference" check reads scanCoordinates directly."""
    for name in ("R0", "R1", "R2"):
        harness._store_region_reference(name, _ref(1.0))

    harness.scanCoordinates.clear_regions()
    harness.update_fov_positions()

    info = ScanPositionInformation.from_scan_coordinates(harness.scanCoordinates)
    assert set(info.scan_region_laser_af_references) == {"R0", "R1", "R2"}
    region_ids = list(harness.scanCoordinates.region_centers.keys())
    assert all(rid in harness.scanCoordinates.region_laser_af_references for rid in region_ids)


class _ZTimeHarness:
    """Just enough of a multipoint panel to drive the Z-stack / Time-lapse groups.

    Both panels mix in ``_ZTimeGroupMixin`` for this, so the methods are taken off
    ``FlexibleMultiPointWidget`` (the wellplate tab only renames the two checkboxes).
    """

    _apply_zstack_enabled = FlexibleMultiPointWidget._apply_zstack_enabled
    _apply_timelapse_enabled = FlexibleMultiPointWidget._apply_timelapse_enabled
    _effective_NZ = FlexibleMultiPointWidget._effective_NZ
    _effective_Nt = FlexibleMultiPointWidget._effective_Nt
    _compute_z_range = FlexibleMultiPointWidget._compute_z_range
    _zstack_checkbox = FlexibleMultiPointWidget._zstack_checkbox
    _timelapse_checkbox = FlexibleMultiPointWidget._timelapse_checkbox

    def __init__(self):
        self.multipointController = MagicMock()
        self.stage = MagicMock()
        self.stage.get_pos.return_value = SimpleNamespace(x_mm=0.0, y_mm=0.0, z_mm=0.0)

        self.checkbox_zstack = QCheckBox("Z-stack")
        self.checkbox_timelapse = QCheckBox("Time-lapse")
        self.checkbox_set_z_range = QCheckBox("Set Z-range")

        self.entry_NZ = QSpinBox()
        self.entry_NZ.setMaximum(2000)
        self.entry_NZ.setValue(1)
        self.entry_Nt = QSpinBox()
        self.entry_Nt.setMaximum(2000)
        self.entry_Nt.setValue(1)

        self.entry_deltaZ = QDoubleSpinBox()
        self.entry_dt = QDoubleSpinBox()

        self.entry_minZ = QDoubleSpinBox()
        self.entry_minZ.setRange(-100000, 100000)
        self.entry_maxZ = QDoubleSpinBox()
        self.entry_maxZ.setRange(-100000, 100000)

        self._zstack_controls = [self.entry_NZ, self.entry_deltaZ, self.checkbox_set_z_range]
        self._timelapse_controls = [self.entry_Nt, self.entry_dt]


@pytest.fixture
def z_time(qtbot):
    h = _ZTimeHarness()
    for widget in (h.checkbox_zstack, h.checkbox_timelapse, h.checkbox_set_z_range):
        qtbot.addWidget(widget)
    return h


def test_unchecked_zstack_pushes_one_plane(z_time):
    z_time.entry_NZ.setValue(11)
    z_time.checkbox_zstack.setChecked(False)

    z_time._apply_zstack_enabled()

    assert z_time._effective_NZ() == 1
    z_time.multipointController.set_NZ.assert_called_with(1)
    assert not z_time.entry_NZ.isEnabled()
    assert not z_time.entry_deltaZ.isEnabled()


def test_checked_zstack_pushes_the_spinbox_value(z_time):
    z_time.entry_NZ.setValue(11)
    z_time.checkbox_zstack.setChecked(True)

    z_time._apply_zstack_enabled()

    assert z_time._effective_NZ() == 11
    z_time.multipointController.set_NZ.assert_called_with(11)
    assert z_time.entry_NZ.isEnabled()


def test_unchecking_zstack_greys_but_does_not_uncheck_set_z_range(z_time):
    """Disabling the Z-stack group must only grey "Set Z-range", never uncheck it -
    unchecking fires toggle_z_range_controls, which overwrites entry_minZ/entry_maxZ
    with the current stage Z and collapses entry_NZ to 1 (F7)."""
    z_time.checkbox_zstack.setChecked(True)
    z_time.checkbox_set_z_range.setChecked(True)

    z_time.checkbox_zstack.setChecked(False)
    z_time._apply_zstack_enabled()

    assert z_time.checkbox_set_z_range.isChecked()
    assert not z_time.checkbox_set_z_range.isEnabled()


def test_disabling_and_reenabling_zstack_preserves_set_z_range_state(z_time):
    """What the user typed stays on screen (the mixin's own docstring contract):
    enabling the group, turning on Set Z-range, and typing a min/max survives a
    disable/re-enable of the whole group untouched (F7)."""
    z_time.checkbox_zstack.setChecked(True)
    z_time.checkbox_set_z_range.setChecked(True)
    z_time.entry_minZ.setValue(900.0)
    z_time.entry_maxZ.setValue(1100.0)
    z_time.entry_NZ.setValue(11)
    z_time._apply_zstack_enabled()

    z_time.checkbox_zstack.setChecked(False)
    z_time._apply_zstack_enabled()

    z_time.checkbox_zstack.setChecked(True)
    z_time._apply_zstack_enabled()

    assert z_time.checkbox_set_z_range.isChecked()
    assert z_time.entry_minZ.value() == pytest.approx(900.0)
    assert z_time.entry_maxZ.value() == pytest.approx(1100.0)
    assert z_time.entry_NZ.value() == 11


def test_set_z_range_keeps_nz_derived_when_the_group_is_on(z_time):
    z_time.checkbox_zstack.setChecked(True)
    z_time.checkbox_set_z_range.setChecked(True)

    z_time._apply_zstack_enabled()

    # Nz comes from the Z-min/Z-max span in this mode, so it must not be typeable.
    assert not z_time.entry_NZ.isEnabled()
    assert z_time.checkbox_set_z_range.isEnabled()


def test_unchecked_timelapse_pushes_one_timepoint(z_time):
    z_time.entry_Nt.setValue(7)
    z_time.checkbox_timelapse.setChecked(False)

    z_time._apply_timelapse_enabled()

    assert z_time._effective_Nt() == 1
    z_time.multipointController.set_Nt.assert_called_with(1)
    assert not z_time.entry_Nt.isEnabled()


def test_checked_timelapse_pushes_the_spinbox_value(z_time):
    z_time.entry_Nt.setValue(7)
    z_time.checkbox_timelapse.setChecked(True)

    z_time._apply_timelapse_enabled()

    assert z_time._effective_Nt() == 7
    z_time.multipointController.set_Nt.assert_called_with(7)
    assert z_time.entry_dt.isEnabled()


def test_compute_z_range_without_set_z_range_stays_in_millimeters(z_time):
    """entry_deltaZ is in um; the span must be converted to mm before being added
    to the stage's mm position, or it comes out ~1000x too large."""
    z_time.stage.get_pos.return_value = SimpleNamespace(x_mm=0.0, y_mm=0.0, z_mm=1.000)
    z_time.checkbox_set_z_range.setChecked(False)
    z_time.entry_deltaZ.setValue(2.0)  # um

    minZ, maxZ = z_time._compute_z_range(effective_NZ=5)

    assert minZ == pytest.approx(1.000, abs=1e-9)
    assert maxZ == pytest.approx(1.008, abs=1e-9)


def test_compute_z_range_with_set_z_range_uses_the_entries(z_time):
    z_time.stage.get_pos.return_value = SimpleNamespace(x_mm=0.0, y_mm=0.0, z_mm=1.000)
    z_time.checkbox_set_z_range.setChecked(True)
    z_time.entry_minZ.setValue(900.0)  # um
    z_time.entry_maxZ.setValue(1100.0)  # um

    minZ, maxZ = z_time._compute_z_range(effective_NZ=5)

    assert minZ == pytest.approx(0.900, abs=1e-9)
    assert maxZ == pytest.approx(1.100, abs=1e-9)


def test_push_tiling_grid_to_controller_pushes_the_current_spinbox_values(qtbot):
    """F10: FlexibleMultiPointWidget.toggle_acquisition must re-push NX/NY at Start, or
    a Flexible run after a Wellplate one (which sets the shared controller to 1x1 for a
    fraction-of-well run) records a stale nx: 1, ny: 1 in acquisition.yaml."""
    entry_NX = QSpinBox()
    entry_NX.setMaximum(50)
    entry_NX.setValue(4)
    entry_NY = QSpinBox()
    entry_NY.setMaximum(50)
    entry_NY.setValue(7)
    qtbot.addWidget(entry_NX)
    qtbot.addWidget(entry_NY)

    widget = SimpleNamespace(entry_NX=entry_NX, entry_NY=entry_NY, multipointController=MagicMock())

    _push_tiling_grid_to_controller(widget)

    widget.multipointController.set_NX.assert_called_once_with(4)
    widget.multipointController.set_NY.assert_called_once_with(7)
