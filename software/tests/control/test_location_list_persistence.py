"""CSV persistence through the actual widget methods and shipped Z-mode labels."""

import json
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from control.models import LaserAFReference
from tests.control.test_flexible_region_state import harness
from tests.control.test_region_z_stacks import _enable_persistence


@pytest.fixture
def persistence(harness, tmp_path, monkeypatch):
    h = _enable_persistence(harness)
    h.entry_deltaZ.setRange(0.001, 1000)
    h.entry_deltaZ.setDecimals(3)
    path = tmp_path / "positions.csv"
    monkeypatch.setattr("gui.widgets.multipoint.QFileDialog.getSaveFileName", lambda *args: (str(path), ""))
    monkeypatch.setattr("gui.widgets.multipoint.QFileDialog.getOpenFileName", lambda *args: (str(path), ""))
    h.import_error = MagicMock()
    monkeypatch.setattr("gui.widgets.multipoint.error_dialog", h.import_error)
    return h, path


@pytest.mark.parametrize(
    "mode,canonical,bounds",
    [
        (0, "FROM BOTTOM", (0.5, 0.504)),
        (1, "FROM CENTER", (0.498, 0.502)),
        (2, "FROM TOP", (0.496, 0.5)),
    ],
)
def test_modes_export_canonical_values_and_restore_physical_ranges(persistence, mode, canonical, bounds):
    h, path = persistence
    h.combobox_z_stack.setCurrentIndex(mode)
    assert h._region_z_bounds(0) == pytest.approx(bounds)
    h.export_location_list()
    df = pd.read_csv(path)
    assert df["z_stacking_config"].tolist() == [canonical] * 3
    assert tuple(df.loc[0, ["z_min (mm)", "z_max (mm)"]]) == pytest.approx(bounds)
    h.combobox_z_stack.setCurrentIndex((mode + 1) % 3)
    h.import_location_list()
    h.import_error.assert_not_called()
    assert h.combobox_z_stack.currentIndex() == mode
    assert h._region_z_bounds(0) == pytest.approx(bounds)
    h.multipointController.set_z_stacking_config.assert_called_with(mode)


@pytest.mark.parametrize(
    "mode,labels",
    [
        (0, ["FROM BOTTOM (Z-MIN)", "FROM BOTTOM", " from bottom (z-min) "]),
        (1, ["FROM CENTER", "From Center", " from center "]),
        (2, ["FROM TOP (Z-MAX)", "FROM TOP", " from top (z-max) "]),
    ],
)
def test_legacy_csv_labels_are_accepted_and_select_correct_mode(persistence, mode, labels):
    h, path = persistence
    h.export_location_list()
    df = pd.read_csv(path)
    # Legacy exports have no explicit shared count or enabled flag.
    df = df.drop(columns=["default_z_count", "z_stack_enabled"])
    df["z_stacking_config"] = labels
    df.to_csv(path, index=False)
    h.import_location_list()
    h.import_error.assert_not_called()
    assert h.combobox_z_stack.currentIndex() == mode
    assert list(h.location_ids) == ["R0", "R1", "R2"]
    assert all(h._region_z_bounds(row) == pytest.approx((0.5, 0.504)) for row in range(3))


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("mode", [0, 1, 2])
def test_stack_switch_defaults_and_overrides_roundtrip(persistence, enabled, mode):
    h, path = persistence
    h.combobox_z_stack.setCurrentIndex(mode)
    h.entry_NZ.setValue(7)
    # Every position is overridden, so shared Nz cannot be inferred from bounds.
    ranges = {"R0": (0.498, 0.508), "R1": (0.5, 0.502), "R2": (0.49, 0.5)}
    h._region_z_ranges = dict(ranges)
    h._zstack_checkbox.setChecked(enabled)
    h.export_location_list()
    h.entry_NZ.setValue(20)
    h.entry_deltaZ.setValue(1)
    h._zstack_checkbox.setChecked(not enabled)
    h.import_location_list()
    h.import_error.assert_not_called()
    assert h._zstack_checkbox.isChecked() == enabled
    assert h._region_z_ranges == ranges
    assert h.entry_NZ.value() == 7
    assert h.entry_deltaZ.value() == 2
    assert h._effective_NZ() == (6 if enabled else 1)
    assert bool(h.scanCoordinates.region_z_ranges) == enabled


@pytest.mark.parametrize("names", [["001", "002", "003"], ["NA", "None", "nan"]])
def test_names_and_full_autofocus_references_roundtrip(persistence, names):
    h, path = persistence
    for row, name in enumerate(names):
        h.type_name(row, name)
    crop = np.arange(12, dtype=np.float32).reshape(3, 4)
    h._store_region_reference(names[0], LaserAFReference.from_capture(123.4, crop))
    h.export_location_list()
    h.import_location_list()
    h.import_error.assert_not_called()
    assert list(h.location_ids) == names
    reference = h._region_laser_af_references[names[0]]
    assert reference.x_reference == 123.4
    np.testing.assert_array_equal(reference.reference_crop, crop)
    assert h.scanCoordinates.region_laser_af_references[names[0]] == reference


