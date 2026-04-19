from ._bootstrap import *
from .common import error_dialog
from control.nidaq import (
    AbstractNIDAQ,
    WaveformData,
    AcquisitionResult,
    TriggerSource,
    TriggerEdge,
    create_ni_daq,
    generate_sine_wave,
    generate_square_wave,
    generate_ramp_wave,
    generate_pulse_train,
)
from control.models.io_endpoint_config import IOControllerType, IOSignalType, IODirection

class NIDAQWidget(QWidget):
    """
    Widget for controlling National Instruments DAQ devices.
    
    Provides a GUI for:
    - Configuring sample rate and number of samples
    - Setting up analog output waveforms
    - Setting up digital output patterns
    - Configuring analog input channels
    - Arming, triggering, and viewing acquired data
    """
    
    signal_acquisition_started = Signal()
    signal_acquisition_finished = Signal()
    # Emitted when DAQ-only acquisition completes (status, error_message); use so main thread updates UI
    signal_daq_only_completed = Signal(object, object)
    
    def __init__(self, ni_daq: AbstractNIDAQ, is_simulation: bool = False, parent=None):
        super().__init__(parent)
        self._log = squid.logging.get_logger(self.__class__.__name__)
        
        # Import NI DAQ module

        self._ni_daq_module = __import__('control.nidaq', fromlist=[''])

        # Endpoint label caches for plot legends (populated in _update_device_info)
        self._endpoint_labels_ao: dict[str, str] = {}
        self._endpoint_labels_ai: dict[str, str] = {}
        self._endpoint_labels_do: dict[tuple[str, int], str] = {}
        
        self.is_simulation = is_simulation

        # Cache IO endpoint config so we can show human-readable labels for
        # NIDAQ-controlled endpoints when available in the machine config.
        # Use MachineConfig.collect_io_endpoints() (which walks device io: blocks
        # with display_name) rather than io_endpoints.yaml (which lacks them).
        try:
            repo = ConfigRepository()
            mc = repo.get_machine_config()
            self._io_endpoint_config = mc.collect_io_endpoints()
        except Exception as e:
            self._log.warning(f"Could not load IO endpoint config for NIDAQWidget: {e}", exc_info=True)
            self._io_endpoint_config = None

        if self.is_simulation:
            # Create a simulated NI DAQ so the widget always talks to an AbstractNIDAQ.
            sim_config = {
                "device_name": "Simulation",
                "sample_rate_hz": 10000.0,
                "samples_per_channel": 10000,
                "ao_channels": [],
                "do_port": "port0",
                "do_lines": [],
                "di_port": "port0",
                "di_lines": [],
                "ai_channels": [],
                "ai_min_voltage": -10.0,
                "ai_max_voltage": 10.0,
                "ai_terminal_config": "RSE",
                "trigger_source": TriggerSource.SOFTWARE,
                "external_trigger_terminal": "/Simulation/PFI0",
                "trigger_edge": TriggerEdge.RISING,
                "continuous": False,
                "do_logic_family": "FIVE_V",
            }
            self._ni_daq = create_ni_daq(sim_config, simulation=True)
        else:
            self._ni_daq = ni_daq
        self._log.info(f"NIDAQWidget initialized with NI DAQ: {self._ni_daq}")
        self._waveforms = WaveformData()
        
        # Waveform data storage
        self._ao_waveforms: dict = {}  # channel -> np.ndarray
        self._do_patterns: dict = {}   # line -> np.ndarray
        
        # DAQ-only completion: signal ensures slot runs on main thread when callback is from worker thread
        self.signal_daq_only_completed.connect(self._on_daq_only_acquisition_completed)
        
        # Initialize UI
        self.init_ui()

    def _get_dio_line_from_config(self, endpoint_name: str, default: int) -> int:
        """Return the NI DAQ DIO line index declared for ``endpoint_name`` in the
        machine config (e.g. ``main_camera.trigger``), falling back to ``default``
        when the endpoint is missing or its channel_id is not parseable."""
        if self._io_endpoint_config is None:
            return default
        ep = self._io_endpoint_config.get(endpoint_name)
        if ep is None:
            return default
        cid = ep.channel_id or ""
        if "line" not in cid:
            return default
        try:
            return int(cid.rsplit("line", 1)[-1])
        except ValueError:
            return default

    def init_ui(self):
        """Initialize the user interface."""
        main_layout = QHBoxLayout()
        self.setLayout(main_layout)
        
        # Left panel - Configuration
        left_panel = QVBoxLayout()
        
        # Device Selection Group
        device_group = QGroupBox("Device Configuration")
        device_layout = QVBoxLayout()
        
        # Device dropdown
        device_row = QHBoxLayout()
        device_row.addWidget(QLabel("Device:"))
        self.device_combo = QComboBox()
        if self.is_simulation:
            self.device_combo.addItems(["Simulation"])
        else:
            self.device_combo.addItems(self._ni_daq.get_available_devices()) # Need to update configuration formats to take into account multiple devices
        self.device_combo.currentTextChanged.connect(self.on_device_changed)
        device_row.addWidget(self.device_combo)
        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setFixedWidth(30) ## TBD: update to use the NIDAQ device list
        self.refresh_btn.clicked.connect(self.on_refresh)
        device_row.addWidget(self.refresh_btn)
        device_layout.addLayout(device_row)
        
        # Sample rate
        rate_row = QHBoxLayout()
        rate_row.addWidget(QLabel("Sample Rate (Hz):"))
        self.sample_rate_spin = QDoubleSpinBox()
        self.sample_rate_spin.setRange(1, 1000000)
        self.sample_rate_spin.setValue(10000)
        self.sample_rate_spin.setDecimals(0)
        rate_row.addWidget(self.sample_rate_spin)
        device_layout.addLayout(rate_row)
        
        # Number of samples
        samples_row = QHBoxLayout()
        samples_row.addWidget(QLabel("Samples per Channel:"))
        self.num_samples_spin = QSpinBox()
        self.num_samples_spin.setRange(2, 10000000)
        self.num_samples_spin.setValue(10000)
        samples_row.addWidget(self.num_samples_spin)
        device_layout.addLayout(samples_row)
        
        # Duration display
        duration_row = QHBoxLayout()
        duration_row.addWidget(QLabel("Duration (s):"))
        self.duration_label = QLabel("1.000")
        duration_row.addWidget(self.duration_label)
        device_layout.addLayout(duration_row)
        
        # Continuous mode checkbox
        self.continuous_checkbox = QCheckBox("Continuous Mode")
        self.continuous_checkbox.stateChanged.connect(self.on_config_changed)
        device_layout.addWidget(self.continuous_checkbox)
        
        # Link to Fast Acquisition: when enabled, changing total time or DAQ sample rate
        # in Fast Acquisition widget will update this widget's waveforms.
        self.link_to_fast_acquisition_checkbox = QCheckBox("Link to Fast Acquisition")
        self.link_to_fast_acquisition_checkbox.setChecked(True)
        self.link_to_fast_acquisition_checkbox.setToolTip(
            "When enabled, changing total acquisition time or DAQ sample rate in Fast Acquisition "
            "will update this widget's waveforms and sample rate."
        )
        device_layout.addWidget(self.link_to_fast_acquisition_checkbox)
        
        device_group.setLayout(device_layout)
        left_panel.addWidget(device_group)
        
        # Trigger Configuration Group
        trigger_group = QGroupBox("Trigger Configuration")
        trigger_layout = QVBoxLayout()
        
        trigger_source_row = QHBoxLayout()
        trigger_source_row.addWidget(QLabel("Trigger Source:"))
        self.trigger_source_combo = QComboBox()
        # Support Software, External (PFI), and Internal (from master task start trigger)
        self.trigger_source_combo.addItems(["Internal", "External", "Software"])
        self.trigger_source_combo.currentTextChanged.connect(self.on_config_changed)
        trigger_source_row.addWidget(self.trigger_source_combo)
        trigger_layout.addLayout(trigger_source_row)
        
        trigger_terminal_row = QHBoxLayout()
        trigger_terminal_row.addWidget(QLabel("External Terminal:"))
        self.trigger_terminal_edit = QLineEdit("/Dev1/PFI2")
        self.trigger_terminal_edit.textChanged.connect(self.on_config_changed)
        trigger_terminal_row.addWidget(self.trigger_terminal_edit)
        trigger_layout.addLayout(trigger_terminal_row)
        
        trigger_edge_row = QHBoxLayout()
        trigger_edge_row.addWidget(QLabel("Trigger Edge:"))
        self.trigger_edge_combo = QComboBox()
        self.trigger_edge_combo.addItems(["Rising", "Falling"])
        self.trigger_edge_combo.currentTextChanged.connect(self.on_config_changed)
        trigger_edge_row.addWidget(self.trigger_edge_combo)
        trigger_layout.addLayout(trigger_edge_row)
        
        trigger_group.setLayout(trigger_layout)
        left_panel.addWidget(trigger_group)
        
        # Analog Output Configuration Group
        ao_group = QGroupBox("Analog Output Channels")
        ao_layout = QVBoxLayout()
        
        self.ao_channels_list = QListWidget()
        # Use per-item checkboxes instead of selection to make state more explicit
        # and less prone to accidental changes.
        self.ao_channels_list.setSelectionMode(QAbstractItemView.NoSelection)
        ao_layout.addWidget(self.ao_channels_list)
        
        ao_btn_row = QHBoxLayout()
        self.add_ao_waveform_btn = QPushButton("Add Waveform")
        self.add_ao_waveform_btn.clicked.connect(self.show_ao_waveform_dialog)
        ao_btn_row.addWidget(self.add_ao_waveform_btn)
        ao_layout.addLayout(ao_btn_row)
        
        ao_group.setLayout(ao_layout)
        left_panel.addWidget(ao_group)
        
        # Digital Output Configuration Group
        do_group = QGroupBox("Digital Output Lines")
        do_layout = QVBoxLayout()
        
        do_port_row = QHBoxLayout()
        do_port_row.addWidget(QLabel("Port:"))
        self.do_port_combo = QComboBox()
        self.do_port_combo.addItems(["port0", "port1", "port2"])
        self.do_port_combo.currentTextChanged.connect(self.on_config_changed)
        do_port_row.addWidget(self.do_port_combo)
        do_layout.addLayout(do_port_row)
        
        self.do_lines_list = QListWidget()
        self.do_lines_list.setSelectionMode(QAbstractItemView.NoSelection)
        for i in range(8):
            item = QListWidgetItem(f"Line {i}")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.do_lines_list.addItem(item)
        do_layout.addWidget(self.do_lines_list)
        
        do_btn_row = QHBoxLayout()
        self.add_do_pattern_btn = QPushButton("Add Pattern")
        self.add_do_pattern_btn.clicked.connect(self.show_do_pattern_dialog)
        do_btn_row.addWidget(self.add_do_pattern_btn)
        do_layout.addLayout(do_btn_row)
        
        do_group.setLayout(do_layout)
        left_panel.addWidget(do_group)
        
        # Live output (constant values for debugging; does not overwrite waveform/pattern data)
        live_group = QGroupBox("Live Output")
        live_group.setToolTip(
            "Send constant values to outputs for debugging. Only channels/lines with a "
            "pattern or waveform appear. Does not change your acquisition data."
        )
        live_layout = QVBoxLayout()
        self._live_output_container = QWidget()
        self._live_output_layout = QVBoxLayout(self._live_output_container)
        self._live_output_layout.setContentsMargins(0, 0, 0, 0)
        live_layout.addWidget(self._live_output_container)
        self._live_ao_controls = {}  # channel -> {live_cb, slider, value_label}
        self._live_do_controls = {}  # line -> {live_cb, high_cb}
        live_group.setLayout(live_layout)
        left_panel.addWidget(live_group)
        
        # Analog Input Configuration Group
        ai_group = QGroupBox("Analog Input Channels")
        ai_layout = QVBoxLayout()
        
        self.ai_channels_list = QListWidget()
        self.ai_channels_list.setSelectionMode(QAbstractItemView.NoSelection)
        ai_layout.addWidget(self.ai_channels_list)
        
        ai_terminal_row = QHBoxLayout()
        ai_terminal_row.addWidget(QLabel("Terminal Config:"))
        self.ai_terminal_combo = QComboBox()
        self.ai_terminal_combo.addItems(["RSE", "NRSE", "Diff", "PseudoDiff"])
        self.ai_terminal_combo.currentTextChanged.connect(self.on_config_changed)
        ai_terminal_row.addWidget(self.ai_terminal_combo)
        ai_layout.addLayout(ai_terminal_row)
        
        ai_group.setLayout(ai_layout)
        left_panel.addWidget(ai_group)
        
        # DAQ-only Fast Acquisition (no camera, waveform output + recording only)
        daq_acq_group = QGroupBox("DAQ-only Fast Acquisition")
        daq_acq_group.setToolTip(
            "Run a timed waveform experiment: output AO/DO waveforms and record AI/DI. "
            "No camera or frame recording."
        )
        daq_acq_layout = QVBoxLayout()
        
        daq_path_row = QHBoxLayout()
        daq_path_row.addWidget(QLabel("Saving Path:"))
        self.daq_only_saving_dir_edit = QLineEdit()
        self.daq_only_saving_dir_edit.setReadOnly(True)
        from control._def import DEFAULT_SAVING_PATH
        self.daq_only_saving_dir_edit.setText(DEFAULT_SAVING_PATH)
        self._daq_only_output_path = DEFAULT_SAVING_PATH
        daq_path_row.addWidget(self.daq_only_saving_dir_edit)
        self.daq_only_browse_btn = QPushButton("Browse")
        self.daq_only_browse_btn.clicked.connect(self._daq_only_set_saving_dir)
        daq_path_row.addWidget(self.daq_only_browse_btn)
        daq_acq_layout.addLayout(daq_path_row)
        
        daq_exp_row = QHBoxLayout()
        daq_exp_row.addWidget(QLabel("Experiment ID:"))
        self.daq_only_experiment_id_edit = QLineEdit()
        self.daq_only_experiment_id_edit.setPlaceholderText("Optional name for this run")
        daq_exp_row.addWidget(self.daq_only_experiment_id_edit)
        daq_acq_layout.addLayout(daq_exp_row)
        
        daq_dur_row = QHBoxLayout()
        daq_dur_row.addWidget(QLabel("Duration (s):"))
        self.daq_only_duration_spin = QDoubleSpinBox()
        self.daq_only_duration_spin.setRange(0.001, 86400.0)
        self.daq_only_duration_spin.setValue(10.0)
        self.daq_only_duration_spin.setDecimals(3)
        self.daq_only_duration_spin.setSuffix(" s")
        daq_dur_row.addWidget(self.daq_only_duration_spin)
        daq_acq_layout.addLayout(daq_dur_row)
        
        daq_btn_row = QHBoxLayout()
        self.daq_only_start_btn = QPushButton("Start DAQ Experiment")
        self.daq_only_start_btn.clicked.connect(self._start_daq_only_acquisition)
        self.daq_only_start_btn.setStyleSheet("background-color: #4CAF50; color: white;")
        daq_btn_row.addWidget(self.daq_only_start_btn)
        self.daq_only_stop_btn = QPushButton("Stop")
        self.daq_only_stop_btn.clicked.connect(self._stop_daq_only_acquisition)
        self.daq_only_stop_btn.setEnabled(False)
        self.daq_only_stop_btn.setStyleSheet("background-color: #f44336; color: white;")
        daq_btn_row.addWidget(self.daq_only_stop_btn)
        daq_acq_layout.addLayout(daq_btn_row)
        
        daq_status_row = QHBoxLayout()
        daq_status_row.addWidget(QLabel("Status:"))
        self.daq_only_status_label = QLabel("Ready")
        self.daq_only_status_label.setStyleSheet("font-weight: bold;")
        daq_status_row.addWidget(self.daq_only_status_label)
        daq_status_row.addStretch()
        daq_acq_layout.addLayout(daq_status_row)
        
        daq_acq_group.setLayout(daq_acq_layout)
        left_panel.addWidget(daq_acq_group)
        
        self._daq_only_controller: Optional[FastAcquisitionController] = None
        self._daq_only_acquiring = False
        self._updating_daq_duration_sync = False  # prevent re-entrancy when syncing duration <-> samples
        
        # Sync Device Configuration with DAQ-only Duration: connect after both panels exist
        self.sample_rate_spin.valueChanged.connect(self._on_device_sample_rate_changed)
        self.num_samples_spin.valueChanged.connect(self._on_device_samples_changed)
        self.daq_only_duration_spin.valueChanged.connect(self._on_daq_only_duration_changed)
        # Initial sync: set duration to match device (samples/rate)
        self.daq_only_duration_spin.blockSignals(True)
        self.daq_only_duration_spin.setValue(
            self.num_samples_spin.value() / max(1.0, self.sample_rate_spin.value())
        )
        self.daq_only_duration_spin.blockSignals(False)
        
        left_panel.addStretch()
        main_layout.addLayout(left_panel, 1)
        
        # Right panel - Waveform display and controls
        right_panel = QVBoxLayout()
        
        # Waveform Plot
        from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
        from matplotlib.figure import Figure
        
        self.fig = Figure(figsize=(8, 6))
        self.canvas = FigureCanvas(self.fig)
        self.ax_ao = self.fig.add_subplot(311)
        self.ax_do = self.fig.add_subplot(312)
        self.ax_ai = self.fig.add_subplot(313)
        
        self.ax_ao.set_title("Analog Output")
        self.ax_ao.set_ylabel("Voltage (V)")
        self.ax_do.set_title("Digital Output")
        self.ax_do.set_ylabel("Level")
        self.ax_ai.set_title("Analog Input (Acquired)")
        self.ax_ai.set_xlabel("Time (s)")
        self.ax_ai.set_ylabel("Voltage (V)")
        
        self.fig.tight_layout()
        right_panel.addWidget(self.canvas)
        
        # Control buttons
        control_row = QHBoxLayout()
        
        self.arm_btn = QPushButton("Arm")
        self.arm_btn.clicked.connect(self.arm_tasks)
        self.arm_btn.setStyleSheet("background-color: #4CAF50; color: white;")
        control_row.addWidget(self.arm_btn)
        
        self.trigger_btn = QPushButton("Trigger")
        self.trigger_btn.clicked.connect(self.send_trigger)
        self.trigger_btn.setEnabled(False)
        self.trigger_btn.setStyleSheet("background-color: #2196F3; color: white;")
        control_row.addWidget(self.trigger_btn)
        
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_tasks)
        self.stop_btn.setStyleSheet("background-color: #f44336; color: white;")
        control_row.addWidget(self.stop_btn)
        
        right_panel.addLayout(control_row)
        
        # Status display
        status_row = QHBoxLayout()
        status_row.addWidget(QLabel("Status:"))
        self.status_label = QLabel("Not configured")
        self.status_label.setStyleSheet("font-weight: bold;")
        status_row.addWidget(self.status_label)
        status_row.addStretch()
        right_panel.addLayout(status_row)
        
        main_layout.addLayout(right_panel, 2)
        self.on_device_changed(self.device_combo.currentText())
        self._load_config_into_ui()
        self._rebuild_live_output_controls()

    def _load_config_into_ui(self):
        """Initialize UI widgets from the current configuration/state."""
        # Sample rate and samples per channel
        self.sample_rate_spin.setValue(float(getattr(self._ni_daq, "sample_rate_hz", 10000.0)))
        self.num_samples_spin.setValue(int(getattr(self._ni_daq, "samples_per_channel", 10000)))
        # Continuous mode
        self.continuous_checkbox.setChecked(bool(getattr(self._ni_daq, "continuous", False)))
        # Trigger source
        if hasattr(self._ni_daq, "trigger_source"):
            src = self._ni_daq.trigger_source
            # Accept both enum and string
            if hasattr(src, "name"):
                src_name = src.name
            else:
                src_name = str(src)
            if src_name.upper().startswith("SOFTWARE"):
                self.trigger_source_combo.setCurrentText("Software")
            elif src_name.upper().startswith("EXTERNAL"):
                self.trigger_source_combo.setCurrentText("External")
            else:
                self.trigger_source_combo.setCurrentText("Internal")
        # Trigger edge
        if hasattr(self._ni_daq, "trigger_edge"):
            edge = self._ni_daq.trigger_edge
            if hasattr(edge, "name"):
                edge_name = edge.name
            else:
                edge_name = str(edge)
            self.trigger_edge_combo.setCurrentText("Rising" if edge_name.upper().startswith("RISING") else "Falling")
        # External trigger terminal
        if hasattr(self._ni_daq, "external_trigger_terminal"):
            self.trigger_terminal_edit.setText(str(self._ni_daq.external_trigger_terminal))
        # AI terminal config
        if hasattr(self._ni_daq, "ai_terminal_config"):
            idx = self.ai_terminal_combo.findText(str(self._ni_daq.ai_terminal_config))
            if idx >= 0:
                self.ai_terminal_combo.setCurrentIndex(idx)

    
    def on_refresh(self):
        """Refresh the list of available devices."""
        self.device_combo.clear()
        
        if self._ni_daq is not None:
            devices = self._ni_daq.get_available_devices()
            if devices:
                self.device_combo.addItems(devices)
                self.status_label.setText("Ready")
                self.on_device_changed(self.device_combo.currentText())
            else:
                self.device_combo.addItem("No devices found")
                self.status_label.setText("No devices found")
        else:
            self.device_combo.addItem("DAQ not available")
            self.status_label.setText("DAQ not available")

    def on_device_changed(self, device_name: str):
        """Handle device selection change."""
        self._log.info(f"DAQ Device changed to: {device_name}")
        if not device_name or device_name in ["No devices found", "DAQ not available"]:
            return
        
        if hasattr(self._ni_daq, "device_name"):
            self._ni_daq.device_name = device_name
        
        # Get device info and update channel lists
        if self._ni_daq is not None:
            info = self._ni_daq.get_device_info(device_name)

            # Build a mapping from NIDAQ physical channel IDs to IO endpoint display names
            self._endpoint_labels_ao = {}
            self._endpoint_labels_ai = {}
            self._endpoint_labels_do = {}
            endpoint_labels_ao = self._endpoint_labels_ao
            endpoint_labels_ai = self._endpoint_labels_ai
            endpoint_labels_do = self._endpoint_labels_do
            if self._io_endpoint_config is not None:
                try:
                    for ep in self._io_endpoint_config.get_controller_endpoints(IOControllerType.NIDAQ):
                        if not ep.display_name:
                            continue
                        cid = ep.channel_id
                        if ep.signal_type == IOSignalType.ANALOG and ep.direction == IODirection.OUTPUT:
                            # Expect "ao0", "ao1", ...
                            endpoint_labels_ao[cid] = ep.display_name
                        elif ep.signal_type == IOSignalType.ANALOG and ep.direction == IODirection.INPUT:
                            # Expect "ai0", "ai1", ...
                            endpoint_labels_ai[cid] = ep.display_name
                        elif ep.signal_type == IOSignalType.DIGITAL:
                            # Expect "port0/line6" style channel_id
                            if "port" in cid and "/line" in cid:
                                try:
                                    port_part, line_part = cid.split("/", 1)
                                    port_name = port_part
                                    line_idx = int(line_part.replace("line", ""))
                                    endpoint_labels_do[(port_name, line_idx)] = ep.display_name
                                except Exception:
                                    # Ignore unparsable IDs; they just won't get labels
                                    continue
                except Exception as e:
                    self._log.warning(f"Failed to build NIDAQ endpoint label map: {e}", exc_info=True)
            
            # Update AO channels
            self.ao_channels_list.clear()
            for ch in info.get("ao_channels", []):
                # Extract just the channel part (e.g., "ao0" from "Dev1/ao0")
                ch_name = ch.split("/")[-1] if "/" in ch else ch
                label = endpoint_labels_ao.get(ch_name)
                if label:
                    text = f"{ch_name} — {label}"
                else:
                    text = ch_name
                item = QListWidgetItem(text)
                # Store the raw channel id for later use
                item.setData(Qt.UserRole, ch_name)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Unchecked)
                self.ao_channels_list.addItem(item)
            
            # Update AI channels  
            self.ai_channels_list.clear()
            for ch in info.get("ai_channels", []):
                ch_name = ch.split("/")[-1] if "/" in ch else ch
                label = endpoint_labels_ai.get(ch_name)
                if label:
                    text = f"{ch_name} — {label}"
                else:
                    text = ch_name
                item = QListWidgetItem(text)
                item.setData(Qt.UserRole, ch_name)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Unchecked)
                self.ai_channels_list.addItem(item)
            
            # Update trigger terminal default
            self.trigger_terminal_edit.setText(f"/{device_name}/PFI2")
            
            # Pre-select channels/lines based on current config/state
            # AO channels: prefer task-IO selection if available, otherwise fall back
            task_io = self._ni_daq.get_task_io()
            selected_ao = set(task_io.get("ao_channels", []))
            for i in range(self.ao_channels_list.count()):
                item = self.ao_channels_list.item(i)
                ch_name = item.data(Qt.UserRole) or item.text()
                item.setCheckState(Qt.Checked if ch_name in selected_ao else Qt.Unchecked)

            # AI channels: prefer task-IO selection if available
            selected_ai = set(task_io.get("ai_channels", []))
            for i in range(self.ai_channels_list.count()):
                item = self.ai_channels_list.item(i)
                ch_name = item.data(Qt.UserRole) or item.text()
                item.setCheckState(Qt.Checked if ch_name in selected_ai else Qt.Unchecked)

            # DO port and lines
            cfg_do_port = getattr(self._ni_daq, "do_port", None)
            if cfg_do_port:
                idx = self.do_port_combo.findText(str(cfg_do_port))
                if idx >= 0:
                    self.do_port_combo.setCurrentIndex(idx)

            selected_do_lines = set(getattr(self._ni_daq, "do_lines", []) or [])
            port = self.do_port_combo.currentText() or "port0"
            for i in range(self.do_lines_list.count()):
                item = self.do_lines_list.item(i)
                item.setCheckState(Qt.Checked if i in selected_do_lines else Qt.Unchecked)
                label = endpoint_labels_do.get((port, i))
                item.setText(f"Line {i} — {label}" if label else f"Line {i}")

            # Push descriptions to NIDAQ so they're available for HDF5 saving
            descriptions: dict[str, str] = {}
            for ch, name in endpoint_labels_ao.items():
                descriptions[ch] = name
            for ch, name in endpoint_labels_ai.items():
                descriptions[ch] = name
            for (_, line_idx), name in endpoint_labels_do.items():
                descriptions[f"line{line_idx}"] = name
            if descriptions and hasattr(self._ni_daq, "set_channel_descriptions"):
                self._ni_daq.set_channel_descriptions(descriptions)
    
    def on_config_changed(self):
        """Handle configuration changes."""
        self._update_config()
        self._update_duration_display()
    
    def is_linked_to_fast_acquisition(self) -> bool:
        """Return True if this widget should be updated when Fast Acquisition parameters change."""
        return self.link_to_fast_acquisition_checkbox.isChecked()
    
    def _update_config(self):
        """Update the configuration from UI values."""
        from control.nidaq import TriggerSource, TriggerEdge
        
        self._ni_daq.device_name = self.device_combo.currentText()
        self._ni_daq.sample_rate_hz = self.sample_rate_spin.value()
        self._ni_daq.samples_per_channel = self.num_samples_spin.value()
        self._ni_daq.continuous = self.continuous_checkbox.isChecked()
        self._ni_daq.do_port = self.do_port_combo.currentText()
        self._ni_daq.ai_terminal_config = self.ai_terminal_combo.currentText()
        self._ni_daq.external_trigger_terminal = self.trigger_terminal_edit.text()
        
        # Trigger source
        src_text = self.trigger_source_combo.currentText()
        if src_text == "Software":
            self._ni_daq.trigger_source = TriggerSource.SOFTWARE
        elif src_text == "External":
            self._ni_daq.trigger_source = TriggerSource.EXTERNAL
            # If terminal is empty, default to PFI2 on the selected device
            if not self._ni_daq.external_trigger_terminal:
                device = self._ni_daq.device_name or self.device_combo.currentText()
                if device:
                    self._ni_daq.external_trigger_terminal = f"/{device}/PFI2"
                    self.trigger_terminal_edit.setText(self._ni_daq.external_trigger_terminal)
        else:
            # "Internal" option: use internal start trigger routing (master AO/DO/DI)
            self._ni_daq.trigger_source = TriggerSource.INTERNAL
        
        # Trigger edge
        if self.trigger_edge_combo.currentText() == "Rising":
            self._ni_daq.trigger_edge = TriggerEdge.RISING
        else:
            self._ni_daq.trigger_edge = TriggerEdge.FALLING
        
        # Get selected AO channels (task IO subset) from checkboxes
        selected_ao = []
        for i in range(self.ao_channels_list.count()):
            item = self.ao_channels_list.item(i)
            if item.checkState() == Qt.Checked:
                ch_name = item.data(Qt.UserRole) or item.text()
                selected_ao.append(ch_name)
        
        # Get selected AI channels (task IO subset) from checkboxes
        selected_ai = []
        for i in range(self.ai_channels_list.count()):
            item = self.ai_channels_list.item(i)
            if item.checkState() == Qt.Checked:
                ch_name = item.data(Qt.UserRole) or item.text()
                selected_ai.append(ch_name)
        
        # Get selected DO lines (task IO subset) from checkboxes
        selected_do: list[int] = []
        for i in range(self.do_lines_list.count()):
            item = self.do_lines_list.item(i)
            if item.checkState() == Qt.Checked:
                selected_do.append(i)

        # Push task IO selection into NI DAQ without overwriting full available sets
        self._ni_daq.configure_task_io(
            ao_channels=selected_ao,
            do_lines=selected_do,
            # DI task selection is currently driven by higher-level controllers;
            # we leave di_lines unchanged here.
            ai_channels=selected_ai,
        )
        
        # Set digital I/O logic family from global config
        # FLIR cameras require 3.3V TTL, Photometrics and others use 5V TTL
        from control._def import NI_DAQ_LOGIC_FAMILY
        self._ni_daq.do_logic_family = NI_DAQ_LOGIC_FAMILY
    
    def _update_duration_display(self):
        """Update the duration display label."""
        duration = self._ni_daq.samples_per_channel / self._ni_daq.sample_rate_hz
        self.duration_label.setText(f"{duration:.3f}")
    
    def show_ao_waveform_dialog(self):
        """Show dialog to configure analog output waveform."""
        dialog = AOWaveformDialog(
            self._ni_daq.sample_rate_hz,
            self._ni_daq.samples_per_channel,
            self._ni_daq.ao_channels,
            self
        )
        if dialog.exec_() == QDialog.Accepted:
            channel, waveform = dialog.get_waveform()
            if channel and waveform is not None:
                self._ao_waveforms[channel] = waveform
                self._update_waveform_plot()
                self._rebuild_live_output_controls()
    
    def show_do_pattern_dialog(self):
        """Show dialog to configure digital output pattern."""
        port = self.do_port_combo.currentText() or "port0"
        available_lines = list(range(8))
        line_labels = {
            line: self._endpoint_labels_do.get((port, line))
            for line in available_lines
        }
        dialog = DOPatternDialog(
            self._ni_daq.sample_rate_hz,
            self._ni_daq.samples_per_channel,
            available_lines,
            self,
            line_labels=line_labels,
        )
        if dialog.exec_() == QDialog.Accepted:
            line, pattern = dialog.get_pattern()
            if line is not None and pattern is not None:
                self._do_patterns[line] = pattern
                self._update_waveform_plot()
                self._rebuild_live_output_controls()
    
    def _update_waveform_plot(self):
        """Update the waveform display plot."""
        # Clear all axes
        self.ax_ao.clear()
        self.ax_do.clear()
        
        t = np.arange(self._ni_daq.samples_per_channel) / self._ni_daq.sample_rate_hz
        
        # Plot AO waveforms
        self.ax_ao.set_title("Analog Output")
        self.ax_ao.set_ylabel("Voltage (V)")
        for channel, data in self._ao_waveforms.items():
            if len(data) == len(t):
                ao_label = self._endpoint_labels_ao.get(channel, channel)
                self.ax_ao.plot(t, data, label=ao_label)
        if self._ao_waveforms:
            self.ax_ao.legend(loc='upper right')
            self.ax_ao.grid(True, alpha=0.3)
        
        # Plot DO patterns
        self.ax_do.set_title("Digital Output")
        self.ax_do.set_ylabel("Level")
        offset = 0
        for line, data in self._do_patterns.items():
            if len(data) == len(t):
                port = self.do_port_combo.currentText() or "port0"
                do_label = self._endpoint_labels_do.get((port, line), f"Line {line}")
                self.ax_do.plot(t, data.astype(float) + offset * 1.2, label=do_label)
                offset += 1
        if self._do_patterns:
            self.ax_do.legend(loc='upper right')
            self.ax_do.grid(True, alpha=0.3)
        
        self.fig.tight_layout()
        self.canvas.draw()
    
    def get_waveforms(self) -> 'WaveformData':
        """
        Get current waveforms configured in the widget.
        
        Returns:
            WaveformData object with analog_output and digital_output dictionaries
        """
        from control.nidaq import WaveformData
        return WaveformData(
            analog_output=self._ao_waveforms.copy(),
            digital_output=self._do_patterns.copy()
        )
    
    def get_config(self):
        """
        Get current configuration from the widget.
        
        Returns:
            Configuration/state object with current settings
        """
        self._update_config()
        return self._ni_daq
    
    def update_waveforms_for_duration(self, new_duration_s: float, sample_rate_hz: float):
        """
        Update all waveforms to match a new duration by cropping or extending with zeros.
        
        Args:
            new_duration_s: New duration in seconds
            sample_rate_hz: Sample rate in Hz
        """
        new_num_samples = int(sample_rate_hz * new_duration_s)
        
        # Update analog output waveforms
        for channel in list(self._ao_waveforms.keys()):
            old_waveform = self._ao_waveforms[channel]
            if len(old_waveform) > new_num_samples:
                # Crop
                self._ao_waveforms[channel] = old_waveform[:new_num_samples]
            elif len(old_waveform) < new_num_samples:
                # Extend with zeros
                extended = np.zeros(new_num_samples, dtype=old_waveform.dtype)
                extended[:len(old_waveform)] = old_waveform
                self._ao_waveforms[channel] = extended
        
        # Update digital output patterns
        for line in list(self._do_patterns.keys()):
            old_pattern = self._do_patterns[line]
            if len(old_pattern) > new_num_samples:
                # Crop
                self._do_patterns[line] = old_pattern[:new_num_samples]
            elif len(old_pattern) < new_num_samples:
                # Extend with zeros
                extended = np.zeros(new_num_samples, dtype=old_pattern.dtype)
                extended[:len(old_pattern)] = old_pattern
                self._do_patterns[line] = extended
        
        # Update config samples_per_channel and sample_rate_hz
        self._ni_daq.samples_per_channel = new_num_samples
        self._ni_daq.sample_rate_hz = sample_rate_hz
        
        # Update UI to reflect new sample rate
        self.sample_rate_spin.blockSignals(True)
        self.sample_rate_spin.setValue(sample_rate_hz)
        self.sample_rate_spin.blockSignals(False)
        
        # Update num_samples_spin to reflect new number of samples
        self.num_samples_spin.blockSignals(True)
        self.num_samples_spin.setValue(new_num_samples)
        self.num_samples_spin.blockSignals(False)
        
        # Update duration display
        self._update_duration_display()
        
        # Update waveform plot
        self._update_waveform_plot()
    
    def update_waveforms_for_sample_rate(self, new_sample_rate_hz: float, duration_s: float):
        """
        Update all waveforms to match a new sample rate by resampling (dilating/contracting).
        
        Args:
            new_sample_rate_hz: New sample rate in Hz
            duration_s: Duration in seconds (to calculate new number of samples)
        """
        from scipy import signal
        
        new_num_samples = int(new_sample_rate_hz * duration_s)
        old_sample_rate_hz = self._ni_daq.sample_rate_hz
        
        # If sample rate hasn't changed, just update duration
        if abs(old_sample_rate_hz - new_sample_rate_hz) < 1e-6:
            self.update_waveforms_for_duration(duration_s, new_sample_rate_hz)
            return
        
        # Update analog output waveforms by resampling
        for channel in list(self._ao_waveforms.keys()):
            old_waveform = self._ao_waveforms[channel]
            old_num_samples = len(old_waveform)
            
            if old_num_samples == 0:
                # Empty waveform, just create zeros
                self._ao_waveforms[channel] = np.zeros(new_num_samples, dtype=old_waveform.dtype)
            elif old_num_samples == new_num_samples:
                # Same number of samples, no resampling needed
                pass
            else:
                # Resample using scipy.signal.resample (preserves signal shape)
                resampled = signal.resample(old_waveform, new_num_samples)
                # Preserve dtype
                self._ao_waveforms[channel] = resampled.astype(old_waveform.dtype)
        
        # Update digital output patterns by resampling
        for line in list(self._do_patterns.keys()):
            old_pattern = self._do_patterns[line]
            old_num_samples = len(old_pattern)
            
            if old_num_samples == 0:
                # Empty pattern, just create zeros
                self._do_patterns[line] = np.zeros(new_num_samples, dtype=old_pattern.dtype)
            elif old_num_samples == new_num_samples:
                # Same number of samples, no resampling needed
                pass
            else:
                # For digital signals, we need to preserve boolean nature
                # Resample as float first, then threshold at 0.5
                resampled_float = signal.resample(old_pattern.astype(float), new_num_samples)
                # Convert back to boolean by thresholding
                self._do_patterns[line] = (resampled_float >= 0.5).astype(old_pattern.dtype)
        
        # Update config
        self._ni_daq.samples_per_channel = new_num_samples
        self._ni_daq.sample_rate_hz = new_sample_rate_hz
        
        # Update UI to reflect new sample rate
        self.sample_rate_spin.blockSignals(True)
        self.sample_rate_spin.setValue(new_sample_rate_hz)
        self.sample_rate_spin.blockSignals(False)
        
        # Update num_samples_spin to reflect new number of samples
        self.num_samples_spin.blockSignals(True)
        self.num_samples_spin.setValue(new_num_samples)
        self.num_samples_spin.blockSignals(False)
        
        # Update duration display
        self._update_duration_display()
        
        # Update waveform plot
        self._update_waveform_plot()
    
    def _rebuild_live_output_controls(self):
        """Rebuild the Live output panel from current _ao_waveforms and _do_patterns."""
        # Clear existing (handles both widgets and nested layouts/rows)
        def _clear_layout(layout):
            while layout.count():
                item = layout.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.deleteLater()
                    continue
                child_layout = item.layout()
                if child_layout is not None:
                    _clear_layout(child_layout)
                    child_layout.deleteLater()
        _clear_layout(self._live_output_layout)
        self._live_ao_controls.clear()
        self._live_do_controls.clear()
        if self._ni_daq is None:
            return
        self._update_config()
        v_min = getattr(self._ni_daq, 'ao_min_voltage', -10.0)
        v_max = getattr(self._ni_daq, 'ao_max_voltage', 10.0)
        for channel in sorted(self._ao_waveforms.keys()):
            row = QHBoxLayout()
            live_cb = QCheckBox("Live")
            live_cb.setToolTip("Output constant voltage to this channel for debugging")
            slider = QSlider(Qt.Horizontal)
            slider.setRange(0, 1000)
            slider.setValue(500)
            slider.setToolTip(f"Voltage: {v_min} to {v_max} V")
            value_label = QLabel("0.00 V")
            value_label.setMinimumWidth(48)
            def _make_ao_sync(ch, sl, lab):
                def _sync():
                    v = v_min + (v_max - v_min) * (sl.value() / 1000.0)
                    lab.setText(f"{v:.2f} V")
                    self._apply_live_output()
                return _sync
            live_cb.stateChanged.connect(lambda checked, c=channel: self._apply_live_output())
            slider.valueChanged.connect(_make_ao_sync(channel, slider, value_label))
            _make_ao_sync(channel, slider, value_label)()
            row.addWidget(QLabel(channel))
            row.addWidget(live_cb)
            row.addWidget(slider)
            row.addWidget(value_label)
            self._live_output_layout.addLayout(row)
            self._live_ao_controls[channel] = {"live_cb": live_cb, "slider": slider, "value_label": value_label}
        for line in sorted(self._do_patterns.keys()):
            row = QHBoxLayout()
            live_cb = QCheckBox("Live")
            live_cb.setToolTip("Output constant level to this line for debugging")
            high_cb = QCheckBox("High")
            high_cb.setToolTip("When Live is on: checked = high, unchecked = low")
            live_cb.stateChanged.connect(lambda: self._apply_live_output())
            high_cb.stateChanged.connect(lambda: self._apply_live_output())
            row.addWidget(QLabel(f"Line {line}"))
            row.addWidget(live_cb)
            row.addWidget(high_cb)
            self._live_output_layout.addLayout(row)
            self._live_do_controls[line] = {"live_cb": live_cb, "high_cb": high_cb}
        if not self._live_ao_controls and not self._live_do_controls:
            self._live_output_layout.addWidget(QLabel("Add a waveform or pattern above to enable live control."))
    
    def _apply_live_output(self):
        """Send current live output values to the DAQ (does not overwrite waveform/pattern data)."""
        if self._ni_daq is None:
            return
        self._update_config()
        v_min = getattr(self._ni_daq, 'ao_min_voltage', -10.0)
        v_max = getattr(self._ni_daq, 'ao_max_voltage', 10.0)
        ao_values = {}
        for channel, ctrl in self._live_ao_controls.items():
            if ctrl["live_cb"].isChecked():
                v = v_min + (v_max - v_min) * (ctrl["slider"].value() / 1000.0)
                ao_values[channel] = float(v)
        do_values = {}
        for line, ctrl in self._live_do_controls.items():
            if ctrl["live_cb"].isChecked():
                do_values[line] = ctrl["high_cb"].isChecked()
        try:
            if ao_values or do_values:
                self._ni_daq.start_live_output(ao_values=ao_values, do_values=do_values)
            else:
                self._ni_daq.stop_live_output()
        except Exception as e:
            self._log.error(f"Live output failed: {e}", exc_info=True)
    
    def _on_daq_only_duration_changed(self, duration_s: float):
        """Sync Device Configuration from DAQ-only Duration: set samples = duration * rate, update waveforms."""
        if self._updating_daq_duration_sync:
            return
        self._updating_daq_duration_sync = True
        try:
            rate = self.sample_rate_spin.value()
            self.update_waveforms_for_duration(duration_s, rate)
            self._update_duration_display()
        finally:
            self._updating_daq_duration_sync = False
    
    def _on_device_samples_changed(self):
        """Sync DAQ-only Duration from Device Configuration samples: duration = samples / rate, update waveforms."""
        if self._updating_daq_duration_sync:
            return
        self.on_config_changed()
        self._updating_daq_duration_sync = True
        try:
            samples = self.num_samples_spin.value()
            rate = self.sample_rate_spin.value()
            if rate <= 0:
                rate = 1.0
            duration_s = samples / rate
            self.daq_only_duration_spin.blockSignals(True)
            self.daq_only_duration_spin.setValue(duration_s)
            self.daq_only_duration_spin.blockSignals(False)
            self.update_waveforms_for_duration(duration_s, rate)
        finally:
            self._updating_daq_duration_sync = False
    
    def _on_device_sample_rate_changed(self):
        """Hold duration constant: set samples = duration * new_rate, resample waveforms."""
        if self._updating_daq_duration_sync:
            return
        self._updating_daq_duration_sync = True
        try:
            duration_s = self.daq_only_duration_spin.value()
            new_rate = self.sample_rate_spin.value()
            self.update_waveforms_for_sample_rate(new_rate, duration_s)
            self._update_config()
        finally:
            self._updating_daq_duration_sync = False
    
    def _daq_only_set_saving_dir(self):
        """Set saving directory for DAQ-only fast acquisition."""
        dialog = QFileDialog()
        save_dir = dialog.getExistingDirectory(None, "Select Folder", self.daq_only_saving_dir_edit.text())
        if save_dir:
            self.daq_only_saving_dir_edit.setText(save_dir)
            self._daq_only_output_path = save_dir
    
    def _start_daq_only_acquisition(self):
        """Start DAQ-only fast acquisition (waveform output + recording, no camera)."""
        if self._daq_only_acquiring:
            self._log.warning("DAQ-only acquisition already running")
            return
        if self._ni_daq is None:
            self._log.error("NI DAQ not available")
            error_dialog("NI DAQ not available.", "Configuration Error")
            return
        
        output_path_base = self.daq_only_saving_dir_edit.text().strip() or self._daq_only_output_path
        if not output_path_base:
            error_dialog("Please set saving path for DAQ-only experiment.", "Configuration Error")
            return
        
        experiment_id = self.daq_only_experiment_id_edit.text().strip()
        if experiment_id:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            full_output_path = os.path.join(output_path_base, f"{timestamp}_{experiment_id}")
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            full_output_path = os.path.join(output_path_base, f"{timestamp}_daq_only_experiment")
        os.makedirs(full_output_path, exist_ok=True)
        
        duration_s = self.daq_only_duration_spin.value()
        sample_rate_hz = self.sample_rate_spin.value()
        self.update_waveforms_for_duration(duration_s, sample_rate_hz)
        waveforms = self.get_waveforms()
        self._update_config()

        # Prefer task-IO subsets for acquisition if available
        task_io = self._ni_daq.get_task_io()
        ai_channels = task_io.get("ai_channels")
        ao_channels = task_io.get("ao_channels")

        di_lines = None  # Optional: list of digital input line indices to record; None or [] records no DI
        
        from control.core.fast_acquisition_controller import AcquisitionCompletionStatus
        
        try:
            self._daq_only_controller = FastAcquisitionController(
                camera=None,
                ni_daq=self._ni_daq,
                output_path=full_output_path,
            )
            def on_done(status: AcquisitionCompletionStatus, error_message: Optional[str]):
                # Emit signal so UI update runs on main thread (callback runs in controller's monitor thread)
                self.signal_daq_only_completed.emit(status, error_message or "")
            self._daq_only_controller.set_completion_callback(on_done)
            self._daq_only_controller.start_acquisition(
                sample_rate_hz=sample_rate_hz,
                ai_channels=ai_channels,
                ao_channels=ao_channels,
                di_lines=di_lines,
                waveforms=waveforms,
                duration_s=duration_s,
            )
            self._daq_only_acquiring = True
            self.daq_only_start_btn.setEnabled(False)
            self.daq_only_stop_btn.setEnabled(True)
            self.daq_only_status_label.setText("Acquiring...")
            self.signal_acquisition_started.emit()
        except Exception as e:
            self._log.error(f"Failed to start DAQ-only acquisition: {e}", exc_info=True)
            error_dialog(f"Failed to start DAQ-only acquisition: {e}", "Error")
    
    def _stop_daq_only_acquisition(self):
        """Stop DAQ-only fast acquisition."""
        if not self._daq_only_acquiring or self._daq_only_controller is None:
            return
        try:
            self._daq_only_controller.stop_acquisition(manual_stop=True)
        except Exception as e:
            self._log.error(f"Error stopping DAQ-only acquisition: {e}", exc_info=True)
    
    def _on_daq_only_acquisition_completed(self, status, error_message: Optional[str] = None):
        """Handle DAQ-only acquisition completion (called on main thread)."""
        from control.core.fast_acquisition_controller import AcquisitionCompletionStatus
        self._daq_only_acquiring = False
        self._daq_only_controller = None
        self.daq_only_start_btn.setEnabled(True)
        self.daq_only_stop_btn.setEnabled(False)
        if status == AcquisitionCompletionStatus.COMPLETED_SUCCESS:
            self.daq_only_status_label.setText("Completed")
        elif status == AcquisitionCompletionStatus.STOPPED_MANUAL:
            self.daq_only_status_label.setText("Stopped")
        elif status == AcquisitionCompletionStatus.COMPLETED_ERROR:
            self.daq_only_status_label.setText("Error" + (f": {error_message}" if error_message else ""))
        else:
            self.daq_only_status_label.setText("Ready")
        # Set all analog and digital outputs to zero after acquisition
        try:
            self.zero_all_outputs()
        except Exception as e:
            self._log.error(f"Failed to zero DAQ outputs after acquisition: {e}", exc_info=True)
        self.signal_acquisition_finished.emit()
    
    def zero_all_outputs(self):
        """
        Set all configured analog and digital outputs to zero.
        This is called after acquisition completes to ensure outputs are safe.
        """
        if self._ni_daq is None:
            self._log.warning("Cannot zero outputs: No DAQ available")
            return
        
        try:
            # Get all configured output channels and lines
            ao_channels = list(self._ao_waveforms.keys())
            do_lines = list(self._do_patterns.keys())
            
            if not ao_channels and not do_lines:
                self._log.debug("No outputs configured, nothing to zero")
                return
            
            # Create zero waveforms for all configured outputs
            # Use current config to determine number of samples
            num_samples = self._ni_daq.samples_per_channel
            if num_samples == 0:
                num_samples = 1  # At least one sample needed
            
            zero_ao_waveforms = {}
            zero_do_patterns = {}
            
            # Create zero waveforms for analog outputs
            for channel in ao_channels:
                zero_ao_waveforms[channel] = np.zeros(num_samples, dtype=np.float64)
            
            # Create zero patterns for digital outputs
            for line in do_lines:
                zero_do_patterns[line] = np.zeros(num_samples, dtype=bool)
            
            # Create a minimal config for writing zeros
            from control.nidaq import WaveformData, TriggerSource
            zero_waveforms = WaveformData(
                analog_output=zero_ao_waveforms,
                digital_output=zero_do_patterns
            )
            
            # Configure DAQ with minimal settings for writing zeros
            zero_config = {
                "device_name": self._ni_daq.device_name,
                "sample_rate_hz": 1000.0,  # Low rate for quick write
                "samples_per_channel": num_samples,
                "ao_channels": ao_channels,
                "do_port": self._ni_daq.do_port,
                "do_lines": do_lines,
                "trigger_source": TriggerSource.SOFTWARE,
                "continuous": False,
                "do_logic_family": self._ni_daq.do_logic_family,
            }
            
            # Stop any running tasks first
            try:
                self._ni_daq.stop()
            except:
                pass
            
            # Configure and set zero waveforms
            self._ni_daq.configure(**zero_config)
            self._ni_daq.set_waveforms(zero_waveforms)
            
            # Arm and trigger to write zeros
            self._ni_daq.arm()
            self._ni_daq.start_trigger()
            
            # Wait briefly for the write to complete
            import time
            time.sleep(0.1)
            
            # Stop and release tasks so the device is free for live output or next acquisition
            self._ni_daq.stop()
            self._ni_daq.release_tasks()
            
            # Note: We do NOT update self._ao_waveforms or self._do_patterns here
            # to preserve the user's configured waveforms. Only the hardware outputs
            # are zeroed, not the stored waveform data.
            
            self._log.info(f"Zeroed all outputs: {len(ao_channels)} AO channels, {len(do_lines)} DO lines")
            
        except Exception as e:
            self._log.error(f"Failed to zero outputs: {e}", exc_info=True)
    
    def is_line_configured(self, line: int, is_digital: bool = True) -> bool:
        """
        Check if an output or input line has a configured pattern.
        
        Args:
            line: Line number to check
            is_digital: If True, check digital output and input; if False, check analog output (by channel name)
            
        Returns:
            True if line/channel has a configured waveform/pattern or is configured as an input
        """
        if is_digital:
            # Check if line is configured as digital output (has a pattern)
            if line in self._do_patterns:
                return True
            # Check if line is configured as digital input
            # Update config first to ensure di_lines is current
            self._update_config()
            if line in self._ni_daq.di_lines:
                return True
            return False
        else:
            # For analog, line is actually a channel name string
            return str(line) in self._ao_waveforms
    
    def arm_tasks(self):
        """Arm the DAQ tasks for triggered acquisition."""
        if self._ni_daq is None:
            self._create_daq()
        
        if self._ni_daq is None:
            self.status_label.setText("No DAQ available")
            return
        
        try:
            # Update configuration
            self._update_config()
            # Persist any changes already written into the NI DAQ attributes
            # (no separate config object to pass)
            
            # Set waveforms
            from control.nidaq import WaveformData
            waveforms = WaveformData(
                analog_output=self._ao_waveforms.copy(),
                digital_output=self._do_patterns.copy()
            )
            self._ni_daq.set_waveforms(waveforms)
            
            # Arm
            self._ni_daq.arm()
            
            self.trigger_btn.setEnabled(True)
            self.status_label.setText("Armed - waiting for trigger")
            self._log.info("DAQ armed and ready")
            
        except Exception as e:
            self._log.error(f"Failed to arm DAQ: {e}")
            self.status_label.setText(f"Error: {e}")
    
    def send_trigger(self):
        """Send software trigger to start acquisition."""
        if self._ni_daq is None or not self._ni_daq.is_armed:
            self.status_label.setText("Not armed")
            return
        
        try:
            self.signal_acquisition_started.emit()
            self._ni_daq.start_trigger()
            self.status_label.setText("Running...")
            self.trigger_btn.setEnabled(False)
            self.start_completion_listener()
            
        except Exception as e:
            self._log.error(f"Failed to trigger: {e}")
            self.status_label.setText(f"Error: {e}")
    
    def start_completion_listener(self):
        """Start the completion listener thread."""
        self._completion_thread = threading.Thread(target=self._wait_for_completion)
        self._completion_thread.daemon = True
        self._completion_thread.start()

    def _wait_for_completion(self):
        """Wait for acquisition to complete and update UI."""
        if self._ni_daq is None:
            return
        
        timeout = (self._ni_daq.samples_per_channel / self._ni_daq.sample_rate_hz) + 5.0
        self._log.info(f"Waiting for tasks to complete (timeout={timeout}s)...")
        success = self._ni_daq.wait_until_done(timeout)
        self._log.info(f"Acquisition completed: {success}")
        
        if success:
            # Get acquired data
            result = self._ni_daq.get_acquired_data()
            
            # Update AI plot on main thread
            self._update_ai_plot(result)
            self.status_label.setText("Acquisition complete")
        else:
            self.status_label.setText("Acquisition timeout")

        self.signal_acquisition_finished.emit()
    
    def _update_ai_plot(self, result):
        """Update the analog input plot with acquired data."""
        from control.nidaq import AcquisitionResult
        
        self.ax_ai.clear()
        self.ax_ai.set_title("Analog Input (Acquired)")
        self.ax_ai.set_xlabel("Time (s)")
        self.ax_ai.set_ylabel("Voltage (V)")
        
        if result.timestamps is not None and len(result.analog_input) > 0:
            for channel, data in result.analog_input.items():
                ai_label = self._endpoint_labels_ai.get(channel, channel)
                self.ax_ai.plot(result.timestamps, data, label=ai_label)
            self.ax_ai.legend(loc='upper right')
            self.ax_ai.grid(True, alpha=0.3)
        
        self.fig.tight_layout()
        self.canvas.draw()
    
    def stop_tasks(self):
        """Stop all running tasks."""
        if self._ni_daq is not None:
            self._ni_daq.stop()
        
        self.trigger_btn.setEnabled(False)
        self.status_label.setText("Stopped")
    
    def closeEvent(self, event):
        """Handle widget close event."""
        if self._ni_daq is not None:
            self._ni_daq.close()
        super().closeEvent(event)


