"""The Wellplate tab's *Tiling per position* method selector.

``Fraction of well`` and ``Nx × Ny`` are two ways to tile the same selected wells,
and both go through ``ScanCoordinates`` methods that skip a well which already has
a region - so which generator runs, and that a re-tile clears first, is the whole
contract. The GUI class cannot be built without hardware, so these tests drive the
widget's own methods against a harness that carries exactly the controls they read.
"""

import os
from types import SimpleNamespace

import pytest
import yaml
from qtpy.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QListWidget,
    QRadioButton,
    QSpinBox,
)
from unittest.mock import MagicMock

from gui.widgets.multipoint import WellplateMultiPointWidget


class _TilingHarness:
    """Just enough of WellplateMultiPointWidget to drive its tiling / cache paths."""

    _tile_wells = WellplateMultiPointWidget._tile_wells
    _tile_live = WellplateMultiPointWidget._tile_live
    _on_tiling_method_changed = WellplateMultiPointWidget._on_tiling_method_changed
    update_coordinates = WellplateMultiPointWidget.update_coordinates
    update_well_coordinates = WellplateMultiPointWidget.update_well_coordinates
    save_multipoint_widget_config_to_cache = WellplateMultiPointWidget.save_multipoint_widget_config_to_cache
    load_multipoint_widget_config_from_cache = WellplateMultiPointWidget.load_multipoint_widget_config_from_cache

    def __init__(self):
        self._log = MagicMock()
        self.tab_widget = None
        # A recorder, not a real ScanCoordinates: these tests are about which
        # generator the widget picks, not about the tile maths (covered by
        # tests/control/core/test_scan_coordinates.py).
        self.scanCoordinates = MagicMock()
        self.scanCoordinates.has_regions.return_value = True
        self.stage = MagicMock()
        self.shapes_mm = None

        self.checkbox_xy = QCheckBox("XY")
        self.checkbox_xy.setChecked(True)
        self.combobox_xy_mode = QComboBox()
        self.combobox_xy_mode.addItems(["Current Position", "Select Wells", "Manual", "Load Coordinates"])
        self.combobox_xy_mode.setCurrentText("Select Wells")

        self.radio_tiling_fraction = QRadioButton("Fraction of well")
        self.radio_tiling_grid = QRadioButton("Nx × Ny")
        self.tiling_method_group = QButtonGroup()
        self.tiling_method_group.addButton(self.radio_tiling_fraction)
        self.tiling_method_group.addButton(self.radio_tiling_grid)
        self.radio_tiling_fraction.setChecked(True)

        self.entry_NX = QSpinBox()
        self.entry_NX.setMaximum(50)
        self.entry_NX.setValue(2)
        self.entry_NY = QSpinBox()
        self.entry_NY.setMaximum(50)
        self.entry_NY.setValue(3)
        self.entry_overlap = QDoubleSpinBox()
        self.entry_overlap.setRange(0, 99)
        self.entry_overlap.setValue(15.0)
        self.entry_scan_size = QDoubleSpinBox()
        self.entry_scan_size.setRange(0.1, 100)
        self.entry_scan_size.setValue(2.5)
        self.combobox_shape = QComboBox()
        self.combobox_shape.addItems(["Square", "Circle", "Rectangle"])

        # Read by the cache paths only.
        self.checkbox_z = QCheckBox("Z-stack")
        self.checkbox_set_z_range = QCheckBox("Set Z-range")
        self.checkbox_time = QCheckBox("Time-lapse")
        self.checkbox_withAutofocus = QCheckBox("Contrast AF")
        self.checkbox_withReflectionAutofocus = QCheckBox("Laser AF")
        self._enable_laser_autofocus = False
        self.entry_NZ = QSpinBox()
        self.entry_NZ.setMaximum(2000)
        self.entry_Nt = QSpinBox()
        self.entry_Nt.setMaximum(2000)
        self.entry_deltaZ = QDoubleSpinBox()
        self.entry_dt = QDoubleSpinBox()
        self.entry_dt.setMaximum(10000)
        self.list_configurations = QListWidget()
        self.stored_xy_params = {mode: {} for mode in ("Current Position", "Select Wells")}
        self._pending_cached_scan_size = None
        self._xy_mode_before_uncheck = None
        self._loading_from_cache = False

        # The load path re-applies UI state through these; the widgets they touch are
        # not what is under test here.
        self.update_scan_control_ui = MagicMock()
        self.toggle_z_range_controls = MagicMock()
        self._apply_zstack_enabled = MagicMock()
        self._apply_timelapse_enabled = MagicMock()

    # There is no label_size_estimate, so _refresh_size_estimate is a no-op.


