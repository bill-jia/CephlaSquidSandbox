from ._bootstrap import *

class LaserAutofocusSettingWidget(QWidget):

    signal_newExposureTime = Signal(float)
    signal_newAnalogGain = Signal(float)
    signal_apply_settings = Signal()
    signal_laser_spot_location = Signal(np.ndarray, float, float)

    def __init__(self, streamHandler, liveController: LiveController, laserAutofocusController, stretch=True):
        super().__init__()
        self.streamHandler = streamHandler
        self.liveController: LiveController = liveController
        self.laserAutofocusController = laserAutofocusController
        self.stretch = stretch
        self.liveController.set_trigger_fps(10)
        self.streamHandler.set_display_fps(10)

        # Enable background filling
        self.setAutoFillBackground(True)

        # Create and set background color
        palette = self.palette()
        palette.setColor(self.backgroundRole(), QColor(240, 240, 240))
        self.setPalette(palette)

        self.spinboxes = {}
        self.init_ui()
        self.update_calibration_label()

    def init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(9, 9, 9, 9)

        # Live control group
        live_group = QFrame()
        live_group.setFrameStyle(QFrame.Panel | QFrame.Raised)
        live_layout = QVBoxLayout()

        # Live button
        self.btn_live = QPushButton("Start Live")
        self.btn_live.setCheckable(True)
        self.btn_live.setStyleSheet("background-color: #C2C2FF")

        # Exposure time control
        exposure_layout = QHBoxLayout()
        exposure_layout.addWidget(QLabel("Focus Camera Exposure (ms):"))
        self.exposure_spinbox = QDoubleSpinBox()
        self.exposure_spinbox.setKeyboardTracking(False)
        self.exposure_spinbox.setSingleStep(0.1)
        try:
            exposure_min_ms, exposure_max_ms = self.laserAutofocusController.camera.get_exposure_limits()
        except Exception:
            exposure_min_ms, exposure_max_ms = 0.01, 10000.0
        self.exposure_spinbox.setRange(exposure_min_ms, exposure_max_ms)
        self.exposure_spinbox.setValue(self.laserAutofocusController.laser_af_properties.focus_camera_exposure_time_ms)
        exposure_layout.addWidget(self.exposure_spinbox)

        # Analog gain control
        analog_gain_layout = QHBoxLayout()
        analog_gain_layout.addWidget(QLabel("Focus Camera Analog Gain:"))
        self.analog_gain_spinbox = QDoubleSpinBox()
        self.analog_gain_spinbox.setKeyboardTracking(False)
        self.analog_gain_spinbox.setRange(0, 24)
        self.analog_gain_spinbox.setValue(self.laserAutofocusController.laser_af_properties.focus_camera_analog_gain)
        analog_gain_layout.addWidget(self.analog_gain_spinbox)

        # Add to live group
        live_layout.addWidget(self.btn_live)
        live_layout.addLayout(exposure_layout)
        live_layout.addLayout(analog_gain_layout)
        live_group.setLayout(live_layout)

        # Non-threshold property group
        non_threshold_group = QFrame()
        non_threshold_group.setFrameStyle(QFrame.Panel | QFrame.Raised)
        non_threshold_layout = QVBoxLayout()

        # Add non-threshold property spinboxes
        self._add_spinbox(non_threshold_layout, "Spot Crop Size (pixels):", "spot_crop_size", 1, 500, 0)
        self._add_spinbox(
            non_threshold_layout, "Calibration Distance (μm):", "pixel_to_um_calibration_distance", 0.1, 20.0, 2
        )
        non_threshold_group.setLayout(non_threshold_layout)

        # Settings group
        settings_group = QFrame()
        settings_group.setFrameStyle(QFrame.Panel | QFrame.Raised)
        settings_layout = QVBoxLayout()

        # Add threshold property spinboxes
        self._add_spinbox(settings_layout, "Laser AF Averaging N:", "laser_af_averaging_n", 1, 100, 0)
        self._add_spinbox(
            settings_layout, "Displacement Success Window (μm):", "displacement_success_window_um", 0.1, 10.0, 2
        )
        self._add_spinbox(settings_layout, "Correlation Threshold:", "correlation_threshold", 0.1, 1.0, 2, 0.1)
        self._add_spinbox(settings_layout, "Laser AF Range (μm):", "laser_af_range", 1, 1000, 1)
        self.update_threshold_button = QPushButton("Apply without Re-initialization")
        settings_layout.addWidget(self.update_threshold_button)
        settings_group.setLayout(settings_layout)

        # Create spot detection group
        spot_detection_group = QFrame()
        spot_detection_group.setFrameStyle(QFrame.Panel | QFrame.Raised)
        spot_detection_layout = QVBoxLayout()

        # Add spot detection related spinboxes
        self._add_spinbox(spot_detection_layout, "Y Window (pixels):", "y_window", 1, 500, 0)
        self._add_spinbox(spot_detection_layout, "X Window (pixels):", "x_window", 1, 500, 0)
        self._add_spinbox(spot_detection_layout, "Min Peak Width:", "min_peak_width", 1, 100, 1)
        self._add_spinbox(spot_detection_layout, "Min Peak Distance:", "min_peak_distance", 1, 100, 1)
        self._add_spinbox(spot_detection_layout, "Min Peak Prominence:", "min_peak_prominence", 0.01, 1.0, 2, 0.1)
        self._add_spinbox(spot_detection_layout, "Spot Spacing (pixels):", "spot_spacing", 1, 1000, 1)
        self._add_spinbox(spot_detection_layout, "Filter Sigma:", "filter_sigma", 0, 100, 1, allow_none=True)

        # Spot detection mode combo box
        spot_mode_layout = QHBoxLayout()
        spot_mode_layout.addWidget(QLabel("Spot Detection Mode:"))
        self.spot_mode_combo = QComboBox()
        for mode in SpotDetectionMode:
            self.spot_mode_combo.addItem(mode.value, mode)
        current_index = self.spot_mode_combo.findData(
            self.laserAutofocusController.laser_af_properties.spot_detection_mode
        )
        self.spot_mode_combo.setCurrentIndex(current_index)
        spot_mode_layout.addWidget(self.spot_mode_combo)
        spot_detection_layout.addLayout(spot_mode_layout)

        # Add Run Spot Detection button
        self.run_spot_detection_button = QPushButton("Run Spot Detection")
        self.run_spot_detection_button.setEnabled(False)  # Disabled by default
        spot_detection_layout.addWidget(self.run_spot_detection_button)
        spot_detection_group.setLayout(spot_detection_layout)

        # Initialize button
        initialize_group = QFrame()
        initialize_layout = QVBoxLayout()
        self.initialize_button = QPushButton("Initialize")
        self.initialize_button.setStyleSheet("background-color: #C2C2FF")
        initialize_layout.addWidget(self.initialize_button)
        initialize_group.setLayout(initialize_layout)

        # Add Laser AF Characterization Mode checkbox
        characterization_group = QFrame()
        characterization_layout = QHBoxLayout()
        self.characterization_checkbox = QCheckBox("Laser AF Characterization Mode")
        self.characterization_checkbox.setChecked(self.laserAutofocusController.characterization_mode)
        characterization_layout.addWidget(self.characterization_checkbox)
        characterization_group.setLayout(characterization_layout)

        # Add to main layout
        layout.addWidget(live_group)
        layout.addWidget(non_threshold_group)
        layout.addWidget(settings_group)
        layout.addWidget(spot_detection_group)
        layout.addWidget(initialize_group)
        layout.addWidget(characterization_group)
        self.setLayout(layout)

        if not self.stretch:
            layout.addStretch()

        # Connect all signals to slots
        self.btn_live.clicked.connect(self.toggle_live)
        self.exposure_spinbox.valueChanged.connect(self.update_exposure_time)
        self.analog_gain_spinbox.valueChanged.connect(self.update_analog_gain)
        self.update_threshold_button.clicked.connect(self.update_threshold_settings)
        self.run_spot_detection_button.clicked.connect(self.run_spot_detection)
        self.initialize_button.clicked.connect(self.apply_and_initialize)
        self.characterization_checkbox.toggled.connect(self.toggle_characterization_mode)

    def _add_spinbox(
        self,
        layout,
        label: str,
        property_name: str,
        min_val: float,
        max_val: float,
        decimals: int,
        step: float = 1,
        allow_none=False,
    ) -> None:
        """Helper method to add a labeled spinbox to the layout."""
        box_layout = QHBoxLayout()
        box_layout.addWidget(QLabel(label))

        spinbox = QDoubleSpinBox()
        spinbox.setKeyboardTracking(False)
        if allow_none:
            spinbox.setRange(min_val - step, max_val)
            spinbox.setSpecialValueText("None")
        else:
            spinbox.setRange(min_val, max_val)
        spinbox.setDecimals(decimals)
        spinbox.setSingleStep(step)
        # Get initial value from laser_af_properties
        current_value = getattr(self.laserAutofocusController.laser_af_properties, property_name)
        if allow_none and current_value is None:
            spinbox.setValue(min_val - step)
        else:
            spinbox.setValue(current_value)

        box_layout.addWidget(spinbox)
        layout.addLayout(box_layout)

        # Store spinbox reference
        self.spinboxes[property_name] = spinbox

    def toggle_live(self, pressed):
        if pressed:
            self.liveController.start_live()
            self.btn_live.setText("Stop Live")
            self.run_spot_detection_button.setEnabled(False)
        else:
            self.liveController.stop_live()
            self.btn_live.setText("Start Live")
            self.run_spot_detection_button.setEnabled(True)

    def stop_live(self):
        """Used for stopping live when switching to other tabs"""
        self.toggle_live(False)
        self.btn_live.setChecked(False)

    def toggle_characterization_mode(self, state):
        self.laserAutofocusController.characterization_mode = state

    def update_exposure_time(self, value):
        self.signal_newExposureTime.emit(value)

    def update_analog_gain(self, value):
        self.signal_newAnalogGain.emit(value)

    def update_values(self):
        """Update all widget values from the controller properties"""
        self.clear_labels()

        # Update spinboxes
        for prop_name, spinbox in self.spinboxes.items():
            current_value = getattr(self.laserAutofocusController.laser_af_properties, prop_name)
            if current_value is None:
                # For spinboxes that allow None, set to minimum (shows "None" special text)
                spinbox.setValue(spinbox.minimum())
            else:
                spinbox.setValue(current_value)

        # Update exposure and gain
        self.exposure_spinbox.setValue(self.laserAutofocusController.laser_af_properties.focus_camera_exposure_time_ms)
        self.analog_gain_spinbox.setValue(self.laserAutofocusController.laser_af_properties.focus_camera_analog_gain)

        # Update spot detection mode
        current_mode = self.laserAutofocusController.laser_af_properties.spot_detection_mode
        index = self.spot_mode_combo.findData(current_mode)
        if index >= 0:
            self.spot_mode_combo.setCurrentIndex(index)

        self.update_threshold_button.setEnabled(self.laserAutofocusController.is_initialized)
        self.update_calibration_label()

    def apply_and_initialize(self):
        self.clear_labels()

        updates = {
            "laser_af_averaging_n": int(self.spinboxes["laser_af_averaging_n"].value()),
            "displacement_success_window_um": self.spinboxes["displacement_success_window_um"].value(),
            "spot_crop_size": int(self.spinboxes["spot_crop_size"].value()),
            "correlation_threshold": self.spinboxes["correlation_threshold"].value(),
            "pixel_to_um_calibration_distance": self.spinboxes["pixel_to_um_calibration_distance"].value(),
            "laser_af_range": self.spinboxes["laser_af_range"].value(),
            "spot_detection_mode": self.spot_mode_combo.currentData(),
            "y_window": int(self.spinboxes["y_window"].value()),
            "x_window": int(self.spinboxes["x_window"].value()),
            "min_peak_width": self.spinboxes["min_peak_width"].value(),
            "min_peak_distance": self.spinboxes["min_peak_distance"].value(),
            "min_peak_prominence": self.spinboxes["min_peak_prominence"].value(),
            "spot_spacing": self.spinboxes["spot_spacing"].value(),
            "filter_sigma": self.spinboxes["filter_sigma"].value(),
            "focus_camera_exposure_time_ms": self.exposure_spinbox.value(),
            "focus_camera_analog_gain": self.analog_gain_spinbox.value(),
            "has_reference": False,
        }
        self.laserAutofocusController.set_laser_af_properties(updates)
        self.laserAutofocusController.initialize_auto()
        self.signal_apply_settings.emit()
        self.update_threshold_button.setEnabled(True)
        self.update_calibration_label()

    def update_threshold_settings(self):
        updates = {
            "laser_af_averaging_n": int(self.spinboxes["laser_af_averaging_n"].value()),
            "displacement_success_window_um": self.spinboxes["displacement_success_window_um"].value(),
            "correlation_threshold": self.spinboxes["correlation_threshold"].value(),
            "laser_af_range": self.spinboxes["laser_af_range"].value(),
        }
        self.laserAutofocusController.update_threshold_properties(updates)

    def update_calibration_label(self):
        # Show calibration result
        # Clear previous calibration label if it exists
        if hasattr(self, "calibration_label"):
            self.calibration_label.deleteLater()

        # Create and add new calibration label
        self.calibration_label = QLabel()
        self.calibration_label.setText(
            f"Calibration Result: {self.laserAutofocusController.laser_af_properties.pixel_to_um:.3f} um/pixel\nPerformed at {self.laserAutofocusController.laser_af_properties.calibration_timestamp}"
        )
        self.layout().addWidget(self.calibration_label)

    def illuminate_and_get_frame(self):
        # Get a frame from the live controller.  We need to reach deep into the liveController here which
        # is not ideal.
        self.liveController.microscope.low_level_drivers.microcontroller.turn_on_AF_laser()
        self.liveController.microscope.low_level_drivers.microcontroller.wait_till_operation_is_completed()
        self.liveController.trigger_acquisition()

        try:
            frame = self.liveController.camera.read_frame()
        finally:
            self.liveController.microscope.low_level_drivers.microcontroller.turn_off_AF_laser()
            self.liveController.microscope.low_level_drivers.microcontroller.wait_till_operation_is_completed()

        return frame

    def clear_labels(self):
        # Remove any existing error or correlation labels
        if hasattr(self, "spot_detection_error_label"):
            self.spot_detection_error_label.deleteLater()
            delattr(self, "spot_detection_error_label")

        if hasattr(self, "correlation_label"):
            self.correlation_label.deleteLater()
            delattr(self, "correlation_label")

    def run_spot_detection(self):
        """Run spot detection with current settings and emit results"""
        params = {
            "y_window": int(self.spinboxes["y_window"].value()),
            "x_window": int(self.spinboxes["x_window"].value()),
            "min_peak_width": self.spinboxes["min_peak_width"].value(),
            "min_peak_distance": self.spinboxes["min_peak_distance"].value(),
            "min_peak_prominence": self.spinboxes["min_peak_prominence"].value(),
            "spot_spacing": self.spinboxes["spot_spacing"].value(),
        }
        mode = self.spot_mode_combo.currentData()
        sigma = self.spinboxes["filter_sigma"].value()

        frame = self.illuminate_and_get_frame()
        if frame is not None:
            try:
                result = utils.find_spot_location(frame, mode=mode, params=params, filter_sigma=sigma, debug_plot=True)
                if result is not None:
                    x, y = result
                    self.signal_laser_spot_location.emit(frame, x, y)
                else:
                    raise Exception("No spot detection result returned")
            except Exception:
                # Show error message
                # Clear previous error label if it exists
                if hasattr(self, "spot_detection_error_label"):
                    self.spot_detection_error_label.deleteLater()

                # Create and add new error label
                self.spot_detection_error_label = QLabel("Spot detection failed!")
                self.layout().addWidget(self.spot_detection_error_label)

    def show_cross_correlation_result(self, value):
        """Show cross-correlation value from validating laser af images"""
        # Clear previous correlation label if it exists
        if hasattr(self, "correlation_label"):
            self.correlation_label.deleteLater()

        # Create and add new correlation label
        self.correlation_label = QLabel()
        self.correlation_label.setText(f"Cross-correlation: {value:.3f}")
        self.layout().addWidget(self.correlation_label)


