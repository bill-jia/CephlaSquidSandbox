"""Presentation contracts that must not trigger hardware or lose user state."""
from types import SimpleNamespace

import pytest
from qtpy.QtCore import QObject, Signal
from qtpy.QtWidgets import QApplication, QTabWidget, QVBoxLayout, QWidget

from control.core.config.repository import ConfigRepository
from control.models.laser_af_config import LaserAFConfig
from control.models.gui_state import GuiState
from gui.profile_selection import ProfileSelectionDialog, resolve_interface_mode, prompt_for_launch, prompt_for_profile
from gui.widgets.tracking_and_controls import LaserAutofocusControlWidget
from gui.gui_hcs.full_interface import add_focus_controls, make_touch_targets
from gui.widgets.illumination_main import IlluminationWidget
from control.lighting import IlluminationController, LEDMatrixIlluminationDevice


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_launcher_default_accept_cancel_and_cli_precedence(app, tmp_path):
    repo = ConfigRepository(tmp_path)
    repo.create_profile("review")
    assert resolve_interface_mode(repo=repo) == "desktop"
    dialog = ProfileSelectionDialog(repo)
    dialog.combo_interface.setCurrentIndex(1)
    dialog.reject()
    assert repo.get_interface_mode() == "desktop"
    dialog = ProfileSelectionDialog(repo)
    dialog.combo_interface.setCurrentIndex(1)
    dialog._accept()
    assert dialog.selected_profile() == "review"
    assert repo.get_interface_mode() == "touch"
    assert resolve_interface_mode("desktop", repo) == "desktop"
    dialog = ProfileSelectionDialog(repo, interface_override="desktop")
    assert not dialog.combo_interface.isEnabled()
    dialog._accept()
    assert repo.get_interface_mode() == "touch"  # temporary CLI override


def test_first_run_selection_and_compatible_wrapper(app, tmp_path, monkeypatch):
    monkeypatch.setenv("SQUID_CONFIG_ROOT", str(tmp_path))
    monkeypatch.setattr(ProfileSelectionDialog, "exec_", lambda self: (self._accept() or self.Accepted))
    choice = prompt_for_launch(interface_override="touch")
    assert choice.profile == "default" and choice.interface == "touch"
    assert prompt_for_profile() == "default"
    # --profile bypass uses the same resolver without constructing a dialog.
    repo = ConfigRepository()
    repo.set_interface_mode("touch")
    assert resolve_interface_mode() == "touch"
    assert resolve_interface_mode("desktop") == "desktop"


def test_layout_states_roundtrip_without_cross_contamination():
    state = GuiState(layouts={"desktop": {"geometry": "large"},
                              "touch": {"task": "focus", "splits": {"focus": [800, 900]}}})
    restored = GuiState.model_validate(state.model_dump())
    assert restored.layouts["desktop"] == {"geometry": "large"}
    assert restored.layouts["touch"]["splits"]["focus"] == [800, 900]


class LaserStub(QObject):
    signal_displacement_um = Signal(float)

    def __init__(self):
        super().__init__()
        self.laser_af_properties = LaserAFConfig()
        self.is_initialized = True  # cached config deliberately does not prove readiness
        self.targets = []
        self.fail = False

    def move_to_target(self, value):
        self.targets.append(value)
        if self.fail:
            raise RuntimeError("fixture move failed")


@pytest.fixture
def laser_widget(app):
    laser = LaserStub()
    calls = []
    live = SimpleNamespace(is_live=True, stop_live=lambda: calls.append("stop"),
                           start_live=lambda: calls.append("start"))
    widget = LaserAutofocusControlWidget(laser, live)
    yield widget, laser, calls
    widget.deleteLater()


def test_focus_setup_restores_inactive_tab_controls_and_initialize(laser_widget, app):
    widget, _, _ = laser_widget
    tabs = QTabWidget()
    tabs.addTab(QWidget(), "Camera")
    contrast = QWidget()
    focus_map = QWidget()
    for panel, title in ((contrast, "Contrast AF"), (widget, "Laser AF"), (focus_map, "Focus Map")):
        tabs.addTab(panel, title)
    tabs.setCurrentIndex(0)
    tabs.show()
    app.processEvents()
    assert widget.isHidden()  # Same inactive desktop tab state used during GUI construction.

    page = QWidget()
    QVBoxLayout(page)
    gui = SimpleNamespace(autofocusWidget=contrast, laserAutofocusControlWidget=widget,
                          focusMapWidget=focus_map, focusCameraDialog=None)
    requests = []
    widget.signal_initialize_requested.connect(lambda: requests.append("initialize"))
    try:
        add_focus_controls(page, gui)
        make_touch_targets(page)
        page.show()
        app.processEvents()
        for panel in (contrast, widget, focus_map):
            assert panel.isVisibleTo(page)
        assert widget.btn_initialize.isVisible()
        assert widget.btn_initialize.height() >= 48
        widget.btn_initialize.click()
        assert requests == ["initialize"]

        widget.on_initialization_started()
        widget.btn_initialize.click()
        assert requests == ["initialize"]
        widget.on_initialization_finished(False)
        page.setEnabled(False)  # Acquisition lockout applies to the restored controls.
        widget.btn_initialize.click()
        assert requests == ["initialize"]
    finally:
        page.close()
        page.deleteLater()
        tabs.close()
        tabs.deleteLater()