class AOWaveformDialog(QDialog):
    """Dialog for configuring analog output waveforms."""
    
    def __init__(self, sample_rate: float, num_samples: int, channels: list, parent=None):
        super().__init__(parent)
        self.sample_rate = sample_rate
        self.num_samples = num_samples
        self.channels = channels
        self._waveform = None
        self._channel = None
        self.log = squid.logging.get_logger(self.__class__.__name__)
        
        self.setWindowTitle("Configure Analog Output Waveform")
        self.setMinimumSize(400, 300)
        self.init_ui()
    
    def init_ui(self):
        layout = QVBoxLayout()
        self.setLayout(layout)
        
        # Channel selection
        channel_row = QHBoxLayout()
        channel_row.addWidget(QLabel("Channel:"))
        self.channel_combo = QComboBox()
        self.channel_combo.addItems(self.channels if self.channels else ["ao0", "ao1", "ao2", "ao3"])
        channel_row.addWidget(self.channel_combo)
        layout.addLayout(channel_row)
        
        # Waveform type
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Waveform Type:"))
        self.type_combo = QComboBox()
        self.type_combo.addItems(["Sine", "Square", "Ramp", "DC", "Staircase Ramp", "Custom"])
        self.type_combo.currentTextChanged.connect(self.on_type_changed)
        type_row.addWidget(self.type_combo)
        layout.addLayout(type_row)
        
        # Parameters group
        self.params_group = QGroupBox("Parameters")
        params_layout = QFormLayout()
        
        self.frequency_spin = QDoubleSpinBox()
        self.frequency_spin.setRange(0.001, self.sample_rate / 2)
        self.frequency_spin.setValue(100)
        self.frequency_spin.setDecimals(3)
        params_layout.addRow("Frequency (Hz):", self.frequency_spin)
        
        self.amplitude_spin = QDoubleSpinBox()
        self.amplitude_spin.setRange(0, 10)
        self.amplitude_spin.setValue(1.0)
        self.amplitude_spin.setDecimals(3)
        params_layout.addRow("Amplitude (V):", self.amplitude_spin)
        
        self.offset_spin = QDoubleSpinBox()
        self.offset_spin.setRange(-10, 10)
        self.offset_spin.setValue(0)
        self.offset_spin.setDecimals(3)
        params_layout.addRow("Offset (V):", self.offset_spin)
        
        self.phase_spin = QDoubleSpinBox()
        self.phase_spin.setRange(0, 360)
        self.phase_spin.setValue(0)
        self.phase_spin.setDecimals(1)
        params_layout.addRow("Phase (deg):", self.phase_spin)
        
        self.duty_spin = QDoubleSpinBox()
        self.duty_spin.setRange(0, 1)
        self.duty_spin.setValue(0.5)
        self.duty_spin.setDecimals(2)
        params_layout.addRow("Duty Cycle:", self.duty_spin)
        
        self.params_group.setLayout(params_layout)
        layout.addWidget(self.params_group)
        
        # Custom waveform text edit (hidden by default)
        self.custom_label = QLabel("Enter values (comma-separated or one per line):")
        self.custom_label.setVisible(False)
        layout.addWidget(self.custom_label)
        
        self.custom_edit = QTextEdit()
        self.custom_edit.setVisible(False)
        self.custom_edit.setPlaceholderText("e.g., 0, 0.5, 1.0, 0.5, 0, -0.5, -1.0, -0.5")
        layout.addWidget(self.custom_edit)
        
        # Buttons
        btn_row = QHBoxLayout()
        self.ok_btn = QPushButton("OK")
        self.ok_btn.clicked.connect(self.accept)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.ok_btn)
        btn_row.addWidget(self.cancel_btn)
        layout.addLayout(btn_row)
        
        self.on_type_changed(self.type_combo.currentText())
    
    def on_type_changed(self, waveform_type: str):
        """Update UI based on waveform type selection."""
        show_params = waveform_type not in ["Custom", "DC", "Staircase Ramp"]
        show_custom = waveform_type in ["Custom", "Staircase Ramp"]
        self.frequency_spin.setEnabled(show_params)
        self.phase_spin.setEnabled(show_params and waveform_type == "Sine")
        self.duty_spin.setEnabled(waveform_type == "Square")
        self.custom_label.setVisible(show_custom)
        self.custom_edit.setVisible(show_custom)
        if waveform_type == "Custom":
            self.custom_edit.setPlaceholderText("e.g., 0, 0.5, 1.0, 0.5, 0, -0.5, -1.0, -0.5")
        elif waveform_type == "Staircase Ramp":
            self.custom_edit.setPlaceholderText("amplitude, ramp_duration_s, delay_start_s, delay_ramp_s, n_staircase_steps")
    
    def get_waveform(self) -> tuple:
        """Generate and return the configured waveform."""
        from control.nidaq import generate_sine_wave, generate_square_wave, generate_ramp_wave, generate_staircase_ramp
        
        channel = self.channel_combo.currentText()
        waveform_type = self.type_combo.currentText()
        self.log.info(f"Waveform generation request type: {waveform_type}")
        
        freq = self.frequency_spin.value()
        amp = self.amplitude_spin.value()
        offset = self.offset_spin.value()
        phase = np.radians(self.phase_spin.value())
        duty = self.duty_spin.value()
        
        # try:
        if waveform_type == "Sine":
            waveform = generate_sine_wave(freq, amp, self.sample_rate, self.num_samples, offset, phase)
        elif waveform_type == "Square":
            waveform = generate_square_wave(freq, amp, self.sample_rate, self.num_samples, offset, duty)
        elif waveform_type == "Ramp":
            waveform = generate_ramp_wave(freq, amp, self.sample_rate, self.num_samples, offset)
        elif waveform_type == "DC":
            waveform = np.full(self.num_samples, offset)
        elif waveform_type == "Staircase Ramp":
            self.log.info(f"Generating staircase ramp")
            text = self.custom_edit.toPlainText()
            values = [float(v.strip()) for v in text.replace('\n', ',').split(',') if v.strip()]
            if len(values) != 5:
                raise ValueError("Staircase Ramp requires 5 values: amplitude, ramp_duration_s, delay_start_s, delay_ramp_s, n_staircase_steps")
            self.log.info(f"Values: {values}")
            amplitude = values[0]
            ramp_duration_s = values[1]
            delay_start_s = values[2]
            delay_ramp_s = values[3]
            n_staircase_steps = values[4]
            waveform = generate_staircase_ramp(amplitude, ramp_duration_s, delay_start_s, delay_ramp_s, n_staircase_steps, self.sample_rate, self.num_samples)
            self.log.info(f"{np.min(waveform)}, {np.max(waveform)}, {np.mean(waveform)}, {np.std(waveform)}")
        elif waveform_type == "Custom":
            text = self.custom_edit.toPlainText()
            values = [float(v.strip()) for v in text.replace('\n', ',').split(',') if v.strip()]
            if len(values) != self.num_samples:
                # Interpolate to match sample count
                x_orig = np.linspace(0, 1, len(values))
                x_new = np.linspace(0, 1, self.num_samples)
                waveform = np.interp(x_new, x_orig, values)
            else:
                waveform = np.array(values)
        else:
            waveform = np.zeros(self.num_samples)
        
        return channel, waveform
        # except Exception as e:
        #     return channel, np.zeros(self.num_samples)


