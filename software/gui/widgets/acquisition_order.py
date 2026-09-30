"""Drag-to-reorder loop blocks, with an explicit nested-bracket preview."""

from qtpy.QtCore import Qt, Signal
from qtpy.QtWidgets import QAbstractItemView, QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from control.models.acquisition_order import DEFAULT_ORDER, validate_order


class AcquisitionOrderWidget(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(QLabel("Loop order — drag: outer → inner"))
        self.blocks = QListWidget()
        self.blocks.setFlow(QListWidget.LeftToRight)
        self.blocks.setWrapping(False)
        self.blocks.setDragDropMode(QAbstractItemView.InternalMove)
        self.blocks.setDefaultDropAction(Qt.MoveAction)
        self.blocks.setSelectionMode(QAbstractItemView.SingleSelection)
        self.blocks.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.blocks.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.blocks.setFixedHeight(36)
        self.blocks.setSpacing(3)
        for axis in DEFAULT_ORDER:
            item = QListWidgetItem(f"[ {axis} ]")
            item.setData(Qt.UserRole, axis)
            item.setToolTip({
                "T": "Time-lapse", "Z": "Z-stack", "Pos": "Positions and tiles",
                "C": "Simple: selected channels. Advanced: selected cycles, including their complete nested loops.",
            }[axis])
            self.blocks.addItem(item)
        layout.addWidget(self.blocks)
        self.preview = QLabel()
        self.preview.setToolTip("Nested brackets show active loops. Inactive dimensions are skipped.")
        layout.addWidget(self.preview)
        self._active = set(DEFAULT_ORDER)
        self.blocks.model().rowsMoved.connect(self._moved)
        self._refresh()

    def order(self):
        return tuple(self.blocks.item(i).data(Qt.UserRole) for i in range(self.blocks.count()))

    def restore(self, order):
        order = validate_order(order)
        items = {self.blocks.item(i).data(Qt.UserRole): self.blocks.item(i) for i in range(self.blocks.count())}
        while self.blocks.count():
            self.blocks.takeItem(0)
        for axis in order:
            self.blocks.addItem(items[axis])
        self._refresh()

    def set_active(self, axes):
        self._active = set(axes)
        self._refresh()

    def _refresh(self):
        active = []
        for i in range(self.blocks.count()):
            item = self.blocks.item(i)
            axis = item.data(Qt.UserRole)
            enabled = axis in self._active
            item.setText(f"[ {axis} ]" if enabled else f"{axis} (off)")
            if enabled:
                active.append(axis)
        self.preview.setText(" ".join("[ " + axis for axis in active) + " [ Capture ]" + " ]" * len(active))

    def _moved(self):
        self._refresh()
        self.changed.emit()
