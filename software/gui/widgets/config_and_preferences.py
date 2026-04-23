from ._bootstrap import *

class CollapsibleGroupBox(QWidget):
    """A collapsible group box with arrow indicator for expand/collapse."""

    def __init__(self, title, collapsed=False):
        super().__init__()
        self._collapsed = collapsed
        self._title = title

        # Main layout
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 8)
        main_layout.setSpacing(0)

        # Header button with arrow
        self._header = QPushButton()
        self._header.setStyleSheet(
            """
            QPushButton {
                text-align: left;
                padding: 8px;
                font-weight: bold;
                background-color: palette(button);
                border: 1px solid palette(mid);
                border-bottom: none;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
            }
            QPushButton:hover {
                background-color: palette(light);
            }
            """
        )
        self._header.clicked.connect(self._toggle)
        main_layout.addWidget(self._header)

        # Content widget with border to show grouping
        self.content_widget = QFrame()
        self.content_widget.setObjectName("collapsibleContent")
        self.content_widget.setFrameShape(QFrame.StyledPanel)
        self.content_widget.setStyleSheet(
            """
            QFrame#collapsibleContent {
                border: 1px solid palette(mid);
                border-top: none;
                border-bottom-left-radius: 4px;
                border-bottom-right-radius: 4px;
                background-color: palette(base);
            }
            QFrame#collapsibleContent QLabel {
                border: none;
                background: transparent;
            }
            """
        )
        self.content = QVBoxLayout(self.content_widget)
        self.content.setContentsMargins(15, 10, 10, 10)
        main_layout.addWidget(self.content_widget)

        # Set initial state
        self._update_header()
        self.content_widget.setVisible(not collapsed)

    def _update_header(self):
        arrow = "▼" if not self._collapsed else "▶"
        self._header.setText(f"{arrow}  {self._title}")

    def _toggle(self):
        self._collapsed = not self._collapsed
        self._update_header()
        self.content_widget.setVisible(not self._collapsed)

    def setCollapsed(self, collapsed):
        """Programmatically set collapsed state."""
        if self._collapsed != collapsed:
            self._collapsed = collapsed
            self._update_header()
            self.content_widget.setVisible(not collapsed)

    def isCollapsed(self):
        """Return current collapsed state."""
        return self._collapsed


class ConfigEditor(QDialog):
    def __init__(self, config):
        super().__init__()
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self.config = config

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area_widget = QWidget()
        self.scroll_area_layout = QVBoxLayout()
        self.scroll_area_widget.setLayout(self.scroll_area_layout)
        self.scroll_area.setWidget(self.scroll_area_widget)

        self.save_config_button = QPushButton("Save Config")
        self.save_config_button.clicked.connect(self.save_config)
        self.save_to_file_button = QPushButton("Save to File")
        self.save_to_file_button.clicked.connect(self.save_to_file)
        self.load_config_button = QPushButton("Load Config from File")
        self.load_config_button.clicked.connect(self.load_config_from_file)

        layout = QVBoxLayout()
        layout.addWidget(self.scroll_area)
        layout.addWidget(self.save_config_button)
        layout.addWidget(self.save_to_file_button)
        layout.addWidget(self.load_config_button)

        self.config_value_widgets = {}

        self.setLayout(layout)
        self.setWindowTitle("Configuration Editor")
        self.init_ui()

    def init_ui(self):
        self.groups = {}
        for section in self.config.sections():
            group_box = CollapsibleGroupBox(section)
            group_layout = QVBoxLayout()

            section_value_widgets = {}

            self.groups[section] = group_box

            for option in self.config.options(section):
                if option.startswith("_") and option.endswith("_options"):
                    continue
                option_value = self.config.get(section, option)
                option_name = QLabel(option)
                option_layout = QHBoxLayout()
                option_layout.addWidget(option_name)
                if f"_{option}_options" in self.config.options(section):
                    option_value_list = self.config.get(section, f"_{option}_options")
                    values = option_value_list.strip("[]").split(",")
                    for i in range(len(values)):
                        values[i] = values[i].strip()
                    if option_value not in values:
                        values.append(option_value)
                    combo_box = QComboBox()
                    combo_box.addItems(values)
                    combo_box.setCurrentText(option_value)
                    option_layout.addWidget(combo_box)
                    section_value_widgets[option] = combo_box
                else:
                    option_input = QLineEdit(option_value)
                    option_layout.addWidget(option_input)
                    section_value_widgets[option] = option_input
                group_layout.addLayout(option_layout)

            self.config_value_widgets[section] = section_value_widgets
            group_box.content.addLayout(group_layout)
            self.scroll_area_layout.addWidget(group_box)

    def save_config(self):
        for section in self.config.sections():
            for option in self.config.options(section):
                if option.startswith("_") and option.endswith("_options"):
                    continue
                old_val = self.config.get(section, option)
                widget = self.config_value_widgets[section][option]
                if type(widget) is QLineEdit:
                    self.config.set(section, option, widget.text())
                else:
                    self.config.set(section, option, widget.currentText())
                if old_val != self.config.get(section, option):
                    print(self.config.get(section, option))

    def save_to_filename(self, filename: str):
        try:
            with open(filename, "w") as configfile:
                self.config.write(configfile)
                return True
        except IOError:
            self._log.exception(f"Failed to write config file to '{filename}'")
            return False

    def save_to_file(self):
        self.save_config()
        file_path, _ = QFileDialog.getSaveFileName(self, "Save Config File", "", "INI Files (*.ini);;All Files (*)")
        if file_path:
            if not self.save_to_filename(file_path):
                QMessageBox.warning(
                    self, "Warning", f"Failed to write config file to '{file_path}'.  Check permissions!"
                )

    def load_config_from_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Load Config File", "", "INI Files (*.ini);;All Files (*)")
        if file_path:
            self.config.read(file_path)
            # Clear and re-initialize the UI
            self.scroll_area_widget.deleteLater()
            self.scroll_area_widget = QWidget()
            self.scroll_area_layout = QVBoxLayout()
            self.scroll_area_widget.setLayout(self.scroll_area_layout)
            self.scroll_area.setWidget(self.scroll_area_widget)
            self.init_ui()


class ConfigEditorBackwardsCompatible(ConfigEditor):
    def __init__(self, config, original_filepath, main_window):
        super().__init__(config)
        self.original_filepath = original_filepath
        self.main_window = main_window

        self.apply_exit_button = QPushButton("Apply and Exit")
        self.apply_exit_button.clicked.connect(self.apply_and_exit)

        self.layout().addWidget(self.apply_exit_button)

    def apply_and_exit(self):
        self.save_config()
        with open(self.original_filepath, "w") as configfile:
            self.config.write(configfile)
        try:
            self.main_window.close()
        except (AttributeError, RuntimeError):
            # main_window may be None or already closed
            pass
        self.close()


class AcquisitionYAMLDropMixin:
    """Mixin class providing drag-and-drop functionality for loading acquisition YAML files.

    Widgets using this mixin must:
    1. Call `self.setAcceptDrops(True)` in __init__
    2. Have `self._log`, `self.multipointController`, `self.objectiveStore` attributes
    3. Implement `_get_expected_widget_type()` returning "wellplate" or "flexible"
    4. Implement `_apply_yaml_settings(yaml_data)` to apply settings to the widget
    """

    def _is_valid_yaml_drop(self, file_path: str) -> bool:
        """Check if the path is a valid YAML file or a folder containing acquisition.yaml."""
        if file_path.endswith(".yaml") or file_path.endswith(".yml"):
            return True
        # Check if it's a directory containing acquisition.yaml
        if os.path.isdir(file_path):
            yaml_path = os.path.join(file_path, "acquisition.yaml")
            if os.path.isfile(yaml_path):
                return True
        return False

    def _resolve_yaml_path(self, file_path: str) -> str:
        """Resolve the actual YAML file path from a file or folder."""
        if file_path.endswith(".yaml") or file_path.endswith(".yml"):
            return file_path
        # Check if it's a directory containing acquisition.yaml
        if os.path.isdir(file_path):
            yaml_path = os.path.join(file_path, "acquisition.yaml")
            if os.path.isfile(yaml_path):
                return yaml_path
        return file_path

    def dragEnterEvent(self, event):
        """Handle drag enter event for YAML file or folder drops."""
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                file_path = url.toLocalFile()
                if self._is_valid_yaml_drop(file_path):
                    event.accept()
                    # Visual feedback - dashed border (store original for restore)
                    if not hasattr(self, "_original_stylesheet"):
                        self._original_stylesheet = self.styleSheet()
                    self.setStyleSheet(
                        self._original_stylesheet + f" {self.__class__.__name__} {{ border: 3px dashed #4a90d9; }}"
                    )
                    return
        event.ignore()

    def dragLeaveEvent(self, event):
        """Handle drag leave event."""
        if hasattr(self, "_original_stylesheet"):
            self.setStyleSheet(self._original_stylesheet)
        event.accept()

    def dropEvent(self, event):
        """Handle drop event for YAML file or folder."""
        if hasattr(self, "_original_stylesheet"):
            self.setStyleSheet(self._original_stylesheet)
        paths = [u.toLocalFile() for u in event.mimeData().urls()]
        yaml_paths = [self._resolve_yaml_path(p) for p in paths if self._is_valid_yaml_drop(p)]
        if yaml_paths:
            if len(yaml_paths) > 1 and hasattr(self, "_log"):
                self._log.warning(
                    "Multiple YAML files/folders dropped (%d). Only loading the first: %s",
                    len(yaml_paths),
                    yaml_paths[0],
                )
            self._load_acquisition_yaml(yaml_paths[0])
        event.accept()

    def _get_expected_widget_type(self) -> str:
        """Return the expected widget_type for this widget. Override in subclass."""
        raise NotImplementedError("Subclass must implement _get_expected_widget_type()")

    def _get_other_widget_name(self) -> str:
        """Return the name of the other widget type for error messages."""
        if self._get_expected_widget_type() == "wellplate":
            return "Flexible Multipoint"
        return "Wellplate Multipoint"

    def _load_acquisition_yaml(self, file_path: str) -> bool:
        """Load acquisition settings from YAML file.

        Returns:
            True if settings were loaded successfully, False otherwise.
        """
        from control.acquisition_yaml_loader import parse_acquisition_yaml, validate_hardware

        try:
            yaml_data = parse_acquisition_yaml(file_path)
        except Exception as e:
            QMessageBox.warning(self, "Load Error", f"Failed to parse YAML file:\n{e}")
            return False

        # Check widget type
        expected_type = self._get_expected_widget_type()
        if yaml_data.widget_type != expected_type:
            QMessageBox.warning(
                self,
                "Widget Type Mismatch",
                f"This YAML is for '{yaml_data.widget_type}' mode.\n"
                f"Please drop this file on the {self._get_other_widget_name()} widget instead.",
            )
            return False

        # Validate hardware
        current_binning = (1, 1)
        try:
            camera = getattr(self.multipointController, "camera", None)
            if camera and hasattr(camera, "get_binning"):
                current_binning = tuple(camera.get_binning())
        except Exception as e:
            self._log.warning(
                "Could not get camera binning for validation; using default %s: %s",
                current_binning,
                e,
            )

        validation = validate_hardware(yaml_data, self.objectiveStore.current_objective, current_binning)

        if not validation.is_valid:
            dialog = AcquisitionYAMLMismatchDialog(validation, self)
            dialog.exec_()
            return False

        # Apply settings with signal blocking
        self._apply_yaml_settings(yaml_data)
        self._log.info(f"Loaded acquisition settings from: {file_path}")
        return True

    def _apply_yaml_settings(self, yaml_data):
        """Apply parsed YAML settings to widget controls. Override in subclass."""
        raise NotImplementedError("Subclass must implement _apply_yaml_settings()")