class DOPatternDialog(QDialog):
    """Dialog for configuring digital output patterns."""
    
    def __init__(self, sample_rate: float, num_samples: int, lines: list, parent=None,
                 line_labels: Optional[dict] = None):
        super().__init__(parent)
        self.sample_rate = sample_rate
        self.num_samples = num_samples
        self.lines = lines if lines else list(range(8))
        self.line_labels = line_labels or {}
        self._pattern = None
        self._line = None

        self.setWindowTitle("Configure Digital Output Pattern")
        self.setMinimumSize(400, 250)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()
        self.setLayout(layout)

        # Line selection
        line_row = QHBoxLayout()
        line_row.addWidget(QLabel("Line:"))
        self.line_combo = QComboBox()
        for line in self.lines:
            label = self.line_labels.get(line)
            text = f"Line {line} — {label}" if label else f"Line {line}"
            self.line_combo.addItem(text, line)
        line_row.addWidget(self.line_combo)
        layout.addLayout(line_row)
        
        # Pattern type
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Pattern Type:"))
        self.type_combo = QComboBox()
        self.type_combo.addItems(["Pulse Train", "Single Pulse", "Always High", "Always Low"])
        self.type_combo.currentTextChanged.connect(self.on_type_changed)
        type_row.addWidget(self.type_combo)
        layout.addLayout(type_row)
        
        # Parameters group
        self.params_group = QGroupBox("Parameters")
        params_layout = QFormLayout()
        
        self.period_spin = QDoubleSpinBox()
        self.period_spin.setRange(0.000001, self.num_samples / self.sample_rate)
        self.period_spin.setValue(0.001)
        self.period_spin.setDecimals(6)
        self.period_spin.setSuffix(" s")
        params_layout.addRow("Period:", self.period_spin)
        
        self.pulse_width_spin = QDoubleSpinBox()
        self.pulse_width_spin.setRange(0.000001, self.num_samples / self.sample_rate)
        self.pulse_width_spin.setValue(0.0005)
        self.pulse_width_spin.setDecimals(6)
        self.pulse_width_spin.setSuffix(" s")
        params_layout.addRow("Pulse Width:", self.pulse_width_spin)
        
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(0, self.num_samples / self.sample_rate)
        self.delay_spin.setValue(0)
        self.delay_spin.setDecimals(6)
        self.delay_spin.setSuffix(" s")
        params_layout.addRow("Initial Delay:", self.delay_spin)
        
        self.inverted_checkbox = QCheckBox()
        params_layout.addRow("Inverted:", self.inverted_checkbox)
        
        self.params_group.setLayout(params_layout)
        layout.addWidget(self.params_group)
        
        # Buttons
        btn_row = QHBoxLayout()
        self.ok_btn = QPushButton("OK")
        self.ok_btn.clicked.connect(self.accept)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.ok_btn)
        btn_row.addWidget(self.cancel_btn)
        layout.addLayout(btn_row)
        
        self.on_type_changed(self.type_combo.currentText())
    
    def on_type_changed(self, pattern_type: str):
        """Update UI based on pattern type selection."""
        show_params = pattern_type in ["Pulse Train", "Single Pulse"]
        self.period_spin.setEnabled(pattern_type == "Pulse Train")
        self.pulse_width_spin.setEnabled(show_params)
        self.delay_spin.setEnabled(show_params)
    
    def get_pattern(self) -> tuple:
        """Generate and return the configured pattern."""
        from control.nidaq import generate_pulse_train
        
        line = self.line_combo.currentData()
        pattern_type = self.type_combo.currentText()
        inverted = self.inverted_checkbox.isChecked()
        
        try:
            if pattern_type == "Pulse Train":
                period_samples = int(self.period_spin.value() * self.sample_rate)
                width_samples = int(self.pulse_width_spin.value() * self.sample_rate)
                delay_samples = int(self.delay_spin.value() * self.sample_rate)
                
                pattern = generate_pulse_train(width_samples, period_samples, self.num_samples, inverted)
                # Apply delay by rolling
                if delay_samples > 0:
                    pattern = np.roll(pattern, delay_samples)
                    pattern[:delay_samples] = inverted  # Fill delay with inverted state
                    
            elif pattern_type == "Single Pulse":
                width_samples = int(self.pulse_width_spin.value() * self.sample_rate)
                delay_samples = int(self.delay_spin.value() * self.sample_rate)
                
                pattern = np.zeros(self.num_samples, dtype=bool)
                if not inverted:
                    start = delay_samples
                    end = min(start + width_samples, self.num_samples)
                    pattern[start:end] = True
                else:
                    pattern[:] = True
                    start = delay_samples
                    end = min(start + width_samples, self.num_samples)
                    pattern[start:end] = False
                    
            elif pattern_type == "Always High":
                pattern = np.ones(self.num_samples, dtype=bool)
                if inverted:
                    pattern = ~pattern
                    
            elif pattern_type == "Always Low":
                pattern = np.zeros(self.num_samples, dtype=bool)
                if inverted:
                    pattern = ~pattern
            else:
                pattern = np.zeros(self.num_samples, dtype=bool)
            
            return line, pattern
        except Exception as e:
            return line, np.zeros(self.num_samples, dtype=bool)


