from ._bootstrap import *
from control.lighting import IlluminationController

class IlluminationWidget(QWidget):
    """Standalone widget for manual illumination control.

    Displays one row per channel defined in the controller's ``channel_config``,
    with an intensity slider/spinbox pair and an on/off toggle button.

    The widget is controller-agnostic: it calls only
    ``IlluminationController.set_channel_intensity`` and ``IlluminationController.set_channel_state``
    regardless of whether the backend is a Teensy MCU,
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
        illumination_controller: IlluminationController,
        parent=None,
        obs_controller=None,
        objective_store=None,
    ):
        """
        Args:
            illumination_controller: An ``IlluminationController`` instance that must
                have a non-None ``channel_config`` attribute.
            parent: Optional parent widget.
            obs_controller: ObservationStateController for mediated hardware access.
                If provided, intensity/on-off/mode changes go through it.
            objective_store: ObjectiveStore for reading the current objective NA
                (used for the 1.25x/0.75x reference marks in the LED matrix config
                popup). Falls back to ``obs_controller.microscope.objective_store``.
        """
        super().__init__(parent)
        self._controller: IlluminationController = illumination_controller
        self._obs_controller = obs_controller
        self._objective_store = objective_store
        self._channel_rows: Dict[str, dict] = {}  # channel_name -> {slider, spinbox, btn}

        self._build_ui()
        self._refresh_from_state()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
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
            na_spinbox: Optional[QDoubleSpinBox] = None
            cfg_btn: Optional[QToolButton] = None
            if unified_lm_name and ch_name == unified_lm_name:
                name_cell = QWidget()
                name_row = QHBoxLayout(name_cell)
                name_row.setContentsMargins(0, 0, 0, 0)
                name_row.setSpacing(4)
                name_row.addWidget(QLabel(label_text))
                mode_combo = QComboBox()
                for mode_key, mode_label in self._controller.led_matrix_mode_items():
                    mode_combo.addItem(mode_label, mode_key)
                cur_mode = self._controller.get_led_matrix_mode()
                if cur_mode is not None:
                    mi = mode_combo.findData(cur_mode)
                    if mi >= 0:
                        mode_combo.setCurrentIndex(mi)
                mode_combo.setMinimumWidth(130)
                name_row.addWidget(mode_combo, stretch=1)

                # Compact Array-NA spinbox + a config-popup button. Only present
                # when the array exposes the API (absent for plain MCU matrices).
                cur_na = None
                if hasattr(self._controller, "get_led_matrix_array_na"):
                    try:
                        cur_na = self._controller.get_led_matrix_array_na()
                    except Exception:
                        cur_na = None
                if cur_na is not None:
                    na_lbl = QLabel("NA")
                    name_row.addWidget(na_lbl)
                    na_spinbox = QDoubleSpinBox()
                    na_spinbox.setMinimum(0.05)
                    na_spinbox.setMaximum(0.99)
                    na_spinbox.setSingleStep(0.05)
                    na_spinbox.setDecimals(2)
                    na_spinbox.setValue(float(cur_na))
                    na_spinbox.setFixedWidth(54)
                    na_spinbox.setToolTip(
                        "LED array NA (bf/df/dpc radius). Re-fires the current "
                        "pattern when changed while the channel is on."
                    )
                    name_row.addWidget(na_spinbox)
                    cfg_btn = QToolButton()
                    cfg_btn.setText("⚙")  # gear
                    cfg_btn.setFixedWidth(24)
                    cfg_btn.setToolTip(
                        "LED matrix configuration: RGB color, inner/outer annulus "
                        "NA, and objective-NA references (1.25x / 0.75x)."
                    )
                    name_row.addWidget(cfg_btn)
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
            if na_spinbox is not None:
                row_data["na_spinbox"] = na_spinbox
            if cfg_btn is not None:
                row_data["cfg_btn"] = cfg_btn
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
            if na_spinbox is not None:
                na_spinbox.valueChanged.connect(
                    lambda v, n=name: self._on_led_matrix_na_changed(n, v)
                )
            if cfg_btn is not None:
                cfg_btn.clicked.connect(
                    lambda _=False, n=name: self._on_led_matrix_config(n)
                )

        root.addWidget(channels_group)
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
        if self._obs_controller is not None:
            self._obs_controller.set_illumination_intensity(channel_name, float(value))
        else:
            self._controller.set_channel_intensity(channel_name, float(value))

    def _on_spinbox_changed(self, channel_name: str, value: float):
        row = self._channel_rows.get(channel_name)
        if row is None:
            return
        slider: QSlider = row["slider"]
        slider.blockSignals(True)
        slider.setValue(int(round(value)))
        slider.blockSignals(False)
        if self._obs_controller is not None:
            self._obs_controller.set_illumination_intensity(channel_name, value)
        else:
            self._controller.set_channel_intensity(channel_name, value)

    def _on_shutter_toggled(self, channel_name: str, checked: bool):
        row = self._channel_rows.get(channel_name)
        if row is None:
            return
        btn: QPushButton = row["btn"]
        btn.setText("ON" if checked else "OFF")
        self._apply_shutter_style(btn, checked)
        if self._obs_controller is not None:
            self._obs_controller.set_illumination_on_off(channel_name, checked)
        else:
            self._controller.set_channel_state(channel_name, checked)

    def _on_led_matrix_mode_changed(self, channel_name: str, index: int):
        row = self._channel_rows.get(channel_name)
        if row is None:
            return
        combo = row.get("mode_combo")
        if combo is None:
            return
        mode_key = combo.itemData(index)
        if mode_key is not None:
            if self._obs_controller is not None:
                self._obs_controller.set_led_matrix_mode(mode_key)
            elif getattr(self._controller, "set_led_matrix_mode", None):
                self._controller.set_led_matrix_mode(mode_key)
            self._update_inline_na_enabled(row, mode_key)

    def _update_inline_na_enabled(self, row: dict, mode_key) -> None:
        """The inline scalar-NA box edits the active mode's NA, so enable it only
        for scalar modes (BF/DF/DPC/low-NA). Annulus and single-LED modes use the
        ⚙ config popup (inner/outer NA, LED index)."""
        na_sb = row.get("na_spinbox")
        if na_sb is None:
            return
        uses_na = False
        try:
            if mode_key and hasattr(self._controller, "led_matrix_mode_uses_array_na"):
                uses_na = bool(self._controller.led_matrix_mode_uses_array_na(mode_key))
        except Exception:
            uses_na = False
        na_sb.setEnabled(uses_na)
        na_sb.setToolTip(
            "NA for this pattern (BF/DF/DPC/low-NA). Re-fires while on."
            if uses_na
            else "This pattern's NA is set in the ⚙ config popup (annulus inner/outer, or single-LED index)."
        )

    def _on_led_matrix_na_changed(self, channel_name: str, value: float):
        """Push LED matrix array-NA change to controller (re-fires pattern if on)."""
        row = self._channel_rows.get(channel_name)
        if row is None:
            return
        try:
            if self._obs_controller is not None and hasattr(
                self._obs_controller, "set_led_matrix_array_na"
            ):
                self._obs_controller.set_led_matrix_array_na(float(value))
            elif hasattr(self._controller, "set_led_matrix_array_na"):
                self._controller.set_led_matrix_array_na(float(value))
        except Exception:
            pass

    def _on_led_matrix_config(self, channel_name: str):
        """Open the LED matrix configuration popup (RGB color, inner/outer annulus
        NA, scalar NA, and objective-NA references). Changes apply live."""
        objective_store = self._objective_store
        if objective_store is None and self._obs_controller is not None:
            objective_store = getattr(
                getattr(self._obs_controller, "microscope", None), "objective_store", None
            )
        dlg = LEDMatrixConfigDialog(
            self._obs_controller, self._controller, objective_store, parent=self
        )
        dlg.exec_()
        # Reflect any scalar-NA change made inside the popup on the inline spinbox.
        self._refresh_from_state()

    def update_ui_for_mode(self, config=None) -> None:
        """Refresh illumination controls to match current hardware state.

        Called during multipoint acquisition when the active channel changes,
        so the illumination panel reflects the intensity/on-off state the
        acquisition code has applied to the hardware.
        """
        self._refresh_from_state()

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

            na_sb = row.get("na_spinbox")
            if na_sb is not None and getattr(self._controller, "get_led_matrix_array_na", None):
                na_val = self._controller.get_led_matrix_array_na()
                if na_val is not None:
                    na_sb.blockSignals(True)
                    na_sb.setValue(float(na_val))
                    na_sb.blockSignals(False)
                if getattr(self._controller, "get_led_matrix_mode", None):
                    self._update_inline_na_enabled(row, self._controller.get_led_matrix_mode())

            slider.blockSignals(False)
            spinbox.blockSignals(False)
            btn.blockSignals(False)


class LEDMatrixConfigDialog(QDialog):
    """Popup for SciMicroscopy LED matrix configuration — shows EVERY pattern
    variety at once.

    - Global RGB color.
    - A NA per variety: BF full, Dark field, BF low-NA each own; the four DPC
      half-circles share one NA (partners locked).
    - Inner/outer NA for the annulus family (full + half annuli, shared).
    - Single-LED index.
    - Read-only 1.25x / 0.75x objective-NA references + an annulus quick-set.

    Changes apply live and, routed through the ObservationStateController, are
    recorded on the current ObservationState so they save/restore.
    """

    # (label, group key, fallback display value)
    _GROUPS = (
        ("BF full", "bf", 0.8),
        ("Dark field", "df", 0.85),
        ("BF low-NA", "low_na", 0.2),
        ("DPC half-circles", "dpc", 0.4),
    )

    def __init__(self, obs_controller, controller, objective_store, parent=None):
        super().__init__(parent)
        self.setWindowTitle("LED matrix configuration")
        self._obs = obs_controller
        self._ctrl = controller
        self._objective_store = objective_store
        self._build_ui()

    # ------------------------------------------------------------------
    def _objective_na(self) -> Optional[float]:
        store = self._objective_store
        if store is None:
            return None
        try:
            info = store.get_current_objective_info()
            return float(info["NA"]) if info and "NA" in info else None
        except Exception:
            return None

    def _get(self, getter: str, *args):
        fn = getattr(self._ctrl, getter, None)
        if fn is None:
            return None
        try:
            return fn(*args)
        except Exception:
            return None

    @staticmethod
    def _mk_na_spin(value: float) -> QDoubleSpinBox:
        sb = QDoubleSpinBox()
        sb.setRange(0.0, 1.0)
        sb.setSingleStep(0.05)
        sb.setDecimals(2)
        sb.setValue(max(0.0, min(1.0, float(value))))
        sb.setFixedWidth(70)
        return sb

    # ------------------------------------------------------------------
    def _build_ui(self):
        form = QFormLayout(self)
        obj_na = self._objective_na()

        # Global RGB color (0-255). Connect AFTER setValue to avoid an init push.
        cur_rgb = self._get("get_led_matrix_color")
        rgb255 = tuple(int(round(max(0.0, min(1.0, c)) * 255)) for c in (cur_rgb or (1.0, 1.0, 1.0)))
        color_row = QHBoxLayout()
        self._r_sb = QSpinBox()
        self._g_sb = QSpinBox()
        self._b_sb = QSpinBox()
        for sb, val, lbl in ((self._r_sb, rgb255[0], "R"), (self._g_sb, rgb255[1], "G"), (self._b_sb, rgb255[2], "B")):
            sb.setRange(0, 255)
            sb.setValue(int(val))
            sb.setFixedWidth(58)
            color_row.addWidget(QLabel(lbl))
            color_row.addWidget(sb)
        color_row.addStretch()
        color_cell = QWidget()
        color_cell.setLayout(color_row)
        form.addRow("Color (RGB):", color_cell)
        for sb in (self._r_sb, self._g_sb, self._b_sb):
            sb.valueChanged.connect(self._on_color_changed)

        if obj_na is not None:
            form.addRow(
                "Objective NA:",
                QLabel(f"{obj_na:.2f}    1.25x = {1.25 * obj_na:.2f}    0.75x = {0.75 * obj_na:.2f}"),
            )

        # Per-variety scalar NA (each group independent; DPC half-circles share one).
        for label, group, default in self._GROUPS:
            cur = self._get("get_led_matrix_group_na", group)
            sb = self._mk_na_spin(cur if cur is not None else default)
            sb.valueChanged.connect(lambda v, g=group: self._push("set_led_matrix_group_na", g, float(v)))
            form.addRow(f"{label} NA:", sb)

        # Annulus family (full annulus + half annuli) shares inner/outer NA.
        ann = self._get("get_led_matrix_annulus_na") or (0.5, 0.95)
        self._inner_sb = self._mk_na_spin(ann[0])
        self._outer_sb = self._mk_na_spin(ann[1])
        form.addRow("Annulus inner NA:", self._inner_sb)
        form.addRow("Annulus outer NA:", self._outer_sb)
        self._inner_sb.valueChanged.connect(self._on_annulus_changed)
        self._outer_sb.valueChanged.connect(self._on_annulus_changed)
        if obj_na is not None:
            btn = QPushButton("Set annulus to 0.75x – 1.25x objective NA")
            btn.setAutoDefault(False)
            btn.setDefault(False)
            btn.clicked.connect(lambda _=False, na=obj_na: self._set_annulus_to_objective(na))
            form.addRow("", btn)

        # Single-LED index.
        cur_idx = self._get("get_led_matrix_single_led_index")
        self._sli_sb = QSpinBox()
        self._sli_sb.setRange(0, 4095)
        self._sli_sb.setValue(int(cur_idx) if cur_idx is not None else 0)
        self._sli_sb.setFixedWidth(80)
        form.addRow("Single LED index:", self._sli_sb)
        self._sli_sb.valueChanged.connect(lambda v: self._push("set_led_matrix_single_led_index", int(v)))

        close_btn = QPushButton("Close")
        close_btn.setAutoDefault(False)
        close_btn.setDefault(False)
        close_btn.clicked.connect(self.accept)
        form.addRow("", close_btn)

    # ------------------------------------------------------------------
    def _push(self, method: str, *args):
        target = self._obs if (self._obs is not None and hasattr(self._obs, method)) else self._ctrl
        fn = getattr(target, method, None)
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:
            pass

    def _on_color_changed(self, _=None):
        r, g, b = self._r_sb.value(), self._g_sb.value(), self._b_sb.value()
        hexv = "#{:02X}{:02X}{:02X}".format(r, g, b)
        # obs_controller takes hex (records per state); raw controller takes 0-1 RGB.
        if self._obs is not None and hasattr(self._obs, "set_led_matrix_color"):
            try:
                self._obs.set_led_matrix_color(hexv)
                return
            except Exception:
                pass
        if hasattr(self._ctrl, "set_led_matrix_color"):
            try:
                self._ctrl.set_led_matrix_color((r / 255.0, g / 255.0, b / 255.0))
            except Exception:
                pass

    def _on_annulus_changed(self, _=None):
        self._push(
            "set_led_matrix_annulus_na",
            float(self._inner_sb.value()),
            float(self._outer_sb.value()),
        )

    def _set_annulus_to_objective(self, na: float):
        # Set both spinboxes, then push once (avoid a double re-fire from each
        # valueChanged firing _on_annulus_changed separately).
        self._inner_sb.blockSignals(True)
        self._outer_sb.blockSignals(True)
        self._inner_sb.setValue(min(0.99, 0.75 * na))
        self._outer_sb.setValue(min(0.99, 1.25 * na))
        self._inner_sb.blockSignals(False)
        self._outer_sb.blockSignals(False)
        self._on_annulus_changed()
