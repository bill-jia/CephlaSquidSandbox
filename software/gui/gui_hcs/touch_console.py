"""Bench touch console — a finger-driven second window for eyepiece-less scopes.

This is the panel that lives next to the microscope on a small touchscreen, for
rigs with no room for a keyboard and mouse. The full GUI stays on the main
display and is driven remotely from the user's desk; the console covers what has
to happen with a hand on the sample: look at it, find it, focus it, and start a
saved acquisition.

Two design rules keep this cheap and safe:

1. **Same process.** Hardware in this codebase is single-owner — camera drivers
   open exclusively, the microcontroller serial port is exclusive per-process,
   and the NIDAQ holds one DO task for every line it touches. A second process
   cannot share any of it, so the console is another window in the existing Qt
   app rather than a separate program.

2. **Drive the widgets, not the controllers.** ``ObservationStateController`` is
   the single mediator for exposure/gain/illumination but emits no change
   notifications, so two UIs writing to it directly would silently disagree.
   Instead the console pokes the *same* input controls the main GUI owns
   (``entry_exposureTime``, ``btn_live``, the objective dropdown, …) and lets
   their existing signal chains apply to hardware and refresh the main window.
   Divergence becomes structurally impossible and no existing widget needs to
   change.

See ``docs/touch-console.md``.
"""

import glob
import os
import threading
import time
from typing import Callable, List, Optional

from qtpy.QtCore import QEvent, Qt, QTimer, Signal
from qtpy.QtGui import QGuiApplication
from qtpy.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

import squid.logging
from control._def import TOUCH_CONSOLE_PRESET_DIR, TOUCH_CONSOLE_SNAP_DIR

# Minimum comfortable finger target. Roughly 9 mm on a typical 10-15" panel at
# 100% scaling, which is the low end of what is reliably hittable without
# looking closely.
TOUCH_TARGET_PX = 52
# Jog buttons repeat while held so focusing feels continuous rather than like
# a series of discrete taps.
JOG_REPEAT_DELAY_MS = 400
JOG_REPEAT_INTERVAL_MS = 120

# Stage step presets offered on the panel, in mm for XY and um for Z.
XY_STEP_PRESETS_MM = [0.05, 0.2, 1.0]
Z_STEP_PRESETS_UM = [1.0, 5.0, 25.0]


class TouchButton(QPushButton):
    """A push button sized for fingers, optionally with press-and-hold repeat.

    Qt's own ``setAutoRepeat`` only repeats ``clicked``; it is used here for the
    jog controls where holding the button should keep moving the stage.
    """

    def __init__(self, text: str, checkable: bool = False, repeat: bool = False, parent=None):
        super().__init__(text, parent)
        self.setCheckable(checkable)
        self.setMinimumHeight(TOUCH_TARGET_PX)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFocusPolicy(Qt.NoFocus)
        if repeat:
            self.setAutoRepeat(True)
            self.setAutoRepeatDelay(JOG_REPEAT_DELAY_MS)
            self.setAutoRepeatInterval(JOG_REPEAT_INTERVAL_MS)


def _section_label(text: str) -> QLabel:
    label = QLabel(text)
    font = label.font()
    font.setBold(True)
    font.setPointSize(max(9, font.pointSize()))
    label.setFont(font)
    label.setContentsMargins(0, 8, 0, 2)
    return label


