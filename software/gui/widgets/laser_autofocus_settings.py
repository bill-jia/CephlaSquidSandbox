"""Laser autofocus configuration UI for the multipoint acquisition widgets.

`LaserAutofocusButton` is a drop-in replacement for the old `QCheckBox("Laser AF")`
control. It exposes the same `isChecked` / `setChecked` / `toggled` surface so
the surrounding multipoint widget code doesn't change, but clicking it opens
`LaserAutofocusSettingsDialog` rather than toggling a bool.

The dialog lets the user pick between:
  - **Fast mode**: the per-FOV offset table + periodic anchor refresh path
    (`laser_af_refresh_every_n_fovs >= 2`).
  - **Legacy mode**: full laser AF at every FOV
    (`laser_af_refresh_every_n_fovs = 1`, `laser_af_seed_mode = "lazy"`; behaves
    identically to the pre-table code path).

Plus the fast-mode parameters: seed behavior (scan vs lazy), refresh cadence,
consistency warning threshold, and the end-of-region displacement check.
"""

import os

os.environ.setdefault("QT_API", "pyqt5")

from qtpy.QtCore import Signal
from qtpy.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)


class LaserAutofocusSettingsDialog(QDialog):
    """Modal editor for laser-AF behavior.

    Reads initial values from the supplied MultiPointController (attributes
    `do_reflection_af`, `laser_af_seed_mode`, `laser_af_refresh_every_n_fovs`,
    `laser_af_consistency_threshold_um`, `laser_af_check_last_fov_per_region`)
    and writes the user's choices back via the controller's setters when the
    user clicks OK.
    """

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Laser Autofocus Settings")
        self.setModal(True)

        self._build_ui()
        self._populate_from_controller()
        self._update_enabled_states()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        self.cb_enabled = QCheckBox("Enable laser autofocus during acquisition")
        layout.addWidget(self.cb_enabled)

        mode_group = QGroupBox("Mode")
        mode_layout = QVBoxLayout(mode_group)
        self.rb_fast = QRadioButton(
            "Fast — per-FOV offset table + anchor refresh every N FOVs"
        )
        self.rb_legacy = QRadioButton(
            "Legacy — full laser AF at every FOV (instant rollback)"
        )
        self.mode_bg = QButtonGroup(self)
        self.mode_bg.addButton(self.rb_fast, 0)
        self.mode_bg.addButton(self.rb_legacy, 1)
        mode_layout.addWidget(self.rb_fast)
        mode_layout.addWidget(self.rb_legacy)
        layout.addWidget(mode_group)

        self.fast_group = QGroupBox("Fast mode options")
        fast_layout = QVBoxLayout(self.fast_group)

        seed_group = QGroupBox("Seed behavior")
        seed_layout = QVBoxLayout(seed_group)
        self.rb_seed_scan = QRadioButton(
            "Pre-acquisition scan (visit every FOV once upfront)"
        )
        self.rb_seed_lazy = QRadioButton(
            "Lazy (seed during first visit in normal acquisition)"
        )
        self.seed_bg = QButtonGroup(self)
        self.seed_bg.addButton(self.rb_seed_scan, 0)
        self.seed_bg.addButton(self.rb_seed_lazy, 1)
        seed_layout.addWidget(self.rb_seed_scan)
        seed_layout.addWidget(self.rb_seed_lazy)
        fast_layout.addWidget(seed_group)

        form = QFormLayout()
        self.sp_refresh_n = QSpinBox()
        self.sp_refresh_n.setRange(2, 10000)
        self.sp_refresh_n.setSuffix(" FOVs")
        self.sp_threshold = QDoubleSpinBox()
        self.sp_threshold.setRange(0.1, 1000.0)
        self.sp_threshold.setDecimals(1)
        self.sp_threshold.setSingleStep(0.5)
        self.sp_threshold.setSuffix(" µm")  # µm
        form.addRow("Anchor refresh every:", self.sp_refresh_n)
        form.addRow("Consistency warn threshold:", self.sp_threshold)
        fast_layout.addLayout(form)

        self.cb_check_last_fov = QCheckBox(
            "Displacement check at last FOV of regions shorter than the refresh cadence"
        )
        fast_layout.addWidget(self.cb_check_last_fov)

        layout.addWidget(self.fast_group)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        self.cb_enabled.toggled.connect(self._update_enabled_states)
        self.mode_bg.buttonToggled.connect(self._update_enabled_states)

    def _populate_from_controller(self):
        c = self.controller
        self.cb_enabled.setChecked(bool(getattr(c, "do_reflection_af", False)))

        refresh_n = int(getattr(c, "laser_af_refresh_every_n_fovs", 10))
        if refresh_n <= 1:
            self.rb_legacy.setChecked(True)
            # Preserve a sensible default in the spinbox in case the user switches
            # back to fast mode.
            self.sp_refresh_n.setValue(10)
        else:
            self.rb_fast.setChecked(True)
            self.sp_refresh_n.setValue(refresh_n)

        self.sp_threshold.setValue(float(getattr(c, "laser_af_consistency_threshold_um", 5.0)))
        self.cb_check_last_fov.setChecked(
            bool(getattr(c, "laser_af_check_last_fov_per_region", True))
        )

        seed_mode = getattr(c, "laser_af_seed_mode", "scan")
        if seed_mode == "lazy":
            self.rb_seed_lazy.setChecked(True)
        else:
            self.rb_seed_scan.setChecked(True)

    def _update_enabled_states(self, *_):
        enabled = self.cb_enabled.isChecked()
        self.rb_fast.setEnabled(enabled)
        self.rb_legacy.setEnabled(enabled)
        self.fast_group.setEnabled(enabled and self.rb_fast.isChecked())

    def accept(self):
        c = self.controller
        c.set_reflection_af_flag(self.cb_enabled.isChecked())
        if self.rb_legacy.isChecked():
            # Legacy = AF every FOV. Force lazy seed so we don't waste ~90 s on
            # an upfront pass when every FOV will be measured anyway.
            c.set_laser_af_refresh_every_n_fovs(1)
            c.set_laser_af_seed_mode("lazy")
        else:
            c.set_laser_af_refresh_every_n_fovs(int(self.sp_refresh_n.value()))
            c.set_laser_af_seed_mode(
                "scan" if self.rb_seed_scan.isChecked() else "lazy"
            )
        c.set_laser_af_consistency_threshold_um(float(self.sp_threshold.value()))
        c.set_laser_af_check_last_fov_per_region(self.cb_check_last_fov.isChecked())
        super().accept()


