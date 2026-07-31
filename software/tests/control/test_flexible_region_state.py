"""Per-region state on the Flexible Multipoint tab: renaming, and the laser-AF
references that have to survive a region rebuild.

The GUI classes are far too entangled to build here without hardware, so these tests
exercise the widget methods against a light harness that provides exactly the state
they touch: the real ``QTableWidget``/``QComboBox`` they edit, a real
``ScanCoordinates`` (backed by mocks), and the widget's own bookkeeping.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from qtpy.QtWidgets import QComboBox, QTableWidget, QTableWidgetItem

from control.core.multi_point_utils import ScanPositionInformation
from control.core.scan_coordinates import ScanCoordinates
from gui.widgets.multipoint import FlexibleMultiPointWidget


class _RegionHarness:
    """Just enough of FlexibleMultiPointWidget to drive its per-region bookkeeping."""

    _location_label = staticmethod(FlexibleMultiPointWidget._location_label)
    _refresh_location_label = FlexibleMultiPointWidget._refresh_location_label
    _set_name_cell = FlexibleMultiPointWidget._set_name_cell
    _rename_region_from_cell = FlexibleMultiPointWidget._rename_region_from_cell
    cell_was_changed = FlexibleMultiPointWidget.cell_was_changed
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

        # update_fov_positions reads these; the tile geometry itself is not under test.
        self.use_overlap = True
        self.entry_NX = SimpleNamespace(value=lambda: 2)
        self.entry_NY = SimpleNamespace(value=lambda: 2)
        self.entry_overlap = SimpleNamespace(value=lambda: 0)

        self.dropdown_location_list = QComboBox()
        self.table_location_list = QTableWidget(len(names), 5)
        for row, (name, (x, y, z)) in enumerate(zip(names, coords)):
            self.dropdown_location_list.addItem(self._location_label(name, x, y, z))
            self.table_location_list.setItem(row, 0, QTableWidgetItem(str(x)))
            self.table_location_list.setItem(row, 1, QTableWidgetItem(str(y)))
            self.table_location_list.setItem(row, 2, QTableWidgetItem(str(z * 1000)))
            self.table_location_list.setItem(row, 3, QTableWidgetItem(name))

    def type_name(self, row, text):
        """Simulate the user editing the Region Name cell and pressing Enter."""
        self.table_location_list.blockSignals(True)
        self.table_location_list.setItem(row, 3, QTableWidgetItem(text))
        self.table_location_list.blockSignals(False)
        self.cell_was_changed(row, 3)

    def name_cell(self, row):
        return self.table_location_list.item(row, 3).text()

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
    qtbot.addWidget(h.dropdown_location_list)
    return h


def test_rename_updates_ids_scan_coordinates_and_label(harness):
    harness.type_name(1, "liver section")

    assert list(harness.location_ids) == ["R0", "liver section", "R2"]
    assert list(harness.scanCoordinates.region_centers.keys()) == ["R0", "liver section", "R2"]
    assert harness.name_cell(1) == "liver section"
    assert harness.dropdown_location_list.itemText(1).startswith("liver section")


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
