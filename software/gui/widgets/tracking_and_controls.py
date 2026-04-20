from ._bootstrap import *

class TrackingControllerWidget(QFrame):
    def __init__(
        self,
        trackingController: TrackingController,
        objectiveStore,
        show_configurations=True,
        main=None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.trackingController = trackingController
        self.objectiveStore = objectiveStore
        self.base_path_is_set = False
        self.add_components(show_configurations)
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)

        self.trackingController.microcontroller.add_joystick_button_listener(
            lambda button_pressed: self.handle_button_state(button_pressed)
        )

    def add_components(self, show_configurations):
        self.btn_setSavingDir = QPushButton("Browse")
        self.btn_setSavingDir.setDefault(False)
        self.btn_setSavingDir.setIcon(QIcon("icon/folder.png"))
        self.lineEdit_savingDir = QLineEdit()
        self.lineEdit_savingDir.setReadOnly(True)
        self.lineEdit_savingDir.setText("Choose a base saving directory")
        self.lineEdit_savingDir.setText(DEFAULT_SAVING_PATH)
        self.trackingController.set_base_path(DEFAULT_SAVING_PATH)
        self.base_path_is_set = True

        self.lineEdit_experimentID = QLineEdit()

        # self.dropdown_objective = QComboBox()
        # self.dropdown_objective.addItems(list(OBJECTIVES.keys()))
        # self.dropdown_objective.setCurrentText(DEFAULT_OBJECTIVE)
        self.objectivesWidget = ObjectivesWidget(self.objectiveStore)

        self.dropdown_tracker = QComboBox()
        self.dropdown_tracker.addItems(TRACKERS)
        self.dropdown_tracker.setCurrentText(DEFAULT_TRACKER)

        self.entry_tracking_interval = QDoubleSpinBox()
        self.entry_tracking_interval.setKeyboardTracking(False)
        self.entry_tracking_interval.setMinimum(0)
        self.entry_tracking_interval.setMaximum(30)
        self.entry_tracking_interval.setSingleStep(0.5)
        self.entry_tracking_interval.setValue(0)

        self.list_configurations = QListWidget()
        for microscope_configuration in self.trackingController.liveController.obs_controller.get_observation_states():
            self.list_configurations.addItems([microscope_configuration.name])
        self.list_configurations.setSelectionMode(
            QAbstractItemView.MultiSelection
        )  # ref: https://doc.qt.io/qt-5/qabstractitemview.html#SelectionMode-enum

        self.checkbox_withAutofocus = QCheckBox("With AF")
        self.checkbox_saveImages = QCheckBox("Save Images")
        self.btn_track = QPushButton("Start Tracking")
        self.btn_track.setCheckable(True)
        self.btn_track.setChecked(False)

        self.checkbox_enable_stage_tracking = QCheckBox(" Enable Stage Tracking")
        self.checkbox_enable_stage_tracking.setChecked(True)

        # layout
        grid_line0 = QGridLayout()
        tmp = QLabel("Saving Path")
        tmp.setFixedWidth(90)
        grid_line0.addWidget(tmp, 0, 0)
        grid_line0.addWidget(self.lineEdit_savingDir, 0, 1, 1, 2)
        grid_line0.addWidget(self.btn_setSavingDir, 0, 3)
        tmp = QLabel("Experiment ID")
        tmp.setFixedWidth(90)
        grid_line0.addWidget(tmp, 1, 0)
        grid_line0.addWidget(self.lineEdit_experimentID, 1, 1, 1, 1)
        tmp = QLabel("Objective")
        tmp.setFixedWidth(90)
        # grid_line0.addWidget(tmp,1,2)
        # grid_line0.addWidget(self.dropdown_objective, 1,3)
        grid_line0.addWidget(tmp, 1, 2)
        grid_line0.addWidget(self.objectivesWidget, 1, 3)

        grid_line3 = QHBoxLayout()
        tmp = QLabel("Configurations")
        tmp.setFixedWidth(90)
        grid_line3.addWidget(tmp)
        grid_line3.addWidget(self.list_configurations)

        grid_line1 = QHBoxLayout()
        tmp = QLabel("Tracker")
        grid_line1.addWidget(tmp)
        grid_line1.addWidget(self.dropdown_tracker)
        tmp = QLabel("Tracking Interval (s)")
        grid_line1.addWidget(tmp)
        grid_line1.addWidget(self.entry_tracking_interval)
        grid_line1.addWidget(self.checkbox_withAutofocus)
        grid_line1.addWidget(self.checkbox_saveImages)

        grid_line4 = QGridLayout()
        grid_line4.addWidget(self.btn_track, 0, 0, 1, 3)
        grid_line4.addWidget(self.checkbox_enable_stage_tracking, 0, 4)

        self.grid = QVBoxLayout()
        self.grid.addLayout(grid_line0)
        if show_configurations:
            self.grid.addLayout(grid_line3)
        else:
            self.list_configurations.setCurrentRow(0)  # select the first configuration
        self.grid.addLayout(grid_line1)
        self.grid.addLayout(grid_line4)
        self.grid.addStretch()
        self.setLayout(self.grid)

        # connections - buttons, checkboxes, entries
        self.checkbox_enable_stage_tracking.stateChanged.connect(self.trackingController.toggle_stage_tracking)
        self.checkbox_withAutofocus.stateChanged.connect(self.trackingController.toggel_enable_af)
        self.checkbox_saveImages.stateChanged.connect(self.trackingController.toggel_save_images)
        self.entry_tracking_interval.valueChanged.connect(self.trackingController.set_tracking_time_interval)
        self.btn_setSavingDir.clicked.connect(self.set_saving_dir)
        self.btn_track.clicked.connect(self.toggle_acquisition)
        # connections - selections and entries
        self.dropdown_tracker.currentIndexChanged.connect(self.update_tracker)
        # self.dropdown_objective.currentIndexChanged.connect(self.update_pixel_size)
        self.objectivesWidget.dropdown.currentIndexChanged.connect(self.update_pixel_size)
        # controller to widget
        self.trackingController.signal_tracking_stopped.connect(self.slot_tracking_stopped)

        # run initialization functions
        self.update_pixel_size()
        self.trackingController.update_image_resizing_factor(1)  # to add: image resizing slider

    # TODO(imo): This needs testing!
    def handle_button_pressed(self, button_state):
        QMetaObject.invokeMethod(self, "slot_joystick_button_pressed", Qt.AutoConnection, button_state)

    def slot_joystick_button_pressed(self, button_state):
        self.btn_track.setChecked(button_state)
        if self.btn_track.isChecked():
            if self.base_path_is_set == False:
                self.btn_track.setChecked(False)
                msg = QMessageBox()
                msg.setText("Please choose base saving directory first")
                msg.exec_()
                return
            self.setEnabled_all(False)
            self.trackingController.start_new_experiment(self.lineEdit_experimentID.text())
            self.trackingController.set_selected_configurations(
                (item.text() for item in self.list_configurations.selectedItems())
            )
            self.trackingController.start_tracking()
        else:
            self.trackingController.stop_tracking()

    def slot_tracking_stopped(self):
        self.btn_track.setChecked(False)
        self.setEnabled_all(True)
        print("tracking stopped")

    def set_saving_dir(self):
        dialog = QFileDialog()
        save_dir_base = dialog.getExistingDirectory(None, "Select Folder")
        self.trackingController.set_base_path(save_dir_base)
        self.lineEdit_savingDir.setText(save_dir_base)
        self.base_path_is_set = True

    def toggle_acquisition(self, pressed):
        if pressed:
            if self.base_path_is_set == False:
                self.btn_track.setChecked(False)
                msg = QMessageBox()
                msg.setText("Please choose base saving directory first")
                msg.exec_()
                return
            # @@@ to do: add a widgetManger to enable and disable widget
            # @@@ to do: emit signal to widgetManager to disable other widgets
            self.setEnabled_all(False)
            self.trackingController.start_new_experiment(self.lineEdit_experimentID.text())
            self.trackingController.set_selected_configurations(
                (item.text() for item in self.list_configurations.selectedItems())
            )
            self.trackingController.start_tracking()
        else:
            self.trackingController.stop_tracking()

    def setEnabled_all(self, enabled):
        self.btn_setSavingDir.setEnabled(enabled)
        self.lineEdit_savingDir.setEnabled(enabled)
        self.lineEdit_experimentID.setEnabled(enabled)
        # self.dropdown_tracker
        # self.dropdown_objective
        self.list_configurations.setEnabled(enabled)

    def update_tracker(self, index):
        self.trackingController.update_tracker_selection(self.dropdown_tracker.currentText())

    def update_pixel_size(self):
        objective = self.dropdown_objective.currentText()
        self.trackingController.objective = objective
        # self.internal_state.data['Objective'] = self.objective
        # TODO: these pixel size code needs to be updated.
        pixel_size_um = CAMERA_PIXEL_SIZE_UM[CAMERA_SENSOR] / (
            TUBE_LENS_MM / (OBJECTIVES[objective]["tube_lens_f_mm"] / OBJECTIVES[objective]["magnification"])
        )
        self.trackingController.update_pixel_size(pixel_size_um)
        print("pixel size is " + str(pixel_size_um) + " μm")

    def update_pixel_size(self):
        objective = self.objectiveStore.current_objective
        self.trackingController.objective = objective
        objective_info = self.objectiveStore.objectives_dict[objective]
        magnification = objective_info["magnification"]
        objective_tube_lens_mm = objective_info["tube_lens_f_mm"]
        tube_lens_mm = TUBE_LENS_MM
        # TODO: these pixel size code needs to be updated.
        pixel_size_um = CAMERA_PIXEL_SIZE_UM[CAMERA_SENSOR]
        pixel_size_xy = pixel_size_um / (magnification / (objective_tube_lens_mm / tube_lens_mm))
        self.trackingController.update_pixel_size(pixel_size_xy)
        print(f"pixel size is {pixel_size_xy:.2f} μm")


class TriggerControlWidget(QFrame):
    # for synchronized trigger
    signal_toggle_live = Signal(bool)
    signal_trigger_mode = Signal(str)
    signal_trigger_fps = Signal(float)

    def __init__(self, microcontroller2):
        super().__init__()
        self.fps_trigger = 10
        self.fps_display = 10
        self.microcontroller2 = microcontroller2
        self.triggerMode = TriggerMode.SOFTWARE
        self.add_components()
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)

    def add_components(self):
        # line 0: trigger mode
        self.triggerMode = None
        self.dropdown_triggerManu = QComboBox()
        self.dropdown_triggerManu.addItems([TriggerMode.SOFTWARE, TriggerMode.HARDWARE])

        # line 1: fps
        self.entry_triggerFPS = QDoubleSpinBox()
        self.entry_triggerFPS.setKeyboardTracking(False)
        self.entry_triggerFPS.setMinimum(0.02)
        self.entry_triggerFPS.setMaximum(1000)
        self.entry_triggerFPS.setSingleStep(1)
        self.entry_triggerFPS.setValue(self.fps_trigger)

        self.btn_live = QPushButton("Live")
        self.btn_live.setCheckable(True)
        self.btn_live.setChecked(False)
        self.btn_live.setDefault(False)

        # connections
        self.dropdown_triggerManu.currentIndexChanged.connect(self.update_trigger_mode)
        self.btn_live.clicked.connect(self.toggle_live)
        self.entry_triggerFPS.valueChanged.connect(self.update_trigger_fps)

        # inititialization
        self.microcontroller2.set_camera_trigger_frequency(self.fps_trigger)

        # layout
        grid_line0 = QGridLayout()
        grid_line0.addWidget(QLabel("Trigger Mode"), 0, 0)
        grid_line0.addWidget(self.dropdown_triggerManu, 0, 1)
        grid_line0.addWidget(QLabel("Trigger FPS"), 0, 2)
        grid_line0.addWidget(self.entry_triggerFPS, 0, 3)
        grid_line0.addWidget(self.btn_live, 1, 0, 1, 4)
        self.setLayout(grid_line0)

    def toggle_live(self, pressed):
        self.signal_toggle_live.emit(pressed)
        if pressed:
            self.microcontroller2.start_camera_trigger()
        else:
            self.microcontroller2.stop_camera_trigger()

    def update_trigger_mode(self):
        self.signal_trigger_mode.emit(self.dropdown_triggerManu.currentText())

    def update_trigger_fps(self, fps):
        self.fps_trigger = fps
        self.signal_trigger_fps.emit(fps)
        self.microcontroller2.set_camera_trigger_frequency(self.fps_trigger)