class TouchConsoleWindow(QMainWindow):
    """Fullscreen touch panel shown on the screen next to the microscope."""

    # Acquisition launches run on a worker thread (see _launch_acquisition), so
    # their outcome comes back to the GUI thread through this signal.
    _acquisition_launch_finished = Signal(bool, str)

    def __init__(self, main_gui, image_display_window, parent=None):
        super().__init__(parent)
        self._log = squid.logging.get_logger(self.__class__.__name__)

        # The main GUI is the console's backing model: every state-bearing
        # action is delegated to a widget it owns.
        self._gui = main_gui
        self._microscope = main_gui.microscope
        self._stage = main_gui.stage
        self._objective_store = main_gui.objectiveStore
        self._live_controller = main_gui.liveController
        self._obs_controller = main_gui.liveController.obs_controller
        self._config_repo = main_gui.microscope.config_repo

        self.imageDisplayWindow = image_display_window

        # Set while the console is writing to a main-GUI widget, so the periodic
        # refresh does not fight the user's in-flight interaction.
        self._syncing = False
        # Only the main window may close the console; a stray touch may not.
        self._shutting_down = False
        self._acquisition_running = False

        # Dispatches saved-acquisition launches. MicroscopeControlServer only
        # binds a socket in start(), which is never called here — it is used
        # purely as the already-tested executor for run_acquisition_from_yaml.
        self._acquisition_executor = None

        self._preset_buttons: List[TouchButton] = []
        self._acquisition_buttons: List[TouchButton] = []
        self._objective_buttons: List[TouchButton] = []
        self._xy_step_buttons: List[TouchButton] = []
        self._z_step_buttons: List[TouchButton] = []
        self._illumination_rows: List[QWidget] = []

        self._xy_step_mm = XY_STEP_PRESETS_MM[1]
        self._z_step_um = Z_STEP_PRESETS_UM[1]

        self.setWindowTitle("Squid — Bench Console")
        self._build_ui()
        self._apply_touch_style()

        self._acquisition_launch_finished.connect(self._on_acquisition_launch_finished)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(200)  # 5 Hz
        self._refresh_timer.timeout.connect(self._refresh_state)
        self._refresh_timer.start()

        self._refresh_state()

    # ────────────────────────────────────────────────────────────────────
    # Construction
    # ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_image_pane(), stretch=1)
        root.addWidget(self._build_control_pane())

        self.setCentralWidget(central)
        self._build_status_bar()

    def _build_image_pane(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self.imageDisplayWindow.widget, stretch=1)
        self._enable_pinch_zoom()

        zoom_row = QHBoxLayout()
        zoom_row.setContentsMargins(4, 4, 4, 4)
        zoom_row.setSpacing(4)
        btn_zoom_in = TouchButton("Zoom +")
        btn_zoom_out = TouchButton("Zoom −")
        btn_fit = TouchButton("Fit")
        btn_zoom_in.clicked.connect(lambda: self._zoom(0.8))
        btn_zoom_out.clicked.connect(lambda: self._zoom(1.25))
        btn_fit.clicked.connect(self._zoom_fit)
        for btn in (btn_zoom_out, btn_fit, btn_zoom_in):
            zoom_row.addWidget(btn)
        layout.addLayout(zoom_row)

        return pane

    def _build_control_pane(self) -> QWidget:
        column = QWidget()
        column.setFixedWidth(340)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        # ── Live / snap ────────────────────────────────────────────────
        layout.addWidget(_section_label("LIVE"))
        self.btn_live = TouchButton("Start Live", checkable=True)
        self.btn_live.clicked.connect(self._on_live_toggled)
        layout.addWidget(self.btn_live)

        live_row = QHBoxLayout()
        live_row.setSpacing(4)
        self.btn_snap = TouchButton("Snap")
        self.btn_snap.clicked.connect(self._on_snap)
        self.btn_autolevel = TouchButton("Autolevel", checkable=True)
        self.btn_autolevel.clicked.connect(self._on_autolevel_toggled)
        live_row.addWidget(self.btn_snap)
        live_row.addWidget(self.btn_autolevel)
        layout.addLayout(live_row)

        # ── Observation state presets ──────────────────────────────────
        layout.addWidget(_section_label("CHANNEL"))
        self._preset_container = QWidget()
        self._preset_layout = QGridLayout(self._preset_container)
        self._preset_layout.setContentsMargins(0, 0, 0, 0)
        self._preset_layout.setSpacing(4)
        layout.addWidget(self._preset_container)
        self._rebuild_preset_buttons()

        # ── Exposure / gain ────────────────────────────────────────────
        layout.addWidget(_section_label("EXPOSURE / GAIN"))
        self.label_exposure = QLabel("—")
        layout.addLayout(
            self._stepper_row(
                self.label_exposure,
                on_down=lambda: self._nudge_exposure(-1),
                on_up=lambda: self._nudge_exposure(1),
            )
        )
        self.label_gain = QLabel("—")
        layout.addLayout(
            self._stepper_row(
                self.label_gain,
                on_down=lambda: self._nudge_gain(-1),
                on_up=lambda: self._nudge_gain(1),
            )
        )

        # ── Illumination ───────────────────────────────────────────────
        layout.addWidget(_section_label("ILLUMINATION"))
        self._illumination_container = QWidget()
        self._illumination_layout = QVBoxLayout(self._illumination_container)
        self._illumination_layout.setContentsMargins(0, 0, 0, 0)
        self._illumination_layout.setSpacing(4)
        layout.addWidget(self._illumination_container)
        self.btn_illumination_off = TouchButton("All Illumination Off")
        self.btn_illumination_off.clicked.connect(self._on_illumination_off)
        layout.addWidget(self.btn_illumination_off)
        self._rebuild_illumination_rows()

        # ── Focus ──────────────────────────────────────────────────────
        layout.addWidget(_section_label("FOCUS"))
        layout.addLayout(self._step_selector_row(
            Z_STEP_PRESETS_UM, "µm", self._z_step_buttons, self._set_z_step, self._z_step_um
        ))
        focus_row = QHBoxLayout()
        focus_row.setSpacing(4)
        btn_z_up = TouchButton("Z ▲", repeat=True)
        btn_z_down = TouchButton("Z ▼", repeat=True)
        btn_z_up.clicked.connect(lambda: self._jog_z(1))
        btn_z_down.clicked.connect(lambda: self._jog_z(-1))
        focus_row.addWidget(btn_z_down)
        focus_row.addWidget(btn_z_up)
        layout.addLayout(focus_row)
        self.btn_autofocus = TouchButton("Autofocus")
        self.btn_autofocus.clicked.connect(self._on_autofocus)
        layout.addWidget(self.btn_autofocus)

        # ── Stage ──────────────────────────────────────────────────────
        layout.addWidget(_section_label("STAGE"))
        layout.addLayout(self._step_selector_row(
            XY_STEP_PRESETS_MM, "mm", self._xy_step_buttons, self._set_xy_step, self._xy_step_mm
        ))
        layout.addLayout(self._build_dpad())

        # ── Objective ──────────────────────────────────────────────────
        layout.addWidget(_section_label("OBJECTIVE"))
        self._objective_container = QWidget()
        self._objective_layout = QGridLayout(self._objective_container)
        self._objective_layout.setContentsMargins(0, 0, 0, 0)
        self._objective_layout.setSpacing(4)
        layout.addWidget(self._objective_container)
        self._rebuild_objective_buttons()

        # ── Saved acquisitions ─────────────────────────────────────────
        layout.addWidget(_section_label("ACQUIRE"))
        self._acquisition_container = QWidget()
        self._acquisition_layout = QVBoxLayout(self._acquisition_container)
        self._acquisition_layout.setContentsMargins(0, 0, 0, 0)
        self._acquisition_layout.setSpacing(4)
        layout.addWidget(self._acquisition_container)
        self._rebuild_acquisition_buttons()

        self.label_progress = QLabel("")
        self.label_progress.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.label_progress)

        self.btn_abort = TouchButton("Abort Acquisition")
        self.btn_abort.setStyleSheet("background-color: #FFC2C2; font-weight: bold;")
        self.btn_abort.clicked.connect(self._on_abort)
        self.btn_abort.setEnabled(False)
        layout.addWidget(self.btn_abort)

        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(column)
        scroll.setFixedWidth(360)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return scroll

    def _stepper_row(self, value_label: QLabel, on_down: Callable, on_up: Callable) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)
        btn_down = TouchButton("−", repeat=True)
        btn_up = TouchButton("+", repeat=True)
        btn_down.setFixedWidth(TOUCH_TARGET_PX + 8)
        btn_up.setFixedWidth(TOUCH_TARGET_PX + 8)
        btn_down.clicked.connect(on_down)
        btn_up.clicked.connect(on_up)
        value_label.setAlignment(Qt.AlignCenter)
        row.addWidget(btn_down)
        row.addWidget(value_label, stretch=1)
        row.addWidget(btn_up)
        return row

    def _step_selector_row(
        self,
        presets: List[float],
        suffix: str,
        button_list: List[TouchButton],
        on_select: Callable[[float], None],
        current: float,
    ) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)
        for value in presets:
            text = f"{value:g} {suffix}"
            btn = TouchButton(text, checkable=True)
            btn.setChecked(value == current)
            btn.clicked.connect(lambda _checked, v=value: on_select(v))
            button_list.append(btn)
            row.addWidget(btn)
        return row

    def _build_dpad(self) -> QGridLayout:
        pad = QGridLayout()
        pad.setSpacing(4)
        btn_up = TouchButton("▲", repeat=True)
        btn_down = TouchButton("▼", repeat=True)
        btn_left = TouchButton("◀", repeat=True)
        btn_right = TouchButton("▶", repeat=True)
        btn_up.clicked.connect(lambda: self._jog_xy(0, 1))
        btn_down.clicked.connect(lambda: self._jog_xy(0, -1))
        btn_left.clicked.connect(lambda: self._jog_xy(-1, 0))
        btn_right.clicked.connect(lambda: self._jog_xy(1, 0))
        pad.addWidget(btn_up, 0, 1)
        pad.addWidget(btn_left, 1, 0)
        pad.addWidget(btn_right, 1, 2)
        pad.addWidget(btn_down, 2, 1)
        return pad

    def _build_status_bar(self) -> None:
        self.label_status_position = QLabel("—")
        self.label_status_state = QLabel("—")
        self.label_status_acquisition = QLabel("")
        bar = self.statusBar()
        bar.addWidget(self.label_status_position)
        bar.addWidget(self.label_status_state, stretch=1)
        bar.addPermanentWidget(self.label_status_acquisition)

    def _apply_touch_style(self) -> None:
        # Scoped to this window so the main GUI's appearance is untouched.
        self.setStyleSheet(
            f"""
            QPushButton {{
                min-height: {TOUCH_TARGET_PX}px;
                font-size: 15px;
                padding: 4px 8px;
            }}
            QPushButton:checked {{
                background-color: #C2C2FF;
                font-weight: bold;
            }}
            QLabel {{ font-size: 15px; }}
            QStatusBar QLabel {{ font-size: 14px; }}
            """
        )

    # ────────────────────────────────────────────────────────────────────
    # Dynamic sections
    # ────────────────────────────────────────────────────────────────────

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _rebuild_preset_buttons(self) -> None:
        self._clear_layout(self._preset_layout)
        self._preset_buttons = []
        try:
            presets = self._config_repo.list_observation_presets()
        except Exception as e:
            self._log.warning("Could not list observation presets: %s", e)
            presets = []

        if not presets:
            self._preset_layout.addWidget(QLabel("No saved presets"), 0, 0)
            return

        for index, name in enumerate(presets):
            btn = TouchButton(name, checkable=True)
            btn.clicked.connect(lambda _checked, n=name: self._on_preset_selected(n))
            self._preset_buttons.append(btn)
            self._preset_layout.addWidget(btn, index // 2, index % 2)

    def _rebuild_objective_buttons(self) -> None:
        self._clear_layout(self._objective_layout)
        self._objective_buttons = []
        names = list(self._objective_store.objectives_dict.keys())
        for index, name in enumerate(names):
            btn = TouchButton(name, checkable=True)
            btn.clicked.connect(lambda _checked, n=name: self._on_objective_selected(n))
            self._objective_buttons.append(btn)
            self._objective_layout.addWidget(btn, index // 2, index % 2)

    def _rebuild_acquisition_buttons(self) -> None:
        self._clear_layout(self._acquisition_layout)
        self._acquisition_buttons = []

        paths = sorted(glob.glob(os.path.join(TOUCH_CONSOLE_PRESET_DIR, "*.yaml")))
        if not paths:
            hint = QLabel(f"No presets in\n{TOUCH_CONSOLE_PRESET_DIR}")
            hint.setWordWrap(True)
            self._acquisition_layout.addWidget(hint)
            return

        for path in paths:
            name = os.path.splitext(os.path.basename(path))[0]
            btn = TouchButton(name)
            btn.clicked.connect(lambda _checked, p=path, n=name: self._on_acquisition_requested(p, n))
            self._acquisition_buttons.append(btn)
            self._acquisition_layout.addWidget(btn)

    def _rebuild_illumination_rows(self) -> None:
        """Rebuild one row per illuminator in the active observation state.

        The set of illuminators changes with the observation state, so this is
        re-run whenever the active state's illuminator names change.
        """
        self._clear_layout(self._illumination_layout)
        self._illumination_rows = []

        state = self._obs_controller.current_observation_state
        if state is None or not state.illuminator_states:
            self._illumination_layout.addWidget(QLabel("No active illuminators"))
            self._illumination_names = []
            return

        self._illumination_names = [ist.illumination_channel for ist in state.illuminator_states]
        for ist in state.illuminator_states:
            channel = ist.illumination_channel
            row_widget = QWidget()
            block = QVBoxLayout(row_widget)
            block.setContentsMargins(0, 0, 0, 0)
            block.setSpacing(2)

            btn_toggle = TouchButton(channel, checkable=True)
            btn_toggle.setChecked(ist.on)
            btn_toggle.clicked.connect(
                lambda checked, c=channel: self._on_illumination_toggled(c, checked)
            )
            block.addWidget(btn_toggle)

            # Slider for coarse moves, ±1% for the fine adjustment a finger
            # cannot make on a slider.
            row = QHBoxLayout()
            row.setSpacing(4)

            btn_down = TouchButton("−", repeat=True)
            btn_up = TouchButton("+", repeat=True)
            btn_down.setFixedWidth(TOUCH_TARGET_PX)
            btn_up.setFixedWidth(TOUCH_TARGET_PX)
            btn_down.clicked.connect(lambda _c, ch=channel: self._nudge_intensity(ch, -1))
            btn_up.clicked.connect(lambda _c, ch=channel: self._nudge_intensity(ch, 1))

            slider = QSlider(Qt.Horizontal)
            slider.setMinimum(0)
            slider.setMaximum(100)
            slider.setValue(int(round(ist.intensity)))
            slider.setMinimumHeight(TOUCH_TARGET_PX)
            slider.setFocusPolicy(Qt.NoFocus)
            slider.valueChanged.connect(
                lambda value, ch=channel: self._on_intensity_slider(ch, value)
            )

            label_intensity = QLabel(f"{ist.intensity:.0f}%")
            label_intensity.setAlignment(Qt.AlignCenter)
            label_intensity.setFixedWidth(56)

            row.addWidget(btn_down)
            row.addWidget(slider, stretch=1)
            row.addWidget(label_intensity)
            row.addWidget(btn_up)
            block.addLayout(row)

            row_widget._channel = channel
            row_widget._btn_toggle = btn_toggle
            row_widget._slider = slider
            row_widget._label_intensity = label_intensity
            self._illumination_rows.append(row_widget)
            self._illumination_layout.addWidget(row_widget)

    # ────────────────────────────────────────────────────────────────────
    # Actions — each delegates to a main-GUI widget
    # ────────────────────────────────────────────────────────────────────

    def _on_live_toggled(self, checked: bool) -> None:
        widget = self._gui.liveControlWidget
        if widget is None:
            return
        with self._sync():
            # click() toggles the checkable button and emits clicked(checked),
            # which is exactly what a press on the main GUI would do.
            if widget.btn_live.isChecked() != checked:
                widget.btn_live.click()

    def _on_snap(self) -> None:
        widget = self._gui.liveControlWidget
        if widget is None:
            return
        os.makedirs(TOUCH_CONSOLE_SNAP_DIR, exist_ok=True)
        with self._sync():
            # The console has no text entry, so the destination is fixed and the
            # tag identifies where the frame came from.
            widget.lineEdit_snapSavingDir.setText(TOUCH_CONSOLE_SNAP_DIR)
            widget.snap_saving_path = TOUCH_CONSOLE_SNAP_DIR
            widget.lineEdit_snapTag.setText(f"bench_{time.strftime('%H%M%S')}")
            widget.snap_frame()
        self.label_status_acquisition.setText("Snapped")

    def _on_autolevel_toggled(self, checked: bool) -> None:
        widget = self._gui.liveControlWidget
        if widget is None:
            return
        with self._sync():
            if widget.btn_autolevel.isChecked() != checked:
                widget.btn_autolevel.click()

    def _on_preset_selected(self, name: str) -> None:
        from gui.widgets.observation_state_dialogs import run_load_observation_state

        with self._sync():
            loaded = run_load_observation_state(
                self,
                self._config_repo,
                self._live_controller,
                self._objective_store,
                self._gui.emission_filter_wheel,
                preset_name=name,
            )
        if loaded:
            self._rebuild_illumination_rows()
        self._refresh_state()

    def _on_objective_selected(self, name: str) -> None:
        widget = self._gui.objectivesWidget
        if widget is None:
            return
        with self._sync():
            # Setting the dropdown fires currentTextChanged, which runs the
            # objective change and updates the main GUI in one path.
            widget.dropdown.setCurrentText(name)

    def _nudge_exposure(self, direction: int) -> None:
        widget = self._gui.cameraSettingWidget
        if widget is None:
            return
        entry = widget.entry_exposureTime
        with self._sync():
            entry.setValue(entry.value() + direction * entry.singleStep())

    def _nudge_gain(self, direction: int) -> None:
        widget = self._gui.cameraSettingWidget
        if widget is None:
            return
        entry = widget.entry_analogGain
        if not entry.isEnabled():
            return
        with self._sync():
            entry.setValue(entry.value() + direction * entry.singleStep())

    def _nudge_intensity(self, channel: str, direction: int) -> None:
        state = self._obs_controller.current_observation_state
        if state is None:
            return
        for ist in state.illuminator_states:
            if ist.illumination_channel == channel:
                self._set_intensity(channel, ist.intensity + direction * 1.0)
                break

    def _on_intensity_slider(self, channel: str, value: int) -> None:
        if self._syncing:
            return
        self._set_intensity(channel, float(value))

    def _set_intensity(self, channel: str, value: float) -> None:
        value = min(100.0, max(0.0, value))
        with self._sync():
            self._obs_controller.set_illumination_intensity(channel, value)
        self._refresh_illumination()

    def _on_illumination_toggled(self, channel: str, on: bool) -> None:
        with self._sync():
            self._obs_controller.set_illumination_on_off(channel, on)
        self._refresh_illumination()

    def _on_illumination_off(self) -> None:
        with self._sync():
            self._obs_controller.turn_off_illumination()
        self._refresh_illumination()

    def _jog_z(self, direction: int) -> None:
        widget = self._gui.navigationWidget
        if widget is None:
            return
        with self._sync():
            widget.set_deltaZ(self._z_step_um)
            if direction > 0:
                widget.move_z_forward()
            else:
                widget.move_z_backward()

    def _jog_xy(self, dx: int, dy: int) -> None:
        widget = self._gui.navigationWidget
        if widget is None:
            return
        with self._sync():
            if dx:
                widget.set_deltaX(self._xy_step_mm)
                widget.move_x_forward() if dx > 0 else widget.move_x_backward()
            if dy:
                widget.set_deltaY(self._xy_step_mm)
                widget.move_y_forward() if dy > 0 else widget.move_y_backward()

    def _on_autofocus(self) -> None:
        controller = self._gui.autofocusController
        if controller is None:
            return
        self.label_status_acquisition.setText("Autofocusing…")
        controller.autofocus()

    # ── Saved acquisitions ──────────────────────────────────────────────

    def _on_acquisition_requested(self, yaml_path: str, name: str) -> None:
        reply = QMessageBox.question(
            self,
            "Start acquisition",
            f"Start '{name}'?\n\nThe scope will be unavailable at the bench until it finishes.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self._launch_acquisition(yaml_path, name)

    def _launch_acquisition(self, yaml_path: str, name: str) -> None:
        """Start a saved acquisition on a worker thread.

        This must not run on the GUI thread. ``_cmd_run_acquisition_from_yaml``
        was written for the control server's socket thread and marshals onto the
        GUI thread internally — via ``QTimer.singleShot`` plus a blocking wait,
        and a ``BlockingQueuedConnection``. Calling it from the GUI thread would
        deadlock on the latter and silently time out on the former.
        """
        if self._acquisition_executor is None:
            from control.microscope_control_server import MicroscopeControlServer

            self._acquisition_executor = MicroscopeControlServer(
                microscope=self._microscope,
                multipoint_controller=self._gui.multipointController,
                scan_coordinates=self._gui.scanCoordinates,
                gui=self._gui,
            )

        self.label_progress.setText(f"Starting {name}…")
        for btn in self._acquisition_buttons:
            btn.setEnabled(False)

        def run():
            try:
                self._acquisition_executor._cmd_run_acquisition_from_yaml(yaml_path=yaml_path)
                self._acquisition_launch_finished.emit(True, name)
            except Exception as e:
                self._log.error("Bench console failed to start acquisition %r: %s", name, e)
                self._acquisition_launch_finished.emit(False, str(e))

        threading.Thread(target=run, name="touch-console-acquisition-launch", daemon=True).start()

    def _on_acquisition_launch_finished(self, ok: bool, message: str) -> None:
        if ok:
            self.label_progress.setText(f"Running {message}")
            return
        self.label_progress.setText("")
        for btn in self._acquisition_buttons:
            btn.setEnabled(True)
        QMessageBox.warning(self, "Acquisition failed to start", message)

    def _on_abort(self) -> None:
        controller = self._gui.multipointController
        if controller is None:
            return
        reply = QMessageBox.question(
            self,
            "Abort acquisition",
            "Abort the running acquisition?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        controller.request_abort_acquisition()
        self.label_progress.setText("Aborting…")

    def on_acquisition_progress(self, current: int, total: int, _region: int) -> None:
        if total:
            self.label_progress.setText(f"{current} / {total}")

    def _set_xy_step(self, value: float) -> None:
        self._xy_step_mm = value
        for btn in self._xy_step_buttons:
            btn.setChecked(btn.text().startswith(f"{value:g}"))

    def _set_z_step(self, value: float) -> None:
        self._z_step_um = value
        for btn in self._z_step_buttons:
            btn.setChecked(btn.text().startswith(f"{value:g}"))

    def _zoom(self, factor: float) -> None:
        view_box = self._view_box()
        if view_box is not None:
            view_box.scaleBy((factor, factor))

    def _zoom_fit(self) -> None:
        view_box = self._view_box()
        if view_box is not None:
            view_box.autoRange()

    def _enable_pinch_zoom(self) -> None:
        """Let two fingers zoom the live image.

        Single-finger drag already pans, because Qt synthesizes mouse events for
        unhandled touches and pyqtgraph's ViewBox handles those. Pinch has no
        mouse equivalent, so the gesture has to be grabbed explicitly. The zoom
        buttons remain the fallback if any of this is unavailable.
        """
        self._pinch_target = None
        try:
            graphics_widget = self.imageDisplayWindow.graphics_widget
            target = graphics_widget.viewport()
            target.grabGesture(Qt.PinchGesture)
            target.installEventFilter(self)
            self._pinch_target = target
        except Exception as e:
            self._log.info("Pinch zoom unavailable (%s); using the zoom buttons instead", e)

    def eventFilter(self, watched, event):
        if watched is getattr(self, "_pinch_target", None) and event.type() == QEvent.Gesture:
            pinch = event.gesture(Qt.PinchGesture)
            if pinch is not None:
                scale = pinch.scaleFactor()
                if scale and scale > 0:
                    # ViewBox.scaleBy shrinks the visible range, so pinching
                    # apart (scale > 1) must scale the range down to zoom in.
                    self._zoom(1.0 / scale)
                return True
        return super().eventFilter(watched, event)

    def _view_box(self):
        # _get_image_view resolves the ViewBox for both LUT and non-LUT layouts.
        try:
            return self.imageDisplayWindow._get_image_view()
        except AttributeError:
            self._log.debug("Image display has no reachable view box; zoom disabled")
            return None

    # ────────────────────────────────────────────────────────────────────
    # State refresh
    # ────────────────────────────────────────────────────────────────────

    class _SyncGuard:
        def __init__(self, console):
            self._console = console

        def __enter__(self):
            self._console._syncing = True

        def __exit__(self, *exc_info):
            self._console._syncing = False
            return False

    def _sync(self) -> "_SyncGuard":
        return TouchConsoleWindow._SyncGuard(self)

    def _refresh_state(self) -> None:
        if self._syncing:
            return
        self._refresh_status_bar()
        if self._acquisition_running:
            # The acquisition drives observation state per channel; mirroring
            # that would churn the panel and re-create controls we just
            # disabled. Status only until it finishes.
            return
        self._refresh_live_and_camera()
        self._refresh_illumination()
        self._refresh_selection_highlights()

    def _refresh_live_and_camera(self) -> None:
        live_widget = self._gui.liveControlWidget
        if live_widget is not None:
            is_live = bool(self._live_controller.is_live)
            if self.btn_live.isChecked() != is_live:
                self.btn_live.setChecked(is_live)
            self.btn_live.setText("Stop Live" if is_live else "Start Live")
            if self.btn_autolevel.isChecked() != live_widget.btn_autolevel.isChecked():
                self.btn_autolevel.setChecked(live_widget.btn_autolevel.isChecked())

        camera_widget = self._gui.cameraSettingWidget
        if camera_widget is not None:
            self.label_exposure.setText(f"{camera_widget.entry_exposureTime.value():.1f} ms")
            if camera_widget.entry_analogGain.isEnabled():
                self.label_gain.setText(f"gain {camera_widget.entry_analogGain.value():.1f}")
            else:
                self.label_gain.setText("gain n/a")

    def _refresh_illumination(self) -> None:
        state = self._obs_controller.current_observation_state
        if state is None:
            return
        names = [ist.illumination_channel for ist in state.illuminator_states]
        if names != getattr(self, "_illumination_names", None):
            self._rebuild_illumination_rows()
            return
        by_name = {ist.illumination_channel: ist for ist in state.illuminator_states}
        for row in self._illumination_rows:
            ist = by_name.get(row._channel)
            if ist is None:
                continue
            if row._btn_toggle.isChecked() != ist.on:
                row._btn_toggle.setChecked(ist.on)
            row._label_intensity.setText(f"{ist.intensity:.0f}%")
            # Mirror the hardware value back without re-emitting into
            # _on_intensity_slider and looping.
            slider_value = int(round(ist.intensity))
            if row._slider.value() != slider_value:
                row._slider.blockSignals(True)
                row._slider.setValue(slider_value)
                row._slider.blockSignals(False)

    def _refresh_selection_highlights(self) -> None:
        current_objective = self._objective_store.current_objective
        for btn in self._objective_buttons:
            btn.setChecked(btn.text() == current_objective)

        active_state = self._obs_controller.get_active_channel_name()
        for btn in self._preset_buttons:
            btn.setChecked(btn.text() == active_state)

    def _refresh_status_bar(self) -> None:
        try:
            pos = self._stage.get_pos()
            self.label_status_position.setText(
                f"X {pos.x_mm:.3f}  Y {pos.y_mm:.3f}  Z {pos.z_mm * 1000:.1f} µm"
            )
        except Exception:
            self.label_status_position.setText("position unavailable")

        state_name = self._obs_controller.get_active_channel_name() or "—"
        self.label_status_state.setText(
            f"{self._objective_store.current_objective}  ·  {state_name}"
        )

    # ────────────────────────────────────────────────────────────────────
    # Acquisition lock-out
    # ────────────────────────────────────────────────────────────────────

    def set_acquisition_running(self, running: bool) -> None:
        """Lock the panel's hardware controls while an acquisition owns the scope.

        Abort stays live — it is the one thing the operator must still be able
        to reach from the bench.
        """
        if running == self._acquisition_running:
            return
        self._acquisition_running = running
        for widget in self.findChildren(TouchButton):
            if widget is not self.btn_abort:
                widget.setEnabled(not running)
        self.btn_abort.setEnabled(running)
        self.label_status_acquisition.setText("ACQUIRING" if running else "")
        if not running:
            self.label_progress.setText("")

    # ────────────────────────────────────────────────────────────────────
    # Placement and lifetime
    # ────────────────────────────────────────────────────────────────────

    def show_on_screen(self, screen_index: Optional[int]) -> None:
        """Show fullscreen on ``screen_index``, or windowed if it does not exist."""
        screens = QGuiApplication.screens()
        if screen_index is not None and 0 <= screen_index < len(screens):
            screen = screens[screen_index]
            self.setGeometry(screen.geometry())
            self.showFullScreen()
            self._log.info(
                "Touch console on screen %d (%s)", screen_index, screen.geometry().getRect()
            )
        else:
            self._log.warning(
                "Touch console screen %s unavailable (%d screen(s) present); "
                "showing as a normal window instead",
                screen_index,
                len(screens),
            )
            self.resize(1280, 800)
            self.show()

    def prepare_for_shutdown(self) -> None:
        """Allow the window to close. Only the main window may call this."""
        self._shutting_down = True
        self._refresh_timer.stop()

    def closeEvent(self, event) -> None:
        # A stray touch must not be able to dismiss the panel and strand the
        # user at the bench with no display.
        if not self._shutting_down:
            event.ignore()
            return
        event.accept()
