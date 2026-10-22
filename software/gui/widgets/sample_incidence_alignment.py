"""Interactive two-stage sample flatness and laser-incidence calibration."""

import threading

import numpy as np
import pyqtgraph as pg
from qtpy.QtCore import QObject, Qt, Signal
from qtpy.QtGui import QTransform
from qtpy.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from control.core.sample_incidence_calibration import (
    AlignmentHardware,
    AlignmentSettings,
    CalibrationCancelled,
    SampleIncidenceSession,
)
from control.core.sample_incidence_analysis import image_to_stage_matrix, stage_field_size, stage_profiles
from control.core.sample_incidence_recording import AlignmentRecorder
import squid.logging

CURRENT = (0, 220, 255)
BEST = (255, 80, 200)


class _CalibrationSignals(QObject):
    progress = Signal(str, object)
    flatness = Signal(object)
    incidence = Signal(object)
    failed = Signal(str)
    finished = Signal()


class SampleIncidenceAlignmentDialog(QDialog):
    def __init__(self, microscope, laser_af=None, is_busy=lambda: False, parent=None):
        super().__init__(parent)
        self.scope, self.laser_af, self.is_busy = microscope, laser_af, is_busy
        self.log = squid.logging.get_logger(self.__class__.__name__)
        self.setWindowTitle("Sample flatness and laser incidence alignment")
        # Blocking the parent while this window is open prevents concurrent
        # manual motion, channel/objective changes, or another acquisition.
        self.setWindowModality(Qt.WindowModal)
        self.resize(1240, 920)
        self._thread = None
        self._cancel = threading.Event()
        self._worker_finished = threading.Event()
        self._worker_finished.set()
        self._close_when_stopped = False
        self._failed = False
        self.best = None
        self.best_score = float("inf")
        self._stage_to_image = None
        self._recorder = None
        self.signals = _CalibrationSignals(self)
        self.signals.progress.connect(self._progress)
        self.signals.flatness.connect(self._flatness)
        self.signals.incidence.connect(self._incidence)
        self.signals.failed.connect(self._error)
        self.signals.finished.connect(self._finished)

        layout = QVBoxLayout(self)
        instructions = QLabel(
            "Select a fluorescence channel and focus on the marker before opening this tool. "
            "Start acquires overlapping XY fields and texture Z scans; it uses laser AF when initialized with a reference. "
            "Incidence follows only after flatness passes, using three Z heights and matched specimen overlaps. "
            "Each dither stays within one FOV of the starting center. Stop returns to the starting position."
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        controls = QGridLayout()
        self.dither = self._number(0.05, 0.45, 0.33, 0.01)
        self.focus_range = self._number(0.1, 20, 2.0, 0.25)
        self.steps = QSpinBox()
        self.steps.setRange(5, 31)
        self.steps.setSingleStep(2)
        self.steps.setValue(9)
        self.baseline = self._number(0.2, 40, 4.0, 0.5)
        self.plane_tolerance = self._number(0.02, 10, 0.5, 0.05)
        self.residual_tolerance = self._number(0.02, 10, 0.25, 0.05)
        self.black_level = self._number(0, 65535, 0, 10)
        self.angle_target = self._number(0.05, 30, 1, 0.1)
        self.center_target = self._number(0.5, 50, 5, 0.5)
        self._controls = [
            self.dither,
            self.focus_range,
            self.steps,
            self.baseline,
            self.plane_tolerance,
            self.residual_tolerance,
            self.black_level,
            self.angle_target,
            self.center_target,
        ]
        labels = [
            "XY offset (FOV)",
            "Focus ± range (µm)",
            "Focus planes",
            "Z baseline (µm)",
            "Max plane span (µm)",
            "Max residual RMS (µm)",
            "Camera black level",
            "Angle target (°)",
            "Center target (% FOV)",
        ]
        for index, (label, widget) in enumerate(zip(labels, self._controls)):
            row, column = divmod(index, 3)
            controls.addWidget(QLabel(label), row, 2 * column)
            controls.addWidget(widget, row, 2 * column + 1)
        self.dither.setToolTip("Offset along each axis. Default 0.33 FOV gives substantial shared specimen overlap.")
        self.black_level.setToolTip("Camera offset from a dark image; do not subtract the dimmest fluorescent region.")
        layout.addLayout(controls)
        recording = QHBoxLayout()
        self.save_intermediate = QCheckBox("Save troubleshooting data")
        self.recording_folder = QLineEdit()
        self.recording_folder.setPlaceholderText("Choose an output folder…")
        self.recording_folder.setToolTip(
            "Each run creates a separate folder containing full-resolution images and analysis data. "
            "Saving can slow calibration and use substantial disk space."
        )
        self.browse_recording_folder = QPushButton("Browse…")
        self.browse_recording_folder.setAutoDefault(False)
        recording.addWidget(self.save_intermediate)
        recording.addWidget(self.recording_folder, 1)
        recording.addWidget(self.browse_recording_folder)
        layout.addLayout(recording)
        self.recording_location = QLabel()
        self.recording_location.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.recording_location.setWordWrap(True)
        self.recording_location.hide()
        layout.addWidget(self.recording_location)
        buttons = QHBoxLayout()
        self.repeat = QCheckBox("Repeat; recheck flatness every 3 incidence sweeps")
        self.repeat.setChecked(True)
        self.start_button, self.stop_button, self.reset_button = (
            QPushButton("Start calibration"),
            QPushButton("Stop and return"),
            QPushButton("Reset best"),
        )
        for button in (self.start_button, self.stop_button, self.reset_button):
            button.setAutoDefault(False)
        self.stop_button.setEnabled(False)
        buttons.addWidget(self.repeat)
        buttons.addStretch()
        for button in (self.start_button, self.stop_button, self.reset_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.status = QLabel("Ready. The current optical configuration is held fixed during calibration.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.orientation_status = QLabel(
            "Stage coordinates: +X right, +Y down (top-left origin). "
            "Camera orientation will be registered during the first flatness sweep."
        )
        self.orientation_status.setWordWrap(True)
        layout.addWidget(self.orientation_status)

        self.graphics = pg.GraphicsLayoutWidget()
        layout.addWidget(self.graphics, 1)
        self.raw_plot = self.graphics.addPlot(row=0, col=0, title="Latest camera field · orientation pending")
        self.focus_plot = self.graphics.addPlot(row=0, col=1, title="Sample best-focus Z · optics corrected")
        self.slope_plot = self.graphics.addPlot(row=0, col=2, title="Best-focus Z slope in each FOV (µm/mm)")
        self.envelope_plot = self.graphics.addPlot(row=1, col=0, title="Illumination · cyan current / magenta best")
        self.profile_plot = self.graphics.addPlot(
            row=1, col=1, colspan=2, title="Illumination profiles through FOV center"
        )
        for plot in (self.raw_plot, self.envelope_plot, self.focus_plot):
            plot.setAspectLocked(True)
            plot.invertY(True)
            plot.invertX(False)
        self.focus_plot.setLabel("bottom", "Stage X from scan center →", units="µm")
        self.focus_plot.setLabel("left", "Stage Y from scan center ↓", units="µm")
        for plot in (self.raw_plot, self.envelope_plot):
            plot.setLabel("bottom", "Stage X from field center →", units="µm")
            plot.setLabel("left", "Stage Y from field center ↓", units="µm")
        for plot in (self.raw_plot, self.envelope_plot, self.focus_plot, self.slope_plot):
            for axis in ("bottom", "left"):
                plot.getAxis(axis).enableAutoSIPrefix(False)
        self.raw_plot.setLabel("bottom", "Delivered camera X", units="px")
        self.raw_plot.setLabel("left", "Delivered camera Y", units="px")
        self.slope_plot.invertY(True)
        self.slope_plot.setLabel("bottom", "dz/dx", units="µm/mm")
        self.slope_plot.setLabel("left", "dz/dy", units="µm/mm")
        self.slope_plot.addLine(x=0, pen=pg.mkPen((100, 100, 100)))
        self.slope_plot.addLine(y=0, pen=pg.mkPen((100, 100, 100)))
        self.raw_image = pg.ImageItem(axisOrder="row-major")
        self.raw_plot.addItem(self.raw_image)
        self.focus_labels = []
        for _ in range(9):
            label = pg.TextItem(anchor=(0.5, 0.5), color="w", fill=pg.mkBrush(0, 0, 0, 160))
            self.raw_plot.addItem(label)
            label.hide()
            self.focus_labels.append(label)
        self.focus_points = pg.ScatterPlotItem(size=11, pen=None)
        self.focus_plot.addItem(self.focus_points)
        self.slope_points = pg.ScatterPlotItem(size=10, pen=pg.mkPen(CURRENT), brush=pg.mkBrush(*CURRENT, 120))
        self.slope_plot.addItem(self.slope_points)
        self.current_image = pg.ImageItem(axisOrder="row-major")
        self.best_image = pg.ImageItem(axisOrder="row-major")
        for item, color in ((self.current_image, CURRENT), (self.best_image, BEST)):
            lut = np.empty((256, 4), np.uint8)
            lut[:, :3], lut[:, 3] = color, np.arange(256)
            item.setLookupTable(lut)
            self.envelope_plot.addItem(item)
        self.best_image.setOpacity(0.45)
        self.beam_center = pg.ScatterPlotItem(symbol="+", size=18, pen=pg.mkPen(CURRENT, width=2))
        self.envelope_plot.addItem(self.beam_center)
        self.profile_plot.addLegend()
        self.profile_plot.setLabel("bottom", "Stage position relative to field center", units="% FOV")
        self.profile_plot.setLabel("left", "Intensity / field mean")
        self.profile_plot.addLine(x=0, pen=pg.mkPen((140, 140, 140), style=Qt.DotLine))
        self.current_x = self.profile_plot.plot(pen=pg.mkPen(CURRENT, width=2), name="Current X")
        self.current_y = self.profile_plot.plot(pen=pg.mkPen(CURRENT, width=2, style=Qt.DashLine), name="Current Y")
        self.best_x = self.profile_plot.plot(pen=pg.mkPen((*BEST, 130), width=3), name="Best X")
        self.best_y = self.profile_plot.plot(pen=pg.mkPen((*BEST, 130), width=3, style=Qt.DashLine), name="Best Y")
        self.flatness_status = QLabel("Flatness has not been measured.")
        self.incidence_status = QLabel("Incidence is gated on reliable, acceptable flatness.")
        for label in (self.flatness_status, self.incidence_status):
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(label)
        note = QLabel(
            "Repeatable camera-field focus effects are excluded from sample flatness. "
            "Image labels show measured best-focus ΔZ from the field center, including optics; negative means lower Z. "
            "Slopes use the same Z coordinate as the stage controls: positive means higher best-focus Z as X or Y increases. "
            "Consistent defocus supports geometric tilt relative to the objective; it cannot uniquely identify a holder fault. "
            "Variable coating is reported separately. A flat/unbounded beam or inconsistent overlaps yield no angle. "
            "Best minimizes angle/angle-target and center-offset/center-target jointly."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.start_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(self.request_stop)
        self.reset_button.clicked.connect(self.reset_best)
        self.save_intermediate.toggled.connect(self._update_recording_controls)
        self.browse_recording_folder.clicked.connect(self._choose_recording_folder)
        self._update_recording_controls()

    def _update_recording_controls(self):
        enabled = self.save_intermediate.isChecked() and self.start_button.isEnabled()
        self.recording_folder.setEnabled(enabled)
        self.browse_recording_folder.setEnabled(enabled)

    def _choose_recording_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose troubleshooting output folder", self.recording_folder.text()
        )
        if folder:
            self.recording_folder.setText(folder)

    @staticmethod
    def _number(minimum, maximum, value, step):
        widget = QDoubleSpinBox()
        widget.setRange(minimum, maximum)
        widget.setValue(value)
        widget.setSingleStep(step)
        widget.setKeyboardTracking(False)
        return widget

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive() and not self._worker_finished.is_set()

    def start(self):
        if self.running:
            return
        if self.is_busy():
            self.status.setText("Another acquisition or autofocus is active; finish it before starting calibration.")
            return
        settings = AlignmentSettings(
            dither_fraction=self.dither.value(),
            focus_half_range_um=self.focus_range.value(),
            focus_steps=self.steps.value(),
            z_baseline_um=self.baseline.value(),
            max_plane_span_um=self.plane_tolerance.value(),
            max_residual_um=self.residual_tolerance.value(),
            black_level=self.black_level.value(),
            repeat=self.repeat.isChecked(),
        )
        try:
            settings.validate()
            pixel_um = self.scope.camera.get_pixel_size_binned_um() * self.scope.objective_store.get_pixel_size_factor()
            self._recorder = (
                AlignmentRecorder(self.recording_folder.text()) if self.save_intermediate.isChecked() else None
            )
        except Exception as error:
            self._error(str(error))
            return
        self.reset_best()
        self.recording_location.setText("Preparing troubleshooting folder…" if self._recorder else "")
        self.recording_location.setVisible(self._recorder is not None)
        self._stage_to_image = None
        for label in self.focus_labels:
            label.hide()
        for item in (self.raw_image, self.current_image, self.best_image):
            item.clear()
        for item in (self.focus_points, self.slope_points, self.beam_center):
            item.clear()
        self.current_x.clear()
        self.current_y.clear()
        self.flatness_status.setText("Flatness has not been measured for this session.")
        self.incidence_status.setText("Incidence is gated on reliable, acceptable flatness.")
        for item in (self.raw_image, self.current_image, self.best_image):
            item.resetTransform()
        self.raw_plot.setTitle("Latest camera field · orientation pending")
        self.raw_plot.setLabel("bottom", "Delivered camera X", units="px")
        self.raw_plot.setLabel("left", "Delivered camera Y", units="px")
        self.orientation_status.setText(
            "Stage: +X right, +Y down; Z follows the stage controls. "
            "Camera orientation pending the first complete XY registration."
        )
        self._failed = False
        self._cancel.clear()
        self._worker_finished.clear()
        self._set_running(True)
        hardware = AlignmentHardware(self.scope, self.laser_af, self._cancel)
        self.status.setText(
            "Starting with laser AF."
            if hardware.af_ready
            else "Laser AF unavailable/unreferenced; using texture focus scans."
        )
        session = SampleIncidenceSession(
            hardware,
            settings,
            pixel_um,
            self.signals.progress.emit,
            self.signals.flatness.emit,
            self.signals.incidence.emit,
            **({"recorder": self._recorder} if self._recorder is not None else {}),
        )

        def run():
            try:
                session.run()
            except CalibrationCancelled:
                pass
            except Exception as error:
                self.log.exception("Sample/incidence calibration failed")
                self.signals.failed.emit(str(error))
            finally:
                self._worker_finished.set()
                self.signals.finished.emit()

        self._thread = threading.Thread(target=run, name="sample-incidence-calibration", daemon=False)
        self._thread.start()

    def _set_running(self, running):
        for widget in self._controls + [self.repeat, self.start_button, self.save_intermediate]:
            widget.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self._update_recording_controls()

    def request_stop(self):
        self._cancel.set()
        self.status.setText("Stopping after the current operation; returning stage and restoring camera state…")
        self.stop_button.setEnabled(False)

    def stop_and_wait(self, timeout=10):
        self._cancel.set()
        if self._thread is not None:
            self._thread.join(timeout)
        if self.running:
            raise RuntimeError("Calibration is still restoring hardware; camera shutdown must wait.")

    def _progress(self, message, image):
        self.status.setText(message)
        self._show_recording_location()
        if image is not None:
            self.raw_image.setImage(image, autoLevels=True)
            self._orient_image(self.raw_image)

    def _show_recording_location(self):
        if self._recorder is not None and self._recorder.path is not None:
            self.recording_location.setText(f"Troubleshooting data: {self._recorder.path}")
            self.recording_location.show()

    def _orient_image(self, image):
        if self._stage_to_image is None or image.image is None:
            return
        matrix = image_to_stage_matrix(self._stage_to_image)
        h, w = image.image.shape[:2]
        offset = -matrix @ np.array([w / 2, h / 2])
        image.setTransform(QTransform(matrix[0, 0], matrix[1, 0], matrix[0, 1], matrix[1, 1], offset[0], offset[1]))

    def _flatness(self, result):
        self._stage_to_image = result.stage_to_image
        if self._stage_to_image is not None:
            for item in (self.raw_image, self.current_image, self.best_image):
                self._orient_image(item)
            self.raw_plot.setTitle("Latest field · last sweep's median focus ΔZ (µm)")
            self.raw_plot.setLabel("bottom", "Stage X from field center →", units="µm")
            self.raw_plot.setLabel("left", "Stage Y from field center ↓", units="µm")
            self.orientation_status.setText(
                "Stage orientation: +X right, +Y down; maps are relative to their centers. "
                "Image labels: measured best-focus ΔZ from center (negative = lower Z), before optics correction."
            )
            h, w = result.field_shape
            for tile, label in enumerate(self.focus_labels):
                row, col = divmod(tile, 3)
                value = result.camera_focus_deltas_um[row, col]
                pixel = np.array([(col + 0.5) * w / 3 - w / 2, (row + 0.5) * h / 3 - h / 2])
                label.setPos(*(image_to_stage_matrix(self._stage_to_image) @ pixel))
                label.setText(f"{value:+.2f}" if np.isfinite(value) else "unresolved")
                label.show()
        colors = pg.colormap.get("viridis").map(
            (result.heights_um - np.min(result.heights_um)) / max(np.ptp(result.heights_um), 1e-6), mode="qcolor"
        )
        self.focus_points.setData(x=result.coordinates_um[:, 0], y=result.coordinates_um[:, 1], brush=colors)
        usable_slopes = result.local_slopes[np.isfinite(result.local_slopes).all(axis=1)]
        self.slope_points.setData(x=usable_slopes[:, 0] * 1000, y=usable_slopes[:, 1] * 1000)
        tilt = np.degrees(np.arctan(np.linalg.norm(result.plane[:2])))
        overlap_text = (
            f"{result.overlap_rms_um:.3f} µm" if np.isfinite(result.overlap_rms_um) else "unavailable (edges excluded)"
        )
        direction_text = (
            f"{result.direction_agreement:.0%}" if np.isfinite(result.direction_agreement) else "not resolved"
        )
        optical_span = (
            0
            if result.camera_focus_offsets_um is None
            else np.nanmax(result.camera_focus_offsets_um) - np.nanmin(result.camera_focus_offsets_um)
        )
        self.flatness_status.setText(
            f"{result.diagnosis}\nPlane tilt: {tilt:.3f}° · height span: {result.plane_span_um:.3f} µm · "
            f"best-focus dZ/dX: {1000 * result.plane[0]:+.3f}, dZ/dY: {1000 * result.plane[1]:+.3f} µm/mm · "
            f"residual RMS: {result.residual_rms_um:.3f} µm · overlap RMS: {overlap_text} · "
            f"direction agreement: {direction_text} · camera-fixed focus variation ignored: {optical_span:.3f} µm"
            + (
                ""
                if result.sample_variation is None
                else f" · specimen brightness variation: {result.sample_variation:.1%}"
            )
            + "\n"
            + result.coating_message
        )
        if not result.accepted:
            self.current_image.clear()
            self.current_x.clear()
            self.current_y.clear()
            self.beam_center.clear()
            self.incidence_status.setText("Incidence gated: sample flatness/consistency did not pass.")
        else:
            self.incidence_status.setText("Flatness passed; incidence is eligible once the Z-baseline sweep completes.")

    def _draw(self, illumination, image, curve_x, curve_y):
        envelope = illumination.envelope
        image.setImage(envelope, autoLevels=False, levels=(0, 2))
        self._orient_image(image)
        transform = -np.eye(2) if self._stage_to_image is None else self._stage_to_image
        x, y = stage_profiles(envelope, transform)
        curve_x.setData(*x)
        curve_y.setData(*y)

    def _incidence(self, result):
        illumination = result.illumination
        self._draw(illumination, self.current_image, self.current_x, self.current_y)
        center_error = None
        if illumination.center_px is not None:
            dimensions = np.array(illumination.envelope.shape[::-1])
            center = illumination.center_px + 0.5 - dimensions / 2
            if self._stage_to_image is not None:
                center = image_to_stage_matrix(self._stage_to_image) @ center
                dimensions = stage_field_size(illumination.envelope.shape, self._stage_to_image)
                self.beam_center.setData(x=[center[0]], y=[center[1]])
            else:
                self.beam_center.setData(x=[illumination.center_px[0] + 0.5], y=[illumination.center_px[1] + 0.5])
            center_offset = 100 * center / dimensions
            center_error = np.linalg.norm(center_offset)
            center_text = f"Center X/Y: {center_offset[0]:+.2f}%, {center_offset[1]:+.2f}% FOV"
        else:
            self.beam_center.clear()
            center_text = "Beam center unbounded"
        reliable_angle = (
            result.angle_uncertainty_deg is not None and result.angle_uncertainty_deg <= self.angle_target.value()
        )
        if result.angle_deg is not None and center_error is not None and reliable_angle:
            score = np.hypot(result.angle_deg / self.angle_target.value(), center_error / self.center_target.value())
            if score < self.best_score:
                self.best, self.best_score = result, score
                self._draw(illumination, self.best_image, self.best_x, self.best_y)
        angle_text = "unavailable" if result.angle_deg is None else f"{result.angle_deg:.3f}°"
        if result.angle_uncertainty_deg is not None:
            angle_text += f" (field scatter ≈{result.angle_uncertainty_deg:.3f}°)"
        best_text = (
            "waiting for a reliable angle"
            if self.best is None
            else f"{self.best.angle_deg:.3f}°; score {self.best_score:.3f}"
        )
        self.incidence_status.setText(
            f"Apparent incidence: {angle_text} · {center_text} · Best: {best_text}\n"
            f"X/Y imbalance: {illumination.imbalance_xy[0]:+.2f}%, {illumination.imbalance_xy[1]:+.2f}% · "
            f"specimen brightness variation: {illumination.sample_variation:.1%} · "
            f"overlap fit residual: {illumination.overlap_residual:.1%} · "
            f"center reference drift: {result.reference_drift:.1%}. {result.message}"
        )

    def reset_best(self):
        self.best, self.best_score = None, float("inf")
        self.best_image.clear()
        self.best_x.clear()
        self.best_y.clear()

    def _error(self, message):
        self._failed = True
        self._show_recording_location()
        self.status.setText("Calibration stopped: " + message)

    def _finished(self):
        self._show_recording_location()
        self._set_running(False)
        if not self._failed:
            self.status.setText("Calibration stopped/completed. Starting stage, piezo and camera state restored.")
        if self._close_when_stopped:
            self._close_when_stopped = False
            self.close()

    def reject(self):
        if self.running:
            self._close_when_stopped = True
            self.request_stop()
        else:
            super().reject()

    def closeEvent(self, event):
        if self.running:
            self._close_when_stopped = True
            self.request_stop()
            event.ignore()
        else:
            super().closeEvent(event)