class MultiCameraRecordingWidget(QFrame):
    def __init__(self, streamHandler, imageSaver, channels, main=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.imageSaver = imageSaver  # for saving path control
        self.streamHandler = streamHandler
        self.channels = channels
        self.base_path_is_set = False
        self.add_components()
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)

    def add_components(self):
        self.btn_setSavingDir = QPushButton("Browse")
        self.btn_setSavingDir.setDefault(False)
        self.btn_setSavingDir.setIcon(QIcon("icon/folder.png"))

        self.lineEdit_savingDir = QLineEdit()
        self.lineEdit_savingDir.setReadOnly(True)
        self.lineEdit_savingDir.setText("Choose a base saving directory")

        self.lineEdit_experimentID = QLineEdit()

        self.entry_saveFPS = QDoubleSpinBox()
        self.entry_saveFPS.setKeyboardTracking(False)
        self.entry_saveFPS.setMinimum(0.02)
        self.entry_saveFPS.setMaximum(1000)
        self.entry_saveFPS.setSingleStep(1)
        self.entry_saveFPS.setValue(1)
        for channel in self.channels:
            self.streamHandler[channel].set_save_fps(1)

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
        grid_line3.addWidget(self.btn_record, 0, 4)

        self.grid = QGridLayout()
        self.grid.addLayout(grid_line1, 0, 0)
        self.grid.addLayout(grid_line2, 1, 0)
        self.grid.addLayout(grid_line3, 2, 0)
        self.setLayout(self.grid)

        # add and display a timer - to be implemented
        # self.timer = QTimer()

        # connections
        self.btn_setSavingDir.clicked.connect(self.set_saving_dir)
        self.btn_record.clicked.connect(self.toggle_recording)
        for channel in self.channels:
            self.entry_saveFPS.valueChanged.connect(self.streamHandler[channel].set_save_fps)
            self.entry_timeLimit.valueChanged.connect(self.imageSaver[channel].set_recording_time_limit)
            self.imageSaver[channel].stop_recording.connect(self.stop_recording)

    def set_saving_dir(self):
        dialog = QFileDialog()
        save_dir_base = dialog.getExistingDirectory(None, "Select Folder")
        for channel in self.channels:
            self.imageSaver[channel].set_base_path(save_dir_base)
        self.lineEdit_savingDir.setText(save_dir_base)
        self.save_dir_base = save_dir_base
        self.base_path_is_set = True

    def toggle_recording(self, pressed):
        if self.base_path_is_set == False:
            self.btn_record.setChecked(False)
            msg = QMessageBox()
            msg.setText("Please choose base saving directory first")
            msg.exec_()
            return
        if pressed:
            self.lineEdit_experimentID.setEnabled(False)
            self.btn_setSavingDir.setEnabled(False)
            experiment_ID = self.lineEdit_experimentID.text()
            experiment_ID = experiment_ID + "_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S.%f")
            utils.ensure_directory_exists(os.path.join(self.save_dir_base, experiment_ID))
            for channel in self.channels:
                self.imageSaver[channel].start_new_experiment(os.path.join(experiment_ID, channel), add_timestamp=False)
                self.streamHandler[channel].start_recording()
        else:
            for channel in self.channels:
                self.streamHandler[channel].stop_recording()
            self.lineEdit_experimentID.setEnabled(True)
            self.btn_setSavingDir.setEnabled(True)

    # stop_recording can be called by imageSaver
    def stop_recording(self):
        self.lineEdit_experimentID.setEnabled(True)
        self.btn_record.setChecked(False)
        for channel in self.channels:
            self.streamHandler[channel].stop_recording()
        self.btn_setSavingDir.setEnabled(True)


class WaveformDisplay(QFrame):

    def __init__(self, N=1000, include_x=True, include_y=True, main=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.N = N
        self.include_x = include_x
        self.include_y = include_y
        self.add_components()
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)

    def add_components(self):
        self.plotWidget = {}
        self.plotWidget["X"] = PlotWidget("X", N=self.N, add_legend=True)
        self.plotWidget["Y"] = PlotWidget("X", N=self.N, add_legend=True)

        layout = QGridLayout()  # layout = QStackedLayout()
        if self.include_x:
            layout.addWidget(self.plotWidget["X"], 0, 0)
        if self.include_y:
            layout.addWidget(self.plotWidget["Y"], 1, 0)
        self.setLayout(layout)

    def plot(self, time, data):
        if self.include_x:
            self.plotWidget["X"].plot(time, data[0, :], "X", color=(255, 255, 255), clear=True)
        if self.include_y:
            self.plotWidget["Y"].plot(time, data[1, :], "Y", color=(255, 255, 255), clear=True)

    def update_N(self, N):
        self.N = N
        self.plotWidget["X"].update_N(N)
        self.plotWidget["Y"].update_N(N)


class PlotWidget(pg.GraphicsLayoutWidget):

    def __init__(self, title="", N=1000, parent=None, add_legend=False):
        super().__init__(parent)
        self.plotWidget = self.addPlot(title="", axisItems={"bottom": pg.DateAxisItem()})
        if add_legend:
            self.plotWidget.addLegend()
        self.N = N

    def plot(self, x, y, label, color, clear=False):
        self.plotWidget.plot(x[-self.N :], y[-self.N :], pen=pg.mkPen(color=color, width=4), name=label, clear=clear)

    def update_N(self, N):
        self.N = N


class DisplacementMeasurementWidget(QFrame):
    def __init__(self, displacementMeasurementController, waveformDisplay, main=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.displacementMeasurementController = displacementMeasurementController
        self.waveformDisplay = waveformDisplay
        self.add_components()
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)

    def add_components(self):
        self.entry_x_offset = QDoubleSpinBox()
        self.entry_x_offset.setMinimum(0)
        self.entry_x_offset.setMaximum(3000)
        self.entry_x_offset.setSingleStep(0.2)
        self.entry_x_offset.setDecimals(3)
        self.entry_x_offset.setValue(0)
        self.entry_x_offset.setKeyboardTracking(False)

        self.entry_y_offset = QDoubleSpinBox()
        self.entry_y_offset.setMinimum(0)
        self.entry_y_offset.setMaximum(3000)
        self.entry_y_offset.setSingleStep(0.2)
        self.entry_y_offset.setDecimals(3)
        self.entry_y_offset.setValue(0)
        self.entry_y_offset.setKeyboardTracking(False)

        self.entry_x_scaling = QDoubleSpinBox()
        self.entry_x_scaling.setMinimum(-100)
        self.entry_x_scaling.setMaximum(100)
        self.entry_x_scaling.setSingleStep(0.1)
        self.entry_x_scaling.setDecimals(3)
        self.entry_x_scaling.setValue(1)
        self.entry_x_scaling.setKeyboardTracking(False)

        self.entry_y_scaling = QDoubleSpinBox()
        self.entry_y_scaling.setMinimum(-100)
        self.entry_y_scaling.setMaximum(100)
        self.entry_y_scaling.setSingleStep(0.1)
        self.entry_y_scaling.setDecimals(3)
        self.entry_y_scaling.setValue(1)
        self.entry_y_scaling.setKeyboardTracking(False)

        self.entry_N_average = QSpinBox()
        self.entry_N_average.setMinimum(1)
        self.entry_N_average.setMaximum(25)
        self.entry_N_average.setSingleStep(1)
        self.entry_N_average.setValue(1)
        self.entry_N_average.setKeyboardTracking(False)

        self.entry_N = QSpinBox()
        self.entry_N.setMinimum(1)
        self.entry_N.setMaximum(5000)
        self.entry_N.setSingleStep(1)
        self.entry_N.setValue(1000)
        self.entry_N.setKeyboardTracking(False)

        self.reading_x = QLabel()
        self.reading_x.setNum(0)
        self.reading_x.setFrameStyle(QFrame.Panel | QFrame.Sunken)

        self.reading_y = QLabel()
        self.reading_y.setNum(0)
        self.reading_y.setFrameStyle(QFrame.Panel | QFrame.Sunken)

        # layout
        grid_line0 = QGridLayout()
        grid_line0.addWidget(QLabel("x offset"), 0, 0)
        grid_line0.addWidget(self.entry_x_offset, 0, 1)
        grid_line0.addWidget(QLabel("x scaling"), 0, 2)
        grid_line0.addWidget(self.entry_x_scaling, 0, 3)
        grid_line0.addWidget(QLabel("y offset"), 0, 4)
        grid_line0.addWidget(self.entry_y_offset, 0, 5)
        grid_line0.addWidget(QLabel("y scaling"), 0, 6)
        grid_line0.addWidget(self.entry_y_scaling, 0, 7)

        grid_line1 = QGridLayout()
        grid_line1.addWidget(QLabel("d from x"), 0, 0)
        grid_line1.addWidget(self.reading_x, 0, 1)
        grid_line1.addWidget(QLabel("d from y"), 0, 2)
        grid_line1.addWidget(self.reading_y, 0, 3)
        grid_line1.addWidget(QLabel("N average"), 0, 4)
        grid_line1.addWidget(self.entry_N_average, 0, 5)
        grid_line1.addWidget(QLabel("N"), 0, 6)
        grid_line1.addWidget(self.entry_N, 0, 7)

        self.grid = QGridLayout()
        self.grid.addLayout(grid_line0, 0, 0)
        self.grid.addLayout(grid_line1, 1, 0)
        self.setLayout(self.grid)

        # connections
        self.entry_x_offset.valueChanged.connect(self.update_settings)
        self.entry_y_offset.valueChanged.connect(self.update_settings)
        self.entry_x_scaling.valueChanged.connect(self.update_settings)
        self.entry_y_scaling.valueChanged.connect(self.update_settings)
        self.entry_N_average.valueChanged.connect(self.update_settings)
        self.entry_N.valueChanged.connect(self.update_settings)
        self.entry_N.valueChanged.connect(self.update_waveformDisplay_N)

    def update_settings(self, new_value):
        print("update settings")
        self.displacementMeasurementController.update_settings(
            self.entry_x_offset.value(),
            self.entry_y_offset.value(),
            self.entry_x_scaling.value(),
            self.entry_y_scaling.value(),
            self.entry_N_average.value(),
            self.entry_N.value(),
        )

    def update_waveformDisplay_N(self, N):
        self.waveformDisplay.update_N(N)

    def display_readings(self, readings):
        self.reading_x.setText("{:.2f}".format(readings[0]))
        self.reading_y.setText("{:.2f}".format(readings[1]))


class LaserAutofocusControlWidget(QFrame):
    def __init__(self, laserAutofocusController, liveController: LiveController, main=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.laserAutofocusController = laserAutofocusController
        self.liveController: LiveController = liveController
        self.add_components()
        self.update_init_state()
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)

    def add_components(self):
        self.btn_set_reference = QPushButton(" Set Reference ")
        self.btn_set_reference.setCheckable(False)
        self.btn_set_reference.setChecked(False)
        self.btn_set_reference.setDefault(False)
        if not self.laserAutofocusController.is_initialized:
            self.btn_set_reference.setEnabled(False)

        self.label_displacement = QLabel()
        self.label_displacement.setFrameStyle(QFrame.Panel | QFrame.Sunken)

        self.btn_measure_displacement = QPushButton("Measure Displacement")
        self.btn_measure_displacement.setCheckable(False)
        self.btn_measure_displacement.setChecked(False)
        self.btn_measure_displacement.setDefault(False)
        if not self.laserAutofocusController.is_initialized:
            self.btn_measure_displacement.setEnabled(False)

        self.entry_target = QDoubleSpinBox()
        self.entry_target.setMinimum(-100)
        self.entry_target.setMaximum(100)
        self.entry_target.setSingleStep(0.01)
        self.entry_target.setDecimals(2)
        self.entry_target.setValue(0)
        self.entry_target.setKeyboardTracking(False)

        self.btn_move_to_target = QPushButton("Move to Target")
        self.btn_move_to_target.setCheckable(False)
        self.btn_move_to_target.setChecked(False)
        self.btn_move_to_target.setDefault(False)
        if not self.laserAutofocusController.is_initialized:
            self.btn_move_to_target.setEnabled(False)

        self.grid = QGridLayout()

        self.grid.addWidget(self.btn_set_reference, 0, 0, 1, 4)

        self.grid.addWidget(QLabel("Displacement (um)"), 1, 0)
        self.grid.addWidget(self.label_displacement, 1, 1)
        self.grid.addWidget(self.btn_measure_displacement, 1, 2, 1, 2)

        self.grid.addWidget(QLabel("Target (um)"), 2, 0)
        self.grid.addWidget(self.entry_target, 2, 1)
        self.grid.addWidget(self.btn_move_to_target, 2, 2, 1, 2)
        self.setLayout(self.grid)

        # make connections
        self.btn_set_reference.clicked.connect(self.on_set_reference_clicked)
        self.btn_measure_displacement.clicked.connect(self.on_measure_displacement_clicked)
        self.btn_move_to_target.clicked.connect(self.move_to_target)
        self.laserAutofocusController.signal_displacement_um.connect(self.label_displacement.setNum)

    def update_init_state(self):
        self.btn_set_reference.setEnabled(self.laserAutofocusController.is_initialized)
        self.btn_measure_displacement.setEnabled(self.laserAutofocusController.laser_af_properties.has_reference)
        self.btn_move_to_target.setEnabled(self.laserAutofocusController.laser_af_properties.has_reference)

    def move_to_target(self):
        was_live = self.liveController.is_live
        if was_live:
            self.liveController.stop_live()
        self.laserAutofocusController.move_to_target(self.entry_target.value())
        if was_live:
            self.liveController.start_live()

    def on_set_reference_clicked(self):
        """Handle set reference button click"""
        was_live = self.liveController.is_live
        if was_live:
            self.liveController.stop_live()
        success = self.laserAutofocusController.set_reference()
        if success:
            self.btn_measure_displacement.setEnabled(True)
            self.btn_move_to_target.setEnabled(True)
        if was_live:
            self.liveController.start_live()

    def on_measure_displacement_clicked(self):
        was_live = self.liveController.is_live
        if was_live:
            self.liveController.stop_live()
        result = self.laserAutofocusController.measure_displacement()
        if math.isnan(result):
            QMessageBox.warning(
                self,
                "Measurement Failed",
                "Could not measure displacement. Please ensure the reference position is set.",
            )
        if was_live:
            self.liveController.start_live()


# Keys shipped with default sample_formats.csv; user-added formats may be removed from the UI.
_BUILTIN_WELLPLATE_FORMAT_KEYS = frozenset(
    {
        "glass slide",
        "6 well plate",
        "12 well plate",
        "24 well plate",
        "96 well plate",
        "384 well plate",
        "1536 well plate",
    }
)


