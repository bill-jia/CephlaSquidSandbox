"""Shared center-to-center tile pitch controls for multipoint acquisition."""

from qtpy.QtCore import Signal
from qtpy.QtWidgets import QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QWidget


class TileSpacingWidget(QWidget):
    changed = Signal()

    def __init__(self, overlap, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.mode = QComboBox()
        self.mode.addItems(["Overlap", "Spacing", "Auto-find"])
        self.mode.model().item(2).setEnabled(False)
        self.mode.model().item(2).setToolTip("Coming later")
        layout.addWidget(self.mode)
        self.overlap_row = QWidget()
        row = QHBoxLayout(self.overlap_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(overlap)
        layout.addWidget(self.overlap_row)
        self.spacing_row = QWidget()
        row = QHBoxLayout(self.spacing_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.units = QComboBox()
        self.units.addItems(["Distance", "N FOVs"])
        row.addWidget(self.units)
        self.distance = self._pair(row, " mm", 0.001, 1000, 1)
        self.fovs = self._pair(row, " FOVs", 1, 1000, 1)
        self.spacing_row.setToolTip(
            "Spacing is measured between tile centers. 1 FOV gives touching tiles; "
            "2 FOVs leaves one FOV between tiles. X uses FOV width; Y uses FOV height."
        )
        layout.addWidget(self.spacing_row)
        layout.addStretch()
        self.mode.currentIndexChanged.connect(self._changed)
        self.units.currentIndexChanged.connect(self._changed)
        self._update_visibility()

    def _pair(self, layout, suffix, minimum, maximum, value):
        panel = QWidget()
        row = QHBoxLayout(panel)
        row.setContentsMargins(0, 0, 0, 0)
        entries = []
        for axis in ("X", "Y"):
            entry = QDoubleSpinBox()
            entry.setDecimals(3)
            entry.setRange(minimum, maximum)
            entry.setValue(value)
            entry.setSuffix(suffix)
            entry.setKeyboardTracking(False)
            entry.valueChanged.connect(self.changed.emit)
            row.addWidget(QLabel(axis))
            row.addWidget(entry)
            entries.append(entry)
        layout.addWidget(panel)
        return panel, entries

    def _update_visibility(self):
        spacing = self.mode.currentIndex() == 1
        self.overlap_row.setVisible(not spacing)
        self.spacing_row.setVisible(spacing)
        self.distance[0].setVisible(self.units.currentIndex() == 0)
        self.fovs[0].setVisible(self.units.currentIndex() == 1)

    def _changed(self):
        self._update_visibility()
        self.changed.emit()

    def parameters(self):
        if self.mode.currentIndex() != 1:
            return {}
        entries = (self.distance if self.units.currentIndex() == 0 else self.fovs)[1]
        return dict(
            spacing_mode="distance" if self.units.currentIndex() == 0 else "fov",
            spacing_x=entries[0].value(), spacing_y=entries[1].value(),
        )

    def restore(self, params):
        self.blockSignals(True)
        try:
            params = params or {}
            mode = params.get("spacing_mode")
            self.mode.setCurrentIndex(1 if mode in ("distance", "fov") else 0)
            self.units.setCurrentIndex(1 if mode == "fov" else 0)
            entries = (self.fovs if mode == "fov" else self.distance)[1]
            entries[0].setValue(params.get("spacing_x", 1))
            entries[1].setValue(params.get("spacing_y", 1))
        finally:
            self.blockSignals(False)


def spacing_parameters(widget):
    controls = getattr(widget, "tile_spacing", None)
    return controls.parameters() if controls is not None else {}