@pytest.fixture
def tiling(qtbot):
    h = _TilingHarness()
    for widget in (h.radio_tiling_fraction, h.radio_tiling_grid, h.entry_NX, h.entry_NY, h.list_configurations):
        qtbot.addWidget(widget)
    return h


def _calls(harness):
    return [c[0] for c in harness.scanCoordinates.method_calls]


def test_grid_method_tiles_selected_wells_as_a_grid(tiling):
    tiling.radio_tiling_grid.setChecked(True)

    tiling.update_well_coordinates(True)

    assert _calls(tiling) == ["has_regions", "clear_regions", "set_well_coordinates_grid"]
    tiling.scanCoordinates.set_well_coordinates_grid.assert_called_once_with(2, 3, 15.0)
    tiling.scanCoordinates.set_well_coordinates.assert_not_called()


def test_fraction_method_tiles_selected_wells_by_scan_size(tiling):
    tiling.update_well_coordinates(True)

    assert _calls(tiling) == ["has_regions", "clear_regions", "set_well_coordinates"]
    tiling.scanCoordinates.set_well_coordinates.assert_called_once_with(2.5, 15.0, "Square")
    tiling.scanCoordinates.set_well_coordinates_grid.assert_not_called()


def test_deselecting_every_well_clears_instead_of_tiling(tiling):
    tiling.radio_tiling_grid.setChecked(True)

    tiling.update_well_coordinates(False)

    tiling.scanCoordinates.clear_regions.assert_called_once()
    tiling.scanCoordinates.set_well_coordinates_grid.assert_not_called()


def test_current_position_mode_tiles_the_stage_position(tiling):
    tiling.combobox_xy_mode.setCurrentText("Current Position")
    tiling.stage.get_pos.return_value = MagicMock(x_mm=11.0, y_mm=22.0)
    tiling.radio_tiling_grid.setChecked(True)

    tiling.update_coordinates()

    tiling.scanCoordinates.set_live_scan_coordinates_grid.assert_called_once_with(11.0, 22.0, 2, 3, 15.0)
    tiling.scanCoordinates.set_live_scan_coordinates.assert_not_called()


def test_current_position_mode_fraction_keeps_the_scan_size_call(tiling):
    tiling.combobox_xy_mode.setCurrentText("Current Position")
    tiling.stage.get_pos.return_value = MagicMock(x_mm=11.0, y_mm=22.0)

    tiling.update_coordinates()

    tiling.scanCoordinates.set_live_scan_coordinates.assert_called_once_with(11.0, 22.0, 2.5, 15.0, "Square")
    tiling.scanCoordinates.set_live_scan_coordinates_grid.assert_not_called()


def test_switching_method_rebuilds_the_regions(tiling):
    """The two methods share region ids, and the generators skip a well that already
    has one: without the rebuild, switching method would change nothing on screen."""
    tiling.update_well_coordinates(True)
    tiling.scanCoordinates.reset_mock()

    tiling.radio_tiling_grid.setChecked(True)
    tiling._on_tiling_method_changed()

    assert "clear_regions" in _calls(tiling)
    tiling.scanCoordinates.set_well_coordinates_grid.assert_called_once_with(2, 3, 15.0)


