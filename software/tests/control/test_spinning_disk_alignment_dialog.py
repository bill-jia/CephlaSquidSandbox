"""Exercise the live popup with real Qt graphics and a fake camera callback."""

from types import SimpleNamespace

import numpy as np
import pytest

from tests.control.core.test_spinning_disk_alignment import pinholes, resolved_pinholes


class FakeCamera:
    def __init__(self):
        self.callbacks = {}
        self.next_id = 0

    def add_frame_callback(self, callback):
        self.next_id += 1
        self.callbacks[self.next_id] = callback
        return self.next_id

    def remove_frame_callback(self, callback_id):
        del self.callbacks[callback_id]

    def emit(self, image):
        for callback in list(self.callbacks.values()):
            callback(SimpleNamespace(frame=image))


@pytest.fixture
def dialog(qtbot):
    from gui.widgets.spinning_disk_alignment import SpinningDiskAlignmentDialog

    widget = SpinningDiskAlignmentDialog(FakeCamera())
    qtbot.addWidget(widget)
    widget.show()
    widget.timer.stop()  # Consume frames deterministically in these tests.
    return widget


def display(dialog, image):
    dialog._last_copy = 0
    dialog.camera.emit(image)
    dialog._update_display()


def test_live_best_tracks_sharpness_retains_overlay_on_detection_loss_and_resets(dialog):
    display(dialog, pinholes(sigma=3))
    first_best = dialog.best
    assert first_best is not None
    display(dialog, pinholes(sigma=2))
    sharp_best = dialog.best
    assert sharp_best.sharpness > first_best.sharpness
    assert "Current edge steepness:" in dialog.status.text()
    assert "Best edge steepness:" in dialog.status.text()
    display(dialog, pinholes(sigma=3.5))
    assert dialog.best is sharp_best
    assert dialog.best_image.opacity() == pytest.approx(0.45)
    assert dialog.current_x.xData[len(dialog.current_x.xData) // 2] == 0
    display(dialog, np.zeros((256, 256)))
    assert dialog.best is sharp_best
    assert dialog.current_image.image is None
    assert dialog.best_image.image is not None
    dialog.reset_button.click()
    assert dialog.best is None
    assert dialog.best_image.image is None


def test_best_ranking_uses_steepness_even_when_winning_pinhole_is_wider(dialog):
    display(dialog, resolved_pinholes(edge_width=1.0, hole_radius=3))
    first_best = dialog.best
    assert first_best is not None
    display(dialog, resolved_pinholes(edge_width=0.4, hole_radius=6))
    sharper_best = dialog.best
    assert sharper_best is not first_best
    assert sharper_best.width > first_best.width
    assert sharper_best.sharpness > first_best.sharpness
    display(dialog, resolved_pinholes(edge_width=1.0, hole_radius=3))
    assert dialog.best is sharper_best


def test_insufficient_holes_show_current_but_do_not_set_best(dialog):
    display(dialog, pinholes([(80.2, 80.3)]))
    assert dialog.best is None
    assert dialog.current_image.image is not None
    assert "1 accepted" in dialog.status.text()


def test_close_disconnects_and_reopen_starts_a_new_session(dialog):
    display(dialog, pinholes())
    callback = next(iter(dialog.camera.callbacks.values()))
    assert dialog.best is not None
    dialog.reject()  # Escape follows the same path.
    assert not dialog.camera.callbacks
    assert not dialog.timer.isActive()
    callback(SimpleNamespace(frame=pinholes()))
    assert dialog._pending is None
    dialog.show()
    assert len(dialog.camera.callbacks) == 1
    assert dialog.best is None
    dialog.show()  # Raising an existing popup must not attach twice.
    assert len(dialog.camera.callbacks) == 1


def test_setting_changes_reset_comparison_and_discard_pending_patch(dialog):
    display(dialog, pinholes())
    dialog._last_copy = 0
    dialog.camera.emit(pinholes())
    dialog.offset_x.setValue(20)
    assert dialog._pending is None
    assert dialog.best is None
    display(dialog, pinholes())
    assert dialog.best is not None
    dialog.radius.setValue(15)
    assert dialog.best is None
    display(dialog, pinholes())
    assert dialog.current_image.image.shape == (31, 31)


def test_camera_callback_only_copies_latest_patch_without_processing(dialog):
    import threading

    full = np.zeros((1024, 1024), dtype=np.uint16)
    thread = threading.Thread(target=lambda: dialog.camera.emit(full))
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert dialog._pending.shape == (512, 512)
    full[:] = 1
    assert not dialog._pending.any()
    assert dialog.patch_image.image is None
