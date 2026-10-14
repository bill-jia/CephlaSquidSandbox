"""Independent region stack geometry, traversal, and persistence."""

from itertools import permutations
from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock

import pytest
from qtpy.QtCore import Qt
from qtpy.QtWidgets import QCheckBox, QDoubleSpinBox, QPushButton, QTableWidgetItem

from control.core.multi_point_controller import MultiPointController, _save_unified_multipoint_acquisition_yaml
from control.core.multi_point_utils import ScanPositionInformation
from control.models.acquisition_cycle import RegionPlan, _index_events
from control.models.acquisition_order import DEFAULT_ORDER, iter_ordered_visits
from control.models.region_z_stack import default_z_range, z_stack_count
from gui.widgets.multipoint import FlexibleMultiPointWidget
from control.acquisition_yaml_loader import parse_acquisition_yaml
from tests.control.test_flexible_region_state import harness
from tests.control.test_multipoint_observation_snaking import _worker
from tests.control.test_multipoint_z_retract import _params


@pytest.mark.parametrize("bounds,step,count", [
    ((1, 1), 0, 1), ((0.5, 0.506), 0.002, 4), ((0.5, 0.507), 0.002, 4),
    ((0.498, 0.5), 0.002, 2),
])
def test_count_stays_inside_limits(bounds, step, count):
    assert z_stack_count(*bounds, step) == count
    assert bounds[0] + (count - 1) * abs(step) <= bounds[1] + 1e-12


@pytest.mark.parametrize("bounds,step", [((1, 0), 0.1), ((0, 1), 0), ((0, float("nan")), 1)])
def test_invalid_ranges_are_rejected(bounds, step):
    with pytest.raises(ValueError):
        z_stack_count(*bounds, step)


@pytest.mark.parametrize("order", list(permutations(DEFAULT_ORDER)))
@pytest.mark.parametrize("snake", [False, True])
def test_all_orders_capture_each_regions_own_planes_and_reference(order, snake):
    plan = RegionPlan.from_events(_index_events([("state", ("stack", True)), ("state", ("reference", False))]))
    observed = [
        (visit.time_point, visit.position, z, event.observation_state)
        for visit in iter_ordered_visits(order, [plan, plan], 2, 5, 0, snake=snake,
                                        position_nz=[2, 5], position_reference_z=[1, 2])
        for z, events in visit.planes for event in events
    ]
    expected = {
        (t, p, z, "stack") for t in range(2) for p, nz in enumerate([2, 5]) for z in range(nz)
    } | {(t, p, ref, "reference") for t in range(2) for p, ref in enumerate([1, 2])}
    assert set(observed) == expected
    assert len(observed) == len(expected)


@pytest.mark.parametrize("mode", ["FROM BOTTOM", "FROM CENTER", "FROM TOP"])
@pytest.mark.parametrize("piezo", [False, True])
def test_worker_captures_physical_ranges_and_restores_each_reference(mode, piezo):
    events = _index_events([("state", ("A", True))])
    worker = _worker(events, nz=5, mode=mode, piezo=piezo, snake=True)
    worker._default_NZ = 5
    worker._region_z_ranges = {"R0": (4.998, 5.002), "R1": (6.000, 6.008)}
    worker._region_nominal_z = {"R0": 5.0, "R1": 6.0}
    worker.scan_region_fov_coords_mm = {"R0": [(0, 0, 5.0)], "R1": [(1, 1, 6.0)]}
    worker.stage.move_z_to = lambda z: setattr(worker.stage, "z", z)
    for region, reference, expected_count in [("R0", 5.0, 3), ("R1", 6.0, 5)]:
        # An AF/focus-map correction must move the entire stack by the same offset.
        correction = 0.001
        worker.stage.z = reference + correction
        positions = []

        def capture(config, file_id, path, z, **kwargs):
            physical_z = worker.stage.z + (worker.piezo.position - 100.0) / 1000
            positions.append(physical_z)
            assert kwargs["save_layout"].z_size == expected_count

        worker.acquire_camera_image = capture
        worker.acquire_at_position(region, "", 0)
        assert len(positions) == expected_count
        low, high = worker._region_z_ranges[region]
        assert min(positions) == pytest.approx(low + correction)
        assert max(positions) == pytest.approx(high + correction)
        assert worker.stage.z == pytest.approx(reference + correction)
        assert worker.piezo.position == pytest.approx(100.0)


