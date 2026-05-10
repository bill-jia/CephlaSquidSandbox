"""
Pulse-timing editor dialog for waveform-driven observation states.

Lets the user attach an :class:`IlluminatorTiming` block (offset_ms,
duration_ms) to each illuminator within a single observation state. The
dialog mutates the supplied ``ObservationState`` in place when the user
clicks OK; the parent dialog (``ObservationStateConfiguratorDialog``) is
responsible for persisting the change to YAML.

No live hardware is touched here — pulse timing only takes effect when an
acquisition runs.
"""

from ._bootstrap import *

from control.models.observation_state import IlluminatorState, IlluminatorTiming, ObservationState


class PulseTimingDialog(QDialog):
    """Per-illuminator pulse-timing editor.

    Args:
        state: The observation state being edited. The dialog mutates its
            ``illuminator_states`` in place on accept.
        illumination_controller: Optional. When supplied the dialog uses
            :meth:`get_nidaq_do_line_for_channel` to grey-out illuminators
            that have no NIDAQ digital-output gate (LED matrix, serial-only
            light sources). Pass ``None`` to skip that check (e.g. when the
            controller is not available in the current context).
        parent: Qt parent widget.
    """

    def __init__(self, state: ObservationState, illumination_controller=None, parent=None):
        super().__init__(parent)
        self._state = state
        self._ic = illumination_controller
        self._rows: list[dict] = []  # per-row widget references

        self.setWindowTitle(f"Pulse Timing — {state.name}")
        self.setMinimumWidth(640)
        self._build_ui()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)

        exposure_ms = float(self._state.exposure_time)
        info = QLabel(
            "Configure NIDAQ-driven pulse timing for each illuminator within the "
            f"{exposure_ms:.2f} ms camera exposure window. "
            "Disabled rows turn the illuminator on for the full exposure (the standard behaviour). "
            "Enabled rows pulse the illuminator's NIDAQ digital-output gate during the configured "
            "offset/duration window only."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        table = QTableWidget()
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(
            ["Channel", "Pulse?", "Offset (ms)", "Duration (ms)", "NIDAQ line"]
        )
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 3, 4):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        table.setSelectionMode(QTableWidget.NoSelection)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.verticalHeader().setVisible(False)

        illuminators = list(self._state.illuminator_states)
        table.setRowCount(len(illuminators))
        for row, ist in enumerate(illuminators):
            self._populate_row(table, row, ist, exposure_ms)
        layout.addWidget(table)
        self._table = table

        # Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _populate_row(self, table: QTableWidget, row: int, ist: IlluminatorState, exposure_ms: float):
        # Channel name
        name_item = QTableWidgetItem(ist.illumination_channel)
        name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
        table.setItem(row, 0, name_item)

        # Enable checkbox
        enable = QCheckBox()
        enable.setChecked(ist.timing is not None)
        enable_widget = QWidget()
        enable_layout = QHBoxLayout(enable_widget)
        enable_layout.setContentsMargins(0, 0, 0, 0)
        enable_layout.setAlignment(Qt.AlignCenter)
        enable_layout.addWidget(enable)
        table.setCellWidget(row, 1, enable_widget)

        # Offset / duration spinboxes
        offset_spin = QDoubleSpinBox()
        offset_spin.setRange(0.0, 1000.0)
        offset_spin.setDecimals(2)
        offset_spin.setSingleStep(0.1)
        offset_spin.setSuffix(" ms")
        offset_spin.setValue(ist.timing.offset_ms if ist.timing else 0.0)
        table.setCellWidget(row, 2, offset_spin)

        duration_spin = QDoubleSpinBox()
        duration_spin.setRange(0.01, 1000.0)
        duration_spin.setDecimals(2)
        duration_spin.setSingleStep(0.1)
        duration_spin.setSuffix(" ms")
        duration_spin.setValue(ist.timing.duration_ms if ist.timing else 1.0)
        table.setCellWidget(row, 3, duration_spin)

        # NIDAQ line label / disabled-state warning
        nidaq_line = None
        if self._ic is not None:
            try:
                nidaq_line = self._ic.get_nidaq_do_line_for_channel(ist.illumination_channel)
            except Exception:
                nidaq_line = None
        if nidaq_line is not None:
            nidaq_label = QLabel(f"line {nidaq_line}")
        elif self._ic is not None:
            nidaq_label = QLabel("(no NIDAQ gate)")
            nidaq_label.setToolTip(
                "This channel has no NIDAQ digital-output gating line — "
                "pulse timing cannot be applied (LED matrix and serial-only "
                "light sources are not supported)."
            )
            enable.setEnabled(False)
            offset_spin.setEnabled(False)
            duration_spin.setEnabled(False)
        else:
            nidaq_label = QLabel("?")
            nidaq_label.setToolTip("NIDAQ line lookup unavailable in this context.")
        nidaq_label.setStyleSheet("color: #666; padding-left: 6px;")
        table.setCellWidget(row, 4, nidaq_label)

        # Toggle spinbox enable state with the checkbox
        def _on_enable_toggled(checked: bool, off=offset_spin, dur=duration_spin):
            off.setEnabled(checked)
            dur.setEnabled(checked)
        enable.toggled.connect(_on_enable_toggled)
        offset_spin.setEnabled(enable.isChecked() and enable.isEnabled())
        duration_spin.setEnabled(enable.isChecked() and enable.isEnabled())

        self._rows.append({
            "ist": ist,
            "enable": enable,
            "offset": offset_spin,
            "duration": duration_spin,
            "nidaq_line": nidaq_line,
        })

    # ── OK handler ───────────────────────────────────────────────────────────

    def _on_accept(self):
        exposure_ms = float(self._state.exposure_time)
        errors: list[str] = []
        new_illuminators: list[IlluminatorState] = []

        for entry in self._rows:
            ist: IlluminatorState = entry["ist"]
            if not entry["enable"].isChecked():
                # Drop the timing block entirely.
                new_illuminators.append(ist.model_copy(update={"timing": None}))
                continue
            offset = float(entry["offset"].value())
            duration = float(entry["duration"].value())
            if duration <= 0:
                errors.append(f"{ist.illumination_channel}: duration must be > 0 ms")
                continue
            if offset + duration > exposure_ms + 1e-6:
                errors.append(
                    f"{ist.illumination_channel}: pulse window "
                    f"[{offset:.2f}, {offset + duration:.2f}] ms exceeds exposure of {exposure_ms:.2f} ms"
                )
                continue
            if entry["nidaq_line"] is None and self._ic is not None:
                errors.append(
                    f"{ist.illumination_channel}: no NIDAQ digital-output gating line"
                )
                continue
            new_illuminators.append(
                ist.model_copy(update={"timing": IlluminatorTiming(offset_ms=offset, duration_ms=duration)})
            )

        if errors:
            QMessageBox.warning(self, "Invalid pulse timing", "\n".join(errors))
            return

        # Mutate the state's illuminator list in place (order preserved).
        self._state.illuminator_states = new_illuminators
        self.accept()

    # ── Convenience ──────────────────────────────────────────────────────────

    @staticmethod
    def summary(state: ObservationState) -> str:
        """Short label for the configurator's Timing column button."""
        timed = sum(
            1 for ist in state.illuminator_states if ist.on and ist.timing is not None
        )
        if timed == 0:
            return "Configure…"
        if timed == 1:
            return "1 channel pulsed"
        return f"{timed} channels pulsed"
