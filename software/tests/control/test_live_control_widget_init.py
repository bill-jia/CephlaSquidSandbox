"""``LiveControlWidget.currentConfiguration`` must exist from construction.

It is assigned in ``select_new_microscope_mode_by_name``, but other widgets read
it before any mode has been selected — ``make_connections`` syncs the confocal
panel's irises from it during startup. When the attribute was only created on
first mode selection, that read raised ``AttributeError`` and the application
died before the window appeared.
"""

import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("qtpy")
from qtpy.QtWidgets import QApplication

from gui.widgets.hardware_panels import LiveControlWidget


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


def _build_widget(monkeypatch):
    """Construct the widget without building its (large) real widget tree."""
    seen = {}

    def fake_add_components(self, *args, **kwargs):
        # Anything reading the widget during construction must see the attribute.
        seen["bound_before_add_components"] = hasattr(self, "currentConfiguration")

    monkeypatch.setattr(LiveControlWidget, "add_components", fake_add_components)

    stream_handler = SimpleNamespace(set_display_fps=lambda fps: None)
    live_controller = SimpleNamespace(microscope=SimpleNamespace(camera=object()))
    widget = LiveControlWidget(
        streamHandler=stream_handler,
        liveController=live_controller,
        objectiveStore=object(),
    )
    return widget, seen


def test_current_configuration_exists_after_init(qt_app, monkeypatch):
    widget, _ = _build_widget(monkeypatch)

    # The read that used to crash startup.
    assert widget.currentConfiguration is None


def test_current_configuration_is_bound_before_widgets_are_built(qt_app, monkeypatch):
    _, seen = _build_widget(monkeypatch)

    assert seen["bound_before_add_components"] is True


def test_falsy_so_guarded_reads_skip_cleanly(qt_app, monkeypatch):
    widget, _ = _build_widget(monkeypatch)

    # Call sites guard with `if widget.currentConfiguration:` — None must not
    # look like a selected channel.
    assert not widget.currentConfiguration