def test_region_table_edits_do_not_move_stage_and_survive_rebuild_and_rename(harness):
    harness._refresh_region_z_cells()
    row = 1
    harness.table_location_list.setItem(row, harness._COL_Z_MIN, QTableWidgetItem("498"))
    harness.cell_was_changed(row, harness._COL_Z_MIN)
    harness.table_location_list.setItem(row, harness._COL_Z_MAX, QTableWidgetItem("508"))
    harness.cell_was_changed(row, harness._COL_Z_MAX)
    assert harness.table_location_list.item(row, harness._COL_NZ).text() == "6"
    assert not harness.table_location_list.item(row, harness._COL_NZ).flags() & Qt.ItemIsEditable
    assert harness.stage.mock_calls == []
    assert harness._region_z_ranges == {"R1": (0.498, 0.508)}
    harness.update_fov_positions()
    snapshot = ScanPositionInformation.from_scan_coordinates(harness.scanCoordinates)
    assert snapshot.scan_region_z_ranges["R1"] == (0.498, 0.508)
    harness.type_name(row, "tissue")
    assert harness._region_z_ranges == {"tissue": (0.498, 0.508)}
    assert harness.scanCoordinates.region_z_ranges["tissue"] == (0.498, 0.508)
    # Shared step changes recalculate all counts but preserve the explicit limits.
    harness.entry_deltaZ.setValue(1.0)
    harness._refresh_region_z_cells()
    assert harness.table_location_list.item(row, harness._COL_NZ).text() == "11"


def test_z_position_edit_translates_range_and_all_fovs(harness):
    harness._region_z_ranges["R0"] = (0.498, 0.508)
    harness.checkbox_moveStageOnClick.setChecked(False)
    harness.go_to = lambda _: None
    harness.table_location_list.setItem(0, harness._COL_Z, QTableWidgetItem("600"))
    harness.cell_was_changed(0, harness._COL_Z)
    assert harness._region_z_ranges["R0"] == pytest.approx((0.598, 0.608))
    assert all(coord[2] == pytest.approx(0.6) for coord in harness.scanCoordinates.region_fov_coordinates["R0"])
    harness.update_fov_positions()
    assert harness.scanCoordinates.region_z_ranges["R0"] == pytest.approx((0.598, 0.608))


def test_stack_disabled_preserves_overrides_but_acquires_single_planes(harness):
    harness._region_z_ranges["R0"] = (0.498, 0.508)
    harness._zstack_checkbox.setChecked(False)
    harness._refresh_region_z_cells()
    assert harness._effective_NZ() == 1
    assert harness.scanCoordinates.region_z_ranges == {}
    assert harness.table_location_list.item(0, harness._COL_NZ).text() == "1"
    assert harness._region_z_ranges["R0"] == (0.498, 0.508)


def test_all_overridden_regions_use_actual_maximum_count(harness):
    harness.entry_NZ.setValue(100)
    harness._region_z_ranges = {"R0": (0.5, 0.5), "R1": (0.5, 0.502), "R2": (0.5, 0.504)}
    harness._refresh_region_z_cells()
    assert harness._effective_NZ() == 3
    harness.multipointController.set_NZ.assert_called_with(3)


@pytest.fixture
def range_panel(harness):
    h = harness
    for name in ("_set_region_range_boundary", "set_z_min", "set_z_max", "update_Nz", "init_z"):
        setattr(h, name, MethodType(getattr(FlexibleMultiPointWidget, name), h))
    h.checkbox_set_z_range = QCheckBox(parent=h.table_location_list)
    h.checkbox_set_z_range.setChecked(True)
    h.checkbox_set_z_globally = QCheckBox("Set Z globally", h.table_location_list)
    for name in ("entry_minZ", "entry_maxZ"):
        entry = QDoubleSpinBox(h.table_location_list)
        entry.setRange(-100000, 100000)
        entry.setDecimals(3)
        setattr(h, name, entry)
    for name in ("set_minZ_button", "set_maxZ_button", "goto_minZ_button", "goto_maxZ_button"):
        setattr(h, name, QPushButton(h.table_location_list))
    h.entry_minZ.valueChanged.connect(lambda value: h._set_region_range_boundary(0, value))
    h.entry_maxZ.valueChanged.connect(lambda value: h._set_region_range_boundary(1, value))
    h.checkbox_set_z_globally.toggled.connect(h._sync_z_range_controls)
    h.table_location_list.currentCellChanged.connect(lambda *_: h._sync_z_range_controls())
    h._select_row(1)
    h._refresh_region_z_cells()
    return h