class LaserAutofocusButton(QPushButton):
    """Drop-in replacement for the old `QCheckBox("Laser AF")`.

    Surfaces a QCheckBox-compatible API (`isChecked`, `setChecked`, `toggled`)
    so the surrounding multipoint widget code keeps working unchanged. The
    label reflects the current enable state and refresh cadence. Clicking
    opens `LaserAutofocusSettingsDialog`.

    The enable state is tracked locally (decoupled from `QWidget.isEnabled`);
    surrounding code calls `setEnabled(bool)` to grey out the control during
    fluidics overrides etc. without changing the stored enable value.
    """

    toggled = Signal(bool)

    def __init__(self, multipoint_controller, parent=None):
        super().__init__(parent)
        self._mpc = multipoint_controller
        self._checked_state = False
        # Light red, mirroring btn_startAcquisition's light-blue (#C2C2FF) styling.
        self.setStyleSheet("background-color: #FFC2C2;")
        self.clicked.connect(self._open_dialog)
        self._refresh_label()

    def isChecked(self) -> bool:
        return self._checked_state

    def setChecked(self, flag: bool):
        new = bool(flag)
        if new == self._checked_state:
            return
        self._checked_state = new
        self._refresh_label()
        self.toggled.emit(self._checked_state)

    def refresh_label(self):
        """Re-read controller state and update the button text. Call after
        bulk updates to the controller (e.g., loading settings from cache)."""
        self._refresh_label()

    def _refresh_label(self):
        if not self._checked_state:
            self.setText("Laser AF: Off ▸")  # ▸
            return
        n = int(getattr(self._mpc, "laser_af_refresh_every_n_fovs", 10))
        if n <= 1:
            self.setText("Laser AF: Every FOV ▸")
        else:
            self.setText(f"Laser AF: Fast (N={n}) ▸")

    def _open_dialog(self):
        # Push the button's local enable state to the controller before showing
        # the dialog so the dialog's "Enable" checkbox starts in sync.
        if bool(self._mpc.do_reflection_af) != self._checked_state:
            self._mpc.set_reflection_af_flag(self._checked_state)

        dlg = LaserAutofocusSettingsDialog(self._mpc, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            new_state = bool(self._mpc.do_reflection_af)
            if new_state != self._checked_state:
                self._checked_state = new_state
                self.toggled.emit(self._checked_state)
            self._refresh_label()