def test_same_xy_different_z_and_duplicate_names_preserve_all_rows(persistence):
    h, path = persistence
    h.export_location_list()
    df = pd.read_csv(path)
    df["x (mm)"] = 10
    df["y (mm)"] = 20
    df["z (mm)"] = [0.5, 0.6, 0.7]
    df["ID"] = ["tissue", "tissue", "unsafe/name"]
    df.to_csv(path, index=False)
    h.import_location_list()
    h.import_error.assert_not_called()
    assert len(h.location_ids) == 3
    assert len(set(h.location_ids)) == 3
    assert len(h.scanCoordinates.region_centers) == 3
    assert h.location_list[:, 2].tolist() == [0.5, 0.6, 0.7]
    assert h.stage.mock_calls == []


def test_reexport_without_references_replaces_old_sidecar(persistence):
    h, path = persistence
    h._store_region_reference("R0", LaserAFReference.from_capture(123.4, None))
    h.export_location_list()
    h._region_laser_af_references.clear()
    h.export_location_list()
    assert json.loads(path.with_suffix(".laser_af.json").read_text()) == {}
    h.import_location_list()
    h.import_error.assert_not_called()
    assert h._region_laser_af_references == {}
    assert h.scanCoordinates.region_laser_af_references == {}


@pytest.mark.parametrize("contents", ["null", "[]", '"R0"', "{invalid json", '{"R0": {"bad": 1}}'])
def test_bad_sidecar_falls_back_to_csv_autofocus_reference(persistence, contents):
    h, path = persistence
    h._store_region_reference("R0", LaserAFReference.from_capture(123.4, None))
    h.export_location_list()
    path.with_suffix(".laser_af.json").write_text(contents)
    h.import_location_list()
    h.import_error.assert_not_called()
    assert h._region_laser_af_references["R0"].x_reference == 123.4


@pytest.mark.parametrize(
    "column,value",
    [
        ("delta_z_um", "bad"),
        ("delta_z_um", ""),
        ("delta_z_um", float("inf")),
        ("delta_z_um", -1),
        ("delta_z_um", 1001),
        ("delta_z_um", 0.0001),
        ("delta_z_um", 1.2345),
        ("x (mm)", float("nan")),
        ("y (mm)", "bad"),
        ("z_min (mm)", 1),
        ("z_stacking_config", "unknown"),
        ("z_range_override", "maybe"),
        ("default_z_count", 2.5),
        ("default_z_count", 2001),
        ("z_stack_enabled", "maybe"),
    ],
)
def test_invalid_csv_preserves_existing_positions_and_settings(persistence, column, value):
    h, path = persistence
    h._region_z_ranges["R0"] = (0.498, 0.508)
    h._store_region_reference("R0", LaserAFReference.from_capture(123.4, None))
    h.export_location_list()
    df = pd.read_csv(path)
    df[column] = value
    df.to_csv(path, index=False)
    positions = h.location_list.copy()
    h.import_location_list()
    h.import_error.assert_called_once()
    np.testing.assert_array_equal(h.location_list, positions)
    assert list(h.location_ids) == ["R0", "R1", "R2"]
    assert h._region_z_ranges == {"R0": (0.498, 0.508)}
    assert h._region_laser_af_references["R0"].x_reference == 123.4
    assert h.entry_deltaZ.value() == 2
    assert h.entry_NZ.value() == 3
    assert h.combobox_z_stack.currentIndex() == 0
    assert not h.table_location_list.signalsBlocked()


@pytest.mark.parametrize("contents", ["", "x (mm),y (mm),z (mm)\n", "x (mm),y (mm)\n1,2\n"])
def test_empty_or_missing_columns_do_not_clear_positions(persistence, contents):
    h, path = persistence
    path.write_text(contents)
    h.import_location_list()
    h.import_error.assert_called_once()
    assert list(h.location_ids) == ["R0", "R1", "R2"]


def test_basic_legacy_csv_without_optional_fields(persistence):
    h, path = persistence
    path.write_text("x (mm),y (mm),z (mm)\n1,2,0.5\n3,4,0.6\n")
    h._region_obs_state_map = {"R0": ["old channel"]}
    h.import_location_list()
    h.import_error.assert_not_called()
    assert h.location_list.tolist() == [[1, 2, 0.5], [3, 4, 0.6]]
    assert h._region_obs_state_map is None
    assert list(h.location_ids) == ["R0", "R1"]
