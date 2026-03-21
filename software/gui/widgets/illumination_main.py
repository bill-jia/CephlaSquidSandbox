from ._bootstrap import *

class IlluminationWidget(QWidget):
    """Standalone widget for manual illumination control.

    Displays one row per channel defined in the controller's ``channel_config``,
    with an intensity slider/spinbox pair and an on/off toggle button. When
    ``config_repo``, ``live_controller``, and ``objective_store`` are provided,
    a separate ``ObservationStateWidget`` (``gui.widgets_observation_state``) is
    placed below the channel rows for saving and loading imaging presets.

    The widget is controller-agnostic: it calls only
    ``IlluminationController.set_channel_intensity``, ``turn_on_channel``, and
    ``turn_off_channel``, regardless of whether the backend is a Teensy MCU,
    NI-DAQ, or serial light source.
    """

    # Thicker horizontal slider track/handle than default (~3px) for touchability
    _ILLUMINATION_SLIDER_QSS = """
        QSlider::groove:horizontal {
            border: 1px solid #888;
            height: 10px;
            background: #3a3a3a;
            margin: 4px 0;
            border-radius: 4px;
        }
        QSlider::sub-page:horizontal {
            background: #3d8f5a;
            border: 1px solid #4a9960;
            height: 10px;
            border-radius: 4px;
        }
        QSlider::add-page:horizontal {
            background: #3a3a3a;
            border-radius: 4px;
        }
        QSlider::handle:horizontal {
            background: #e8e8e8;
            border: 1px solid #666;
            width: 16px;
            height: 16px;
            margin: -5px 0;
            border-radius: 4px;
        }
        QSlider::handle:horizontal:hover {
            background: #ffffff;
        }
    """

    def __init__(
        self,
        illumination_controller,
        parent=None,
        *,
        config_repo=None,
        live_controller=None,
        objective_store=None,
        emission_filter_wheel=None,
        on_observation_state_changed=None,
    ):
        """
        Args:
            illumination_controller: An ``IlluminationController`` instance that must
                have a non-None ``channel_config`` attribute.
            parent: Optional parent widget.
            config_repo: ``ConfigRepository`` for Observation State presets (optional).
            live_controller: ``LiveController`` for collecting/applying Observation State.
            objective_store: ``ObjectiveStore`` for current objective when applying state.
            emission_filter_wheel: Optional hardware handle for filter positions in presets.
            on_observation_state_changed: Callback after save/load (e.g. refresh channel lists).
        """
        super().__init__(parent)
        self._controller = illumination_controller
        self._channel_rows: Dict[str, dict] = {}  # channel_name -> {slider, spinbox, btn}
        self._on_observation_state_changed = on_observation_state_changed
        self.observation_state_widget: Optional["ObservationStateWidget"] = None

        self._build_ui(
            config_repo=config_repo,
            live_controller=live_controller,
            objective_store=objective_store,
            emission_filter_wheel=emission_filter_wheel,
        )
        self._refresh_from_state()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(
        self,
        *,
        config_repo=None,
        live_controller=None,
        objective_store=None,
        emission_filter_wheel=None,
    ):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(4)

        # Title (compact so channel rows can use taller sliders)
        title = QLabel("Illumination Control")
        title.setStyleSheet("font-weight: bold; font-size: 11px;")
        root.addWidget(title)

        # Channel rows (no "Channels" group title — layout only)
        channels_group = QWidget()
        channels_layout = QGridLayout(channels_group)
        channels_layout.setContentsMargins(0, 0, 0, 0)
        channels_layout.setHorizontalSpacing(6)
        channels_layout.setVerticalSpacing(6)

        channel_config = getattr(self._controller, "channel_config", None)
        channel_names: List[str] = getattr(self._controller, "channel_names", []) or []
        name_to_wavelength: Dict[str, Optional[int]] = {}

        unified_lm_name: Optional[str] = None
        if getattr(self._controller, "has_unified_led_matrix", lambda: False)():
            unified_lm_name = getattr(self._controller, "unified_led_matrix_channel_name", lambda: None)()

        # Option B: show only channels backed by devices, i.e. controller.channel_names.
        # If the controller also exposes an IlluminationChannelConfig, use it only
        # to enrich labels (wavelength display).
        if channel_config is not None and hasattr(channel_config, "channels"):
            try:
                channels = channel_config.channels or []
                for ch in channels:
                    if ch.name in channel_names:
                        name_to_wavelength[ch.name] = getattr(ch, "wavelength_nm", None)
            except Exception:
                # If we can't inspect channel_config, fall back to parsing from names.
                name_to_wavelength = {}

        # Fallback wavelength extraction for compositor-backed channel names.
        # Channel names typically look like "Fluorescence 405 nm Ex".
        _WL_RE = re.compile(r"(\d{3,4})\s*nm", re.IGNORECASE)
        for ch_name in channel_names:
            if ch_name not in name_to_wavelength:
                m = _WL_RE.search(ch_name)
                name_to_wavelength[ch_name] = int(m.group(1)) if m else None

        for row_idx, ch_name in enumerate(channel_names):
            # Name label (unified LED matrix: label + mode selector)
            label_text = ch_name
            wl = name_to_wavelength.get(ch_name)
            if wl is not None:
                label_text += f"  ({wl} nm)"

            mode_combo: Optional[QComboBox] = None
            if unified_lm_name and ch_name == unified_lm_name:
                name_cell = QWidget()
                name_row = QHBoxLayout(name_cell)
                name_row.setContentsMargins(0, 0, 0, 0)
                name_row.setSpacing(6)
                name_row.addWidget(QLabel(label_text))
                mode_combo = QComboBox()
                for mode_key, mode_label in self._controller.led_matrix_mode_items():
                    mode_combo.addItem(mode_label, mode_key)
                cur_mode = self._controller.get_led_matrix_mode()
                if cur_mode is not None:
                    mi = mode_combo.findData(cur_mode)
                    if mi >= 0:
                        mode_combo.setCurrentIndex(mi)
                mode_combo.setMinimumWidth(160)
                name_row.addWidget(mode_combo, stretch=1)
                label = name_cell
            else:
                label = QLabel(label_text)
                label.setMinimumWidth(120)

            # Intensity slider (thicker groove/handle for easier interaction)
            slider = QSlider(Qt.Horizontal)
            slider.setMinimum(0)
            slider.setMaximum(100)
            slider.setValue(0)
            slider.setMinimumWidth(120)
            slider.setMinimumHeight(22)
            slider.setStyleSheet(IlluminationWidget._ILLUMINATION_SLIDER_QSS)

            # Intensity spinbox
            spinbox = QDoubleSpinBox()
            spinbox.setMinimum(0.0)
            spinbox.setMaximum(100.0)
            spinbox.setSingleStep(1.0)
            spinbox.setDecimals(1)
            spinbox.setValue(0.0)
            spinbox.setSuffix(" %")
            spinbox.setFixedWidth(80)

            # On/Off toggle button
            btn = QPushButton("OFF")
            btn.setCheckable(True)
            btn.setChecked(False)
            btn.setFixedWidth(52)
            self._apply_shutter_style(btn, False)

            channels_layout.addWidget(label,   row_idx, 0)
            channels_layout.addWidget(slider,  row_idx, 1)
            channels_layout.addWidget(spinbox, row_idx, 2)
            channels_layout.addWidget(btn,     row_idx, 3)

            # Store references
            row_data: Dict = {
                "slider": slider,
                "spinbox": spinbox,
                "btn": btn,
            }
            if mode_combo is not None:
                row_data["mode_combo"] = mode_combo
            self._channel_rows[ch_name] = row_data

            # Wire signals (capture ch.name by closure)
            name = ch_name
            slider.valueChanged.connect(
                lambda v, n=name: self._on_slider_changed(n, v)
            )
            spinbox.valueChanged.connect(
                lambda v, n=name: self._on_spinbox_changed(n, v)
            )
            btn.toggled.connect(
                lambda checked, n=name: self._on_shutter_toggled(n, checked)
            )
            if mode_combo is not None:
                mode_combo.currentIndexChanged.connect(
                    lambda idx, n=name: self._on_led_matrix_mode_changed(n, idx)
                )

        root.addWidget(channels_group)

        if config_repo is not None and live_controller is not None and objective_store is not None:
            from gui.widgets_observation_state import ObservationStateWidget

            self.observation_state_widget = ObservationStateWidget(
                parent=self,
                config_repo=config_repo,
                live_controller=live_controller,
                objective_store=objective_store,
                emission_filter_wheel=emission_filter_wheel,
                on_state_changed=self._on_observation_state_applied,
            )
            root.addWidget(self.observation_state_widget)
        root.addStretch()

    # ------------------------------------------------------------------
    # Slot handlers
    # ------------------------------------------------------------------

    def _on_slider_changed(self, channel_name: str, value: int):
        row = self._channel_rows.get(channel_name)
        if row is None:
            return
        spinbox: QDoubleSpinBox = row["spinbox"]
        spinbox.blockSignals(True)
        spinbox.setValue(float(value))
        spinbox.blockSignals(False)
        self._controller.set_channel_intensity(channel_name, float(value))

    def _on_spinbox_changed(self, channel_name: str, value: float):
        row = self._channel_rows.get(channel_name)
        if row is None:
            return
        slider: QSlider = row["slider"]
        slider.blockSignals(True)
        slider.setValue(int(round(value)))
        slider.blockSignals(False)
        self._controller.set_channel_intensity(channel_name, value)

    def _on_shutter_toggled(self, channel_name: str, checked: bool):
        row = self._channel_rows.get(channel_name)
        if row is None:
            return
        btn: QPushButton = row["btn"]
        btn.setText("ON" if checked else "OFF")
        self._apply_shutter_style(btn, checked)
        if checked:
            self._controller.turn_on_channel(channel_name)
        else:
            self._controller.turn_off_channel(channel_name)

    def _on_led_matrix_mode_changed(self, channel_name: str, index: int):
        row = self._channel_rows.get(channel_name)
        if row is None:
            return
        combo = row.get("mode_combo")
        if combo is None:
            return
        mode_key = combo.itemData(index)
        if mode_key is not None and getattr(self._controller, "set_led_matrix_mode", None):
            self._controller.set_led_matrix_mode(mode_key)

    def refresh_observation_state_presets(self) -> None:
        """Refresh the Observation State preset dropdown (e.g. after profile switch)."""
        if self.observation_state_widget is not None:
            self.observation_state_widget.refresh_presets()

    def _on_observation_state_applied(self) -> None:
        self._refresh_from_state()
        if self._on_observation_state_changed:
            self._on_observation_state_changed()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_shutter_style(btn: QPushButton, is_on: bool):
        if is_on:
            btn.setStyleSheet(
                "QPushButton { background-color: #2ecc71; color: white; font-weight: bold; border-radius: 3px; }"
            )
        else:
            btn.setStyleSheet(
                "QPushButton { background-color: #555; color: #ccc; font-weight: bold; border-radius: 3px; }"
            )

    def _refresh_from_state(self):
        """Update all UI controls to match the controller's current _channel_state."""
        channel_state: dict = getattr(self._controller, "_channel_state", {})
        for name, row in self._channel_rows.items():
            state = channel_state.get(name)
            if state is None:
                continue
            slider: QSlider = row["slider"]
            spinbox: QDoubleSpinBox = row["spinbox"]
            btn: QPushButton = row["btn"]

            slider.blockSignals(True)
            spinbox.blockSignals(True)
            btn.blockSignals(True)

            slider.setValue(int(round(state.intensity)))
            spinbox.setValue(state.intensity)
            btn.setChecked(state.is_on)
            btn.setText("ON" if state.is_on else "OFF")
            self._apply_shutter_style(btn, state.is_on)

            combo = row.get("mode_combo")
            if combo is not None and getattr(self._controller, "get_led_matrix_mode", None):
                mk = self._controller.get_led_matrix_mode()
                if mk is not None:
                    mi = combo.findData(mk)
                    if mi >= 0:
                        combo.blockSignals(True)
                        combo.setCurrentIndex(mi)
                        combo.blockSignals(False)

            slider.blockSignals(False)
            spinbox.blockSignals(False)
            btn.blockSignals(False)