class WellplateFormatWidget(QWidget):

    signalWellplateSettings = Signal(str, float, float, int, int, float, float, int, int, int)

    def __init__(self, stage: AbstractStage, navigationViewer, streamHandler, liveController):
        super().__init__()
        self.stage = stage
        self.navigationViewer = navigationViewer
        self.streamHandler = streamHandler
        self.liveController = liveController
        self.wellplate_format = WELLPLATE_FORMAT
        self.yaml_path = SAMPLE_FORMATS_YAML_PATH  # 'sample_formats.yaml'
        self.initUI()

    def initUI(self):
        layout = QHBoxLayout(self)
        self.label = QLabel("Sample Format", self)
        self.comboBox = QComboBox(self)
        self.populate_combo_box()
        self.comboBox.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(self.label)
        layout.addWidget(self.comboBox)
        self.comboBox.currentIndexChanged.connect(self.wellplateChanged)
        index = self.comboBox.findData(self.wellplate_format)
        if index >= 0:
            self.comboBox.setCurrentIndex(index)

    def populate_combo_box(self):
        self.comboBox.clear()
        for format_, settings in WELLPLATE_FORMAT_SETTINGS.items():
            self.comboBox.addItem(format_, format_)

        # Add custom item and set its font to italic
        self.comboBox.addItem("calibrate format...", "custom")
        index = self.comboBox.count() - 1  # Get the index of the last item
        font = QFont()
        font.setItalic(True)
        self.comboBox.setItemData(index, font, Qt.FontRole)

    def wellplateChanged(self, index):
        self.wellplate_format = self.comboBox.itemData(index)
        if self.wellplate_format == "custom":
            calibration_dialog = WellplateCalibration(
                self, self.stage, self.navigationViewer, self.streamHandler, self.liveController
            )
            result = calibration_dialog.exec_()
            if result == QDialog.Rejected:
                # If the dialog was closed without adding a new format, revert to the previous selection
                prev_index = self.comboBox.findData(self.wellplate_format)
                self.comboBox.setCurrentIndex(prev_index)
        else:
            self.setWellplateSettings(self.wellplate_format)

    def setWellplateSettings(self, wellplate_format):
        if wellplate_format in WELLPLATE_FORMAT_SETTINGS:
            settings = WELLPLATE_FORMAT_SETTINGS[wellplate_format]
        elif wellplate_format == "glass slide":
            self.signalWellplateSettings.emit("glass slide", 0, 0, 0, 0, 0, 0, 0, 1, 1)
            return
        else:
            print(f"Wellplate format {wellplate_format} not recognized")
            return

        self.signalWellplateSettings.emit(
            wellplate_format,
            settings["a1_x_mm"],
            settings["a1_y_mm"],
            settings["a1_x_pixel"],
            settings["a1_y_pixel"],
            settings["well_size_mm"],
            settings["well_spacing_mm"],
            settings["number_of_skip"],
            settings["rows"],
            settings["cols"],
        )

    def getWellplateSettings(self, wellplate_format):
        if wellplate_format in WELLPLATE_FORMAT_SETTINGS:
            settings = WELLPLATE_FORMAT_SETTINGS[wellplate_format]
        elif wellplate_format == "glass slide":
            settings = {
                "format": "glass slide",
                "a1_x_mm": 0,
                "a1_y_mm": 0,
                "a1_x_pixel": 0,
                "a1_y_pixel": 0,
                "well_size_mm": 0,
                "well_spacing_mm": 0,
                "number_of_skip": 0,
                "rows": 1,
                "cols": 1,
            }
        else:
            return None
        return settings

    def add_custom_format(self, name, settings):
        WELLPLATE_FORMAT_SETTINGS[name] = settings
        self.populate_combo_box()
        index = self.comboBox.findData(name)
        if index >= 0:
            self.comboBox.setCurrentIndex(index)
        self.wellplateChanged(index)

    @staticmethod
    def is_builtin_format(format_id):
        return format_id in _BUILTIN_WELLPLATE_FORMAT_KEYS

    def remove_format(self, format_id):
        """Remove a non-built-in format, update cache CSV, refresh combo, and fix current selection."""
        if format_id not in WELLPLATE_FORMAT_SETTINGS or self.is_builtin_format(format_id):
            return False

        del WELLPLATE_FORMAT_SETTINGS[format_id]
        image_basename = f"{str(format_id).replace(' ', '_')}.png"
        cache_image_path = os.path.join("cache", "plate_images", image_basename)
        if os.path.isfile(cache_image_path):
            try:
                os.remove(cache_image_path)
            except OSError:
                pass

        was_current = self.wellplate_format == format_id
        self.save_formats_to_yaml()
        self.populate_combo_box()

        if was_current:
            for i in range(self.comboBox.count()):
                fid = self.comboBox.itemData(i)
                if fid != "custom":
                    self.comboBox.blockSignals(True)
                    self.comboBox.setCurrentIndex(i)
                    self.comboBox.blockSignals(False)
                    self.wellplate_format = fid
                    self.setWellplateSettings(fid)
                    break
        else:
            idx = self.comboBox.findData(self.wellplate_format)
            if idx >= 0:
                self.comboBox.blockSignals(True)
                self.comboBox.setCurrentIndex(idx)
                self.comboBox.blockSignals(False)

        return True

    def save_formats_to_yaml(self):
        cache_path = os.path.join("cache", self.yaml_path)
        os.makedirs("cache", exist_ok=True)

        formats_list = []
        for format_id, s in WELLPLATE_FORMAT_SETTINGS.items():
            shape = s.get("well_shape", "circle")
            well_block = {"shape": shape}
            if shape == "circle":
                well_block["diameter_mm"] = float(s.get("well_diameter_mm", s.get("well_size_mm", 0.0)))
            else:
                well_block["width_mm"] = float(s.get("well_width_mm", s.get("well_size_mm", 0.0)))
                well_block["height_mm"] = float(s.get("well_height_mm", s.get("well_size_mm", 0.0)))
                if s.get("well_corner_radius_mm"):
                    well_block["corner_radius_mm"] = float(s["well_corner_radius_mm"])

            formats_list.append({
                "id": format_id,
                "display_name": s.get("display_name", format_id),
                "plate_dimensions_mm": [float(s["plate_dimensions_mm"][0]),
                                        float(s["plate_dimensions_mm"][1])],
                "plate_corner_radius_mm": float(s.get("plate_corner_radius_mm", 0.0)),
                "a1_chamfer": bool(s.get("a1_chamfer", False)),
                "a1_offset_mm": [float(s["a1_offset_mm"][0]), float(s["a1_offset_mm"][1])],
                "grid": {
                    "rows": int(s["rows"]),
                    "cols": int(s["cols"]),
                    "row_spacing_mm": float(s.get("row_spacing_mm", s.get("well_spacing_mm", 0.0))),
                    "col_spacing_mm": float(s.get("col_spacing_mm", s.get("well_spacing_mm", 0.0))),
                },
                "well": well_block,
                "number_of_skip": int(s.get("number_of_skip", 0)),
            })

        with open(cache_path, "w") as f:
            yaml.safe_dump({"formats": formats_list}, f, sort_keys=False)