class AcquisitionYAMLMismatchDialog(QDialog):
    """Dialog shown when hardware configuration doesn't match loaded YAML settings."""

    def __init__(self, validation_result, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cannot Load Settings")
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)

        # Warning icon and title
        title_layout = QHBoxLayout()
        icon_label = QLabel()
        icon_label.setPixmap(self.style().standardIcon(QStyle.SP_MessageBoxWarning).pixmap(32, 32))
        title_layout.addWidget(icon_label)
        title_label = QLabel("<b>Hardware Configuration Mismatch</b>")
        title_label.setStyleSheet("font-size: 14px;")
        title_layout.addWidget(title_label)
        title_layout.addStretch()
        layout.addLayout(title_layout)

        layout.addSpacing(10)

        # Mismatch details
        message_label = QLabel(validation_result.message)
        message_label.setWordWrap(True)
        message_label.setStyleSheet("background-color: #fff3cd; padding: 10px; border-radius: 4px;")
        layout.addWidget(message_label)

        layout.addSpacing(10)

        # Instructions
        instruction_label = QLabel(
            "Please update your hardware settings to match the YAML file, then drag and drop again."
        )
        instruction_label.setWordWrap(True)
        instruction_label.setStyleSheet("color: #666;")
        layout.addWidget(instruction_label)

        layout.addSpacing(15)

        # OK button
        button_layout = QHBoxLayout()
        ok_btn = QPushButton("OK")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        button_layout.addStretch()
        button_layout.addWidget(ok_btn)
        layout.addLayout(button_layout)