def test_laser_af_target_readiness_and_lockout(laser_widget):
    widget, laser, calls = laser_widget
    widget.entry_target.setValue(12.5)
    assert widget.btn_move_to_target.text() == "Laser AF Now"
    laser.laser_af_properties.has_reference = True
    widget._apply_button_state()
    widget.move_to_target()
    assert laser.targets == []  # cached initialization never enables this operation
    widget.on_initialization_finished(True)
    laser.laser_af_properties.has_reference = False
    widget._apply_button_state()
    widget.move_to_target()
    assert laser.targets == []
    laser.laser_af_properties.has_reference = True
    widget._apply_button_state()
    widget.setEnabled(False)
    widget.move_to_target()
    assert laser.targets == []
    widget.setEnabled(True)
    widget.btn_move_to_target.click()
    assert laser.targets == [12.5]
    assert calls == ["stop", "start"]


def test_laser_af_resumes_live_on_failure_and_prevents_reentry(laser_widget):
    widget, laser, calls = laser_widget
    laser.laser_af_properties.has_reference = True
    widget.on_initialization_finished(True)
    widget._action_busy = True
    widget.move_to_target()
    assert laser.targets == []
    widget._action_busy = False
    laser.fail = True
    with pytest.raises(RuntimeError):
        widget.move_to_target()
    assert calls == ["stop", "start"]
    assert not widget._action_busy


def test_reinitialization_blocks_a_previously_ready_laser_action(laser_widget):
    widget, laser, calls = laser_widget
    laser.laser_af_properties.has_reference = True
    widget.on_initialization_finished(True)
    assert widget.btn_move_to_target.isEnabled()
    widget.on_initialization_started()
    assert widget.quick_action_unavailable_reason() == "Laser AF busy"
    assert not widget.btn_initialize.isEnabled()
    widget.move_to_target()
    assert laser.targets == [] and calls == []
    widget.on_initialization_finished(False)
    assert widget.btn_initialize.isEnabled()
    assert widget.quick_action_unavailable_reason() == "Initialize in Focus Setup"


@pytest.mark.parametrize("method", ["on_measure_displacement_clicked", "on_set_reference_clicked"])
def test_focus_measurement_and_reference_block_target_reentry(laser_widget, method):
    widget, laser, calls = laser_widget
    laser.laser_af_properties.has_reference = True
    widget.on_initialization_finished(True)

    def operation():
        assert widget.quick_action_unavailable_reason() == "Laser AF busy"
        widget.move_to_target()
        return True

    laser.measure_displacement = operation
    laser.set_reference = operation
    getattr(widget, method)()
    assert laser.targets == []
    assert calls == ["stop", "start"]
    assert widget.btn_move_to_target.isEnabled()


@pytest.mark.parametrize("unified", [True, False])
def test_matrix_modes_are_one_row_and_refresh_emits_no_hardware_writes(app, unified):
    operations = []
    mcu = SimpleNamespace(
        apply_led_matrix_channel_configuration=lambda *a, **k: operations.append((a, k)),
        turn_on_illumination=lambda: operations.append("on"),
        turn_off_illumination=lambda: operations.append("off"),
        wait_till_operation_is_completed=lambda: None)
    matrix = LEDMatrixIlluminationDevice({"BF LED matrix full": 0, "DF LED matrix": 3}, mcu, unified=unified)
    fluorescence = SimpleNamespace(channel_names=["Fluorescence 488 nm Ex"],
                                   set_intensity=lambda *a: operations.append(a))
    controller = IlluminationController([matrix, fluorescence])
    widget = IlluminationWidget(controller)
    assert set(widget._channel_rows) == {"LED matrix", "Fluorescence 488 nm Ex"}
    controller.set_channel_intensity("BF LED matrix full", 23)
    controller.set_channel_state("DF LED matrix", True, force_hardware=True)
    operations.clear()
    widget._refresh_from_state()
    assert operations == []
    combo = widget._channel_rows["LED matrix"]["mode_combo"]
    assert combo.currentData() == ("df" if unified else "DF LED matrix")
    assert len(widget._channel_rows) == 2
    widget._refresh_from_state()
    assert operations == []
    widget.deleteLater()