class WellplateCalibration(QDialog):

    def __init__(self, wellplateFormatWidget, stage: AbstractStage, navigationViewer, streamHandler, liveController):
        super().__init__()
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self.setWindowTitle("Well Plate Calibration")
        self.wellplateFormatWidget = wellplateFormatWidget
        self.stage = stage
        self.navigationViewer = navigationViewer
        self.streamHandler = streamHandler
        self.liveController: LiveController = liveController
        self.was_live = self.liveController.is_live
        self.corners = [None, None, None]
        self.center_point = None  # For center point calibration method
        self.show_virtual_joystick = True  # FLAG
        self.initUI()
        # Initially allow click-to-move and hide the joystick controls
        self.clickToMoveCheckbox.setChecked(True)
        self.toggleVirtualJoystick(False)
        # Set minimum height to accommodate all UI configurations
        self.setMinimumHeight(580)

    def initUI(self):
        layout = QHBoxLayout(self)  # Change to QHBoxLayout to have two columns

        # Left column for existing controls
        left_layout = QVBoxLayout()

        # Add radio buttons for selecting mode
        self.mode_group = QButtonGroup(self)
        self.new_format_radio = QRadioButton("Add New Format")
        self.calibrate_format_radio = QRadioButton("Calibrate Existing Format")
        self.mode_group.addButton(self.new_format_radio)
        self.mode_group.addButton(self.calibrate_format_radio)
        self.new_format_radio.setChecked(True)

        left_layout.addWidget(self.new_format_radio)
        left_layout.addWidget(self.calibrate_format_radio)

        self.delete_format_button = QPushButton("Delete Format")
        self.delete_format_button.clicked.connect(self.delete_selected_format)

        # Existing format selection (initially hidden)
        self.existing_format_combo = QComboBox(self)
        self.populate_existing_formats()
        self.existing_format_combo.hide()
        self.existing_format_combo.currentIndexChanged.connect(self.on_existing_format_changed)
        left_layout.addWidget(self.existing_format_combo)

        # Connect radio buttons to toggle visibility
        self.new_format_radio.toggled.connect(self.toggle_input_mode)
        self.calibrate_format_radio.toggled.connect(self.toggle_input_mode)

        # New format inputs container (hidden when calibrating existing format)
        self.new_format_widget = QWidget()
        self.form_layout = QFormLayout(self.new_format_widget)
        self.form_layout.setContentsMargins(0, 0, 0, 0)

        self.nameInput = QLineEdit(self)
        self.nameInput.setPlaceholderText("custom well plate")
        self.form_layout.addRow("Sample Name:", self.nameInput)

        self.rowsInput = QSpinBox(self)
        self.rowsInput.setKeyboardTracking(False)
        self.rowsInput.setRange(1, 100)
        self.rowsInput.setValue(8)
        self.form_layout.addRow("# Rows:", self.rowsInput)

        self.colsInput = QSpinBox(self)
        self.colsInput.setKeyboardTracking(False)
        self.colsInput.setRange(1, 100)
        self.colsInput.setValue(12)
        self.form_layout.addRow("# Columns:", self.colsInput)

        # Add new inputs for plate dimensions
        self.plateWidthInput = QDoubleSpinBox(self)
        self.plateWidthInput.setKeyboardTracking(False)
        self.plateWidthInput.setRange(10, 500)  # Adjust range as needed
        self.plateWidthInput.setValue(127.76)  # Default value for a standard 96-well plate
        self.plateWidthInput.setSuffix(" mm")
        self.form_layout.addRow("Plate Width:", self.plateWidthInput)

        self.plateHeightInput = QDoubleSpinBox(self)
        self.plateHeightInput.setKeyboardTracking(False)
        self.plateHeightInput.setRange(10, 500)  # Adjust range as needed
        self.plateHeightInput.setValue(85.48)  # Default value for a standard 96-well plate
        self.plateHeightInput.setSuffix(" mm")
        self.form_layout.addRow("Plate Height:", self.plateHeightInput)

        # A1 offset from plate top-left corner (plate-intrinsic; drives the
        # rendering of the auto-generated navigator map). SBS 96-well defaults.
        self.a1OffsetXInput = QDoubleSpinBox(self)
        self.a1OffsetXInput.setKeyboardTracking(False)
        self.a1OffsetXInput.setRange(0.0, 500.0)
        self.a1OffsetXInput.setSingleStep(0.1)
        self.a1OffsetXInput.setDecimals(3)
        self.a1OffsetXInput.setValue(14.38)
        self.a1OffsetXInput.setSuffix(" mm")
        self.a1OffsetXInput.setToolTip(
            "X distance from the plate's physical top-left corner to the A1 well center.\n"
            "Affects navigator map aesthetics only; stage positioning uses the calibrated A1."
        )
        self.form_layout.addRow("A1 X offset (drawing):", self.a1OffsetXInput)

        self.a1OffsetYInput = QDoubleSpinBox(self)
        self.a1OffsetYInput.setKeyboardTracking(False)
        self.a1OffsetYInput.setRange(0.0, 500.0)
        self.a1OffsetYInput.setSingleStep(0.1)
        self.a1OffsetYInput.setDecimals(3)
        self.a1OffsetYInput.setValue(11.24)
        self.a1OffsetYInput.setSuffix(" mm")
        self.a1OffsetYInput.setToolTip(
            "Y distance from the plate's physical top-left corner to the A1 well center.\n"
            "Affects navigator map aesthetics only; stage positioning uses the calibrated A1."
        )
        self.form_layout.addRow("A1 Y offset (drawing):", self.a1OffsetYInput)

        self.wellSpacingInput = QDoubleSpinBox(self)
        self.wellSpacingInput.setKeyboardTracking(False)
        self.wellSpacingInput.setRange(0.1, 100)
        self.wellSpacingInput.setValue(9)
        self.wellSpacingInput.setSingleStep(0.1)
        self.wellSpacingInput.setDecimals(2)
        self.wellSpacingInput.setSuffix(" mm")
        self.form_layout.addRow("Well Spacing:", self.wellSpacingInput)

        self.wellShapeInput = QComboBox(self)
        self.wellShapeInput.addItems(["Circle", "Rectangle"])
        self.form_layout.addRow("Well Shape:", self.wellShapeInput)
        self.wellShapeInput.currentTextChanged.connect(self._on_new_format_shape_changed)

        self.new_format_well_size_input = QDoubleSpinBox(self)
        self.new_format_well_size_input.setKeyboardTracking(False)
        self.new_format_well_size_input.setRange(0.1, 50)
        self.new_format_well_size_input.setSingleStep(0.1)
        self.new_format_well_size_input.setDecimals(3)
        self.new_format_well_size_input.setValue(6.21)
        self.new_format_well_size_input.setSuffix(" mm")
        self._well_size_label = "Well diameter (3-point):"
        self.form_layout.addRow(self._well_size_label, self.new_format_well_size_input)

        self.new_format_well_height_input = QDoubleSpinBox(self)
        self.new_format_well_height_input.setKeyboardTracking(False)
        self.new_format_well_height_input.setRange(0.1, 50)
        self.new_format_well_height_input.setSingleStep(0.1)
        self.new_format_well_height_input.setDecimals(3)
        self.new_format_well_height_input.setValue(6.21)
        self.new_format_well_height_input.setSuffix(" mm")
        self.form_layout.addRow("Well height:", self.new_format_well_height_input)
        self.new_format_well_height_input.setEnabled(False)  # enabled when shape == Rectangle

        self.new_format_corner_radius_input = QDoubleSpinBox(self)
        self.new_format_corner_radius_input.setKeyboardTracking(False)
        self.new_format_corner_radius_input.setRange(0.0, 25.0)
        self.new_format_corner_radius_input.setSingleStep(0.1)
        self.new_format_corner_radius_input.setDecimals(3)
        self.new_format_corner_radius_input.setValue(0.0)
        self.new_format_corner_radius_input.setSuffix(" mm")
        self.form_layout.addRow("Corner radius (rect):", self.new_format_corner_radius_input)
        self.new_format_corner_radius_input.setEnabled(False)

        left_layout.addWidget(self.new_format_widget)

        # Existing format parameters section (initially hidden)
        self.existing_params_group = QGroupBox("Format Parameters")
        existing_params_layout = QFormLayout()

        self.existing_spacing_input = QDoubleSpinBox(self)
        self.existing_spacing_input.setKeyboardTracking(False)
        self.existing_spacing_input.setRange(0.1, 100)
        self.existing_spacing_input.setSingleStep(0.1)
        self.existing_spacing_input.setDecimals(3)
        self.existing_spacing_input.setSuffix(" mm")
        existing_params_layout.addRow("Well Spacing:", self.existing_spacing_input)

        self.existing_well_size_input = QDoubleSpinBox(self)
        self.existing_well_size_input.setKeyboardTracking(False)
        self.existing_well_size_input.setRange(0.1, 50)
        self.existing_well_size_input.setSingleStep(0.1)
        self.existing_well_size_input.setDecimals(3)
        self.existing_well_size_input.setSuffix(" mm")
        existing_params_layout.addRow("Well Size:", self.existing_well_size_input)

        self.existing_a1_offset_x_input = QDoubleSpinBox(self)
        self.existing_a1_offset_x_input.setKeyboardTracking(False)
        self.existing_a1_offset_x_input.setRange(0.0, 500.0)
        self.existing_a1_offset_x_input.setSingleStep(0.1)
        self.existing_a1_offset_x_input.setDecimals(3)
        self.existing_a1_offset_x_input.setSuffix(" mm")
        self.existing_a1_offset_x_input.setToolTip(
            "X distance from the plate's physical top-left corner to A1 center.\n"
            "Drawing-only: controls navigator map layout, not stage positioning."
        )
        existing_params_layout.addRow("A1 X offset (drawing):", self.existing_a1_offset_x_input)

        self.existing_a1_offset_y_input = QDoubleSpinBox(self)
        self.existing_a1_offset_y_input.setKeyboardTracking(False)
        self.existing_a1_offset_y_input.setRange(0.0, 500.0)
        self.existing_a1_offset_y_input.setSingleStep(0.1)
        self.existing_a1_offset_y_input.setDecimals(3)
        self.existing_a1_offset_y_input.setSuffix(" mm")
        self.existing_a1_offset_y_input.setToolTip(
            "Y distance from the plate's physical top-left corner to A1 center.\n"
            "Drawing-only: controls navigator map layout, not stage positioning."
        )
        existing_params_layout.addRow("A1 Y offset (drawing):", self.existing_a1_offset_y_input)

        self.existing_params_group.setLayout(existing_params_layout)

        self.update_params_button = QPushButton("Update Parameters")
        self.update_params_button.clicked.connect(self.update_existing_parameters)

        existing_format_buttons = QHBoxLayout()
        existing_format_buttons.addWidget(self.update_params_button, 1)
        existing_format_buttons.addWidget(self.delete_format_button)

        self.existing_params_group.hide()
        self.update_params_button.hide()
        self.delete_format_button.hide()
        left_layout.addWidget(self.existing_params_group)
        left_layout.addLayout(existing_format_buttons)

        # Calibration method selection
        self.calibration_method_group = QGroupBox("Calibration Method")
        calibration_method_layout = QVBoxLayout()

        self.method_button_group = QButtonGroup(self)
        self.edge_points_radio = QRadioButton("3 Edge Points (recommended for large wells)")
        self.center_point_radio = QRadioButton("Center Point (recommended for small wells)")
        self.method_button_group.addButton(self.edge_points_radio)
        self.method_button_group.addButton(self.center_point_radio)
        self.edge_points_radio.setChecked(True)

        calibration_method_layout.addWidget(self.edge_points_radio)
        calibration_method_layout.addWidget(self.center_point_radio)
        self.calibration_method_group.setLayout(calibration_method_layout)
        left_layout.addWidget(self.calibration_method_group)

        # Only connect one radio button to avoid double-calls (both emit toggled when selection changes)
        self.edge_points_radio.toggled.connect(self.toggle_calibration_method)

        # 3 Edge Points UI
        self.points_widget = QWidget()
        points_layout = QGridLayout(self.points_widget)
        points_layout.setContentsMargins(0, 0, 0, 0)
        self.cornerLabels = []
        self.setPointButtons = []
        self.edge_points_label = QLabel("Navigate to and Select\n3 Points on the Edge of Well A1")
        self.edge_points_label.setAlignment(Qt.AlignCenter)
        points_layout.addWidget(self.edge_points_label, 0, 0, 1, 2)
        for i in range(1, 4):
            label = QLabel(f"Point {i}: N/A")
            button = QPushButton("Set Point")
            button.setFixedWidth(button.sizeHint().width())
            button.clicked.connect(lambda checked, index=i - 1: self.setCorner(index))
            points_layout.addWidget(label, i, 0)
            points_layout.addWidget(button, i, 1)
            self.cornerLabels.append(label)
            self.setPointButtons.append(button)

        points_layout.setColumnStretch(0, 1)
        left_layout.addWidget(self.points_widget)

        # Center Point UI
        self.center_point_widget = QWidget()
        center_point_layout = QGridLayout(self.center_point_widget)
        center_point_layout.setContentsMargins(0, 0, 0, 0)

        center_point_label = QLabel("Navigate to the Center of Well A1")
        center_point_label.setAlignment(Qt.AlignCenter)
        center_point_layout.addWidget(center_point_label, 0, 0, 1, 2)

        self.center_point_status_label = QLabel("Center: Not set")
        self.set_center_button = QPushButton("Set Center")
        self.set_center_button.setFixedWidth(self.set_center_button.sizeHint().width())
        self.set_center_button.clicked.connect(self.setCenterPoint)
        center_point_layout.addWidget(self.center_point_status_label, 1, 0)
        center_point_layout.addWidget(self.set_center_button, 1, 1)

        # Well size input for center point method (since we can't calculate it)
        # Hidden when calibrating existing formats (Format Parameters section has well size)
        self.center_well_size_label = QLabel("Well Size:")
        self.center_well_size_input = QDoubleSpinBox(self)
        self.center_well_size_input.setKeyboardTracking(False)
        self.center_well_size_input.setRange(0.1, 50)
        self.center_well_size_input.setSingleStep(0.1)
        self.center_well_size_input.setDecimals(3)
        self.center_well_size_input.setValue(3.0)  # Default for small wells
        self.center_well_size_input.setSuffix(" mm")
        center_point_layout.addWidget(self.center_well_size_label, 2, 0)
        center_point_layout.addWidget(self.center_well_size_input, 2, 1)

        center_point_layout.setColumnStretch(0, 1)
        self.center_point_widget.hide()  # Initially hidden
        left_layout.addWidget(self.center_point_widget)

        # Add 'Click to Move' checkbox
        self.clickToMoveCheckbox = QCheckBox("Click to Move")
        self.clickToMoveCheckbox.stateChanged.connect(self.toggleClickToMove)
        left_layout.addWidget(self.clickToMoveCheckbox)

        # Add 'Show Virtual Joystick' checkbox
        self.showJoystickCheckbox = QCheckBox("Virtual Joystick")
        self.showJoystickCheckbox.stateChanged.connect(self.toggleVirtualJoystick)
        left_layout.addWidget(self.showJoystickCheckbox)

        self.calibrateButton = QPushButton("Calibrate")
        self.calibrateButton.clicked.connect(self.calibrate)
        self.calibrateButton.setEnabled(False)
        left_layout.addWidget(self.calibrateButton)

        # Add left column to main layout
        layout.addLayout(left_layout)

        self.live_viewer = CalibrationLiveViewer()
        self.streamHandler.image_to_display.connect(self.live_viewer.display_image)

        if not self.was_live:
            self.liveController.start_live()

        # when the dialog closes i want to # self.liveController.stop_live() if live was stopped before. . . if it was on before, leave it on
        layout.addWidget(self.live_viewer)

        # Right column for joystick and sensitivity controls
        self.right_layout = QVBoxLayout()
        self.right_layout.addStretch(1)

        self.joystick = Joystick(self)
        self.joystick.joystickMoved.connect(self.moveStage)
        self.right_layout.addWidget(self.joystick, 0, Qt.AlignTop | Qt.AlignHCenter)

        self.right_layout.addStretch(1)

        # Create a container widget for sensitivity label and slider
        sensitivity_layout = QVBoxLayout()

        sensitivityLabel = QLabel("Joystick Sensitivity")
        sensitivityLabel.setAlignment(Qt.AlignCenter)
        sensitivity_layout.addWidget(sensitivityLabel)

        self.sensitivitySlider = QSlider(Qt.Horizontal)
        self.sensitivitySlider.setMinimum(1)
        self.sensitivitySlider.setMaximum(100)
        self.sensitivitySlider.setValue(50)
        self.sensitivitySlider.setTickPosition(QSlider.TicksBelow)
        self.sensitivitySlider.setTickInterval(10)

        label_width = sensitivityLabel.sizeHint().width()
        self.sensitivitySlider.setFixedWidth(label_width)

        sensitivity_layout.addWidget(self.sensitivitySlider, 0, Qt.AlignHCenter)

        self.right_layout.addLayout(sensitivity_layout)

        layout.addLayout(self.right_layout)

        if not self.was_live:
            self.liveController.start_live()

    def toggleVirtualJoystick(self, state):
        if state:
            self.joystick.show()
            self.sensitivitySlider.show()
            self.right_layout.itemAt(self.right_layout.indexOf(self.joystick)).widget().show()
            self.right_layout.itemAt(self.right_layout.count() - 1).layout().itemAt(
                0
            ).widget().show()  # Show sensitivity label
            self.right_layout.itemAt(self.right_layout.count() - 1).layout().itemAt(
                1
            ).widget().show()  # Show sensitivity slider
        else:
            self.joystick.hide()
            self.sensitivitySlider.hide()
            self.right_layout.itemAt(self.right_layout.indexOf(self.joystick)).widget().hide()
            self.right_layout.itemAt(self.right_layout.count() - 1).layout().itemAt(
                0
            ).widget().hide()  # Hide sensitivity label
            self.right_layout.itemAt(self.right_layout.count() - 1).layout().itemAt(
                1
            ).widget().hide()  # Hide sensitivity slider

    def moveStage(self, x, y):
        sensitivity = self.sensitivitySlider.value() / 50.0  # Normalize to 0-2 range
        max_speed = 0.1 * sensitivity
        exponent = 2

        dx = math.copysign(max_speed * abs(x) ** exponent, x)
        dy = math.copysign(max_speed * abs(y) ** exponent, y)

        self.stage.move_x(dx)
        self.stage.move_y(dy)

    def toggleClickToMove(self, state):
        if state == Qt.Checked:
            self.live_viewer.signal_calibration_viewer_click.connect(self.viewerClicked)
        else:
            self.live_viewer.signal_calibration_viewer_click.disconnect(self.viewerClicked)

    def viewerClicked(self, x, y, width, height):
        pixel_size_um = (
            self.navigationViewer.objectiveStore.get_pixel_size_factor()
            * self.liveController.microscope.camera.get_pixel_size_binned_um()
        )

        pixel_sign_x = 1
        pixel_sign_y = 1 if INVERTED_OBJECTIVE else -1

        delta_x = pixel_sign_x * pixel_size_um * x / 1000.0
        delta_y = pixel_sign_y * pixel_size_um * y / 1000.0

        self.stage.move_x(delta_x)
        self.stage.move_y(delta_y)

    def setCorner(self, index):
        if self.corners[index] is None:
            pos = self.stage.get_pos()
            x = pos.x_mm
            y = pos.y_mm

            # Check if the new point is different from existing points
            if any(corner is not None and np.allclose([x, y], corner) for corner in self.corners):
                QMessageBox.warning(
                    self,
                    "Duplicate Point",
                    "This point is too close to an existing point. Please choose a different location.",
                )
                return

            self.corners[index] = (x, y)
            self.cornerLabels[index].setText(f"Point {index+1}: ({x:.3f}, {y:.3f})")
            self.setPointButtons[index].setText("Clear Point")
        else:
            self.corners[index] = None
            self.cornerLabels[index].setText(f"Point {index+1}: Not set")
            self.setPointButtons[index].setText("Set Point")

        self.update_calibrate_button_state()

    def _format_display_name(self, format_id) -> str:
        """Return a display name for a wellplate format, adding 'well plate' suffix if not present."""
        name = str(format_id)
        if "well plate" not in name.lower():
            return f"{format_id} well plate"
        return name

    def populate_existing_formats(self):
        self.existing_format_combo.clear()
        for format_ in WELLPLATE_FORMAT_SETTINGS:
            self.existing_format_combo.addItem(self._format_display_name(format_), format_)
        self._sync_delete_format_button()

    def toggle_input_mode(self):
        is_new_format = self.new_format_radio.isChecked()

        self.new_format_widget.setVisible(is_new_format)
        self.center_well_size_label.setVisible(is_new_format)
        self.center_well_size_input.setVisible(is_new_format)

        self.existing_format_combo.setVisible(not is_new_format)
        self.existing_params_group.setVisible(not is_new_format)
        self.update_params_button.setVisible(not is_new_format)
        self.delete_format_button.setVisible(not is_new_format)

        if not is_new_format:
            self.load_existing_format_values()
        self._sync_delete_format_button()

    def _sync_delete_format_button(self):
        if self.new_format_radio.isChecked():
            self.delete_format_button.setEnabled(False)
            self.delete_format_button.setToolTip("")
            return
        fid = self.existing_format_combo.currentData()
        if fid is None:
            self.delete_format_button.setEnabled(False)
            self.delete_format_button.setToolTip("")
            return
        if WellplateFormatWidget.is_builtin_format(fid):
            self.delete_format_button.setEnabled(False)
            self.delete_format_button.setToolTip("Built-in sample formats cannot be deleted.")
            return
        self.delete_format_button.setEnabled(True)
        self.delete_format_button.setToolTip("Remove this format from the list and saved settings.")

    def delete_selected_format(self):
        selected_format = self.existing_format_combo.currentData()
        if selected_format is None or WellplateFormatWidget.is_builtin_format(selected_format):
            return
        display_name = self._format_display_name(selected_format)
        reply = QMessageBox.question(
            self,
            "Delete Format",
            f"Remove '{display_name}' from saved sample formats?\nThis cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        if not self.wellplateFormatWidget.remove_format(selected_format):
            QMessageBox.warning(self, "Delete Failed", "This format could not be removed.")
            return
        self.populate_existing_formats()
        self.load_existing_format_values()
        self.reset_calibration_points()
        self._sync_delete_format_button()
        QMessageBox.information(self, "Format Removed", f"'{display_name}' has been removed.")

    def load_existing_format_values(self):
        """Load current values from selected existing format into the parameter inputs."""
        selected_format = self.existing_format_combo.currentData()
        if selected_format is None:
            return

        settings = WELLPLATE_FORMAT_SETTINGS.get(selected_format, {})
        self.existing_spacing_input.setValue(settings.get("well_spacing_mm", 9.0))

        # Use consistent well size for both inputs
        well_size = settings.get("well_size_mm", 6.0)
        self.existing_well_size_input.setValue(well_size)
        self.center_well_size_input.setValue(well_size)

        # A1 offset from plate edge (drawing / navigator map only).
        a1_offset = settings.get("a1_offset_mm", [settings.get("a1_x_mm", 0.0),
                                                   settings.get("a1_y_mm", 0.0)])
        self.existing_a1_offset_x_input.setValue(float(a1_offset[0]))
        self.existing_a1_offset_y_input.setValue(float(a1_offset[1]))

        # Auto-select center point method for 384 and 1536 well plates because their
        # small well diameters make it difficult to reliably set 3 distinct points
        # on the well edge under a microscope
        if selected_format in ("384 well plate", "1536 well plate"):
            self.center_point_radio.setChecked(True)
        else:
            self.edge_points_radio.setChecked(True)

    def on_existing_format_changed(self):
        """Handle existing format combo box selection change."""
        if self.calibrate_format_radio.isChecked():
            self.load_existing_format_values()
            # Reset calibration points when format changes
            self.reset_calibration_points()
        self._sync_delete_format_button()

    def reset_calibration_points(self):
        """Reset all calibration points to unset state."""
        # Reset edge points
        for i in range(3):
            self.corners[i] = None
            self.cornerLabels[i].setText(f"Point {i+1}: Not set")
            self.setPointButtons[i].setText("Set Point")

        # Reset center point
        self.center_point = None
        self.center_point_status_label.setText("Center: Not set")
        self.set_center_button.setText("Set Center")

        self.update_calibrate_button_state()

    def toggle_calibration_method(self):
        """Toggle between 3 edge points and center point calibration methods."""
        if self.edge_points_radio.isChecked():
            self.points_widget.show()
            self.center_point_widget.hide()
        else:
            self.points_widget.hide()
            self.center_point_widget.show()
        self.update_calibrate_button_state()

    def setCenterPoint(self):
        """Set or clear the center point for center point calibration method."""
        if self.center_point is None:
            pos = self.stage.get_pos()
            x = pos.x_mm
            y = pos.y_mm
            self.center_point = (x, y)
            self.center_point_status_label.setText(f"Center: ({x:.3f}, {y:.3f})")
            self.set_center_button.setText("Clear Center")
        else:
            self.center_point = None
            self.center_point_status_label.setText("Center: Not set")
            self.set_center_button.setText("Set Center")
        self.update_calibrate_button_state()

    def update_calibrate_button_state(self):
        """Update the calibrate button enabled state based on current calibration method."""
        if self.center_point_radio.isChecked():
            self.calibrateButton.setEnabled(self.center_point is not None)
        else:
            self.calibrateButton.setEnabled(all(corner is not None for corner in self.corners))

    def _get_calibration_data(self):
        """Extract calibration data based on current calibration method.

        Returns:
            tuple: (a1_x_mm, a1_y_mm, well_size_mm) or None if validation fails.
            Displays appropriate warning message if validation fails.
        """
        if self.center_point_radio.isChecked():
            if self.center_point is None:
                QMessageBox.warning(self, "Incomplete Information", "Please set the center point before calibrating.")
                return None
            a1_x_mm, a1_y_mm = self.center_point
            # Use appropriate well size input based on mode
            if self.calibrate_format_radio.isChecked():
                well_size_mm = self.existing_well_size_input.value()
            else:
                well_size_mm = self.center_well_size_input.value()
        else:
            if not all(self.corners):
                QMessageBox.warning(self, "Incomplete Information", "Please set 3 corner points before calibrating.")
                return None
            if self.calibrate_format_radio.isChecked():
                well_size_mm = self.existing_well_size_input.value()
            else:
                well_size_mm = self.new_format_well_size_input.value()
            if well_size_mm <= 0:
                QMessageBox.warning(
                    self,
                    "Invalid Well Size",
                    "Well diameter must be positive for 3-point calibration.",
                )
                return None
            self._log.info(
                f"Fitting circle (fixed diameter {well_size_mm} mm) to corners: {self.corners}"
            )
            center, radius = self.calculate_circle(self.corners, well_size_mm)
            self._log.info(f"Fitted circle center: {center}, radius: {radius}")
            a1_x_mm, a1_y_mm = center
            well_size_mm = float(radius * 2)
        return a1_x_mm, a1_y_mm, well_size_mm

    def update_existing_parameters(self):
        """Update parameters for an existing format without recalibrating the position."""
        selected_format = self.existing_format_combo.currentData()
        if selected_format is None:
            QMessageBox.warning(self, "No Format Selected", "Please select a format to update.")
            return

        try:
            # Get the new values
            new_spacing = self.existing_spacing_input.value()
            new_well_size = self.existing_well_size_input.value()
            new_a1_offset_x = self.existing_a1_offset_x_input.value()
            new_a1_offset_y = self.existing_a1_offset_y_input.value()

            # Get existing settings
            existing_settings = WELLPLATE_FORMAT_SETTINGS.get(selected_format)
            if existing_settings is None:
                QMessageBox.critical(self, "Update Failed", f"Format '{selected_format}' not found in settings.")
                return

            print(f"Updating parameters for {self._format_display_name(selected_format)}")
            print(
                f"OLD: spacing={existing_settings.get('well_spacing_mm')}, well_size={existing_settings.get('well_size_mm')}, "
                f"a1_offset={existing_settings.get('a1_offset_mm')}"
            )
            print(
                f"NEW: spacing={new_spacing}, well_size={new_well_size}, "
                f"a1_offset=({new_a1_offset_x}, {new_a1_offset_y})"
            )

            # Update the settings — well shape-specific fields follow the current
            # shape so the generator picks up the new size correctly.
            shape = existing_settings.get("well_shape", "circle")
            scale = 1 / PLATE_IMAGE_MM_PER_PX
            updates = {
                "well_spacing_mm": new_spacing,
                "row_spacing_mm": new_spacing,
                "col_spacing_mm": new_spacing,
                "well_size_mm": new_well_size,
                "a1_offset_mm": [float(new_a1_offset_x), float(new_a1_offset_y)],
                "a1_x_pixel": round(float(new_a1_offset_x) * scale),
                "a1_y_pixel": round(float(new_a1_offset_y) * scale),
            }
            if shape == "circle":
                updates["well_diameter_mm"] = new_well_size
                updates["well_width_mm"] = new_well_size
                updates["well_height_mm"] = new_well_size
            else:
                updates["well_width_mm"] = new_well_size
            WELLPLATE_FORMAT_SETTINGS[selected_format].update(updates)

            # Save, regenerate the navigator map, and refresh.
            self.wellplateFormatWidget.save_formats_to_yaml()
            from control.core import wellplate_image_generator
            wellplate_image_generator.ensure_plate_image(selected_format)
            self.wellplateFormatWidget.populate_combo_box()

            # Re-select the format (triggers wellplateChanged which calls setWellplateSettings)
            index = self.wellplateFormatWidget.comboBox.findData(selected_format)
            if index >= 0:
                self.wellplateFormatWidget.comboBox.setCurrentIndex(index)

            QMessageBox.information(
                self,
                "Parameters Updated",
                f"Parameters for '{self._format_display_name(selected_format)}' have been updated successfully.",
            )

        except Exception as e:
            import traceback

            traceback.print_exc()
            QMessageBox.critical(self, "Update Failed", f"An error occurred while updating parameters: {str(e)}")

    def calibrate(self):
        """Execute wellplate calibration based on current settings.

        Supports two modes:
        - New format: Creates a new custom wellplate format with all parameters
        - Existing format: Updates position calibration (a1_x_mm, a1_y_mm) and well_size_mm

        Supports two calibration methods:
        - 3 Edge Points: Calculates well center and diameter from 3 points on well edge
        - Center Point: Uses directly-specified center position with manual well size
        """
        try:
            if self.new_format_radio.isChecked():
                self._calibrate_new_format()
            else:
                self._calibrate_existing_format()
        except np.linalg.LinAlgError:
            import traceback

            traceback.print_exc()
            QMessageBox.critical(
                self,
                "Calibration Error",
                "Unable to calculate well center from the provided points.\n"
                "The 3 points may be nearly collinear (in a straight line).\n"
                "Please choose points that are more spread out around the well edge.",
            )
        except Exception as e:
            import traceback

            traceback.print_exc()
            QMessageBox.critical(self, "Calibration Error", f"An error occurred during calibration: {str(e)}")

    def _on_new_format_shape_changed(self, shape_text):
        """Enable/disable rectangle-only fields and update the size-field label."""
        is_rect = shape_text == "Rectangle"
        self.new_format_well_height_input.setEnabled(is_rect)
        self.new_format_corner_radius_input.setEnabled(is_rect)
        new_label = "Well width:" if is_rect else "Well diameter (3-point):"
        for i in range(self.form_layout.rowCount()):
            field_item = self.form_layout.itemAt(i, QFormLayout.FieldRole)
            if field_item and field_item.widget() is self.new_format_well_size_input:
                label_item = self.form_layout.itemAt(i, QFormLayout.LabelRole)
                if label_item and isinstance(label_item.widget(), QLabel):
                    label_item.widget().setText(new_label)
                break

    def _calibrate_new_format(self):
        """Create and calibrate a new wellplate format."""
        if not self.nameInput.text():
            QMessageBox.warning(self, "Incomplete Information", "Please enter a name for the format.")
            return

        calibration_data = self._get_calibration_data()
        if calibration_data is None:
            return
        a1_x_mm, a1_y_mm, well_size_mm = calibration_data

        name = self.nameInput.text()
        plate_width_mm = self.plateWidthInput.value()
        plate_height_mm = self.plateHeightInput.value()

        well_spacing_mm = self.wellSpacingInput.value()
        rows = self.rowsInput.value()
        cols = self.colsInput.value()
        shape_text = self.wellShapeInput.currentText()
        shape = "circle" if shape_text == "Circle" else "rectangle"
        if shape == "circle":
            well_diameter_mm = well_size_mm
            well_width_mm = well_diameter_mm
            well_height_mm = well_diameter_mm
            well_corner_radius_mm = 0.0
        else:
            well_diameter_mm = well_size_mm  # legacy alias
            well_width_mm = well_size_mm
            well_height_mm = self.new_format_well_height_input.value()
            well_corner_radius_mm = self.new_format_corner_radius_input.value()

        a1_offset_x_mm = self.a1OffsetXInput.value()
        a1_offset_y_mm = self.a1OffsetYInput.value()
        scale = 1 / PLATE_IMAGE_MM_PER_PX
        new_format = {
            "display_name": name,
            "plate_dimensions_mm": [float(plate_width_mm), float(plate_height_mm)],
            "plate_corner_radius_mm": 3.18,
            "a1_chamfer": False,
            # Plate-intrinsic offset drives the navigator map drawing; stage
            # calibration (below) drives moves. They are distinct concepts but
            # currently alias the same legacy field on reload — re-calibrate A1
            # after changing machines.
            "a1_offset_mm": [float(a1_offset_x_mm), float(a1_offset_y_mm)],
            "rows": int(rows),
            "cols": int(cols),
            "row_spacing_mm": float(well_spacing_mm),
            "col_spacing_mm": float(well_spacing_mm),
            "number_of_skip": 0,
            "well_shape": shape,
            "well_diameter_mm": float(well_diameter_mm),
            "well_width_mm": float(well_width_mm),
            "well_height_mm": float(well_height_mm),
            "well_corner_radius_mm": float(well_corner_radius_mm),
            "a1_x_mm": float(a1_x_mm),
            "a1_y_mm": float(a1_y_mm),
            "a1_x_pixel": round(float(a1_offset_x_mm) * scale),
            "a1_y_pixel": round(float(a1_offset_y_mm) * scale),
            "well_size_mm": float(well_width_mm),
            "well_spacing_mm": float(well_spacing_mm),
        }

        self.wellplateFormatWidget.add_custom_format(name, new_format)
        self.wellplateFormatWidget.save_formats_to_yaml()
        from control.core import wellplate_image_generator
        wellplate_image_generator.ensure_plate_image(name)

        self._finish_calibration(name, f"New format '{name}' has been successfully created and calibrated.")

    def _calibrate_existing_format(self):
        """Recalibrate an existing wellplate format."""
        selected_format = self.existing_format_combo.currentData()

        calibration_data = self._get_calibration_data()
        if calibration_data is None:
            return
        a1_x_mm, a1_y_mm, well_size_mm = calibration_data

        existing_settings = WELLPLATE_FORMAT_SETTINGS[selected_format]
        display_name = self._format_display_name(selected_format)

        print(f"Updating existing format {display_name}")
        print(
            f"OLD: 'a1_x_mm': {existing_settings['a1_x_mm']}, 'a1_y_mm': {existing_settings['a1_y_mm']}, "
            f"'well_size_mm': {existing_settings['well_size_mm']}"
        )
        print(f"NEW: 'a1_x_mm': {a1_x_mm}, 'a1_y_mm': {a1_y_mm}, 'well_size_mm': {well_size_mm}")

        WELLPLATE_FORMAT_SETTINGS[selected_format].update(
            {
                "a1_x_mm": a1_x_mm,
                "a1_y_mm": a1_y_mm,
                "well_size_mm": well_size_mm,
            }
        )

        # Sync a1_offset_mm (plate-intrinsic field consumed by the YAML serializer
        # and the image generator) with the newly calibrated A1 position.
        if "a1_offset_mm" in WELLPLATE_FORMAT_SETTINGS[selected_format]:
            WELLPLATE_FORMAT_SETTINGS[selected_format]["a1_offset_mm"] = [a1_x_mm, a1_y_mm]
            scale = 1 / PLATE_IMAGE_MM_PER_PX
            WELLPLATE_FORMAT_SETTINGS[selected_format]["a1_x_pixel"] = round(a1_x_mm * scale)
            WELLPLATE_FORMAT_SETTINGS[selected_format]["a1_y_pixel"] = round(a1_y_mm * scale)

        self.wellplateFormatWidget.save_formats_to_yaml()
        from control.core import wellplate_image_generator
        wellplate_image_generator.ensure_plate_image(selected_format)

        self._finish_calibration(selected_format, f"Format '{display_name}' has been successfully recalibrated.")

    def _finish_calibration(self, format_id, success_message: str):
        """Complete calibration by updating UI and showing success message."""
        self.wellplateFormatWidget.populate_combo_box()
        index = self.wellplateFormatWidget.comboBox.findData(format_id)
        if index >= 0:
            self.wellplateFormatWidget.comboBox.setCurrentIndex(index)

        QMessageBox.information(self, "Calibration Successful", success_message)
        self.accept()

    @staticmethod
    def _fit_circle_unconstrained(points):
        """Circumcenter-style center and mean radial distance as radius (original behavior)."""
        pts = np.asarray(points, dtype=float)
        A = np.array([pts[1] - pts[0], pts[2] - pts[0]])
        b = np.sum(A * (pts[1:3] + pts[0]) / 2, axis=1)
        center = np.linalg.solve(A, b)
        radius = np.mean(np.linalg.norm(pts - center, axis=1))
        return center, radius

    def calculate_circle(self, points, well_size_mm):
        """Best-fit circle: fixed-radius least squares, or unconstrained fit on failure.

        Constrained step minimizes sum_i (||p_i - c|| - R)^2 with R = well_size_mm / 2.
        If that optimization fails, falls back to :meth:`_fit_circle_unconstrained`.

        Returns:
            (center, radius_mm): ``radius_mm`` is half the well diameter to use (fitted mean
            radius after unconstrained fallback).
        """
        from scipy.optimize import least_squares

        pts = np.asarray(points, dtype=float)
        r_mm = well_size_mm / 2.0

        A = np.array([pts[1] - pts[0], pts[2] - pts[0]])
        b = np.sum(A * (pts[1:3] + pts[0]) / 2, axis=1)
        try:
            x0 = np.linalg.solve(A, b)
        except np.linalg.LinAlgError:
            x0 = np.mean(pts, axis=0)

        def residuals(c_xy):
            cx, cy = c_xy
            d = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
            return d - r_mm

        warn_msg = (
            "Fixed-radius optimization did not succeed; using unconstrained fit from the three points instead."
        )

        try:
            result = least_squares(residuals, x0, method="trf")
        except Exception:
            self._log.warning("Fixed-radius circle least_squares raised", exc_info=True)
            QMessageBox.warning(self, "Circle fit", warn_msg)
            return self._fit_circle_unconstrained(points)

        if not result.success or not np.all(np.isfinite(result.x)):
            self._log.warning("Fixed-radius circle fit did not converge: %s", result.message)
            QMessageBox.warning(self, "Circle fit", warn_msg)
            return self._fit_circle_unconstrained(points)

        return result.x, r_mm

    def closeEvent(self, event):
        # Stop live view if it wasn't initially on
        if not self.was_live:
            self.liveController.stop_live()
        super().closeEvent(event)

    def accept(self):
        # Stop live view if it wasn't initially on
        if not self.was_live:
            self.liveController.stop_live()
        super().accept()

    def reject(self):
        # This method is called when the dialog is closed without accepting
        if not self.was_live:
            self.liveController.stop_live()
        sample = self.navigationViewer.sample

        # Convert sample string to format int
        if "glass slide" in sample:
            sample_format = "glass slide"
        else:
            try:
                sample_format = int(sample.split()[0])
            except (ValueError, IndexError):
                print(f"Unable to parse sample format from '{sample}'. Defaulting to 0.")
                sample_format = "glass slide"

        # Set dropdown to the current sample format
        index = self.wellplateFormatWidget.comboBox.findData(sample_format)
        if index >= 0:
            self.wellplateFormatWidget.comboBox.setCurrentIndex(index)

        # Update wellplate settings
        self.wellplateFormatWidget.setWellplateSettings(sample_format)

        super().reject()


class CalibrationLiveViewer(QWidget):

    signal_calibration_viewer_click = Signal(int, int, int, int)
    signal_mouse_moved = Signal(int, int)

    def __init__(self):
        super().__init__()
        self.initial_zoom_set = False
        self.initUI()

    def initUI(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.view = pg.GraphicsLayoutWidget()
        self.viewbox = self.view.addViewBox()
        self.viewbox.setAspectLocked(True)
        self.viewbox.invertY(True)

        self.viewbox.setMouseEnabled(x=False, y=False)  # Disable panning
        self.viewbox.setMenuEnabled(False)

        # Set appropriate panning limits based on the acquisition image or plate size
        xmax = int(CAMERA_CONFIG.CROP_WIDTH_UNBINNED)
        ymax = int(CAMERA_CONFIG.CROP_HEIGHT_UNBINNED)
        self.viewbox.setLimits(xMin=0, xMax=xmax, yMin=0, yMax=ymax)

        self.img_item = pg.ImageItem()
        self.viewbox.addItem(self.img_item)

        # Add fixed crosshair
        pen = QPen(QColor(255, 0, 0))  # Red color
        pen.setWidth(4)

        self.crosshair_h = pg.InfiniteLine(angle=0, movable=False, pen=pen)
        self.crosshair_v = pg.InfiniteLine(angle=90, movable=False, pen=pen)
        self.viewbox.addItem(self.crosshair_h)
        self.viewbox.addItem(self.crosshair_v)

        layout.addWidget(self.view)

        # Connect double-click event
        self.view.scene().sigMouseClicked.connect(self.onMouseClicked)

        # Set fixed size for the viewer
        self.setFixedSize(500, 500)

    def setCrosshairPosition(self):
        center = self.viewbox.viewRect().center()
        self.crosshair_h.setPos(center.y())
        self.crosshair_v.setPos(center.x())

    def display_image(self, image):
        # Step 1: Update the image
        self.img_item.setImage(image)

        # Step 2: Get the image dimensions
        image_width = image.shape[1]
        image_height = image.shape[0]

        # Step 3: Calculate the center of the image
        image_center_x = image_width / 2
        image_center_y = image_height / 2

        # Step 4: Calculate the current view range
        current_view_range = self.viewbox.viewRect()

        # Step 5: If it's the first image or initial zoom hasn't been set, center the image
        if not self.initial_zoom_set:
            self.viewbox.setRange(xRange=(0, image_width), yRange=(0, image_height), padding=0)
            self.initial_zoom_set = True  # Mark initial zoom as set

        # Step 6: Always center the view around the image center (for seamless transitions)
        else:
            self.viewbox.setRange(
                xRange=(
                    image_center_x - current_view_range.width() / 2,
                    image_center_x + current_view_range.width() / 2,
                ),
                yRange=(
                    image_center_y - current_view_range.height() / 2,
                    image_center_y + current_view_range.height() / 2,
                ),
                padding=0,
            )

        # Step 7: Ensure the crosshair is updated
        self.setCrosshairPosition()

    # def mouseMoveEvent(self, event):
    #     self.signal_mouse_moved.emit(event.x(), event.y())

    def onMouseClicked(self, event):
        # Map the scene position to view position
        if event.double():  # double click to move
            pos = event.pos()
            scene_pos = self.viewbox.mapSceneToView(pos)

            # Get the x, y coordinates
            x, y = int(scene_pos.x()), int(scene_pos.y())
            # Ensure the coordinates are within the image boundaries
            image_shape = self.img_item.image.shape
            if 0 <= x < image_shape[1] and 0 <= y < image_shape[0]:
                # Adjust the coordinates to be relative to the center of the image
                x_centered = x - image_shape[1] // 2
                y_centered = y - image_shape[0] // 2
                # Emit the signal with the clicked coordinates and image size
                self.signal_calibration_viewer_click.emit(x_centered, y_centered, image_shape[1], image_shape[0])
            else:
                print("click was outside the image bounds.")
        else:
            print("single click only detected")

    def wheelEvent(self, event):
        if event.angleDelta().y() > 0:
            scale_factor = 0.9
        else:
            scale_factor = 1.1

        # Get the center of the viewbox
        center = self.viewbox.viewRect().center()

        # Scale the view
        self.viewbox.scaleBy((scale_factor, scale_factor), center)

        # Update crosshair position after scaling
        self.setCrosshairPosition()

        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setCrosshairPosition()


class Joystick(QWidget):
    joystickMoved = Signal(float, float)  # Emits x and y values between -1 and 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(200, 200)
        self.inner_radius = 40
        self.max_distance = self.width() // 2 - self.inner_radius
        self.outer_radius = int(self.width() * 3 / 8)
        self.current_x = 0
        self.current_y = 0
        self.is_pressed = False
        self.timer = QTimer(self)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Calculate the painting area
        paint_rect = QRectF(0, 0, 200, 200)

        # Draw outer circle
        painter.setBrush(QColor(230, 230, 230))  # Light grey fill
        painter.setPen(QPen(QColor(100, 100, 100), 2))  # Dark grey outline
        painter.drawEllipse(paint_rect.center(), self.outer_radius, self.outer_radius)

        # Draw inner circle (joystick position)
        painter.setBrush(QColor(100, 100, 100))
        painter.setPen(Qt.NoPen)
        joystick_x = paint_rect.center().x() + self.current_x * self.max_distance
        joystick_y = paint_rect.center().y() + self.current_y * self.max_distance
        painter.drawEllipse(QPointF(joystick_x, joystick_y), self.inner_radius, self.inner_radius)

    def mousePressEvent(self, event):
        if QRectF(0, 0, 200, 200).contains(event.pos()):
            self.is_pressed = True
            self.updateJoystickPosition(event.pos())
            self.timer.timeout.connect(self.update_position)
            self.timer.start(10)

    def mouseMoveEvent(self, event):
        if self.is_pressed and QRectF(0, 0, 200, 200).contains(event.pos()):
            self.updateJoystickPosition(event.pos())

    def mouseReleaseEvent(self, event):
        self.is_pressed = False
        self.updateJoystickPosition(QPointF(100, 100))  # Center position
        self.timer.timeout.disconnect(self.update_position)
        self.joystickMoved.emit(0, 0)

    def update_position(self):
        if self.is_pressed:
            self.joystickMoved.emit(self.current_x, -self.current_y)

    def updateJoystickPosition(self, pos):
        center = QPointF(100, 100)
        dx = pos.x() - center.x()
        dy = pos.y() - center.y()
        distance = math.sqrt(dx**2 + dy**2)

        if distance > self.max_distance:
            dx = dx * self.max_distance / distance
            dy = dy * self.max_distance / distance

        self.current_x = dx / self.max_distance
        self.current_y = dy / self.max_distance
        self.update()


class Well1536SelectionWidget(QWidget):

    signal_wellSelected = Signal(bool)
    signal_wellSelectedPos = Signal(float, float)

    def __init__(self, wellplateFormatWidget):
        super().__init__()
        self.wellplateFormatWidget = wellplateFormatWidget
        self.format = "1536 well plate"
        self.selected_cells = {}  # Dictionary to keep track of selected cells and their colors
        self.current_cell = None  # To track the current (green) cell

        # defaults
        self.rows = 32
        self.columns = 48
        self.spacing_mm = 2.25
        self.number_of_skip = 0
        self.well_size_mm = 1.5
        self.a1_x_mm = 11.0  # measured stage position - to update
        self.a1_y_mm = 7.86  # measured stage position - to update
        self.a1_x_pixel = 144  # coordinate on the png - to update
        self.a1_y_pixel = 108  # coordinate on the png - to update

        self.well_shape = "circle"
        if self.wellplateFormatWidget is not None:
            s = self.wellplateFormatWidget.getWellplateSettings(self.format)
            self.rows = s["rows"]
            self.columns = s["cols"]
            self.spacing_mm = s["well_spacing_mm"]
            self.number_of_skip = s["number_of_skip"]
            self.a1_x_mm = s["a1_x_mm"]
            self.a1_y_mm = s["a1_y_mm"]
            self.a1_x_pixel = s["a1_x_pixel"]
            self.a1_y_pixel = s["a1_y_pixel"]
            self.well_size_mm = s["well_size_mm"]
            self.well_shape = s.get("well_shape", "circle")

        self.initUI()

    def initUI(self):
        self.setWindowTitle("1536 Well Plate")
        self.setGeometry(100, 100, 750, 400)  # Increased width to accommodate controls

        self.a = 11
        image_width = 48 * self.a
        image_height = 32 * self.a

        self.image = QPixmap(image_width, image_height)
        self.image.fill(QColor("white"))
        self.label = QLabel()
        self.label.setPixmap(self.image)
        self.label.setFixedSize(image_width, image_height)
        self.label.setAlignment(Qt.AlignCenter)

        # Mouse interaction is handled on the widget that *displays* the pixmap (QLabel),
        # not on the QPixmap itself. We delay the single-click handler so that it can be
        # cancelled when a double-click arrives.
        self._pending_click_cell = None
        self._pending_click_modifiers = Qt.NoModifier
        self._click_token = 0
        self._press_pos = None
        self._press_button = None
        self._press_modifiers = Qt.NoModifier
        self._is_dragging = False
        self._drag_start_cell = None
        self._last_drag_rect = None  # (r0, r1, c0, c1)
        self._drag_mode = None  # "replace" | "add" | "remove"
        app = QApplication.instance()
        self._double_click_ms = app.doubleClickInterval() if app is not None else 250
        self.label.mousePressEvent = self._on_label_mouse_press
        self.label.mouseDoubleClickEvent = self._on_label_mouse_double_click
        self.label.mouseMoveEvent = self._on_label_mouse_move
        self.label.mouseReleaseEvent = self._on_label_mouse_release

        self.cell_input = QLineEdit(self)
        self.cell_input.setPlaceholderText("e.g. AE12 or B4")
        go_button = QPushButton("Go to well", self)
        go_button.clicked.connect(self.go_to_cell)
        self.selection_input = QLineEdit(self)
        self.selection_input.setPlaceholderText("e.g. A1:E48, X1, AC24, Z2:AF6, ...")
        self.selection_input.editingFinished.connect(self.select_cells)
        self.selection_input.returnPressed.connect(self.select_cells)

        # Create navigation buttons
        up_button = QPushButton("↑", self)
        left_button = QPushButton("←", self)
        right_button = QPushButton("→", self)
        down_button = QPushButton("↓", self)
        add_button = QPushButton("Select", self)

        # Connect navigation buttons to their respective functions
        up_button.clicked.connect(self.move_up)
        left_button.clicked.connect(self.move_left)
        right_button.clicked.connect(self.move_right)
        down_button.clicked.connect(self.move_down)
        add_button.clicked.connect(self.add_current_well)

        layout = QHBoxLayout()
        layout.addWidget(self.label)

        layout_controls = QVBoxLayout()
        layout_controls.addStretch(2)

        # Add navigation buttons in a + sign layout
        layout_move = QGridLayout()
        layout_move.addWidget(up_button, 0, 2)
        layout_move.addWidget(left_button, 1, 1)
        layout_move.addWidget(add_button, 1, 2)
        layout_move.addWidget(right_button, 1, 3)
        layout_move.addWidget(down_button, 2, 2)
        layout_move.setColumnStretch(0, 1)
        layout_move.setColumnStretch(4, 1)
        layout_controls.addLayout(layout_move)

        layout_controls.addStretch(1)

        layout_input = QGridLayout()
        layout_input.addWidget(QLabel("Well Navigation"), 0, 0)
        layout_input.addWidget(self.cell_input, 0, 1)
        layout_input.addWidget(go_button, 0, 2)
        layout_input.addWidget(QLabel("Well Selection"), 1, 0)
        layout_input.addWidget(self.selection_input, 1, 1, 1, 2)
        layout_controls.addLayout(layout_input)

        control_widget = QWidget()
        control_widget.setLayout(layout_controls)
        control_widget.setFixedHeight(image_height)  # Set the height of controls to match the image

        layout.addWidget(control_widget)
        self.setLayout(layout)

    def _cell_from_label_pos(self, pos: QPoint):
        """Map a click position in label pixel coords -> (row, col) or None."""
        col = int(pos.x() // self.a)
        row = int(pos.y() // self.a)
        if 0 <= row < self.rows and 0 <= col < self.columns:
            return (row, col)
        return None

    def _row_label(self, row: int) -> str:
        # A..Z, AA..AF for 32 rows
        if row < 26:
            return chr(65 + row)
        return chr(64 + (row // 26)) + chr(65 + (row % 26))

    def _cell_name(self, row: int, col: int) -> str:
        return f"{self._row_label(row)}{col + 1}"

    def _emit_selection_changed(self):
        """Refresh UI elements that depend on selected_cells and notify listeners."""
        self.redraw_wells()
        self._set_selection_input_from_selected_cells()
        self.signal_wellSelected.emit(bool(self.selected_cells))

    def _toggle_or_replace_selection(self, cell, *, additive: bool):
        """
        Selection semantics to match the table-based well selector:
        - additive=False: replace selection with only this cell
        - additive=True: toggle this cell without clearing others
        """
        if additive:
            if cell in self.selected_cells:
                self.selected_cells.pop(cell, None)
            else:
                self.selected_cells[cell] = "#1f77b4"
        else:
            self.selected_cells = {cell: "#1f77b4"}

    def _set_selection_input_from_selected_cells(self):
        """Render current selection into the textbox, compacted into per-row ranges."""
        if not self.selected_cells:
            self.selection_input.setText("")
            return

        rows_to_cols = {}
        for r, c in self.selected_cells.keys():
            rows_to_cols.setdefault(r, []).append(c)

        parts = []
        for r in sorted(rows_to_cols.keys()):
            cols = sorted(set(rows_to_cols[r]))
            start = prev = cols[0]
            for c in cols[1:]:
                if c == prev + 1:
                    prev = c
                    continue
                # flush run
                if start == prev:
                    parts.append(f"{self._row_label(r)}{start + 1}")
                else:
                    parts.append(f"{self._row_label(r)}{start + 1}:{self._row_label(r)}{prev + 1}")
                start = prev = c
            # flush last run
            if start == prev:
                parts.append(f"{self._row_label(r)}{start + 1}")
            else:
                parts.append(f"{self._row_label(r)}{start + 1}:{self._row_label(r)}{prev + 1}")

        self.selection_input.setText(", ".join(parts))

    def _commit_single_click(self, token: int):
        # If a double-click happened, the token will have changed -> ignore.
        if token != self._click_token:
            return
        if self._is_dragging:
            return
        cell = self._pending_click_cell
        mods = self._pending_click_modifiers
        self._pending_click_cell = None
        self._pending_click_modifiers = Qt.NoModifier
        if cell is None:
            return

        self.current_cell = cell
        self._toggle_or_replace_selection(cell, additive=bool(mods & Qt.ShiftModifier))

        # Update UI without navigating (no signal_wellSelectedPos here).
        row, col = cell
        self.cell_input.setText(self._cell_name(row, col))
        self._emit_selection_changed()

    def _on_label_mouse_press(self, event):
        if event.button() not in (Qt.LeftButton, Qt.RightButton):
            return

        cell = self._cell_from_label_pos(event.pos())
        if cell is None:
            return

        self._press_pos = QPoint(event.pos())
        self._press_button = event.button()
        self._press_modifiers = event.modifiers()
        self._is_dragging = False
        self._drag_start_cell = cell
        self._last_drag_rect = None
        self._drag_mode = None

        # Delay single-click action so we can cancel it if a double-click arrives.
        if event.button() == Qt.LeftButton:
            self._pending_click_cell = cell
            self._pending_click_modifiers = event.modifiers()
            self._click_token += 1
            token = self._click_token
            QTimer.singleShot(self._double_click_ms, lambda: self._commit_single_click(token))
        event.accept()

    def _apply_drag_rect(self, rect, mode: str):
        r0, r1, c0, c1 = rect
        if mode == "add":
            for r in range(r0, r1 + 1):
                for c in range(c0, c1 + 1):
                    self.selected_cells[(r, c)] = "#1f77b4"
        elif mode == "remove":
            for r in range(r0, r1 + 1):
                for c in range(c0, c1 + 1):
                    self.selected_cells.pop((r, c), None)

    def _on_label_mouse_move(self, event):
        if self._press_pos is None or self._drag_start_cell is None:
            return

        # Start drag if we moved far enough.
        if not self._is_dragging:
            threshold = QApplication.startDragDistance()
            if (event.pos() - self._press_pos).manhattanLength() < threshold:
                return

            # Cancel any pending single-click action.
            self._click_token += 1
            self._pending_click_cell = None
            self._pending_click_modifiers = Qt.NoModifier
            self._is_dragging = True

            # Determine drag mode:
            # - Left-drag: replace selection (unless Shift is held, then add)
            # - Right-drag: remove
            if self._press_button == Qt.RightButton:
                self._drag_mode = "remove"
            elif self._press_modifiers & Qt.ShiftModifier:
                self._drag_mode = "add"
            else:
                self._drag_mode = "replace"

        current_cell = self._cell_from_label_pos(event.pos())
        if current_cell is None:
            return

        r0 = min(self._drag_start_cell[0], current_cell[0])
        r1 = max(self._drag_start_cell[0], current_cell[0])
        c0 = min(self._drag_start_cell[1], current_cell[1])
        c1 = max(self._drag_start_cell[1], current_cell[1])
        rect = (r0, r1, c0, c1)
        if rect == self._last_drag_rect:
            return
        self._last_drag_rect = rect

        if self._drag_mode == "replace":
            self.selected_cells = {}
            self._apply_drag_rect(rect, "add")
        else:
            # add/remove
            self._apply_drag_rect(rect, self._drag_mode)
        self.current_cell = current_cell  # keep outline tracking cursor
        self.redraw_wells()
        event.accept()

    def _on_label_mouse_release(self, event):
        if self._press_pos is None:
            return

        if self._is_dragging:
            # Finalize drag selection: sync textbox + update navigation overlay.
            self._set_selection_input_from_selected_cells()
            self.signal_wellSelected.emit(bool(self.selected_cells))

        self._press_pos = None
        self._press_button = None
        self._press_modifiers = Qt.NoModifier
        self._is_dragging = False
        self._drag_start_cell = None
        self._last_drag_rect = None
        self._drag_mode = None
        event.accept()

    def _on_label_mouse_double_click(self, event):
        if event.button() != Qt.LeftButton:
            return

        cell = self._cell_from_label_pos(event.pos())
        if cell is None:
            return

        # Cancel any pending single-click action.
        self._click_token += 1
        self._pending_click_cell = None
        self._is_dragging = False

        # Double-click navigates to the cell AND selects it.
        self._toggle_or_replace_selection(cell, additive=bool(event.modifiers() & Qt.ShiftModifier))
        self._set_selection_input_from_selected_cells()
        self.signal_wellSelected.emit(bool(self.selected_cells))

        # Navigate to the cell (emits signal_wellSelectedPos).
        self.current_cell = cell
        self.update_current_cell()
        event.accept()

    def move_up(self):
        if self.current_cell:
            row, col = self.current_cell
            if row > 0:
                self.current_cell = (row - 1, col)
                self.update_current_cell()

    def move_left(self):
        if self.current_cell:
            row, col = self.current_cell
            if col > 0:
                self.current_cell = (row, col - 1)
                self.update_current_cell()

    def move_right(self):
        if self.current_cell:
            row, col = self.current_cell
            if col < self.columns - 1:
                self.current_cell = (row, col + 1)
                self.update_current_cell()

    def move_down(self):
        if self.current_cell:
            row, col = self.current_cell
            if row < self.rows - 1:
                self.current_cell = (row + 1, col)
                self.update_current_cell()

    def add_current_well(self):
        if self.current_cell:
            row, col = self.current_cell
            cell = (row, col)
            cell_name = self._cell_name(row, col)
            if cell in self.selected_cells:
                self.selected_cells.pop(cell, None)
                print(f"Removed well {cell_name}")
            else:
                self.selected_cells[cell] = "#1f77b4"
                print(f"Added well {cell_name}")
            # Redraw only (do not navigate on select/toggle).
            self._emit_selection_changed()

    def update_current_cell(self):
        self.redraw_wells()
        row, col = self.current_cell
        # Update cell_input with the correct label (e.g., A1, B2, AA1, etc.)
        self.cell_input.setText(self._cell_name(row, col))

        x_mm = col * self.spacing_mm + self.a1_x_mm + WELLPLATE_OFFSET_X_mm
        y_mm = row * self.spacing_mm + self.a1_y_mm + WELLPLATE_OFFSET_Y_mm
        self.signal_wellSelectedPos.emit(x_mm, y_mm)

    def redraw_wells(self):
        self.image.fill(QColor("white"))  # Clear the pixmap first
        painter = QPainter(self.image)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QColor("white"))
        use_ellipse = getattr(self, "well_shape", "circle") == "circle"
        # Draw selected cells (blue)
        for (row, col), color in self.selected_cells.items():
            painter.setBrush(QColor(color))
            if use_ellipse:
                painter.drawEllipse(col * self.a, row * self.a, self.a, self.a)
            else:
                painter.drawRect(col * self.a, row * self.a, self.a, self.a)
        # Draw current cell outline (red).
        if self.current_cell:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor("red"), 2))
            row, col = self.current_cell
            if use_ellipse:
                painter.drawEllipse(col * self.a + 2, row * self.a + 2, self.a - 3, self.a - 3)
            else:
                painter.drawRect(col * self.a + 2, row * self.a + 2, self.a - 3, self.a - 3)
        painter.end()
        self.label.setPixmap(self.image)

    def go_to_cell(self):
        cell_desc = self.cell_input.text().strip()
        match = re.match(r"([A-Za-z]+)(\d+)", cell_desc)
        if match:
            row_part, col_part = match.groups()
            row_index = self.row_to_index(row_part)
            col_index = int(col_part) - 1
            self.current_cell = (row_index, col_index)  # Update the current cell
            self.update_current_cell()

    def select_cells(self):
        # first clear selection
        self.selected_cells = {}

        pattern = r"([A-Za-z]+)(\d+):?([A-Za-z]*)(\d*)"
        cell_descriptions = self.selection_input.text().split(",")
        for desc in cell_descriptions:
            match = re.match(pattern, desc.strip())
            if match:
                start_row, start_col, end_row, end_col = match.groups()
                start_row_index = self.row_to_index(start_row)
                start_col_index = int(start_col) - 1

                if end_row and end_col:  # It's a range
                    end_row_index = self.row_to_index(end_row)
                    end_col_index = int(end_col) - 1
                    for row in range(min(start_row_index, end_row_index), max(start_row_index, end_row_index) + 1):
                        for col in range(min(start_col_index, end_col_index), max(start_col_index, end_col_index) + 1):
                            self.selected_cells[(row, col)] = "#1f77b4"
                else:  # It's a single cell
                    self.selected_cells[(start_row_index, start_col_index)] = "#1f77b4"
        self.redraw_wells()
        self.signal_wellSelected.emit(bool(self.selected_cells))

    def row_to_index(self, row):
        index = 0
        for char in row:
            index = index * 26 + (ord(char.upper()) - ord("A") + 1)
        return index - 1

    def onSelectionChanged(self):
        self.get_selected_cells()

    def onWellplateChanged(self):
        """A placeholder to match the method in WellSelectionWidget"""
        pass

    def get_selected_cells(self):
        list_of_selected_cells = list(self.selected_cells.keys())
        return list_of_selected_cells


class LedMatrixSettingsDialog(QDialog):
    def __init__(self, led_array):
        self.led_array = led_array
        super().__init__()
        self.setWindowTitle("LED Matrix Settings")

        self.layout = QVBoxLayout()

        # Add QDoubleSpinBox for LED intensity (0-1)
        self.NA_spinbox = QDoubleSpinBox()
        self.NA_spinbox.setKeyboardTracking(False)
        self.NA_spinbox.setRange(0, 1)
        self.NA_spinbox.setSingleStep(0.01)
        self.NA_spinbox.setValue(self.led_array.NA)

        NA_layout = QHBoxLayout()
        NA_layout.addWidget(QLabel("NA"))
        NA_layout.addWidget(self.NA_spinbox)

        self.layout.addLayout(NA_layout)
        self.setLayout(self.layout)

        # add ok/cancel buttons
        self.button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        self.layout.addWidget(self.button_box)

        self.button_box.accepted.connect(self.update_NA)

    def update_NA(self):
        self.led_array.set_NA(self.NA_spinbox.value())


class SampleSettingsWidget(QFrame):
    def __init__(self, ObjectivesWidget, WellplateFormatWidget, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.objectivesWidget = ObjectivesWidget
        self.wellplateFormatWidget = WellplateFormatWidget

        # Objective lens lives next to Start Live / Autolevel in LiveControlWidget; sample format only here.
        top_row_layout = QHBoxLayout()
        top_row_layout.setSpacing(8)
        top_row_layout.setContentsMargins(0, 2, 0, 2)
        top_row_layout.addWidget(self.wellplateFormatWidget, 1)
        self.setLayout(top_row_layout)
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)

        # Connect signals for saving settings
        self.objectivesWidget.signal_objective_changed.connect(self.save_settings)
        self.wellplateFormatWidget.signalWellplateSettings.connect(lambda *args: self.save_settings())

    def save_settings(self):
        """Save current objective and wellplate format to cache"""
        os.makedirs("cache", exist_ok=True)
        data = {
            "objective": self.objectivesWidget.dropdown.currentText(),
            "wellplate_format": self.wellplateFormatWidget.wellplate_format,
        }

        with open("cache/objective_and_sample_format.txt", "w") as f:
            json.dump(data, f)


from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from mpl_toolkits.mplot3d import proj3d
from scipy.interpolate import griddata


class SurfacePlotWidget(QWidget):
    """
    A widget that displays a 3D surface plot of the coordinates.
    """

    signal_point_clicked = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._log = squid.logging.get_logger(__name__)

        # Setup canvas and figure
        self.fig = Figure()
        self.canvas = FigureCanvas(self.fig)
        self.ax = self.fig.add_subplot(111, projection="3d")

        layout = QVBoxLayout()
        layout.addWidget(self.canvas)
        self.setLayout(layout)

        self.selected_index = None
        self.plot_populated = False

        # Connect events
        self.canvas.mpl_connect("scroll_event", self.on_scroll)
        self.canvas.mpl_connect("button_press_event", self.on_click)

        self.x = list()
        self.y = list()
        self.z = list()
        self.regions = list()
        # Filtered coordinates for plotting (min Z at each unique X,Y)
        self.x_plot = np.array([])
        self.y_plot = np.array([])
        self.z_plot = np.array([])

    def clear(self):
        self.x.clear()
        self.y.clear()
        self.z.clear()
        self.regions.clear()
        self.x_plot = np.array([])
        self.y_plot = np.array([])
        self.z_plot = np.array([])
        # Reset plot state and clear the visual axes
        self.plot_populated = False
        self.ax.clear()
        self.canvas.draw()

    def add_point(self, x: float, y: float, z: float, region: int):
        self.x.append(x)
        self.y.append(y)
        self.z.append(z)
        self.regions.append(region)

    def plot(self) -> None:
        """
        Plot both surface and scatter points in 3D.

        For Z-stacks, uses the minimum Z at each unique X,Y location. This shows
        the bottom/focus surface of the sample and avoids interpolation artifacts
        that would occur if the surface passed through the middle of the stack.
        """
        try:
            # Clear previous plot
            self.ax.clear()

            if len(self.x) == 0:
                self._log.debug("No data to plot")
                self.canvas.draw()
                self.plot_populated = False
                return

            x = np.array(self.x).astype(float)
            y = np.array(self.y).astype(float)
            z = np.array(self.z).astype(float)
            regions = np.array(self.regions)

            # Filter to get minimum Z at each unique X,Y location (for Z-stacks)
            # Use vectorized approach for better performance with large datasets
            xy_precision = 4  # decimal places for grouping
            xy_keys = np.round(x, xy_precision) + 1j * np.round(y, xy_precision)

            # Find index of minimum Z for each unique (X, Y) using vectorized operations
            unique_xy, inverse = np.unique(xy_keys, return_inverse=True)

            # Sort by group (inverse) then by Z, so first in each group has minimum Z
            order = np.lexsort((z, inverse))
            grouped_inverse = inverse[order]

            # First occurrence of each group in sorted order corresponds to minimum Z
            _, first_indices = np.unique(grouped_inverse, return_index=True)
            min_z_indices = order[first_indices]

            # Store filtered coordinates using the min-Z indices (ensures x, y, z, region all match)
            self.x_plot = x[min_z_indices]
            self.y_plot = y[min_z_indices]
            self.z_plot = z[min_z_indices]
            regions_plot = regions[min_z_indices]

            # plot surface by region
            for r in np.unique(regions_plot):
                try:
                    mask = regions_plot == r
                    num_points = np.sum(mask)
                    if num_points >= 4:
                        # Check if points have sufficient spread in X and Y for surface interpolation
                        # griddata uses Delaunay triangulation which requires 2D spread in X-Y space
                        x_range = np.ptp(self.x_plot[mask])  # peak-to-peak (max - min)
                        y_range = np.ptp(self.y_plot[mask])
                        # Use practical threshold based on typical stage precision (~1 µm)
                        # Smaller spreads can lead to nearly collinear points and Qhull errors
                        min_spread = 1e-3  # minimum spread in mm (~1 µm)

                        if x_range < min_spread or y_range < min_spread:
                            # Single FOV or collinear points: skip surface, scatter plot will still show
                            self._log.debug(
                                f"Region {r}: insufficient X,Y spread for surface "
                                f"(x_range={x_range:.2e}, y_range={y_range:.2e}), showing scatter only"
                            )
                        else:
                            x_surface = self.x_plot[mask]
                            y_surface = self.y_plot[mask]
                            z_surface = self.z_plot[mask]

                            grid_x, grid_y = np.mgrid[
                                min(x_surface) : max(x_surface) : 10j, min(y_surface) : max(y_surface) : 10j
                            ]
                            grid_z = griddata((x_surface, y_surface), z_surface, (grid_x, grid_y), method="cubic")
                            self.ax.plot_surface(grid_x, grid_y, grid_z, cmap="viridis", edgecolor="none")
                    else:
                        self._log.debug(f"Region {r} has only {num_points} point(s), skipping surface interpolation")
                except Exception as e:
                    raise Exception(f"Cannot plot region {r}: {e}")

            # Create scatter plot using filtered coordinates (bottom Z only)
            self.colors = ["r"] * len(self.x_plot)
            self.scatter = self.ax.scatter(self.x_plot, self.y_plot, self.z_plot, c=self.colors, s=30)

            # Set labels
            self.ax.set_xlabel("X (mm)")
            self.ax.set_ylabel("Y (mm)")
            self.ax.set_zlabel("Z (um)")
            self.ax.set_title("Double-click a point to go to that position")

            # Force x and y to have same scale
            max_range = max(np.ptp(self.x_plot), np.ptp(self.y_plot))
            if max_range == 0:
                max_range = 1.0  # Default range for single point
            center_x = np.mean(self.x_plot)
            center_y = np.mean(self.y_plot)

            self.ax.set_xlim(center_x - max_range / 2, center_x + max_range / 2)
            self.ax.set_ylim(center_y - max_range / 2, center_y + max_range / 2)

            self.canvas.draw()
            self.plot_populated = True
        except Exception as e:
            self._log.error(f"Error plotting surface: {e}")

    def on_scroll(self, event):
        scale = 1.1 if event.button == "up" else 0.9

        def zoom(lim):
            center = (lim[0] + lim[1]) / 2
            half_range = (lim[1] - lim[0]) / 2 * scale
            return center - half_range, center + half_range

        self.ax.set_xlim(zoom(self.ax.get_xlim()))
        self.ax.set_ylim(zoom(self.ax.get_ylim()))
        self.ax.set_zlim(zoom(self.ax.get_zlim()))
        self.canvas.draw()

    def on_click(self, event):
        if not self.plot_populated:
            return
        if not event.dblclick or event.inaxes != self.ax:
            return

        # Cancel drag mode after double-click
        self.canvas.button_pressed = None  # FIX: Avoids AttributeError

        # Project 3D points to 2D screen space (use filtered plot coordinates)
        x2d, y2d, _ = proj3d.proj_transform(self.x_plot, self.y_plot, self.z_plot, self.ax.get_proj())
        dists = np.hypot(x2d - event.xdata, y2d - event.ydata)
        idx = np.argmin(dists)

        # Threshold in data coordinates
        display_thresh = 0.05 * max(
            self.ax.get_xlim()[1] - self.ax.get_xlim()[0], self.ax.get_ylim()[1] - self.ax.get_ylim()[0]
        )
        if dists[idx] > display_thresh:
            return

        # Change point color
        self.colors = ["r"] * len(self.x_plot)
        self.colors[idx] = "g"
        self.scatter.remove()
        self.scatter = self.ax.scatter(self.x_plot, self.y_plot, self.z_plot, c=self.colors, s=30)

        print(f"Clicked Point: x={self.x_plot[idx]:.3f}, y={self.y_plot[idx]:.3f}, z={self.z_plot[idx]:.3f}")
        self.canvas.draw()
        self.signal_point_clicked.emit(float(self.x_plot[idx]), float(self.y_plot[idx]))
