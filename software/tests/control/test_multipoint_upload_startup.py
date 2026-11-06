from concurrent.futures import Future
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import control._def
from control.core import multi_point_controller as controller_module
from control.core.multi_point_controller import MultiPointController, NoOpCallbacks
from control.core.upload_manager_client import UploadManagerClient, UploadManagerUnavailable


class SetupComplete(Exception):
    """Stop the test at the first operation beyond the readiness gate."""


@pytest.mark.parametrize("generate_map", [False, True])
def test_controller_builds_parameters_without_waiting_or_generating_focus(monkeypatch, tmp_path, generate_map):
    ready = Future()
    start = MagicMock(return_value=ready)
    monkeypatch.setattr(UploadManagerClient, "ensure_ready_async", start)
    monkeypatch.setattr(control._def, "ENABLE_MEMORY_PROFILING", False)
    scan = SimpleNamespace(scan_region_fov_coords_mm={"A": [(1, 2, 3)]}, scan_region_coords_mm={"A": (1, 2)})
    monkeypatch.setattr(controller_module.ScanPositionInformation, "from_scan_coordinates", lambda _: scan)

    controller = MagicMock()
    controller.file_saving_option = control._def.FileSavingOption.ZARR_V3
    controller.zarr_upload_enabled = True
    controller.zarr_upload_remote_root = "remote"
    controller.validation_mode = False
    controller.base_path = str(tmp_path)
    controller.experiment_ID = "run"
    (tmp_path / "run").mkdir()
    controller.selected_observation_state_names = ["channel"]
    controller.focus_map = None
    controller.gen_focus_map = generate_map
    controller.do_reflection_af = False
    controller.deltaX = controller.deltaY = 1
    controller.scanCoordinates.get_scan_bounds.return_value = {"x": (0, 2), "y": (0, 2)}
    controller.autofocusController.focus_map_coords = []
    controller.callbacks = NoOpCallbacks
    controller.autofocusController.gen_focus_map.side_effect = SetupComplete()
    controller.build_params.side_effect = SetupComplete()
    controller.camera.stop_streaming.side_effect = lambda: (
        start.assert_called_once(),
        pytest.fail("setup waited for manager") if ready.done() else None,
    )
    with pytest.raises(SetupComplete):
        MultiPointController.run_acquisition(controller)
    assert not ready.done()
    assert (tmp_path / "run" / "coordinates.csv").is_file()
    controller.camera.stop_streaming.assert_called_once()
    controller.camera.enable_callbacks.assert_called_once_with(True)
    controller.build_params.assert_called_once()
    controller.autofocusController.gen_focus_map.assert_not_called()