class PreferencesDialog(QDialog):
    """User-friendly preferences dialog with tabbed interface for common settings."""

    signal_config_changed = Signal()

    def __init__(self, config, config_filepath, parent=None, on_restart=None):
        super().__init__(parent)
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self.config = config
        self.config_filepath = config_filepath
        self._on_restart = on_restart  # Optional callback for application restart
        self.setWindowTitle("Preferences")
        self.setMinimumWidth(500)
        self.setMinimumHeight(600)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        # Tab widget
        self.tab_widget = QTabWidget()
        layout.addWidget(self.tab_widget)

        # Create tabs
        self._create_general_tab()
        self._create_acquisition_tab()
        self._create_camera_tab()
        self._create_views_tab()
        self._create_advanced_tab()
        self._create_development_tab()

        # Buttons
        button_layout = QHBoxLayout()
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self._save_and_close)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)

        button_layout.addStretch()
        button_layout.addWidget(self.save_button)
        button_layout.addWidget(self.cancel_button)
        layout.addLayout(button_layout)

    def _create_general_tab(self):
        tab = QWidget()
        layout = QFormLayout(tab)
        layout.setSpacing(10)

        # File Saving Format
        self.file_saving_combo = QComboBox()
        self.file_saving_combo.addItems([e.name for e in FileSavingOption])
        current_value = self._get_config_value("GENERAL", "file_saving_option", "OME_TIFF")
        self.file_saving_combo.setCurrentText(current_value)
        layout.addRow("File Saving Format:", self.file_saving_combo)

        # Zarr Compression (only visible when ZARR_V3 is selected)
        self.zarr_compression_combo = QComboBox()
        self.zarr_compression_combo.addItems(["none", "fast", "balanced", "best"])
        self.zarr_compression_combo.setToolTip(
            "none: No compression, maximum speed (~2x faster than TIFF)\n"
            "fast: blosc-lz4, ~1000 MB/s, ~2x compression (default)\n"
            "balanced: blosc-zstd level 3, ~500 MB/s, ~3-4x compression\n"
            "best: blosc-zstd level 9, slower but best compression"
        )
        zarr_compression_value = self._get_config_value("GENERAL", "zarr_compression", "balanced")
        self.zarr_compression_combo.setCurrentText(zarr_compression_value)
        self.zarr_compression_label = QLabel("Zarr Compression:")
        layout.addRow(self.zarr_compression_label, self.zarr_compression_combo)

        # Show/hide zarr options based on file saving format selection
        self._update_zarr_options_visibility()
        self.file_saving_combo.currentTextChanged.connect(self._update_zarr_options_visibility)

        # Default Saving Path
        path_widget = QWidget()
        path_layout = QHBoxLayout(path_widget)
        path_layout.setContentsMargins(0, 0, 0, 0)
        self.saving_path_edit = QLineEdit()
        self.saving_path_edit.setText(
            self._get_config_value("GENERAL", "default_saving_path", control._def.DEFAULT_SAVING_PATH)
        )
        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self._browse_saving_path)
        path_layout.addWidget(self.saving_path_edit)
        path_layout.addWidget(browse_button)
        layout.addRow("Default Saving Path:", path_widget)

        self.tab_widget.addTab(tab, "General")

    def _create_acquisition_tab(self):
        tab = QWidget()
        layout = QFormLayout(tab)
        layout.setSpacing(10)

        # Autofocus Channel
        self.autofocus_channel_edit = QLineEdit()
        self.autofocus_channel_edit.setText(
            self._get_config_value("GENERAL", "multipoint_autofocus_channel", "BF LED matrix full")
        )
        layout.addRow("Autofocus Channel:", self.autofocus_channel_edit)

        # Enable Flexible Multipoint
        self.flexible_multipoint_checkbox = QCheckBox()
        self.flexible_multipoint_checkbox.setChecked(
            self._get_config_bool("GENERAL", "enable_flexible_multipoint", True)
        )
        layout.addRow("Enable Flexible Multipoint:", self.flexible_multipoint_checkbox)

        self.tab_widget.addTab(tab, "Acquisition")

    def _create_camera_tab(self):
        tab = QWidget()
        layout = QFormLayout(tab)
        layout.setSpacing(10)

        # Restart warning label
        restart_label = QLabel("Note: Camera settings require software restart to take effect.")
        restart_label.setStyleSheet("color: #666; font-style: italic;")
        layout.addRow(restart_label)

        # Default Binning Factor
        self.binning_spinbox = QSpinBox()
        self.binning_spinbox.setRange(1, 4)
        self.binning_spinbox.setValue(self._get_config_int("CAMERA_CONFIG", "binning_factor_default", 2))
        layout.addRow("Default Binning Factor:", self.binning_spinbox)

        # Image Flip
        self.flip_combo = QComboBox()
        self.flip_combo.addItems(["None", "Vertical", "Horizontal", "Both"])
        current_flip = self._get_config_value("CAMERA_CONFIG", "flip_image", "None")
        self.flip_combo.setCurrentText(current_flip)
        layout.addRow("Image Flip:", self.flip_combo)

        # Temperature Default
        self.temperature_spinbox = QSpinBox()
        self.temperature_spinbox.setRange(-20, 40)
        self.temperature_spinbox.setValue(self._get_config_int("CAMERA_CONFIG", "temperature_default", 20))
        self.temperature_spinbox.setSuffix(" °C")
        layout.addRow("Temperature Default:", self.temperature_spinbox)

        # ROI Width
        self.roi_width_spinbox = QSpinBox()
        self.roi_width_spinbox.setRange(0, 10000)
        self.roi_width_spinbox.setSpecialValueText("Auto")
        roi_width = self._get_config_value("CAMERA_CONFIG", "roi_width_default", "None")
        if roi_width == "None":
            self.roi_width_spinbox.setValue(0)
        else:
            try:
                self.roi_width_spinbox.setValue(int(roi_width))
            except ValueError:
                self._log.warning(f"Invalid roi_width_default value '{roi_width}', using Auto")
                self.roi_width_spinbox.setValue(0)
        layout.addRow("ROI Width:", self.roi_width_spinbox)

        # ROI Height
        self.roi_height_spinbox = QSpinBox()
        self.roi_height_spinbox.setRange(0, 10000)
        self.roi_height_spinbox.setSpecialValueText("Auto")
        roi_height = self._get_config_value("CAMERA_CONFIG", "roi_height_default", "None")
        if roi_height == "None":
            self.roi_height_spinbox.setValue(0)
        else:
            try:
                self.roi_height_spinbox.setValue(int(roi_height))
            except ValueError:
                self._log.warning(f"Invalid roi_height_default value '{roi_height}', using Auto")
                self.roi_height_spinbox.setValue(0)
        layout.addRow("ROI Height:", self.roi_height_spinbox)

        self.tab_widget.addTab(tab, "Camera")

    def _create_advanced_tab(self):
        tab = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea { background-color: palette(light); border: none; }"
            "QScrollArea > QWidget > QWidget { background-color: palette(light); }"
        )
        scroll_content = QWidget()
        layout = QVBoxLayout(scroll_content)

        # Stage & Motion section (requires restart)
        stage_group = CollapsibleGroupBox("Stage && Motion *", collapsed=True)
        stage_layout = QFormLayout()

        self.max_vel_x = QDoubleSpinBox()
        self.max_vel_x.setRange(0.1, 100)
        self.max_vel_x.setValue(self._get_config_float("GENERAL", "max_velocity_x_mm", 30))
        self.max_vel_x.setSuffix(" mm/s")
        stage_layout.addRow("Max Velocity X:", self.max_vel_x)

        self.max_vel_y = QDoubleSpinBox()
        self.max_vel_y.setRange(0.1, 100)
        self.max_vel_y.setValue(self._get_config_float("GENERAL", "max_velocity_y_mm", 30))
        self.max_vel_y.setSuffix(" mm/s")
        stage_layout.addRow("Max Velocity Y:", self.max_vel_y)

        self.max_vel_z = QDoubleSpinBox()
        self.max_vel_z.setRange(0.1, 20)
        self.max_vel_z.setValue(self._get_config_float("GENERAL", "max_velocity_z_mm", 3.8))
        self.max_vel_z.setSuffix(" mm/s")
        stage_layout.addRow("Max Velocity Z:", self.max_vel_z)

        self.max_accel_x = QDoubleSpinBox()
        self.max_accel_x.setRange(1, 2000)
        self.max_accel_x.setValue(self._get_config_float("GENERAL", "max_acceleration_x_mm", 500))
        self.max_accel_x.setSuffix(" mm/s2")
        stage_layout.addRow("Max Acceleration X:", self.max_accel_x)

        self.max_accel_y = QDoubleSpinBox()
        self.max_accel_y.setRange(1, 2000)
        self.max_accel_y.setValue(self._get_config_float("GENERAL", "max_acceleration_y_mm", 500))
        self.max_accel_y.setSuffix(" mm/s2")
        stage_layout.addRow("Max Acceleration Y:", self.max_accel_y)

        self.max_accel_z = QDoubleSpinBox()
        self.max_accel_z.setRange(1, 500)
        self.max_accel_z.setValue(self._get_config_float("GENERAL", "max_acceleration_z_mm", 100))
        self.max_accel_z.setSuffix(" mm/s2")
        stage_layout.addRow("Max Acceleration Z:", self.max_accel_z)

        self.scan_stab_x = QSpinBox()
        self.scan_stab_x.setRange(0, 1000)
        self.scan_stab_x.setValue(self._get_config_int("GENERAL", "scan_stabilization_time_ms_x", 25))
        self.scan_stab_x.setSuffix(" ms")
        stage_layout.addRow("Scan Stabilization X:", self.scan_stab_x)

        self.scan_stab_y = QSpinBox()
        self.scan_stab_y.setRange(0, 1000)
        self.scan_stab_y.setValue(self._get_config_int("GENERAL", "scan_stabilization_time_ms_y", 25))
        self.scan_stab_y.setSuffix(" ms")
        stage_layout.addRow("Scan Stabilization Y:", self.scan_stab_y)

        self.scan_stab_z = QSpinBox()
        self.scan_stab_z.setRange(0, 1000)
        self.scan_stab_z.setValue(self._get_config_int("GENERAL", "scan_stabilization_time_ms_z", 20))
        self.scan_stab_z.setSuffix(" ms")
        stage_layout.addRow("Scan Stabilization Z:", self.scan_stab_z)

        stage_group.content.addLayout(stage_layout)
        layout.addWidget(stage_group)

        # Contrast Autofocus section
        af_group = CollapsibleGroupBox("Contrast Autofocus", collapsed=True)
        af_layout = QFormLayout()

        self.af_stop_threshold = QDoubleSpinBox()
        self.af_stop_threshold.setRange(0.1, 1.0)
        self.af_stop_threshold.setSingleStep(0.05)
        self.af_stop_threshold.setValue(self._get_config_float("AF", "stop_threshold", 0.85))
        af_layout.addRow("Stop Threshold:", self.af_stop_threshold)

        self.af_crop_width = QSpinBox()
        self.af_crop_width.setRange(100, 4000)
        self.af_crop_width.setValue(self._get_config_int("AF", "crop_width", 800))
        self.af_crop_width.setSuffix(" px")
        af_layout.addRow("Crop Width:", self.af_crop_width)

        self.af_crop_height = QSpinBox()
        self.af_crop_height.setRange(100, 4000)
        self.af_crop_height.setValue(self._get_config_int("AF", "crop_height", 800))
        self.af_crop_height.setSuffix(" px")
        af_layout.addRow("Crop Height:", self.af_crop_height)

        af_group.content.addLayout(af_layout)
        layout.addWidget(af_group)

        # Hardware Configuration section
        hw_group = CollapsibleGroupBox("Hardware Configuration", collapsed=True)
        hw_layout = QFormLayout()

        self.z_motor_combo = QComboBox()
        self.z_motor_combo.addItems(["STEPPER", "STEPPER + PIEZO", "PIEZO", "LINEAR"])
        self.z_motor_combo.setCurrentText(self._get_config_value("GENERAL", "z_motor_config", "STEPPER"))
        hw_layout.addRow("Z Motor Config *:", self.z_motor_combo)

        self.spinning_disk_checkbox = QCheckBox()
        self.spinning_disk_checkbox.setChecked(self._get_config_bool("GENERAL", "enable_spinning_disk_confocal", False))
        hw_layout.addRow("Enable Spinning Disk *:", self.spinning_disk_checkbox)

        self.led_r_factor = QDoubleSpinBox()
        self.led_r_factor.setRange(0.0, 1.0)
        self.led_r_factor.setSingleStep(0.1)
        self.led_r_factor.setValue(self._get_config_float("GENERAL", "led_matrix_r_factor", 1.0))
        hw_layout.addRow("LED Matrix R Factor:", self.led_r_factor)

        self.led_g_factor = QDoubleSpinBox()
        self.led_g_factor.setRange(0.0, 1.0)
        self.led_g_factor.setSingleStep(0.1)
        self.led_g_factor.setValue(self._get_config_float("GENERAL", "led_matrix_g_factor", 1.0))
        hw_layout.addRow("LED Matrix G Factor:", self.led_g_factor)

        self.led_b_factor = QDoubleSpinBox()
        self.led_b_factor.setRange(0.0, 1.0)
        self.led_b_factor.setSingleStep(0.1)
        self.led_b_factor.setValue(self._get_config_float("GENERAL", "led_matrix_b_factor", 1.0))
        hw_layout.addRow("LED Matrix B Factor:", self.led_b_factor)

        self.illumination_factor = QDoubleSpinBox()
        self.illumination_factor.setRange(0.0, 1.0)
        self.illumination_factor.setSingleStep(0.1)
        self.illumination_factor.setValue(self._get_config_float("GENERAL", "illumination_intensity_factor", 0.6))
        hw_layout.addRow("Illumination Intensity Factor:", self.illumination_factor)

        hw_group.content.addLayout(hw_layout)
        layout.addWidget(hw_group)

        # Software Position Limits section
        limits_group = CollapsibleGroupBox("Software Position Limits", collapsed=True)
        limits_layout = QFormLayout()

        self.limit_x_pos = QDoubleSpinBox()
        self.limit_x_pos.setRange(0, 500)
        self.limit_x_pos.setValue(self._get_config_float("SOFTWARE_POS_LIMIT", "x_positive", 115))
        self.limit_x_pos.setSuffix(" mm")
        limits_layout.addRow("X Positive:", self.limit_x_pos)

        self.limit_x_neg = QDoubleSpinBox()
        self.limit_x_neg.setRange(0, 500)
        self.limit_x_neg.setValue(self._get_config_float("SOFTWARE_POS_LIMIT", "x_negative", 5))
        self.limit_x_neg.setSuffix(" mm")
        limits_layout.addRow("X Negative:", self.limit_x_neg)

        self.limit_y_pos = QDoubleSpinBox()
        self.limit_y_pos.setRange(0, 500)
        self.limit_y_pos.setValue(self._get_config_float("SOFTWARE_POS_LIMIT", "y_positive", 76))
        self.limit_y_pos.setSuffix(" mm")
        limits_layout.addRow("Y Positive:", self.limit_y_pos)

        self.limit_y_neg = QDoubleSpinBox()
        self.limit_y_neg.setRange(0, 500)
        self.limit_y_neg.setValue(self._get_config_float("SOFTWARE_POS_LIMIT", "y_negative", 4))
        self.limit_y_neg.setSuffix(" mm")
        limits_layout.addRow("Y Negative:", self.limit_y_neg)

        self.limit_z_pos = QDoubleSpinBox()
        self.limit_z_pos.setRange(0, 50)
        self.limit_z_pos.setValue(self._get_config_float("SOFTWARE_POS_LIMIT", "z_positive", 6))
        self.limit_z_pos.setSuffix(" mm")
        limits_layout.addRow("Z Positive:", self.limit_z_pos)

        self.limit_z_neg = QDoubleSpinBox()
        self.limit_z_neg.setRange(0, 50)
        self.limit_z_neg.setDecimals(3)
        self.limit_z_neg.setValue(self._get_config_float("SOFTWARE_POS_LIMIT", "z_negative", 0.05))
        self.limit_z_neg.setSuffix(" mm")
        limits_layout.addRow("Z Negative:", self.limit_z_neg)

        limits_group.content.addLayout(limits_layout)
        layout.addWidget(limits_group)

        # Tracking section (hidden - widgets exist for config persistence)
        tracking_group = CollapsibleGroupBox("Tracking", collapsed=True)
        tracking_layout = QFormLayout()

        self.enable_tracking_checkbox = QCheckBox()
        self.enable_tracking_checkbox.setChecked(self._get_config_bool("GENERAL", "enable_tracking", False))
        tracking_layout.addRow("Enable Tracking:", self.enable_tracking_checkbox)

        self.default_tracker_combo = QComboBox()
        self.default_tracker_combo.addItems(["csrt", "kcf", "mil", "tld", "medianflow", "mosse", "daSiamRPN"])
        self.default_tracker_combo.setCurrentText(self._get_config_value("TRACKING", "default_tracker", "csrt"))
        tracking_layout.addRow("Default Tracker:", self.default_tracker_combo)

        self.search_area_ratio = QSpinBox()
        self.search_area_ratio.setRange(1, 50)
        self.search_area_ratio.setValue(self._get_config_int("TRACKING", "search_area_ratio", 10))
        tracking_layout.addRow("Search Area Ratio:", self.search_area_ratio)

        tracking_group.content.addLayout(tracking_layout)
        layout.addWidget(tracking_group)
        tracking_group.hide()  # Hidden but widgets exist for config save/load

        # Acquisition Throttling section
        throttle_group = CollapsibleGroupBox("Acquisition Throttling", collapsed=True)
        throttle_layout = QFormLayout()

        self.throttling_enabled_checkbox = QCheckBox()
        self.throttling_enabled_checkbox.setChecked(
            self._get_config_bool(
                "GENERAL", "acquisition_throttling_enabled", control._def.ACQUISITION_THROTTLING_ENABLED
            )
        )
        self.throttling_enabled_checkbox.setToolTip(
            "When enabled, acquisition pauses when pending jobs or RAM usage exceeds limits.\n"
            "Prevents RAM exhaustion when acquisition speed exceeds disk write speed."
        )
        throttle_layout.addRow("Enable Throttling:", self.throttling_enabled_checkbox)

        self.max_pending_jobs_spinbox = QSpinBox()
        self.max_pending_jobs_spinbox.setRange(1, 100)
        self.max_pending_jobs_spinbox.setValue(
            self._get_config_int("GENERAL", "acquisition_max_pending_jobs", control._def.ACQUISITION_MAX_PENDING_JOBS)
        )
        self.max_pending_jobs_spinbox.setToolTip(
            "Maximum number of jobs in flight before throttling.\n"
            "Higher values allow more parallelism but use more RAM."
        )
        throttle_layout.addRow("Max Pending Jobs:", self.max_pending_jobs_spinbox)

        self.max_pending_mb_spinbox = QDoubleSpinBox()
        self.max_pending_mb_spinbox.setRange(100.0, 10000.0)
        self.max_pending_mb_spinbox.setSingleStep(100.0)
        self.max_pending_mb_spinbox.setValue(
            self._get_config_float("GENERAL", "acquisition_max_pending_mb", control._def.ACQUISITION_MAX_PENDING_MB)
        )
        self.max_pending_mb_spinbox.setSuffix(" MB")
        self.max_pending_mb_spinbox.setToolTip(
            "Maximum RAM usage (MB) for pending jobs before throttling.\n"
            "Higher values allow faster acquisition but risk RAM exhaustion."
        )
        throttle_layout.addRow("Max Pending RAM:", self.max_pending_mb_spinbox)

        self.throttle_timeout_spinbox = QDoubleSpinBox()
        self.throttle_timeout_spinbox.setRange(5.0, 300.0)
        self.throttle_timeout_spinbox.setSingleStep(5.0)
        self.throttle_timeout_spinbox.setValue(
            self._get_config_float(
                "GENERAL", "acquisition_throttle_timeout_s", control._def.ACQUISITION_THROTTLE_TIMEOUT_S
            )
        )
        self.throttle_timeout_spinbox.setSuffix(" s")
        self.throttle_timeout_spinbox.setToolTip(
            "Maximum time to wait when throttled before reporting a warning.\n"
            "If disk I/O cannot keep up within this time, acquisition logs a warning."
        )
        throttle_layout.addRow("Throttle Timeout:", self.throttle_timeout_spinbox)

        throttle_group.content.addLayout(throttle_layout)
        layout.addWidget(throttle_group)

        # Diagnostics section
        diagnostics_group = CollapsibleGroupBox("Diagnostics", collapsed=True)
        diagnostics_layout = QFormLayout()

        self.enable_memory_profiling_checkbox = QCheckBox()
        self.enable_memory_profiling_checkbox.setChecked(
            self._get_config_bool("GENERAL", "enable_memory_profiling", control._def.ENABLE_MEMORY_PROFILING)
        )
        self.enable_memory_profiling_checkbox.setToolTip(
            "Show real-time RAM usage in status bar during acquisition.\n"
            "Also logs periodic memory snapshots to help diagnose memory issues."
        )
        diagnostics_layout.addRow("Enable RAM Monitoring:", self.enable_memory_profiling_checkbox)

        diagnostics_group.content.addLayout(diagnostics_layout)
        layout.addWidget(diagnostics_group)

        # Developer Options section
        dev_options_group = CollapsibleGroupBox("Developer Options", collapsed=True)
        dev_options_layout = QFormLayout()

        self.show_dev_tab_checkbox = QCheckBox()
        self.show_dev_tab_checkbox.setChecked(self._get_config_bool("GENERAL", "show_dev_tab", False))
        self.show_dev_tab_checkbox.setToolTip("Show the Dev tab with development/testing settings")
        self.show_dev_tab_checkbox.stateChanged.connect(self._toggle_dev_tab_visibility)
        dev_options_layout.addRow("Show Dev Tab:", self.show_dev_tab_checkbox)

        dev_options_group.content.addLayout(dev_options_layout)
        layout.addWidget(dev_options_group)

        # Legend for restart indicator
        legend_label = QLabel("* Requires software restart to take effect")
        legend_label.setStyleSheet("color: #666; font-style: italic;")
        layout.addWidget(legend_label)

        layout.addStretch()
        scroll.setWidget(scroll_content)

        tab_layout = QVBoxLayout(tab)
        tab_layout.addWidget(scroll)
        self.tab_widget.addTab(tab, "Advanced")

    def _create_views_tab(self):
        # NOTE: Views settings read from control._def (runtime state) instead of config file.
        # This enables MCP commands to modify these settings for RAM usage diagnostics,
        # with changes reflected when this dialog opens. See PR #424 for context.
        # This pattern may be modified if the settings architecture is refactored.

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(10)

        # Plate View section
        plate_group = CollapsibleGroupBox("Plate View")
        plate_layout = QFormLayout()

        # Save Downsampled Well Images
        self.save_downsampled_checkbox = QCheckBox()
        self.save_downsampled_checkbox.setChecked(control._def.SAVE_DOWNSAMPLED_WELL_IMAGES)
        self.save_downsampled_checkbox.setToolTip(
            "Save individual well TIFFs (e.g., wells/A1_5um.tiff, wells/A1_10um.tiff)"
        )
        plate_layout.addRow("Save Downsampled Well Images:", self.save_downsampled_checkbox)

        # Display Plate View
        self.display_plate_view_checkbox = QCheckBox()
        self.display_plate_view_checkbox.setChecked(control._def.DISPLAY_PLATE_VIEW)
        self.display_plate_view_checkbox.setToolTip(
            "Show plate view tab in GUI during acquisition.\n"
            "Note: Plate view TIFF is always saved when either option is enabled."
        )
        plate_layout.addRow("Display Plate View:", self.display_plate_view_checkbox)

        # Well Resolutions (comma-separated)
        self.well_resolutions_edit = QLineEdit()
        default_resolutions = ", ".join(str(r) for r in control._def.DOWNSAMPLED_WELL_RESOLUTIONS_UM)
        self.well_resolutions_edit.setText(default_resolutions)
        self.well_resolutions_edit.setToolTip(
            "Comma-separated list of resolution values in micrometers (e.g., 5.0, 10.0, 20.0)"
        )
        # Validator for comma-separated positive numbers
        from qtpy.QtCore import QRegularExpression
        from qtpy.QtGui import QRegularExpressionValidator

        well_res_pattern = QRegularExpression(r"^\s*\d+(\.\d+)?(\s*,\s*\d+(\.\d+)?)*\s*$")
        self.well_resolutions_edit.setValidator(QRegularExpressionValidator(well_res_pattern))
        plate_layout.addRow("Well Resolutions (μm):", self.well_resolutions_edit)

        # Target Pixel Size
        self.plate_resolution_spinbox = QDoubleSpinBox()
        self.plate_resolution_spinbox.setRange(1.0, 100.0)
        self.plate_resolution_spinbox.setSingleStep(1.0)
        self.plate_resolution_spinbox.setValue(control._def.DOWNSAMPLED_PLATE_RESOLUTION_UM)
        self.plate_resolution_spinbox.setSuffix(" μm")
        self.plate_resolution_spinbox.setToolTip("Pixel size for the plate view overview image")
        plate_layout.addRow("Target Pixel Size:", self.plate_resolution_spinbox)

        # Z-Projection Mode
        self.z_projection_combo = QComboBox()
        self.z_projection_combo.addItems(["mip", "middle"])
        current_projection = control._def.DOWNSAMPLED_Z_PROJECTION.value
        self.z_projection_combo.setCurrentText(current_projection)
        plate_layout.addRow("Z-Projection Mode:", self.z_projection_combo)

        # Interpolation Method
        self.interpolation_method_combo = QComboBox()
        self.interpolation_method_combo.addItems(["inter_linear", "inter_area_fast", "inter_area"])
        current_interp = control._def.DOWNSAMPLED_INTERPOLATION_METHOD.value
        self.interpolation_method_combo.setCurrentText(current_interp)
        self.interpolation_method_combo.setToolTip(
            "inter_linear: Fastest (~0.05ms), good for real-time previews\n"
            "inter_area_fast: Balanced (~1ms), pyramid downsampling\n"
            "inter_area: Slowest (~18ms), highest quality for final output"
        )
        plate_layout.addRow("Interpolation Method:", self.interpolation_method_combo)

        plate_group.content.addLayout(plate_layout)
        layout.addWidget(plate_group)

        # Mosaic View section
        mosaic_group = CollapsibleGroupBox("Mosaic View")
        mosaic_layout = QFormLayout()

        # Display Mosaic View
        self.display_mosaic_view_checkbox = QCheckBox()
        self.display_mosaic_view_checkbox.setChecked(control._def.USE_NAPARI_FOR_MOSAIC_DISPLAY)
        mosaic_layout.addRow("Display Mosaic View:", self.display_mosaic_view_checkbox)

        # Mosaic Target Pixel Size
        self.mosaic_pixel_size_spinbox = QDoubleSpinBox()
        self.mosaic_pixel_size_spinbox.setRange(0.5, 20.0)
        self.mosaic_pixel_size_spinbox.setSingleStep(0.5)
        self.mosaic_pixel_size_spinbox.setValue(control._def.MOSAIC_VIEW_TARGET_PIXEL_SIZE_UM)
        self.mosaic_pixel_size_spinbox.setSuffix(" μm")
        mosaic_layout.addRow("Target Pixel Size:", self.mosaic_pixel_size_spinbox)

        mosaic_group.content.addLayout(mosaic_layout)
        layout.addWidget(mosaic_group)

        # NDViewer section
        ndviewer_group = CollapsibleGroupBox("NDViewer")
        ndviewer_layout = QFormLayout()

        # Enable NDViewer
        self.enable_ndviewer_checkbox = QCheckBox()
        self.enable_ndviewer_checkbox.setChecked(control._def.ENABLE_NDVIEWER)
        self.enable_ndviewer_checkbox.setToolTip("Enable the NDViewer tab for viewing acquired datasets")
        ndviewer_layout.addRow("Enable NDViewer *:", self.enable_ndviewer_checkbox)

        ndviewer_group.content.addLayout(ndviewer_layout)
        layout.addWidget(ndviewer_group)

        layout.addStretch()
        self.tab_widget.addTab(tab, "Views")

    def _create_development_tab(self):
        """Create the Development tab for development/testing settings."""
        self.dev_tab = QWidget()
        layout = QVBoxLayout(self.dev_tab)
        layout.setSpacing(10)

        # Use Simulated Hardware section
        hw_sim_group = CollapsibleGroupBox("Use Simulated Hardware *")
        hw_sim_layout = QFormLayout()

        # Helper to create simulation checkboxes
        def create_sim_checkbox(config_key):
            checkbox = QCheckBox()
            current = self._get_config_value("SIMULATION", config_key, "false").lower()
            checkbox.setChecked(current in ("true", "1", "yes", "simulate"))
            return checkbox

        sim_tooltip = "Simulate this component (even without --simulation flag).\nWith --simulation, unset components default to simulated; set to unchecked to use real hardware for this component."

        self.sim_camera_checkbox = create_sim_checkbox("simulate_camera")
        self.sim_camera_checkbox.setToolTip(sim_tooltip)
        hw_sim_layout.addRow("Simulate Camera:", self.sim_camera_checkbox)

        self.sim_mcu_checkbox = create_sim_checkbox("simulate_microcontroller")
        self.sim_mcu_checkbox.setToolTip(sim_tooltip)
        hw_sim_layout.addRow("Simulate MCU/Stage:", self.sim_mcu_checkbox)

        self.sim_spinning_disk_checkbox = create_sim_checkbox("simulate_spinning_disk")
        self.sim_spinning_disk_checkbox.setToolTip(sim_tooltip)
        hw_sim_layout.addRow("Simulate Spinning Disk:", self.sim_spinning_disk_checkbox)

        self.sim_filter_wheel_checkbox = create_sim_checkbox("simulate_filter_wheel")
        self.sim_filter_wheel_checkbox.setToolTip(sim_tooltip)
        hw_sim_layout.addRow("Simulate Filter Wheel:", self.sim_filter_wheel_checkbox)

        self.sim_objective_changer_checkbox = create_sim_checkbox("simulate_objective_changer")
        self.sim_objective_changer_checkbox.setToolTip(sim_tooltip)
        hw_sim_layout.addRow("Simulate Objective Changer:", self.sim_objective_changer_checkbox)

        self.sim_laser_af_camera_checkbox = create_sim_checkbox("simulate_laser_af_camera")
        self.sim_laser_af_camera_checkbox.setToolTip(sim_tooltip)
        hw_sim_layout.addRow("Simulate Laser AF Camera:", self.sim_laser_af_camera_checkbox)

        hw_sim_group.content.addLayout(hw_sim_layout)
        layout.addWidget(hw_sim_group)

        # Simulated Disk I/O section
        dev_group = CollapsibleGroupBox("Simulated Disk I/O *")
        dev_layout = QFormLayout()

        self.simulated_io_checkbox = QCheckBox()
        self.simulated_io_checkbox.setChecked(self._get_config_bool("GENERAL", "simulated_disk_io_enabled", False))
        self.simulated_io_checkbox.setToolTip(
            "When enabled, images are encoded to memory but NOT saved to disk.\n"
            "Use this for development/testing to avoid SSD wear."
        )
        dev_layout.addRow("Enable Simulated Disk I/O:", self.simulated_io_checkbox)

        self.simulated_io_speed_spinbox = QDoubleSpinBox()
        self.simulated_io_speed_spinbox.setRange(10.0, 3000.0)
        self.simulated_io_speed_spinbox.setValue(
            self._get_config_float("GENERAL", "simulated_disk_io_speed_mb_s", 200.0)
        )
        self.simulated_io_speed_spinbox.setSuffix(" MB/s")
        self.simulated_io_speed_spinbox.setToolTip(
            "Simulated write speed: HDD: 50-100, SATA SSD: 200-500, NVMe: 1000-3000 MB/s"
        )
        dev_layout.addRow("Simulated Write Speed:", self.simulated_io_speed_spinbox)

        self.simulated_io_compression_checkbox = QCheckBox()
        self.simulated_io_compression_checkbox.setChecked(
            self._get_config_bool("GENERAL", "simulated_disk_io_compression", True)
        )
        self.simulated_io_compression_checkbox.setToolTip(
            "When enabled, images are compressed during simulation (more realistic CPU/RAM usage)"
        )
        dev_layout.addRow("Simulate Compression:", self.simulated_io_compression_checkbox)

        dev_group.content.addLayout(dev_layout)
        layout.addWidget(dev_group)

        # Legend
        legend_label = QLabel("* Requires software restart to take effect")
        legend_label.setStyleSheet("color: #666; font-style: italic;")
        layout.addWidget(legend_label)

        layout.addStretch()
        self._dev_tab_index = self.tab_widget.addTab(self.dev_tab, "Dev")

        # Initially hide if not enabled
        if not self._get_config_bool("GENERAL", "show_dev_tab", False):
            self.tab_widget.setTabVisible(self._dev_tab_index, False)

    def _toggle_dev_tab_visibility(self, state):
        """Show or hide the Dev tab based on checkbox state."""
        # Handle both PyQt5 (int) and PyQt6 (CheckState enum) signal types
        # PyQt6 enums have .value property, integers don't - use getattr for compatibility
        state_value = getattr(state, "value", state)
        checked_value = getattr(Qt.Checked, "value", Qt.Checked)
        self.tab_widget.setTabVisible(self._dev_tab_index, state_value == checked_value)

    def _get_config_value(self, section, option, default=""):
        try:
            return self.config.get(section, option)
        except (configparser.NoSectionError, configparser.NoOptionError):
            return default

    def _get_config_bool(self, section, option, default=False):
        try:
            val = self.config.get(section, option)
            return str(val).strip().lower() in ("true", "1", "yes", "on")
        except (configparser.NoSectionError, configparser.NoOptionError):
            return default

    def _get_config_int(self, section, option, default=0):
        try:
            return int(self.config.get(section, option))
        except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
            return default

    def _get_config_float(self, section, option, default=0.0):
        try:
            return float(self.config.get(section, option))
        except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
            return default

    def _floats_equal(self, a, b, epsilon=1e-4):
        """Compare two floats with epsilon tolerance to avoid precision issues."""
        return abs(a - b) < epsilon

    def _browse_saving_path(self):
        path = QFileDialog.getExistingDirectory(self, "Select Default Saving Path", self.saving_path_edit.text())
        if path:
            if os.access(path, os.W_OK):
                self.saving_path_edit.setText(path)
            else:
                QMessageBox.warning(self, "Invalid Path", f"The selected directory is not writable:\n{path}")

    def _update_zarr_options_visibility(self):
        """Show/hide zarr options based on file saving format."""
        is_zarr = self.file_saving_combo.currentText() == "ZARR_V3"
        self.zarr_compression_label.setVisible(is_zarr)
        self.zarr_compression_combo.setVisible(is_zarr)

    def _ensure_section(self, section):
        """Ensure a config section exists, creating it if necessary."""
        if not self.config.has_section(section):
            self.config.add_section(section)

    def _apply_settings(self) -> bool:
        """Apply settings to config file. Returns True on success, False on failure."""
        # Ensure all required sections exist
        for section in ["GENERAL", "CAMERA_CONFIG", "AF", "SOFTWARE_POS_LIMIT", "TRACKING", "VIEWS"]:
            self._ensure_section(section)

        # General settings
        self.config.set("GENERAL", "file_saving_option", self.file_saving_combo.currentText())
        self.config.set("GENERAL", "zarr_compression", self.zarr_compression_combo.currentText())
        self.config.set("GENERAL", "default_saving_path", self.saving_path_edit.text())
        self.config.set("GENERAL", "show_dev_tab", "true" if self.show_dev_tab_checkbox.isChecked() else "false")

        # Acquisition settings
        self.config.set("GENERAL", "multipoint_autofocus_channel", self.autofocus_channel_edit.text())
        self.config.set(
            "GENERAL",
            "enable_flexible_multipoint",
            "true" if self.flexible_multipoint_checkbox.isChecked() else "false",
        )

        # Camera settings
        self.config.set("CAMERA_CONFIG", "binning_factor_default", str(self.binning_spinbox.value()))
        self.config.set("CAMERA_CONFIG", "flip_image", self.flip_combo.currentText())
        self.config.set("CAMERA_CONFIG", "temperature_default", str(self.temperature_spinbox.value()))
        roi_width = "None" if self.roi_width_spinbox.value() == 0 else str(self.roi_width_spinbox.value())
        roi_height = "None" if self.roi_height_spinbox.value() == 0 else str(self.roi_height_spinbox.value())
        self.config.set("CAMERA_CONFIG", "roi_width_default", roi_width)
        self.config.set("CAMERA_CONFIG", "roi_height_default", roi_height)

        # Advanced - Stage & Motion
        self.config.set("GENERAL", "max_velocity_x_mm", str(self.max_vel_x.value()))
        self.config.set("GENERAL", "max_velocity_y_mm", str(self.max_vel_y.value()))
        self.config.set("GENERAL", "max_velocity_z_mm", str(self.max_vel_z.value()))
        self.config.set("GENERAL", "max_acceleration_x_mm", str(self.max_accel_x.value()))
        self.config.set("GENERAL", "max_acceleration_y_mm", str(self.max_accel_y.value()))
        self.config.set("GENERAL", "max_acceleration_z_mm", str(self.max_accel_z.value()))
        self.config.set("GENERAL", "scan_stabilization_time_ms_x", str(self.scan_stab_x.value()))
        self.config.set("GENERAL", "scan_stabilization_time_ms_y", str(self.scan_stab_y.value()))
        self.config.set("GENERAL", "scan_stabilization_time_ms_z", str(self.scan_stab_z.value()))

        # Advanced - Autofocus
        self.config.set("AF", "stop_threshold", str(self.af_stop_threshold.value()))
        self.config.set("AF", "crop_width", str(self.af_crop_width.value()))
        self.config.set("AF", "crop_height", str(self.af_crop_height.value()))

        # Advanced - Hardware
        self.config.set("GENERAL", "z_motor_config", self.z_motor_combo.currentText())
        self.config.set(
            "GENERAL",
            "enable_spinning_disk_confocal",
            "true" if self.spinning_disk_checkbox.isChecked() else "false",
        )
        self.config.set("GENERAL", "led_matrix_r_factor", str(self.led_r_factor.value()))
        self.config.set("GENERAL", "led_matrix_g_factor", str(self.led_g_factor.value()))
        self.config.set("GENERAL", "led_matrix_b_factor", str(self.led_b_factor.value()))
        self.config.set("GENERAL", "illumination_intensity_factor", str(self.illumination_factor.value()))

        # Advanced - Development Settings
        self.config.set(
            "GENERAL",
            "simulated_disk_io_enabled",
            "true" if self.simulated_io_checkbox.isChecked() else "false",
        )
        self.config.set("GENERAL", "simulated_disk_io_speed_mb_s", str(self.simulated_io_speed_spinbox.value()))
        self.config.set(
            "GENERAL",
            "simulated_disk_io_compression",
            "true" if self.simulated_io_compression_checkbox.isChecked() else "false",
        )

        # Advanced - Acquisition Throttling
        self.config.set(
            "GENERAL",
            "acquisition_throttling_enabled",
            "true" if self.throttling_enabled_checkbox.isChecked() else "false",
        )
        self.config.set("GENERAL", "acquisition_max_pending_jobs", str(self.max_pending_jobs_spinbox.value()))
        self.config.set("GENERAL", "acquisition_max_pending_mb", str(self.max_pending_mb_spinbox.value()))
        self.config.set("GENERAL", "acquisition_throttle_timeout_s", str(self.throttle_timeout_spinbox.value()))

        # Advanced - Position Limits
        self.config.set("SOFTWARE_POS_LIMIT", "x_positive", str(self.limit_x_pos.value()))
        self.config.set("SOFTWARE_POS_LIMIT", "x_negative", str(self.limit_x_neg.value()))
        self.config.set("SOFTWARE_POS_LIMIT", "y_positive", str(self.limit_y_pos.value()))
        self.config.set("SOFTWARE_POS_LIMIT", "y_negative", str(self.limit_y_neg.value()))
        self.config.set("SOFTWARE_POS_LIMIT", "z_positive", str(self.limit_z_pos.value()))
        self.config.set("SOFTWARE_POS_LIMIT", "z_negative", str(self.limit_z_neg.value()))

        # Advanced - Tracking (hidden but still saved)
        self.config.set("GENERAL", "enable_tracking", "true" if self.enable_tracking_checkbox.isChecked() else "false")
        self.config.set("TRACKING", "default_tracker", self.default_tracker_combo.currentText())
        self.config.set("TRACKING", "search_area_ratio", str(self.search_area_ratio.value()))

        # Advanced - Diagnostics
        self.config.set(
            "GENERAL",
            "enable_memory_profiling",
            "true" if self.enable_memory_profiling_checkbox.isChecked() else "false",
        )

        # Views settings
        self.config.set(
            "VIEWS",
            "save_downsampled_well_images",
            "true" if self.save_downsampled_checkbox.isChecked() else "false",
        )
        self.config.set(
            "VIEWS",
            "display_plate_view",
            "true" if self.display_plate_view_checkbox.isChecked() else "false",
        )
        self.config.set("VIEWS", "downsampled_well_resolutions_um", self.well_resolutions_edit.text())
        self.config.set("VIEWS", "downsampled_plate_resolution_um", str(self.plate_resolution_spinbox.value()))
        self.config.set("VIEWS", "downsampled_z_projection", self.z_projection_combo.currentText())
        self.config.set("VIEWS", "downsampled_interpolation_method", self.interpolation_method_combo.currentText())
        self.config.set(
            "VIEWS",
            "display_mosaic_view",
            "true" if self.display_mosaic_view_checkbox.isChecked() else "false",
        )
        self.config.set("VIEWS", "mosaic_view_target_pixel_size_um", str(self.mosaic_pixel_size_spinbox.value()))
        self.config.set(
            "VIEWS",
            "enable_ndviewer",
            "true" if self.enable_ndviewer_checkbox.isChecked() else "false",
        )

        # Hardware Simulation settings (in [SIMULATION] section)
        self._ensure_section("SIMULATION")
        self.config.set("SIMULATION", "simulate_camera", str(self.sim_camera_checkbox.isChecked()).lower())
        self.config.set("SIMULATION", "simulate_microcontroller", str(self.sim_mcu_checkbox.isChecked()).lower())
        self.config.set(
            "SIMULATION", "simulate_spinning_disk", str(self.sim_spinning_disk_checkbox.isChecked()).lower()
        )
        self.config.set("SIMULATION", "simulate_filter_wheel", str(self.sim_filter_wheel_checkbox.isChecked()).lower())
        self.config.set(
            "SIMULATION", "simulate_objective_changer", str(self.sim_objective_changer_checkbox.isChecked()).lower()
        )
        self.config.set(
            "SIMULATION", "simulate_laser_af_camera", str(self.sim_laser_af_camera_checkbox.isChecked()).lower()
        )

        # Save to file
        try:
            with open(self.config_filepath, "w") as f:
                self.config.write(f)
            self._log.info(f"Configuration saved to {self.config_filepath}")
        except OSError as e:
            self._log.exception("Failed to save configuration")
            QMessageBox.warning(
                self,
                "Error",
                (
                    f"Failed to save configuration to:\n"
                    f"{self.config_filepath}\n\n"
                    "Please check that:\n"
                    "- You have write permission to this location.\n"
                    "- The file is not open in another application.\n"
                    "- The disk is not full or write-protected.\n\n"
                    f"System error: {e}"
                ),
            )
            return False

        # Update runtime values for settings that can be applied live
        try:
            self._apply_live_settings()
        except Exception:
            self._log.exception("Failed to apply live settings")

        self.signal_config_changed.emit()
        return True

    def _apply_live_settings(self):
        """Apply settings that can take effect without restart."""
        # File saving option
        control._def.FILE_SAVING_OPTION = control._def.FileSavingOption.convert_to_enum(
            self.file_saving_combo.currentText()
        )

        # Zarr compression (only applicable when using ZARR_V3)
        control._def.ZARR_COMPRESSION = control._def.ZarrCompression.convert_to_enum(
            self.zarr_compression_combo.currentText()
        )

        # Default saving path
        control._def.DEFAULT_SAVING_PATH = self.saving_path_edit.text()

        # Autofocus channel
        control._def.MULTIPOINT_AUTOFOCUS_CHANNEL = self.autofocus_channel_edit.text()

        # Flexible multipoint
        control._def.ENABLE_FLEXIBLE_MULTIPOINT = self.flexible_multipoint_checkbox.isChecked()

        # AF settings
        control._def.AF.STOP_THRESHOLD = self.af_stop_threshold.value()
        control._def.AF.CROP_WIDTH = self.af_crop_width.value()
        control._def.AF.CROP_HEIGHT = self.af_crop_height.value()

        # LED matrix factors
        control._def.LED_MATRIX_R_FACTOR = self.led_r_factor.value()
        control._def.LED_MATRIX_G_FACTOR = self.led_g_factor.value()
        control._def.LED_MATRIX_B_FACTOR = self.led_b_factor.value()

        # Illumination intensity factor
        control._def.ILLUMINATION_INTENSITY_FACTOR = self.illumination_factor.value()

        # Development settings - simulated disk I/O
        control._def.SIMULATED_DISK_IO_ENABLED = self.simulated_io_checkbox.isChecked()
        control._def.SIMULATED_DISK_IO_SPEED_MB_S = self.simulated_io_speed_spinbox.value()
        control._def.SIMULATED_DISK_IO_COMPRESSION = self.simulated_io_compression_checkbox.isChecked()

        # Acquisition throttling settings
        control._def.ACQUISITION_THROTTLING_ENABLED = self.throttling_enabled_checkbox.isChecked()
        control._def.ACQUISITION_MAX_PENDING_JOBS = self.max_pending_jobs_spinbox.value()
        control._def.ACQUISITION_MAX_PENDING_MB = self.max_pending_mb_spinbox.value()
        control._def.ACQUISITION_THROTTLE_TIMEOUT_S = self.throttle_timeout_spinbox.value()

        # Software position limits
        control._def.SOFTWARE_POS_LIMIT.X_POSITIVE = self.limit_x_pos.value()
        control._def.SOFTWARE_POS_LIMIT.X_NEGATIVE = self.limit_x_neg.value()
        control._def.SOFTWARE_POS_LIMIT.Y_POSITIVE = self.limit_y_pos.value()
        control._def.SOFTWARE_POS_LIMIT.Y_NEGATIVE = self.limit_y_neg.value()
        control._def.SOFTWARE_POS_LIMIT.Z_POSITIVE = self.limit_z_pos.value()
        control._def.SOFTWARE_POS_LIMIT.Z_NEGATIVE = self.limit_z_neg.value()

        # Tracking settings (hidden but still updated)
        control._def.ENABLE_TRACKING = self.enable_tracking_checkbox.isChecked()
        control._def.Tracking.DEFAULT_TRACKER = self.default_tracker_combo.currentText()
        control._def.Tracking.SEARCH_AREA_RATIO = self.search_area_ratio.value()

        # Diagnostics settings
        control._def.ENABLE_MEMORY_PROFILING = self.enable_memory_profiling_checkbox.isChecked()

        # Views settings
        control._def.SAVE_DOWNSAMPLED_WELL_IMAGES = self.save_downsampled_checkbox.isChecked()
        control._def.DISPLAY_PLATE_VIEW = self.display_plate_view_checkbox.isChecked()
        # Parse comma-separated resolutions
        resolutions_str = self.well_resolutions_edit.text()
        try:
            control._def.DOWNSAMPLED_WELL_RESOLUTIONS_UM = [
                float(x.strip()) for x in resolutions_str.split(",") if x.strip()
            ]
        except ValueError:
            self._log.warning(f"Invalid well resolutions format: {resolutions_str}")
        control._def.DOWNSAMPLED_PLATE_RESOLUTION_UM = self.plate_resolution_spinbox.value()
        control._def.DOWNSAMPLED_Z_PROJECTION = control._def.ZProjectionMode.convert_to_enum(
            self.z_projection_combo.currentText()
        )
        control._def.DOWNSAMPLED_INTERPOLATION_METHOD = control._def.DownsamplingMethod.convert_to_enum(
            self.interpolation_method_combo.currentText()
        )
        control._def.USE_NAPARI_FOR_MOSAIC_DISPLAY = self.display_mosaic_view_checkbox.isChecked()
        control._def.MOSAIC_VIEW_TARGET_PIXEL_SIZE_UM = self.mosaic_pixel_size_spinbox.value()
        control._def.ENABLE_NDVIEWER = self.enable_ndviewer_checkbox.isChecked()

    def _get_changes(self):
        """Get list of settings that have changed from current config.
        Returns list of (name, old, new, requires_restart) tuples."""
        changes = []

        # General settings (live update)
        old_val = self._get_config_value("GENERAL", "file_saving_option", "OME_TIFF")
        new_val = self.file_saving_combo.currentText()
        if old_val != new_val:
            changes.append(("File Saving Format", old_val, new_val, False))

        old_val = self._get_config_value("GENERAL", "default_saving_path", control._def.DEFAULT_SAVING_PATH)
        new_val = self.saving_path_edit.text()
        if old_val != new_val:
            changes.append(("Default Saving Path", old_val, new_val, False))

        old_val = self._get_config_bool("GENERAL", "show_dev_tab", False)
        new_val = self.show_dev_tab_checkbox.isChecked()
        if old_val != new_val:
            changes.append(("Show Dev Tab", str(old_val), str(new_val), False))

        # Acquisition settings (live update)
        old_val = self._get_config_value("GENERAL", "multipoint_autofocus_channel", "BF LED matrix full")
        new_val = self.autofocus_channel_edit.text()
        if old_val != new_val:
            changes.append(("Autofocus Channel", old_val, new_val, False))

        old_val = self._get_config_bool("GENERAL", "enable_flexible_multipoint", True)
        new_val = self.flexible_multipoint_checkbox.isChecked()
        if old_val != new_val:
            changes.append(("Enable Flexible Multipoint", str(old_val), str(new_val), False))

        # Camera settings (require restart)
        old_val = self._get_config_int("CAMERA_CONFIG", "binning_factor_default", 2)
        new_val = self.binning_spinbox.value()
        if old_val != new_val:
            changes.append(("Default Binning Factor", str(old_val), str(new_val), True))

        old_val = self._get_config_value("CAMERA_CONFIG", "flip_image", "None")
        new_val = self.flip_combo.currentText()
        if old_val != new_val:
            changes.append(("Image Flip", old_val, new_val, True))

        old_val = self._get_config_int("CAMERA_CONFIG", "temperature_default", 20)
        new_val = self.temperature_spinbox.value()
        if old_val != new_val:
            changes.append(("Temperature Default", f"{old_val} °C", f"{new_val} °C", True))

        old_val = self._get_config_value("CAMERA_CONFIG", "roi_width_default", "None")
        new_val = "None" if self.roi_width_spinbox.value() == 0 else str(self.roi_width_spinbox.value())
        if old_val != new_val:
            changes.append(("ROI Width", old_val, new_val, True))

        old_val = self._get_config_value("CAMERA_CONFIG", "roi_height_default", "None")
        new_val = "None" if self.roi_height_spinbox.value() == 0 else str(self.roi_height_spinbox.value())
        if old_val != new_val:
            changes.append(("ROI Height", old_val, new_val, True))

        # Advanced - Stage & Motion (require restart)
        old_val = self._get_config_float("GENERAL", "max_velocity_x_mm", 30)
        new_val = self.max_vel_x.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("Max Velocity X", f"{old_val} mm/s", f"{new_val} mm/s", True))

        old_val = self._get_config_float("GENERAL", "max_velocity_y_mm", 30)
        new_val = self.max_vel_y.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("Max Velocity Y", f"{old_val} mm/s", f"{new_val} mm/s", True))

        old_val = self._get_config_float("GENERAL", "max_velocity_z_mm", 3.8)
        new_val = self.max_vel_z.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("Max Velocity Z", f"{old_val} mm/s", f"{new_val} mm/s", True))

        old_val = self._get_config_float("GENERAL", "max_acceleration_x_mm", 500)
        new_val = self.max_accel_x.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("Max Acceleration X", f"{old_val} mm/s2", f"{new_val} mm/s2", True))

        old_val = self._get_config_float("GENERAL", "max_acceleration_y_mm", 500)
        new_val = self.max_accel_y.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("Max Acceleration Y", f"{old_val} mm/s2", f"{new_val} mm/s2", True))

        old_val = self._get_config_float("GENERAL", "max_acceleration_z_mm", 100)
        new_val = self.max_accel_z.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("Max Acceleration Z", f"{old_val} mm/s2", f"{new_val} mm/s2", True))

        old_val = self._get_config_int("GENERAL", "scan_stabilization_time_ms_x", 25)
        new_val = self.scan_stab_x.value()
        if old_val != new_val:
            changes.append(("Scan Stabilization X", f"{old_val} ms", f"{new_val} ms", True))

        old_val = self._get_config_int("GENERAL", "scan_stabilization_time_ms_y", 25)
        new_val = self.scan_stab_y.value()
        if old_val != new_val:
            changes.append(("Scan Stabilization Y", f"{old_val} ms", f"{new_val} ms", True))

        old_val = self._get_config_int("GENERAL", "scan_stabilization_time_ms_z", 20)
        new_val = self.scan_stab_z.value()
        if old_val != new_val:
            changes.append(("Scan Stabilization Z", f"{old_val} ms", f"{new_val} ms", True))

        # Advanced - Autofocus (live update)
        old_val = self._get_config_float("AF", "stop_threshold", 0.85)
        new_val = self.af_stop_threshold.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("AF Stop Threshold", str(old_val), str(new_val), False))

        old_val = self._get_config_int("AF", "crop_width", 800)
        new_val = self.af_crop_width.value()
        if old_val != new_val:
            changes.append(("AF Crop Width", f"{old_val} px", f"{new_val} px", False))

        old_val = self._get_config_int("AF", "crop_height", 800)
        new_val = self.af_crop_height.value()
        if old_val != new_val:
            changes.append(("AF Crop Height", f"{old_val} px", f"{new_val} px", False))

        # Advanced - Hardware (require restart)
        old_val = self._get_config_value("GENERAL", "z_motor_config", "STEPPER")
        new_val = self.z_motor_combo.currentText()
        if old_val != new_val:
            changes.append(("Z Motor Config", old_val, new_val, True))

        old_val = self._get_config_bool("GENERAL", "enable_spinning_disk_confocal", False)
        new_val = self.spinning_disk_checkbox.isChecked()
        if old_val != new_val:
            changes.append(("Enable Spinning Disk", str(old_val), str(new_val), True))

        # LED matrix factors (live update)
        old_val = self._get_config_float("GENERAL", "led_matrix_r_factor", 1.0)
        new_val = self.led_r_factor.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("LED Matrix R Factor", str(old_val), str(new_val), False))

        old_val = self._get_config_float("GENERAL", "led_matrix_g_factor", 1.0)
        new_val = self.led_g_factor.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("LED Matrix G Factor", str(old_val), str(new_val), False))

        old_val = self._get_config_float("GENERAL", "led_matrix_b_factor", 1.0)
        new_val = self.led_b_factor.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("LED Matrix B Factor", str(old_val), str(new_val), False))

        old_val = self._get_config_float("GENERAL", "illumination_intensity_factor", 0.6)
        new_val = self.illumination_factor.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("Illumination Intensity Factor", str(old_val), str(new_val), False))

        # Advanced - Development Settings
        # Enable/disable requires restart (for warning banner/dialog), but speed/compression
        # take effect on next acquisition since each acquisition starts a fresh subprocess
        old_val = self._get_config_bool("GENERAL", "simulated_disk_io_enabled", False)
        new_val = self.simulated_io_checkbox.isChecked()
        if old_val != new_val:
            changes.append(("Simulated Disk I/O", str(old_val), str(new_val), True))

        old_val = self._get_config_float("GENERAL", "simulated_disk_io_speed_mb_s", 200.0)
        new_val = self.simulated_io_speed_spinbox.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("Simulated Write Speed", f"{old_val} MB/s", f"{new_val} MB/s", False))

        old_val = self._get_config_bool("GENERAL", "simulated_disk_io_compression", True)
        new_val = self.simulated_io_compression_checkbox.isChecked()
        if old_val != new_val:
            changes.append(("Simulate Compression", str(old_val), str(new_val), False))

        # Advanced - Acquisition Throttling (takes effect on next acquisition)
        old_val = self._get_config_bool(
            "GENERAL", "acquisition_throttling_enabled", control._def.ACQUISITION_THROTTLING_ENABLED
        )
        new_val = self.throttling_enabled_checkbox.isChecked()
        if old_val != new_val:
            changes.append(("Acquisition Throttling", str(old_val), str(new_val), False))

        old_val = self._get_config_int(
            "GENERAL", "acquisition_max_pending_jobs", control._def.ACQUISITION_MAX_PENDING_JOBS
        )
        new_val = self.max_pending_jobs_spinbox.value()
        if old_val != new_val:
            changes.append(("Max Pending Jobs", str(old_val), str(new_val), False))

        old_val = self._get_config_float(
            "GENERAL", "acquisition_max_pending_mb", control._def.ACQUISITION_MAX_PENDING_MB
        )
        new_val = self.max_pending_mb_spinbox.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("Max Pending RAM", f"{old_val} MB", f"{new_val} MB", False))

        old_val = self._get_config_float(
            "GENERAL", "acquisition_throttle_timeout_s", control._def.ACQUISITION_THROTTLE_TIMEOUT_S
        )
        new_val = self.throttle_timeout_spinbox.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("Throttle Timeout", f"{old_val} s", f"{new_val} s", False))

        # Advanced - Position Limits (live update)
        old_val = self._get_config_float("SOFTWARE_POS_LIMIT", "x_positive", 115)
        new_val = self.limit_x_pos.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("X Positive Limit", f"{old_val} mm", f"{new_val} mm", False))

        old_val = self._get_config_float("SOFTWARE_POS_LIMIT", "x_negative", 5)
        new_val = self.limit_x_neg.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("X Negative Limit", f"{old_val} mm", f"{new_val} mm", False))

        old_val = self._get_config_float("SOFTWARE_POS_LIMIT", "y_positive", 76)
        new_val = self.limit_y_pos.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("Y Positive Limit", f"{old_val} mm", f"{new_val} mm", False))

        old_val = self._get_config_float("SOFTWARE_POS_LIMIT", "y_negative", 4)
        new_val = self.limit_y_neg.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("Y Negative Limit", f"{old_val} mm", f"{new_val} mm", False))

        old_val = self._get_config_float("SOFTWARE_POS_LIMIT", "z_positive", 6)
        new_val = self.limit_z_pos.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("Z Positive Limit", f"{old_val} mm", f"{new_val} mm", False))

        old_val = self._get_config_float("SOFTWARE_POS_LIMIT", "z_negative", 0.05)
        new_val = self.limit_z_neg.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("Z Negative Limit", f"{old_val} mm", f"{new_val} mm", False))

        # Advanced - Tracking (hidden but still tracked)
        old_val = self._get_config_bool("GENERAL", "enable_tracking", False)
        new_val = self.enable_tracking_checkbox.isChecked()
        if old_val != new_val:
            changes.append(("Enable Tracking", str(old_val), str(new_val), False))

        old_val = self._get_config_value("TRACKING", "default_tracker", "csrt")
        new_val = self.default_tracker_combo.currentText()
        if old_val != new_val:
            changes.append(("Default Tracker", old_val, new_val, False))

        old_val = self._get_config_int("TRACKING", "search_area_ratio", 10)
        new_val = self.search_area_ratio.value()
        if old_val != new_val:
            changes.append(("Search Area Ratio", str(old_val), str(new_val), False))

        # Advanced - Diagnostics (live update)
        old_val = self._get_config_bool("GENERAL", "enable_memory_profiling", control._def.ENABLE_MEMORY_PROFILING)
        new_val = self.enable_memory_profiling_checkbox.isChecked()
        if old_val != new_val:
            changes.append(("Enable RAM Monitoring", str(old_val), str(new_val), False))

        # Views settings (live update)
        # NOTE: Compare against control._def values (runtime state) since UI is initialized from control._def.
        # This enables MCP commands to modify these settings for RAM usage diagnostics.
        # See PR #424 for context. This pattern may change if settings architecture is refactored.
        old_val = control._def.SAVE_DOWNSAMPLED_WELL_IMAGES
        new_val = self.save_downsampled_checkbox.isChecked()
        if old_val != new_val:
            changes.append(("Save Downsampled Well Images", str(old_val), str(new_val), False))

        old_val = control._def.DISPLAY_PLATE_VIEW
        new_val = self.display_plate_view_checkbox.isChecked()
        if old_val != new_val:
            changes.append(("Display Plate View *", str(old_val), str(new_val), True))

        old_val = ", ".join(str(r) for r in control._def.DOWNSAMPLED_WELL_RESOLUTIONS_UM)
        new_val = self.well_resolutions_edit.text()
        if old_val != new_val:
            changes.append(("Well Resolutions", old_val, new_val, False))

        old_val = control._def.DOWNSAMPLED_PLATE_RESOLUTION_UM
        new_val = self.plate_resolution_spinbox.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("Target Pixel Size", f"{old_val} μm", f"{new_val} μm", False))

        old_val = control._def.DOWNSAMPLED_Z_PROJECTION.value
        new_val = self.z_projection_combo.currentText()
        if old_val != new_val:
            changes.append(("Z-Projection Mode", old_val, new_val, False))

        old_val = control._def.DOWNSAMPLED_INTERPOLATION_METHOD.value
        new_val = self.interpolation_method_combo.currentText()
        if old_val != new_val:
            changes.append(("Interpolation Method", old_val, new_val, False))

        old_val = control._def.USE_NAPARI_FOR_MOSAIC_DISPLAY
        new_val = self.display_mosaic_view_checkbox.isChecked()
        if old_val != new_val:
            changes.append(("Display Mosaic View *", str(old_val), str(new_val), True))

        old_val = control._def.MOSAIC_VIEW_TARGET_PIXEL_SIZE_UM
        new_val = self.mosaic_pixel_size_spinbox.value()
        if not self._floats_equal(old_val, new_val):
            changes.append(("Mosaic Target Pixel Size", f"{old_val} μm", f"{new_val} μm", False))

        old_val = control._def.ENABLE_NDVIEWER
        new_val = self.enable_ndviewer_checkbox.isChecked()
        if old_val != new_val:
            changes.append(("Enable NDViewer *", str(old_val), str(new_val), True))

        # Hardware Simulation settings (require restart)
        old_val = self._get_config_value("SIMULATION", "simulate_camera", "false").lower() == "true"
        new_val = self.sim_camera_checkbox.isChecked()
        if old_val != new_val:
            changes.append(("Simulate Camera *", str(old_val), str(new_val), True))

        old_val = self._get_config_value("SIMULATION", "simulate_microcontroller", "false").lower() == "true"
        new_val = self.sim_mcu_checkbox.isChecked()
        if old_val != new_val:
            changes.append(("Simulate MCU/Stage *", str(old_val), str(new_val), True))

        old_val = self._get_config_value("SIMULATION", "simulate_spinning_disk", "false").lower() == "true"
        new_val = self.sim_spinning_disk_checkbox.isChecked()
        if old_val != new_val:
            changes.append(("Simulate Spinning Disk *", str(old_val), str(new_val), True))

        old_val = self._get_config_value("SIMULATION", "simulate_filter_wheel", "false").lower() == "true"
        new_val = self.sim_filter_wheel_checkbox.isChecked()
        if old_val != new_val:
            changes.append(("Simulate Filter Wheel *", str(old_val), str(new_val), True))

        old_val = self._get_config_value("SIMULATION", "simulate_objective_changer", "false").lower() == "true"
        new_val = self.sim_objective_changer_checkbox.isChecked()
        if old_val != new_val:
            changes.append(("Simulate Objective Changer *", str(old_val), str(new_val), True))

        old_val = self._get_config_value("SIMULATION", "simulate_laser_af_camera", "false").lower() == "true"
        new_val = self.sim_laser_af_camera_checkbox.isChecked()
        if old_val != new_val:
            changes.append(("Simulate Laser AF Camera *", str(old_val), str(new_val), True))

        return changes

    def _offer_restart_dialog(self):
        """Show a dialog offering to restart the application now."""
        msg = QMessageBox(self)
        msg.setWindowTitle("Restart Required")
        msg.setText("Settings have been saved. This change requires a restart to take effect.")
        msg.setInformativeText("Would you like to restart now?")
        msg.setIcon(QMessageBox.Information)
        restart_btn = msg.addButton("Restart Now", QMessageBox.AcceptRole)
        msg.addButton("Later", QMessageBox.RejectRole)
        msg.exec_()
        if msg.clickedButton() == restart_btn:
            self._trigger_restart()

    def _trigger_restart(self):
        """Trigger application restart via callback."""
        if self._on_restart:
            try:
                self._on_restart()
            except Exception as e:
                self._log.exception("Failed to restart application")
                QMessageBox.warning(
                    self,
                    "Restart Failed",
                    f"An error occurred while trying to restart the application.\n\n"
                    f"Error: {e}\n\nPlease restart the application manually.",
                )
        else:
            self._log.error("No restart callback configured")
            QMessageBox.warning(
                self,
                "Restart Failed",
                "Could not trigger automatic restart.\nPlease restart the application manually.",
            )

    def _save_and_close(self):
        changes = self._get_changes()

        if not changes:
            self.accept()
            return

        # Check if any changes require restart
        requires_restart = any(change[3] for change in changes)

        # For single change, save directly without confirmation
        if len(changes) == 1:
            if not self._apply_settings():
                return  # Save failed, dialog stays open
            if requires_restart:
                self._offer_restart_dialog()
            self.accept()
            return

        # For multiple changes, show confirmation dialog
        dialog = QDialog(self)
        dialog.setWindowTitle("Confirm Changes")
        dialog.setMinimumWidth(450)
        if self.isModal():
            dialog.setModal(True)
        layout = QVBoxLayout(dialog)

        label = QLabel("The following settings will be changed:")
        layout.addWidget(label)

        # Create text showing before/after for each change
        changes_text = QTextEdit()
        changes_text.setReadOnly(True)
        changes_lines = []
        for name, old_val, new_val, needs_restart in changes:
            restart_note = " [restart required]" if needs_restart else ""
            changes_lines.append(f"{name}{restart_note}:\n  Before: {old_val}\n  After:  {new_val}")
        changes_text.setPlainText("\n\n".join(changes_lines))
        changes_text.setMinimumHeight(200)
        layout.addWidget(changes_text)

        # Only show restart warning if at least one change requires restart
        if requires_restart:
            note_label = QLabel(
                "Note: Settings marked [restart required] will only take effect after restarting the software."
            )
            note_label.setStyleSheet("color: #666; font-style: italic;")
            note_label.setWordWrap(True)
            layout.addWidget(note_label)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        # Track which button was clicked
        dialog.restart_requested = False

        if requires_restart:
            save_restart_btn = QPushButton("Save and Restart")
            save_restart_btn.setToolTip("Save settings and restart the application now")

            def on_save_restart():
                dialog.restart_requested = True
                dialog.accept()

            save_restart_btn.clicked.connect(on_save_restart)
            button_layout.addWidget(save_restart_btn)

        save_btn = QPushButton("Save")
        cancel_btn = QPushButton("Cancel")
        save_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)
        button_layout.addWidget(save_btn)
        button_layout.addWidget(cancel_btn)
        layout.addLayout(button_layout)

        if dialog.exec_() == QDialog.Accepted:
            if self._apply_settings():
                if dialog.restart_requested:
                    self._trigger_restart()
                self.accept()
            # If save failed, dialog stays open (error already shown)


