import threading
import json
from dataclasses import replace

import numpy as np
import pytest
from qtpy.QtCore import QPointF, Qt

from control.core.sample_incidence_analysis import IlluminationResult, IncidenceResult
from tests.control.core.test_sample_incidence_calibration import hardware
from tests.control.core.test_sample_incidence_analysis import flatness_example


@pytest.fixture
def dialog(qtbot):
    from gui.widgets.sample_incidence_alignment import SampleIncidenceAlignmentDialog

    hw, _, _ = hardware(initialized=False)
    hw.scope.camera.get_pixel_size_binned_um = lambda: 1
    hw.scope.objective_store = type("Objective", (), {"get_pixel_size_factor": lambda self: 1})()
    widget = SampleIncidenceAlignmentDialog(hw.scope)
    qtbot.addWidget(widget)
    widget.show()
    yield widget
    widget.stop_and_wait()


def result(angle=1.0, center=(15.5, 15.5)):
    illumination = IlluminationResult(np.ones((32, 32)), np.zeros(5), np.array(center), 0, np.zeros(2), 0.1, 0.01, 0.05)
    return IncidenceResult(illumination, angle, None, "Test result", 0.01 if angle is not None else None)


def test_popup_blocks_parent_and_best_uses_angle_and_center(dialog):
    assert dialog.windowModality() == Qt.WindowModal
    dialog._incidence(result(2))
    first = dialog.best
    dialog._incidence(result(1))
    assert dialog.best is not first
    best = dialog.best
    dialog._incidence(result(0.9, center=(30, 30)))
    assert dialog.best is best  # A displaced beam does not win on angle alone.
    dialog._incidence(result(None))
    assert dialog.best is best
    assert "unavailable" in dialog.incidence_status.text()
    assert dialog.best_image.opacity() == pytest.approx(0.45)
    dialog.reset_button.click()
    assert dialog.best is None and dialog.best_image.image is None


def test_flatness_status_reports_inconsistent_focus_and_gates_incidence(dialog):
    dialog._flatness(flatness_example((0.02, -0.01), inconsistent=True))
    assert "holder tilt is unproven" in dialog.flatness_status.text()
    assert "gated" in dialog.incidence_status.text()
    dialog._flatness(flatness_example(centers_only=True))
    assert "unavailable (edges excluded)" in dialog.flatness_status.text()
    assert "camera-fixed focus variation ignored" in dialog.flatness_status.text()
    assert "Flatness passed" in dialog.incidence_status.text()


@pytest.mark.parametrize(
    "transform",
    [-np.eye(2), np.eye(2), np.array([[0, -2], [-2, 0]]), np.array([[-2, 0.2], [0.1, 3]])],
)
def test_image_focus_labels_beam_marker_and_profiles_share_stage_orientation(dialog, transform):
    # Before registration, be explicit that this is the delivered camera frame.
    assert "orientation pending" in dialog.raw_plot.titleLabel.text
    image = np.arange(128 * 128, dtype=float).reshape(128, 128)
    dialog._progress("Scanning", image)
    flatness = replace(flatness_example(), stage_to_image=transform)
    dialog._flatness(flatness)
    for plot in (dialog.raw_plot, dialog.focus_plot, dialog.envelope_plot, dialog.slope_plot):
        assert plot.getViewBox().state["yInverted"]
        assert not plot.getViewBox().state["xInverted"]
    assert "negative = lower Z" in dialog.orientation_status.text()
    assert "dZ/dX" in dialog.flatness_status.text()

    # The same specimen location appears at the same physical position in the
    # raw image, its focus label, and the corrected focus-map coordinate frame.
    label = dialog.focus_labels[2]
    local_pixel = np.array([128 / 3, -128 / 3])
    expected = -np.linalg.solve(transform, local_pixel)
    np.testing.assert_allclose([label.pos().x(), label.pos().y()], expected)
    point = dialog.raw_image.mapToParent(QPointF(*(local_pixel + 64)))
    np.testing.assert_allclose([point.x(), point.y()], expected)
    assert float(label.toPlainText()) == pytest.approx(flatness.camera_focus_deltas_um[0, 2], abs=0.005)

    incidence = result(center=(20, 10))
    yy, xx = np.mgrid[:32, :32]
    incidence.illumination.envelope = 1 + 0.01 * xx + 0.02 * yy
    dialog._incidence(incidence)
    expected_center = -np.linalg.solve(transform, np.array([20.5, 10.5]) - 16)
    marker_x, marker_y = dialog.beam_center.getData()
    np.testing.assert_allclose([marker_x[0], marker_y[0]], expected_center)
    for item in (dialog.current_image, dialog.best_image):
        point = item.mapToParent(QPointF(20.5, 10.5))
        np.testing.assert_allclose([point.x(), point.y()], expected_center)
    # Compare profile direction against an independently transformed intensity
    # gradient; a camera flip/rotation must not swap or reverse stage X and Y.
    stage_gradient = np.array([0.01, 0.02]) @ (-transform)
    for axis, curve in enumerate((dialog.current_x, dialog.current_y)):
        _, values = curve.getData()
        assert np.sign(values[-1] - values[0]) == np.sign(stage_gradient[axis])


