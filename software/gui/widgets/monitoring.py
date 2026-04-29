from ._bootstrap import *

class RAMMonitorWidget(QWidget):
    """Compact RAM monitor widget for status bar.

    Displays current RAM usage continuously when enabled. During acquisition,
    connects to MemoryMonitor for more detailed tracking.

    State Invariants:
        - When _memory_monitor is set, updates come via signals (timer is paused)
        - When _memory_monitor is None, updates come via timer

    Attributes:
        label_current: QLabel showing current RAM usage
        label_available: QLabel showing available system RAM
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._memory_monitor = None
        self._session_peak_mb = 0.0  # Track peak RAM usage across the session
        self._log = logging.getLogger("squid." + self.__class__.__name__)
        self._setup_ui()
        self._setup_timer()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)

        self.label_icon = QLabel("RAM usage:")
        self.label_icon.setStyleSheet("font-weight: bold;")

        # Value labels with fixed widths for stable layout
        fm = QFontMetrics(self.font())
        self.label_current = self._create_value_label(fm.horizontalAdvance("88.88 GB"))
        self.label_peak = self._create_value_label(fm.horizontalAdvance("88.88 GB"))
        self.label_available = self._create_value_label(fm.horizontalAdvance("888.8 GB"))

        # Separator and descriptor labels
        separator_style = "color: #666;"
        self.label_separator1 = QLabel("|")
        self.label_separator1.setStyleSheet(separator_style)
        self.label_peak_label = QLabel("peak:")
        self.label_peak_label.setStyleSheet(separator_style)
        self.label_separator2 = QLabel("|")
        self.label_separator2.setStyleSheet(separator_style)
        self.label_available_label = QLabel("available:")
        self.label_available_label.setStyleSheet(separator_style)

        layout.addWidget(self.label_icon)
        layout.addWidget(self.label_current)
        layout.addWidget(self.label_separator1)
        layout.addWidget(self.label_peak_label)
        layout.addWidget(self.label_peak)
        layout.addWidget(self.label_separator2)
        layout.addWidget(self.label_available_label)
        layout.addWidget(self.label_available)

    def _create_value_label(self, width: int) -> QLabel:
        """Create a left-aligned value label with fixed width."""
        label = QLabel("--")
        label.setFixedWidth(width)
        label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        return label

    def _setup_timer(self):
        """Setup timer for periodic memory updates when not connected to monitor."""
        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(self._update_memory_display)
        self._update_timer.setInterval(1000)  # Update every 1 second

    def start_monitoring(self, reset_peak: bool = True):
        """Start continuous memory monitoring.

        Args:
            reset_peak: If True, reset session peak tracking. Set to False when
                       resuming monitoring after disconnecting from an acquisition monitor.
        """
        if self._memory_monitor is not None:
            self._log.warning("Cannot start timer while connected to external monitor")
            return

        self._log.info("Starting continuous RAM monitoring timer")
        if reset_peak:
            self._session_peak_mb = 0.0
            self.label_peak.setText("--")
        self._update_memory_display()  # Initial update
        self._update_timer.start()

    def stop_monitoring(self):
        """Stop continuous memory monitoring."""
        self._update_timer.stop()
        self.label_current.setText("--")
        self.label_peak.setText("--")
        self.label_available.setText("--")

    def _update_memory_display(self):
        """Update memory display using direct measurement."""
        if self._memory_monitor is not None:
            # During acquisition, let the monitor signals handle updates
            return

        try:
            from control.core.memory_profiler import get_memory_footprint_mb

            # Get current process memory usage
            footprint_mb = get_memory_footprint_mb(os.getpid())
            # self._log.debug(f"RAM monitor update: footprint={footprint_mb:.1f} MB")
            if footprint_mb > 0:
                self._session_peak_mb = max(self._session_peak_mb, footprint_mb)
                current_gb = footprint_mb / 1024
                peak_gb = self._session_peak_mb / 1024
                self.label_current.setText(f"{current_gb:.2f} GB")
                self.label_peak.setText(f"{peak_gb:.2f} GB")
            else:
                # Footprint unavailable on this platform/configuration
                self.label_current.setText("N/A")
                self.label_peak.setText("N/A")
                self._log.debug("Memory footprint unavailable (platform may not support this metric)")

            # Get system available memory
            mem_info = psutil.virtual_memory()
            available_gb = mem_info.available / (1024**3)
            self.label_available.setText(f"{available_gb:.1f} GB")
        except Exception as e:
            self._log.warning(f"RAM monitor update failed: {e}")

    def connect_monitor(self, memory_monitor: Optional["MemoryMonitor"]) -> None:
        """Connect to a MemoryMonitor's signals for live updates during acquisition.

        When connected, the timer-based updates are paused and updates come via signals.

        Args:
            memory_monitor: MemoryMonitor instance with signals attribute.
        """
        if memory_monitor is not None:
            self._update_timer.stop()  # Pause timer - signals will handle updates
        self._memory_monitor = memory_monitor
        if memory_monitor is not None and memory_monitor.signals is not None:
            memory_monitor.signals.footprint_updated.connect(self._on_footprint_updated)

    def disconnect_monitor(self) -> None:
        """Disconnect from acquisition monitor.

        Note: This method only disconnects from the monitor and clears the reference.
        It does NOT restart the timer - the caller is responsible for deciding whether
        to call start_monitoring() or stop_monitoring() based on the current settings.
        This avoids coupling the widget to control._def settings.
        """
        if self._memory_monitor is not None and self._memory_monitor.signals is not None:
            try:
                self._memory_monitor.signals.footprint_updated.disconnect(self._on_footprint_updated)
            except RuntimeError:
                # Already disconnected - this is expected
                self._log.debug("Signal already disconnected")
            except TypeError as e:
                # Unexpected - slot signature mismatch could indicate a bug
                self._log.warning(f"Signal disconnect type error (possible bug): {e}")
        self._memory_monitor = None
        # Timer is NOT started here - caller decides via start_monitoring()/stop_monitoring()

    def _on_footprint_updated(self, footprint_mb: float) -> None:
        """Handle footprint update signal from MemoryMonitor.

        Args:
            footprint_mb: Current memory footprint in megabytes.
        """
        # Track peak and display in GB for readability
        self._session_peak_mb = max(self._session_peak_mb, footprint_mb)
        current_gb = footprint_mb / 1024
        peak_gb = self._session_peak_mb / 1024
        self.label_current.setText(f"{current_gb:.2f} GB")
        self.label_peak.setText(f"{peak_gb:.2f} GB")

        # Also update available RAM
        try:
            mem_info = psutil.virtual_memory()
            available_gb = mem_info.available / (1024**3)
            self.label_available.setText(f"{available_gb:.1f} GB")
        except Exception as e:
            self._log.debug(f"Failed to read available RAM: {e}")
            self.label_available.setText("--")

    def closeEvent(self, event):
        """Ensure monitoring resources are cleaned up when the widget closes."""
        try:
            self.stop_monitoring()
        except Exception as e:
            self._log.debug(f"Error stopping monitoring on close: {e}")

        try:
            self.disconnect_monitor()
        except Exception as e:
            self._log.debug(f"Error disconnecting monitor on close: {e}")

        super().closeEvent(event)


class BackpressureMonitorWidget(QWidget):
    """Compact backpressure monitor widget for status bar.

    Displays pending jobs and bytes during acquisition when backpressure
    throttling is enabled. Shows a warning indicator when throttling is active.
    """

    # How long to keep [THROTTLED] visible after throttle releases (in update cycles)
    THROTTLE_STICKY_CYCLES = 4  # 4 cycles * 500ms = 2 seconds

    def __init__(self, parent=None):
        super().__init__(parent)
        self._controller = None
        self._log = logging.getLogger("squid." + self.__class__.__name__)
        self._throttle_sticky_counter = 0  # Countdown for sticky throttle indicator
        self._setup_ui()
        self._setup_timer()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)

        self.label_prefix = QLabel("Queue:")
        self.label_prefix.setStyleSheet("font-weight: bold;")

        # Value labels with fixed widths for stable layout
        fm = QFontMetrics(self.font())
        self.label_jobs = self._create_value_label(fm.horizontalAdvance("888/888 jobs"))
        self.label_bytes = self._create_value_label(fm.horizontalAdvance("8888.8/8888.8 MB"))

        self.label_separator = QLabel("|")
        self.label_separator.setStyleSheet("color: #666;")

        self.label_throttled = QLabel("")
        self.label_throttled.setStyleSheet("color: #e74c3c; font-weight: bold;")

        layout.addWidget(self.label_prefix)
        layout.addWidget(self.label_jobs)
        layout.addWidget(self.label_separator)
        layout.addWidget(self.label_bytes)
        layout.addWidget(self.label_throttled)

    def _create_value_label(self, width: int) -> QLabel:
        """Create a left-aligned value label with fixed width."""
        label = QLabel("--")
        label.setFixedWidth(width)
        label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        return label

    def _setup_timer(self):
        """Setup timer for periodic backpressure updates."""
        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(self._update_display)
        self._update_timer.setInterval(500)  # Update every 500ms

    def start_monitoring(self, controller: "BackpressureController") -> None:
        """Start monitoring backpressure stats.

        Args:
            controller: BackpressureController instance to monitor.
        """
        if controller is None:
            self._log.warning("start_monitoring called with None controller")
            return

        self._controller = controller
        self._throttle_sticky_counter = 0  # Reset state for clean start
        self._log.info("Starting backpressure monitoring")
        self._update_display()  # Initial update
        self._update_timer.start()

    def stop_monitoring(self) -> None:
        """Stop monitoring and reset display."""
        self._update_timer.stop()
        self._controller = None
        self._throttle_sticky_counter = 0
        self.label_jobs.setText("--")
        self.label_bytes.setText("--")
        self.label_throttled.setText("")

    def _update_display(self) -> None:
        """Update display with current backpressure stats."""
        if self._controller is None:
            return

        try:
            stats = self._controller.get_stats()

            self.label_jobs.setText(f"{stats.pending_jobs}/{stats.max_pending_jobs} jobs")
            self.label_bytes.setText(f"{stats.pending_bytes_mb:.1f}/{stats.max_pending_mb:.1f} MB")

            # Sticky throttle indicator: stays visible for THROTTLE_STICKY_CYCLES after release
            if stats.is_throttled:
                self._throttle_sticky_counter = self.THROTTLE_STICKY_CYCLES
                self.label_throttled.setText("[THROTTLED]")
            elif self._throttle_sticky_counter > 0:
                self._throttle_sticky_counter -= 1
                if self._throttle_sticky_counter == 0:
                    self.label_throttled.setText("")

        except (BrokenPipeError, EOFError) as e:
            # Multiprocessing communication ended - acquisition finished
            self._log.debug(f"Backpressure controller communication ended: {e}")
            self.stop_monitoring()
        except Exception as e:
            self._log.warning(f"Backpressure monitor update failed: {e}")
            self.stop_monitoring()

    def closeEvent(self, event):
        """Ensure monitoring resources are cleaned up when the widget closes."""
        try:
            self.stop_monitoring()
        except Exception as e:
            self._log.debug(f"Error stopping monitoring on close: {e}")

        super().closeEvent(event)


def _is_filter_wheel_enabled(config_repo=None) -> bool:
    """True if ``emission_filter_wheel`` is enabled in MachineConfig."""
    from control.core.config.repository import ConfigRepository

    repo = config_repo or ConfigRepository()
    mc = repo.get_machine_config()
    d = mc.get_device("emission_filter_wheel")
    return d is not None and d.enabled


def _populate_filter_positions_for_combo(
    combo: QComboBox,
    channel_wheel: Optional[str],
    config_repo,
    current_position: Optional[int] = None,
) -> None:
    """Populate filter position dropdown, auto-resolving wheel selection.

    Args:
        combo: The QComboBox to populate
        channel_wheel: Raw filter_wheel value from channel (None, "auto", or wheel name)
        config_repo: ConfigRepository instance
        current_position: Position to select (None for first position)
    """
    combo.clear()

    registry = config_repo.get_filter_wheel_registry()
    has_registry = registry and registry.filter_wheels

    # No filter wheel system at all
    if not has_registry and not _is_filter_wheel_enabled():
        combo.addItem("N/A", None)
        combo.setEnabled(False)
        return

    # Resolve wheel: explicit name, or auto-select first wheel
    wheel = None
    if channel_wheel and channel_wheel not in ("(None)", "auto"):
        # Explicit wheel name specified
        wheel = registry.get_wheel_by_name(channel_wheel) if registry else None
        if not wheel and registry:
            logger.warning(f"Filter wheel '{channel_wheel}' not found in registry")
    elif has_registry:
        # Auto-select first wheel (works for both single and multi-wheel systems)
        wheel = registry.get_first_wheel()

    if not wheel:
        # No wheel resolved - check if we should show default positions or N/A
        if has_registry or _is_filter_wheel_enabled():
            # Filter wheel enabled but no registry - show default positions
            combo.setEnabled(True)
            for pos in range(1, 9):
                combo.addItem(f"Position {pos}", pos)
        else:
            combo.addItem("N/A", None)
            combo.setEnabled(False)
            return
    else:
        # Populate from wheel's actual positions
        combo.setEnabled(True)
        for pos, filter_name in sorted(wheel.positions.items()):
            combo.addItem(f"{pos}: {filter_name}", pos)

    # Select current position, or default to first
    if current_position is not None:
        for i in range(combo.count()):
            if combo.itemData(i) == current_position:
                combo.setCurrentIndex(i)
                return
    combo.setCurrentIndex(0)


class ObservationStateConfiguratorDialog(QDialog):
    """Dialog for editing acquisition channel configurations (observation presets).

    Edits user_profiles/{profile}/observation_presets/*.yaml.
    Each preset is an ObservationState representing one acquisition channel.
    """

    signal_channels_updated = Signal()

    # Column indices for the channels table
    COL_ENABLED = 0
    COL_NAME = 1
    COL_ILLUMINATION = 2
    COL_CAMERA = 3
    COL_FILTER_WHEEL = 4
    COL_FILTER_POSITION = 5
    COL_DISPLAY_COLOR = 6

    def __init__(self, config_repo, parent=None):
        super().__init__(parent)
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self.config_repo = config_repo
        self._preset_states: list = []      # List[ObservationState] currently in dialog
        self._original_names: list = []     # names as loaded, to track deletions
        self.illumination_config = None
        self.setWindowTitle("Acquisition Channel Configuration")
        self.setMinimumSize(700, 400)
        self._setup_ui()
        self._load_channels()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # Info label
        info_label = QLabel(
            "Configure acquisition channels for the current profile. "
            "Changes affect how channels appear in the live view and acquisition panels."
        )
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        # Table for acquisition channels
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            ["Enabled", "Name", "Illumination", "Camera", "Filter Wheel", "Filter", "Color"]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_NAME, QHeaderView.Stretch)
        header.setSectionResizeMode(self.COL_DISPLAY_COLOR, QHeaderView.Fixed)
        self.table.setColumnWidth(self.COL_DISPLAY_COLOR, 60)
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

        button_layout.addSpacing(20)

        self.btn_export = QPushButton("Export...")
        self.btn_export.setAutoDefault(False)
        self.btn_export.clicked.connect(self._export_config)
        button_layout.addWidget(self.btn_export)

        self.btn_import = QPushButton("Import...")
        self.btn_import.setAutoDefault(False)
        self.btn_import.clicked.connect(self._import_config)
        button_layout.addWidget(self.btn_import)

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

    def _set_buttons_enabled(self, enabled: bool):
        """Enable or disable action buttons based on config availability."""
        self.btn_add.setEnabled(enabled)
        self.btn_remove.setEnabled(enabled)
        self.btn_move_up.setEnabled(enabled)
        self.btn_move_down.setEnabled(enabled)
        self.btn_export.setEnabled(enabled)
        self.btn_save.setEnabled(enabled)
        # Import is always enabled since it can create a new config
        # Cancel is always enabled

    def _load_channels(self):
        """Load acquisition channels from observation presets into the table."""
        self.illumination_config = self.config_repo.get_illumination_config()

        preset_names = self.config_repo.list_observation_presets()
        self._preset_states = []
        for name in preset_names:
            state = self.config_repo.load_observation_preset(name)
            if state is not None:
                self._preset_states.append(state)
        self._original_names = [s.name for s in self._preset_states]

        if not self._preset_states:
            self._log.warning("No observation presets found for current profile")
            QMessageBox.warning(
                self,
                "No Configuration",
                "No channel configuration found for the current profile.\n"
                "Please ensure a profile is selected and has been initialized.",
            )
            self._set_buttons_enabled(False)
            return

        self._set_buttons_enabled(True)

        # Determine column visibility
        camera_names = self.config_repo.get_camera_names()
        wheel_names = self.config_repo.get_filter_wheel_names()
        has_any_wheel = wheel_names or _is_filter_wheel_enabled(self.config_repo)

        if len(camera_names) <= 1:
            self.table.setColumnHidden(self.COL_CAMERA, True)
        if len(wheel_names) <= 1:
            self.table.setColumnHidden(self.COL_FILTER_WHEEL, True)
        if not has_any_wheel:
            self.table.setColumnHidden(self.COL_FILTER_POSITION, True)

        self.table.setRowCount(len(self._preset_states))
        for row, state in enumerate(self._preset_states):
            self._populate_row(row, state)

    def _populate_row(self, row: int, state):
        """Populate a table row with observation state data."""
        # Enabled checkbox (ObservationState doesn't have 'enabled', always True)
        checkbox_widget = QWidget()
        checkbox_layout = QHBoxLayout(checkbox_widget)
        checkbox_layout.setContentsMargins(0, 0, 0, 0)
        checkbox_layout.setAlignment(Qt.AlignCenter)
        checkbox = QCheckBox()
        checkbox.setChecked(True)
        checkbox_layout.addWidget(checkbox)
        self.table.setCellWidget(row, self.COL_ENABLED, checkbox_widget)

        # Name (editable text)
        name_item = QTableWidgetItem(state.name)
        self.table.setItem(row, self.COL_NAME, name_item)

        # Illumination dropdown
        illum_combo = QComboBox()
        if self.illumination_config:
            illum_names = [ch.name for ch in self.illumination_config.channels]
            illum_combo.addItems(illum_names)
            # Set current illumination from first illuminator state
            if state.illuminator_states:
                current_illum = state.illuminator_states[0].illumination_channel
                if current_illum and current_illum in illum_names:
                    illum_combo.setCurrentText(current_illum)
        self.table.setCellWidget(row, self.COL_ILLUMINATION, illum_combo)

        # Camera dropdown
        camera_combo = QComboBox()
        camera_combo.addItem("(None)")
        camera_names = self.config_repo.get_camera_names()
        camera_combo.addItems(camera_names)
        self.table.setCellWidget(row, self.COL_CAMERA, camera_combo)

        # Filter wheel dropdown
        wheel_combo = QComboBox()
        wheel_combo.addItem("(None)")
        wheel_names = self.config_repo.get_filter_wheel_names()
        wheel_combo.addItems(wheel_names)
        wheel_combo.currentTextChanged.connect(lambda text, r=row: self._on_wheel_changed(r, text))
        self.table.setCellWidget(row, self.COL_FILTER_WHEEL, wheel_combo)

        # Filter position dropdown
        position_combo = QComboBox()
        # Get emission filter position from state
        filter_pos = state.emission_filter_positions.get("default")
        _populate_filter_positions_for_combo(
            position_combo, None, self.config_repo, filter_pos
        )
        self.table.setCellWidget(row, self.COL_FILTER_POSITION, position_combo)

        # Display color (color picker button - fills cell width)
        color = state.display_color if hasattr(state, "display_color") else "#FFFFFF"
        color_btn = QPushButton()
        color_btn.setStyleSheet(f"background-color: {color};")
        color_btn.setProperty("color", color)
        color_btn.clicked.connect(lambda _checked, r=row: self._pick_color(r))
        self.table.setCellWidget(row, self.COL_DISPLAY_COLOR, color_btn)

    def _on_wheel_changed(self, row: int, wheel_name: str):
        """Update filter position options when wheel selection changes."""
        position_combo = self.table.cellWidget(row, self.COL_FILTER_POSITION)
        if position_combo:
            _populate_filter_positions_for_combo(position_combo, wheel_name, self.config_repo)

    def _pick_color(self, row: int):
        """Open color picker for a row."""
        color_btn = self.table.cellWidget(row, self.COL_DISPLAY_COLOR)
        current_color = QColor(color_btn.property("color") if color_btn else "#FFFFFF")
        color = QColorDialog.getColor(current_color, self, "Select Display Color")
        if color.isValid():
            color_btn.setStyleSheet(f"background-color: {color.name()};")
            color_btn.setProperty("color", color.name())

    def _add_channel(self):
        """Add a new observation state."""
        dialog = AddObservationStateDialog(self.config_repo, self)
        if dialog.exec_() == QDialog.Accepted:
            state = dialog.get_channel()
            if state:
                self._preset_states.append(state)
                self.table.setRowCount(len(self._preset_states))
                self._populate_row(len(self._preset_states) - 1, state)

    def _remove_channel(self):
        """Remove selected observation state."""
        current_row = self.table.currentRow()
        if current_row < 0 or current_row >= len(self._preset_states):
            return

        name_item = self.table.item(current_row, self.COL_NAME)
        name = name_item.text() if name_item else self._preset_states[current_row].name
        reply = QMessageBox.question(
            self,
            "Confirm Removal",
            f"Remove channel '{name}'?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            del self._preset_states[current_row]
            self.table.setRowCount(len(self._preset_states))
            for row, state in enumerate(self._preset_states):
                self._populate_row(row, state)

    def _move_up(self):
        """Move selected observation state up."""
        current_row = self.table.currentRow()
        if current_row <= 0 or current_row >= len(self._preset_states):
            return

        self._preset_states[current_row - 1], self._preset_states[current_row] = (
            self._preset_states[current_row],
            self._preset_states[current_row - 1],
        )
        self._populate_row(current_row - 1, self._preset_states[current_row - 1])
        self._populate_row(current_row, self._preset_states[current_row])
        self.table.selectRow(current_row - 1)

    def _move_down(self):
        """Move selected observation state down."""
        current_row = self.table.currentRow()
        if current_row < 0 or current_row >= len(self._preset_states) - 1:
            return

        self._preset_states[current_row], self._preset_states[current_row + 1] = (
            self._preset_states[current_row + 1],
            self._preset_states[current_row],
        )
        self._populate_row(current_row, self._preset_states[current_row])
        self._populate_row(current_row + 1, self._preset_states[current_row + 1])
        self.table.selectRow(current_row + 1)

    def _save_changes(self):
        """Save changes to observation presets."""
        self._sync_table_to_config()

        try:
            current_names = {s.name for s in self._preset_states}
            # Delete presets that were removed
            for name in self._original_names:
                if name not in current_names:
                    from control.core.observation_state_service import observation_preset_path
                    path = observation_preset_path(self.config_repo, name)
                    if path.exists():
                        path.unlink()
            # Save each preset
            for state in self._preset_states:
                self.config_repo.save_observation_preset(state.name, state)
        except (PermissionError, OSError) as e:
            self._log.error(f"Failed to save channel configuration: {e}")
            QMessageBox.critical(self, "Save Failed", f"Cannot write configuration file:\n{e}")
            return
        except Exception as e:
            self._log.error(f"Unexpected error saving channel configuration: {e}")
            QMessageBox.critical(self, "Save Failed", f"Failed to save configuration:\n{e}")
            return

        self.signal_channels_updated.emit()
        self.accept()

    def _export_config(self):
        """Export current channel configuration to a YAML file."""
        import yaml

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Channel Configuration",
            "channel_config.yaml",
            "YAML Files (*.yaml *.yml);;All Files (*)",
        )
        if not file_path:
            return

        self._sync_table_to_config()

        if not self._preset_states:
            QMessageBox.warning(self, "Export Failed", "No configuration loaded to export.")
            return

        try:
            data = {"observation_states": [s.model_dump(mode="json", exclude_none=True) for s in self._preset_states]}
            with open(file_path, "w") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            QMessageBox.information(self, "Export Successful", f"Configuration exported to:\n{file_path}")
        except (PermissionError, OSError) as e:
            self._log.warning(f"Failed to write export file {file_path}: {e}")
            QMessageBox.critical(self, "Export Failed", f"Cannot write to file:\n{e}")
        except Exception as e:
            self._log.error(f"Unexpected error during export: {e}")
            QMessageBox.critical(self, "Export Failed", f"Unexpected error:\n{e}")

    def _import_config(self):
        """Import channel configuration from a YAML file."""
        from pydantic import ValidationError
        from control.models.observation_state import ObservationState
        import yaml

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Channel Configuration",
            "",
            "YAML Files (*.yaml *.yml);;All Files (*)",
        )
        if not file_path:
            return

        try:
            with open(file_path, "r") as f:
                data = yaml.safe_load(f)
            if data is None or not isinstance(data, dict):
                raise ValueError("File is empty or contains no valid YAML content")
            raw_states = data.get("observation_states")
            if not isinstance(raw_states, list):
                raise ValueError("Expected 'observation_states' list in import file")
            imported_states = [ObservationState.model_validate(d) for d in raw_states]
        except (PermissionError, FileNotFoundError) as e:
            self._log.warning(f"Cannot read import file {file_path}: {e}")
            QMessageBox.critical(self, "Import Failed", f"Cannot read file:\n{e}")
            return
        except yaml.YAMLError as e:
            self._log.warning(f"Invalid YAML in {file_path}: {e}")
            QMessageBox.critical(self, "Import Failed", f"File contains invalid YAML:\n{e}")
            return
        except (ValidationError, ValueError) as e:
            self._log.warning(f"Config validation failed for {file_path}: {e}")
            QMessageBox.critical(self, "Import Failed", f"Configuration format error:\n{e}")
            return

        self._preset_states = imported_states
        self.table.setRowCount(0)
        self._load_channels()

        QMessageBox.information(
            self, "Import Successful", f"Imported {len(imported_states)} channels from:\n{file_path}"
        )

    def _sync_table_to_config(self):
        """Sync table data back to self._preset_states without saving to disk."""
        num_rows = min(self.table.rowCount(), len(self._preset_states))
        for row in range(num_rows):
            state = self._preset_states[row]

            name_item = self.table.item(row, self.COL_NAME)
            if name_item:
                state.name = name_item.text().strip()

            illum_combo = self.table.cellWidget(row, self.COL_ILLUMINATION)
            if illum_combo and isinstance(illum_combo, QComboBox):
                illum_name = illum_combo.currentText()
                if state.illuminator_states:
                    state.illuminator_states[0].illumination_channel = illum_name
                else:
                    from control.models.observation_state import IlluminatorState
                    state.illuminator_states = [
                        IlluminatorState(illumination_channel=illum_name, intensity=20.0, on=False)
                    ]

            position_combo = self.table.cellWidget(row, self.COL_FILTER_POSITION)
            if position_combo and isinstance(position_combo, QComboBox):
                pos = position_combo.currentData()
                if pos is not None:
                    state.emission_filter_positions["default"] = pos

            color_btn = self.table.cellWidget(row, self.COL_DISPLAY_COLOR)
            if color_btn:
                state.display_color = color_btn.property("color") or "#FFFFFF"


class AddObservationStateDialog(QDialog):
    """Dialog for adding a new acquisition channel."""

    def __init__(self, config_repo, parent=None):
        super().__init__(parent)
        self.config_repo = config_repo
        self._display_color = "#FFFFFF"
        self.setWindowTitle("Add Acquisition Channel")
        self._setup_ui()

    def _setup_ui(self):
        layout = QFormLayout(self)

        # Name
        self.name_edit = QLineEdit()
        layout.addRow("Name:", self.name_edit)

        # Illumination source dropdown
        self.illumination_combo = QComboBox()
        illum_config = self.config_repo.get_illumination_config()
        if illum_config:
            self.illumination_combo.addItems([ch.name for ch in illum_config.channels])
        layout.addRow("Illumination:", self.illumination_combo)

        # Camera dropdown (hidden if single camera - 0 or 1 cameras)
        camera_names = self.config_repo.get_camera_names()
        if len(camera_names) > 1:
            self.camera_combo = QComboBox()
            self.camera_combo.addItem("(None)")
            self.camera_combo.addItems(camera_names)
            layout.addRow("Camera:", self.camera_combo)
        else:
            self.camera_combo = None

        # Filter wheel dropdown (hidden if single wheel - 0 or 1 wheels)
        wheel_names = self.config_repo.get_filter_wheel_names()
        has_any_wheel = wheel_names or _is_filter_wheel_enabled(self.config_repo)

        # Show wheel dropdown only for multi-wheel systems
        if len(wheel_names) > 1:
            self.wheel_combo = QComboBox()
            self.wheel_combo.addItem("(None)")
            self.wheel_combo.addItems(wheel_names)
            self.wheel_combo.currentTextChanged.connect(self._on_wheel_changed)
            layout.addRow("Filter Wheel:", self.wheel_combo)
        else:
            self.wheel_combo = None

        # Filter position dropdown (shown if any filter wheels exist)
        if has_any_wheel:
            self.position_combo = QComboBox()
            # Populate positions - function auto-resolves single-wheel systems
            _populate_filter_positions_for_combo(self.position_combo, None, self.config_repo)
            layout.addRow("Filter Position:", self.position_combo)
        else:
            self.position_combo = None

        # Display color
        self.color_btn = QPushButton()
        self.color_btn.setFixedSize(60, 25)
        self.color_btn.setStyleSheet(f"background-color: {self._display_color}; border: 1px solid #888;")
        self.color_btn.clicked.connect(self._pick_color)
        layout.addRow("Display Color:", self.color_btn)

        # Buttons
        button_layout = QHBoxLayout()
        self.btn_ok = QPushButton("Add")
        self.btn_ok.clicked.connect(self._validate_and_accept)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)
        button_layout.addWidget(self.btn_ok)
        button_layout.addWidget(self.btn_cancel)
        layout.addRow(button_layout)

    def _on_wheel_changed(self, wheel_name: str):
        """Update filter position options when wheel selection changes."""
        if self.position_combo is not None:
            _populate_filter_positions_for_combo(self.position_combo, wheel_name, self.config_repo)

    def _pick_color(self):
        """Open color picker."""
        color = QColorDialog.getColor(QColor(self._display_color), self, "Select Display Color")
        if color.isValid():
            self._display_color = color.name()
            self.color_btn.setStyleSheet(f"background-color: {self._display_color}; border: 1px solid #888;")

    def _validate_and_accept(self):
        """Validate input before accepting."""
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Validation Error", "Channel name cannot be empty.")
            return

        # Check for duplicate names
        existing_names = self.config_repo.list_observation_presets()
        if name in existing_names:
            QMessageBox.warning(self, "Validation Error", f"Channel '{name}' already exists.")
            return

        self.accept()

    def get_channel(self):
        """Build ObservationState from dialog inputs."""
        from control.models.observation_state import (
            ObservationState,
            CameraSettings,
            IlluminatorState,
        )

        name = self.name_edit.text().strip()
        illum_name = self.illumination_combo.currentText()

        # Filter position -> emission_filter_positions
        emission_filter_positions = {}
        filter_position = self.position_combo.currentData() if self.position_combo else None
        if filter_position is not None:
            emission_filter_positions["default"] = filter_position

        return ObservationState(
            version=3,
            name=name,
            display_color=self._display_color,
            camera_settings=CameraSettings(
                exposure_time_ms=20.0,
                gain_mode=10.0,
            ),
            illuminator_states=[
                IlluminatorState(
                    illumination_channel=illum_name,
                    intensity=20.0,
                    on=False,
                ),
            ],
            z_offset_um=0.0,
            emission_filter_positions=emission_filter_positions,
        )


class FilterWheelConfiguratorDialog(QDialog):
    """Dialog for configuring filter wheel position names.

    Edits machine_configs/filter_wheels.yaml to define filter wheels
    and their position-to-name mappings.
    """

    signal_config_updated = Signal()

    def __init__(self, config_repo, parent=None):
        super().__init__(parent)
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self.config_repo = config_repo
        self.registry = None
        self.setWindowTitle("Filter Wheel Configuration")
        self.setMinimumSize(500, 400)
        self._setup_ui()
        self._load_config()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # Instructions
        instructions = QLabel(
            "Configure filter wheel position names. Each position can have a descriptive name\n"
            "(e.g., 'DAPI emission', 'GFP emission') that will appear in channel configuration."
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)

        # Wheel selector (hidden for single-wheel systems)
        self.wheel_layout = QHBoxLayout()
        self.wheel_label = QLabel("Filter Wheel:")
        self.wheel_layout.addWidget(self.wheel_label)
        self.wheel_combo = QComboBox()
        self.wheel_combo.currentIndexChanged.connect(self._on_wheel_selected)
        self.wheel_layout.addWidget(self.wheel_combo, 1)
        layout.addLayout(self.wheel_layout)

        # Positions table
        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Position", "Filter Name"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table)

        # Save/Cancel buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        self.btn_save = QPushButton("Save")
        self.btn_save.clicked.connect(self._save_config)
        button_layout.addWidget(self.btn_save)

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)
        button_layout.addWidget(self.btn_cancel)

        layout.addLayout(button_layout)

    def _load_config(self):
        """Load filter wheel registry from config."""
        from control.models.filter_wheel_config import FilterWheelRegistryConfig, FilterWheelDefinition, FilterWheelType

        self.registry = self.config_repo.get_filter_wheel_registry()

        # Check if filter wheel is enabled in machine_config.yaml
        filter_wheel_enabled = _is_filter_wheel_enabled(self.config_repo)
        fw_cfg = squid.config.get_filter_wheel_config()
        if fw_cfg and fw_cfg.indices:
            configured_indices = list(fw_cfg.indices)
        else:
            dev = self.config_repo.get_machine_config().get_device("emission_filter_wheel")
            raw = (dev.config or {}).get("indices", [1]) if dev else [1]
            configured_indices = list(raw)

        # If no registry exists but filter wheel is enabled, create one with wheels for all configured indices
        if self.registry is None:
            if filter_wheel_enabled:
                default_positions = {i: f"Position {i}" for i in range(1, 9)}
                wheels = []
                for wheel_id in configured_indices:
                    if len(configured_indices) == 1:
                        # Single wheel: no name/id needed
                        wheels.append(
                            FilterWheelDefinition(type=FilterWheelType.EMISSION, positions=default_positions.copy())
                        )
                    else:
                        # Multi-wheel: use id and name to distinguish
                        wheels.append(
                            FilterWheelDefinition(
                                id=wheel_id,
                                name=f"Wheel {wheel_id}",
                                type=FilterWheelType.EMISSION,
                                positions=default_positions.copy(),
                            )
                        )
                self.registry = FilterWheelRegistryConfig(filter_wheels=wheels)
            else:
                self.registry = FilterWheelRegistryConfig(filter_wheels=[])

        # Ensure registry has entries for all wheels configured in .ini
        # This handles the case where user updated .ini but didn't update filter_wheels.yaml
        if filter_wheel_enabled and len(configured_indices) > 1:
            existing_ids = {w.id for w in self.registry.filter_wheels if w.id is not None}
            default_positions = {i: f"Position {i}" for i in range(1, 9)}
            for wheel_id in configured_indices:
                if wheel_id not in existing_ids:
                    self._log.info(
                        f"Auto-creating filter wheel entry for wheel {wheel_id} (configured in .ini but missing in filter_wheels.yaml)"
                    )
                    self.registry.filter_wheels.append(
                        FilterWheelDefinition(
                            id=wheel_id,
                            name=f"Wheel {wheel_id}",
                            type=FilterWheelType.EMISSION,
                            positions=default_positions.copy(),
                        )
                    )

        # For single wheel systems: remove name if present (migrate from old "Emission" name)
        is_single_wheel = len(self.registry.filter_wheels) == 1
        if is_single_wheel:
            wheel = self.registry.filter_wheels[0]
            if wheel.name is not None or wheel.id is not None:
                self.registry.filter_wheels[0] = FilterWheelDefinition(type=wheel.type, positions=wheel.positions)

        # Hide wheel selector for single-wheel systems
        self.wheel_label.setVisible(not is_single_wheel)
        self.wheel_combo.setVisible(not is_single_wheel)

        # Populate wheel combo (for multi-wheel systems)
        self.wheel_combo.clear()
        for wheel in self.registry.filter_wheels:
            display_name = wheel.name or "(Unnamed)"
            self.wheel_combo.addItem(display_name, wheel)

        # Select first wheel and load its positions
        if self.wheel_combo.count() > 0:
            self.wheel_combo.setCurrentIndex(0)
            self._on_wheel_selected(0)
        else:
            self.table.setRowCount(0)

    def _on_wheel_selected(self, index):
        """Load positions for selected wheel into table."""
        self.table.setRowCount(0)

        if index < 0:
            return

        wheel = self.wheel_combo.itemData(index)
        if wheel is None:
            return

        # Populate table with positions
        for pos in sorted(wheel.positions.keys()):
            row = self.table.rowCount()
            self.table.insertRow(row)

            # Position number (read-only)
            pos_item = QTableWidgetItem(str(pos))
            pos_item.setFlags(pos_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 0, pos_item)

            # Filter name (editable)
            name_item = QTableWidgetItem(wheel.positions[pos])
            self.table.setItem(row, 1, name_item)

    def _save_config(self):
        """Save filter wheel configuration to YAML file."""
        import yaml

        # Sync table data back to current wheel
        index = self.wheel_combo.currentIndex()
        if index >= 0:
            wheel = self.wheel_combo.itemData(index)
            if wheel:
                wheel.positions.clear()
                for row in range(self.table.rowCount()):
                    pos_item = self.table.item(row, 0)
                    name_item = self.table.item(row, 1)
                    if pos_item and name_item:
                        pos = int(pos_item.text())
                        name = name_item.text().strip() or f"Position {pos}"
                        wheel.positions[pos] = name

        # Save to file using repository (ensures consistent serialization)
        try:
            self.config_repo.save_filter_wheel_registry(self.registry)
            self.signal_config_updated.emit()
            QMessageBox.information(self, "Saved", "Filter wheel configuration saved.")
            self.accept()
        except (PermissionError, OSError) as e:
            self._log.error(f"Failed to save filter wheel config: {e}")
            QMessageBox.critical(self, "Error", f"Cannot write configuration file:\n{e}")
        except yaml.YAMLError as e:
            self._log.error(f"Failed to serialize filter wheel config: {e}")
            QMessageBox.critical(self, "Error", f"Configuration data could not be serialized:\n{e}")
        except Exception as e:
            self._log.exception(f"Unexpected error saving filter wheel config: {e}")
            QMessageBox.critical(self, "Error", f"Failed to save configuration:\n{e}")


class _QtLogSignalHolder(QObject):
    """QObject that holds the signal for QtLoggingHandler.

    Defined at module level to avoid dynamic class creation.
    """

    message_logged = Signal(int, str, str)  # level, logger_name, message


class QtLoggingHandler(logging.Handler):
    """Logging handler that emits Qt signals for WARNING+ messages.

    Thread-safe: Qt signal system handles cross-thread delivery automatically.
    Used by WarningErrorWidget to display warnings/errors in the status bar.
    """

    def __init__(self, min_level: int = logging.WARNING):
        super().__init__()
        self.setLevel(min_level)
        self._signal_holder = _QtLogSignalHolder()
        self.setFormatter(logging.Formatter(fmt=squid.logging.LOG_FORMAT, datefmt=squid.logging.LOG_DATEFORMAT))
        # Intentionally reuse the private thread_id filter from squid.logging for consistent
        # formatting across all log handlers. This creates a controlled dependency on
        # squid.logging's internal API.
        self.addFilter(squid.logging._thread_id_filter)

    @property
    def signal_message_logged(self):
        return self._signal_holder.message_logged

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            self._signal_holder.message_logged.emit(record.levelno, record.name, msg)
        except Exception:
            self.handleError(record)


class WarningErrorWidget(QWidget):
    """Status bar widget displaying logged warnings and errors.

    Features:
    - Color-coded: yellow for warnings, red for errors
    - Shows timestamp for each message
    - Expandable popup showing all messages when multiple exist
    - Deduplication: repeated identical messages show count instead of duplicates
    - Rate limiting: max 10 messages per second to prevent GUI freeze
    """

    MAX_MESSAGES = 100  # Prevent unbounded memory growth
    RATE_LIMIT_WINDOW_MS = 1000  # 1 second window
    RATE_LIMIT_MAX_MESSAGES = 10  # Max messages per window
    POLL_INTERVAL_MS = 100  # How often to poll handler for new messages

    def __init__(self, parent=None):
        super().__init__(parent)
        # List of dicts with keys: id, level, logger_name, message, count, datetime
        self._messages = []
        self._next_message_id = 0
        self._rate_limit_timestamps = []  # For rate limiting
        self._dropped_count = 0  # Track rate-limited messages
        self._popup = None
        self._handler = None
        self._poll_timer = None
        self._setup_ui()

    def connect_handler(self, handler: "BufferingHandler"):
        """Connect to a logging handler and start polling for messages.

        Must be called from the GUI thread (creates QTimer which is not thread-safe).

        Args:
            handler: The BufferingHandler to poll for messages.
        """
        # Disconnect any existing handler first to avoid orphaned timers
        self.disconnect_handler()

        self._handler = handler
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_messages)
        self._poll_timer.start(self.POLL_INTERVAL_MS)

    def disconnect_handler(self):
        """Disconnect from the logging handler and stop polling.

        Must be called from the GUI thread (QTimer operations are not thread-safe).
        """
        if self._poll_timer is not None:
            self._poll_timer.stop()
            self._poll_timer.deleteLater()
            self._poll_timer = None
        self._handler = None

    def _poll_messages(self):
        """Poll the handler for new messages."""
        if self._handler is None:
            return
        try:
            for level, logger_name, message in self._handler.get_pending():
                self.add_message(level, logger_name, message)
        except Exception as e:
            # Log but don't crash the timer - allow recovery on next poll.
            # Qt silently swallows exceptions in timer callbacks, so we must log explicitly.
            squid.logging.get_logger(__name__).error(f"Error polling warning/error messages: {e}", exc_info=True)

    def closeEvent(self, event):
        """Clean up popup and timer when widget is closed."""
        self.disconnect_handler()
        self._cleanup_popup()
        super().closeEvent(event)

    def _cleanup_popup(self):
        """Safely clean up popup if it exists."""
        if self._popup is not None:
            try:
                self._popup.hide()
                self._popup.deleteLater()
            except RuntimeError:
                # Popup may already be deleted
                pass
            self._popup = None

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(6)

        # Level icon (warning/error indicator) - circular badge
        self.label_icon = QLabel()
        self.label_icon.setFixedSize(20, 20)
        self.label_icon.setAlignment(Qt.AlignCenter)

        # Message text
        self.label_text = QLabel()
        self.label_text.setTextInteractionFlags(Qt.TextSelectableByMouse)

        # Expand button (shows when multiple messages or dropped messages)
        self.btn_expand = QPushButton()
        self.btn_expand.setFixedHeight(18)
        self.btn_expand.setMinimumWidth(32)  # Allow width to grow for longer text
        self.btn_expand.setCursor(Qt.PointingHandCursor)
        self.btn_expand.setStyleSheet(
            "QPushButton { background-color: #666; color: white; border-radius: 9px; "
            "font-size: 11px; font-weight: bold; padding: 0px 6px; }"
            "QPushButton:hover { background-color: #444; }"
            "QPushButton:pressed { background-color: #222; }"
        )
        self.btn_expand.clicked.connect(self._on_expand_clicked)
        self.btn_expand.setVisible(False)

        # Dismiss button (X)
        self.btn_dismiss = QPushButton("✕")
        self.btn_dismiss.setFixedSize(18, 18)
        self.btn_dismiss.setCursor(Qt.PointingHandCursor)
        self.btn_dismiss.setStyleSheet(
            "QPushButton { background: transparent; border: none; color: #888; font-size: 14px; padding: 0px; }"
            "QPushButton:hover { color: #000; }"
        )
        self.btn_dismiss.clicked.connect(self.dismiss_current)

        layout.addWidget(self.label_icon)
        layout.addWidget(self.label_text)
        layout.addWidget(self.btn_expand)
        layout.addWidget(self.btn_dismiss)
        layout.addStretch()  # Push everything to the left

    def _on_expand_clicked(self):
        """Handle expand button click."""
        self._toggle_popup()

    def _toggle_popup(self):
        """Toggle the popup showing all messages."""
        if self._popup is not None and self._popup.isVisible():
            self._cleanup_popup()
            return
        self._show_popup()

    def _show_popup(self):
        """Show popup with scrollable list of all messages."""
        # Recreate popup each time to ensure fresh state
        self._cleanup_popup()

        self._popup = QFrame(self.window(), Qt.Popup | Qt.FramelessWindowHint)
        self._popup.setStyleSheet("QFrame { background-color: white; border: 1px solid #aaa; border-radius: 6px; }")

        popup_layout = QVBoxLayout(self._popup)
        popup_layout.setContentsMargins(0, 0, 0, 0)
        popup_layout.setSpacing(0)

        # Header with title and Clear All button
        header = QWidget()
        header.setStyleSheet("background-color: #f5f5f5; border-bottom: 1px solid #ddd;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 8, 12, 8)
        header_label = QLabel(f"<b>Warnings & Errors</b> ({len(self._messages)})")
        btn_clear = QPushButton("Clear All")
        btn_clear.setCursor(Qt.PointingHandCursor)
        btn_clear.setStyleSheet(
            "QPushButton { background-color: #e74c3c; color: white; border: none; "
            "border-radius: 4px; padding: 4px 12px; font-weight: bold; }"
            "QPushButton:hover { background-color: #c0392b; }"
        )
        btn_clear.clicked.connect(self._clear_all_from_popup)
        header_layout.addWidget(header_label)
        header_layout.addStretch()
        header_layout.addWidget(btn_clear)
        popup_layout.addWidget(header)

        # Scrollable list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet("QScrollArea { border: none; background: white; }")

        list_widget = QWidget()
        list_widget.setStyleSheet("background: white;")
        list_layout = QVBoxLayout(list_widget)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(0)

        # Add messages (newest first) - use message ID for dismiss callback
        for msg in reversed(self._messages):
            item_widget = self._create_popup_item(msg)
            list_layout.addWidget(item_widget)

        list_layout.addStretch()
        scroll.setWidget(list_widget)
        popup_layout.addWidget(scroll)

        # Size and position
        self._popup.setFixedWidth(550)
        self._popup.setMinimumHeight(100)
        self._popup.setMaximumHeight(350)

        # Position above this widget (popup appears above status bar)
        # with bounds checking to stay on screen
        global_pos = self.mapToGlobal(QPoint(0, 0))
        popup_height = min(350, 50 + len(self._messages) * 60)
        self._popup.setFixedHeight(popup_height)

        # Calculate position, ensuring popup stays on screen
        popup_x = global_pos.x()
        popup_y = global_pos.y() - popup_height - 5

        # Get available screen geometry
        from qtpy.QtWidgets import QApplication

        screen = QApplication.screenAt(global_pos)
        if screen is not None:
            screen_geo = screen.availableGeometry()
            # Ensure popup doesn't go above screen top
            if popup_y < screen_geo.top():
                # Show below the widget instead
                popup_y = global_pos.y() + self.height() + 5
            # Ensure popup doesn't go off right edge (and not past left edge on narrow screens)
            if popup_x + 550 > screen_geo.right():
                popup_x = max(screen_geo.left(), screen_geo.right() - 550)

        self._popup.move(popup_x, popup_y)
        self._popup.show()

    def _create_popup_item(self, msg: dict) -> QWidget:
        """Create a single item widget for the popup list."""
        level = msg["level"]
        message = msg["message"]
        logger_name = msg.get("logger_name", "")
        count = msg["count"]
        dt = msg["datetime"]
        msg_id = msg["id"]

        item = QWidget()
        is_error = level >= logging.ERROR
        bg_color = "#fef2f2" if is_error else "#fefce8"
        item.setStyleSheet(f"background-color: {bg_color}; border-bottom: 1px solid #eee;")

        layout = QHBoxLayout(item)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)

        # Level indicator
        icon_label = QLabel("⬤")
        icon_color = "#dc2626" if is_error else "#ca8a04"
        icon_label.setStyleSheet(f"color: {icon_color}; font-size: 8px;")
        icon_label.setFixedWidth(14)
        icon_label.setAlignment(Qt.AlignCenter)

        # Date/Time - show full date and time
        time_str = dt.strftime("%m/%d %H:%M:%S")
        time_label = QLabel(time_str)
        time_label.setStyleSheet("color: #666; font-size: 11px; font-family: monospace;")
        time_label.setFixedWidth(90)

        # Message (allow wrapping), prefixed with the logger/class name and source location
        core_msg = html.escape(self._extract_core_message(message))
        short_name = self._format_logger_name(logger_name)
        location = self._extract_file_location(message)
        header_parts = []
        if short_name:
            # html.escape so logger names with special chars (e.g. "<locals>") render safely
            header_parts.append(f"<b style='color: #555;'>{html.escape(short_name)}</b>")
        if location:
            header_parts.append(f"<span style='color: #888; font-size: 11px;'>{html.escape(location)}</span>")
        if header_parts:
            core_msg = f"{' '.join(header_parts)}: {core_msg}"
        if count > 1:
            core_msg = f"{core_msg} <b style='color: #666;'>(×{count})</b>"
        msg_label = QLabel(core_msg)
        msg_label.setWordWrap(True)
        msg_label.setStyleSheet("font-size: 12px; color: #333;")
        msg_label.setTextFormat(Qt.RichText)
        msg_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        # Dismiss button - use message ID for stable reference
        btn_dismiss = QPushButton("✕")
        btn_dismiss.setFixedSize(20, 20)
        btn_dismiss.setCursor(Qt.PointingHandCursor)
        btn_dismiss.setStyleSheet(
            "QPushButton { background: #ddd; border: none; color: #666; border-radius: 10px; font-size: 12px; }"
            "QPushButton:hover { background: #ccc; color: #333; }"
        )
        btn_dismiss.clicked.connect(lambda checked, mid=msg_id: self._dismiss_by_id(mid))

        layout.addWidget(icon_label, 0, Qt.AlignTop)
        layout.addWidget(time_label, 0, Qt.AlignTop)
        layout.addWidget(msg_label, 1)
        layout.addWidget(btn_dismiss, 0, Qt.AlignTop)

        return item

    def _dismiss_by_id(self, msg_id: int):
        """Dismiss a message by its unique ID."""
        for i, msg in enumerate(self._messages):
            if msg["id"] == msg_id:
                self._messages.pop(i)
                self._update_display()
                if self._popup is not None:
                    if self._messages:
                        # Refresh popup with updated list
                        self._cleanup_popup()
                        self._show_popup()
                    else:
                        self._cleanup_popup()
                return

    def _clear_all_from_popup(self):
        """Clear all messages and close popup."""
        self.clear_all()
        self._cleanup_popup()

    def add_message(self, level: int, logger_name: str, message: str):
        """Add a new warning/error message to the queue."""
        # Rate limiting - but never rate-limit ERROR or higher (they're too important to drop)
        now_ms = time.time() * 1000
        cutoff = now_ms - self.RATE_LIMIT_WINDOW_MS
        self._rate_limit_timestamps = [t for t in self._rate_limit_timestamps if t > cutoff]

        if level < logging.ERROR and len(self._rate_limit_timestamps) >= self.RATE_LIMIT_MAX_MESSAGES:
            self._dropped_count += 1
            self._update_display()  # Update to show dropped count
            return  # Rate limited

        # Extract datetime from message or use current time
        dt = self._extract_datetime(message)

        # Deduplication - check if identical message already exists
        # Note: duplicates don't consume rate limit slots since they don't create new entries
        core_msg = self._extract_core_message(message)
        for i, msg in enumerate(self._messages):
            if self._extract_core_message(msg["message"]) == core_msg and msg["level"] == level:
                # Update with new datetime and increment count
                msg["datetime"] = dt
                msg["count"] += 1
                msg["message"] = message  # Update to latest message text
                self._messages.append(self._messages.pop(i))  # Move to end
                self._update_display()
                return

        # New message - consume rate limit slot and assign unique ID
        self._rate_limit_timestamps.append(now_ms)
        if len(self._messages) >= self.MAX_MESSAGES:
            self._messages.pop(0)

        new_msg = {
            "id": self._next_message_id,
            "level": level,
            "logger_name": logger_name,
            "message": message,
            "count": 1,
            "datetime": dt,
        }
        self._next_message_id += 1
        self._messages.append(new_msg)
        self._update_display()

    def dismiss_current(self):
        """Dismiss the most recent message."""
        if self._messages:
            self._messages.pop()
            self._update_display()

    def clear_all(self):
        """Clear all messages and reset dropped count."""
        self._messages.clear()
        self._dropped_count = 0
        self._update_display()

    def get_dropped_count(self) -> int:
        """Return the number of messages dropped due to rate limiting."""
        return self._dropped_count

    def has_messages(self) -> bool:
        """Return True if there are pending messages."""
        return len(self._messages) > 0

    def _update_display(self):
        """Update the main widget display."""
        if not self._messages:
            self.setVisible(False)
            return

        self.setVisible(True)
        msg = self._messages[-1]
        level = msg["level"]
        message = msg["message"]
        logger_name = msg.get("logger_name", "")
        count = msg["count"]
        dt = msg["datetime"]
        is_error = level >= logging.ERROR

        # Colors
        if is_error:
            bg_color = "#fef2f2"
            text_color = "#b91c1c"
            icon_text = "✕"
            icon_style = (
                "background-color: #dc2626; color: white; font-weight: bold; font-size: 12px; border-radius: 10px;"
            )
        else:
            bg_color = "#fefce8"
            text_color = "#a16207"
            icon_text = "!"
            icon_style = (
                "background-color: #eab308; color: white; font-weight: bold; font-size: 14px; border-radius: 10px;"
            )

        self.setStyleSheet(f"background-color: {bg_color}; border-radius: 4px;")
        self.label_icon.setText(icon_text)
        self.label_icon.setStyleSheet(icon_style)

        # Format message with compact time (HH:MM only)
        time_str = dt.strftime("%H:%M")
        display_msg = self._format_display_message(logger_name, message)
        if count > 1:
            display_msg = f"[{time_str}] {display_msg} (×{count})"
        else:
            display_msg = f"[{time_str}] {display_msg}"
        self.label_text.setText(display_msg)
        self.label_text.setStyleSheet(f"color: {text_color}; font-weight: bold;")

        # Tooltip shows full message with date, logger name, source location, and dropped count if any
        full_time = dt.strftime("%Y-%m-%d %H:%M:%S")
        short_name = self._format_logger_name(logger_name)
        location = self._extract_file_location(message)
        core = self._extract_core_message(message)
        header = " ".join(p for p in (short_name, location) if p)
        tooltip_body = f"{header}: {core}" if header else core
        tooltip = f"{full_time}\n{tooltip_body}"
        if self._dropped_count > 0:
            tooltip += f"\n\n⚠ {self._dropped_count} message(s) dropped due to rate limiting"
        self.setToolTip(tooltip)

        # Show expand button if multiple messages or dropped messages
        msg_count = len(self._messages)
        if msg_count > 1 or self._dropped_count > 0:
            if self._dropped_count > 0:
                # Show both additional messages and dropped count
                extra = msg_count - 1
                if extra > 0:
                    self.btn_expand.setText(f"+{extra} ({self._dropped_count}⚠)")
                else:
                    self.btn_expand.setText(f"({self._dropped_count}⚠)")
            else:
                self.btn_expand.setText(f"+{msg_count - 1}")
            self.btn_expand.setVisible(True)
        else:
            self.btn_expand.setVisible(False)

    def _extract_datetime(self, message: str) -> datetime:
        """Extract datetime from log message."""
        # Format: "2026-01-22 23:44:23.123 - ..."
        try:
            if " - " in message:
                datetime_part = message.split(" - ")[0]
                # Parse "2026-01-22 23:44:23.123"
                if "." in datetime_part:
                    datetime_part = datetime_part.rsplit(".", 1)[0]
                return datetime.strptime(datetime_part, "%Y-%m-%d %H:%M:%S")
        except (ValueError, IndexError):
            # Timestamp is optional - fall back to current time if parsing fails
            pass
        return datetime.now()

    # Pattern to match file location suffix like " (gui.widgets:123)"
    _FILE_LOCATION_PATTERN = re.compile(r" \([^)]+:\d+\)$")

    def _extract_core_message(self, message: str) -> str:
        """Extract core message content (without timestamp/thread/location)."""
        for marker in [" - WARNING - ", " - ERROR - ", " - CRITICAL - "]:
            if marker in message:
                parts = message.split(marker, 1)
                if len(parts) > 1:
                    msg = parts[1]
                    # Strip file location suffix like " (module:123)" but not arbitrary parentheses
                    msg = self._FILE_LOCATION_PATTERN.sub("", msg)
                    return msg
        return message

    def _extract_file_location(self, message: str) -> str:
        """Return the trailing "file.py:123" location from a formatted log line, or ""."""
        match = self._FILE_LOCATION_PATTERN.search(message)
        if not match:
            return ""
        # match.group(0) is " (file.py:123)"; strip the surrounding " (" and ")"
        return match.group(0).strip()[1:-1]

    def _format_logger_name(self, logger_name: str) -> str:
        """Format a logger name for display, dropping the redundant 'squid' root prefix."""
        if not logger_name:
            return ""
        if logger_name == "squid":
            return ""
        if logger_name.startswith("squid."):
            return logger_name[len("squid.") :]
        return logger_name

    def _format_display_message(self, logger_name: str, message: str) -> str:
        """Format message for single-line display, prefixed with the logger/class name and source location."""
        msg = self._extract_core_message(message)
        short_name = self._format_logger_name(logger_name)
        location = self._extract_file_location(message)
        prefix_parts = [p for p in (short_name, location) if p]
        if prefix_parts:
            msg = f"{' '.join(prefix_parts)}: {msg}"
        if len(msg) > 80:
            msg = msg[:77] + "..."
        return msg


