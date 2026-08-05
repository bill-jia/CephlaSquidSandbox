"""Tests for the bench touch console (gui/gui_hcs/touch_console.py).

The console's whole contract is that it delegates to the main GUI's widgets
rather than driving controllers itself, so these tests stand a stub GUI in for
the real one and assert the delegation actually lands. No hardware and no
Microscope are involved.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_API", "pyqt5")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication, QComboBox, QDoubleSpinBox, QLineEdit, QPushButton

import control._def
from control.core.core import ImageDisplayWindow
from control.core.stream_handler import StreamHandler, StreamHandlerFunctions
from control.models.observation_state import CameraSettings, IlluminatorState, ObservationState
from gui.gui_hcs.touch_console import TouchConsoleWindow
from squid.abc import CameraFrame


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ─────────────────────────────────────────────────────────────────────────────
# Stubs standing in for the main GUI
# ─────────────────────────────────────────────────────────────────────────────


class StubObsController:
    def __init__(self, state):
        self.current_observation_state = state
        self.intensity_calls = []
        self.on_off_calls = []
        self.turn_off_calls = 0

    def get_active_channel_name(self):
        return self.current_observation_state.name

    def set_illumination_intensity(self, channel, intensity):
        self.intensity_calls.append((channel, intensity))
        for ist in self.current_observation_state.illuminator_states:
            if ist.illumination_channel == channel:
                ist.intensity = intensity

    def set_illumination_on_off(self, channel, is_on):
        self.on_off_calls.append((channel, is_on))
        for ist in self.current_observation_state.illuminator_states:
            if ist.illumination_channel == channel:
                ist.on = is_on

    def turn_off_illumination(self):
        self.turn_off_calls += 1
        for ist in self.current_observation_state.illuminator_states:
            ist.on = False


class StubStage:
    class Pos:
        x_mm, y_mm, z_mm = 12.345, 6.789, 0.0123

    def __init__(self):
        self.moves = []

    def get_pos(self):
        return StubStage.Pos()

    def move_x(self, d):
        self.moves.append(("x", d))

    def move_y(self, d):
        self.moves.append(("y", d))

    def move_z(self, d):
        self.moves.append(("z", d))


class StubObjectiveStore:
    def __init__(self):
        self.objectives_dict = {"10x": {}, "20x": {}, "40x": {}}
        self.current_objective = "10x"

    def set_current_objective(self, name):
        self.current_objective = name


class StubLiveControlWidget:
    def __init__(self):
        self.btn_live = QPushButton()
        self.btn_live.setCheckable(True)
        self.btn_autolevel = QPushButton()
        self.btn_autolevel.setCheckable(True)
        self.lineEdit_snapSavingDir = QLineEdit()
        self.lineEdit_snapTag = QLineEdit()
        self.snap_saving_path = ""
        self.snaps = 0
        self.btn_live.clicked.connect(
            lambda checked: self.btn_live.setText("Stop Live" if checked else "Start Live")
        )

    def snap_frame(self):
        self.snaps += 1


class StubCameraSettingsWidget:
    def __init__(self):
        self.entry_exposureTime = QDoubleSpinBox()
        self.entry_exposureTime.setRange(0.1, 1000)
        self.entry_exposureTime.setSingleStep(1)
        self.entry_exposureTime.setValue(20)
        self.entry_analogGain = QDoubleSpinBox()
        self.entry_analogGain.setRange(0, 24)
        self.entry_analogGain.setSingleStep(1)
        self.entry_analogGain.setValue(0)


class StubNavigationWidget:
    def __init__(self, stage):
        self.stage = stage
        self.dX = self.dY = self.dZ = 0.0

    def set_deltaX(self, v):
        self.dX = v

    def set_deltaY(self, v):
        self.dY = v

    def set_deltaZ(self, v):
        self.dZ = v

    def move_x_forward(self):
        self.stage.move_x(self.dX)

    def move_x_backward(self):
        self.stage.move_x(-self.dX)

    def move_y_forward(self):
        self.stage.move_y(self.dY)

    def move_y_backward(self):
        self.stage.move_y(-self.dY)

    def move_z_forward(self):
        self.stage.move_z(self.dZ / 1000)

    def move_z_backward(self):
        self.stage.move_z(-self.dZ / 1000)


class StubGui:
    def __init__(self):
        state = ObservationState(
            name="GFP",
            camera_settings=CameraSettings(exposure_time_ms=20, gain_mode=0),
            illuminator_states=[
                IlluminatorState(illumination_channel="488", intensity=30.0, on=True),
                IlluminatorState(illumination_channel="561", intensity=50.0, on=False),
            ],
        )
        self.config_repo = type("Repo", (), {"list_observation_presets": lambda self: ["BF", "GFP", "mCherry"]})()
        self.obs_controller = StubObsController(state)
        self.liveController = type("LC", (), {"is_live": False, "obs_controller": self.obs_controller})()
        self.microscope = type("M", (), {"config_repo": self.config_repo})()
        self.stage = StubStage()
        self.objectiveStore = StubObjectiveStore()
        self.liveControlWidget = StubLiveControlWidget()
        self.cameraSettingWidget = StubCameraSettingsWidget()
        self.navigationWidget = StubNavigationWidget(self.stage)
        self.objectivesWidget = self._make_objectives_widget()
        self.autofocusController = type("AF", (), {"calls": 0, "autofocus": lambda s: setattr(s, "calls", s.calls + 1)})()
        self.multipointController = type("MP", (), {"aborts": 0, "request_abort_acquisition": lambda s: setattr(s, "aborts", s.aborts + 1)})()
        self.scanCoordinates = None
        self.emission_filter_wheel = None

    def _make_objectives_widget(self):
        widget = type("OW", (), {})()
        widget.dropdown = QComboBox()
        widget.dropdown.addItems(self.objectiveStore.objectives_dict.keys())
        widget.dropdown.setCurrentText(self.objectiveStore.current_objective)
        widget.dropdown.currentTextChanged.connect(self.objectiveStore.set_current_objective)
        return widget


@pytest.fixture
def console(qapp):
    gui_stub = StubGui()
    display = ImageDisplayWindow(None, None, show_LUT=False, autoLevels=True)
    window = TouchConsoleWindow(gui_stub, display)
    yield window, gui_stub
    window.prepare_for_shutdown()
    window.close()


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestConstruction:
    def test_builds_a_button_per_preset(self, console):
        window, _ = console
        assert [b.text() for b in window._preset_buttons] == ["BF", "GFP", "mCherry"]

    def test_builds_a_button_per_objective(self, console):
        window, _ = console
        assert [b.text() for b in window._objective_buttons] == ["10x", "20x", "40x"]

    def test_builds_a_row_per_illuminator(self, console):
        window, _ = console
        assert [row._channel for row in window._illumination_rows] == ["488", "561"]


class TestWindowLifetime:
    def test_absent_screen_falls_back_to_a_window(self, console):
        window, _ = console
        window.show_on_screen(99)
        assert window.isVisible()
        assert not window.isFullScreen()

    def test_stray_close_is_refused(self, console):
        window, _ = console
        window.show_on_screen(99)
        window.close()
        assert window.isVisible(), "a stray touch must not strand the user with no display"

    def test_closes_once_shutdown_is_flagged(self, console):
        window, _ = console
        window.show_on_screen(99)
        window.prepare_for_shutdown()
        window.close()
        assert not window.isVisible()


class TestDelegatesToMainGui:
    """The console must never hold its own copy of state — it drives the widgets."""

    def test_exposure_moves_the_main_gui_spinbox(self, console):
        window, gui_stub = console
        entry = gui_stub.cameraSettingWidget.entry_exposureTime
        window._nudge_exposure(1)
        assert entry.value() == 21.0
        window._nudge_exposure(-1)
        assert entry.value() == 20.0

    def test_gain_moves_the_main_gui_spinbox(self, console):
        window, gui_stub = console
        window._nudge_gain(1)
        assert gui_stub.cameraSettingWidget.entry_analogGain.value() == 1.0

    def test_live_presses_the_main_gui_button(self, console):
        window, gui_stub = console
        window._on_live_toggled(True)
        assert gui_stub.liveControlWidget.btn_live.isChecked()
        assert gui_stub.liveControlWidget.btn_live.text() == "Stop Live"
        window._on_live_toggled(False)
        assert not gui_stub.liveControlWidget.btn_live.isChecked()

    def test_objective_goes_through_the_dropdown(self, console):
        window, gui_stub = console
        window._on_objective_selected("40x")
        assert gui_stub.objectiveStore.current_objective == "40x"

    def test_autofocus_calls_the_controller(self, console):
        window, gui_stub = console
        window._on_autofocus()
        assert gui_stub.autofocusController.calls == 1


class TestReverseSync:
    def test_console_follows_a_change_made_in_the_main_gui(self, console):
        window, gui_stub = console
        gui_stub.cameraSettingWidget.entry_exposureTime.setValue(37.0)
        window._refresh_state()
        assert window.label_exposure.text().startswith("37.0")

    def test_status_bar_reports_position_objective_and_state(self, console):
        window, _ = console
        window._refresh_status_bar()
        assert "12.345" in window.label_status_position.text()
        assert "10x" in window.label_status_state.text()
        assert "GFP" in window.label_status_state.text()


class TestSnap:
    def test_snap_needs_no_typing(self, console):
        window, gui_stub = console
        window._on_snap()
        assert gui_stub.liveControlWidget.snaps == 1
        assert gui_stub.liveControlWidget.snap_saving_path == control._def.TOUCH_CONSOLE_SNAP_DIR
        assert gui_stub.liveControlWidget.lineEdit_snapTag.text().startswith("bench_")


class TestStageJog:
    def test_xy_jog_uses_the_selected_step(self, console):
        window, gui_stub = console
        window._set_xy_step(1.0)
        window._jog_xy(1, 0)
        assert gui_stub.stage.moves == [("x", 1.0)]

    def test_z_jog_converts_um_to_mm(self, console):
        window, gui_stub = console
        window._set_z_step(25.0)
        window._jog_z(-1)
        assert gui_stub.stage.moves == [("z", -0.025)]


class TestIllumination:
    def test_steppers_move_by_one_percent(self, console):
        window, gui_stub = console
        window._nudge_intensity("488", 1)
        assert gui_stub.obs_controller.intensity_calls[-1] == ("488", 31.0)
        window._nudge_intensity("488", -1)
        assert gui_stub.obs_controller.intensity_calls[-1] == ("488", 30.0)

    def test_intensity_is_clamped_to_the_valid_range(self, console):
        window, gui_stub = console
        window._set_intensity("488", 150.0)
        assert gui_stub.obs_controller.intensity_calls[-1] == ("488", 100.0)
        window._set_intensity("488", -20.0)
        assert gui_stub.obs_controller.intensity_calls[-1] == ("488", 0.0)

    def test_slider_mirrors_and_sets_intensity(self, console):
        window, gui_stub = console
        row = window._illumination_rows[0]
        assert row._slider.value() == 30
        row._slider.setValue(75)
        assert gui_stub.obs_controller.intensity_calls[-1] == ("488", 75.0)

    def test_refresh_does_not_loop_back_into_the_controller(self, console):
        """A refresh writing the slider must not re-emit valueChanged."""
        window, gui_stub = console
        before = len(gui_stub.obs_controller.intensity_calls)
        window._refresh_illumination()
        window._refresh_illumination()
        assert len(gui_stub.obs_controller.intensity_calls) == before

    def test_master_off_turns_everything_off(self, console):
        window, gui_stub = console
        window._on_illumination_off()
        assert gui_stub.obs_controller.turn_off_calls == 1
        assert all(not i.on for i in gui_stub.obs_controller.current_observation_state.illuminator_states)


class TestAcquisitionLockout:
    def test_controls_disabled_but_abort_stays_live(self, console):
        window, _ = console
        window.set_acquisition_running(True)
        assert not window.btn_live.isEnabled()
        assert window.btn_abort.isEnabled(), "abort must stay reachable from the bench"

    def test_refresh_does_not_re_enable_controls_mid_acquisition(self, console):
        window, _ = console
        window.set_acquisition_running(True)
        window._refresh_state()
        assert not window.btn_live.isEnabled()

    def test_progress_is_reported_and_cleared(self, console):
        window, _ = console
        window.set_acquisition_running(True)
        window.on_acquisition_progress(3, 10, 0)
        assert window.label_progress.text() == "3 / 10"
        window.set_acquisition_running(False)
        assert window.label_progress.text() == ""
        assert window.btn_live.isEnabled()
        assert not window.btn_abort.isEnabled()


class TestDecimation:
    """display_max_dim must be a true resize; display_resolution_scaling is a crop."""

    @staticmethod
    def _run(handler):
        seen = {}
        handler._fns = StreamHandlerFunctions(
            image_to_display=lambda img: seen.__setitem__("shape", img.shape),
            packet_image_to_write=lambda *a: None,
            signal_new_frame_received=lambda: None,
            accept_new_frame=lambda: True,
        )
        handler.fps_display = 10000
        frame = np.zeros((2400, 2400), dtype=np.uint16)
        handler.on_new_frame(
            CameraFrame(frame_id=1, timestamp=0.0, frame=frame, frame_format=None, frame_pixel_format=None)
        )
        return seen["shape"]

    def test_console_handler_resizes_to_max_dim(self):
        handler = StreamHandler(None, display_max_dim=1024)
        assert self._run(handler) == (1024, 1024)

    def test_main_handler_is_unchanged(self):
        handler = StreamHandler(None)
        assert self._run(handler) == (2400, 2400)

    def test_frames_already_within_max_dim_are_untouched(self):
        from control.core.downsampled_views import downsample_to_max_dim

        small = np.zeros((512, 512), dtype=np.uint16)
        assert downsample_to_max_dim(small, 1024) is small

    def test_aspect_ratio_is_preserved(self):
        from control.core.downsampled_views import downsample_to_max_dim

        wide = np.zeros((600, 2400), dtype=np.uint16)
        assert downsample_to_max_dim(wide, 1200).shape == (300, 1200)
