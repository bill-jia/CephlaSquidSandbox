import pytest
import yaml

from control.acquisition_yaml_loader import parse_acquisition_yaml
from qtpy.QtWidgets import QDoubleSpinBox

from gui.widgets.tile_spacing import TileSpacingWidget
from tests.control.test_wellplate_tiling_method import _TilingHarness


def test_spacing_selector_visibility_and_values(qtbot):
    widget = TileSpacingWidget(QDoubleSpinBox())
    qtbot.addWidget(widget)
    widget.show()
    assert widget.parameters() == {}
    assert widget.overlap_row.isVisible()
    assert not widget.spacing_row.isVisible()
    assert not widget.mode.model().item(2).isEnabled()
    widget.mode.setCurrentIndex(1)
    assert not widget.overlap_row.isVisible()
    assert widget.distance[0].isVisible()
    widget.distance[1][0].setValue(2.5)
    assert widget.parameters() == dict(spacing_mode="distance", spacing_x=2.5, spacing_y=1)
    widget.units.setCurrentIndex(1)
    assert widget.fovs[0].isVisible()
    assert not widget.distance[0].isVisible()
    widget.fovs[1][1].setValue(3)
    params = widget.parameters()
    widget.restore({})
    assert widget.parameters() == {}
    widget.restore(params)
    assert widget.parameters() == params


def test_wellplate_spacing_reaches_grid_generators(qtbot):
    harness = _TilingHarness()
    harness.tile_spacing = TileSpacingWidget(harness.entry_overlap)
    qtbot.addWidget(harness.tile_spacing)
    harness.tile_spacing.restore(dict(spacing_mode="fov", spacing_x=2, spacing_y=3))
    harness.radio_tiling_grid.setChecked(True)
    harness._tile_wells()
    harness.scanCoordinates.set_well_coordinates_grid.assert_called_once_with(
        2, 3, 15, spacing_mode="fov", spacing_x=2, spacing_y=3
    )
    harness._tile_live(30, 40)
    harness.scanCoordinates.set_live_scan_coordinates_grid.assert_called_once_with(
        30, 40, 2, 3, 15, spacing_mode="fov", spacing_x=2, spacing_y=3
    )


@pytest.mark.parametrize("mode", ["distance", "fov"])
def test_spacing_cache_round_trip(qtbot, tmp_path, monkeypatch, mode):
    monkeypatch.chdir(tmp_path)
    harness = _TilingHarness()
    harness.tile_spacing = TileSpacingWidget(harness.entry_overlap)
    qtbot.addWidget(harness.tile_spacing)
    params = dict(spacing_mode=mode, spacing_x=2.5, spacing_y=3)
    harness.tile_spacing.restore(params)
    harness.radio_tiling_grid.setChecked(True)
    harness.save_multipoint_widget_config_to_cache()
    harness.tile_spacing.restore({})
    harness.radio_tiling_fraction.setChecked(True)
    harness.load_multipoint_widget_config_from_cache()
    assert harness.tile_spacing.parameters() == params
    assert harness.radio_tiling_grid.isChecked()


@pytest.mark.parametrize("widget_type", ["flexible", "wellplate"])
@pytest.mark.parametrize("mode", ["distance", "fov"])
def test_spacing_yaml_round_trip(qtbot, tmp_path, widget_type, mode):
    widget = TileSpacingWidget(QDoubleSpinBox())
    qtbot.addWidget(widget)
    params = dict(spacing_mode=mode, spacing_x=2.5, spacing_y=3)
    widget.restore(params)
    path = tmp_path / "acquisition.yaml"
    path.write_text(yaml.safe_dump({
        "acquisition": {"widget_type": widget_type},
        widget_type + "_scan": {"nx": 3, "ny": 2, "tile_spacing": widget.parameters()},
    }))
    widget.restore({})
    loaded = parse_acquisition_yaml(str(path))
    widget.restore(loaded.tile_spacing)
    assert widget.parameters() == params
    assert (loaded.nx, loaded.ny) == (3, 2)