def test_manual_mode_ignores_the_tiling_method(tiling):
    """Manual shapes bring their own FOV list; only Overlap applies to them."""
    tiling.combobox_xy_mode.setCurrentText("Manual")
    tiling.radio_tiling_grid.setChecked(True)

    tiling.update_coordinates()

    tiling.scanCoordinates.set_manual_coordinates.assert_called_once_with(None, 15.0)
    tiling.scanCoordinates.set_well_coordinates_grid.assert_not_called()


@pytest.mark.parametrize("method_is_grid", [True, False])
def test_tiling_method_and_grid_size_round_trip_through_the_cache(tiling, tmp_path, monkeypatch, method_is_grid):
    monkeypatch.chdir(tmp_path)
    tiling.radio_tiling_grid.setChecked(method_is_grid)
    tiling.entry_NX.setValue(4)
    tiling.entry_NY.setValue(7)
    tiling.entry_overlap.setValue(12.5)

    tiling.save_multipoint_widget_config_to_cache()

    saved = yaml.safe_load(open(os.path.join("cache", "multipoint_widget_config.yaml")))
    assert saved["tiling_method"] == ("grid" if method_is_grid else "fraction")
    assert (saved["nx"], saved["ny"]) == (4, 7)

    # A fresh panel starts on the fraction method with a 1x1 grid.
    tiling.radio_tiling_fraction.setChecked(True)
    tiling.entry_NX.setValue(1)
    tiling.entry_NY.setValue(1)
    tiling.entry_overlap.setValue(10.0)

    tiling.load_multipoint_widget_config_from_cache()

    assert tiling.radio_tiling_grid.isChecked() == method_is_grid
    assert (tiling.entry_NX.value(), tiling.entry_NY.value()) == (4, 7)
    assert tiling.entry_overlap.value() == 12.5


class _ZRangeHarness:
    """Just enough of WellplateMultiPointWidget to drive ``_compute_z_range``.

    Both panels mix in ``_ZTimeGroupMixin`` for this shared helper - see the
    equivalent harness in test_flexible_region_state.py.
    """

    _compute_z_range = WellplateMultiPointWidget._compute_z_range

    def __init__(self):
        self.stage = MagicMock()
        self.stage.get_pos.return_value = SimpleNamespace(x_mm=0.0, y_mm=0.0, z_mm=1.000)

        self.checkbox_set_z_range = QCheckBox("Set Z-range")
        self.entry_deltaZ = QDoubleSpinBox()
        self.entry_deltaZ.setRange(-100000, 100000)
        self.entry_minZ = QDoubleSpinBox()
        self.entry_minZ.setRange(-100000, 100000)
        self.entry_maxZ = QDoubleSpinBox()
        self.entry_maxZ.setRange(-100000, 100000)


@pytest.fixture
def z_range(qtbot):
    h = _ZRangeHarness()
    qtbot.addWidget(h.checkbox_set_z_range)
    return h


def test_z_range_without_set_z_range_stays_in_millimeters(z_range):
    """Regression: entry_deltaZ is in um (spinbox suffix), stage z is in mm - the
    step must be converted before being added, or the span comes out ~1000x too
    large (e.g. 1.000 -> 9.000 mm instead of 1.000 -> 1.008 mm)."""
    z_range.checkbox_set_z_range.setChecked(False)
    z_range.entry_deltaZ.setValue(2.0)  # um

    minZ, maxZ = z_range._compute_z_range(effective_NZ=5)

    assert minZ == pytest.approx(1.000, abs=1e-9)
    assert maxZ == pytest.approx(1.008, abs=1e-9)


def test_z_range_with_set_z_range_uses_the_entries(z_range):
    z_range.checkbox_set_z_range.setChecked(True)
    z_range.entry_minZ.setValue(900.0)  # um
    z_range.entry_maxZ.setValue(1100.0)  # um

    minZ, maxZ = z_range._compute_z_range(effective_NZ=5)

    assert minZ == pytest.approx(0.900, abs=1e-9)
    assert maxZ == pytest.approx(1.100, abs=1e-9)