@dataclass
class CameraState:
    """Stores camera state for restoration after fast acquisition."""
    acquisition_mode: CameraAcquisitionMode
    exposure_time_ms: float
    pixel_format: CameraPixelFormat
    binning_x: int
    binning_y: int
    roi_offset_x: int
    roi_offset_y: int
    roi_width: int
    roi_height: int
    camera_live: bool


class FastAcquisitionWidget(QWidget):
    """
    Widget for controlling fast acquisition mode.
    
    Features:
    - Trigger source selection (TI Microcontroller / NI DAQ)
    - Frame rate and exposure time settings
    - Buffer size configuration
    - File format selection (TIFF / Zarr / HDF5 / Raw)
    - Output directory selection
    - Start/Stop acquisition controls
    - Real-time statistics (FPS, buffer fill, write speed)
    - DAQ channel configuration
    """
    
    signal_acquisition_started = Signal()
    signal_acquisition_finished = Signal()
    
    def __init__(self, microscope, ni_daq_widget: Optional['NIDAQWidget'] = None, 
                 live_controller: Optional[LiveController] = None,
                 live_control_widget: Optional['LiveControlWidget'] = None,
                 parent=None):
        super().__init__(parent)
        self._log = squid.logging.get_logger(self.__class__.__name__)
        
        self.microscope = microscope
        self.camera = microscope.camera
        self.microcontroller = microscope.low_level_drivers.microcontroller
        self.ni_daq_widget = ni_daq_widget
        self.live_controller = live_controller
        self.live_control_widget = live_control_widget
        
        # Controller instance (created when starting acquisition)
        self._controller: Optional[FastAcquisitionController] = None
        
        # State
        self._is_acquiring = False
        self._updating_acquisition_params = False  # Flag to prevent circular updates
        self._camera_state_before_acquisition: Optional[CameraState] = None  # Store camera state before fast acquisition
        self._was_live_before_fast_acquisition: bool = False  # Track live state to restore after acquisition
        
        # Initialize UI
        self.init_ui()
        
        # Connect signals to update NIDAQWidget when parameters change
        self._connect_ni_daq_signals()
        
        # Statistics update timer
        self._stats_timer = QTimer()
        self._stats_timer.timeout.connect(self.update_statistics)
        self._stats_timer.setInterval(500)  # Update every 500ms
    
    def init_ui(self):
        """Initialize UI components."""
        _compact = 4  # margins/spacing for denser layout (matches multipoint-style tabs)

        # --- Output first (same order as other acquisition widgets) ---
        output_group = QGroupBox("Output")
        output_layout = QGridLayout()
        output_layout.setHorizontalSpacing(8)
        output_layout.setVerticalSpacing(_compact)

        self.lineEdit_savingDir = QLineEdit()
        self.lineEdit_savingDir.setReadOnly(True)
        from control._def import DEFAULT_SAVING_PATH

        self.lineEdit_savingDir.setText(DEFAULT_SAVING_PATH)
        self.output_path = DEFAULT_SAVING_PATH
        self.base_path_is_set = True

        self.btn_setSavingDir = QPushButton("Browse")
        self.btn_setSavingDir.setDefault(False)
        try:
            self.btn_setSavingDir.setIcon(QIcon("icon/folder.png"))
        except Exception:
            pass
        self.btn_setSavingDir.clicked.connect(self.set_saving_dir)

        self.lineEdit_experimentID = QLineEdit()

        output_layout.addWidget(QLabel("Saving Path:"), 0, 0)
        output_layout.addWidget(self.lineEdit_savingDir, 0, 1)
        output_layout.addWidget(self.btn_setSavingDir, 0, 2)
        output_layout.addWidget(QLabel("Experiment ID:"), 1, 0)
        output_layout.addWidget(self.lineEdit_experimentID, 1, 1, 1, 2)
        output_layout.setColumnStretch(1, 1)
        output_layout.setContentsMargins(_compact, _compact, _compact, _compact)
        output_group.setLayout(output_layout)

        # --- Acquisition + buffer in one compact 3×4 grid ---
        acq_group = QGroupBox("Acquisition Parameters")
        acq_layout = QGridLayout()
        acq_layout.setHorizontalSpacing(10)
        acq_layout.setVerticalSpacing(_compact)

        acq_layout.addWidget(QLabel("Frame Rate (Hz):"), 0, 0)
        self.frame_rate_spinbox = QDoubleSpinBox()
        self.frame_rate_spinbox.setRange(0.1, 1000.0)
        self.frame_rate_spinbox.setValue(10.0)
        self.frame_rate_spinbox.setDecimals(2)
        self.frame_rate_spinbox.valueChanged.connect(self._update_max_exposure_time)
        self.frame_rate_spinbox.valueChanged.connect(self._update_acquisition_time_from_frames)
        acq_layout.addWidget(self.frame_rate_spinbox, 0, 1)

        acq_layout.addWidget(QLabel("Exposure Time (ms):"), 0, 2)
        self.exposure_time_spinbox = QDoubleSpinBox()
        self.exposure_time_spinbox.setRange(0.1, 10000.0)
        self.exposure_time_spinbox.setValue(20.0)
        self.exposure_time_spinbox.setDecimals(2)
        acq_layout.addWidget(self.exposure_time_spinbox, 0, 3)

        self._update_max_exposure_time()

        acq_layout.addWidget(QLabel("Number of Frames:"), 1, 0)
        self.num_frames_spinbox = QSpinBox()
        self.num_frames_spinbox.setRange(0, 1000000)
        self.num_frames_spinbox.setValue(100)
        self.num_frames_spinbox.setSpecialValueText("Continuous")
        self.num_frames_spinbox.valueChanged.connect(self._update_acquisition_time_from_frames)
        acq_layout.addWidget(self.num_frames_spinbox, 1, 1)

        acq_layout.addWidget(QLabel("Total Acquisition Time (s):"), 1, 2)
        self.total_time_spinbox = QDoubleSpinBox()
        self.total_time_spinbox.setRange(0.001, 3600.0)
        self.total_time_spinbox.setValue(10.0)
        self.total_time_spinbox.setDecimals(3)
        self.total_time_spinbox.setSuffix(" s")
        self.total_time_spinbox.valueChanged.connect(self._update_frames_from_acquisition_time)
        acq_layout.addWidget(self.total_time_spinbox, 1, 3)

        acq_layout.addWidget(QLabel("Buffer Size:"), 2, 0)
        self.buffer_size_spinbox = QSpinBox()
        self.buffer_size_spinbox.setRange(10, 10000)
        self.buffer_size_spinbox.setValue(500)
        acq_layout.addWidget(self.buffer_size_spinbox, 2, 1)

        acq_layout.addWidget(QLabel("File Format:"), 2, 2)
        self.file_format_combo = QComboBox()
        self.file_format_combo.addItems(["TIFF", "Zarr", "HDF5", "Raw"])
        acq_layout.addWidget(self.file_format_combo, 2, 3)

        acq_layout.setContentsMargins(_compact, _compact, _compact, _compact)
        acq_group.setLayout(acq_layout)

        self._update_acquisition_time_from_frames()

        # --- DAQ configuration (2×2 grid, unchanged logic) ---
        daq_group = QGroupBox("DAQ Configuration")
        daq_layout = QGridLayout()
        daq_layout.setHorizontalSpacing(10)
        daq_layout.setVerticalSpacing(_compact)

        daq_layout.addWidget(QLabel("Trigger Mode:"), 0, 0)
        self.trigger_mode_combo = QComboBox()
        self.trigger_mode_combo.addItems(["Frame Start", "Acquisition Start"])
        self.trigger_mode_combo.setToolTip(
            "Frame Start: Trigger each frame individually\n"
            "Acquisition Start: Single trigger to start continuous acquisition"
        )
        daq_layout.addWidget(self.trigger_mode_combo, 0, 1)

        daq_layout.addWidget(QLabel("Trigger DIO Line:"), 0, 2)
        self.camera_trigger_dio_line_spinbox = QSpinBox()
        self.camera_trigger_dio_line_spinbox.setRange(0, 31)
        self.camera_trigger_dio_line_spinbox.setValue(
            self.ni_daq_widget._get_dio_line_from_config("main_camera.trigger", 12)
        )
        self.camera_trigger_dio_line_spinbox.setToolTip(
            "NI DAQ digital output line for camera triggers "
            "(default read from machine config main_camera.io.trigger)"
        )
        daq_layout.addWidget(self.camera_trigger_dio_line_spinbox, 0, 3)

        daq_layout.addWidget(QLabel("Camera Frame DIO Line:"), 1, 0)
        self.camera_dio_line_spinbox = QSpinBox()
        self.camera_dio_line_spinbox.setRange(0, 31)
        self.camera_dio_line_spinbox.setValue(
            self.ni_daq_widget._get_dio_line_from_config("main_camera.frame_readout", 7)
        )
        self.camera_dio_line_spinbox.setToolTip(
            "NI DAQ digital input line connected to camera frame signal "
            "(default read from machine config main_camera.io.frame_readout)"
        )
        daq_layout.addWidget(self.camera_dio_line_spinbox, 1, 1)

        daq_layout.addWidget(QLabel("DAQ Sample Rate (Hz):"), 1, 2)
        self.daq_sample_rate_spinbox = QDoubleSpinBox()
        self.daq_sample_rate_spinbox.setRange(100.0, 1000000.0)
        self.daq_sample_rate_spinbox.setValue(10000.0)
        self.daq_sample_rate_spinbox.setToolTip("Sample rate for NI DAQ waveforms")
        daq_layout.addWidget(self.daq_sample_rate_spinbox, 1, 3)

        daq_layout.setContentsMargins(_compact, _compact, _compact, _compact)
        daq_group.setLayout(daq_layout)

        # --- Control buttons ---
        control_layout = QHBoxLayout()
        control_layout.setSpacing(_compact)
        self.start_button = QPushButton("Start Acquisition")
        self.start_button.clicked.connect(self.start_acquisition)
        control_layout.addWidget(self.start_button)

        self.stop_button = QPushButton("Stop Acquisition")
        self.stop_button.clicked.connect(self.stop_acquisition)
        self.stop_button.setEnabled(False)
        control_layout.addWidget(self.stop_button)

        # --- Statistics: single row ---
        stats_group = QGroupBox("Statistics")
        stats_layout = QHBoxLayout()
        stats_layout.setSpacing(8)
        stats_layout.addWidget(QLabel("Buffer Fill:"))
        self.buffer_progress_bar = QProgressBar()
        self.buffer_progress_bar.setRange(0, 100)
        self.buffer_progress_bar.setMinimumHeight(18)
        stats_layout.addWidget(self.buffer_progress_bar, stretch=1)
        self.stats_label = QLabel("Not acquiring")
        self.stats_label.setMinimumWidth(120)
        stats_layout.addWidget(self.stats_label)
        stats_layout.setContentsMargins(_compact, _compact, _compact, _compact)
        stats_group.setLayout(stats_layout)

        main_layout = QVBoxLayout()
        self.setLayout(main_layout)
        main_layout.setSpacing(_compact)
        main_layout.setContentsMargins(_compact, _compact, _compact, _compact)
        main_layout.addWidget(output_group)
        main_layout.addWidget(acq_group)
        main_layout.addWidget(daq_group)
        main_layout.addLayout(control_layout)
        main_layout.addWidget(stats_group)
        main_layout.addStretch()
    
    
    def _connect_ni_daq_signals(self):
        """Connect signals to update NIDAQWidget when acquisition parameters change."""
        if self.ni_daq_widget is None:
            return
        
        # Connect DAQ sample rate changes
        self.daq_sample_rate_spinbox.valueChanged.connect(self._on_daq_sample_rate_changed)
        
        # Note: Total acquisition time changes are already handled in 
        # _update_acquisition_time_from_frames and _update_frames_from_acquisition_time
    
    def _on_daq_sample_rate_changed(self, new_sample_rate_hz: float):
        """
        Handle DAQ sample rate changes from FastAcquisitionWidget.
        Updates NIDAQWidget waveforms by resampling.
        """
        if self._updating_acquisition_params or self._is_acquiring:
            return
        
        if self.ni_daq_widget is None or not self.ni_daq_widget.is_linked_to_fast_acquisition():
            return
        
        try:
            # Get current total acquisition time
            total_time_s = self.total_time_spinbox.value()
            
            # Update waveforms in NI DAQ widget by resampling
            self.ni_daq_widget.update_waveforms_for_sample_rate(new_sample_rate_hz, total_time_s)
            
        except Exception as e:
            self._log.warning(f"Failed to update NI DAQ waveforms for sample rate change: {e}")
    
    def set_saving_dir(self):
        """Set saving directory (matching RecordingWidget style)."""
        dialog = QFileDialog()
        save_dir_base = dialog.getExistingDirectory(None, "Select Folder", self.lineEdit_savingDir.text())
        if save_dir_base is None or save_dir_base == "":
            self.base_path_is_set = True
            return
        self.lineEdit_savingDir.setText(save_dir_base)
        self.output_path = save_dir_base
        self.base_path_is_set = True
    
    def _update_max_exposure_time(self):
        """
        Update maximum exposure time based on frame rate and camera readout time.
        
        Maximum exposure time = (1 / frame_rate) - readout_time
        This ensures exposure + readout fits within one frame period.
        
        If readout time is per-row, it's multiplied by the ROI height.
        """
        try:
            # readout_time_ms = self.camera.get_readout_time()
            readout_time_ms = 0.04 # TBD: get from camera
            readout_time_us = 40.0 # TBD: get from camera
            frame_rate_hz = self.frame_rate_spinbox.value()
            
            # Calculate frame period in milliseconds
            frame_period_ms = (1.0 / frame_rate_hz) * 1000.0
            
            # Maximum exposure time = frame period - readout time
            # Add a small safety margin (1% of frame period) to account for timing variations
            max_exposure_time_ms = max(frame_period_ms, readout_time_ms)-0.05

            self._log.info(f"Current frame rate: {frame_rate_hz} Hz, frame period: {frame_period_ms:.2f} ms, readout time: {readout_time_ms:.2f} ms, max exposure time: {max_exposure_time_ms:.2f} ms")
            
            # Ensure minimum value
            min_exposure_ms = 0.1
            if max_exposure_time_ms < min_exposure_ms:
                max_exposure_time_ms = min_exposure_ms
                self._log.warning(
                    f"Frame rate {frame_rate_hz} Hz is too high for readout time {readout_time_us:.2f} us. "
                    f"Maximum exposure time limited to {min_exposure_ms} ms."
                )
            
            # Update the maximum value
            self.exposure_time_spinbox.setMaximum(max_exposure_time_ms)
            
            # If current exposure time exceeds new maximum, clamp it
            current_exposure = self.exposure_time_spinbox.value()
            if current_exposure > max_exposure_time_ms:
                self.exposure_time_spinbox.setValue(max_exposure_time_ms)
                self._log.info(
                    f"Exposure time clamped to {max_exposure_time_ms:.2f} ms "
                    f"(max for {frame_rate_hz} Hz frame rate with {readout_time_us:.2f} us readout)"
                )
            
            self._log.debug(
                f"Updated max exposure time: {max_exposure_time_ms:.2f} ms "
                f"(frame rate: {frame_rate_hz} Hz, readout: {readout_time_us:.2f} us)"
            )
            
        except Exception as e:
            self._log.warning(f"Failed to update max exposure time: {e}")
            # Fallback: keep original maximum
            self.exposure_time_spinbox.setMaximum(10000.0)
    
    def _update_acquisition_time_from_frames(self):
        """
        Update total acquisition time based on number of frames and frame rate.
        Called when number of frames or frame rate changes.
        """
        if self._updating_acquisition_params:
            return
        
        try:
            self._updating_acquisition_params = True
            
            num_frames = self.num_frames_spinbox.value()
            frame_rate_hz = self.frame_rate_spinbox.value()
            
            if num_frames == 0:
                # Continuous mode - don't update total time
                # User can still set time manually if needed
                return
            
            # Calculate total time: frames / frame_rate
            total_time_s = num_frames / frame_rate_hz
            self.total_time_spinbox.setValue(total_time_s)
            
            # Update waveforms in NI DAQ widget if available and linking is enabled
            if (self.ni_daq_widget and not self._is_acquiring
                    and self.ni_daq_widget.is_linked_to_fast_acquisition()):
                try:
                    sample_rate_hz = self.daq_sample_rate_spinbox.value()
                    self.ni_daq_widget.update_waveforms_for_duration(total_time_s, sample_rate_hz)
                except Exception as e:
                    self._log.warning(f"Failed to update NI DAQ waveforms: {e}")
            
        except Exception as e:
            self._log.warning(f"Failed to update acquisition time from frames: {e}")
        finally:
            self._updating_acquisition_params = False
    
    def _update_frames_from_acquisition_time(self):
        """
        Update number of frames based on total acquisition time and frame rate.
        Called when total acquisition time changes.
        """
        if self._updating_acquisition_params:
            return
        
        try:
            self._updating_acquisition_params = True
            
            total_time_s = self.total_time_spinbox.value()
            frame_rate_hz = self.frame_rate_spinbox.value()
            
            # Calculate number of frames: time * frame_rate
            num_frames = int(total_time_s * frame_rate_hz)
            
            # Ensure minimum of 1 frame (0 is reserved for "Continuous")
            if num_frames < 1:
                num_frames = 1
            
            # Update the spinbox
            self.num_frames_spinbox.setValue(num_frames)
            
            # Update waveforms in NI DAQ widget if available and linking is enabled
            if (self.ni_daq_widget and not self._is_acquiring
                    and self.ni_daq_widget.is_linked_to_fast_acquisition()):
                try:
                    sample_rate_hz = self.daq_sample_rate_spinbox.value()
                    self.ni_daq_widget.update_waveforms_for_duration(total_time_s, sample_rate_hz)
                except Exception as e:
                    self._log.warning(f"Failed to update NI DAQ waveforms: {e}")
            
        except Exception as e:
            self._log.warning(f"Failed to update frames from acquisition time: {e}")
        finally:
            self._updating_acquisition_params = False

    def emit_selected_channels(self):
        # TBD: implement this
        pass
    
    def start_acquisition(self, camera_offset_ms: float = 0):
        """Start fast acquisition."""
        if self._is_acquiring:
            self._log.warning("Acquisition already running")
            return
        
        # Store camera state before fast acquisition
        try:
            acquisition_mode = self.camera.get_acquisition_mode()
            live_exposure_time_ms = self.camera.get_exposure_time()
            pixel_format = self.camera.get_pixel_format()
            binning_x, binning_y = self.camera.get_binning()
            roi_offset_x, roi_offset_y, roi_width, roi_height = self.camera.get_region_of_interest()
            camera_live = self.camera.get_is_streaming()
            self._camera_state_before_acquisition = CameraState(
                camera_live=camera_live,
                acquisition_mode=acquisition_mode,
                exposure_time_ms=live_exposure_time_ms,
                pixel_format=pixel_format,
                binning_x=binning_x,
                binning_y=binning_y,
                roi_offset_x=roi_offset_x,
                roi_offset_y=roi_offset_y,
                roi_width=roi_width,
                roi_height=roi_height
            )
        except Exception as e:
            self._log.warning(f"Could not store camera state before fast acquisition: {e}", exc_info=True)
            self._camera_state_before_acquisition = None
        
        # Stop live view if running (and remember we did so)
        if self.live_controller:
            self._was_live_before_fast_acquisition = self.live_controller.is_live
        else:
            self._was_live_before_fast_acquisition = False

        if self.live_controller and self.live_controller.is_live:
            self._log.info("Stopping live view for fast acquisition")
            self.live_controller.stop_live()
            # Update live control widget button state
            if self.live_control_widget:
                self.live_control_widget.btn_live.setChecked(False)
                self.live_control_widget.btn_live.setText("Start Live")
        
        # Validate output directory
        if not self.base_path_is_set or not self.output_path:
            error_dialog("Please choose base saving directory first", "Configuration Error")
            return
        
        # Create full output path with experiment ID
        experiment_id = self.lineEdit_experimentID.text().strip()
        if experiment_id:
            # Prepend timestamp to experiment ID: YYYY-MM-DD_HH-MM-SS_experiment_id
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            experiment_id_with_timestamp = f"{timestamp}_{experiment_id}"
            full_output_path = os.path.join(self.output_path, experiment_id_with_timestamp)
        else:
            # Use timestamp if no experiment ID
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            full_output_path = os.path.join(self.output_path, f"{timestamp}_fast_acquisition")
        
        # Create directory if it doesn't exist
        os.makedirs(full_output_path, exist_ok=True)
        
        # Validate NI DAQ availability
        if not self.ni_daq_widget or not self.ni_daq_widget._ni_daq:
            error_dialog("NI DAQ not available. Please configure NI DAQ first.", "Configuration Error")
            return
        
        # Get trigger and camera frame lines
        camera_trigger_dio_line = self.camera_trigger_dio_line_spinbox.value()
        frame_counter_dio_line = self.camera_dio_line_spinbox.value()
        
        # Check for line conflicts - this must be done FIRST before any other changes
        ni_daq_widget = self.ni_daq_widget
        conflicts = []
        if ni_daq_widget.is_line_configured(camera_trigger_dio_line, is_digital=True):
            conflicts.append(f"Digital output line {camera_trigger_dio_line} (trigger)")
        if ni_daq_widget.is_line_configured(frame_counter_dio_line, is_digital=True):
            conflicts.append(f"Digital input line {frame_counter_dio_line} (camera frame counter)")
        
        if conflicts:
            conflict_msg = (
                f"The following lines are already configured in the NI DAQ tab:\n\n"
                + "\n".join(f"  - {c}" for c in conflicts)
                + "\n\n"
                f"Fast acquisition will overwrite these configurations.\n"
                f"Continue anyway?"
            )
            reply = QMessageBox.warning(
                self,
                "Line Configuration Conflict",
                conflict_msg,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply == QMessageBox.No:
                return
        
        try:
            ni_daq = self.ni_daq_widget._ni_daq
            
            # Get configuration and waveforms from NI DAQ widget
            ni_daq_config = ni_daq_widget.get_config()
            ni_daq_waveforms = ni_daq_widget.get_waveforms()
            
            # Get analog input/output channels for this acquisition.
            # Prefer task-IO subsets from NI DAQ if available, otherwise fall back
            # to the full channel collections on the config.
            task_io = ni_daq.get_task_io()
            ai_channels = task_io.get("ai_channels")
            ao_channels = task_io.get("ao_channels")
            self._log.info(f"AO channels (task IO): {ao_channels}")
            self._log.info(f"AI channels (task IO): {ai_channels}")
            
            # Calculate acquisition duration
            num_frames = self.num_frames_spinbox.value() if self.num_frames_spinbox.value() > 0 else None
            frame_rate_hz = self.frame_rate_spinbox.value()
            sample_rate_hz = self.daq_sample_rate_spinbox.value()
            fast_exposure_time_ms = self.exposure_time_spinbox.value()
            
            if num_frames is None:
                duration_s = 1  # Continuous mode
            else:
                duration_s = num_frames / frame_rate_hz
            # Ensure that last frame is captured and its output trigger is recorded
            n_exposures_effective = np.floor((duration_s*1000-camera_offset_ms)/fast_exposure_time_ms) + 1
            duration_s = (n_exposures_effective * fast_exposure_time_ms + camera_offset_ms + 1) / 1000.0 + self.camera._trigger_duration_us *1e-6*1.5
            # Update waveforms in NI DAQ widget to match acquisition duration
            # This will crop/extend waveforms and update the display
            ni_daq_widget.update_waveforms_for_duration(duration_s, sample_rate_hz)
            
            # Get updated waveforms after duration update
            ni_daq_waveforms = ni_daq_widget.get_waveforms()
            
            # Determine acquisition mode
            trigger_mode_text = self.trigger_mode_combo.currentText()
            if trigger_mode_text == "Frame Start":
                acquisition_mode = CameraAcquisitionMode.HARDWARE_TRIGGER
            else:  # "Acquisition Start"
                acquisition_mode = CameraAcquisitionMode.HARDWARE_TRIGGER_FIRST
            
            # Create main controller
            file_format_map = {
                "TIFF": "tiff",
                "Zarr": "zarr",
                "HDF5": "hdf5",
                "Raw": "raw",
            }
            self._controller = FastAcquisitionController(
                camera=self.camera,
                ni_daq=ni_daq,
                output_path=full_output_path,
                buffer_size=self.buffer_size_spinbox.value(),
                file_format=file_format_map[self.file_format_combo.currentText()],
                camera_trigger_dio_line=self.camera_trigger_dio_line_spinbox.value(),
                frame_counter_dio_line=self.camera_dio_line_spinbox.value(),
                microscope=self.microscope,
                live_controller=self.live_controller,
            )
            
            # Set completion callback to handle acquisition completion
            from control.core.fast_acquisition_controller import AcquisitionCompletionStatus
            def on_acquisition_completed(status: AcquisitionCompletionStatus, error_message: Optional[str]):
                """Handle acquisition completion from controller."""
                # Use QTimer.singleShot to ensure this runs on the main thread
                QTimer.singleShot(0, lambda: self._on_acquisition_completed_from_controller(status, error_message))
            
            self._controller.set_completion_callback(on_acquisition_completed)
            # Start acquisition
            self._controller.start_acquisition(
                num_frames=num_frames,
                frame_rate_hz=frame_rate_hz,
                exposure_time_ms=fast_exposure_time_ms,
                sample_rate_hz=sample_rate_hz,
                ai_channels=ai_channels,
                ao_channels=ao_channels,
                acquisition_mode=acquisition_mode,
                waveforms=ni_daq_waveforms,
                camera_trigger_dio_line=camera_trigger_dio_line,
                frame_counter_dio_line=frame_counter_dio_line,
                duration_s=duration_s,
                camera_offset_ms=camera_offset_ms
            )
            
            # Update UI
            self._is_acquiring = True
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
            self._stats_timer.start()
            self.signal_acquisition_started.emit()
            
            self._log.info("Fast acquisition started")
        
        except Exception as e:
            self._log.error(f"Error starting acquisition: {e}", exc_info=True)
            error_dialog(f"Failed to start acquisition: {e}", "Error")
    
    def stop_acquisition(self):
        """Stop fast acquisition (manual stop by user)."""
        if not self._is_acquiring:
            return
        
        try:
            if self._controller:
                # Stop with manual_stop=True - completion callback will handle UI update
                self._controller.stop_acquisition(manual_stop=True)
            
            self._log.info("Fast acquisition stop requested")
        
        except Exception as e:
            self._log.error(f"Error stopping acquisition: {e}", exc_info=True)
            # Update UI on error
            self._on_acquisition_completed(success=False, error_message=str(e))
            error_dialog(f"Failed to stop acquisition: {e}", "Error")
    
    def _on_acquisition_completed_from_controller(self, status, error_message: Optional[str] = None):
        """
        Handle acquisition completion from controller callback.
        
        Args:
            status: AcquisitionCompletionStatus enum value
            error_message: Optional error message
        """
        from control.core.fast_acquisition_controller import AcquisitionCompletionStatus
        
        # Update state
        self._is_acquiring = False
        
        # Stop statistics timer
        self._stats_timer.stop()
        
        # Update button states
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        
        # Update status label based on completion status
        if status == AcquisitionCompletionStatus.COMPLETED_SUCCESS:
            completion_text = "Done: " + self.stats_label.text()
            self.stats_label.setText(completion_text)
            self.stats_label.setStyleSheet("")
        elif status == AcquisitionCompletionStatus.STOPPED_MANUAL:
            completion_text = "Stopped: " + self.stats_label.text()
            self.stats_label.setText(completion_text)
            self.stats_label.setStyleSheet("")
        elif status == AcquisitionCompletionStatus.COMPLETED_ERROR:
            error_text = "Error in acquisition"
            if error_message:
                error_text += f": {error_message}"
            self.stats_label.setText(error_text)
            self.stats_label.setStyleSheet("color: red;")
        else:
            self.stats_label.setText("Acquisition ended")
            self.stats_label.setStyleSheet("")
        
        # Reset buffer progress bar
        self.buffer_progress_bar.setValue(0)
        
        # Restore camera state to original configuration
        self._restore_camera_state()
        
        # Zero out all DAQ outputs (analog and digital) after acquisition completes
        if self.ni_daq_widget:
            try:
                self._log.info("Zeroing all DAQ outputs after acquisition completion")
                self.ni_daq_widget.zero_all_outputs()
            except Exception as e:
                self._log.error(f"Failed to zero DAQ outputs after acquisition: {e}", exc_info=True)

        # Restart live view if it was running before fast acquisition
        if self.live_controller and self._was_live_before_fast_acquisition:
            try:
                self._log.info("Restarting live view after fast acquisition")
                self.live_controller.start_live()
                if self.live_control_widget:
                    self.live_control_widget.btn_live.setChecked(True)
                    self.live_control_widget.btn_live.setText("Stop Live")
            except Exception as e:
                self._log.error(f"Failed to restart live view after fast acquisition: {e}", exc_info=True)
            finally:
                # Reset flag regardless of success
                self._was_live_before_fast_acquisition = False
        else:
            # Ensure flag does not leak into subsequent acquisitions
            self._was_live_before_fast_acquisition = False
        
        # Emit signal
        self.signal_acquisition_finished.emit()
    
    def _restore_camera_state(self):
        """
        Restore camera state to original configuration before fast acquisition.
        
        Restores:
        - Acquisition mode
        - Exposure time
        - Pixel format
        - Binning
        - ROI
        """
        if not self._camera_state_before_acquisition:
            self._log.warning("No camera state saved, cannot restore")
            return
        
        state = self._camera_state_before_acquisition
        self._log.info("Restoring camera state to original configuration")
        
        try:
            # Restore acquisition mode
            try:
                current_mode = self.camera.get_acquisition_mode()
                if current_mode != state.acquisition_mode:
                    self._log.info(f"Restoring acquisition mode from {current_mode.value} to {state.acquisition_mode.value}")
                    self.camera.set_acquisition_mode(state.acquisition_mode)
                else:
                    self._log.debug(f"Acquisition mode already correct: {state.acquisition_mode.value}")
            except Exception as e:
                self._log.error(f"Failed to restore acquisition mode: {e}", exc_info=True)
            
            # Restore exposure time
            try:
                current_exposure = self.camera.get_exposure_time()
                if abs(current_exposure - state.exposure_time_ms) > 0.01:  # Tolerance for floating point
                    self._log.info(f"Restoring exposure time from {current_exposure}ms to {state.exposure_time_ms}ms")
                    self.camera.set_exposure_time(state.exposure_time_ms)
                else:
                    self._log.debug(f"Exposure time already correct: {state.exposure_time_ms}ms")
            except Exception as e:
                self._log.error(f"Failed to restore exposure time: {e}", exc_info=True)
            
            # Restore pixel format
            try:
                current_pixel_format = self.camera.get_pixel_format()
                if current_pixel_format != state.pixel_format:
                    self._log.info(f"Restoring pixel format from {current_pixel_format.value} to {state.pixel_format.value}")
                    self.camera.set_pixel_format(state.pixel_format)
                else:
                    self._log.debug(f"Pixel format already correct: {state.pixel_format.value}")
            except Exception as e:
                self._log.error(f"Failed to restore pixel format: {e}", exc_info=True)
            
            # Restore binning
            try:
                target_bx = state.binning_x
                target_by = state.binning_y
                current_binning_x, current_binning_y = self.camera.get_binning()
                if current_binning_x != target_bx or current_binning_y != target_by:
                    self._log.info(f"Restoring binning from ({current_binning_x},{current_binning_y}) to ({target_bx},{target_by})")
                    self.camera.set_binning(target_bx, target_by)
                else:
                    self._log.debug(f"Binning already correct: ({target_bx},{target_by})")
            except Exception as e:
                self._log.error(f"Failed to restore binning: {e}", exc_info=True)
            
            # Restore ROI
            try:
                current_roi = self.camera.get_region_of_interest()
                if (current_roi[0] != state.roi_offset_x or current_roi[1] != state.roi_offset_y or
                    current_roi[2] != state.roi_width or current_roi[3] != state.roi_height):
                    self._log.info(f"Restoring ROI from ({current_roi[0]},{current_roi[1]},{current_roi[2]},{current_roi[3]}) "
                                 f"to ({state.roi_offset_x},{state.roi_offset_y},{state.roi_width},{state.roi_height})")
                    self.camera.set_region_of_interest(state.roi_offset_x, state.roi_offset_y, 
                                                      state.roi_width, state.roi_height)
                else:
                    self._log.debug(f"ROI already correct: ({state.roi_offset_x},{state.roi_offset_y},{state.roi_width},{state.roi_height})")
            except Exception as e:
                self._log.error(f"Failed to restore ROI: {e}", exc_info=True)
            
            self._log.info("Camera state restoration completed successfully")
            
        except Exception as e:
            self._log.error(f"Error during camera state restoration: {e}", exc_info=True)
    
    def _on_acquisition_completed(self, success: bool = True, error_message: Optional[str] = None):
        """
        Handle acquisition completion (legacy method for direct calls).
        
        Args:
            success: True if acquisition completed successfully, False if there was an error
            error_message: Optional error message to display
        """
        from control.core.fast_acquisition_controller import AcquisitionCompletionStatus
        
        # Convert to status enum
        if success:
            status = AcquisitionCompletionStatus.COMPLETED_SUCCESS
        else:
            status = AcquisitionCompletionStatus.COMPLETED_ERROR
        
        self._on_acquisition_completed_from_controller(status, error_message)
    
    def update_statistics(self):
        """Update real-time statistics display."""
        # Only update if acquisition is active - stop updating when acquisition ends
        if not self._is_acquiring:
            return
        
        if not self._controller:
            return
        
        # Check completion status - if not in progress, acquisition has ended
        # The callback should handle UI updates, but we check here as a safety net
        try:
            from control.core.fast_acquisition_controller import AcquisitionCompletionStatus
            status = self._controller.completion_status
            if status not in [AcquisitionCompletionStatus.IN_PROGRESS, AcquisitionCompletionStatus.NOT_STARTED]:
                # Acquisition has completed - callback should have been called, but ensure UI is updated
                if self._is_acquiring:  # Only update if we haven't already
                    error_msg = self._controller.last_completion_error
                    self._on_acquisition_completed_from_controller(status, error_msg)
                return
        except Exception:
            pass  # Ignore errors checking controller state
        
        try:
            stats = self._controller.get_statistics()
            
            stats_text = (
                f"Frames: {stats['frame_count']} | "
                f"Rate: {stats['frame_rate']:.1f} fps | "
                f"Written: {stats['frames_written']} | "
                f"Write Rate: {stats['write_rate']:.1f} fps"
            )
            self.stats_label.setText(stats_text)
            
            self.buffer_progress_bar.setValue(stats['buffer_fill_percent'])
        
        except Exception as e:
            self._log.warning(f"Error updating statistics: {e}")
            # On error, stop updating and show error message
            self._on_acquisition_completed(success=False, error_message=f"Statistics error: {e}")