class SpinningDiskConfocalWidget(QWidget):

    signal_toggle_confocal_widefield = Signal(bool)
    signal_illumination_iris_changed = Signal(float)
    signal_emission_iris_changed = Signal(float)

    def __init__(self, xlight):
        super(SpinningDiskConfocalWidget, self).__init__()

        self._log = squid.logging.get_logger(self.__class__.__name__)
        self.xlight = xlight

        self.init_ui()

        if self.xlight.has_emission_filters_wheel:
            self.dropdown_emission_filter.setCurrentText(str(self.xlight.get_emission_filter()))
            self.dropdown_emission_filter.currentIndexChanged.connect(self.set_emission_filter)
        if self.xlight.has_dichroic_filters_wheel:
            self.dropdown_dichroic.setCurrentText(str(self.xlight.get_dichroic()))
            self.dropdown_dichroic.currentIndexChanged.connect(self.set_dichroic)
        if self.xlight.has_dichroic_filter_slider:
            self.filter_slider.setValue(self.xlight.get_filter_slider())

        self.disk_position_state = self.xlight.get_disk_position()

        self.signal_toggle_confocal_widefield.emit(self.disk_position_state)  # signal initial state

        if self.disk_position_state == 1:
            self.btn_toggle_widefield.setText("Switch to Widefield")

        self.btn_toggle_widefield.clicked.connect(self.toggle_disk_position)
        self.btn_toggle_motor.clicked.connect(self.toggle_motor)

        if self.xlight.has_dichroic_filter_slider:
            self.filter_slider.valueChanged.connect(self.set_filter_slider)

        if self.xlight.has_illumination_iris_diaphragm:
            # Slider values are set from acquisition config via update_iris_from_config()
            # after signal connections are established in gui.gui_hcs
            self.slider_illumination_iris.sliderReleased.connect(lambda: self.update_illumination_iris(True))
            # Update spinbox + apply on click-to-position (not during drag)
            self.slider_illumination_iris.valueChanged.connect(self._on_illumination_iris_value_changed)
            self.spinbox_illumination_iris.editingFinished.connect(lambda: self.update_illumination_iris(False))
        if self.xlight.has_emission_iris_diaphragm:
            self.slider_emission_iris.sliderReleased.connect(lambda: self.update_emission_iris(True))
            # Update spinbox + apply on click-to-position (not during drag)
            self.slider_emission_iris.valueChanged.connect(self._on_emission_iris_value_changed)
            self.spinbox_emission_iris.editingFinished.connect(lambda: self.update_emission_iris(False))

    def init_ui(self):

        # Only create widgets if hardware supports them
        self.dropdown_emission_filter = None
        if self.xlight.has_emission_filters_wheel:
            self.dropdown_emission_filter = QComboBox(self)
            self.dropdown_emission_filter.addItems([str(i + 1) for i in range(XLIGHT_EMISSION_FILTER_POSITIONS)])

        self.dropdown_dichroic = None
        if self.xlight.has_dichroic_filters_wheel:
            self.dropdown_dichroic = QComboBox(self)
            self.dropdown_dichroic.addItems([str(i + 1) for i in range(5)])

        illuminationIrisLayout = QHBoxLayout()
        illuminationIrisLayout.addWidget(QLabel("Illumination Iris"))
        self.slider_illumination_iris = QSlider(Qt.Horizontal)
        self.slider_illumination_iris.setRange(0, 100)
        self.spinbox_illumination_iris = QSpinBox()
        self.spinbox_illumination_iris.setRange(0, 100)
        self.spinbox_illumination_iris.setKeyboardTracking(False)
        illuminationIrisLayout.addWidget(self.slider_illumination_iris)
        illuminationIrisLayout.addWidget(self.spinbox_illumination_iris)

        emissionIrisLayout = QHBoxLayout()
        emissionIrisLayout.addWidget(QLabel("Emission Iris"))
        self.slider_emission_iris = QSlider(Qt.Horizontal)
        self.slider_emission_iris.setRange(0, 100)
        self.spinbox_emission_iris = QSpinBox()
        self.spinbox_emission_iris.setRange(0, 100)
        self.spinbox_emission_iris.setKeyboardTracking(False)
        emissionIrisLayout.addWidget(self.slider_emission_iris)
        emissionIrisLayout.addWidget(self.spinbox_emission_iris)

        filterSliderLayout = QHBoxLayout()
        filterSliderLayout.addWidget(QLabel("Filter Slider"))
        # self.filter_slider = QComboBox(self)
        # self.filter_slider.addItems(["0", "1", "2", "3"])
        self.filter_slider = QSlider(Qt.Horizontal)
        self.filter_slider.setRange(0, 3)
        self.filter_slider.setTickPosition(QSlider.TicksBelow)
        self.filter_slider.setTickInterval(1)
        filterSliderLayout.addWidget(self.filter_slider)

        self.btn_toggle_widefield = QPushButton("Switch to Confocal")

        self.btn_toggle_motor = QPushButton("Disk Motor On")
        self.btn_toggle_motor.setCheckable(True)

        layout = QGridLayout(self)

        # row 1
        if self.xlight.has_dichroic_filter_slider:
            layout.addLayout(filterSliderLayout, 0, 0, 1, 2)
        layout.addWidget(self.btn_toggle_motor, 0, 2)
        layout.addWidget(self.btn_toggle_widefield, 0, 3)

        # row 2
        if self.xlight.has_dichroic_filters_wheel:
            layout.addWidget(QLabel("Dichroic Filter Wheel"), 1, 0)
            layout.addWidget(self.dropdown_dichroic, 1, 1)
        if self.xlight.has_illumination_iris_diaphragm:
            layout.addLayout(illuminationIrisLayout, 1, 2, 1, 2)

        # row 3
        if self.xlight.has_emission_filters_wheel:
            layout.addWidget(QLabel("Emission Filter Wheel"), 2, 0)
            layout.addWidget(self.dropdown_emission_filter, 2, 1)
        if self.xlight.has_emission_iris_diaphragm:
            layout.addLayout(emissionIrisLayout, 2, 2, 1, 2)

        layout.setColumnStretch(2, 1)
        layout.setColumnStretch(3, 1)
        self.setLayout(layout)

    @Slot(bool)
    def enable_all_buttons(self, enable: bool):
        if self.dropdown_emission_filter:
            self.dropdown_emission_filter.setEnabled(enable)
        if self.dropdown_dichroic:
            self.dropdown_dichroic.setEnabled(enable)
        self.btn_toggle_widefield.setEnabled(enable)
        self.btn_toggle_motor.setEnabled(enable)
        self.slider_illumination_iris.setEnabled(enable)
        self.spinbox_illumination_iris.setEnabled(enable)
        self.slider_emission_iris.setEnabled(enable)
        self.spinbox_emission_iris.setEnabled(enable)
        if self.xlight.has_dichroic_filter_slider:
            self.filter_slider.setEnabled(enable)

    def block_iris_control_signals(self, block: bool):
        self.slider_illumination_iris.blockSignals(block)
        self.spinbox_illumination_iris.blockSignals(block)
        self.slider_emission_iris.blockSignals(block)
        self.spinbox_emission_iris.blockSignals(block)

    def toggle_disk_position(self):
        self.enable_all_buttons(False)
        target_position = 0 if self.disk_position_state == 1 else 1

        def on_finished(success, error_msg):
            QMetaObject.invokeMethod(
                self, "_on_disk_position_toggled", Qt.QueuedConnection, Q_ARG(int, target_position)
            )

        utils.threaded_operation_helper(self.xlight.set_disk_position, on_finished, position=target_position)

    @Slot(int)
    def _on_disk_position_toggled(self, position):
        self.disk_position_state = position
        if position == 1:
            self.btn_toggle_widefield.setText("Switch to Widefield")
        else:
            self.btn_toggle_widefield.setText("Switch to Confocal")
        self.enable_all_buttons(True)
        self.signal_toggle_confocal_widefield.emit(self.disk_position_state)

    def toggle_motor(self):
        self.enable_all_buttons(False)
        state = self.btn_toggle_motor.isChecked()

        def on_finished(success, error_msg):
            QMetaObject.invokeMethod(self, "enable_all_buttons", Qt.QueuedConnection, Q_ARG(bool, True))

        utils.threaded_operation_helper(self.xlight.set_disk_motor_state, on_finished, state=state)

    def set_emission_filter(self, index):
        self.enable_all_buttons(False)
        selected_pos = self.dropdown_emission_filter.currentText()
        self.xlight.set_emission_filter(selected_pos)
        self.enable_all_buttons(True)

    def set_dichroic(self, index):
        self.enable_all_buttons(False)
        selected_pos = self.dropdown_dichroic.currentText()
        self.xlight.set_dichroic(selected_pos)
        self.enable_all_buttons(True)

    def _set_iris_ui(self, slider, spinbox, value):
        """Set an iris slider+spinbox pair to the given value."""
        slider.setValue(value)
        spinbox.setValue(value)

    def update_iris_from_config(self, configuration):
        """Update iris UI controls from a channel's confocal_hardware_settings."""
        hw_settings = getattr(configuration, "confocal_hardware_settings", None)
        self.block_iris_control_signals(True)
        try:
            for has_iris, iris_val, slider, spinbox in (
                (
                    self.xlight.has_illumination_iris_diaphragm,
                    getattr(hw_settings, "illumination_iris", None) if hw_settings else None,
                    self.slider_illumination_iris,
                    self.spinbox_illumination_iris,
                ),
                (
                    self.xlight.has_emission_iris_diaphragm,
                    getattr(hw_settings, "emission_iris", None) if hw_settings else None,
                    self.slider_emission_iris,
                    self.spinbox_emission_iris,
                ),
            ):
                if not has_iris:
                    continue
                value = int(iris_val) if iris_val is not None else slider.minimum()
                self._set_iris_ui(slider, spinbox, value)
        finally:
            self.block_iris_control_signals(False)

    def _on_illumination_iris_value_changed(self, value):
        """Handle illumination iris slider valueChanged — sync spinbox, apply on click-to-position."""
        self.spinbox_illumination_iris.setValue(value)
        if not self.slider_illumination_iris.isSliderDown():
            self.update_illumination_iris(True)

    def _on_emission_iris_value_changed(self, value):
        """Handle emission iris slider valueChanged — sync spinbox, apply on click-to-position."""
        self.spinbox_emission_iris.setValue(value)
        if not self.slider_emission_iris.isSliderDown():
            self.update_emission_iris(True)

    def _update_iris_hardware(self, from_slider, slider, spinbox, hw_setter, signal):
        """Shared logic for updating an iris value from UI interaction."""
        self.block_iris_control_signals(True)
        self.enable_all_buttons(False)
        if from_slider:
            value = slider.value()
        else:
            value = spinbox.value()
            slider.setValue(value)
        hw_setter(value)
        signal.emit(float(value))
        self.enable_all_buttons(True)
        self.block_iris_control_signals(False)

    def update_illumination_iris(self, from_slider: bool):
        self._update_iris_hardware(
            from_slider,
            self.slider_illumination_iris,
            self.spinbox_illumination_iris,
            self.xlight.set_illumination_iris,
            self.signal_illumination_iris_changed,
        )

    def update_emission_iris(self, from_slider: bool):
        self._update_iris_hardware(
            from_slider,
            self.slider_emission_iris,
            self.spinbox_emission_iris,
            self.xlight.set_emission_iris,
            self.signal_emission_iris_changed,
        )

    def set_filter_slider(self, index):
        self.enable_all_buttons(False)
        position = str(self.filter_slider.value())

        def on_finished(success, error_msg):
            QMetaObject.invokeMethod(self, "enable_all_buttons", Qt.QueuedConnection, Q_ARG(bool, True))

        utils.threaded_operation_helper(self.xlight.set_filter_slider, on_finished, position=position)

    def get_confocal_mode(self) -> bool:
        """Get current confocal mode state.

        Returns:
            True if in confocal mode, False if in widefield mode.
        """
        return bool(self.disk_position_state)


