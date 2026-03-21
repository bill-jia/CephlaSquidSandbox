from ._bootstrap import *

class WavelengthWidget(QWidget):
    """Widget for wavelength field with checkbox to toggle between int and N/A."""

    def __init__(self, wavelength_nm=None, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(4)

        self.checkbox = QCheckBox()
        self.checkbox.setToolTip("Check to set wavelength, uncheck for N/A")
        self.checkbox.stateChanged.connect(self._on_checkbox_changed)
        layout.addWidget(self.checkbox)

        self.spinbox = QSpinBox()
        self.spinbox.setRange(200, 900)
        self.spinbox.setValue(405)
        layout.addWidget(self.spinbox)

        self.na_label = QLabel("N/A")
        self.na_label.setStyleSheet("color: gray;")
        layout.addWidget(self.na_label)

        # Set initial state
        if wavelength_nm is not None:
            self.checkbox.setChecked(True)
            self.spinbox.setValue(wavelength_nm)
            self.spinbox.setVisible(True)
            self.na_label.setVisible(False)
        else:
            self.checkbox.setChecked(False)
            self.spinbox.setVisible(False)
            self.na_label.setVisible(True)

    def _on_checkbox_changed(self, state):
        checked = state == Qt.Checked
        self.spinbox.setVisible(checked)
        self.na_label.setVisible(not checked)

    def get_wavelength(self):
        """Return wavelength value or None if N/A."""
        if self.checkbox.isChecked():
            return self.spinbox.value()
        return None

    def set_wavelength(self, wavelength_nm):
        """Set wavelength value or N/A."""
        if wavelength_nm is not None:
            self.checkbox.setChecked(True)
            self.spinbox.setValue(wavelength_nm)
        else:
            self.checkbox.setChecked(False)


class IlluminationChannelConfiguratorDialog(QDialog):
    """Dialog for editing illumination channel hardware configuration.

    This edits the machine_configs/illumination_channel_config.yaml file which defines
    the physical illumination hardware. User-facing acquisition settings (display color,
    enabled state, filter position) are configured separately in user profile configs.
    """

    signal_channels_updated = Signal()

    # Column indices for the channels table
    COL_NAME = 0
    COL_TYPE = 1
    COL_PORT = 2
    COL_WAVELENGTH = 3
    COL_CALIBRATION = 4

    def __init__(self, config_repo, parent=None):
        super().__init__(parent)
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self.config_repo = config_repo
        self.illumination_config = None
        self.setWindowTitle("Illumination Channel Configurator")
        self.setMinimumSize(900, 500)
        self._setup_ui()
        self._load_channels()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # Warning label
        warning_label = QLabel(
            "Warning: Illumination channel configuration is hardware-specific. "
            "Modifying these settings may break existing acquisition configurations. "
            "Only change these settings when necessary."
        )
        warning_label.setWordWrap(True)
        warning_label.setStyleSheet("color: #CC0000; font-weight: bold;")
        layout.addWidget(warning_label)

        # Table for illumination channels
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Name", "Type", "Controller Port", "Wavelength (nm)", "Calibration File"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        layout.addWidget(self.table)

        # Buttons
        button_layout = QHBoxLayout()

        self.btn_add = QPushButton("Add Channel")
        self.btn_add.setAutoDefault(False)
        self.btn_add.setDefault(False)
        self.btn_add.clicked.connect(self._add_channel)
        button_layout.addWidget(self.btn_add)

        self.btn_remove = QPushButton("Remove Channel")
        self.btn_remove.setAutoDefault(False)
        self.btn_remove.setDefault(False)
        self.btn_remove.clicked.connect(self._remove_channel)
        button_layout.addWidget(self.btn_remove)

        self.btn_move_up = QPushButton("Move Up")
        self.btn_move_up.setAutoDefault(False)
        self.btn_move_up.clicked.connect(self._move_up)
        button_layout.addWidget(self.btn_move_up)

        self.btn_move_down = QPushButton("Move Down")
        self.btn_move_down.setAutoDefault(False)
        self.btn_move_down.clicked.connect(self._move_down)
        button_layout.addWidget(self.btn_move_down)

        self.btn_port_mapping = QPushButton("Port Mapping...")
        self.btn_port_mapping.setAutoDefault(False)
        self.btn_port_mapping.clicked.connect(self._open_port_mapping)
        button_layout.addWidget(self.btn_port_mapping)

        button_layout.addStretch()

        self.btn_save = QPushButton("Save")
        self.btn_save.setAutoDefault(False)
        self.btn_save.clicked.connect(self._save_changes)
        button_layout.addWidget(self.btn_save)

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setAutoDefault(False)
        self.btn_cancel.clicked.connect(self.reject)
        button_layout.addWidget(self.btn_cancel)

        layout.addLayout(button_layout)

    def _get_calibration_full_path(self, filename):
        """Get full path for calibration file."""
        if not filename:
            return ""
        calib_dir = self.config_repo.machine_configs_path / "intensity_calibrations"
        return str(calib_dir / filename)

    def _load_channels(self):
        """Load illumination channels from YAML config into the table"""
        self.illumination_config = self.config_repo.get_illumination_config()
        if not self.illumination_config:
            return

        # Get available ports (only those with mappings)
        available_ports = self.illumination_config.get_available_ports()

        self.table.setRowCount(len(self.illumination_config.channels))

        for row, channel in enumerate(self.illumination_config.channels):
            # Name (editable)
            name_item = QTableWidgetItem(channel.name)
            self.table.setItem(row, self.COL_NAME, name_item)

            # Type (dropdown)
            type_combo = QComboBox()
            type_combo.addItems(["epi_illumination", "transillumination"])
            type_combo.setCurrentText(channel.type.value)
            type_combo.currentTextChanged.connect(lambda text, r=row: self._on_type_changed(r, text))
            self.table.setCellWidget(row, self.COL_TYPE, type_combo)

            # Controller Port (dropdown) - only ports with mappings
            port_combo = QComboBox()
            port_combo.addItems(available_ports)
            port_combo.setCurrentText(channel.controller_port)
            self.table.setCellWidget(row, self.COL_PORT, port_combo)

            # Wavelength (checkbox + spinbox, or N/A)
            wave_widget = WavelengthWidget(channel.wavelength_nm)
            self.table.setCellWidget(row, self.COL_WAVELENGTH, wave_widget)

            # Calibration file (full path)
            full_path = self._get_calibration_full_path(channel.intensity_calibration_file)
            calib_item = QTableWidgetItem(full_path)
            self.table.setItem(row, self.COL_CALIBRATION, calib_item)

    def _on_type_changed(self, row, new_type):
        """Handle type change - update wavelength default and controller port"""
        wave_widget = self.table.cellWidget(row, self.COL_WAVELENGTH)
        available_ports = self.illumination_config.get_available_ports()

        # Find first available USB and D ports
        first_usb = next((p for p in available_ports if p.startswith("USB")), None)
        first_d = next((p for p in available_ports if p.startswith("D")), None)

        if new_type == "epi_illumination":
            # Set wavelength to default 405nm for epi
            if isinstance(wave_widget, WavelengthWidget):
                wave_widget.set_wavelength(405)

            # Update controller port to first available laser port
            port_combo = self.table.cellWidget(row, self.COL_PORT)
            if port_combo and port_combo.currentText().startswith("USB") and first_d:
                port_combo.setCurrentText(first_d)
        else:
            # Set wavelength to N/A for transillumination
            if isinstance(wave_widget, WavelengthWidget):
                wave_widget.set_wavelength(None)

            # Update controller port to first available USB port
            port_combo = self.table.cellWidget(row, self.COL_PORT)
            if port_combo and port_combo.currentText().startswith("D") and first_usb:
                port_combo.setCurrentText(first_usb)

    def _add_channel(self):
        """Add a new illumination channel"""
        dialog = AddIlluminationChannelDialog(self.illumination_config, self)
        if dialog.exec_() == QDialog.Accepted:
            channel_data = dialog.get_channel_data()
            from control.models.illumination_config import IlluminationChannel

            new_channel = IlluminationChannel(**channel_data)
            self.illumination_config.channels.append(new_channel)
            self._load_channels()

    def _remove_channel(self):
        """Remove selected channel"""
        current_row = self.table.currentRow()
        if current_row < 0:
            return

        name_item = self.table.item(current_row, 0)
        if name_item:
            reply = QMessageBox.question(
                self, "Confirm Removal", f"Remove channel '{name_item.text()}'?", QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                del self.illumination_config.channels[current_row]
                self._load_channels()

    def _move_up(self):
        """Move selected channel up"""
        current_row = self.table.currentRow()
        if current_row <= 0:
            return

        channels = self.illumination_config.channels
        channels[current_row], channels[current_row - 1] = channels[current_row - 1], channels[current_row]
        self._load_channels()
        self.table.selectRow(current_row - 1)

    def _move_down(self):
        """Move selected channel down"""
        current_row = self.table.currentRow()
        if not self.illumination_config or current_row < 0 or current_row >= len(self.illumination_config.channels) - 1:
            return

        channels = self.illumination_config.channels
        channels[current_row], channels[current_row + 1] = channels[current_row + 1], channels[current_row]
        self._load_channels()
        self.table.selectRow(current_row + 1)

    def _open_port_mapping(self):
        """Open the controller port mapping dialog"""
        dialog = ControllerPortMappingDialog(self.config_repo, self)
        dialog.signal_mappings_updated.connect(self._load_channels)
        dialog.exec_()

    def _save_changes(self):
        """Save all changes to illumination channel config"""
        if not self.illumination_config:
            return

        # Confirmation dialog
        reply = QMessageBox.question(
            self,
            "Confirm Save",
            "Saving these changes will modify your hardware configuration.\n"
            "This may affect existing acquisition settings.\n\n"
            "Do you want to continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        from control.models.illumination_config import IlluminationType

        # Validate channel names before saving
        names = []
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, self.COL_NAME)
            if name_item:
                name = name_item.text().strip()
                if not name:
                    QMessageBox.warning(
                        self,
                        "Validation Error",
                        f"Channel name at row {row + 1} cannot be empty.",
                    )
                    return
                if name in names:
                    QMessageBox.warning(
                        self,
                        "Validation Error",
                        f"Duplicate channel name '{name}' found.",
                    )
                    return
                names.append(name)

        # Update channels from table
        for row in range(self.table.rowCount()):
            channel = self.illumination_config.channels[row]

            # Name
            name_item = self.table.item(row, self.COL_NAME)
            if name_item:
                channel.name = name_item.text().strip()

            # Type
            type_widget = self.table.cellWidget(row, self.COL_TYPE)
            if isinstance(type_widget, QComboBox):
                channel.type = IlluminationType(type_widget.currentText())

            # Controller Port
            port_widget = self.table.cellWidget(row, self.COL_PORT)
            if isinstance(port_widget, QComboBox):
                channel.controller_port = port_widget.currentText()

            # Wavelength (checkbox + spinbox widget)
            wave_widget = self.table.cellWidget(row, self.COL_WAVELENGTH)
            if isinstance(wave_widget, WavelengthWidget):
                channel.wavelength_nm = wave_widget.get_wavelength()
            else:
                channel.wavelength_nm = None

            # Calibration file (extract filename from full path)
            calib_item = self.table.item(row, self.COL_CALIBRATION)
            if calib_item:
                calib_text = calib_item.text().strip()
                if calib_text:
                    # Extract just the filename from full path
                    channel.intensity_calibration_file = Path(calib_text).name
                else:
                    channel.intensity_calibration_file = None

        # Save to YAML file
        self.config_repo.save_illumination_config(self.illumination_config)
        self.signal_channels_updated.emit()
        self.accept()


# Keep old name as alias for backwards compatibility
ChannelEditorDialog = IlluminationChannelConfiguratorDialog


class AddIlluminationChannelDialog(QDialog):
    """Dialog for adding a new illumination channel"""

    def __init__(self, illumination_config, parent=None):
        super().__init__(parent)
        self.illumination_config = illumination_config
        self.setWindowTitle("Add Illumination Channel")
        self._setup_ui()

    def _setup_ui(self):
        layout = QFormLayout(self)

        # Channel type
        self.type_combo = QComboBox()
        self.type_combo.addItems(["epi_illumination", "transillumination"])
        self.type_combo.currentTextChanged.connect(self._on_type_changed)
        layout.addRow("Type:", self.type_combo)

        # Name
        self.name_edit = QLineEdit()
        layout.addRow("Name:", self.name_edit)

        # Controller port - only ports with mappings
        available_ports = self.illumination_config.get_available_ports() if self.illumination_config else []
        # Reorder: D ports first for epi_illumination default
        d_ports = [p for p in available_ports if p.startswith("D")]
        usb_ports = [p for p in available_ports if p.startswith("USB")]
        self.port_combo = QComboBox()
        self.port_combo.addItems(d_ports + usb_ports)
        layout.addRow("Controller Port:", self.port_combo)

        # Wavelength (for epi_illumination, optional for transillumination)
        self.wave_spin = QSpinBox()
        self.wave_spin.setRange(200, 900)
        self.wave_spin.setValue(405)
        self.wave_spin.setSpecialValueText("N/A")  # Show N/A when value is minimum
        self.wave_spin.setMinimum(0)  # Allow 0 to represent N/A
        layout.addRow("Wavelength (nm):", self.wave_spin)

        # Calibration file
        self.calib_edit = QLineEdit()
        self.calib_edit.setPlaceholderText("e.g., 405.csv")
        layout.addRow("Calibration File:", self.calib_edit)

        # Buttons
        button_layout = QHBoxLayout()
        self.btn_ok = QPushButton("Add")
        self.btn_ok.clicked.connect(self._validate_and_accept)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)
        button_layout.addWidget(self.btn_ok)
        button_layout.addWidget(self.btn_cancel)
        layout.addRow(button_layout)

    def _validate_and_accept(self):
        """Validate input before accepting"""
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Validation Error", "Channel name cannot be empty.")
            return

        # Check for duplicate names
        if self.illumination_config:
            existing_names = [ch.name for ch in self.illumination_config.channels]
            if name in existing_names:
                QMessageBox.warning(self, "Validation Error", f"Channel '{name}' already exists.")
                return

        self.accept()

    def _on_type_changed(self, type_str):
        is_epi = type_str == "epi_illumination"
        available_ports = self.illumination_config.get_available_ports() if self.illumination_config else []
        first_d = next((p for p in available_ports if p.startswith("D")), None)
        first_usb = next((p for p in available_ports if p.startswith("USB")), None)

        # Update port default based on type
        if is_epi:
            if first_d:
                self.port_combo.setCurrentText(first_d)
            self.wave_spin.setValue(405)
        else:
            if first_usb:
                self.port_combo.setCurrentText(first_usb)
            self.wave_spin.setValue(0)  # Shows as N/A

    def get_channel_data(self):
        from control.models.illumination_config import IlluminationType

        channel_type = IlluminationType(self.type_combo.currentText())
        wavelength = self.wave_spin.value()
        data = {
            "name": self.name_edit.text().strip(),
            "type": channel_type,
            "controller_port": self.port_combo.currentText(),
            "wavelength_nm": wavelength if wavelength > 0 else None,
        }

        calib_text = self.calib_edit.text().strip()
        data["intensity_calibration_file"] = calib_text if calib_text else None

        return data


# Keep old name as alias for backwards compatibility
AddChannelDialog = AddIlluminationChannelDialog


class ControllerPortMappingDialog(QDialog):
    """Dialog for editing controller port to source code mappings.

    Shows all available controller ports (USB1-USB8 for LED matrix, D1-D8 for lasers)
    and their corresponding illumination source codes.
    """

    signal_mappings_updated = Signal()

    def __init__(self, config_repo, parent=None):
        super().__init__(parent)
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self.config_repo = config_repo
        self.illumination_config = None
        self.setWindowTitle("Controller Port Mapping")
        self.setMinimumSize(400, 450)
        self._setup_ui()
        self._load_mappings()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # Info label
        info_label = QLabel(
            "Map controller ports to illumination source codes. "
            "USB ports are for LED matrix patterns, D ports are for lasers."
        )
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: gray; font-style: italic;")
        layout.addWidget(info_label)

        # Table for port mappings
        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Controller Port", "Source Code"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        self.btn_save = QPushButton("Save")
        self.btn_save.clicked.connect(self._save_changes)
        button_layout.addWidget(self.btn_save)

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)
        button_layout.addWidget(self.btn_cancel)

        layout.addLayout(button_layout)

    def _load_mappings(self):
        """Load current port mappings into the table"""
        from control.models.illumination_config import IlluminationChannelConfig

        self.illumination_config = self.config_repo.get_illumination_config()
        if not self.illumination_config:
            return

        port_mapping = self.illumination_config.controller_port_mapping
        all_ports = IlluminationChannelConfig.ALL_PORTS

        self.table.setRowCount(len(all_ports))

        for row, port in enumerate(all_ports):
            # Controller port (read-only)
            port_item = QTableWidgetItem(port)
            port_item.setFlags(port_item.flags() & ~Qt.ItemIsEditable)
            port_item.setBackground(QColor(240, 240, 240))
            self.table.setItem(row, 0, port_item)

            # Source code (editable spinbox with N/A option)
            source_code = port_mapping.get(port)
            source_widget = SourceCodeWidget(source_code)
            self.table.setCellWidget(row, 1, source_widget)

    def _save_changes(self):
        """Save changes to port mappings"""
        if not self.illumination_config:
            return

        # Confirmation dialog
        reply = QMessageBox.question(
            self,
            "Confirm Save",
            "Saving these changes will modify your controller port mappings.\n"
            "This may affect existing acquisition settings.\n\n"
            "Do you want to continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        # Update mappings from table
        new_mapping = {}
        for row in range(self.table.rowCount()):
            port_item = self.table.item(row, 0)
            if not port_item:
                continue

            port = port_item.text()
            source_widget = self.table.cellWidget(row, 1)

            if isinstance(source_widget, SourceCodeWidget):
                source_code = source_widget.get_source_code()
                if source_code is not None:
                    new_mapping[port] = source_code

        self.illumination_config.controller_port_mapping = new_mapping
        self.config_repo.save_illumination_config(self.illumination_config)
        self.signal_mappings_updated.emit()
        self.accept()


class SourceCodeWidget(QWidget):
    """Widget for source code field with checkbox to toggle between int and N/A."""

    def __init__(self, source_code=None, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(4)

        self.checkbox = QCheckBox()
        self.checkbox.setToolTip("Check to set source code, uncheck for N/A")
        self.checkbox.stateChanged.connect(self._on_checkbox_changed)
        layout.addWidget(self.checkbox)

        self.spinbox = QSpinBox()
        self.spinbox.setRange(0, 30)
        self.spinbox.setValue(0)
        layout.addWidget(self.spinbox)

        self.na_label = QLabel("N/A")
        self.na_label.setStyleSheet("color: gray;")
        layout.addWidget(self.na_label)

        # Set initial state
        if source_code is not None:
            self.checkbox.setChecked(True)
            self.spinbox.setValue(source_code)
            self.spinbox.setVisible(True)
            self.na_label.setVisible(False)
        else:
            self.checkbox.setChecked(False)
            self.spinbox.setVisible(False)
            self.na_label.setVisible(True)

    def _on_checkbox_changed(self, state):
        checked = state == Qt.Checked
        self.spinbox.setVisible(checked)
        self.na_label.setVisible(not checked)

    def get_source_code(self):
        """Return source code value or None if N/A."""
        if self.checkbox.isChecked():
            return self.spinbox.value()
        return None

    def set_source_code(self, source_code):
        """Set source code value or N/A."""
        if source_code is not None:
            self.checkbox.setChecked(True)
            self.spinbox.setValue(source_code)
        else:
            self.checkbox.setChecked(False)


# Keep old name as alias for backwards compatibility
AdvancedChannelMappingDialog = ControllerPortMappingDialog