def test_set_buttons_default_to_selected_position_without_changing_other_defaults(range_panel):
    h = range_panel
    assert not h.checkbox_set_z_globally.isChecked()
    h.stage.get_pos.return_value.z_mm = 0.498
    h.set_z_min()
    h.stage.get_pos.return_value.z_mm = 0.510
    h.set_z_max()
    assert h._region_z_ranges == {"R1": (0.498, 0.510)}
    assert h.entry_NZ.value() == 3
    assert [h.table_location_list.item(row, h._COL_NZ).text() for row in range(3)] == ["3", "7", "3"]
    assert not any(c[0].startswith("move_") for c in h.stage.mock_calls)


def test_global_min_updates_all_positions_preserving_each_opposite_limit(range_panel):
    h = range_panel
    h._region_z_ranges = {"R0": (0.5, 0.506), "R1": (0.5, 0.508), "R2": (0.5, 0.510)}
    h._refresh_region_z_cells()
    h.checkbox_set_z_globally.setChecked(True)
    # Changing the scope alone must not rewrite any range.
    assert h._region_z_ranges["R0"] == (0.5, 0.506)
    h.stage.get_pos.return_value.z_mm = 0.498
    h.set_z_min()
    assert h._region_z_ranges == {"R0": (0.498, 0.506), "R1": (0.498, 0.508), "R2": (0.498, 0.510)}
    h.stage.get_pos.return_value.z_mm = 0.512
    h.set_z_max()
    assert all(bounds == (0.498, 0.512) for bounds in h._region_z_ranges.values())


def test_range_fields_follow_selection_without_mutating_ranges(range_panel):
    h = range_panel
    h._region_z_ranges = {"R0": (0.490, 0.506), "R1": (0.498, 0.510)}
    h._refresh_region_z_cells()
    before = dict(h._region_z_ranges)
    h._select_row(0)
    assert (h.entry_minZ.value(), h.entry_maxZ.value()) == (490, 506)
    h._select_row(1)
    assert (h.entry_minZ.value(), h.entry_maxZ.value()) == (498, 510)
    assert h._region_z_ranges == before


def test_stage_z_initialization_does_not_replace_selected_range_fields(range_panel):
    h = range_panel
    h.init_z(0.8)
    assert (h.entry_minZ.value(), h.entry_maxZ.value()) == (500, 504)
    assert h._region_z_ranges == {}


def test_typing_range_fields_uses_selected_scope_and_shared_step_recomputes_counts(range_panel):
    h = range_panel
    h.entry_minZ.setValue(498)
    h.entry_maxZ.setValue(510)
    assert h._region_z_ranges == {"R1": (0.498, 0.510)}
    h.entry_deltaZ.setValue(1)
    h.update_Nz()
    assert h.entry_NZ.value() == 3
    assert h.table_location_list.item(1, h._COL_NZ).text() == "13"


def test_global_button_applies_even_when_stage_z_matches_visible_field(range_panel):
    h = range_panel
    h._region_z_ranges = {"R0": (0.490, 0.506), "R1": (0.498, 0.510)}
    h._refresh_region_z_cells()
    h.checkbox_set_z_globally.setChecked(True)
    h.stage.get_pos.return_value.z_mm = 0.498
    h.set_z_min()
    assert all(bounds[0] == 0.498 for bounds in h._region_z_ranges.values())


def test_local_range_controls_disabled_without_selection_and_reenabled_for_global_scope(range_panel):
    h = range_panel
    h.table_location_list.setCurrentCell(-1, -1)
    h._sync_z_range_controls()
    assert not h.set_minZ_button.isEnabled()
    h.stage.get_pos.return_value.z_mm = 0.498
    h.set_z_min()
    assert h._region_z_ranges == {}
    h.checkbox_set_z_globally.setChecked(True)
    assert h.set_minZ_button.isEnabled()
    h.set_z_min()
    assert set(h._region_z_ranges) == {"R0", "R1", "R2"}