def test_busy_acquisition_cannot_start_calibration(dialog):
    dialog.is_busy = lambda: True
    dialog.start_button.click()
    assert not dialog.running
    assert "Another acquisition" in dialog.status.text()


def test_recording_checkbox_folder_picker_and_missing_folder_validation(dialog, monkeypatch, tmp_path):
    import gui.widgets.sample_incidence_alignment as module

    assert not dialog.save_intermediate.isChecked()
    assert not dialog.recording_folder.isEnabled()
    assert not dialog.browse_recording_folder.isEnabled()
    dialog.save_intermediate.setChecked(True)
    assert dialog.recording_folder.isEnabled() and dialog.browse_recording_folder.isEnabled()
    monkeypatch.setattr(module.QFileDialog, "getExistingDirectory", lambda *args: str(tmp_path))
    dialog.browse_recording_folder.click()
    assert dialog.recording_folder.text() == str(tmp_path)
    dialog.recording_folder.clear()
    dialog.start_button.click()
    assert not dialog.running and "Choose a troubleshooting output folder" in dialog.status.text()


def test_recording_controls_lock_during_run_and_output_path_remains_visible(dialog, qtbot, monkeypatch, tmp_path):
    import gui.widgets.sample_incidence_alignment as module

    entered = threading.Event()

    class Session:
        def __init__(self, hw, settings, pixel_um, progress, *args, recorder=None):
            self.hw, self.recorder, self.progress = hw, recorder, progress

        def run(self):
            self.recorder.start({})
            self.progress("Troubleshooting started", None)
            entered.set()
            self.hw.cancel.wait(2)
            self.recorder.finish("cancelled")

    monkeypatch.setattr(module, "SampleIncidenceSession", Session)
    dialog.save_intermediate.setChecked(True)
    dialog.recording_folder.setText(str(tmp_path))
    dialog.start_button.click()
    qtbot.waitUntil(entered.is_set)
    qtbot.waitUntil(lambda: dialog.recording_location.isVisible())
    assert not dialog.save_intermediate.isEnabled()
    assert not dialog.recording_folder.isEnabled() and not dialog.browse_recording_folder.isEnabled()
    path = dialog._recorder.path
    assert str(path) in dialog.recording_location.text()
    dialog.stop_button.click()
    qtbot.waitUntil(lambda: not dialog.running and dialog.start_button.isEnabled())
    assert dialog.save_intermediate.isEnabled() and dialog.recording_folder.isEnabled()
    assert dialog.recording_location.isVisible()
    assert json.loads((path / "session.json").read_text())["status"] == "cancelled"


def test_close_during_worker_cancels_waits_for_restoration_and_then_closes(dialog, qtbot, monkeypatch):
    import gui.widgets.sample_incidence_alignment as module

    entered = threading.Event()
    restored = threading.Event()

    class Session:
        def __init__(self, hw, *args):
            self.hw = hw

        def run(self):
            entered.set()
            self.hw.cancel.wait(2)
            restored.set()

    monkeypatch.setattr(module, "SampleIncidenceSession", Session)
    dialog.start_button.click()
    qtbot.waitUntil(entered.is_set)
    assert dialog.running
    assert not dialog.dither.isEnabled()
    dialog.close()
    qtbot.waitUntil(lambda: restored.is_set() and not dialog.isVisible())
    assert not dialog.running