class DragonflyConfocalWidget(QWidget):

    signal_toggle_confocal_widefield = Signal(bool)

    def __init__(self, dragonfly):
        super(DragonflyConfocalWidget, self).__init__()

        self.dragonfly = dragonfly

        self.init_ui()

        # Initialize current states from hardware
        try:
            current_modality = self.dragonfly.get_modality()
            self.confocal_mode = current_modality == "CONFOCAL" if current_modality else False

            current_dichroic = self.dragonfly.get_port_selection_dichroic()
            if current_dichroic is not None:
                self.dropdown_dichroic.setCurrentText(str(current_dichroic))

            current_port1_filter = self.dragonfly.get_emission_filter(1)
            if current_port1_filter is not None:
                self.dropdown_port1_emission_filter.setCurrentText(str(current_port1_filter))

            current_port2_filter = self.dragonfly.get_emission_filter(2)
            if current_port2_filter is not None:
                self.dropdown_port2_emission_filter.setCurrentText(str(current_port2_filter))

            current_field_aperture = self.dragonfly.get_field_aperture_wheel_position()
            if current_field_aperture is not None:
                self.dropdown_field_aperture.setCurrentText(str(current_field_aperture))

            motor_state = self.dragonfly.get_disk_motor_state()
            if motor_state is not None:
                self.btn_disk_motor.setChecked(motor_state)

        except Exception as e:
            print(f"Error initializing widget state: {e}")

        # Set initial button text
        if self.confocal_mode:
            self.btn_toggle_confocal.setText("Switch to Widefield")
        else:
            self.btn_toggle_confocal.setText("Switch to Confocal")

        # Connect signals
        self.btn_toggle_confocal.clicked.connect(self.toggle_confocal_mode)
        self.btn_disk_motor.clicked.connect(self.toggle_disk_motor)
        self.dropdown_dichroic.currentIndexChanged.connect(self.set_dichroic)
        self.dropdown_port1_emission_filter.currentIndexChanged.connect(self.set_port1_emission_filter)
        self.dropdown_port2_emission_filter.currentIndexChanged.connect(self.set_port2_emission_filter)
        self.dropdown_field_aperture.currentIndexChanged.connect(self.set_field_aperture)

        # Emit initial state
        self.signal_toggle_confocal_widefield.emit(self.confocal_mode)

    def init_ui(self):
        main_layout = QVBoxLayout()

        layout_confocal = QHBoxLayout()
        # Row 1: Switch to Confocal button, Disk Motor button, Dichroic dropdown
        self.btn_toggle_confocal = QPushButton("Switch to Confocal")
        self.btn_disk_motor = QPushButton("Disk Motor On")
        self.btn_disk_motor.setCheckable(True)

        dichroic_label = QLabel("Port Selection")
        dichroic_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.dropdown_dichroic = QComboBox(self)
        self.dropdown_dichroic.addItems(self.dragonfly.get_port_selection_dichroic_info())

        layout_confocal.addWidget(self.btn_toggle_confocal)
        layout_confocal.addWidget(self.btn_disk_motor)
        layout_confocal.addWidget(dichroic_label)
        layout_confocal.addWidget(self.dropdown_dichroic)

        layout_wheels = QGridLayout()
        # Row 2: Camera Port 1 Emission Filter and Field Aperture
        port1_emission_label = QLabel("Port 1 Emission Filter")
        self.dropdown_port1_emission_filter = QComboBox(self)
        self.dropdown_port1_emission_filter.addItems(self.dragonfly.get_emission_filter_info(1))

        port1_aperture_label = QLabel("Field Aperture")
        self.dropdown_field_aperture = QComboBox(self)
        self.dropdown_field_aperture.addItems(self.dragonfly.get_field_aperture_info())

        layout_wheels.addWidget(port1_emission_label, 0, 0)
        layout_wheels.addWidget(self.dropdown_port1_emission_filter, 0, 1)
        layout_wheels.addWidget(port1_aperture_label, 0, 2)
        layout_wheels.addWidget(self.dropdown_field_aperture, 0, 3)

        # Row 3: Camera Port 2 Emission Filter and Field Aperture
        port2_emission_label = QLabel("Port 2 Emission Filter")
        self.dropdown_port2_emission_filter = QComboBox(self)
        self.dropdown_port2_emission_filter.addItems(self.dragonfly.get_emission_filter_info(2))

        layout_wheels.addWidget(port2_emission_label, 1, 0)
        layout_wheels.addWidget(self.dropdown_port2_emission_filter, 1, 1)

        main_layout.addLayout(layout_confocal)
        main_layout.addLayout(layout_wheels)

        self.setLayout(main_layout)

    def enable_all_buttons(self, enable: bool):
        """Enable or disable all controls"""
        self.btn_toggle_confocal.setEnabled(enable)
        self.btn_disk_motor.setEnabled(enable)
        self.dropdown_dichroic.setEnabled(enable)
        self.dropdown_port1_emission_filter.setEnabled(enable)
        self.dropdown_port2_emission_filter.setEnabled(enable)
        self.dropdown_field_aperture.setEnabled(enable)

    def toggle_confocal_mode(self):
        """Toggle between confocal and widefield modes"""
        self.enable_all_buttons(False)
        try:
            if self.confocal_mode:
                # Switch to widefield
                self.dragonfly.set_modality("BF")  # or whatever widefield mode string is
                self.confocal_mode = False
                self.btn_toggle_confocal.setText("Switch to Confocal")
            else:
                # Switch to confocal
                self.dragonfly.set_modality("CONFOCAL")
                self.confocal_mode = True
                self.btn_toggle_confocal.setText("Switch to Widefield")

            self.signal_toggle_confocal_widefield.emit(self.confocal_mode)
        except Exception as e:
            print(f"Error toggling confocal mode: {e}")
        finally:
            self.enable_all_buttons(True)

    def toggle_disk_motor(self):
        """Toggle disk motor on/off"""
        self.enable_all_buttons(False)
        try:
            if self.btn_disk_motor.isChecked():
                self.dragonfly.set_disk_motor_state(True)
            else:
                self.dragonfly.set_disk_motor_state(False)
        except Exception as e:
            print(f"Error toggling disk motor: {e}")
        finally:
            self.enable_all_buttons(True)

    def set_dichroic(self, index):
        """Set dichroic position"""
        self.enable_all_buttons(False)
        try:
            selected_pos = self.dropdown_dichroic.currentIndex()
            self.dragonfly.set_port_selection_dichroic(selected_pos + 1)
        except Exception as e:
            print(f"Error setting dichroic: {e}")
        finally:
            self.enable_all_buttons(True)

    def set_port1_emission_filter(self, index):
        """Set port 1 emission filter position"""
        self.enable_all_buttons(False)
        try:
            selected_pos = self.dropdown_port1_emission_filter.currentIndex()
            self.dragonfly.set_emission_filter(1, selected_pos + 1)
        except Exception as e:
            print(f"Error setting port 1 emission filter: {e}")
        finally:
            self.enable_all_buttons(True)

    def set_port2_emission_filter(self, index):
        """Set port 2 emission filter position"""
        self.enable_all_buttons(False)
        try:
            selected_pos = self.dropdown_port2_emission_filter.currentIndex()
            self.dragonfly.set_emission_filter(2, selected_pos + 1)
        except Exception as e:
            print(f"Error setting port 2 emission filter: {e}")
        finally:
            self.enable_all_buttons(True)

    def set_field_aperture(self, index):
        """Set port 1 field aperture position"""
        self.enable_all_buttons(False)
        try:
            selected_pos = self.dropdown_field_aperture.currentIndex()
            self.dragonfly.set_field_aperture_wheel_position(selected_pos + 1)
        except Exception as e:
            print(f"Error setting port 1 field aperture: {e}")
        finally:
            self.enable_all_buttons(True)

    def get_confocal_mode(self) -> bool:
        """Get current confocal mode state.

        Returns:
            True if in confocal mode, False if in widefield mode.
        """
        return self.confocal_mode


class ObjectivesWidget(QWidget):
    signal_objective_changed = Signal()

    def __init__(self, objective_store, objective_changer=None):
        super(ObjectivesWidget, self).__init__()
        self.objectiveStore = objective_store
        self.objective_changer = objective_changer
        self.init_ui()
        self.dropdown.setCurrentText(self.objectiveStore.current_objective)

    def init_ui(self):
        self.dropdown = QComboBox(self)
        self.dropdown.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.dropdown.addItems(self.objectiveStore.objectives_dict.keys())
        self.dropdown.currentTextChanged.connect(self.on_objective_changed)

        layout = QHBoxLayout()
        layout.addWidget(QLabel("Objective Lens"))
        layout.addWidget(self.dropdown)
        self.setLayout(layout)

    def on_objective_changed(self, objective_name):
        self.objectiveStore.set_current_objective(objective_name)
        if USE_XERYON:
            if objective_name in XERYON_OBJECTIVE_SWITCHER_POS_1 and self.objective_changer.currentPosition() != 1:
                self.objective_changer.moveToPosition1()
            elif objective_name in XERYON_OBJECTIVE_SWITCHER_POS_2 and self.objective_changer.currentPosition() != 2:
                self.objective_changer.moveToPosition2()
        self.signal_objective_changed.emit()


