"""
Pulse-timing editor dialog for waveform-driven observation states.

Per-illuminator pulse-comb editor used for both capture-window timed pulses
(``IlluminatorTiming`` overlaid on a camera exposure) and stimulus-only
steps (``ObservationState.is_stimulus_only=True``). A single pulse is a
comb with ``num_pulses=1``; a comb is a regularly spaced train of pulses.

For stimulus-only states, a top-of-dialog ``Stimulus duration (ms)`` spin
box controls the total NIDAQ window. For capture states, combs must fit
within the camera exposure.

No live hardware is touched here — pulse timing only takes effect when an
acquisition runs.
"""

from ._bootstrap import *

from control.models.observation_state import IlluminatorState, IlluminatorTiming, ObservationState


_DEFAULT_DURATION_MS = 1.0
_DEFAULT_PERIOD_MS = 10.0
_MAX_MS = 1_000_000.0


class PulseTimingDialog(QDialog):
    """Per-illuminator pulse-comb editor.

    Args:
        state: The observation state being edited. The dialog mutates its
            ``illuminator_states`` (and ``stimulus_duration_ms`` for
            stimulus-only states) in place on accept.
        illumination_controller: Optional. When supplied, rows for channels
            without an NIDAQ DO gate are greyed out with an explanatory
            tooltip.
        parent: Qt parent widget.
    """

    def __init__(self, state: ObservationState, illumination_controller=None, parent=None):
        super().__init__(parent)
        self._state = state
        self._ic = illumination_controller
        self._rows: list[dict] = []

        self.setWindowTitle(f"Pulse Timing — {state.name}")
        self.setMinimumWidth(820)
        self._build_ui()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)

        is_stimulus = bool(self._state.is_stimulus_only)
        if is_stimulus:
            info_text = (
                "Stimulus-only step: configure NIDAQ pulse combs that fire on the "
                "selected illuminators. Each comb runs once per FOV inside the configured "
                "Stimulus duration window — no camera frame is captured."
            )
        else:
            info_text = (
                f"Configure NIDAQ pulse combs that fire on each illuminator inside the "
                f"{float(self._state.exposure_time):.2f} ms camera exposure. Rows with the "
                "checkbox unticked keep the illuminator on for the full exposure (standard "
                "behaviour)."
            )
        info = QLabel(info_text)
        info.setWordWrap(True)
        layout.addWidget(info)

        # Stimulus-only: surface the total stimulus duration spinbox.
        if is_stimulus:
            dur_row = QHBoxLayout()
            dur_row.addWidget(QLabel("Stimulus duration:"))
            self._stim_duration_spin = QDoubleSpinBox()
            self._stim_duration_spin.setRange(0.01, _MAX_MS)
            self._stim_duration_spin.setDecimals(2)
            self._stim_duration_spin.setSingleStep(10.0)
            self._stim_duration_spin.setSuffix(" ms")
            self._stim_duration_spin.setValue(float(self._state.stimulus_duration_ms or 100.0))
            self._stim_duration_spin.valueChanged.connect(self._refresh_end_labels)
            dur_row.addWidget(self._stim_duration_spin)
            dur_row.addStretch(1)
            layout.addLayout(dur_row)
        else:
            self._stim_duration_spin = None

        # Comb table
        table = QTableWidget()
        table.setColumnCount(8)
        table.setHorizontalHeaderLabels([
            "Channel", "Pulse?", "Start (ms)", "Width (ms)",
            "Period (ms)", "# pulses", "Last edge (ms)", "NIDAQ line",
        ])
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, 8):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        table.setSelectionMode(QTableWidget.NoSelection)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.verticalHeader().setVisible(False)

        illuminators = list(self._state.illuminator_states)
        table.setRowCount(len(illuminators))
        for row, ist in enumerate(illuminators):
            self._populate_row(table, row, ist)
        layout.addWidget(table)
        self._table = table

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh_end_labels()

    def _populate_row(self, table: QTableWidget, row: int, ist: IlluminatorState):
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

        timing = ist.timing

        # Start offset
        start_spin = QDoubleSpinBox()
        start_spin.setRange(0.0, _MAX_MS)
        start_spin.setDecimals(2)
        start_spin.setSingleStep(0.1)
        start_spin.setSuffix(" ms")
        start_spin.setValue(timing.start_offset_ms if timing else 0.0)
        table.setCellWidget(row, 2, start_spin)

        # Pulse width
        width_spin = QDoubleSpinBox()
        width_spin.setRange(0.01, _MAX_MS)
        width_spin.setDecimals(2)
        width_spin.setSingleStep(0.1)
        width_spin.setSuffix(" ms")
        width_spin.setValue(timing.pulse_width_ms if timing else _DEFAULT_DURATION_MS)
        table.setCellWidget(row, 3, width_spin)

        # Period
        period_spin = QDoubleSpinBox()
        period_spin.setRange(0.0, _MAX_MS)
        period_spin.setDecimals(2)
        period_spin.setSingleStep(0.1)
        period_spin.setSuffix(" ms")
        period_spin.setValue(timing.period_ms if timing else _DEFAULT_PERIOD_MS)
        table.setCellWidget(row, 4, period_spin)

        # Num pulses
        num_spin = QSpinBox()
        num_spin.setRange(1, 1_000_000)
        num_spin.setSingleStep(1)
        num_spin.setValue(timing.num_pulses if timing else 1)
        table.setCellWidget(row, 5, num_spin)

        # Last edge label (read-only summary; recomputed on field changes)
        end_label = QLabel("—")
        end_label.setStyleSheet("color: #666; padding-left: 6px;")
        table.setCellWidget(row, 6, end_label)

        # NIDAQ line / disabled-state warning
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
        else:
            nidaq_label = QLabel("?")
            nidaq_label.setToolTip("NIDAQ line lookup unavailable in this context.")
        nidaq_label.setStyleSheet("color: #666; padding-left: 6px;")
        table.setCellWidget(row, 7, nidaq_label)

        # Period spinbox is only meaningful for combs with num_pulses > 1.
        def _sync_period_enabled():
            period_spin.setEnabled(enable.isChecked() and num_spin.value() > 1)

        # Enable/disable spinboxes with the checkbox; update end label on any change.
        def _on_enable_toggled(checked: bool):
            for w in (start_spin, width_spin, num_spin):
                w.setEnabled(checked)
            _sync_period_enabled()
            self._refresh_end_labels()

        enable.toggled.connect(_on_enable_toggled)
        for spin in (start_spin, width_spin, period_spin, num_spin):
            spin.valueChanged.connect(lambda *_: self._refresh_end_labels())
        num_spin.valueChanged.connect(lambda *_: _sync_period_enabled())

        enabled_initial = enable.isChecked() and enable.isEnabled()
        for w in (start_spin, width_spin, num_spin):
            w.setEnabled(enabled_initial)
        period_spin.setEnabled(enabled_initial and num_spin.value() > 1)

        self._rows.append({
            "ist": ist,
            "enable": enable,
            "start": start_spin,
            "width": width_spin,
            "period": period_spin,
            "num": num_spin,
            "end_label": end_label,
            "nidaq_line": nidaq_line,
        })

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _window_ms(self) -> float:
        if self._stim_duration_spin is not None:
            return float(self._stim_duration_spin.value())
        return float(self._state.exposure_time)

    def _refresh_end_labels(self) -> None:
        window_ms = self._window_ms()
        for entry in self._rows:
            label: QLabel = entry["end_label"]
            if not entry["enable"].isChecked():
                label.setText("—")
                label.setStyleSheet("color: #666; padding-left: 6px;")
                continue
            start = float(entry["start"].value())
            width = float(entry["width"].value())
            period = float(entry["period"].value())
            num = int(entry["num"].value())
            end_ms = start + max(0, num - 1) * period + width
            label.setText(f"{end_ms:.2f}")
            if end_ms > window_ms + 1e-6:
                label.setStyleSheet("color: #c00; padding-left: 6px; font-weight: bold;")
                label.setToolTip(f"Exceeds {window_ms:.2f} ms step window")
            else:
                label.setStyleSheet("color: #666; padding-left: 6px;")
                label.setToolTip("")

    # ── OK handler ───────────────────────────────────────────────────────────

    def _on_accept(self):
        window_ms = self._window_ms()
        errors: list[str] = []
        new_illuminators: list[IlluminatorState] = []

        for entry in self._rows:
            ist: IlluminatorState = entry["ist"]
            if not entry["enable"].isChecked():
                # Drop the timing block entirely.
                new_illuminators.append(ist.model_copy(update={"timing": None}))
                continue

            start = float(entry["start"].value())
            width = float(entry["width"].value())
            period = float(entry["period"].value())
            num = int(entry["num"].value())

            if width <= 0:
                errors.append(f"{ist.illumination_channel}: pulse width must be > 0 ms")
                continue
            if num > 1 and period <= width:
                errors.append(
                    f"{ist.illumination_channel}: period ({period:.2f} ms) must exceed pulse width "
                    f"({width:.2f} ms) for a multi-pulse comb"
                )
                continue
            end_ms = start + max(0, num - 1) * period + width
            if end_ms > window_ms + 1e-6:
                errors.append(
                    f"{ist.illumination_channel}: comb ends at {end_ms:.2f} ms, "
                    f"outside the {window_ms:.2f} ms step window"
                )
                continue
            if entry["nidaq_line"] is None and self._ic is not None:
                errors.append(
                    f"{ist.illumination_channel}: no NIDAQ digital-output gating line"
                )
                continue
            new_illuminators.append(
                ist.model_copy(update={
                    "timing": IlluminatorTiming(
                        start_offset_ms=start,
                        pulse_width_ms=width,
                        period_ms=period if num > 1 else 0.0,
                        num_pulses=num,
                    )
                })
            )

        # For stimulus-only states require at least one timed illuminator.
        if self._state.is_stimulus_only:
            has_timed = any(ist.on and ist.timing is not None for ist in new_illuminators)
            if not has_timed:
                errors.append(
                    "Stimulus-only step requires at least one active illuminator with pulse timing"
                )

        if errors:
            QMessageBox.warning(self, "Invalid pulse timing", "\n".join(errors))
            return

        # Commit changes in place.
        self._state.illuminator_states = new_illuminators
        if self._stim_duration_spin is not None:
            self._state.stimulus_duration_ms = float(self._stim_duration_spin.value())
        self.accept()

    # ── Convenience ──────────────────────────────────────────────────────────

    @staticmethod
    def summary(state: ObservationState) -> str:
        """Short label for the configurator's Timing column button."""
        combs = [
            ist for ist in state.illuminator_states if ist.on and ist.timing is not None
        ]
        if not combs:
            return "Configure…"
        if len(combs) == 1:
            t = combs[0].timing
            if t.num_pulses == 1:
                return f"1 × {t.pulse_width_ms:.2f} ms @ {t.start_offset_ms:.2f} ms"
            return f"{t.num_pulses} × {t.pulse_width_ms:.2f} ms @ {t.period_ms:.2f} ms period"
        return f"{len(combs)} channels pulsed"