class StageUtils(QDialog):
    """Dialog containing microscope utility functions like homing, zeroing, and slide positioning."""

    signal_threaded_stage_move_started = Signal()
    signal_loading_position_reached = Signal()
    signal_scanning_position_reached = Signal()

    def __init__(self, stage: AbstractStage, live_controller: LiveController, is_wellplate: bool, parent=None):
        super().__init__(parent)
        self.log = squid.logging.get_logger(self.__class__.__name__)
        self.stage = stage
        self.live_controller = live_controller
        self.is_wellplate = is_wellplate
        self.slide_position = None

        self.setWindowTitle("Stage Utils")
        self.setModal(False)  # Allow interaction with main window while dialog is open
        self.setup_ui()

    def setup_ui(self):
        """Setup the UI components."""
        # Create buttons
        self.btn_home_X = QPushButton("Home X")
        self.btn_home_X.setDefault(False)
        self.btn_home_X.setEnabled(HOMING_ENABLED_X)

        self.btn_home_Y = QPushButton("Home Y")
        self.btn_home_Y.setDefault(False)
        self.btn_home_Y.setEnabled(HOMING_ENABLED_Y)

        self.btn_home_Z = QPushButton("Home Z")
        self.btn_home_Z.setDefault(False)
        self.btn_home_Z.setEnabled(HOMING_ENABLED_Z)

        self.btn_zero_X = QPushButton("Zero X")
        self.btn_zero_X.setDefault(False)

        self.btn_zero_Y = QPushButton("Zero Y")
        self.btn_zero_Y.setDefault(False)

        self.btn_zero_Z = QPushButton("Zero Z")
        self.btn_zero_Z.setDefault(False)

        self.btn_load_slide = QPushButton("Move To Loading Position")
        self.btn_load_slide.setStyleSheet("background-color: #C2C2FF")

        # Connect buttons to functions
        self.btn_home_X.clicked.connect(self.home_x)
        self.btn_home_Y.clicked.connect(self.home_y)
        self.btn_home_Z.clicked.connect(self.home_z)
        self.btn_zero_X.clicked.connect(self.zero_x)
        self.btn_zero_Y.clicked.connect(self.zero_y)
        self.btn_zero_Z.clicked.connect(self.zero_z)
        self.btn_load_slide.clicked.connect(self.switch_position)

        # Layout
        main_layout = QVBoxLayout()

        # Homing section
        homing_group = QGroupBox("Homing")
        homing_layout = QHBoxLayout()
        homing_layout.addWidget(self.btn_home_X)
        homing_layout.addWidget(self.btn_home_Y)
        homing_layout.addWidget(self.btn_home_Z)
        homing_group.setLayout(homing_layout)

        # Zero section
        zero_group = QGroupBox("Zero Position")
        zero_layout = QHBoxLayout()
        zero_layout.addWidget(self.btn_zero_X)
        zero_layout.addWidget(self.btn_zero_Y)
        zero_layout.addWidget(self.btn_zero_Z)
        zero_group.setLayout(zero_layout)

        # Slide positioning section
        slide_group = QGroupBox("Slide Positioning")
        slide_layout = QVBoxLayout()
        slide_layout.addWidget(self.btn_load_slide)
        slide_group.setLayout(slide_layout)

        # Add sections to main layout
        main_layout.addWidget(homing_group)
        main_layout.addWidget(zero_group)
        main_layout.addWidget(slide_group)

        # Close button
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        main_layout.addWidget(close_button)

        self.setLayout(main_layout)

    def home_x(self):
        """Home X axis with confirmation dialog."""
        self._show_confirmation_dialog(x=True, y=False, z=False, theta=False)

    def home_y(self):
        """Home Y axis with confirmation dialog."""
        self._show_confirmation_dialog(x=False, y=True, z=False, theta=False)

    def home_z(self):
        """Home Z axis with confirmation dialog."""
        self._show_confirmation_dialog(x=False, y=False, z=True, theta=False)
        move_z_axis_to_safety_position(self.stage)

    def _show_confirmation_dialog(self, x: bool, y: bool, z: bool, theta: bool):
        """Display a confirmation dialog and home the specified axis if confirmed."""
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Information)
        msg.setText("Confirm your action")
        msg.setInformativeText("Click OK to run homing")
        msg.setWindowTitle("Confirmation")
        msg.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        msg.setDefaultButton(QMessageBox.Cancel)
        retval = msg.exec_()
        if QMessageBox.Ok == retval:
            self.stage.home(x=x, y=y, z=z, theta=theta)

    def zero_x(self):
        """Zero X axis position."""
        self.stage.zero(x=True, y=False, z=False, theta=False)

    def zero_y(self):
        """Zero Y axis position."""
        self.stage.zero(x=False, y=True, z=False, theta=False)

    def zero_z(self):
        """Zero Z axis position."""
        self.stage.zero(x=False, y=False, z=True, theta=False)

    def switch_position(self):
        """Switch between loading and scanning positions."""
        self._was_live = self.live_controller.is_live
        if self._was_live:
            self.live_controller.stop_live()
        self.signal_threaded_stage_move_started.emit()
        if self.slide_position != "loading":
            move_to_loading_position(
                self.stage,
                blocking=False,
                callback=self._callback_loading_position_reached,
                is_wellplate=self.is_wellplate,
            )
        else:
            move_to_scanning_position(
                self.stage,
                blocking=False,
                callback=self._callback_scanning_position_reached,
                is_wellplate=self.is_wellplate,
            )
        self.btn_load_slide.setEnabled(False)

    def _callback_loading_position_reached(self, success: bool, error_message: Optional[str]):
        """Handle slide loading position reached signal."""
        self.slide_position = "loading"
        self.btn_load_slide.setStyleSheet("background-color: #C2FFC2")
        self.btn_load_slide.setText("Move to Scanning Position")
        self.btn_load_slide.setEnabled(True)
        if self._was_live:
            self.live_controller.start_live()
        if not success:
            QMessageBox.warning(self, "Error", error_message)
        self.signal_loading_position_reached.emit()

    def _callback_scanning_position_reached(self, success: bool, error_message: Optional[str]):
        """Handle slide scanning position reached signal."""
        self.slide_position = "scanning"
        self.btn_load_slide.setStyleSheet("background-color: #C2C2FF")
        self.btn_load_slide.setText("Move to Loading Position")
        self.btn_load_slide.setEnabled(True)
        if self._was_live:
            self.live_controller.start_live()
        if not success:
            QMessageBox.warning(self, "Error", error_message)
        self.signal_scanning_position_reached.emit()


