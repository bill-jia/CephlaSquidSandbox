"""Real Napari layers retain sparse channels and commit position previews."""

from types import SimpleNamespace

import numpy as np
import pytest

from control.core.acquisition_preview import PreviewStarted, PreviewCaptured, PreviewFrame, PreviewProcessed


@pytest.fixture
def widget(qtbot):
    from control.core.contrast_manager import ContrastManager
    from gui.widgets.napari_views import NapariMultiChannelWidget

    objective = SimpleNamespace(
        get_pixel_size_factor=lambda: 1, get_current_objective_info=lambda: {"magnification": 20}
    )
    camera = SimpleNamespace(get_pixel_size_binned_um=lambda: 0.5)
    w = NapariMultiChannelWidget(objective, camera, ContrastManager())
    qtbot.addWidget(w)
    w.initLayersShape(5, 2)
    w.resetAcquisitionPreview()
    yield w
    w.viewer.close()


def image(value, shape=(8, 8), dtype=np.uint16):
    return np.full(shape, value, dtype=dtype)


def test_position_commit_includes_processed_planes_of_different_size(widget):
    w = widget
    w.preview_mode.setCurrentIndex(1)
    first = ("run", 0, "A1", 0)
    second = ("run", 1, "B1", 0)
    w.handlePreviewEvent(PreviewStarted(first, 2, ("process",)))
    w.handlePreviewEvent(PreviewFrame(first, "raw", image(4), 0, 2))
    w.handlePreviewEvent(PreviewFrame(first, "raw", image(5), 1, 2))
    w.handlePreviewEvent(PreviewCaptured(first))
    assert len(w.viewer.layers) == 0
    w.handlePreviewEvent(
        PreviewProcessed(first, "process", {"derived": image(11, (4, 6))}, {"derived": image(22, (4, 6))})
    )
    assert int(w.viewer.layers["20x raw"].data[0, 0]) == 5
    derived = w.viewer.layers["20x derived"]
    assert derived.data.shape == (4, 6)
    assert derived.data[0, 0] == 11
    derived.gamma = 1.6
    w.preview_z_choice.setCurrentIndex(1)
    assert derived.data[0, 0] == 22 and derived.gamma == 1.6
    w.handlePreviewEvent(PreviewStarted(second, 1))
    w.handlePreviewEvent(PreviewFrame(second, "other", image(99), 0, 1))
    assert "20x derived" in w.viewer.layers  # holds across timepoints/regions
    w.handlePreviewEvent(PreviewCaptured(second))
    assert [layer.name for layer in w.viewer.layers] == ["20x other"]
    assert "B1" in w.preview_status.text()


def test_sparse_rgb_and_mixed_dtype_channels_hold_and_disabled_preview_replays(widget):
    w = widget
    key = ("run", 0, "A1", 0)
    w.handlePreviewEvent(PreviewStarted(key, 4))
    w.handlePreviewEvent(PreviewFrame(key, "reference", image(70, (8, 8, 3), np.uint8), 0, 1))
    reference = w.viewer.layers["20x reference"]
    assert reference.rgb and reference.data.shape == (8, 8, 3)
    reference.gamma = 1.5
    w.handlePreviewEvent(PreviewFrame(key, "stack", image(200), 0, 3))
    assert reference in w.viewer.layers and reference.data[0, 0, 0] == 70
    assert reference.gamma == 1.5
    w.setPreviewEnabled(False)
    w.handlePreviewEvent(PreviewFrame(key, "stack", image(300), 1, 3))
    w.handlePreviewEvent(PreviewFrame(key, "stack", image(400), 2, 3))
    w.handlePreviewEvent(PreviewCaptured(key))
    assert w.viewer.layers["20x stack"].data[0, 0] == 200
    w.finishAcquisitionPreview()
    assert w.viewer.layers["20x stack"].data[0, 0] == 400
    w.preview_mode.setCurrentIndex(1)
    assert w.viewer.layers["20x stack"].data[0, 0] == 300


def test_controller_callback_queues_preview_from_background_thread(widget, qtbot, monkeypatch):
    import threading
    from qtpy.QtCore import Qt
    from control.core.multi_point_controller import MultiPointController
    from gui.gui_hcs.qt_controllers import QtMultiPointController

    callbacks = []
    monkeypatch.setattr(MultiPointController, "__init__", lambda self, **kw: callbacks.append(kw["callbacks"]))
    controller = QtMultiPointController(None, None, None, None)
    controller.acquisition_preview.connect(widget.handlePreviewEvent, Qt.QueuedConnection)
    controller.acquisition_preview_enabled.connect(widget.setPreviewEnabled)
    key = ("run", 0, "A1", 0)

    def capture():
        for event in (PreviewStarted(key, 1), PreviewFrame(key, "raw", image(42), 0, 1), PreviewCaptured(key)):
            callbacks[0].signal_acquisition_preview(event)

    thread = threading.Thread(target=capture)
    thread.start()
    thread.join()
    qtbot.waitUntil(lambda: "20x raw" in widget.viewer.layers)
    assert widget.viewer.layers["20x raw"].data[0, 0] == 42
    controller.set_show_live_during_acquisition(False)
    assert not widget._preview_enabled
