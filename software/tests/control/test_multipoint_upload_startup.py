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
@pytest.mark.parametrize("manager_fails", [False, True])
def test_setup_overlaps_startup_but_focus_and_acquisition_wait(monkeypatch, tmp_path, generate_map, manager_fails):
    gate_reached = Event()

    class Readiness(Future):
        def result(self, timeout=None):
            gate_reached.set()
            return super().result(timeout=5)

    ready = Readiness()
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
    errors = []

    def run():
        try:
            MultiPointController.run_acquisition(controller)
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=run, daemon=True)
    thread.start()
    try:
        assert gate_reached.wait(5), errors
        assert (tmp_path / "run" / "coordinates.csv").is_file()
        controller.camera.stop_streaming.assert_called_once()
        controller.camera.enable_callbacks.assert_called_once_with(True)
        controller.autofocusController.gen_focus_map.assert_not_called()
        controller.build_params.assert_not_called()
        if manager_fails:
            ready.set_exception(UploadManagerUnavailable("unavailable"))
        else:
            ready.set_result(None)
        thread.join(5)
        assert not thread.is_alive()
        assert len(errors) == 1
        if manager_fails:
            assert isinstance(errors[0], UploadManagerUnavailable)
            controller.autofocusController.gen_focus_map.assert_not_called()
            controller.build_params.assert_not_called()
        else:
            assert isinstance(errors[0], SetupComplete)
            if generate_map:
                controller.autofocusController.gen_focus_map.assert_called_once()
            else:
                controller.build_params.assert_called_once()
    finally:
        if not ready.done():
            ready.set_result(None)
        thread.join(5)