def test_image_count_uses_each_regions_depth_and_channel_selection():
    controller = MultiPointController.__new__(MultiPointController)
    controller.scanCoordinates = SimpleNamespace(region_fov_coordinates={"R0": [(0, 0)] * 2, "R1": [(1, 1)]},
                                                region_z_ranges={"R0": (0.5, 0.504), "R1": (0.6, 0.608)})
    controller.NZ, controller.Nt, controller.deltaZ = 5, 2, 0.002
    controller.selected_observation_state_names = ["A"]
    controller.selected_cycle_names = []
    controller.region_cycle_map = None
    controller.region_observation_state_map = {"R1": ["A", "B"]}
    controller._resolve_plan = lambda _, names: RegionPlan.from_events(
        _index_events([("state", (name, True)) for name in (names or ["A"])])
    )
    assert controller.get_acquisition_image_count() == 2 * (2 * 3 + 1 * 5 * 2)


def test_yaml_records_region_ranges_and_counts(tmp_path, monkeypatch):
    monkeypatch.setattr("control.core.acquisition_metadata_helpers.augment_multipoint_acquisition_yaml_dict",
                        lambda base_yaml, **kwargs: {"schema_version": 2, **base_yaml})
    params = _params()
    params.deltaZ = 0.002
    params.scan_position_information.scan_region_z_ranges = {"R0": (0.5, 0.508)}
    _save_unified_multipoint_acquisition_yaml(
        params, str(tmp_path), widget_type="flexible", repo=MagicMock(), live_controller=MagicMock(),
        camera=MagicMock(), objective_store=MagicMock(), recording_start_time=0.0,
        selected_observation_state_names=[], use_manual_focus_map=False, logger=MagicMock(),
    )
    data = parse_acquisition_yaml(str(tmp_path / "acquisition.yaml"))
    region = next(p for p in data.flexible_positions if p["name"] == "R0")
    assert region["z_range_mm"] == [0.5, 0.508]
    assert region["nz"] == 5
    assert data.nz == 5


def _enable_persistence(h):
    for name in ("export_location_list", "import_location_list", "clear_only_location_list", "_load_positions",
                 "_next_region_id", "_load_laser_af_sidecar", "_reference_from_import_row"):
        setattr(h, name, MethodType(getattr(FlexibleMultiPointWidget, name), h))
    h._laser_af_sidecar_path = FlexibleMultiPointWidget._laser_af_sidecar_path
    h._update_position_count = lambda: None
    return h


def test_csv_roundtrip_preserves_overrides_shared_step_and_default_counts(harness, tmp_path, monkeypatch):
    h = _enable_persistence(harness)
    h._region_z_ranges["R0"] = (0.498, 0.508)
    path = str(tmp_path / "positions.csv")
    monkeypatch.setattr("gui.widgets.multipoint.QFileDialog.getSaveFileName", lambda *args: (path, ""))
    monkeypatch.setattr("gui.widgets.multipoint.QFileDialog.getOpenFileName", lambda *args: (path, ""))
    h.export_location_list()
    h._region_z_ranges.clear()
    h.entry_deltaZ.setValue(1)
    h.entry_NZ.setValue(20)
    h.import_location_list()
    assert h._region_z_ranges == {"R0": (0.498, 0.508)}
    assert h.entry_deltaZ.value() == 2
    assert h.entry_NZ.value() == 3
    assert [h.table_location_list.item(row, h._COL_NZ).text() for row in range(3)] == ["6", "3", "3"]


def test_yaml_position_loading_blocks_edit_callbacks_and_restores_ranges(harness):
    h = _enable_persistence(harness)
    h.table_location_list.cellChanged.connect(h.cell_was_changed)
    h._load_positions([{"name": "tissue", "center_mm": [1, 2, 0.6], "z_range_mm": [0.598, 0.608]}])
    assert list(h.location_ids) == ["tissue"]
    assert h.location_list.tolist() == [[1, 2, 0.6]]
    assert h.scanCoordinates.region_z_ranges == {"tissue": (0.598, 0.608)}
    assert h.table_location_list.item(0, h._COL_NZ).text() == "6"
    assert h.stage.mock_calls == []