class EmissionFilterWheelPanel(QWidget):
    """Compact emission filter wheel controls for the Camera tab (replaces standalone FilterControllerWidget)."""

    def __init__(
        self,
        live_controller: Optional[LiveController],
        config_repo: Optional[ConfigRepository] = None,
        filter_controller: Optional[AbstractFilterWheelController] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self.live_controller = live_controller
        self.config_repo = config_repo
        self.filter_controller = filter_controller
        self._combo_boxes: Dict[int, QComboBox] = {}
        self._home_buttons: Dict[int, QPushButton] = {}
        self._get_pos_buttons: Dict[int, QPushButton] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        device_configured = False
        if config_repo is not None:
            mc = config_repo.get_machine_config()
            dev = mc.get_device("emission_filter_wheel")
            device_configured = dev is not None and dev.enabled

        if not device_configured:
            row = QHBoxLayout()
            row.setSpacing(6)
            lab = QLabel("Emission filter:")
            lab.setMinimumWidth(100)
            cb = QComboBox()
            cb.addItem("Not configured (machine_config)")
            cb.setEnabled(False)
            cb.setMaximumWidth(240)
            row.addWidget(lab)
            row.addWidget(cb)
            row.addStretch()
            layout.addLayout(row)
            return

        if filter_controller is None:
            row = QHBoxLayout()
            row.setSpacing(6)
            lab = QLabel("Emission filter:")
            lab.setMinimumWidth(100)
            cb = QComboBox()
            cb.addItem("Hardware not available")
            cb.setEnabled(False)
            cb.setMaximumWidth(240)
            row.addWidget(lab)
            row.addWidget(cb)
            row.addStretch()
            layout.addLayout(row)
            return

        self._wheel_indices = list(filter_controller.available_filter_wheels) or [1]
        use_tabs = len(self._wheel_indices) > 1

        if use_tabs:
            self._tab_widget = QTabWidget()
            self._tab_widget.setMaximumHeight(118)
            self._tab_widget.setDocumentMode(True)
            for wheel_id in self._wheel_indices:
                tab = self._create_wheel_row_widget(wheel_id)
                self._tab_widget.addTab(tab, self._get_wheel_name(wheel_id))
            layout.addWidget(self._tab_widget)
        else:
            layout.addWidget(self._create_wheel_row_widget(self._wheel_indices[0]))

        if live_controller is not None:
            self.checkBox = QCheckBox("Do not move filter when switching channels", self)
            self.checkBox.setToolTip(
                "When checked, the wheel does not move automatically when the microscope configuration channel changes."
            )
            self.checkBox.stateChanged.connect(self.disable_movement_by_switching_channels)
            layout.addWidget(self.checkBox)

    def _get_wheel_name(self, wheel_id: int) -> str:
        if self.config_repo:
            try:
                registry = self.config_repo.get_filter_wheel_registry()
                if registry and registry.filter_wheels:
                    for wheel in registry.filter_wheels:
                        if wheel.id == wheel_id and wheel.name:
                            return wheel.name
            except Exception as e:
                self._log.warning(f"Failed to get filter wheel name for wheel_id={wheel_id}: {e}")
        return f"Wheel {wheel_id}"

    def _create_wheel_row_widget(self, wheel_id: int) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        try:
            wheel_info = self.filter_controller.get_filter_wheel_info(wheel_id)
            num_positions = wheel_info.number_of_slots
        except Exception:
            num_positions = 8

        position_names: Dict = {}
        if self.config_repo:
            try:
                registry = self.config_repo.get_filter_wheel_registry()
                if registry and registry.filter_wheels:
                    for wheel in registry.filter_wheels:
                        if wheel.id == wheel_id:
                            position_names = wheel.positions
                            break
            except Exception as e:
                self._log.warning(f"Failed to get filter position names for wheel {wheel_id}: {e}")

        combo_box = QComboBox()
        combo_box.setMaximumWidth(260)
        for i in range(1, num_positions + 1):
            filter_name = position_names.get(i) or position_names.get(str(i)) or f"Position {i}"
            combo_box.addItem(f"{i}: {filter_name}")
        self._combo_boxes[wheel_id] = combo_box

        get_pos_btn = QPushButton("Get")
        get_pos_btn.setMaximumWidth(48)
        home_btn = QPushButton("Home")
        home_btn.setMaximumWidth(48)

        self._get_pos_buttons[wheel_id] = get_pos_btn
        self._home_buttons[wheel_id] = home_btn

        row.addWidget(QLabel("Position:"))
        row.addWidget(combo_box)
        row.addWidget(get_pos_btn)
        row.addWidget(home_btn)
        row.addStretch()

        combo_box.currentIndexChanged.connect(lambda idx, wid=wheel_id: self._on_selection_change(wid, idx))
        get_pos_btn.clicked.connect(lambda checked, wid=wheel_id: self._update_position_from_controller(wid))
        home_btn.clicked.connect(lambda checked, wid=wheel_id: self._home(wid))

        return w

    def _home(self, wheel_id: int):
        self.filter_controller.home(wheel_id)

    def _update_position_from_controller(self, wheel_id: int):
        try:
            current_pos = self.filter_controller.get_filter_wheel_position().get(wheel_id, 1)
            combo_box = self._combo_boxes.get(wheel_id)
            if combo_box:
                combo_box.blockSignals(True)
                combo_box.setCurrentIndex(current_pos - 1)
                combo_box.blockSignals(False)
        except Exception as e:
            self._log.error(f"Error getting filter wheel {wheel_id} position: {e}")

    def _on_selection_change(self, wheel_id: int, index: int):
        if index >= 0:
            self.filter_controller.set_filter_wheel_position({wheel_id: index + 1})

    def disable_movement_by_switching_channels(self, state):
        if self.live_controller is None:
            return
        if state:
            self.live_controller.enable_channel_auto_filter_switching = False
        else:
            self.live_controller.enable_channel_auto_filter_switching = True

    def sync_positions_from_hardware(self) -> None:
        """Update Position comboboxes from the filter controller (after preset load or external move)."""
        boxes = getattr(self, "_combo_boxes", None)
        if not boxes:
            return
        for wheel_id in boxes:
            self._update_position_from_controller(wheel_id)


class CameraSettingsWidget(QFrame):

    # Emitted whenever a setting that changes the captured FOV size on the
    # sample is applied (binning, ROI width/height/offset). Consumers like the
    # NavigationViewer refresh their cached fov_width_mm reactively on this
    # signal instead of polling the camera every frame.
    signal_binning_changed = Signal()

    def __init__(
        self,
        camera: AbstractCamera,
        include_gain_exposure_time=False,
        include_camera_temperature_setting=False,
        include_camera_auto_wb_setting=False,
        include_trigger_controls: bool = False,
        obs_controller=None,
        main=None,
        filter_wheel_controller: Optional[AbstractFilterWheelController] = None,
        *args,
        **kwargs,
    ):

        super().__init__(*args, **kwargs)
        self.camera: AbstractCamera = camera
        self._log = squid.logging.get_logger(f"{self.__class__.__name__}/{self.camera.__class__})")
        self._obs_controller = obs_controller
        self.live_controller = obs_controller.live_controller if obs_controller else None
        self._filter_wheel_controller = filter_wheel_controller
        self.add_components(
            include_gain_exposure_time,
            include_camera_temperature_setting,
            include_camera_auto_wb_setting,
            include_trigger_controls,
        )
        # set frame style
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)

    def add_components(
        self,
        include_gain_exposure_time,
        include_camera_temperature_setting,
        include_camera_auto_wb_setting,
        include_trigger_controls: bool,
    ):

        # add buttons and input fields
        self.entry_exposureTime = QDoubleSpinBox()
        self.entry_exposureTime.setKeyboardTracking(False)
        self.entry_exposureTime.setMinimum(self.camera.get_exposure_limits()[0])
        self.entry_exposureTime.setMaximum(self.camera.get_exposure_limits()[1])
        self.entry_exposureTime.setSingleStep(1)
        self.entry_exposureTime.setValue(20)
        self.camera.set_exposure_time(20)

        self.entry_analogGain = QDoubleSpinBox()
        try:
            gain_range = self.camera.get_gain_range()
            self.entry_analogGain.setMinimum(gain_range.min_gain)
            self.entry_analogGain.setMaximum(gain_range.max_gain)
            self.entry_analogGain.setSingleStep(gain_range.gain_step)
            self.entry_analogGain.setValue(gain_range.min_gain)
            self.camera.set_analog_gain(gain_range.min_gain)
        except NotImplementedError:
            self._log.info("Camera does not support analog gain, disabling analog gain control.")
            self.entry_analogGain.setValue(0)
            self.entry_analogGain.setEnabled(False)

        self.dropdown_cameraMode = QComboBox()
        try:
            camera_modes = self.camera.get_available_camera_modes()
        except NotImplementedError:
            camera_modes = ["FULL"]
        self.dropdown_cameraMode.addItems(camera_modes)
        if self.camera.get_camera_mode() is not None:
            self.dropdown_cameraMode.setCurrentText(self.camera.get_camera_mode())
        self.dropdown_cameraMode.setToolTip("Select camera mode (may be pixel format, well depth, etc.)")
        # self.dropdown_cameraMode.currentTextChanged.connect(self.set_camera_mode)
        self.dropdown_cameraMode.setSizePolicy(QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed))
        # to do: load and save pixel format in configurations

        roi_info = self.camera.get_region_of_interest()
        max_x, max_y = self.camera.get_resolution()

        self.entry_ROI_offset_x = QSpinBox()
        self.entry_ROI_offset_x.setSingleStep(8)
        self.entry_ROI_offset_x.setFixedWidth(60)
        self.entry_ROI_offset_x.setMinimum(0)
        self.entry_ROI_offset_x.setMaximum(max_x)
        self.entry_ROI_offset_x.setKeyboardTracking(False)
        self.entry_ROI_offset_x.setValue(roi_info[0])

        self.entry_ROI_offset_y = QSpinBox()
        self.entry_ROI_offset_y.setSingleStep(8)
        self.entry_ROI_offset_y.setFixedWidth(60)
        self.entry_ROI_offset_y.setMinimum(0)
        self.entry_ROI_offset_y.setMaximum(max_y)
        self.entry_ROI_offset_y.setKeyboardTracking(False)
        self.entry_ROI_offset_y.setValue(roi_info[1])

        self.entry_ROI_width = QSpinBox()
        self.entry_ROI_width.setMinimum(16)
        self.entry_ROI_width.setMaximum(max_x)
        self.entry_ROI_width.setSingleStep(8)
        self.entry_ROI_width.setFixedWidth(60)
        self.entry_ROI_width.setKeyboardTracking(False)
        self.entry_ROI_width.setValue(roi_info[2])

        self.entry_ROI_height = QSpinBox()
        self.entry_ROI_height.setSingleStep(8)
        self.entry_ROI_height.setMinimum(16)
        self.entry_ROI_height.setMaximum(max_y)
        self.entry_ROI_height.setFixedWidth(60)
        self.entry_ROI_height.setKeyboardTracking(False)
        self.entry_ROI_height.setValue(roi_info[3])

        # checkbox to control automatic centering of ROI
        self.checkbox_ROI_centered = QCheckBox("Centered")
        self.checkbox_ROI_centered.setChecked(True)

        self.entry_temperature = QDoubleSpinBox()
        self.entry_temperature.setKeyboardTracking(False)
        self.entry_temperature.setMaximum(25)
        self.entry_temperature.setMinimum(-50)
        self.entry_temperature.setDecimals(1)
        self.label_temperature_measured = QLabel()
        # self.label_temperature_measured.setNum(0)
        self.label_temperature_measured.setFrameStyle(QFrame.Panel | QFrame.Sunken)

        # connection — all mutations go through obs_controller
        if self._obs_controller is not None:
            self.entry_exposureTime.valueChanged.connect(self._obs_controller.set_exposure_time)
            self.entry_analogGain.valueChanged.connect(self._obs_controller.set_analog_gain)
            self.dropdown_cameraMode.currentTextChanged.connect(self._obs_controller.set_camera_mode)
        else:
            # Fallback for secondary cameras without obs_controller
            self.entry_exposureTime.valueChanged.connect(self.camera.set_exposure_time)
            self.entry_analogGain.valueChanged.connect(self.camera.set_analog_gain)
            self.dropdown_cameraMode.currentTextChanged.connect(
                lambda s: self.camera.set_camera_mode(s)
            )
        self.entry_ROI_offset_x.valueChanged.connect(self.set_ROI_offset)
        self.entry_ROI_offset_y.valueChanged.connect(self.set_ROI_offset)
        self.entry_ROI_height.valueChanged.connect(self.set_Height)
        self.entry_ROI_width.valueChanged.connect(self.set_Width)
        self.checkbox_ROI_centered.toggled.connect(self.on_centered_toggled)

        # ensure initial enabled/disabled state of offsets matches checkbox state
        self.on_centered_toggled(self.checkbox_ROI_centered.isChecked())

        # layout — left: exposure / trigger / mode / emission; right (~half width): ROI in two rows + Centered
        self.camera_layout = QVBoxLayout()
        self.camera_layout.setSpacing(3)
        self.camera_layout.setContentsMargins(2, 2, 2, 2)
        self.entry_exposureTime.setMaximumWidth(120)
        self.entry_analogGain.setMaximumWidth(120)

        left_col = QVBoxLayout()
        left_col.setSpacing(3)

        if include_gain_exposure_time:
            exposure_gain_line = QHBoxLayout()
            exposure_gain_line.setSpacing(6)
            exposure_gain_line.addWidget(QLabel("Exposure Time (ms)"))
            exposure_gain_line.addWidget(self.entry_exposureTime)
            exposure_gain_line.addSpacing(16)
            exposure_gain_line.addWidget(QLabel("Analog Gain"))
            exposure_gain_line.addWidget(self.entry_analogGain)
            exposure_gain_line.addStretch()
            left_col.addLayout(exposure_gain_line)

        self.dropdown_cameraMode.setMaximumWidth(200)
        try:
            current_binning = self.camera.get_binning()
            current_binning_string = "x".join([str(current_binning[0]), str(current_binning[1])])
            binning_options = [f"{binning[0]}x{binning[1]}" for binning in self.camera.get_binning_options()]
            self.dropdown_binning = QComboBox()
            self.dropdown_binning.addItems(binning_options)
            self.dropdown_binning.setCurrentText(current_binning_string)

            self.dropdown_binning.currentTextChanged.connect(self.set_binning)
        except AttributeError as ae:
            print(ae)
            self.dropdown_binning = QComboBox()
            self.dropdown_binning.setEnabled(False)
            pass
        self.dropdown_binning.setMaximumWidth(88)

        if include_trigger_controls and self.live_controller is not None:
            self.dropdown_triggerMode = QComboBox()
            self.dropdown_triggerMode.addItems([TriggerMode.SOFTWARE, TriggerMode.HARDWARE, TriggerMode.CONTINUOUS])
            self.dropdown_triggerMode.setMaximumWidth(160)

            initial_trigger_mode = TriggerMode.CONTINUOUS
            self.dropdown_triggerMode.blockSignals(True)
            idx = self.dropdown_triggerMode.findText(initial_trigger_mode)
            if idx >= 0:
                self.dropdown_triggerMode.setCurrentIndex(idx)
            self.dropdown_triggerMode.blockSignals(False)
            # Drive LiveController + camera into the default mode so they agree with the UI.
            try:
                self.live_controller.set_trigger_mode(initial_trigger_mode)
            except Exception:
                pass

            self.entry_triggerFPS = QDoubleSpinBox()
            self.entry_triggerFPS.setKeyboardTracking(False)
            self.entry_triggerFPS.setRange(0.02, 1000)
            self.entry_triggerFPS.setSingleStep(1)
            self.entry_triggerFPS.setDecimals(0)
            self.entry_triggerFPS.setMaximumWidth(72)

            # Legacy default: LiveControlWidget historically defaulted to 10 fps.
            initial_fps = getattr(self.live_controller, "fps_trigger", None)
            # Treat the LiveController constructor default (1 fps) as "unset" for UI defaults.
            if initial_fps is None or initial_fps <= 0 or initial_fps == 1:
                initial_fps = 10

            try:
                self.live_controller.set_trigger_fps(initial_fps)
            except Exception:
                # If trigger_fps cannot be set in the current mode, keep the UI value.
                pass
            self.entry_triggerFPS.blockSignals(True)
            self.entry_triggerFPS.setValue(float(initial_fps))
            self.entry_triggerFPS.blockSignals(False)

            # Wire after initial values are set to avoid overriding during construction.
            if self._obs_controller is not None:
                self.dropdown_triggerMode.currentTextChanged.connect(self._obs_controller.set_trigger_mode)
                self.entry_triggerFPS.valueChanged.connect(self._obs_controller.set_trigger_fps)
            elif hasattr(self, 'live_controller') and self.live_controller is not None:
                self.dropdown_triggerMode.currentTextChanged.connect(lambda s: self.live_controller.set_trigger_mode(s))
                self.entry_triggerFPS.valueChanged.connect(lambda v: self.live_controller.set_trigger_fps(v))

            trigger_row = QHBoxLayout()
            trigger_row.setSpacing(6)
            trigger_row.addWidget(QLabel("Trigger Mode"))
            trigger_row.addWidget(self.dropdown_triggerMode)
            trigger_row.addWidget(QLabel("Trigger FPS"))
            trigger_row.addWidget(self.entry_triggerFPS)
            trigger_row.addStretch()
            left_col.addLayout(trigger_row)

        format_row = QHBoxLayout()
        format_row.setSpacing(6)
        format_row.addWidget(QLabel("Camera Mode"))
        format_row.addWidget(self.dropdown_cameraMode)
        format_row.addWidget(QLabel("Binning"))
        format_row.addWidget(self.dropdown_binning)
        format_row.addStretch()
        left_col.addLayout(format_row)

        if self._obs_controller is not None:
            self._emission_filter_panel = EmissionFilterWheelPanel(
                self.live_controller,
                config_repo=self._obs_controller.config_repo,
                filter_controller=self._filter_wheel_controller,
                parent=self,
            )
            left_col.addWidget(self._emission_filter_panel)

        left_widget = QWidget()
        left_widget.setLayout(left_col)

        roi_block = QGroupBox("ROI")
        roi_block_layout = QVBoxLayout(roi_block)
        roi_block_layout.setSpacing(4)
        roi_block_layout.setContentsMargins(8, 10, 8, 8)
        roi_row_hw = QHBoxLayout()
        roi_row_hw.setSpacing(6)
        roi_row_hw.addWidget(QLabel("Height"))
        roi_row_hw.addWidget(self.entry_ROI_height)
        roi_row_hw.addSpacing(8)
        roi_row_hw.addWidget(QLabel("Width"))
        roi_row_hw.addWidget(self.entry_ROI_width)
        roi_row_hw.addStretch()
        roi_row_xy = QHBoxLayout()
        roi_row_xy.setSpacing(6)
        roi_row_xy.addWidget(QLabel("Y-offset"))
        roi_row_xy.addWidget(self.entry_ROI_offset_y)
        roi_row_xy.addSpacing(8)
        roi_row_xy.addWidget(QLabel("X-offset"))
        roi_row_xy.addWidget(self.entry_ROI_offset_x)
        roi_row_xy.addStretch()
        roi_block_layout.addLayout(roi_row_hw)
        roi_block_layout.addLayout(roi_row_xy)
        roi_block_layout.addWidget(self.checkbox_ROI_centered)

        top_split = QHBoxLayout()
        top_split.setSpacing(10)
        top_split.addWidget(left_widget, 1, Qt.AlignTop)
        top_split.addWidget(roi_block, 1, Qt.AlignTop)
        self.camera_layout.addLayout(top_split)

        if include_camera_temperature_setting:
            temp_line = QHBoxLayout()
            temp_line.addWidget(QLabel("Set Temperature (C)"))
            temp_line.addWidget(self.entry_temperature)
            temp_line.addWidget(QLabel("Actual Temperature (C)"))
            temp_line.addWidget(self.label_temperature_measured)
            try:
                self.entry_temperature.valueChanged.connect(self.set_temperature)
                self.camera.set_temperature_reading_callback(self.update_measured_temperature)
            except AttributeError:
                pass
            self.camera_layout.addLayout(temp_line)

        if DISPLAY_TOUPCAMER_BLACKLEVEL_SETTINGS is True:
            blacklevel_line = QHBoxLayout()
            blacklevel_line.addWidget(QLabel("Black Level"))

            self.label_blackLevel = QSpinBox()
            self.label_blackLevel.setKeyboardTracking(False)
            self.label_blackLevel.setMinimum(0)
            self.label_blackLevel.setMaximum(31)
            self.label_blackLevel.valueChanged.connect(self.update_blacklevel)
            self.label_blackLevel.setSuffix(" ")

            blacklevel_line.addWidget(self.label_blackLevel)

            self.camera_layout.addLayout(blacklevel_line)

        if include_camera_auto_wb_setting and CameraPixelFormat.is_color_format(self.camera.get_pixel_format()):
            # auto white balance
            self.btn_auto_wb = QPushButton("Auto White Balance")
            self.btn_auto_wb.setCheckable(True)
            self.btn_auto_wb.setChecked(False)
            self.btn_auto_wb.clicked.connect(self.toggle_auto_wb)

            self.camera_layout.addWidget(self.btn_auto_wb)

        self.setLayout(self.camera_layout)

    def toggle_auto_wb(self, pressed):
        # 0: OFF  1:CONTINUOUS  2:ONCE
        if pressed:
            # Run auto white balance once, then uncheck
            self.camera.set_auto_white_balance_gains(on=True)
        else:
            self.camera.set_auto_white_balance_gains(on=False)
            r, g, b = self.camera.get_white_balance_gains()
            self.camera.set_white_balance_gains(r, g, b)

    def set_exposure_time(self, exposure_time):
        self.entry_exposureTime.setValue(exposure_time)

    def set_analog_gain(self, analog_gain):
        self.entry_analogGain.setValue(analog_gain)

    def set_Width(self):
        # round width to an even number so centering works cleanly
        width = int(self.entry_ROI_width.value() // 2) * 2
        self.entry_ROI_width.blockSignals(True)
        self.entry_ROI_width.setValue(width)
        self.entry_ROI_width.blockSignals(False)
        if getattr(self, "checkbox_ROI_centered", None) is not None and self.checkbox_ROI_centered.isChecked():
            self.update_centered_roi()
        else:
            self.camera.set_region_of_interest(
                self.entry_ROI_offset_x.value(),
                self.entry_ROI_offset_y.value(),
                self.entry_ROI_width.value(),
                self.entry_ROI_height.value(),
            )
            self.signal_binning_changed.emit()

    def set_Height(self):
        # round height to an even number so centering works cleanly
        height = int(self.entry_ROI_height.value() // 2) * 2
        self.entry_ROI_height.blockSignals(True)
        self.entry_ROI_height.setValue(height)
        self.entry_ROI_height.blockSignals(False)
        if getattr(self, "checkbox_ROI_centered", None) is not None and self.checkbox_ROI_centered.isChecked():
            self.update_centered_roi()
        else:
            self.camera.set_region_of_interest(
                self.entry_ROI_offset_x.value(),
                self.entry_ROI_offset_y.value(),
                self.entry_ROI_width.value(),
                self.entry_ROI_height.value(),
            )
            self.signal_binning_changed.emit()
        self._log.info(f"Current camera ROI: {self.camera.get_region_of_interest()}")

    def set_ROI_offset(self):
        self.camera.set_region_of_interest(
            self.entry_ROI_offset_x.value(),
            self.entry_ROI_offset_y.value(),
            self.entry_ROI_width.value(),
            self.entry_ROI_height.value(),
        )
        self.signal_binning_changed.emit()

    def update_centered_roi(self):
        """
        Center the ROI based on the current width/height and full sensor resolution.
        Offsets and dimensions are rounded to even numbers so centering works cleanly.
        """
        max_x, max_y = self.camera.get_resolution()

        # ensure even dimensions
        width = int(self.entry_ROI_width.value() // 2) * 2
        height = int(self.entry_ROI_height.value() // 2) * 2

        self.entry_ROI_width.blockSignals(True)
        self.entry_ROI_width.setValue(width)
        self.entry_ROI_width.blockSignals(False)

        self.entry_ROI_height.blockSignals(True)
        self.entry_ROI_height.setValue(height)
        self.entry_ROI_height.blockSignals(False)

        # compute even offsets for centering
        offset_x = (max_x - width) / 2
        offset_y = (max_y - height) / 2

        offset_x = int(offset_x // 2) * 2
        offset_y = int(offset_y // 2) * 2

        self.entry_ROI_offset_x.blockSignals(True)
        self.entry_ROI_offset_x.setValue(offset_x)
        self.entry_ROI_offset_x.blockSignals(False)

        self.entry_ROI_offset_y.blockSignals(True)
        self.entry_ROI_offset_y.setValue(offset_y)
        self.entry_ROI_offset_y.blockSignals(False)

        self.camera.set_region_of_interest(
            self.entry_ROI_offset_x.value(),
            self.entry_ROI_offset_y.value(),
            self.entry_ROI_width.value(),
            self.entry_ROI_height.value(),
        )
        self.signal_binning_changed.emit()

    def on_centered_toggled(self, checked: bool):
        """
        When centered is enabled, disable manual editing of offsets and
        automatically compute centered ROI from width/height.
        """
        self.entry_ROI_offset_x.setEnabled(not checked)
        self.entry_ROI_offset_y.setEnabled(not checked)

        if checked:
            self.update_centered_roi()

    def set_temperature(self):
        try:
            self.camera.set_temperature(float(self.entry_temperature.value()))
        except AttributeError:
            self._log.warning("Cannot set temperature - not supported.")

    def update_measured_temperature(self, temperature):
        self.label_temperature_measured.setNum(temperature)

    def set_binning(self, binning_text):
        binning_parts = binning_text.split("x")
        binning_x = int(binning_parts[0])
        binning_y = int(binning_parts[1])

        self.camera.set_binning(binning_x, binning_y)

        self.entry_ROI_offset_x.blockSignals(True)
        self.entry_ROI_offset_y.blockSignals(True)
        self.entry_ROI_height.blockSignals(True)
        self.entry_ROI_width.blockSignals(True)

        # TODO: move these calculations to camera class as they can be different for different cameras
        def round_to_8(val):
            return int(8 * val // 8)

        x_offset, y_offset, width, height = self.camera.get_region_of_interest()
        x_max, y_max = self.camera.get_resolution()
        self.entry_ROI_height.setMaximum(y_max)
        self.entry_ROI_width.setMaximum(x_max)

        self.entry_ROI_offset_x.setMaximum(x_max)
        self.entry_ROI_offset_y.setMaximum(y_max)

        self.entry_ROI_offset_x.setValue(round_to_8(x_offset))
        self.entry_ROI_offset_y.setValue(round_to_8(y_offset))
        self.entry_ROI_height.setValue(round_to_8(height))
        self.entry_ROI_width.setValue(round_to_8(width))

        self.entry_ROI_offset_x.blockSignals(False)
        self.entry_ROI_offset_y.blockSignals(False)
        self.entry_ROI_height.blockSignals(False)
        self.entry_ROI_width.blockSignals(False)

        self.signal_binning_changed.emit()

    def update_blacklevel(self, blacklevel):
        try:
            self.camera.set_black_level(blacklevel)
        except AttributeError:
            self._log.warning("Cannot set black level - not supported.")

    def sync_controls_from_hardware(self) -> None:
        """
        Refresh Camera tab widgets from camera + live_controller (after Observation State load, etc.).
        """
        from control.core.observation_state_service import infer_roi_centered_from_camera

        self.entry_exposureTime.blockSignals(True)
        self.entry_analogGain.blockSignals(True)
        self.dropdown_cameraMode.blockSignals(True)
        if getattr(self, "dropdown_binning", None) is not None:
            self.dropdown_binning.blockSignals(True)
        self.entry_ROI_offset_x.blockSignals(True)
        self.entry_ROI_offset_y.blockSignals(True)
        self.entry_ROI_width.blockSignals(True)
        self.entry_ROI_height.blockSignals(True)
        self.checkbox_ROI_centered.blockSignals(True)
        if getattr(self, "dropdown_triggerMode", None) is not None:
            self.dropdown_triggerMode.blockSignals(True)
        if getattr(self, "entry_triggerFPS", None) is not None:
            self.entry_triggerFPS.blockSignals(True)
        try:
            self.entry_exposureTime.setValue(float(self.camera.get_exposure_time()))
            try:
                self.entry_analogGain.setValue(float(self.camera.get_analog_gain()))
            except Exception:
                pass
            try:
                cm = self.camera.get_camera_mode()
                if cm is not None:
                    self.dropdown_cameraMode.setCurrentText(cm)
            except Exception:
                pass
            try:
                bx, by = self.camera.get_binning()
                if getattr(self, "dropdown_binning", None) is not None and self.dropdown_binning.isEnabled():
                    self.dropdown_binning.setCurrentText(f"{bx}x{by}")
            except Exception:
                pass
            try:
                ox, oy, w, h = self.camera.get_region_of_interest()
                self.entry_ROI_offset_x.setValue(int(ox))
                self.entry_ROI_offset_y.setValue(int(oy))
                self.entry_ROI_width.setValue(int(w))
                self.entry_ROI_height.setValue(int(h))
            except Exception:
                pass
            centered = infer_roi_centered_from_camera(self.camera)
            self.checkbox_ROI_centered.setChecked(centered)
            self.entry_ROI_offset_x.setEnabled(not centered)
            self.entry_ROI_offset_y.setEnabled(not centered)

            if self.live_controller is not None:
                if getattr(self, "dropdown_triggerMode", None) is not None:
                    try:
                        tm = self.live_controller.get_trigger_mode()
                        text = tm if isinstance(tm, str) else getattr(tm, "value", str(tm))
                        idx = self.dropdown_triggerMode.findText(text)
                        if idx >= 0:
                            self.dropdown_triggerMode.setCurrentIndex(idx)
                    except Exception:
                        pass
                if getattr(self, "entry_triggerFPS", None) is not None:
                    try:
                        fps = float(getattr(self.live_controller, "fps_trigger", 10.0) or 10.0)
                        self.entry_triggerFPS.setValue(fps)
                    except Exception:
                        pass

            panel = getattr(self, "_emission_filter_panel", None)
            if panel is not None and hasattr(panel, "sync_positions_from_hardware"):
                panel.sync_positions_from_hardware()
            if panel is not None and self.live_controller is not None and hasattr(panel, "checkBox"):
                panel.checkBox.blockSignals(True)
                try:
                    panel.checkBox.setChecked(not self.live_controller.enable_channel_auto_filter_switching)
                except Exception:
                    pass
                panel.checkBox.blockSignals(False)
        finally:
            self.entry_exposureTime.blockSignals(False)
            self.entry_analogGain.blockSignals(False)
            self.dropdown_cameraMode.blockSignals(False)
            if getattr(self, "dropdown_binning", None) is not None:
                self.dropdown_binning.blockSignals(False)
            self.entry_ROI_offset_x.blockSignals(False)
            self.entry_ROI_offset_y.blockSignals(False)
            self.entry_ROI_width.blockSignals(False)
            self.entry_ROI_height.blockSignals(False)
            self.checkbox_ROI_centered.blockSignals(False)
            if getattr(self, "dropdown_triggerMode", None) is not None:
                self.dropdown_triggerMode.blockSignals(False)
            if getattr(self, "entry_triggerFPS", None) is not None:
                self.entry_triggerFPS.blockSignals(False)


class ProfileWidget(QFrame):

    signal_profile_changed = Signal()

    def __init__(self, config_repo: ConfigRepository, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config_repo = config_repo

        self.setFrameStyle(QFrame.Panel | QFrame.Raised)
        self.setup_ui()

    def setup_ui(self):
        # Create widgets
        self.dropdown_profiles = QComboBox()
        self.dropdown_profiles.addItems(self.config_repo.get_available_profiles())
        if self.config_repo.current_profile:
            self.dropdown_profiles.setCurrentText(self.config_repo.current_profile)
        sizePolicy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.dropdown_profiles.setSizePolicy(sizePolicy)

        self.btn_newProfile = QPushButton("Save As")

        # Connect signals
        self.dropdown_profiles.currentTextChanged.connect(self.load_profile)
        self.btn_newProfile.clicked.connect(self.create_new_profile)

        # Layout
        layout = QHBoxLayout()
        layout.addWidget(QLabel("Configuration Profile"))
        layout.addWidget(self.dropdown_profiles, 2)
        layout.addWidget(self.btn_newProfile)

        self.setLayout(layout)

    def load_profile(self):
        """Load the selected profile."""
        profile_name = self.dropdown_profiles.currentText()
        # Load the profile (ensures defaults and sets as current)
        self.config_repo.load_profile(profile_name)
        self.signal_profile_changed.emit()

    def create_new_profile(self):
        """Create a new profile with current configurations."""
        dialog = QInputDialog()
        profile_name, ok = dialog.getText(self, "New Profile", "Enter new profile name:", QLineEdit.Normal, "")

        if ok and profile_name:
            try:
                current = self.config_repo.current_profile
                if current:
                    self.config_repo.copy_profile(current, profile_name)
                    self.config_repo.set_profile(profile_name)
                else:
                    # No current profile, create empty
                    self.config_repo.create_profile(profile_name)
                    self.config_repo.load_profile(profile_name)
                # Update profile dropdown
                self.dropdown_profiles.addItem(profile_name)
                self.dropdown_profiles.setCurrentText(profile_name)
                # Notify listeners that profile changed
                self.signal_profile_changed.emit()
            except ValueError as e:
                QMessageBox.warning(self, "Error", str(e))

    def get_current_profile(self):
        """Return the currently selected profile name."""
        return self.dropdown_profiles.currentText()


class LiveControlWidget(QFrame):

    signal_autoLevelSetting = Signal(bool)
    signal_live_configuration = Signal(object)
    signal_start_live = Signal()

    def __init__(
        self,
        streamHandler,
        liveController,
        objectiveStore,
        show_display_options=False,
        show_autolevel=False,
        autolevel=False,
        stretch=True,
        objectives_widget=None,
        main=None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self.liveController: LiveController = liveController
        self.camera = self.liveController.microscope.camera
        self.streamHandler = streamHandler
        self.objectiveStore = objectiveStore
        self.fps_display = 10
        self.streamHandler.set_display_fps(self.fps_display)

        # channels = self.liveController.get_observation_states()
        # if not channels:
        #     self._log.error("No channels available - cannot initialize LiveControlWidget")
        #     self.currentConfiguration = None
        # else:
        #     # Restore the last active channel if available, otherwise use first
        #     selected = channels[0]
        #     last_name = self.liveController.microscope.config_repo.get_last_active_channel_name()
        #     if last_name:
        #         for s in channels:
        #             if s.name == last_name:
        #                 selected = s
        #                 break
        #     self.currentConfiguration = selected

        self.add_components(show_display_options, show_autolevel, autolevel, stretch, objectives_widget)
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)

        self.is_switching_mode = False

    def add_components(self, show_display_options, show_autolevel, autolevel, stretch, objectives_widget=None):
        sizePolicy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # self.dropdown_modeSelection = QComboBox()
        # for state in self.liveController.get_observation_states():
        #     self.dropdown_modeSelection.addItems([state.name])
        # if self.currentConfiguration:
        #     self.dropdown_modeSelection.setCurrentText(self.currentConfiguration.name)
        # self.dropdown_modeSelection.setSizePolicy(sizePolicy)

        self.btn_live = QPushButton("Start Live")
        self.btn_live.setCheckable(True)
        self.btn_live.setChecked(False)
        self.btn_live.setDefault(False)
        self.btn_live.setStyleSheet("background-color: #C2C2FF")
        self.btn_live.setSizePolicy(sizePolicy)

        # display resolution scaling
        self.entry_displayFPS = QDoubleSpinBox()
        self.entry_displayFPS.setKeyboardTracking(False)
        self.entry_displayFPS.setMinimum(1)
        self.entry_displayFPS.setMaximum(240)
        self.entry_displayFPS.setSingleStep(1)
        self.entry_displayFPS.setDecimals(0)
        self.entry_displayFPS.setValue(self.fps_display)

        self.slider_resolutionScaling = QSlider(Qt.Horizontal)
        self.slider_resolutionScaling.setTickPosition(QSlider.TicksBelow)
        self.slider_resolutionScaling.setMinimum(10)
        self.slider_resolutionScaling.setMaximum(100)
        self.slider_resolutionScaling.setValue(100)
        self.slider_resolutionScaling.setSingleStep(10)

        self.label_resolutionScaling = QSpinBox()
        self.label_resolutionScaling.setKeyboardTracking(False)
        self.label_resolutionScaling.setMinimum(10)
        self.label_resolutionScaling.setMaximum(100)
        self.label_resolutionScaling.setValue(self.slider_resolutionScaling.value())
        self.label_resolutionScaling.setSuffix(" %")
        self.slider_resolutionScaling.setSingleStep(5)

        self.slider_resolutionScaling.valueChanged.connect(lambda v: self.label_resolutionScaling.setValue(round(v)))
        self.label_resolutionScaling.valueChanged.connect(lambda v: self.slider_resolutionScaling.setValue(round(v)))

        # autolevel
        self.btn_autolevel = QPushButton("Autolevel")
        self.btn_autolevel.setCheckable(True)
        self.btn_autolevel.setChecked(autolevel)

        # snap frame grabber
        from control._def import DEFAULT_SAVING_PATH
        self.lineEdit_snapSavingDir = QLineEdit()
        self.lineEdit_snapSavingDir.setReadOnly(True)
        self.lineEdit_snapSavingDir.setText(DEFAULT_SAVING_PATH)
        self.snap_saving_path = DEFAULT_SAVING_PATH

        self.btn_setSnapSavingDir = QPushButton("Browse")
        self.btn_setSnapSavingDir.setDefault(False)
        try:
            self.btn_setSnapSavingDir.setIcon(QIcon("icon/folder.png"))
        except:
            pass
        self.btn_setSnapSavingDir.clicked.connect(self.set_snap_saving_dir)

        self.lineEdit_snapTag = QLineEdit()
        self.lineEdit_snapTag.setPlaceholderText("Enter tag (optional)")

        self.btn_snap = QPushButton("Snap")
        self.btn_snap.setDefault(False)
        self.btn_snap.setStyleSheet("background-color: #FFC2C2")
        self.btn_snap.clicked.connect(self.snap_frame)

        # connections
        self.entry_displayFPS.valueChanged.connect(self.streamHandler.set_display_fps)
        self.slider_resolutionScaling.valueChanged.connect(self.streamHandler.set_display_resolution_scaling)
        self.slider_resolutionScaling.valueChanged.connect(self.liveController.set_display_resolution_scaling)
        self.btn_live.clicked.connect(self.toggle_live)
        self.btn_autolevel.toggled.connect(self.signal_autoLevelSetting.emit)

        # layout
        grid_line05 = QHBoxLayout()
        if show_display_options:
            resolution_label = QLabel("Display Resolution")
            resolution_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid_line05.addWidget(resolution_label)
            grid_line05.addWidget(self.slider_resolutionScaling)
            grid_line05.addWidget(self.label_resolutionScaling)

        snap_group = QGroupBox("Snap")
        snap_layout = QVBoxLayout()

        snap_path_layout = QGridLayout()
        snap_path_layout.addWidget(QLabel("Saving Path"), 0, 0)
        snap_path_layout.addWidget(self.lineEdit_snapSavingDir, 0, 1)
        snap_path_layout.addWidget(self.btn_setSnapSavingDir, 0, 2)

        snap_tag_layout = QGridLayout()
        snap_tag_layout.addWidget(QLabel("Tag"), 0, 0)
        snap_tag_layout.addWidget(self.lineEdit_snapTag, 0, 1)

        snap_button_layout = QHBoxLayout()
        snap_button_layout.addWidget(self.btn_snap)
        snap_button_layout.addStretch()

        snap_layout.addLayout(snap_path_layout)
        snap_layout.addLayout(snap_tag_layout)
        snap_layout.addLayout(snap_button_layout)
        snap_layout.setContentsMargins(8, 8, 8, 8)
        snap_group.setLayout(snap_layout)

        self.grid = QVBoxLayout()
        top_line = QHBoxLayout()
        top_line.setSpacing(8)
        top_line.addWidget(self.btn_live, 0)
        if show_autolevel:
            top_line.addWidget(self.btn_autolevel, 0)
        if objectives_widget is not None:
            top_line.addWidget(objectives_widget, 0)
        top_line.addStretch(1)
        self.grid.addLayout(top_line)
        if show_display_options:
            self.grid.addLayout(grid_line05)
        self.grid.addWidget(snap_group)
        if not stretch:
            self.grid.addStretch()
        self.setLayout(self.grid)

    def toggle_live(self, pressed):
        if pressed:
            self.liveController.start_live()
            self.btn_live.setText("Stop Live")
            self.signal_start_live.emit()
        else:
            self.liveController.stop_live()
            self.btn_live.setText("Start Live")

    def set_snap_saving_dir(self):
        """Set saving directory for snap frames."""
        dialog = QFileDialog()
        save_dir_base = dialog.getExistingDirectory(None, "Select Folder", self.lineEdit_snapSavingDir.text())
        if save_dir_base:
            self.lineEdit_snapSavingDir.setText(save_dir_base)
            self.snap_saving_path = save_dir_base

    def _save_snap_acquisition_metadata(self, filepath: str) -> None:
        """Write ``{snap_stem}_acquisition_metadata.yaml`` next to the snap TIFF."""
        from pathlib import Path

        from control.core.acquisition_metadata_helpers import build_acquisition_metadata
        from control.core.observation_state_service import collect_emission_filter_positions

        path = Path(filepath)
        stem = path.stem
        meta_filename = f"{stem}_acquisition_metadata.yaml"
        repo = self.liveController.microscope.config_repo

        obs_state = None
        if repo.current_profile:
            try:
                wheel = getattr(self.liveController.microscope, "emission_filter_wheel", None)
                emission = collect_emission_filter_positions(wheel)
                obs_state = self.liveController.obs_controller.collect_observation_state(
                    emission_filter_positions=emission or None,
                )
            except Exception as e:
                self._log.warning("Snap: could not collect observation state for metadata: %s", e)

        selected_names = []
        if obs_state and obs_state.illuminator_states:
            selected_names = [ist.illumination_channel for ist in obs_state.illuminator_states if ist.on]
        elif self.currentConfiguration is not None:
            selected_names = [self.currentConfiguration.name]

        try:
            metadata = build_acquisition_metadata(
                experiment_id=stem,
                recording_start_time=time.time(),
                objective_store=self.objectiveStore,
                live_controller=self.liveController,
                camera=self.camera,
                scan_parameters={"source": "live_snap", "image_file": path.name},
                observation_state=obs_state,
                selected_channel_names=selected_names,
            )
            out = repo.save_acquisition_metadata(path.parent, metadata, filename=meta_filename)
            self._log.info("Snap acquisition metadata saved to: %s", out)
        except Exception as e:
            self._log.warning("Snap: could not save acquisition metadata: %s", e)

    def snap_frame(self):
        """Capture and save the most recent frame from Live View."""
        import imageio
        from datetime import datetime

        was_live_before_snap = self.liveController.is_live

        try:
            if not was_live_before_snap:
                self.liveController.start_live()
                exposure_time_ms = float(self.camera.get_exposure_time())
                wait_time_s = max(0.5, (exposure_time_ms / 1000.0) * 2)
                time.sleep(wait_time_s)

            frame = self.camera.read_camera_frame()
            if frame is None:
                self._log.warning("Failed to capture frame for snap")
                msg = QMessageBox()
                msg.setText("Failed to capture frame. Please ensure the camera is streaming.")
                msg.exec_()
                return

            image = np.squeeze(frame.frame)

            if hasattr(self.streamHandler, '_fns') and hasattr(self.streamHandler._fns, 'image_to_display'):
                self.streamHandler._fns.image_to_display(image)

            tag = self.lineEdit_snapTag.text().strip()
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

            if tag:
                filename = f"{timestamp}_{tag}.tif"
            else:
                filename = f"{timestamp}_snap.tif"

            filepath = os.path.join(self.snap_saving_path, filename)

            imageio.imwrite(filepath, image)
            self._log.info(f"Snap frame saved to: {filepath}")
            self._save_snap_acquisition_metadata(filepath)

        except Exception as e:
            self._log.error(f"Error during snap: {e}")
            msg = QMessageBox()
            msg.setText(f"Error saving snap frame: {str(e)}")
            msg.exec_()
        finally:
            if not was_live_before_snap:
                self.liveController.stop_live()

    def toggle_autolevel(self, autolevel_on):
        self.btn_autolevel.setChecked(autolevel_on)

    def select_new_microscope_mode_by_name(self, config_name):
        maybe_new_config = self.liveController.obs_controller.get_observation_state_by_name(config_name)
        if not maybe_new_config:
            self._log.error(f"User attempted to select config named '{config_name}' but it does not exist!")
            return
        self.liveController.obs_controller.apply_full_observation_state(maybe_new_config)
        self.update_ui_for_mode(maybe_new_config)

    def update_ui_for_mode(self, config):
        try:
            self.is_switching_mode = True
            self.currentConfiguration = config
            if self.currentConfiguration is not None:
                self.liveController.obs_controller.set_active_observation_state(self.currentConfiguration)
            if self.currentConfiguration:
                self.signal_live_configuration.emit(self.currentConfiguration)
        finally:
            self.is_switching_mode = False

    def _persist_iris_config(self, setting_name, new_value):
        if self.currentConfiguration:
            ok = self.liveController.microscope.config_repo.update_channel_setting(
                self.currentConfiguration.name,
                setting_name,
                new_value,
            )
            if not ok:
                logger.warning("Failed to persist %s value %.1f", setting_name, new_value)

    def update_config_illumination_iris(self, new_value):
        self._persist_iris_config("IlluminationIris", new_value)

    def update_config_emission_iris(self, new_value):
        self._persist_iris_config("EmissionIris", new_value)


class PiezoWidget(QFrame):
    def __init__(self, piezo: PiezoStage, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.piezo = piezo
        self.piezo_displacement_um = 0.00
        self.add_components()

    def add_components(self):
        # Row 1: Slider and Double Spin Box for direct control
        self.slider = QSlider(Qt.Horizontal, self)
        self.slider.setMinimum(0)
        self.slider.setMaximum(int(self.piezo.range_um * 100))  # Multiplied by 100 for 0.01 precision
        self.slider.setValue(int(self.piezo._home_position_um * 100))

        self.spinBox = QDoubleSpinBox(self)
        self.spinBox.setRange(0.0, self.piezo.range_um)
        self.spinBox.setDecimals(2)
        self.spinBox.setSingleStep(1)
        self.spinBox.setSuffix(" μm")
        self.spinBox.setKeyboardTracking(False)
        self.spinBox.setValue(self.piezo._home_position_um)

        # Row 3: Home Button
        self.home_btn = QPushButton(f" Set to {self.piezo._home_position_um} μm ", self)

        hbox1 = QHBoxLayout()
        hbox1.addWidget(self.home_btn)
        hbox1.addWidget(self.slider)
        hbox1.addWidget(self.spinBox)

        # Row 2: Increment Double Spin Box, Move Up and Move Down Buttons
        self.increment_spinBox = QDoubleSpinBox(self)
        self.increment_spinBox.setKeyboardTracking(False)
        self.increment_spinBox.setRange(0.0, 100.0)
        self.increment_spinBox.setDecimals(2)
        self.increment_spinBox.setSingleStep(1)
        self.increment_spinBox.setValue(1.00)
        self.increment_spinBox.setSuffix(" μm")
        self.move_up_btn = QPushButton("Move Up", self)
        self.move_down_btn = QPushButton("Move Down", self)

        hbox2 = QHBoxLayout()
        hbox2.addWidget(self.increment_spinBox)
        hbox2.addWidget(self.move_up_btn)
        hbox2.addWidget(self.move_down_btn)

        # Vertical Layout to include all HBoxes
        vbox = QVBoxLayout()
        vbox.addLayout(hbox1)
        vbox.addLayout(hbox2)

        self.setLayout(vbox)

        # Connect signals and slots
        self.slider.valueChanged.connect(self.update_from_slider)
        self.spinBox.valueChanged.connect(self.update_from_spinBox)
        self.move_up_btn.clicked.connect(lambda: self.adjust_position(True))
        self.move_down_btn.clicked.connect(lambda: self.adjust_position(False))
        self.home_btn.clicked.connect(self.home)

    def update_from_slider(self, value):
        self.piezo_displacement_um = value / 100  # Convert back to float with two decimal places
        self.update_spinBox()
        self.update_piezo_position()

    def update_from_spinBox(self, value):
        self.piezo_displacement_um = value
        self.update_slider()
        self.update_piezo_position()

    def update_spinBox(self):
        self.spinBox.blockSignals(True)
        self.spinBox.setValue(self.piezo_displacement_um)
        self.spinBox.blockSignals(False)

    def update_slider(self):
        self.slider.blockSignals(True)
        self.slider.setValue(int(self.piezo_displacement_um * 100))
        self.slider.blockSignals(False)

    def update_piezo_position(self):
        self.piezo.move_to(self.piezo_displacement_um)

    def adjust_position(self, up):
        increment = self.increment_spinBox.value()
        if up:
            self.piezo_displacement_um = min(self.piezo.range_um, self.spinBox.value() + increment)
        else:
            self.piezo_displacement_um = max(0, self.spinBox.value() - increment)
        self.update_spinBox()
        self.update_slider()
        self.update_piezo_position()

    def home(self):
        self.piezo.home()
        self.piezo_displacement_um = self.piezo._home_position_um
        self.update_spinBox()
        self.update_slider()

    def update_displacement_um_display(self, displacement=None):
        if displacement is None:
            displacement = self.piezo.position
        self.piezo_displacement_um = round(displacement, 2)
        self.update_spinBox()
        self.update_slider()


class RecordingWidget(QFrame):

    signal_acquisition_started = Signal(bool)  # true = started, false = finished
    signal_acquisition_channels = Signal(list)  # list channels
    signal_acquisition_shape = Signal(int, float)  # Nz, dz

    def __init__(self, streamHandler, imageSaver, liveController=None, main=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.imageSaver = imageSaver  # for saving path control
        self.streamHandler = streamHandler
        self.liveController = liveController
        self.base_path_is_set = False
        self._was_live_before_recording = False  # Track if live was running before recording
        self.add_components()
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)

    def add_components(self):
        self.btn_setSavingDir = QPushButton("Browse")
        self.btn_setSavingDir.setDefault(False)
        self.btn_setSavingDir.setIcon(QIcon("icon/folder.png"))

        self.lineEdit_savingDir = QLineEdit()
        self.lineEdit_savingDir.setReadOnly(True)
        self.lineEdit_savingDir.setText("Choose a base saving directory")

        self.lineEdit_savingDir.setText(DEFAULT_SAVING_PATH)
        self.imageSaver.set_base_path(DEFAULT_SAVING_PATH)
        self.base_path_is_set = True

        self.lineEdit_experimentID = QLineEdit()

        self.entry_saveFPS = QDoubleSpinBox()
        self.entry_saveFPS.setKeyboardTracking(False)
        self.entry_saveFPS.setMinimum(0.02)
        self.entry_saveFPS.setMaximum(1000)
        self.entry_saveFPS.setSingleStep(1)
        self.entry_saveFPS.setValue(1)
        self.streamHandler.set_save_fps(1)

        self.entry_timeLimit = QSpinBox()
        self.entry_timeLimit.setKeyboardTracking(False)
        self.entry_timeLimit.setMinimum(-1)
        self.entry_timeLimit.setMaximum(60 * 60 * 24 * 30)
        self.entry_timeLimit.setSingleStep(1)
        self.entry_timeLimit.setValue(-1)

        self.btn_record = QPushButton("Record")
        self.btn_record.setCheckable(True)
        self.btn_record.setChecked(False)
        self.btn_record.setDefault(False)

        grid_line1 = QGridLayout()
        grid_line1.addWidget(QLabel("Saving Path"))
        grid_line1.addWidget(self.lineEdit_savingDir, 0, 1)
        grid_line1.addWidget(self.btn_setSavingDir, 0, 2)

        grid_line2 = QGridLayout()
        grid_line2.addWidget(QLabel("Experiment ID"), 0, 0)
        grid_line2.addWidget(self.lineEdit_experimentID, 0, 1)

        grid_line3 = QGridLayout()
        grid_line3.addWidget(QLabel("Saving FPS"), 0, 0)
        grid_line3.addWidget(self.entry_saveFPS, 0, 1)
        grid_line3.addWidget(QLabel("Time Limit (s)"), 0, 2)
        grid_line3.addWidget(self.entry_timeLimit, 0, 3)

        self.grid = QVBoxLayout()
        self.grid.addLayout(grid_line1)
        self.grid.addLayout(grid_line2)
        self.grid.addLayout(grid_line3)
        self.grid.addWidget(self.btn_record)
        self.setLayout(self.grid)

        # add and display a timer - to be implemented
        # self.timer = QTimer()

        # connections
        self.btn_setSavingDir.clicked.connect(self.set_saving_dir)
        self.btn_record.clicked.connect(self.toggle_recording)
        self.entry_saveFPS.valueChanged.connect(self.streamHandler.set_save_fps)
        self.entry_timeLimit.valueChanged.connect(self.imageSaver.set_recording_time_limit)
        self.imageSaver.stop_recording.connect(self.stop_recording)

    def set_saving_dir(self):
        dialog = QFileDialog()
        save_dir_base = dialog.getExistingDirectory(None, "Select Folder", self.lineEdit_savingDir.text())
        if save_dir_base is None or save_dir_base == "":
            self.base_path_is_set = True
            return
        self.lineEdit_savingDir.setText(save_dir_base)
        self.imageSaver.set_base_path(save_dir_base)
        self.base_path_is_set = True


    def toggle_recording(self, pressed):
        if self.base_path_is_set == False:
            self.btn_record.setChecked(False)
            msg = QMessageBox()
            msg.setText("Please choose base saving directory first")
            msg.exec_()
            return
        if pressed:
            # Ensure camera is streaming - frames won't be captured if camera isn't streaming
            if self.liveController is not None:
                self._was_live_before_recording = self.liveController.is_live
                if not self.liveController.is_live:
                    self.liveController.start_live()
            else:
                self._log.error("Live controller is not set for RecordingWidget")
                return
            
            self.lineEdit_experimentID.setEnabled(False)
            self.btn_setSavingDir.setEnabled(False)
            self.imageSaver.set_base_path(self.lineEdit_savingDir.text())
            self.imageSaver.start_new_experiment(self.lineEdit_experimentID.text())
            self.streamHandler.start_recording()
            self.signal_acquisition_started.emit(True)
        else:
            self.streamHandler.stop_recording()
            # Optionally stop live if it wasn't running before recording started
            # (commented out - user might want to keep viewing)
            if self.liveController is not None and not self._was_live_before_recording:
                self.liveController.stop_live()
            self.lineEdit_experimentID.setEnabled(True)
            self.btn_setSavingDir.setEnabled(True)
            self.signal_acquisition_started.emit(False)

    # stop_recording can be called by imageSaver
    def stop_recording(self):
        self.lineEdit_experimentID.setEnabled(True)
        self.btn_record.setChecked(False)
        self.streamHandler.stop_recording()
        self.btn_setSavingDir.setEnabled(True)
        self.signal_acquisition_started.emit(False)

    def emit_selected_channels(self):
        """Emit selected channels signal. RecordingWidget doesn't use channel configurations,
        so this emits an empty list to maintain consistency with other acquisition widgets."""
        self.signal_acquisition_channels.emit([])

    def display_progress_bar(self, show):
        """Display progress bar. RecordingWidget doesn't have progress bars,
        so this is a no-op to maintain consistency with other acquisition widgets."""
        pass


class NavigationWidget(QFrame):
    def __init__(
        self,
        stage: AbstractStage,
        main=None,
        widget_configuration="full",
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.log = squid.logging.get_logger(self.__class__.__name__)
        self.stage = stage
        self.widget_configuration = widget_configuration
        self.slide_position = None
        self.add_components()
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)

        self.position_update_timer = QTimer()
        self.position_update_timer.setInterval(100)
        self.position_update_timer.timeout.connect(self._update_position)
        self.position_update_timer.start()

    def _update_position(self):
        pos = self.stage.get_pos()
        self.label_Xpos.setNum(pos.x_mm)
        self.label_Ypos.setNum(pos.y_mm)
        # NOTE: The z label is in um
        self.label_Zpos.setNum(pos.z_mm * 1000)

    def add_components(self):
        x_label = QLabel("X :")
        x_label.setFixedWidth(15)
        self.label_Xpos = QLabel()
        self.label_Xpos.setNum(0)
        self.label_Xpos.setFrameStyle(QFrame.Panel | QFrame.Sunken)
        self.entry_dX = QDoubleSpinBox()
        self.entry_dX.setMinimum(0)
        self.entry_dX.setMaximum(25)
        self.entry_dX.setSingleStep(0.2)
        self.entry_dX.setValue(0)
        self.entry_dX.setDecimals(3)
        self.entry_dX.setSuffix(" mm")
        self.entry_dX.setKeyboardTracking(False)
        self.entry_dX.setFixedWidth(70)
        self.btn_moveX_forward = QPushButton("Up")
        self.btn_moveX_forward.setDefault(False)
        self.btn_moveX_forward.setFixedWidth(55)
        self.btn_moveX_backward = QPushButton("Down")
        self.btn_moveX_backward.setDefault(False)
        self.btn_moveX_backward.setFixedWidth(55)

        self.checkbox_clickToMove = QCheckBox("Click to Move")
        self.checkbox_clickToMove.setChecked(False)
        self.checkbox_clickToMove.setSizePolicy(QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed))

        y_label = QLabel("Y :")
        y_label.setFixedWidth(15)
        self.label_Ypos = QLabel()
        self.label_Ypos.setNum(0)
        self.label_Ypos.setFrameStyle(QFrame.Panel | QFrame.Sunken)
        self.entry_dY = QDoubleSpinBox()
        self.entry_dY.setMinimum(0)
        self.entry_dY.setMaximum(25)
        self.entry_dY.setSingleStep(0.2)
        self.entry_dY.setValue(0)
        self.entry_dY.setDecimals(3)
        self.entry_dY.setSuffix(" mm")

        self.entry_dY.setKeyboardTracking(False)
        self.entry_dY.setFixedWidth(70)
        self.btn_moveY_forward = QPushButton("Up")
        self.btn_moveY_forward.setDefault(False)
        self.btn_moveY_forward.setFixedWidth(55)
        self.btn_moveY_backward = QPushButton("Down")
        self.btn_moveY_backward.setDefault(False)
        self.btn_moveY_backward.setFixedWidth(55)

        self.z_label = QLabel("Z :")
        self.z_label.setFixedWidth(15)
        self.label_Zpos = QLabel()
        self.label_Zpos.setNum(0)
        self.label_Zpos.setFrameStyle(QFrame.Panel | QFrame.Sunken)
        self.entry_dZ = QDoubleSpinBox()
        self.entry_dZ.setMinimum(0)
        self.entry_dZ.setMaximum(1000)
        self.entry_dZ.setSingleStep(0.2)
        self.entry_dZ.setValue(0)
        self.entry_dZ.setDecimals(3)
        self.entry_dZ.setSuffix(" μm")
        self.entry_dZ.setKeyboardTracking(False)
        self.entry_dZ.setFixedWidth(70)
        self.btn_moveZ_forward = QPushButton("Up")
        self.btn_moveZ_forward.setDefault(False)
        self.btn_moveZ_forward.setFixedWidth(55)
        self.btn_moveZ_backward = QPushButton("Down")
        self.btn_moveZ_backward.setDefault(False)
        self.btn_moveZ_backward.setFixedWidth(55)

        grid_line0 = QGridLayout()
        grid_line0.setHorizontalSpacing(4)
        grid_line0.setVerticalSpacing(2)
        grid_line0.addWidget(x_label, 0, 0)
        grid_line0.addWidget(self.label_Xpos, 0, 1)
        grid_line0.addWidget(self.entry_dX, 0, 2)
        grid_line0.addWidget(self.btn_moveX_forward, 0, 3)
        grid_line0.addWidget(self.btn_moveX_backward, 0, 4)

        grid_line0.addWidget(y_label, 1, 0)
        grid_line0.addWidget(self.label_Ypos, 1, 1)
        grid_line0.addWidget(self.entry_dY, 1, 2)
        grid_line0.addWidget(self.btn_moveY_forward, 1, 3)
        grid_line0.addWidget(self.btn_moveY_backward, 1, 4)

        grid_line0.addWidget(self.z_label, 2, 0)
        grid_line0.addWidget(self.label_Zpos, 2, 1)
        grid_line0.addWidget(self.entry_dZ, 2, 2)
        grid_line0.addWidget(self.btn_moveZ_forward, 2, 3)
        grid_line0.addWidget(self.btn_moveZ_backward, 2, 4)

        # Hide Z controls in piezo-only mode (Z is controlled via piezo widget)
        if IS_PIEZO_ONLY:
            self.z_label.setVisible(False)
            self.label_Zpos.setVisible(False)
            self.entry_dZ.setVisible(False)
            self.btn_moveZ_forward.setVisible(False)
            self.btn_moveZ_backward.setVisible(False)

        self.grid = QVBoxLayout()
        self.grid.addLayout(grid_line0)
        self.set_click_to_move(ENABLE_CLICK_TO_MOVE_BY_DEFAULT)
        if not ENABLE_CLICK_TO_MOVE_BY_DEFAULT:
            grid_line3 = QHBoxLayout()
            grid_line3.addWidget(self.checkbox_clickToMove, 1)
            self.grid.addLayout(grid_line3)
        self.setLayout(self.grid)

        self.entry_dX.valueChanged.connect(self.set_deltaX)
        self.entry_dY.valueChanged.connect(self.set_deltaY)
        self.entry_dZ.valueChanged.connect(self.set_deltaZ)

        self.btn_moveX_forward.clicked.connect(self.move_x_forward)
        self.btn_moveX_backward.clicked.connect(self.move_x_backward)
        self.btn_moveY_forward.clicked.connect(self.move_y_forward)
        self.btn_moveY_backward.clicked.connect(self.move_y_backward)
        self.btn_moveZ_forward.clicked.connect(self.move_z_forward)
        self.btn_moveZ_backward.clicked.connect(self.move_z_backward)

    def set_click_to_move(self, enabled):
        self.log.info(f"Click to move enabled={enabled}")
        self.setEnabled_all(enabled)
        self.checkbox_clickToMove.setChecked(enabled)

    def get_click_to_move_enabled(self):
        return self.checkbox_clickToMove.isChecked()

    def setEnabled_all(self, enabled):
        self.checkbox_clickToMove.setEnabled(enabled)
        self.btn_moveX_forward.setEnabled(enabled)
        self.btn_moveX_backward.setEnabled(enabled)
        self.btn_moveY_forward.setEnabled(enabled)
        self.btn_moveY_backward.setEnabled(enabled)
        self.btn_moveZ_forward.setEnabled(enabled)
        self.btn_moveZ_backward.setEnabled(enabled)

    def move_x_forward(self):
        self.stage.move_x(self.entry_dX.value())

    def move_x_backward(self):
        self.stage.move_x(-self.entry_dX.value())

    def move_y_forward(self):
        self.stage.move_y(self.entry_dY.value())

    def move_y_backward(self):
        self.stage.move_y(-self.entry_dY.value())

    def move_z_forward(self):
        self.stage.move_z(self.entry_dZ.value() / 1000)

    def move_z_backward(self):
        self.stage.move_z(-self.entry_dZ.value() / 1000)

    def set_deltaX(self, value):
        mm_per_ustep = 1.0 / self.stage.x_mm_to_usteps(1.0)
        deltaX = round(value / mm_per_ustep) * mm_per_ustep
        self.entry_dX.setValue(deltaX)

    def set_deltaY(self, value):
        mm_per_ustep = 1.0 / self.stage.y_mm_to_usteps(1.0)
        deltaY = round(value / mm_per_ustep) * mm_per_ustep
        self.entry_dY.setValue(deltaY)

    def set_deltaZ(self, value):
        mm_per_ustep = 1.0 / self.stage.z_mm_to_usteps(1.0)
        deltaZ = round(value / 1000 / mm_per_ustep) * mm_per_ustep * 1000
        self.entry_dZ.setValue(deltaZ)


class DACControWidget(QFrame):
    def __init__(self, microcontroller, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.microcontroller = microcontroller
        self.add_components()
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)

    def add_components(self):
        self.slider_DAC0 = QSlider(Qt.Horizontal)
        self.slider_DAC0.setTickPosition(QSlider.TicksBelow)
        self.slider_DAC0.setMinimum(0)
        self.slider_DAC0.setMaximum(100)
        self.slider_DAC0.setSingleStep(1)
        self.slider_DAC0.setValue(0)

        self.entry_DAC0 = QDoubleSpinBox()
        self.entry_DAC0.setMinimum(0)
        self.entry_DAC0.setMaximum(100)
        self.entry_DAC0.setSingleStep(0.1)
        self.entry_DAC0.setValue(0)
        self.entry_DAC0.setKeyboardTracking(False)

        self.slider_DAC1 = QSlider(Qt.Horizontal)
        self.slider_DAC1.setTickPosition(QSlider.TicksBelow)
        self.slider_DAC1.setMinimum(0)
        self.slider_DAC1.setMaximum(100)
        self.slider_DAC1.setValue(0)
        self.slider_DAC1.setSingleStep(1)

        self.entry_DAC1 = QDoubleSpinBox()
        self.entry_DAC1.setMinimum(0)
        self.entry_DAC1.setMaximum(100)
        self.entry_DAC1.setSingleStep(0.1)
        self.entry_DAC1.setValue(0)
        self.entry_DAC1.setKeyboardTracking(False)

        # connections
        self.entry_DAC0.valueChanged.connect(self.set_DAC0)
        self.entry_DAC0.valueChanged.connect(self.slider_DAC0.setValue)
        self.slider_DAC0.valueChanged.connect(self.entry_DAC0.setValue)
        self.entry_DAC1.valueChanged.connect(self.set_DAC1)
        self.entry_DAC1.valueChanged.connect(self.slider_DAC1.setValue)
        self.slider_DAC1.valueChanged.connect(self.entry_DAC1.setValue)

        # layout
        grid_line1 = QHBoxLayout()
        grid_line1.addWidget(QLabel("DAC0"))
        grid_line1.addWidget(self.slider_DAC0)
        grid_line1.addWidget(self.entry_DAC0)
        grid_line1.addWidget(QLabel("DAC1"))
        grid_line1.addWidget(self.slider_DAC1)
        grid_line1.addWidget(self.entry_DAC1)

        self.grid = QGridLayout()
        self.grid.addLayout(grid_line1, 1, 0)
        self.setLayout(self.grid)

    def set_DAC0(self, value):
        self.microcontroller.analog_write_onboard_DAC(0, round(value * 65535 / 100))

    def set_DAC1(self, value):
        self.microcontroller.analog_write_onboard_DAC(1, round(value * 65535 / 100))


class AutoFocusWidget(QFrame):
    signal_autoLevelSetting = Signal(bool)

    def __init__(self, autofocusController, main=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.autofocusController = autofocusController
        self.log = squid.logging.get_logger(self.__class__.__name__)
        self.add_components()
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)
        self.stage = self.autofocusController.stage

    def add_components(self):
        self.entry_delta = QDoubleSpinBox()
        self.entry_delta.setMinimum(0)
        self.entry_delta.setMaximum(20)
        self.entry_delta.setSingleStep(0.2)
        self.entry_delta.setDecimals(3)
        self.entry_delta.setSuffix(" μm")
        self.entry_delta.setValue(1.524)
        self.entry_delta.setKeyboardTracking(False)
        self.entry_delta.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.autofocusController.set_deltaZ(1.524)

        self.entry_N = QSpinBox()
        self.entry_N.setMinimum(3)
        self.entry_N.setMaximum(10000)
        self.entry_N.setFixedWidth(self.entry_N.sizeHint().width())
        self.entry_N.setMaximum(20)
        self.entry_N.setSingleStep(1)
        self.entry_N.setValue(10)
        self.entry_N.setKeyboardTracking(False)
        self.entry_N.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.autofocusController.set_N(10)

        self.btn_autofocus = QPushButton("Autofocus")
        self.btn_autofocus.setDefault(False)
        self.btn_autofocus.setCheckable(True)
        self.btn_autofocus.setChecked(False)

        self.btn_autolevel = QPushButton("Autolevel")
        self.btn_autolevel.setCheckable(True)
        self.btn_autolevel.setChecked(False)
        self.btn_autolevel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # layout
        self.grid = QVBoxLayout()
        grid_line0 = QHBoxLayout()
        grid_line0.addWidget(QLabel("\u0394 Z"))
        grid_line0.addWidget(self.entry_delta)
        grid_line0.addSpacing(20)
        grid_line0.addWidget(QLabel("# of Z-Planes"))
        grid_line0.addWidget(self.entry_N)
        grid_line0.addSpacing(20)
        grid_line0.addWidget(self.btn_autolevel)

        self.grid.addLayout(grid_line0)
        self.grid.addWidget(self.btn_autofocus)
        self.setLayout(self.grid)

        # connections
        self.btn_autofocus.toggled.connect(lambda: self.autofocusController.autofocus(False))
        self.btn_autolevel.toggled.connect(self.signal_autoLevelSetting.emit)
        self.entry_delta.valueChanged.connect(self.set_deltaZ)
        self.entry_N.valueChanged.connect(self.autofocusController.set_N)
        self.autofocusController.autofocusFinished.connect(self.autofocus_is_finished)

    def set_deltaZ(self, value):
        mm_per_ustep = 1.0 / self.stage.get_config().Z_AXIS.convert_real_units_to_ustep(1.0)
        deltaZ = round(value / 1000 / mm_per_ustep) * mm_per_ustep * 1000
        self.log.debug(f"{deltaZ=}")

        self.entry_delta.setValue(deltaZ)
        self.autofocusController.set_deltaZ(deltaZ)

    def autofocus_is_finished(self):
        self.btn_autofocus.setChecked(False)



class StatsDisplayWidget(QFrame):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initUI()
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)

    def initUI(self):
        self.layout = QVBoxLayout()
        self.table_widget = QTableWidget()
        self.table_widget.setColumnCount(2)
        self.table_widget.verticalHeader().hide()
        self.table_widget.horizontalHeader().hide()
        self.table_widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.layout.addWidget(self.table_widget)
        self.setLayout(self.layout)

    def display_stats(self, stats):
        print("displaying parasite stats")
        locale.setlocale(locale.LC_ALL, "")
        self.table_widget.setRowCount(len(stats))
        row = 0
        for key, value in stats.items():
            key_item = QTableWidgetItem(str(key))
            value_item = None
            try:
                value_item = QTableWidgetItem(f"{value:n}")
            except:
                value_item = QTableWidgetItem(str(value))
            self.table_widget.setItem(row, 0, key_item)
            self.table_widget.setItem(row, 1, value_item)
            row += 1


class _WellShapeDelegate(QStyledItemDelegate):
    """Paints a circle or rectangle inside each cell matching the well shape.

    Qt's default cell rendering draws square cells only, which misrepresents
    round wells. This delegate overlays the correct silhouette on top of the
    standard background (so selection highlighting still works).
    """

    def __init__(self, parent, widget):
        super().__init__(parent)
        self._widget = widget

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if not (index.flags() & Qt.ItemIsSelectable):
            return  # Skipped wells stay blank.
        shape = getattr(self._widget, "well_shape", "circle")
        rect = option.rect.adjusted(2, 2, -2, -2)
        if rect.width() <= 0 or rect.height() <= 0:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor("#444444"))
        pen.setWidth(1)
        painter.setPen(pen)
        if option.state & QStyle.State_Selected:
            painter.setBrush(option.palette.highlight())
        else:
            painter.setBrush(QColor("white"))
        if shape == "rectangle":
            corner_px = max(0, int(min(rect.width(), rect.height()) * 0.08))
            painter.drawRoundedRect(rect, corner_px, corner_px)
        else:
            # Inscribe the well in a square inside the cell.
            side = min(rect.width(), rect.height())
            cx = rect.center().x()
            cy = rect.center().y()
            painter.drawEllipse(cx - side // 2, cy - side // 2, side, side)
        painter.restore()


class WellSelectionWidget(QTableWidget):
    signal_wellSelected = Signal(bool)
    signal_wellSelectedPos = Signal(float, float)

    def __init__(self, format_, wellplateFormatWidget, *args, **kwargs):
        super(WellSelectionWidget, self).__init__(*args, **kwargs)
        self.wellplateFormatWidget = wellplateFormatWidget
        self.cellDoubleClicked.connect(self.onDoubleClick)
        self.itemSelectionChanged.connect(self.onSelectionChanged)
        self.fixed_height = 400
        self._shape_delegate = _WellShapeDelegate(self, self)
        self.setItemDelegate(self._shape_delegate)
        self.setFormat(format_)

    def setFormat(self, format_):
        self.format = format_
        settings = self.wellplateFormatWidget.getWellplateSettings(self.format)
        self.rows = settings["rows"]
        self.columns = settings["cols"]
        self.spacing_mm = settings["well_spacing_mm"]
        self.number_of_skip = settings["number_of_skip"]
        self.a1_x_mm = settings["a1_x_mm"]
        self.a1_y_mm = settings["a1_y_mm"]
        self.a1_x_pixel = settings["a1_x_pixel"]
        self.a1_y_pixel = settings["a1_y_pixel"]
        self.well_size_mm = settings["well_size_mm"]
        self.well_shape = settings.get("well_shape", "circle")

        self.setRowCount(self.rows)
        self.setColumnCount(self.columns)
        self.initUI()
        self.setData()

    def initUI(self):
        # Disable editing, scrollbars, and other interactions
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.verticalScrollBar().setDisabled(True)
        self.horizontalScrollBar().setDisabled(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setTabKeyNavigation(False)
        self.setDragEnabled(False)
        self.setAcceptDrops(False)
        self.setDragDropOverwriteMode(False)
        self.setMouseTracking(False)

        if self.format == "1536 well plate":
            font = QFont()
            font.setPointSize(6)  # You can adjust this value as needed
        else:
            font = QFont()
        self.horizontalHeader().setFont(font)
        self.verticalHeader().setFont(font)

        self.setLayout()

    def setLayout(self):
        # Calculate available space and cell size
        header_height = self.horizontalHeader().height()
        available_height = self.fixed_height - header_height  # Fixed height of 408 pixels

        # Calculate cell size based on the minimum of available height and width
        cell_size = available_height // self.rowCount()

        self.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.verticalHeader().setDefaultSectionSize(cell_size)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.horizontalHeader().setDefaultSectionSize(cell_size)

        # Ensure sections do not resize
        self.verticalHeader().setMinimumSectionSize(cell_size)
        self.verticalHeader().setMaximumSectionSize(cell_size)
        self.horizontalHeader().setMinimumSectionSize(cell_size)
        self.horizontalHeader().setMaximumSectionSize(cell_size)

        row_header_width = self.verticalHeader().width()

        # Calculate total width and height
        total_height = (self.rowCount() * cell_size) + header_height
        total_width = (self.columnCount() * cell_size) + row_header_width

        # Set the widget's fixed size
        self.setFixedHeight(total_height)
        self.setFixedWidth(total_width)

        # Force the widget to update its layout
        self.updateGeometry()
        self.viewport().update()

    def onWellplateChanged(self):
        self.setFormat(self.wellplateFormatWidget.wellplate_format)

    def setData(self):
        for i in range(self.rowCount()):
            for j in range(self.columnCount()):
                item = self.item(i, j)
                if not item:  # Create a new item if none exists
                    item = QTableWidgetItem()
                    self.setItem(i, j, item)
                # Reset to selectable by default
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)

        if self.number_of_skip > 0 and self.format != 0:
            for i in range(self.number_of_skip):
                for j in range(self.columns):  # Apply to rows
                    self.item(i, j).setFlags(self.item(i, j).flags() & ~Qt.ItemIsSelectable)
                    self.item(self.rows - 1 - i, j).setFlags(
                        self.item(self.rows - 1 - i, j).flags() & ~Qt.ItemIsSelectable
                    )
                for k in range(self.rows):  # Apply to columns
                    self.item(k, i).setFlags(self.item(k, i).flags() & ~Qt.ItemIsSelectable)
                    self.item(k, self.columns - 1 - i).setFlags(
                        self.item(k, self.columns - 1 - i).flags() & ~Qt.ItemIsSelectable
                    )

        # Update row headers
        row_headers = []
        for i in range(self.rows):
            if i < 26:
                label = chr(ord("A") + i)
            else:
                first_letter = chr(ord("A") + (i // 26) - 1)
                second_letter = chr(ord("A") + (i % 26))
                label = first_letter + second_letter
            row_headers.append(label)
        self.setVerticalHeaderLabels(row_headers)

        # Adjust vertical header width after setting labels
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)

    def onDoubleClick(self, row, col):
        print("double click well", row, col)
        if (row >= 0 + self.number_of_skip and row <= self.rows - 1 - self.number_of_skip) and (
            col >= 0 + self.number_of_skip and col <= self.columns - 1 - self.number_of_skip
        ):
            x_mm = col * self.spacing_mm + self.a1_x_mm + WELLPLATE_OFFSET_X_mm
            y_mm = row * self.spacing_mm + self.a1_y_mm + WELLPLATE_OFFSET_Y_mm
            self.signal_wellSelectedPos.emit(x_mm, y_mm)
            print("well location:", (x_mm, y_mm))
            self.signal_wellSelected.emit(True)
        else:
            self.signal_wellSelected.emit(False)

    def onSingleClick(self, row, col):
        print("single click well", row, col)
        if (row >= 0 + self.number_of_skip and row <= self.rows - 1 - self.number_of_skip) and (
            col >= 0 + self.number_of_skip and col <= self.columns - 1 - self.number_of_skip
        ):
            self.signal_wellSelected.emit(True)
        else:
            self.signal_wellSelected.emit(False)

    def onSelectionChanged(self):
        # Check if there are any selected indexes before proceeding
        if self.format != "glass slide":
            has_selection = bool(self.selectedIndexes())
            self.signal_wellSelected.emit(has_selection)

    def get_selected_cells(self):
        list_of_selected_cells = []
        print("getting selected cells...")
        if self.format == "glass slide":
            return list_of_selected_cells
        for index in self.selectedIndexes():
            row, col = index.row(), index.column()
            # Check if the cell is within the allowed bounds
            if (row >= 0 + self.number_of_skip and row <= self.rows - 1 - self.number_of_skip) and (
                col >= 0 + self.number_of_skip and col <= self.columns - 1 - self.number_of_skip
            ):
                list_of_selected_cells.append((row, col))
        if list_of_selected_cells:
            print("cells:", list_of_selected_cells)
        else:
            print("no cells")
        return list_of_selected_cells

    def resizeEvent(self, event):
        self.initUI()
        super().resizeEvent(event)

    def wheelEvent(self, event):
        # Ignore wheel events to prevent scrolling
        event.ignore()

    def scrollTo(self, index, hint=QAbstractItemView.EnsureVisible):
        pass

    def set_white_boundaries_style(self):
        style = """
        QTableWidget {
            gridline-color: white;
            border: 1px solid white;
        }
        QHeaderView::section {
            color: white;
        }
        """
        self.setStyleSheet(style)


