"""Exercise failure teardown without importing camera drivers."""
import ast
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def cleanup_method():
    path = Path(__file__).parents[1] / "control/core/fast_acquisition_controller.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "_cleanup_after_failed_start")
    namespace = {"AcquisitionCompletionStatus": SimpleNamespace(COMPLETED_ERROR="error")}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[method.name]


@pytest.mark.parametrize("external,daq_only", [(False, False), (True, False), (False, True)])
@pytest.mark.parametrize("release_fails", [False, True])
def test_failed_start_releases_owned_resources_and_preserves_error(external, daq_only, release_fails):
    controller = SimpleNamespace(
        _stop_event=threading.Event(), _is_acquiring=True, _stop_called=False,
        _ni_daq=Mock(), _camera=Mock(), _frame_writer=Mock(), _log=Mock(),
        _daq_only=daq_only, _external_frame_grabbing=external,
        _cleanup_camera_acquisition_resources=Mock(),
        _illumination_controller=Mock(), _illumination_snapshot={"saved": True},
    )
    if release_fails:
        controller._ni_daq.release_tasks.side_effect = RuntimeError("device unavailable")
    cleanup_method()(controller, RuntimeError("DAQ startup failed -200946"))
    assert controller._stop_event.is_set()
    assert not controller._is_acquiring
    assert controller._stop_called
    assert controller._completion_status == "error"
    assert controller._completion_error_message == "DAQ startup failed -200946"
    controller._ni_daq.release_tasks.assert_called_once()
    controller._ni_daq.wait_until_done.assert_not_called()
    controller._ni_daq.restore_after_acquisition.assert_called_once()
    controller._illumination_controller.restore.assert_called_once_with({"saved": True}, force_hardware=True)
    if external or daq_only:
        controller._camera.stop_fast_acquisition_frame_grabbing.assert_not_called()
        controller._frame_writer.stop.assert_not_called()
    else:
        controller._camera.stop_fast_acquisition_frame_grabbing.assert_called_once()
        controller._frame_writer.stop.assert_called_once_with(wait=True)
        controller._cleanup_camera_acquisition_resources.assert_called_once()