def test_invalid_yaml_range_does_not_clear_positions(harness):
    h = _enable_persistence(harness)
    with pytest.raises(ValueError):
        h._load_positions([{"name": "bad", "center_mm": [1, 2, 0.6], "z_range_mm": [0.7, 0.6]}])
    assert list(h.location_ids) == ["R0", "R1", "R2"]


@pytest.mark.parametrize("format_name", ["ZARR_V3", "OME_TIFF"])
def test_worker_saves_unequal_region_depths_and_pixels(tmp_path, format_name):
    import numpy as np
    import tifffile
    from control._def import FileSavingOption
    from control.core.job_processing import AcquisitionInfo, CaptureInfo, JobImage, SaveOMETiffJob, SaveZarrJob, ZarrWriterInfo
    from squid.abc import Pos
    from tests.control.core.test_cycle_zarr_layout import _obs, _read_tensorstore

    option = FileSavingOption[format_name]
    events = _index_events([("state", ("A", True))])
    worker = _worker(events, nz=5, mode="FROM BOTTOM", piezo=False, snake=True)
    worker._default_NZ = 5
    worker._region_z_ranges = {"R0": (0.5, 0.504), "R1": (0.6, 0.608)}
    worker._region_nominal_z = {"R0": 0.5, "R1": 0.6}
    worker.scan_region_fov_coords_mm = {"R0": [(0, 0, 0.5)], "R1": [(1, 1, 0.6)]}
    worker.stage.move_z_to = lambda z: setattr(worker.stage, "z", z)
    acquisition_info = AcquisitionInfo(total_time_points=2, total_z_levels=5, total_channels=1,
                                       channel_names=["A"], experiment_path=str(tmp_path),
                                       physical_size_z_um=2, fovs_per_region={"R0": 1, "R1": 1})
    writer_info = ZarrWriterInfo(base_path=str(tmp_path), t_size=2, c_size=1, z_size=5, is_hcs=False,
                                 region_fov_counts={"R0": 1, "R1": 1}, channel_names=["A"], z_step_um=2)

    def capture(config, file_id, path, z, **kwargs):
        region = kwargs["region_id"]
        layout = kwargs["save_layout"]
        value = worker.time_point * 100 + (10 if region == "R1" else 0) + z
        info = CaptureInfo(position=Pos(x_mm=0, y_mm=0, z_mm=worker.stage.z, theta_rad=None),
                           z_index=z, capture_time=float(worker.time_point + 1), observation_state=_obs("A"),
                           save_directory=str(tmp_path), file_id=file_id, region_id=region, fov=0,
                           configuration_idx=0, time_point=worker.time_point, acquisition_root=str(tmp_path),
                           file_saving_option=option, save_t_index=layout.t_index, save_c_index=layout.c_index,
                           save_t_size=layout.t_size, save_c_size=layout.c_size, save_z_size=layout.z_size,
                           stack_z_size=worker.NZ)
        image = JobImage(np.full((16, 16), value, dtype=np.uint16))
        if option == FileSavingOption.OME_TIFF:
            job = SaveOMETiffJob(capture_info=info, capture_image=image, acquisition_info=acquisition_info)
        else:
            job = SaveZarrJob(capture_info=info, capture_image=image)
            job.zarr_writer_info = writer_info
        assert job.run()

    worker.acquire_camera_image = capture
    try:
        for t in range(2):
            worker.time_point = t
            for region, reference in worker._region_nominal_z.items():
                worker.stage.z = reference
                worker.acquire_at_position(region, str(tmp_path), 0)
    finally:
        SaveOMETiffJob.finalize_all_writers()
        SaveZarrJob.finalize_all_writers()
    for region, nz in [("R0", 3), ("R1", 5)]:
        if option == FileSavingOption.OME_TIFF:
            with tifffile.TiffFile(tmp_path / "ome_tiff" / f"{region}.ome.tiff") as tif:
                assert tif.series[0].shape == (2, nz, 16, 16)
                pixels = tif.asarray().reshape(2, 1, nz, 16, 16)
        else:
            ds = _read_tensorstore(writer_info.get_output_path(region, 0))
            assert tuple(ds.shape) == (2, 1, nz, 16, 16)
            pixels = ds.read().result()
        for t in range(2):
            for z in range(nz):
                assert np.all(pixels[t, 0, z] == t * 100 + (10 if region == "R1" else 0) + z)
