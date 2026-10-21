"""Live stationary-disk alignment popup using the main camera's native frames."""

import threading
import time

import numpy as np
import pyqtgraph as pg
from qtpy.QtCore import QRectF, QTimer, Qt
from qtpy.QtWidgets import (
    QDialog,
    QDoubleSpinBox,
    QGridLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from control.core.spinning_disk_alignment import camera_patch, measure_pinholes


CURRENT_COLOR = (0, 220, 255)
BEST_COLOR = (255, 80, 200)


class SpinningDiskAlignmentDialog(QDialog):
    """Passive camera subscriber: does not change disk, ROI, or live settings.

    The camera callback copies only the latest small patch. A GUI timer consumes
    it at up to 10 Hz, avoiding an unbounded frame queue or camera-thread work.
    Closing/Escape removes the callback; reopening begins a new best session.
    """

    def __init__(self, camera, parent=None):
        super().__init__(parent)
        self.camera = camera
        self.setWindowTitle("Spinning Disk alignment")
        self.setModal(False)
        self.resize(1080, 780)
        self._lock = threading.Lock()
        self._callback_id = None
        self._pending = None
        self._active = False
        self._last_copy = 0.0
        self._offset = (0, 0)
        self.best = None

        layout = QVBoxLayout(self)
        instructions = QLabel(
            "Stop disk rotation and start Live in the main camera controls. "
            "Adjust alignment while watching the native-pixel patch.\n"
            "Cyan: current average. Magenta: session best (transparent overlay). "
            "Profiles are background-subtracted and peak-normalized."
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        controls = QGridLayout()
        self.offset_x = self._spin(-100000, 100000, 0)
        self.offset_y = self._spin(-100000, 100000, 0)
        self.radius = self._spin(3, 50, 12)
        self.radius.setToolTip(
            "Half-width of each averaging window. Fit one complete, isolated pinhole with dark borders."
        )
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(3, 30)
        self.threshold.setValue(6)
        self.threshold.setKeyboardTracking(False)
        for row, column, label, widget in (
            (0, 0, "Patch X offset (px)", self.offset_x),
            (0, 2, "Y offset (px)", self.offset_y),
            (1, 0, "Pinhole radius (px)", self.radius),
            (1, 2, "Detection (σ)", self.threshold),
        ):
            controls.addWidget(QLabel(label), row, column)
            controls.addWidget(widget, row, column + 1)
        self.reset_button = QPushButton("Reset best")
        self.reset_button.setAutoDefault(False)
        controls.addWidget(self.reset_button, 0, 4, 2, 1)
        layout.addLayout(controls)

        self.graphics = pg.GraphicsLayoutWidget()
        layout.addWidget(self.graphics, 1)
        self.patch_plot = self.graphics.addPlot(row=0, col=0, title="Camera patch · up to 512 × 512 px")
        self.average_plot = self.graphics.addPlot(row=0, col=1, title="Average pinhole · current + best")
        for plot in (self.patch_plot, self.average_plot):
            plot.setAspectLocked(True)
            plot.invertY(True)
            plot.setLabel("bottom", "X", units="px")
            plot.setLabel("left", "Y", units="px")
        self.patch_image = pg.ImageItem(axisOrder="row-major")
        self.patch_plot.addItem(self.patch_image)
        self.markers = pg.ScatterPlotItem(pen=pg.mkPen(CURRENT_COLOR), brush=None, size=13)
        self.patch_plot.addItem(self.markers)
        self.current_image = pg.ImageItem(axisOrder="row-major")
        self.best_image = pg.ImageItem(axisOrder="row-major")
        self.current_image.setLookupTable(self._lut(CURRENT_COLOR))
        self.best_image.setLookupTable(self._lut(BEST_COLOR))
        self.best_image.setOpacity(0.45)
        self.average_plot.addItem(self.current_image)
        self.average_plot.addItem(self.best_image)

        self.profile_plot = self.graphics.addPlot(row=1, col=0, colspan=2, title="Intensity through pinhole peak")
        self.profile_plot.setLabel("bottom", "Distance from peak", units="px")
        self.profile_plot.setLabel("left", "Normalized intensity")
        self.profile_plot.addLegend()
        self.profile_plot.showGrid(x=True, y=True, alpha=0.2)
        self.profile_plot.addLine(x=0, pen=pg.mkPen((150, 150, 150), style=Qt.DotLine))
        for level in (0.2, 0.8):
            self.profile_plot.addLine(y=level, pen=pg.mkPen((150, 150, 150), style=Qt.DotLine))
        self.current_x = self.profile_plot.plot(pen=pg.mkPen(CURRENT_COLOR, width=2), name="Current X")
        self.current_y = self.profile_plot.plot(
            pen=pg.mkPen(CURRENT_COLOR, width=2, style=Qt.DashLine), name="Current Y"
        )
        self.best_x = self.profile_plot.plot(pen=pg.mkPen((*BEST_COLOR, 130), width=3), name="Best X")
        self.best_y = self.profile_plot.plot(
            pen=pg.mkPen((*BEST_COLOR, 130), width=3, style=Qt.DashLine), name="Best Y"
        )
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        explanation = QLabel(
            "Best = highest mean 20–80% edge steepness (1/px), using at least 3 accepted pinholes. "
            "Edge-clipped, overlapping, saturated and undersized detections are excluded. "
            "Changing patch or detection settings resets best."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        for control in (self.offset_x, self.offset_y, self.radius, self.threshold):
            control.valueChanged.connect(self._settings_changed)
        self.reset_button.clicked.connect(self.reset_best)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._update_display)
        self.reset_best()

    @staticmethod
    def _spin(minimum, maximum, value):
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        spin.setKeyboardTracking(False)
        return spin

    @staticmethod
    def _lut(color):
        # Low intensity is transparent so the best image does not obscure current.
        levels = np.linspace(0, 1, 256)
        lut = np.empty((256, 4), dtype=np.uint8)
        lut[:, :3] = color
        lut[:, 3] = (255 * levels).astype(np.uint8)
        return lut

    def reset_best(self):
        self.best = None
        self.best_image.clear()
        self.best_x.clear()
        self.best_y.clear()
        self._clear_current()
        self.status.setText("Waiting for camera frames. Start Live in the main camera controls.")

    def _clear_current(self):
        self.current_image.clear()
        self.current_x.clear()
        self.current_y.clear()
        self.markers.clear()

    def _settings_changed(self):
        with self._lock:
            self._offset = (self.offset_x.value(), self.offset_y.value())
            self._pending = None
        self.reset_best()

    def showEvent(self, event):
        if self._callback_id is None:
            self.reset_best()
            with self._lock:
                self._active = True
                self._last_copy = 0.0
            self._callback_id = self.camera.add_frame_callback(self._on_frame)
            self.timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self.timer.stop()
        with self._lock:
            self._active = False
            self._pending = None
        if self._callback_id is not None:
            self.camera.remove_frame_callback(self._callback_id)
            self._callback_id = None
        super().hideEvent(event)

    def _on_frame(self, frame):
        # Camera-thread hot path: never touch Qt widgets or analyze full frames.
        with self._lock:
            now = time.monotonic()
            if not self._active or now - self._last_copy < 0.1:
                return
            try:
                self._pending = camera_patch(frame.frame, offset=self._offset)
            except ValueError:
                return
            self._last_copy = now

    def _draw_measurement(self, measurement, image, curve_x, curve_y):
        radius = (measurement.average.shape[0] - 1) // 2
        image.setImage(np.clip(measurement.average, 0, 1), autoLevels=False, levels=(0, 1))
        image.setRect(QRectF(-radius - 0.5, -radius - 0.5, 2 * radius + 1, 2 * radius + 1))
        x = np.arange(-radius, radius + 1)
        curve_x.setData(x, measurement.horizontal)
        curve_y.setData(x, measurement.vertical)

    def _update_display(self):
        with self._lock:
            patch, self._pending = self._pending, None
        if patch is None:
            return
        self.patch_image.setImage(patch, autoLevels=True)
        measurement = measure_pinholes(patch, self.radius.value(), self.threshold.value())
        if measurement is None:
            self._clear_current()
            self.status.setText(
                "No complete isolated pinholes detected. Adjust radius/detection threshold or illumination. "
                + self._best_text()
            )
            return
        self.markers.setData(x=measurement.centers[:, 0] + 0.5, y=measurement.centers[:, 1] + 0.5)
        self._draw_measurement(measurement, self.current_image, self.current_x, self.current_y)
        count = len(measurement.centers)
        if count >= 3 and (self.best is None or measurement.sharpness > self.best.sharpness):
            self.best = measurement
            self._draw_measurement(self.best, self.best_image, self.best_x, self.best_y)
        self.status.setText(
            f"{count} accepted pinholes · Current edge steepness: {measurement.sharpness:.3f} 1/px · "
            + self._best_text()
        )

    def _best_text(self):
        if self.best is None:
            return "Best: waiting for at least 3 complete pinholes."
        return f"Best edge steepness: {self.best.sharpness:.3f} 1/px (higher is sharper)."
