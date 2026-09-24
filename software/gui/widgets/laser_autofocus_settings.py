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
consistency warning threshold, the end-of-region displacement check, and the
diagnostic table-path audit.
"""

import os
import json
from collections import deque

os.environ.setdefault("QT_API", "pyqt5")

from qtpy.QtCore import Signal
from qtpy.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
)

from control._def import MULTIPOINT_AUTOFOCUS_CHANNEL, FocusMeasureOperator, Acquisition
from control.models.contrast_autofocus import (
    AcquisitionContrastAFOverride, ContrastAFSettings, ContrastSupervisionPolicy,
    default_20x_contrast_af_settings,
)
from gui.widgets.contrast_af_editor import add_phase1_fields, validated_settings


class LaserAutofocusSettingsDialog(QDialog):
    """Modal editor for laser-AF behavior.

    Reads initial values from the supplied MultiPointController (attributes
    `do_reflection_af`, `laser_af_seed_mode`, `laser_af_refresh_every_n_fovs`,
    `laser_af_consistency_threshold_um`, `laser_af_check_last_fov_per_region`,
    `laser_af_table_path_audit`)
    and writes the user's choices back via the controller's setters when the
    user clicks OK.
    """

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Autofocus Settings")
        self.setModal(True)

        self._build_ui()
        self._populate_from_controller()
        self._update_enabled_states()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        contrast_group = QGroupBox("Contrast AF — acquisition settings")
        contrast_layout = QVBoxLayout(contrast_group)
        self.cb_contrast_enabled = QCheckBox("Enable contrast AF during acquisition")
        contrast_layout.addWidget(self.cb_contrast_enabled)
        form = QFormLayout()
        self.af_state = QComboBox()
        self.af_state.setEditable(False)
        self.af_method = QComboBox()
        self.af_method.addItem("Legacy contrast scan", "legacy")
        self.af_method.addItem("Frequency-assisted contrast search", "frequency_assisted")
        self.af_metric = QComboBox()
        for metric in FocusMeasureOperator:
            self.af_metric.addItem(metric.value, metric.value)
        form.addRow("AF observation state:", self.af_state)
        form.addRow("Algorithm:", self.af_method)
        form.addRow("Sharpness:", self.af_metric)
        self.af_inputs = add_phase1_fields(form)
        box = QDoubleSpinBox()
        box.setRange(0, 10000)
        box.setDecimals(3)
        form.addRow("Legacy step (µm)", box)
        self.af_inputs["legacy_step_um"] = box
        for name, label, minimum in (
            ("legacy_count", "Legacy plane count", 1),
            ("crop_width", "Crop width", 1),
            ("crop_height", "Crop height", 1),
        ):
            box = QSpinBox()
            box.setRange(minimum, 10000)
            form.addRow(label, box)
            self.af_inputs[name] = box
        self.af_fallback = QCheckBox("Allow one dense fallback")
        form.addRow(self.af_fallback)
        self.af_failure = QComboBox()
        self.af_failure.addItem("Stop acquisition", "stop")
        self.af_failure.addItem("Continue at restored nominal Z after optical failure", "continue_restored")
        form.addRow("On search failure:", self.af_failure)
        contrast_layout.addLayout(form)
        self.af_cadence = QLabel(
            f"Measured scan every {Acquisition.NUMBER_OF_FOVS_PER_AF} FOV visits; "
            "a valid focus map takes precedence. Laser AF takes precedence if enabled.")
        self.af_cadence.setWordWrap(True)
        contrast_layout.addWidget(self.af_cadence)
        contrast_scroll = QScrollArea()
        contrast_scroll.setWidgetResizable(True)
        contrast_scroll.setWidget(contrast_group)
        contrast_scroll.setMinimumHeight(320)
        layout.addWidget(contrast_scroll)
        self.af_state.currentIndexChanged.connect(self._load_contrast_state)

        self.cb_enabled = QCheckBox("Enable laser autofocus during acquisition")
        layout.addWidget(self.cb_enabled)

        self.laser_mode_group = QGroupBox("Laser AF mode")
        mode_layout = QVBoxLayout(self.laser_mode_group)
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
        layout.addWidget(self.laser_mode_group)

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

        self.cb_table_path_audit = QCheckBox(
            "Table-path audit (diagnostic): full AF + before/after CSV at every table FOV"
        )
        self.cb_table_path_audit.setToolTip(
            "Validation runs only — corrects Z at audited FOVs and adds ~300 ms per FOV.\n"
            "Writes table_path_audit.csv next to the acquisition data."
        )
        fast_layout.addWidget(self.cb_table_path_audit)

        layout.addWidget(self.fast_group)

        supervision = QGroupBox("Contrast supervision of laser AF")
        sf = QFormLayout(supervision)
        self.supervision_mode = QComboBox()
        for label, value in (("Off", "off"), ("Monitor only", "monitor"),
                             ("Correct when quality is persistently poor", "quality")):
            self.supervision_mode.addItem(label, value)
        sf.addRow("Mode:", self.supervision_mode)
        self.supervision_entry = QCheckBox("Check on every region/timepoint entry")
        sf.addRow(self.supervision_entry)
        self.supervision_verify_baseline = QCheckBox("Verify a trusted baseline once with a bounded contrast scan")
        sf.addRow(self.supervision_verify_baseline)
        self.supervision_confirm_reference = QCheckBox(
            "I independently confirmed the region laser references are in focus; seed from first good check")
        sf.addRow(self.supervision_confirm_reference)
        self.supervision_inputs = {}
        for key, label, low, high in (
            ("every_n_completed_fovs", "Check every N completed FOVs (0=off)", 0, 10000),
            ("min_baseline_fields", "Trusted independent fields", 1, 1000),
            ("bad_checks_required", "Consecutive bad checks", 1, 100),
            ("max_attempts_per_region", "Maximum attempts per region", 0, 100),
            ("cooldown_completed_fovs", "Cooldown in completed FOVs", 0, 10000),
            ("max_frames_per_correction", "Frames per correction", 1, 10000),
            ("max_frames_per_region", "Frames per region", 1, 100000),
            ("max_frames_per_run", "Frames per run", 1, 1000000),
        ):
            box = QSpinBox()
            box.setRange(low, high)
            sf.addRow(label, box)
            self.supervision_inputs[key] = box
        for key, label, low, high, suffix in (
            ("relative_drop", "Relative sharpness drop (provisional)", 0.01, 0.99, ""),
            ("max_baseline_age_s", "Maximum baseline age", 1, 86400, " s"),
            ("max_laser_age_s", "Maximum laser evidence age", 0.1, 3600, " s"),
            ("min_correlation", "Minimum laser correlation", -1, 1, ""),
            ("max_residual_um", "Maximum laser residual", 0.01, 100, " µm"),
            ("min_brightness_fraction", "Minimum brightness / full scale (provisional)", 0, 1, ""),
            ("min_absolute_signal_adu", "Minimum absolute signal (provisional)", 0, 65535, " ADU"),
            ("min_absolute_noise_adu", "Minimum absolute noise floor (provisional)", 0, 65535, " ADU"),
            ("max_saturation_fraction", "Maximum saturated fraction (provisional)", 0, 1, ""),
            ("min_tile_coverage", "Minimum textured tile coverage (provisional)", 0, 1, ""),
            ("min_tile_contrast_fraction", "Minimum tile contrast / full scale (provisional)", 0, 1, ""),
            ("min_brightness_ratio", "Minimum brightness / baseline (provisional)", 0.01, 1, ""),
            ("max_brightness_ratio", "Maximum brightness / baseline (provisional)", 1, 10, ""),
            ("max_noise_ratio", "Maximum noise / baseline (provisional)", 1, 10, ""),
            ("min_peak_margin_fraction", "Minimum peak neighbor margin (provisional)", 0, 1, ""),
            ("max_good_learning_drop_fraction", "Maximum downward baseline learning (provisional)", 0, 1, ""),
            ("max_correction_um", "Maximum correction", 0.1, 1000, " µm"),
            ("max_scan_travel_um", "Maximum scan travel including backlash", 0.1, 2000, " µm"),
            ("confirmation_spacing_um", "Local confirmation spacing", 0.1, 100, " µm"),
            ("max_exposure_ms_per_correction", "Exposure per correction", 1, 100000, " ms"),
            ("max_exposure_ms_per_region", "Exposure per region", 1, 1000000, " ms"),
            ("max_exposure_ms_per_run", "Exposure per run", 1, 10000000, " ms"),
            ("max_laser_exposure_ms_per_correction", "Laser exposure per correction", 1, 100000, " ms"),
            ("max_laser_exposure_ms_per_region", "Laser exposure per region", 1, 1000000, " ms"),
            ("max_laser_exposure_ms_per_run", "Laser exposure per run", 1, 10000000, " ms"),
            ("max_time_s_per_correction", "Time per correction", 1, 3600, " s"),
            ("max_time_s_per_region", "Added time per region", 1, 86400, " s"),
            ("max_time_s_per_run", "Added time per run", 1, 604800, " s"),
            ("nominal_plane_offset_um", "Monitor-to-nominal plane offset", -1000, 1000, " µm"),
        ):
            box = QDoubleSpinBox()
            box.setRange(low, high)
            box.setDecimals(3)
            box.setSuffix(suffix)
            sf.addRow(label, box)
            self.supervision_inputs[key] = box
        self.supervision_failure = QComboBox()
        self.supervision_failure.addItem("Stop acquisition", "stop")
        self.supervision_failure.addItem("Continue after verified optical rollback", "continue_restored")
        sf.addRow("Correction failure:", self.supervision_failure)
        self.supervision_status = QLabel("Supervision is off; laser anchor refresh has its own cadence.")
        self.supervision_status.setWordWrap(True)
        sf.addRow(self.supervision_status)
        details_button = QPushButton("Show latest supervision details")
        details_button.clicked.connect(self._show_supervision_details)
        sf.addRow(details_button)
        supervision_scroll = QScrollArea()
        supervision_scroll.setWidgetResizable(True)
        supervision_scroll.setWidget(supervision)
        supervision_scroll.setMinimumHeight(220)
        layout.addWidget(supervision_scroll)
        self.supervision_mode.currentIndexChanged.connect(self._update_supervision_summary)
        self.supervision_entry.toggled.connect(self._update_supervision_summary)
        self.supervision_inputs["every_n_completed_fovs"].valueChanged.connect(self._update_supervision_summary)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        self.cb_enabled.toggled.connect(self._update_enabled_states)
        self.mode_bg.buttonToggled.connect(self._update_enabled_states)

    def _populate_from_controller(self):
        c = self.controller
        self.cb_contrast_enabled.setChecked(bool(getattr(c, "do_autofocus", False)))
        names = [state.name for state in c.liveController.get_observation_states()]
        self.af_state.addItems(names)
        selected = getattr(c, "contrast_af_state_name", MULTIPOINT_AUTOFOCUS_CHANNEL)
        if selected not in names:
            self.af_state.addItem(f"{selected} (missing)", selected)
        self.af_state.setCurrentIndex(self.af_state.findData(selected) if self.af_state.findData(selected) >= 0
                                      else self.af_state.findText(selected))
        self._load_contrast_state()
        self.cb_enabled.setChecked(bool(getattr(c, "do_reflection_af", False)))
        has_laser = getattr(c, "laserAutoFocusController", None) is not None
        self._has_laser = has_laser
        self.cb_enabled.setVisible(has_laser)
        self.laser_mode_group.setVisible(has_laser)
        self.fast_group.setVisible(has_laser)

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
        self.cb_table_path_audit.setChecked(
            bool(getattr(c, "laser_af_table_path_audit", False))
        )
        policy = getattr(c, "contrast_supervision_policy", ContrastSupervisionPolicy())
        self.supervision_mode.setCurrentIndex(self.supervision_mode.findData(policy.mode))
        self.supervision_entry.setChecked(policy.check_on_entry)
        self.supervision_verify_baseline.setChecked(policy.verify_baseline_once)
        self.supervision_confirm_reference.setChecked(policy.operator_confirms_reference_focus)
        for key, box in self.supervision_inputs.items():
            box.setValue(getattr(policy, key))
        self.supervision_failure.setCurrentIndex(self.supervision_failure.findData(policy.failure_action))
        self._update_supervision_summary()
        live_status = getattr(c, "last_supervision_status", None)
        self._latest_supervision_status = live_status
        if isinstance(live_status, dict):
            self.supervision_status.setText(
                self.supervision_status.text() +
                f" Last: {live_status.get('event')} at t={live_status.get('timepoint')}, "
                f"region={live_status.get('region')}, FOV={live_status.get('fov')}; "
                f"reference revision {live_status.get('revision')}, "
                f"quality {live_status.get('quality', live_status.get('reason', '—'))}, "
                f"score {live_status.get('score', '—')}, support {live_status.get('support', '—')}; "
                f"attempts {live_status.get('correction_attempts_region', '—')}, "
                f"resets {live_status.get('reference_resets_region', '—')}, "
                f"added {live_status.get('added_time_s', '—')} s, "
                f"main/laser exposure {live_status.get('main_exposure_ms_reserved', '—')}/"
                f"{live_status.get('laser_exposure_ms_reserved', '—')} ms reserved.")
        path = os.path.join(getattr(c, "base_path", "") or "", getattr(c, "experiment_ID", "") or "",
                            "contrast_supervision.jsonl")
        if live_status is None and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as stream:
                    last_line = next(iter(deque(stream, maxlen=1)), None)
                if last_line:
                    last = json.loads(last_line)
                    self._latest_supervision_status = last
                    self.supervision_status.setText(
                        self.supervision_status.text() +
                        f" Last: {last.get('event')} at t={last.get('timepoint')}, "
                        f"region={last.get('region')}, FOV={last.get('fov')}; "
                        f"reference revision {last.get('revision')}, "
                        f"quality {last.get('quality', last.get('reason', '—'))}, "
                        f"score {last.get('score', '—')}, support {last.get('support', '—')}; "
                        f"attempts {last.get('correction_attempts_region', '—')}, "
                        f"resets {last.get('reference_resets_region', '—')}, "
                        f"added {last.get('added_time_s', '—')} s, "
                        f"main/laser exposure {last.get('main_exposure_ms_reserved', '—')}/"
                        f"{last.get('laser_exposure_ms_reserved', '—')} ms reserved.")
            except (OSError, ValueError):
                pass
        seed_mode = getattr(c, "laser_af_seed_mode", "scan")
        if seed_mode == "lazy":
            self.rb_seed_lazy.setChecked(True)
        else:
            self.rb_seed_scan.setChecked(True)

    def _show_supervision_details(self):
        details = QDialog(self)
        details.setWindowTitle("Latest contrast supervision details")
        layout = QVBoxLayout(details)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setPlainText(json.dumps(self._latest_supervision_status or {}, indent=2, default=str))
        layout.addWidget(view)
        details.resize(700, 500)
        details.exec_()

    def _update_supervision_summary(self, *_):
        mode = self.supervision_mode.currentText()
        self.supervision_verify_baseline.setEnabled(self.supervision_mode.currentData() == "quality")
        parts = []
        if self.supervision_entry.isChecked():
            parts.append("each region/timepoint entry")
        n = self.supervision_inputs["every_n_completed_fovs"].value()
        if n:
            parts.append(f"every {n} completed FOVs within that visit")
        self.supervision_status.setText(
            f"{mode}: " + (" and ".join(parts) or "no check scheduled") +
            ". Laser anchor refresh uses its separate cadence. Thresholds are provisional.")

    def _update_enabled_states(self, *_):
        enabled = self.cb_enabled.isChecked()
        self.rb_fast.setEnabled(enabled)
        self.rb_legacy.setEnabled(enabled)
        self.fast_group.setEnabled(enabled and self.rb_fast.isChecked())

    def _load_contrast_state(self, *_):
        name = self.af_state.currentData() or self.af_state.currentText()
        state = self.controller.liveController.get_observation_state_by_name(name)
        if state is None:
            return
        override = getattr(self.controller, "contrast_af_override", None)
        if override is not None and override.state_name != name:
            override = None
        defaults = default_20x_contrast_af_settings()
        settings = override.settings if override else (state.contrast_af or ContrastAFSettings())
        self.af_method.setCurrentIndex(self.af_method.findData(settings.method))
        metric = override.metric if override else state.focus_measure_operator
        self.af_metric.setCurrentIndex(self.af_metric.findData(metric))
        values = {
            "legacy_step_um": override.legacy_step_um if override else self.controller.autofocusController.deltaZ * 1000,
            "legacy_count": override.legacy_count if override else self.controller.autofocusController.N,
            "crop_width": override.crop_width if override else self.controller.autofocusController.crop_width,
            "crop_height": override.crop_height if override else self.controller.autofocusController.crop_height,
        }
        for name, box in self.af_inputs.items():
            value = values.get(name, getattr(settings, name, None))
            if value is None:
                value = getattr(defaults, name)
            box.setValue(value)
        self.af_fallback.setChecked(settings.dense_fallback)
        self.af_failure.setCurrentIndex(self.af_failure.findData(
            override.failure_policy if override else "stop"))

    def accept(self):
        c = self.controller
        name = self.af_state.currentData() or self.af_state.currentText()
        state = c.liveController.get_observation_state_by_name(name)
        contrast_enabled = self.cb_contrast_enabled.isChecked()
        supervision_mode = self.supervision_mode.currentData()
        if supervision_mode != "off" and not (self.cb_enabled.isChecked() and self._has_laser):
            QMessageBox.warning(self, "Autofocus Settings", "Contrast supervision requires laser AF enabled.")
            return
        if (contrast_enabled or supervision_mode != "off") and (state is None or state.is_stimulus_only):
            QMessageBox.warning(self, "Autofocus Settings", "Select an existing imaging observation state.")
            return
        values = {key: box.value() for key, box in self.af_inputs.items()}
        legacy = {key: values.pop(key) for key in
                  ("legacy_step_um", "legacy_count", "crop_width", "crop_height")}
        override = None
        if state is not None and not state.is_stimulus_only:
            try:
                override = AcquisitionContrastAFOverride(
                    state_name=name, settings=validated_settings(
                        {key: self.af_inputs[key] for key in values},
                        self.af_method.currentData(), self.af_fallback.isChecked(),
                        preserve_legacy=True),
                    metric=self.af_metric.currentData(),
                    failure_policy=self.af_failure.currentData(), **legacy)
            except (ValueError, TypeError) as exc:
                if contrast_enabled or supervision_mode != "off":
                    QMessageBox.warning(self, "Autofocus Settings", str(exc))
                    return
        try:
            policy = ContrastSupervisionPolicy(
                mode=supervision_mode,
                check_on_entry=self.supervision_entry.isChecked(),
                verify_baseline_once=self.supervision_verify_baseline.isChecked(),
                operator_confirms_reference_focus=self.supervision_confirm_reference.isChecked(),
                failure_action=self.supervision_failure.currentData(),
                **{key: box.value() for key, box in self.supervision_inputs.items()})
        except ValueError as exc:
            QMessageBox.warning(self, "Autofocus Settings", str(exc))
            return
        if override is not None:
            c.set_contrast_af_acquisition_settings(name, override)
        c.set_af_flag(contrast_enabled)
        if hasattr(c, "set_contrast_supervision_policy"):
            c.set_contrast_supervision_policy(policy)
        c.set_reflection_af_flag(self.cb_enabled.isChecked() and self._has_laser)
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
        c.set_laser_af_table_path_audit(self.cb_table_path_audit.isChecked())
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
    contrastToggled = Signal(bool)

    def __init__(self, multipoint_controller, parent=None):
        super().__init__(parent)
        self._mpc = multipoint_controller
        self._checked_state = False
        # Light red, mirroring btn_startAcquisition's light-blue (#C2C2FF) styling.
        # Scoped to LaserAutofocusButton so it doesn't propagate into the dialog
        # opened as a child — the dialog should use the normal system palette.
        self.setStyleSheet("LaserAutofocusButton { background-color: #FFC2C2; }")
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
        # The caller puts a "Laser AF" label beside the button, so the text is just
        # the current state — repeating the name here read as "Laser AF Laser AF: Off".
        self.setText("Autofocus Settings ▸")

    def _open_dialog(self):
        dlg = LaserAutofocusSettingsDialog(self._mpc, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            new_state = bool(self._mpc.do_reflection_af)
            if new_state != self._checked_state:
                self._checked_state = new_state
                self.toggled.emit(self._checked_state)
            self.contrastToggled.emit(bool(self._mpc.do_autofocus))
            self._refresh_label()
