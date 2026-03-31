# set QT_API environment variable
import os
import subprocess
import sys
from configparser import ConfigParser

from control.core.auto_focus_controller import AutoFocusController
from control.core.job_processing import CaptureInfo
from control.core.laser_auto_focus_controller import LaserAutofocusController
from control.core.scan_coordinates import (
    ScanCoordinates,
    ScanCoordinatesUpdate,
    AddScanCoordinateRegion,
    RemovedScanCoordinateRegion,
    ClearedScanCoordinates,
)
from control.NL5 import NL5

os.environ["QT_API"] = "pyqt5"
import re
import time
from typing import Any, List, Optional, Tuple

import numpy as np
import serial



# qt libraries
from qtpy.QtCore import *
from qtpy.QtWidgets import *
from qtpy.QtGui import *

from control._def import *

# app specific libraries
from control.NL5Widget import NL5Widget
from control.core.contrast_manager import ContrastManager
from control.core.live_controller import LiveController
from control.core.multi_point_controller import MultiPointController
from control.core.multi_point_utils import (
    MultiPointControllerFunctions,
    AcquisitionParameters,
    OverallProgressUpdate,
    RegionProgressUpdate,
    PlateViewInit,
    PlateViewUpdate,
)
from control.core.objective_store import ObjectiveStore
from control.core.stream_handler import StreamHandler
from control.lighting import LightSourceType, IntensityControlMode, ShutterControlMode, IlluminationController
from control.microcontroller import Microcontroller
from control.microscope import Microscope, _should_simulate
from control.models import ObservationState
from control.nidaq import AbstractNIDAQ
from squid.abc import AbstractCamera, AbstractStage
import control._def
import control.lighting
import control.utils
import control.utils_acquisition
import control.microscope
import gui.widgets as widgets
import pyqtgraph.dockarea as dock
import squid.abc
import squid.camera.settings_cache
import control.illumination_settings_cache
import squid.camera.utils
import squid.config
import squid.logging
import squid.stage.utils
from squid.config import CameraVariant

log = squid.logging.get_logger(__name__)

if USE_PRIOR_STAGE:
    import squid.stage.prior
else:
    import squid.stage.cephla
from control.piezo import PiezoStage

if USE_XERYON:
    from control.objective_changer_2_pos_controller import (
        ObjectiveChanger2PosController,
        ObjectiveChanger2PosController_Simulation,
    )

import control.core.core as core
import control.microcontroller as microcontroller
import control.serial_peripherals as serial_peripherals
import control.core_displacement_measurement as core_displacement_measurement



if USE_JUPYTER_CONSOLE:
    from control.console import JupyterWidget

if RUN_FLUIDICS:
    from control.fluidics import Fluidics

from control.slack_notifier import SlackNotifier, TimepointStats, AcquisitionStats
from gui.widgets.integrations import SlackSettingsDialog, load_slack_settings_from_cache
from gui.widgets.multipoint import TemplateMultiPointWidget

from .qt_controllers import MovementUpdater, QtAutoFocusController, QtMultiPointController

class HighContentScreeningGui(QMainWindow):
    fps_software_trigger = 100
    LASER_BASED_FOCUS_TAB_NAME = "Laser-Based Focus"
    signal_performance_mode_changed = Signal(bool)

    def __init__(
        self,
        microscope: control.microscope.Microscope,
        is_simulation=False,
        live_only_mode=False,
        skip_init=False,
        skip_homing=False,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self.log = squid.logging.get_logger(self.__class__.__name__)
        self._skip_init = skip_init
        self._skip_homing = skip_homing

        self.microscope: control.microscope.Microscope = microscope
        self.stage: AbstractStage = microscope.stage
        self.camera: AbstractCamera = microscope.camera
        self.microcontroller: Microcontroller = microscope.low_level_drivers.microcontroller

        self.xlight: Optional[serial_peripherals.XLight] = microscope.addons.xlight
        self.dragonfly: Optional[serial_peripherals.Dragonfly] = microscope.addons.dragonfly
        self.nl5: Optional[Any] = microscope.addons.nl5
        self.cellx: Optional[serial_peripherals.CellX] = microscope.addons.cellx
        self.emission_filter_wheel: Optional[serial_peripherals.Optospin | serial_peripherals.FilterController] = (
            microscope.addons.emission_filter_wheel
        )
        self.objective_changer: Optional[Any] = microscope.addons.objective_changer
        self.camera_focus: Optional[AbstractCamera] = microscope.addons.camera_focus
        self.fluidics: Optional[Fluidics] = microscope.addons.fluidics
        self.piezo: Optional[PiezoStage] = microscope.addons.piezo_stage
        self.nidaq: Optional[AbstractNIDAQ] = microscope.addons.nidaq

        self.contrastManager: ContrastManager = microscope.contrast_manager
        self.liveController: LiveController = microscope.live_controller
        self.objectiveStore: ObjectiveStore = microscope.objective_store

        self.liveController_focus_camera: Optional[LiveController] = None
        self.streamHandler_focus_camera: Optional[StreamHandler] = None
        self.imageDisplayWindow_focus: Optional[core.ImageDisplayWindow] = None
        self.displacementMeasurementController: Optional[
            core_displacement_measurement.DisplacementMeasurementController
        ] = None
        self.laserAutofocusController: Optional[LaserAutofocusController] = None
        if self.microscope.addons.camera_focus:
            # self.log.info(self.microscope.addons.camera_focus._config)
            self.liveController_focus_camera = self.microscope.live_controller_focus
            self.streamHandler_focus_camera = core.QtStreamHandler(
                accept_new_frame_fn=lambda: self.liveController_focus_camera.is_live
            ,camera=self.camera_focus)
            self.imageDisplayWindow_focus = core.ImageDisplayWindow(
                liveController=self.liveController_focus_camera, show_LUT=False, autoLevels=False
            )
            self.displacementMeasurementController = core_displacement_measurement.DisplacementMeasurementController()
            af_laser_ep = (
                microscope.addons.io_registry.get("laser_af.laser_gate")
                if microscope.addons.io_registry else None
            )
            self.laserAutofocusController = LaserAutofocusController(
                self.microcontroller,
                self.camera_focus,
                self.liveController_focus_camera,
                self.stage,
                self.piezo,
                self.objectiveStore,
                af_laser_endpoint=af_laser_ep,
            )

        self.live_only_mode = live_only_mode or LIVE_ONLY_MODE
        self.is_live_scan_grid_on = False
        self.live_scan_grid_was_on = None
        self.performance_mode = False
        self.napari_connections = {}
        self.well_selector_visible = False  # Add this line to track well selector visibility

        self.multipointController: QtMultiPointController = None
        self.streamHandler: core.QtStreamHandler = None
        self.autofocusController: AutoFocusController = None
        self.imageSaver: core.ImageSaver = core.ImageSaver()
        self.imageDisplay: core.ImageDisplay = core.ImageDisplay()
        self.trackingController: core.TrackingController = None
        self.navigationViewer: core.NavigationViewer = None
        self.scanCoordinates: Optional[ScanCoordinates] = None
        self.slackNotifier: Optional[SlackNotifier] = None
        self.slackSettingsDialog: Optional[SlackSettingsDialog] = None
        self.workflowRunnerDialog = None
        self.workflowRunner = None

        # Load Slack settings from cache
        load_slack_settings_from_cache()

        self.load_objects(is_simulation=is_simulation)
        self.setup_hardware(skip_init=self._skip_init, skip_homing=self._skip_homing)

        self.setup_movement_updater()

        # Pre-declare and give types to all our widgets so type hinting tools work.  You should
        # add to this as you add widgets.
        self.spinningDiskConfocalWidget: Optional[widgets.SpinningDiskConfocalWidget] = None
        self.nl5Wdiget: Optional[NL5Widget] = None
        self.cameraSettingWidget: Optional[widgets.CameraSettingsWidget] = None
        self.profileWidget: Optional[widgets.ProfileWidget] = None
        self.liveControlWidget: Optional[widgets.LiveControlWidget] = None
        self.navigationWidget: Optional[widgets.NavigationWidget] = None
        self.stageUtils: Optional[widgets.StageUtils] = None
        self.dacControlWidget: Optional[widgets.DACControWidget] = None
        self.autofocusWidget: Optional[widgets.AutoFocusWidget] = None
        self.piezoWidget: Optional[widgets.PiezoWidget] = None
        self.objectivesWidget: Optional[widgets.ObjectivesWidget] = None
        self.squidFilterWidget: Optional[widgets.SquidFilterWidget] = None
        self.recordingControlWidget: Optional[widgets.RecordingWidget] = None
        self.wellplateFormatWidget: Optional[widgets.WellplateFormatWidget] = None
        self.wellSelectionWidget: Optional[widgets.WellSelectionWidget] = None
        self.focusMapWidget: Optional[widgets.FocusMapWidget] = None
        self.cameraSettingWidget_focus_camera: Optional[widgets.CameraSettingsWidget] = None
        self.laserAutofocusSettingWidget: Optional[widgets.LaserAutofocusSettingWidget] = None
        self.waveformDisplay: Optional[widgets.WaveformDisplay] = None
        self.displacementMeasurementWidget: Optional[widgets.DisplacementMeasurementWidget] = None
        self.laserAutofocusControlWidget: Optional[widgets.LaserAutofocusControlWidget] = None
        self.fluidicsWidget: Optional[widgets.FluidicsWidget] = None
        self.flexibleMultiPointWidget: Optional[widgets.FlexibleMultiPointWidget] = None
        self.wellplateMultiPointWidget: Optional[widgets.WellplateMultiPointWidget] = None
        self.templateMultiPointWidget: Optional[TemplateMultiPointWidget] = None
        self.multiPointWithFluidicsWidget: Optional[widgets.MultiPointWithFluidicsWidget] = None
        self.sampleSettingsWidget: Optional[widgets.SampleSettingsWidget] = None
        self.trackingControlWidget: Optional[widgets.TrackingControllerWidget] = None
        self.napariLiveWidget: Optional[widgets.NapariLiveWidget] = None
        self.alignmentWidget: Optional[widgets.AlignmentWidget] = None
        self.imageDisplayWindow: Optional[core.ImageDisplayWindow] = None
        self.imageDisplayWindow_focus: Optional[core.ImageDisplayWindow] = None
        self.napariMultiChannelWidget: Optional[widgets.NapariMultiChannelWidget] = None
        self.zPlotWidget: Optional[widgets.SurfacePlotWidget] = None
        self.niDAQWidget: Optional[widgets.NIDAQWidget] = None
        self.fastAcquisitionWidget: Optional[widgets.FastAcquisitionWidget] = None
        self.ramMonitorWidget: Optional[widgets.RAMMonitorWidget] = None
        self.backpressureMonitorWidget: Optional[widgets.BackpressureMonitorWidget] = None

        self.recordTabWidget: QTabWidget = QTabWidget()
        # Always-visible controls panel (replaces the old tabbed UI).
        self.cameraTabWidget: QWidget = QWidget()
        self.load_widgets(is_simulation=is_simulation)
        self._sync_camera_ui_from_observation_state()
        self.setup_layout()
        self.make_connections()

        # Emit initial performance mode state to sync widgets
        self.signal_performance_mode_changed.emit(self.performance_mode)

        # Initialize live scan grid state
        self.wellplateMultiPointWidget.initialize_live_scan_grid_state()

        # Initialize Slack notifier
        self._setup_slack_notifier()

        # Skip cached position restoration on restart (hardware position hasn't changed),
        # except Z when using Xeryon (Z was retracted during cleanup).
        if self._skip_init:
            if USE_XERYON and self.objective_changer:
                if cached_pos := squid.stage.utils.get_cached_position():
                    safety_z_mm = int(Z_HOME_SAFETY_POINT) / 1000.0
                    target_z_mm = max(cached_pos.z_mm, safety_z_mm)
                    self.log.info(f"Restoring cached Z position after Xeryon restart: {target_z_mm} mm")
                    self.stage.move_z_to(target_z_mm)
            else:
                self.log.info("Skipping cached position restoration (--skip-init flag set)")
        elif self._skip_homing:
            self.log.info("Skipping cached position restoration and init_z (--skip-homing flag set)")
        elif HOMING_ENABLED_X and HOMING_ENABLED_Y:
            # Restore last session position after homing. Z homing must not gate this: if Z homing is
            # disabled in config, XY homing still runs and we still need to leave the post-homing
            # position (X offset +50 mm, etc.) and return to the cached or default workspace.
            squid.stage.utils.move_to_cached_or_default_startup_position(self.stage, self.stage.get_config())

            if ENABLE_WELLPLATE_MULTIPOINT:
                self.wellplateMultiPointWidget.init_z()
            self.flexibleMultiPointWidget.init_z()

        # Create the menu bar
        menubar = self.menuBar()
        settings_menu = menubar.addMenu("Settings")

        # Settings action (opens Preferences dialog)
        config_action = QAction("Settings...", self)
        config_action.setMenuRole(QAction.NoRole)
        config_action.triggered.connect(self.openPreferences)
        settings_menu.addAction(config_action)

        if SUPPORT_SCIMICROSCOPY_LED_ARRAY:
            led_matrix_action = QAction("LED Matrix", self)
            led_matrix_action.triggered.connect(self.openLedMatrixSettings)
            settings_menu.addAction(led_matrix_action)

        # Channel Configuration (user-facing acquisition channels)
        acq_channel_config_action = QAction("Channel Configuration...", self)
        acq_channel_config_action.setMenuRole(QAction.NoRole)
        acq_channel_config_action.triggered.connect(self.openObservationStateConfigEditor)
        settings_menu.addAction(acq_channel_config_action)

        save_observation_state_action = QAction("Save Observation State Preset...", self)
        save_observation_state_action.setMenuRole(QAction.NoRole)
        save_observation_state_action.triggered.connect(self.saveObservationStatePreset)
        settings_menu.addAction(save_observation_state_action)

        load_observation_state_action = QAction("Load Observation State Preset...", self)
        load_observation_state_action.setMenuRole(QAction.NoRole)
        load_observation_state_action.triggered.connect(self.loadObservationStatePreset)
        settings_menu.addAction(load_observation_state_action)

        # Advanced submenu
        advanced_menu = settings_menu.addMenu("Advanced")

        # Notifications section
        settings_menu.addSeparator()
        slack_action = QAction("Slack Notifications...", self)
        slack_action.setMenuRole(QAction.NoRole)
        slack_action.triggered.connect(self.openSlackSettings)
        settings_menu.addAction(slack_action)

        # Illumination Channel Configuration (in Advanced menu)
        channel_config_action = QAction("Illumination Channel Configuration", self)
        channel_config_action.triggered.connect(self.openChannelConfigurationEditor)
        advanced_menu.addAction(channel_config_action)

        # Filter Wheel Configuration (only shown if filter wheel hardware is present)
        if self.emission_filter_wheel:
            filter_wheel_config_action = QAction("Filter Wheel Configuration", self)
            filter_wheel_config_action.triggered.connect(self.openFilterWheelConfigEditor)
            advanced_menu.addAction(filter_wheel_config_action)

        if USE_JUPYTER_CONSOLE:
            # Create namespace to expose to Jupyter
            self.namespace = {
                "microscope": self.microscope,
            }

            # Create Jupyter widget as a dock widget
            self.jupyter_dock = QDockWidget("Jupyter Console", self)
            self.jupyter_widget = JupyterWidget(namespace=self.namespace)
            self.jupyter_dock.setWidget(self.jupyter_widget)
            self.addDockWidget(Qt.LeftDockWidgetArea, self.jupyter_dock)

    def load_objects(self, is_simulation):
        self.streamHandler = core.QtStreamHandler(accept_new_frame_fn=lambda: self.liveController.is_live, camera=self.camera)
        self.autofocusController = QtAutoFocusController(
            self.camera, self.stage, self.liveController, self.microcontroller, self.nl5
        )
        if ENABLE_TRACKING:
            self.trackingController = core.TrackingController(
                self.camera,
                self.microcontroller,
                self.stage,
                self.objectiveStore,
                self.liveController,
                self.autofocusController,
                self.imageDisplayWindow,
            )
        if WELLPLATE_FORMAT == "glass slide" and IS_HCS:
            self.navigationViewer = core.NavigationViewer(self.objectiveStore, self.camera, sample="4 glass slide")
        else:
            self.navigationViewer = core.NavigationViewer(self.objectiveStore, self.camera, sample=WELLPLATE_FORMAT)

        def scan_coordinate_callback(update: ScanCoordinatesUpdate):
            if isinstance(update, AddScanCoordinateRegion):
                self.navigationViewer.register_fovs_to_image(update.fov_centers)
            elif isinstance(update, RemovedScanCoordinateRegion):
                self.navigationViewer.deregister_fovs_from_image(update.fov_centers)
            elif isinstance(update, ClearedScanCoordinates):
                self.navigationViewer.clear_overlay()
            if self.focusMapWidget:
                self.focusMapWidget.on_regions_updated()

        self.scanCoordinates = ScanCoordinates(
            objectiveStore=self.objectiveStore,
            stage=self.stage,
            camera=self.camera,
            update_callback=scan_coordinate_callback,
        )
        self.multipointController = QtMultiPointController(
            self.microscope,
            self.liveController,
            self.autofocusController,
            self.objectiveStore,
            scan_coordinates=self.scanCoordinates,
            laser_autofocus_controller=self.laserAutofocusController,
            fluidics=self.fluidics,
        )

    def setup_hardware(self, skip_init: bool = False, skip_homing: bool = False):
        # Setup hardware components
        if not self.microcontroller:
            raise ValueError("Microcontroller must be none-None for hardware setup.")

        try:
            x_config = self.stage.get_config().X_AXIS
            y_config = self.stage.get_config().Y_AXIS
            z_config = self.stage.get_config().Z_AXIS

            if skip_init:
                self.log.info("Skipping hardware initialization (--skip-init flag set)")
            else:
                self.log.info(
                    f"Setting stage limits to:"
                    f" x=[{x_config.MIN_POSITION},{x_config.MAX_POSITION}],"
                    f" y=[{y_config.MIN_POSITION},{y_config.MAX_POSITION}],"
                    f" z=[{z_config.MIN_POSITION},{z_config.MAX_POSITION}]"
                )

                self.stage.set_limits(
                    x_pos_mm=x_config.MAX_POSITION,
                    x_neg_mm=x_config.MIN_POSITION,
                    y_pos_mm=y_config.MAX_POSITION,
                    y_neg_mm=y_config.MIN_POSITION,
                    z_pos_mm=z_config.MAX_POSITION,
                    z_neg_mm=z_config.MIN_POSITION,
                )

                if not skip_homing:
                    self.microscope.home_xyz()
                else:
                    self.log.info("Skipping stage homing (--skip-homing flag set)")

        except TimeoutError as e:
            # If we can't recover from a timeout, at least do our best to make sure the system is left in a safe
            # and restartable state.
            self.log.error("Setup timed out, resetting microcontroller before failing gui setup")
            self.microcontroller.reset()
            raise e
        if DEFAULT_TRIGGER_MODE == TriggerMode.HARDWARE:
            print("Setting acquisition mode to HARDWARE_TRIGGER")
            self.camera.set_acquisition_mode(squid.abc.CameraAcquisitionMode.HARDWARE_TRIGGER)
            self.microcontroller.set_trigger_mode(HARDWARE_TRIGGER_MODE)
        elif DEFAULT_TRIGGER_MODE == TriggerMode.SOFTWARE:
            self.camera.set_acquisition_mode(squid.abc.CameraAcquisitionMode.SOFTWARE_TRIGGER)
        elif DEFAULT_TRIGGER_MODE == TriggerMode.CONTINUOUS:
            self.camera.set_acquisition_mode(squid.abc.CameraAcquisitionMode.CONTINUOUS)
        else:
            raise ValueError(f"Invalid trigger mode: {DEFAULT_TRIGGER_MODE}")
        # Set up live acquisition to pull frames from background thread
        self.camera.add_frame_callback(self.streamHandler.get_frame_callback())
        self.camera.enable_callbacks(enabled=True)

        if self.camera_focus:
            self.camera_focus.set_acquisition_mode(
                squid.abc.CameraAcquisitionMode.SOFTWARE_TRIGGER
            )  # self.camera.set_continuous_acquisition()
            self.camera_focus.add_frame_callback(self.streamHandler_focus_camera.get_frame_callback())
            self.camera_focus.enable_callbacks(enabled=True)
            self.camera_focus.start_streaming()

        if self.objective_changer and not skip_homing:
            self.objective_changer.home()
            self.objective_changer.setSpeed(XERYON_SPEED)
            if DEFAULT_OBJECTIVE in XERYON_OBJECTIVE_SWITCHER_POS_1:
                self.objective_changer.moveToPosition1(move_z=False)
            elif DEFAULT_OBJECTIVE in XERYON_OBJECTIVE_SWITCHER_POS_2:
                self.objective_changer.moveToPosition2(move_z=False)

    def waitForMicrocontroller(self, timeout=5.0, error_message=None):
        try:
            self.microcontroller.wait_till_operation_is_completed(timeout)
        except TimeoutError as e:
            self.log.error(error_message or "Microcontroller operation timed out!")
            raise e

    def load_widgets(self, is_simulation=False):
        # Initialize all GUI widgets
        if ENABLE_SPINNING_DISK_CONFOCAL:
            # TODO: For user compatibility, when ENABLE_SPINNING_DISK_CONFOCAL is True, we use XLight/Cicero on default.
            # This needs to be changed when we figure out better machine configuration structure.
            if USE_DRAGONFLY:
                self.spinningDiskConfocalWidget = widgets.DragonflyConfocalWidget(self.dragonfly)
            else:
                self.spinningDiskConfocalWidget = widgets.SpinningDiskConfocalWidget(self.xlight)
        if ENABLE_NL5:
            import control.NL5Widget as NL5Widget

            self.nl5Wdiget = NL5Widget.NL5Widget(self.nl5)

        if CAMERA_TYPE in ["Toupcam", "Tucsen", "Kinetix"]:
            self.cameraSettingWidget = widgets.CameraSettingsWidget(
                self.camera,
                include_gain_exposure_time=True,
                include_trigger_controls=True,
                live_controller=self.liveController,
                include_camera_temperature_setting=True,
                include_camera_auto_wb_setting=False,
                filter_wheel_controller=self.emission_filter_wheel,
                config_repo=self.microscope.config_repo,
            )
        else:
            self.cameraSettingWidget = widgets.CameraSettingsWidget(
                self.camera,
                include_gain_exposure_time=True,
                include_trigger_controls=True,
                live_controller=self.liveController,
                include_camera_temperature_setting=False,
                include_camera_auto_wb_setting=True,
                filter_wheel_controller=self.emission_filter_wheel,
                config_repo=self.microscope.config_repo,
            )

        self._restore_cached_camera_settings()

        if USE_XERYON:
            self.objectivesWidget = widgets.ObjectivesWidget(self.objectiveStore, self.objective_changer)
        else:
            self.objectivesWidget = widgets.ObjectivesWidget(self.objectiveStore)

        self.profileWidget = widgets.ProfileWidget(self.microscope.config_repo)
        self.liveControlWidget = widgets.LiveControlWidget(
            self.streamHandler,
            self.liveController,
            self.objectiveStore,
            show_display_options=False,
            show_autolevel=True,
            autolevel=True,
            objectives_widget=self.objectivesWidget,
        )
        self.navigationWidget = widgets.NavigationWidget(
            self.stage, widget_configuration=f"{WELLPLATE_FORMAT} well plate"
        )
        self.stageUtils = widgets.StageUtils(self.stage, self.liveController, is_wellplate=True)
        self.dacControlWidget = widgets.DACControWidget(self.microcontroller)
        self.autofocusWidget = widgets.AutoFocusWidget(self.autofocusController)
        if self.piezo:
            self.piezoWidget = widgets.PiezoWidget(self.piezo)

        self.recordingControlWidget = widgets.RecordingWidget(self.streamHandler, self.imageSaver, self.liveController)
        self.wellplateFormatWidget = widgets.WellplateFormatWidget(
            self.stage, self.navigationViewer, self.streamHandler, self.liveController
        )
        if WELLPLATE_FORMAT != "1536 well plate":
            self.wellSelectionWidget = widgets.WellSelectionWidget(WELLPLATE_FORMAT, self.wellplateFormatWidget)
        else:
            self.wellSelectionWidget = widgets.Well1536SelectionWidget(self.wellplateFormatWidget)
        self.scanCoordinates.add_well_selector(self.wellSelectionWidget)
        self.focusMapWidget = widgets.FocusMapWidget(
            self.stage, self.navigationViewer, self.scanCoordinates, core.FocusMap()
        )

        if self.microscope.addons.camera_focus:
            if self.microscope.addons.camera_focus._config.camera_type == CameraVariant.TOUPCAM:
                self.cameraSettingWidget_focus_camera = widgets.CameraSettingsWidget(
                    self.camera_focus,
                    include_gain_exposure_time=False,
                    include_camera_temperature_setting=True,
                    include_camera_auto_wb_setting=False,
                )
            else:
                self.cameraSettingWidget_focus_camera = widgets.CameraSettingsWidget(
                    self.camera_focus,
                    include_gain_exposure_time=False,
                    include_camera_temperature_setting=False,
                    include_camera_auto_wb_setting=True,
                )
            self.laserAutofocusSettingWidget = widgets.LaserAutofocusSettingWidget(
                self.streamHandler_focus_camera,
                self.liveController_focus_camera,
                self.laserAutofocusController,
                stretch=False,
            )  # ,show_display_options=True)
            self.waveformDisplay = widgets.WaveformDisplay(N=1000, include_x=True, include_y=False)
            self.displacementMeasurementWidget = widgets.DisplacementMeasurementWidget(
                self.displacementMeasurementController, self.waveformDisplay
            )
            self.laserAutofocusControlWidget: widgets.LaserAutofocusControlWidget = widgets.LaserAutofocusControlWidget(
                self.laserAutofocusController, self.liveController
            )
            self.imageDisplayWindow_focus = core.ImageDisplayWindow(liveController=self.liveController_focus_camera)

        if RUN_FLUIDICS:
            self.fluidicsWidget = widgets.FluidicsWidget(self.fluidics)

        # Determine NIDAQ and fast-acquisition capabilities from MachineConfig
        from control.core.config import ConfigRepository

        mc = ConfigRepository().get_machine_config()
        nidaq_entry = mc.get_device("nidaq")
        nidaq_enabled = bool(nidaq_entry and nidaq_entry.enabled)
        fast_acq_enabled = bool(
            getattr(mc, "software", None)
            and getattr(mc.software, "acquisition", None)
            and bool(getattr(mc.software.acquisition, "fast_acquisition", False))
        )

        if nidaq_enabled:
            nidaq_simulated = _should_simulate(is_simulation, SIMULATE_NIDAQ)
            self.niDAQWidget = widgets.NIDAQWidget(self.nidaq, is_simulation=nidaq_simulated)

        # Fast acquisition widget
        if fast_acq_enabled:
            self.fastAcquisitionWidget = widgets.FastAcquisitionWidget(
                self.microscope,
                ni_daq_widget=self.niDAQWidget if nidaq_enabled else None,
                live_controller=self.liveController,
                live_control_widget=self.liveControlWidget,
            )

        self.imageDisplayTabs = QTabWidget(parent=self)
        if self.live_only_mode:
            if ENABLE_TRACKING:
                self.imageDisplayWindow = core.ImageDisplayWindow(self.liveController, self.contrastManager)
                self.imageDisplayWindow.show_ROI_selector()
            else:
                self.imageDisplayWindow = core.ImageDisplayWindow(
                    self.liveController, self.contrastManager, show_LUT=True, autoLevels=True
                )
            self.imageDisplayTabs = self.imageDisplayWindow.widget
            self.napariMosaicDisplayWidget = None
        else:
            self.setupImageDisplayTabs()

        # Setup alignment widget if using napari for live view
        if USE_NAPARI_FOR_LIVE_VIEW and self.napariLiveWidget is not None:
            self._setup_alignment_widget()

        self.flexibleMultiPointWidget = widgets.FlexibleMultiPointWidget(
            self.stage,
            self.microscope,
            self.navigationViewer,
            self.multipointController,
            self.objectiveStore,
            self.scanCoordinates,
            self.focusMapWidget,
            self.napariMosaicDisplayWidget,
        )
        self.wellplateMultiPointWidget = widgets.WellplateMultiPointWidget(
            self.stage,
            self.microscope,
            self.navigationViewer,
            self.multipointController,
            self.liveController,
            self.objectiveStore,
            self.scanCoordinates,
            self.focusMapWidget,
            self.napariMosaicDisplayWidget,
            tab_widget=self.recordTabWidget,
            well_selection_widget=self.wellSelectionWidget,
        )
        if USE_TEMPLATE_MULTIPOINT:
            self.templateMultiPointWidget = TemplateMultiPointWidget(
                self.stage,
                self.microscope,
                self.navigationViewer,
                self.multipointController,
                self.objectiveStore,
                self.scanCoordinates,
                self.focusMapWidget,
            )
        self.multiPointWithFluidicsWidget = widgets.MultiPointWithFluidicsWidget(
            self.stage,
            self.microscope,
            self.navigationViewer,
            self.multipointController,
            self.objectiveStore,
            self.scanCoordinates,
            self.napariMosaicDisplayWidget,
        )
        self.sampleSettingsWidget = widgets.SampleSettingsWidget(self.objectivesWidget, self.wellplateFormatWidget)

        if ENABLE_TRACKING:
            self.trackingControlWidget = widgets.TrackingControllerWidget(
                self.trackingController,
                self.objectiveStore,
                show_configurations=TRACKING_SHOW_MICROSCOPE_CONFIGURATIONS,
            )

        self.setupRecordTabWidget()
        self.setupCameraTabWidget()
        self._restore_cached_illumination_settings()

    def _restore_cached_illumination_settings(self) -> None:
        """Restore cached illumination intensities and logical on/off; hardware stays dark until live."""
        cached = control.illumination_settings_cache.load_illumination_settings()
        if not cached:
            return
        ic = getattr(self.microscope, "illumination_controller", None)
        if ic is None:
            return
        try:
            ic.restore(cached.snapshot, force_hardware=False)
            if cached.led_matrix_mode and getattr(ic, "set_led_matrix_mode", None):
                ic.set_led_matrix_mode(cached.led_matrix_mode)
        except Exception as e:
            self.log.warning("Could not restore cached illumination settings: %s", e)
        if getattr(self, "illuminationWidget", None) is not None:
            try:
                self.illuminationWidget._refresh_from_state()
            except Exception as e:
                self.log.warning("Could not refresh illumination widget after cache restore: %s", e)

    def _sync_camera_ui_from_observation_state(self) -> None:
        """Sync camera settings UI with the active observation state from general.yaml.

        Called once after load_widgets so the exposure/gain spinboxes reflect the
        values that LiveControlWidget.__init__ applied to camera hardware.
        """
        config = getattr(self.liveControlWidget, "currentConfiguration", None)
        if config is None or config.camera_settings is None:
            return
        csw = self.cameraSettingWidget
        if csw is None:
            return
        try:
            csw.entry_exposureTime.blockSignals(True)
            csw.entry_exposureTime.setValue(config.camera_settings.exposure_time_ms)
            csw.entry_exposureTime.blockSignals(False)
        except Exception as e:
            self.log.warning("Could not sync exposure time UI: %s", e)
        try:
            csw.entry_analogGain.blockSignals(True)
            csw.entry_analogGain.setValue(config.camera_settings.gain_mode)
            csw.entry_analogGain.blockSignals(False)
        except Exception as e:
            self.log.warning("Could not sync analog gain UI: %s", e)

    def _restore_cached_camera_settings(self) -> None:
        """Restore cached camera settings from disk and update UI widgets.

        Applies both hardware settings (via camera API) and synchronizes the UI
        dropdown widgets. Silently returns if no cached settings exist.
        Errors are logged but do not prevent application startup.
        """
        cached_settings = squid.camera.settings_cache.load_camera_settings()
        if not cached_settings:
            return

        binning_restored = self._restore_binning(cached_settings.binning)

        # Prefer restoring high-level camera modes when the driver exposes them
        # (e.g., Photometrics and Tucsen), and fall back to pixel format for
        # legacy cameras.
        camera_mode_restored = False
        if getattr(cached_settings, "camera_mode", None):
            camera_mode_restored = self._restore_camera_mode(cached_settings.camera_mode)

        pixel_format_restored = False
        if not camera_mode_restored and getattr(cached_settings, "pixel_format", None):
            pixel_format_restored = self._restore_pixel_format(cached_settings.pixel_format)

        if binning_restored or camera_mode_restored or pixel_format_restored:
            self.log.info(
                "Restored camera settings: "
                f"binning={cached_settings.binning}, "
                f"camera_mode={getattr(cached_settings, 'camera_mode', None)}, "
                f"pixel_format={getattr(cached_settings, 'pixel_format', None)}"
            )

    def _restore_binning(self, binning: Tuple[int, int]) -> bool:
        """Apply binning setting to camera and sync UI dropdown.

        Returns True if successfully applied, False otherwise.
        """
        try:
            self.camera.set_binning(*binning)
        except ValueError as e:
            self.log.warning(f"Cannot restore binning {binning} - not supported by camera: {e}")
            return False
        except (AttributeError, RuntimeError) as e:
            self.log.error(f"Camera error while restoring binning settings: {e}")
            return False

        binning_text = f"{binning[0]}x{binning[1]}"
        self.cameraSettingWidget.dropdown_binning.blockSignals(True)
        self.cameraSettingWidget.dropdown_binning.setCurrentText(binning_text)
        self.cameraSettingWidget.dropdown_binning.blockSignals(False)
        return True

    def _restore_camera_mode(self, mode_name: Optional[str]) -> bool:
        """Apply camera mode setting (Photometrics / Tucsen-style) and sync UI dropdown.

        Returns True if successfully applied, False otherwise.
        """
        if not mode_name:
            return False

        # Only attempt restore when the camera exposes the camera-mode API.
        if not hasattr(self.camera, "set_camera_mode") or not hasattr(self.camera, "get_available_camera_modes"):
            return False

        try:
            available_modes = set(self.camera.get_available_camera_modes())  # type: ignore[attr-defined]
        except Exception as e:
            self.log.error(f"Camera error while querying available camera modes: {e}")
            return False

        if mode_name not in available_modes:
            self.log.warning(
                f"Cached camera mode '{mode_name}' is not available on this camera. "
                f"Available modes: {sorted(available_modes)}"
            )
            return False

        try:
            self.camera.set_camera_mode(mode_name)  # type: ignore[attr-defined]
        except (ValueError, AttributeError, RuntimeError) as e:
            self.log.error(f"Camera error while restoring camera mode '{mode_name}': {e}")
            return False

        # Sync the camera-mode dropdown with the restored value.
        self.cameraSettingWidget.dropdown_cameraMode.blockSignals(True)
        self.cameraSettingWidget.dropdown_cameraMode.setCurrentText(mode_name)
        self.cameraSettingWidget.dropdown_cameraMode.blockSignals(False)
        return True

    def _restore_pixel_format(self, pixel_format_str: Optional[str]) -> bool:
        """Apply pixel format setting to camera and sync UI dropdown.

        This is kept for backwards compatibility and for cameras that do not yet
        expose a high-level camera-mode API.

        Returns True if successfully applied, False otherwise.
        """
        if not pixel_format_str:
            return False

        try:
            pixel_format = squid.config.CameraPixelFormat.from_string(pixel_format_str)
        except KeyError:
            self.log.warning(f"Cached pixel format '{pixel_format_str}' is not recognized")
            return False

        try:
            self.camera.set_pixel_format(pixel_format)
        except ValueError as e:
            self.log.warning(f"Cannot restore pixel format {pixel_format_str} - not supported by this camera: {e}")
            return False
        except (AttributeError, RuntimeError, NotImplementedError) as e:
            self.log.error(f"Camera error while restoring pixel format settings: {e}")
            return False

        # For legacy cameras where "camera mode" is effectively just pixel
        # format, keep the dropdown text in sync with the pixel-format name.
        self.cameraSettingWidget.dropdown_cameraMode.blockSignals(True)
        self.cameraSettingWidget.dropdown_cameraMode.setCurrentText(pixel_format_str)
        self.cameraSettingWidget.dropdown_cameraMode.blockSignals(False)
        return True

    def setupImageDisplayTabs(self):
        if USE_NAPARI_FOR_LIVE_VIEW:
            self.napariLiveWidget = widgets.NapariLiveWidget(
                self.streamHandler,
                self.liveController,
                self.stage,
                self.objectiveStore,
                self.contrastManager,
                self.wellSelectionWidget,
            )
            self.imageDisplayTabs.addTab(self.napariLiveWidget, "Live View")
        else:
            if ENABLE_TRACKING:
                self.imageDisplayWindow = core.ImageDisplayWindow(self.liveController, self.contrastManager)
                self.imageDisplayWindow.show_ROI_selector()
            else:
                self.imageDisplayWindow = core.ImageDisplayWindow(
                    self.liveController, self.contrastManager, show_LUT=True, autoLevels=True
                )
            self.imageDisplayTabs.addTab(self.imageDisplayWindow.widget, "Live View")

        if not self.live_only_mode:
            self.napariMultiChannelWidget = widgets.NapariMultiChannelWidget(
                self.objectiveStore, self.camera, self.contrastManager
            )
            self.imageDisplayTabs.addTab(self.napariMultiChannelWidget, "Multichannel Acquisition")

            self.napariMosaicDisplayWidget = None
            if USE_NAPARI_FOR_MOSAIC_DISPLAY:
                self.napariMosaicDisplayWidget = widgets.NapariMosaicDisplayWidget(
                    self.objectiveStore, self.camera, self.contrastManager
                )
                self.imageDisplayTabs.addTab(self.napariMosaicDisplayWidget, "Mosaic View")

            # Plate view for well-based acquisitions (independent of mosaic view)
            self.napariPlateViewWidget = None
            if DISPLAY_PLATE_VIEW:
                self.napariPlateViewWidget = widgets.NapariPlateViewWidget(self.contrastManager)
                self.imageDisplayTabs.addTab(self.napariPlateViewWidget, "Plate View")

            # Embedded NDViewer (lightweight) - initialized AFTER napari widgets because
            # NDV and napari both use vispy for OpenGL rendering. Initializing NDV first
            # can cause OpenGL context conflicts since both libraries share vispy state.
            self.ndviewerTab = None
            if control._def.ENABLE_NDVIEWER:
                try:
                    self.ndviewerTab = widgets.NDViewerTab()
                    self.imageDisplayTabs.addTab(self.ndviewerTab, "NDViewer")
                except ImportError:
                    self.log.warning("NDViewer tab unavailable: ndviewer_light module not installed")
                except (RuntimeError, OSError) as e:
                    self.log.exception(f"Failed to initialize NDViewer tab due to system error: {e}")
                except Exception:
                    self.log.exception("Failed to initialize NDViewer tab - unexpected error")

            # Connect plate view double-click to NDViewer navigation and tab switch
            if self.napariPlateViewWidget is not None and self.ndviewerTab is not None:
                self.napariPlateViewWidget.signal_well_fov_clicked.connect(self._on_plate_view_fov_clicked)
            elif self.napariPlateViewWidget is None:
                self.log.debug("Plate view not available, FOV click navigation disabled")
            elif self.ndviewerTab is None:
                self.log.debug("NDViewer tab not available, FOV click navigation disabled")

            # z plot
            self.zPlotWidget = widgets.SurfacePlotWidget()
            dock_surface_plot = dock.Dock("Z Plot", autoOrientation=False)
            dock_surface_plot.showTitleBar()
            dock_surface_plot.addWidget(self.zPlotWidget)
            dock_surface_plot.setStretch(x=100, y=100)

            surface_plot_dockArea = dock.DockArea()
            surface_plot_dockArea.addDock(dock_surface_plot)

            self.imageDisplayTabs.addTab(surface_plot_dockArea, "Plots")

            # Connect the point clicked signal to move the stage
            self.zPlotWidget.signal_point_clicked.connect(self.move_to_mm)

        if self.microscope.addons.camera_focus:
            dock_laserfocus_image_display = dock.Dock("Focus Camera Image Display", autoOrientation=False)
            dock_laserfocus_image_display.showTitleBar()
            dock_laserfocus_image_display.addWidget(self.imageDisplayWindow_focus.widget)
            dock_laserfocus_image_display.setStretch(x=100, y=100)

            dock_laserfocus_liveController = dock.Dock("Laser Autofocus Settings", autoOrientation=False)
            dock_laserfocus_liveController.showTitleBar()
            dock_laserfocus_liveController.addWidget(self.laserAutofocusSettingWidget)
            dock_laserfocus_liveController.setStretch(x=100, y=100)
            dock_laserfocus_liveController.setFixedWidth(self.laserAutofocusSettingWidget.minimumSizeHint().width())

            dock_waveform = dock.Dock("Displacement Measurement", autoOrientation=False)
            dock_waveform.showTitleBar()
            dock_waveform.addWidget(self.waveformDisplay)
            dock_waveform.setStretch(x=100, y=40)

            dock_displayMeasurement = dock.Dock("Displacement Measurement Control", autoOrientation=False)
            dock_displayMeasurement.showTitleBar()
            dock_displayMeasurement.addWidget(self.displacementMeasurementWidget)
            dock_displayMeasurement.setStretch(x=100, y=40)
            dock_displayMeasurement.setFixedWidth(self.displacementMeasurementWidget.minimumSizeHint().width())

            laserfocus_dockArea = dock.DockArea()
            laserfocus_dockArea.addDock(dock_laserfocus_image_display)
            laserfocus_dockArea.addDock(
                dock_laserfocus_liveController, "right", relativeTo=dock_laserfocus_image_display
            )
            if SHOW_LEGACY_DISPLACEMENT_MEASUREMENT_WINDOWS:
                laserfocus_dockArea.addDock(dock_waveform, "bottom", relativeTo=dock_laserfocus_liveController)
                laserfocus_dockArea.addDock(dock_displayMeasurement, "bottom", relativeTo=dock_waveform)

            self.imageDisplayTabs.addTab(laserfocus_dockArea, self.LASER_BASED_FOCUS_TAB_NAME)

        if RUN_FLUIDICS:
            self.imageDisplayTabs.addTab(self.fluidicsWidget, "Fluidics")

        # Only add NI DAQ tab if the widget was created (nidaq enabled in MachineConfig)
        if hasattr(self, "niDAQWidget") and self.niDAQWidget is not None:
            self.imageDisplayTabs.addTab(self.niDAQWidget, "NI DAQ")

    def setupRecordTabWidget(self):
        if ENABLE_WELLPLATE_MULTIPOINT:
            self.recordTabWidget.addTab(self.wellplateMultiPointWidget, "Wellplate Multipoint")
        if ENABLE_FLEXIBLE_MULTIPOINT:
            self.recordTabWidget.addTab(self.flexibleMultiPointWidget, "Flexible Multipoint")
        if USE_TEMPLATE_MULTIPOINT:
            self.recordTabWidget.addTab(self.templateMultiPointWidget, "Template Multipoint")
        if RUN_FLUIDICS:
            self.recordTabWidget.addTab(self.multiPointWithFluidicsWidget, "Multipoint with Fluidics")
        if ENABLE_TRACKING:
            self.recordTabWidget.addTab(self.trackingControlWidget, "Tracking")
        if ENABLE_RECORDING:
            self.recordTabWidget.addTab(self.recordingControlWidget, "Simple Recording")
        # Only add Fast Acquisition tab if the widget was created (fast_acquisition enabled in MachineConfig)
        if hasattr(self, "fastAcquisitionWidget") and self.fastAcquisitionWidget is not None:
            self.recordTabWidget.addTab(self.fastAcquisitionWidget, "Fast Acquisition")
        self.recordTabWidget.currentChanged.connect(lambda: self.resizeCurrentTab(self.recordTabWidget))
        self.resizeCurrentTab(self.recordTabWidget)

    def _setup_alignment_widget(self):
        """Setup alignment widget and connect to navigation viewer and multipoint controller."""
        if self.napariLiveWidget is None:
            self.log.warning("Cannot setup alignment widget: napariLiveWidget not available")
            return

        self.alignmentWidget = widgets.AlignmentWidget(
            napari_viewer=self.napariLiveWidget.viewer,
            parent=None,
        )

        self.alignmentWidget.signal_move_to_position.connect(self._alignment_move_to)
        self.alignmentWidget.signal_request_current_position.connect(self._alignment_provide_position)
        self.alignmentWidget.signal_offset_set.connect(
            lambda x, y: self.log.info(f"Alignment offset active: ({x:.4f}, {y:.4f})mm")
        )
        self.alignmentWidget.signal_offset_cleared.connect(lambda: self.log.info("Alignment offset cleared"))

        self.multipointController.set_alignment_widget(self.alignmentWidget)
        self.navigationViewer.set_alignment_widget(self.alignmentWidget)
        self.log.info("Alignment widget setup complete")

    def _alignment_move_to(self, x_mm: float, y_mm: float):
        """Handle alignment widget request to move stage."""
        self.stage.move_x_to(x_mm)
        self.stage.move_y_to(y_mm)

    def _alignment_provide_position(self):
        """Provide current stage position to alignment widget."""
        pos = self.stage.get_pos()
        self.alignmentWidget.set_current_position(pos.x_mm, pos.y_mm)

    def setupCameraTabWidget(self):
        # Camera + autofocus are grouped into a tabbed block. Default tab on startup:
        # "Camera".
        camera_autofocus_tabs = QTabWidget()
        camera_autofocus_tabs.setSizePolicy(QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding))

        # Camera tab (default)
        camera_tab = QWidget()
        camera_tab_layout = QVBoxLayout()
        camera_tab_layout.setContentsMargins(0, 0, 0, 0)
        camera_tab_layout.setSpacing(6)
        camera_tab_layout.addWidget(self.cameraSettingWidget)
        camera_tab.setLayout(camera_tab_layout)
        camera_autofocus_tabs.addTab(camera_tab, "Camera")

        # Autofocus-related tabs
        camera_autofocus_tabs.addTab(self.autofocusWidget, "Contrast AF")
        if self.microscope.addons.camera_focus and getattr(self, "laserAutofocusControlWidget", None):
            camera_autofocus_tabs.addTab(self.laserAutofocusControlWidget, "Laser AF")
        if getattr(self, "focusMapWidget", None) is not None:
            camera_autofocus_tabs.addTab(self.focusMapWidget, "Focus Map")

        camera_autofocus_tabs.setCurrentIndex(0)

        camera_outer_layout = QVBoxLayout()
        camera_outer_layout.setContentsMargins(0, 0, 0, 0)
        camera_outer_layout.setSpacing(6)
        camera_outer_layout.addWidget(camera_autofocus_tabs)
        self.cameraTabWidget.setLayout(camera_outer_layout)

        # Illumination control (2/3 width in setup_layout)
        self.illuminationWidget = widgets.IlluminationWidget(
            self.microscope.illumination_controller,
            parent=self,
            config_repo=self.microscope.config_repo,
            live_controller=self.liveController,
            objective_store=self.objectiveStore,
            emission_filter_wheel=self.emission_filter_wheel,
            on_observation_state_changed=self._on_observation_state_changed,
        )

        # Stage controls (1/3 width in setup_layout): keep only basic controls here.
        self.stageControlsWidget = QWidget()
        stage_layout = QVBoxLayout()
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.setSpacing(6)
        stage_layout.addWidget(self.navigationWidget)
        if self.piezoWidget:
            stage_layout.addWidget(self.piezoWidget)
        self.stageControlsWidget.setLayout(stage_layout)

        # RAM monitor widget (always create, visibility controlled by setting)
        self.ramMonitorWidget = widgets.RAMMonitorWidget()
        self.ramMonitorWidget.setVisible(False)
        self._ram_monitor_should_show = False

        # Backpressure monitor widget (always create, visibility controlled during acquisition)
        self.backpressureMonitorWidget = widgets.BackpressureMonitorWidget()
        self.backpressureMonitorWidget.setVisible(False)
        self._bp_monitor_should_show = False

        # Warning/Error display widget (auto-hides when empty)
        self.warningErrorWidget = widgets.WarningErrorWidget()
        self.warningErrorWidget.setVisible(False)
        self._warning_handler = None

    def setup_layout(self):
        layout = QVBoxLayout()

        # Add warning banner if simulated disk I/O mode is enabled
        import control._def

        if control._def.SIMULATED_DISK_IO_ENABLED:
            simulated_io_banner = QLabel("  SIMULATED DISK I/O - Images are encoded but NOT saved to disk  ")
            simulated_io_banner.setStyleSheet(
                "background-color: #FF6B6B; color: white; font-weight: bold; padding: 8px;"
            )
            simulated_io_banner.setAlignment(Qt.AlignCenter)
            layout.addWidget(simulated_io_banner)

        layout.addWidget(self.profileWidget)

        # Top row: snaps/start live/autolevel on the left, camera+autofocus tabs on the right.
        top_row = QWidget()
        top_row_layout = QHBoxLayout(top_row)
        top_row_layout.setContentsMargins(0, 0, 0, 0)
        top_row_layout.setSpacing(8)
        top_row_layout.addWidget(self.liveControlWidget, stretch=1)
        top_row_layout.addWidget(self.cameraTabWidget, stretch=1)
        layout.addWidget(top_row)

        # Bottom row: illumination takes 2/3 width; stage takes 1/3 width.
        bottom_row = QWidget()
        bottom_row_layout = QHBoxLayout(bottom_row)
        bottom_row_layout.setContentsMargins(0, 0, 0, 0)
        bottom_row_layout.setSpacing(8)
        bottom_row_layout.addWidget(self.illuminationWidget, stretch=2)
        bottom_row_layout.addWidget(self.stageControlsWidget, stretch=1)
        layout.addWidget(bottom_row)

        if SHOW_DAC_CONTROL:
            layout.addWidget(self.dacControlWidget)

        # Create a widget to hold sample settings and navigation viewer
        navigation_section_widget = QWidget()
        navigation_section_layout = QVBoxLayout()
        navigation_section_layout.setContentsMargins(0, 0, 0, 0)
        navigation_section_layout.setSpacing(0)
        navigation_section_layout.addWidget(self.sampleSettingsWidget)
        navigation_section_layout.addWidget(self.navigationViewer)
        navigation_section_widget.setLayout(navigation_section_layout)

        # Create a splitter between recordTabWidget and navigation section (50/50)
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.recordTabWidget)
        splitter.addWidget(navigation_section_widget)
        splitter.setStretchFactor(0, 1)  # recordTabWidget 50%
        splitter.setStretchFactor(1, 1)  # navigation section 50%

        layout.addWidget(splitter)

        # Add performance mode toggle button at the bottom with natural height
        if not self.live_only_mode:
            self.performanceModeToggle = QPushButton("Enable Performance Mode")
            self.performanceModeToggle.setCheckable(True)
            self.performanceModeToggle.setChecked(self.performance_mode)
            self.performanceModeToggle.clicked.connect(self.togglePerformanceMode)
            layout.addWidget(self.performanceModeToggle)

        self.centralWidget = QWidget()
        self.centralWidget.setLayout(layout)
        self.centralWidget.setMinimumWidth(self.centralWidget.minimumSizeHint().width())

        self.setupSingleWindowLayout()

        # Add RAM monitor widget to left side of status bar
        # Status bar is hidden when RAM monitoring is disabled
        # Visibility update is deferred to showEvent since status bar isn't visible until window is shown
        if self.ramMonitorWidget is not None:
            self.statusBar().addWidget(self.ramMonitorWidget)  # Left-aligned

        # Add backpressure monitor widget to status bar (next to RAM monitor)
        # Only visible during acquisition when throttling is enabled
        if self.backpressureMonitorWidget is not None:
            self.statusBar().addWidget(self.backpressureMonitorWidget)  # Left-aligned

        # Add warning/error display widget to status bar
        # Auto-hides when no messages pending
        if self.warningErrorWidget is not None:
            self.statusBar().addWidget(self.warningErrorWidget)  # Left-aligned

    def _getMainWindowMinimumSize(self):
        """
        We want our main window to fit on the primary screen, so grab the users primary screen and return
        something slightly smaller than that.
        """
        desktop_info = QDesktopWidget()
        primary_screen_size = desktop_info.screen(desktop_info.primaryScreen()).size()

        height_min = int(0.9 * primary_screen_size.height())
        width_min = int(0.96 * primary_screen_size.width())

        return (width_min, height_min)

    def setupSingleWindowLayout(self):
        main_dockArea = dock.DockArea()

        dock_display = dock.Dock("Image Display", autoOrientation=False)
        dock_display.showTitleBar()
        dock_display.addWidget(self.imageDisplayTabs)
        dock_display.setStretch(x=3, y=100)
        main_dockArea.addDock(dock_display)

        self.dock_wellSelection = dock.Dock("Well Selector", autoOrientation=False)
        self.dock_wellSelection.showTitleBar()
        if not USE_NAPARI_WELL_SELECTION or self.live_only_mode:
            self.dock_wellSelection.addWidget(self.wellSelectionWidget)
            self.dock_wellSelection.setFixedHeight(self.dock_wellSelection.minimumSizeHint().height())
            main_dockArea.addDock(self.dock_wellSelection, "bottom")

        dock_controlPanel = dock.Dock("Controls", autoOrientation=False)
        dock_controlPanel.addWidget(self.centralWidget)
        dock_controlPanel.setStretch(x=1, y=100)
        main_dockArea.addDock(dock_controlPanel, "right")
        self.setCentralWidget(main_dockArea)

        self.setMinimumSize(*self._getMainWindowMinimumSize())
        self.onTabChanged(self.recordTabWidget.currentIndex())

    def make_connections(self):
        self.streamHandler.signal_new_frame_received.connect(self.liveController.on_new_frame)
        self.streamHandler.packet_image_to_write.connect(self.imageSaver.enqueue)

        if ENABLE_FLEXIBLE_MULTIPOINT:
            self.flexibleMultiPointWidget.signal_acquisition_started.connect(self.toggleAcquisitionStart)
            self.signal_performance_mode_changed.connect(self.flexibleMultiPointWidget.set_performance_mode)

        if ENABLE_WELLPLATE_MULTIPOINT:
            self.wellplateMultiPointWidget.signal_acquisition_started.connect(self.toggleAcquisitionStart)
            self.wellplateMultiPointWidget.signal_toggle_live_scan_grid.connect(self.toggle_live_scan_grid)
            self.signal_performance_mode_changed.connect(self.wellplateMultiPointWidget.set_performance_mode)

        if RUN_FLUIDICS:
            self.multiPointWithFluidicsWidget.signal_acquisition_started.connect(self.toggleAcquisitionStart)
            self.multiPointWithFluidicsWidget.signal_acquisition_started.connect(
                self.fluidicsWidget.set_acquisition_running
            )
            self.fluidicsWidget.fluidics_initialized_signal.connect(self.multiPointWithFluidicsWidget.init_fluidics)
            self.signal_performance_mode_changed.connect(self.multiPointWithFluidicsWidget.set_performance_mode)

        self.recordingControlWidget.signal_acquisition_started.connect(self.toggleAcquisitionStart)

        self.profileWidget.signal_profile_changed.connect(self.liveControlWidget.refresh_mode_list)
        self.profileWidget.signal_profile_changed.connect(self.illuminationWidget.refresh_observation_state_presets)

        self.liveControlWidget.signal_newExposureTime.connect(self.cameraSettingWidget.set_exposure_time)
        self.liveControlWidget.signal_newAnalogGain.connect(self.cameraSettingWidget.set_analog_gain)
        if not self.live_only_mode:
            self.liveControlWidget.signal_start_live.connect(self.onStartLive)
        # LiveControlWidget no longer owns exposure/gain/trigger/illumination for manual live.

        self.connectSlidePositionController()

        self.navigationViewer.signal_coordinates_clicked.connect(self.move_from_click_mm)
        self.objectivesWidget.signal_objective_changed.connect(self.navigationViewer.redraw_fov)
        self.cameraSettingWidget.signal_binning_changed.connect(self.navigationViewer.redraw_fov)
        if ENABLE_FLEXIBLE_MULTIPOINT:
            self.objectivesWidget.signal_objective_changed.connect(self.flexibleMultiPointWidget.update_fov_positions)
        # TODO(imo): Fix position updates after removal of navigation controller
        self.movement_updater.position_after_move.connect(self.navigationViewer.draw_fov_current_location)
        self.multipointController.signal_register_current_fov.connect(self.navigationViewer.register_fov)
        self.multipointController.signal_current_configuration.connect(self.liveControlWidget.update_ui_for_mode)
        self.multipointController.signal_current_configuration.connect(self.illuminationWidget.update_ui_for_mode)
        if self.piezoWidget:
            self.movement_updater.piezo_z_um.connect(self.piezoWidget.update_displacement_um_display)
        self.multipointController.signal_set_display_tabs.connect(self.setAcquisitionDisplayTabs)

        # RAM monitor widget connections - use controller signals which fire AFTER memory monitor is created
        self.multipointController.signal_acquisition_start.connect(self._connect_ram_monitor_widget)
        self.multipointController.acquisition_finished.connect(self._disconnect_ram_monitor_widget)

        # Backpressure monitor widget connections - fires AFTER worker is created
        self.multipointController.signal_acquisition_start.connect(self._connect_backpressure_monitor_widget)
        self.multipointController.acquisition_finished.connect(self._disconnect_backpressure_monitor_widget)

        # NDViewer push-based API connections
        if self.ndviewerTab is not None:
            # TIFF mode signals
            self.multipointController.ndviewer_start_acquisition.connect(self.ndviewerTab.start_acquisition)
            self.multipointController.ndviewer_register_image.connect(self.ndviewerTab.register_image)
            self.multipointController.acquisition_finished.connect(self.ndviewerTab.end_acquisition)
            # Zarr mode signals
            self.multipointController.ndviewer_start_zarr_acquisition.connect(self.ndviewerTab.start_zarr_acquisition)
            self.multipointController.ndviewer_start_zarr_acquisition_6d.connect(
                self.ndviewerTab.start_zarr_acquisition_6d
            )
            self.multipointController.ndviewer_notify_zarr_frame.connect(self.ndviewerTab.notify_zarr_frame)
            self.multipointController.ndviewer_end_zarr_acquisition.connect(self.ndviewerTab.end_zarr_acquisition)

        self.recordTabWidget.currentChanged.connect(self.onTabChanged)
        if not self.live_only_mode:
            self.imageDisplayTabs.currentChanged.connect(self.onDisplayTabChanged)

        if USE_NAPARI_FOR_LIVE_VIEW and not self.live_only_mode:
            self.multipointController.signal_current_configuration.connect(self.napariLiveWidget.update_ui_for_mode)
            self.autofocusController.image_to_display.connect(
                lambda image: self.napariLiveWidget.updateLiveLayer(image, from_autofocus=True)
            )
            self.streamHandler.image_to_display.connect(
                lambda image: self.napariLiveWidget.updateLiveLayer(image, from_autofocus=False)
            )
            self.multipointController.image_to_display.connect(
                lambda image: self.napariLiveWidget.updateLiveLayer(image, from_autofocus=False)
            )
            self.napariLiveWidget.signal_coordinates_clicked.connect(self.move_from_click_image)
            self.liveControlWidget.signal_live_configuration.connect(self.napariLiveWidget.set_live_configuration)

            if USE_NAPARI_FOR_LIVE_CONTROL:
                self.napariLiveWidget.signal_newExposureTime.connect(self.cameraSettingWidget.set_exposure_time)
                self.napariLiveWidget.signal_newAnalogGain.connect(self.cameraSettingWidget.set_analog_gain)
                self.napariLiveWidget.signal_autoLevelSetting.connect(self.imageDisplayWindow.set_autolevel)
        else:
            self.streamHandler.image_to_display.connect(self.imageDisplay.enqueue)
            self.imageDisplay.image_to_display.connect(self.imageDisplayWindow.display_image)
            self.autofocusController.image_to_display.connect(self.imageDisplayWindow.display_image)
            self.multipointController.image_to_display.connect(self.imageDisplayWindow.display_image)
            self.liveControlWidget.signal_autoLevelSetting.connect(self.imageDisplayWindow.set_autolevel)
            self.imageDisplayWindow.image_click_coordinates.connect(self.move_from_click_image)

        self.makeNapariConnections()

        self.wellplateFormatWidget.signalWellplateSettings.connect(self.navigationViewer.update_wellplate_settings)
        self.wellplateFormatWidget.signalWellplateSettings.connect(self.scanCoordinates.update_wellplate_settings)
        self.wellplateFormatWidget.signalWellplateSettings.connect(self.wellSelectionWidget.onWellplateChanged)
        self.wellplateFormatWidget.signalWellplateSettings.connect(
            lambda format_, *args: self.onWellplateChanged(format_)
        )

        self.wellSelectionWidget.signal_wellSelectedPos.connect(self.move_to_mm)
        if ENABLE_WELLPLATE_MULTIPOINT:
            self.wellSelectionWidget.signal_wellSelected.connect(self.wellplateMultiPointWidget.update_well_coordinates)
            self.objectivesWidget.signal_objective_changed.connect(
                self.wellplateMultiPointWidget.handle_objective_change
            )

        self.profileWidget.signal_profile_changed.connect(
            lambda: self.liveControlWidget.select_new_microscope_mode_by_name(
                self.liveControlWidget.currentConfiguration.name
            )
        )
        self.objectivesWidget.signal_objective_changed.connect(
            lambda: self.liveControlWidget.select_new_microscope_mode_by_name(
                self.liveControlWidget.currentConfiguration.name
            )
        )

        if self.microscope.addons.camera_focus:
            self.log.info(f"laser autofocus controller: {self.laserAutofocusController}, camera: {self.camera_focus}, setting up connections")

            def slot_settings_changed_laser_af():
                self.laserAutofocusController.on_settings_changed()
                self.laserAutofocusControlWidget.update_init_state()
                self.laserAutofocusSettingWidget.update_values()

            self.profileWidget.signal_profile_changed.connect(slot_settings_changed_laser_af)
            self.objectivesWidget.signal_objective_changed.connect(slot_settings_changed_laser_af)
            self.laserAutofocusSettingWidget.signal_newExposureTime.connect(
                self.cameraSettingWidget_focus_camera.set_exposure_time
            )
            self.laserAutofocusSettingWidget.signal_newAnalogGain.connect(
                self.cameraSettingWidget_focus_camera.set_analog_gain
            )
            self.laserAutofocusSettingWidget.signal_apply_settings.connect(
                self.laserAutofocusControlWidget.update_init_state
            )
            self.laserAutofocusSettingWidget.signal_laser_spot_location.connect(self.imageDisplayWindow_focus.mark_spot)
            self.laserAutofocusSettingWidget.update_exposure_time(
                self.laserAutofocusSettingWidget.exposure_spinbox.value()
            )
            self.laserAutofocusSettingWidget.update_analog_gain(
                self.laserAutofocusSettingWidget.analog_gain_spinbox.value()
            )
            self.laserAutofocusController.signal_cross_correlation.connect(
                self.laserAutofocusSettingWidget.show_cross_correlation_result
            )

            self.streamHandler_focus_camera.signal_new_frame_received.connect(
                self.liveController_focus_camera.on_new_frame
            )
            self.streamHandler_focus_camera.image_to_display.connect(self.imageDisplayWindow_focus.display_image)

            self.streamHandler_focus_camera.image_to_display.connect(
                self.displacementMeasurementController.update_measurement
            )
            self.displacementMeasurementController.signal_plots.connect(self.waveformDisplay.plot)
            self.displacementMeasurementController.signal_readings.connect(
                self.displacementMeasurementWidget.display_readings
            )
            self.laserAutofocusController.image_to_display.connect(self.imageDisplayWindow_focus.display_image)

            # Add connection for piezo position updates
            if self.piezoWidget:
                self.laserAutofocusController.signal_piezo_position_update.connect(
                    self.piezoWidget.update_displacement_um_display
                )

        if ENABLE_SPINNING_DISK_CONFOCAL:
            self.spinningDiskConfocalWidget.signal_toggle_confocal_widefield.connect(
                self.liveController.toggle_confocal_widefield
            )
            self.spinningDiskConfocalWidget.signal_toggle_confocal_widefield.connect(
                lambda: self.liveControlWidget.select_new_microscope_mode_by_name(
                    self.liveControlWidget.currentConfiguration.name
                )
            )
            # Update iris UI when channel changes
            self.liveControlWidget.signal_live_configuration.connect(
                self.spinningDiskConfocalWidget.update_iris_from_config
            )
            # Save iris values to config when changed (persistence through LiveControlWidget)
            self.spinningDiskConfocalWidget.signal_illumination_iris_changed.connect(
                self.liveControlWidget.update_config_illumination_iris
            )
            self.spinningDiskConfocalWidget.signal_emission_iris_changed.connect(
                self.liveControlWidget.update_config_emission_iris
            )
            # Sync iris UI from the initial channel config (signal wasn't connected during __init__)
            if self.liveControlWidget.currentConfiguration:
                self.spinningDiskConfocalWidget.update_iris_from_config(self.liveControlWidget.currentConfiguration)

        # Connect to plot xyz data when coordinates are saved
        self.multipointController.signal_coordinates.connect(self.zPlotWidget.add_point)

        def plot_after_each_region(current_region: int, total_regions: int, current_timepoint: int):
            if current_region > 1:
                self.zPlotWidget.plot()
            self.zPlotWidget.clear()

        self.multipointController.signal_acquisition_progress.connect(plot_after_each_region)
        # Since we don't get a region progress call after the last, make sure there's one last plot for
        # the final region.
        self.multipointController.acquisition_finished.connect(self.zPlotWidget.plot)

        # Connect well selector button
        if hasattr(self.imageDisplayWindow, "btn_well_selector"):
            self.imageDisplayWindow.btn_well_selector.clicked.connect(
                lambda: self.toggleWellSelector(not self.dock_wellSelection.isVisible())
            )

    def setup_movement_updater(self):
        # We provide a few signals about the system's physical movement to other parts of the UI.  Ideally, they other
        # parts would register their interest (instead of us needing to know that they want to hear about the movements
        # here), but as an intermediate pumping it all from one location is better than nothing.
        self.movement_updater = MovementUpdater(stage=self.stage, piezo=self.piezo)
        self.movement_update_timer = QTimer()
        self.movement_update_timer.setInterval(100)
        self.movement_update_timer.timeout.connect(self.movement_updater.do_update)
        self.movement_update_timer.start()

    def makeNapariConnections(self):
        """Initialize all Napari connections in one place"""
        self.napari_connections = {
            "napariLiveWidget": [],
            "napariMultiChannelWidget": [],
            "napariMosaicDisplayWidget": [],
        }

        # Setup live view connections
        if USE_NAPARI_FOR_LIVE_VIEW and not self.live_only_mode:
            self.napari_connections["napariLiveWidget"] = [
                (self.multipointController.signal_current_configuration, self.napariLiveWidget.update_ui_for_mode),
                (
                    self.autofocusController.image_to_display,
                    lambda image: self.napariLiveWidget.updateLiveLayer(image, from_autofocus=True),
                ),
                (
                    self.streamHandler.image_to_display,
                    lambda image: self.napariLiveWidget.updateLiveLayer(image, from_autofocus=False),
                ),
                (
                    self.multipointController.image_to_display,
                    lambda image: self.napariLiveWidget.updateLiveLayer(image, from_autofocus=False),
                ),
                (self.napariLiveWidget.signal_coordinates_clicked, self.move_from_click_image),
                (self.liveControlWidget.signal_live_configuration, self.napariLiveWidget.set_live_configuration),
            ]

            if USE_NAPARI_FOR_LIVE_CONTROL:
                self.napari_connections["napariLiveWidget"].extend(
                    [
                        (self.napariLiveWidget.signal_newExposureTime, self.cameraSettingWidget.set_exposure_time),
                        (self.napariLiveWidget.signal_newAnalogGain, self.cameraSettingWidget.set_analog_gain),
                        (self.napariLiveWidget.signal_autoLevelSetting, self.imageDisplayWindow.set_autolevel),
                    ]
                )
        else:
            # Non-Napari display connections
            self.streamHandler.image_to_display.connect(self.imageDisplay.enqueue)
            self.imageDisplay.image_to_display.connect(self.imageDisplayWindow.display_image)
            self.autofocusController.image_to_display.connect(self.imageDisplayWindow.display_image)
            self.multipointController.image_to_display.connect(self.imageDisplayWindow.display_image)
            self.liveControlWidget.signal_autoLevelSetting.connect(self.imageDisplayWindow.set_autolevel)
            self.imageDisplayWindow.image_click_coordinates.connect(self.move_from_click_image)

        if not self.live_only_mode:
            # Setup multichannel widget connections
            self.napari_connections["napariMultiChannelWidget"] = [
                (self.multipointController.napari_layers_init, self.napariMultiChannelWidget.initLayers),
                (self.multipointController.napari_layers_update, self.napariMultiChannelWidget.updateLayers),
            ]

            if ENABLE_FLEXIBLE_MULTIPOINT:
                self.napari_connections["napariMultiChannelWidget"].extend(
                    [
                        (
                            self.flexibleMultiPointWidget.signal_acquisition_channels,
                            self.napariMultiChannelWidget.initChannels,
                        ),
                        (
                            self.flexibleMultiPointWidget.signal_acquisition_shape,
                            self.napariMultiChannelWidget.initLayersShape,
                        ),
                    ]
                )

            if ENABLE_WELLPLATE_MULTIPOINT:
                self.napari_connections["napariMultiChannelWidget"].extend(
                    [
                        (
                            self.wellplateMultiPointWidget.signal_acquisition_channels,
                            self.napariMultiChannelWidget.initChannels,
                        ),
                        (
                            self.wellplateMultiPointWidget.signal_acquisition_shape,
                            self.napariMultiChannelWidget.initLayersShape,
                        ),
                    ]
                )
            if RUN_FLUIDICS:
                self.napari_connections["napariMultiChannelWidget"].extend(
                    [
                        (
                            self.multiPointWithFluidicsWidget.signal_acquisition_channels,
                            self.napariMultiChannelWidget.initChannels,
                        ),
                        (
                            self.multiPointWithFluidicsWidget.signal_acquisition_shape,
                            self.napariMultiChannelWidget.initLayersShape,
                        ),
                    ]
                )

            # Setup mosaic display widget connections
            if USE_NAPARI_FOR_MOSAIC_DISPLAY:
                self.napari_connections["napariMosaicDisplayWidget"] = [
                    (self.multipointController.napari_layers_update, self.napariMosaicDisplayWidget.updateMosaic),
                    (self.napariMosaicDisplayWidget.signal_coordinates_clicked, self.move_from_click_mm),
                    (self.napariMosaicDisplayWidget.signal_clear_viewer, self.navigationViewer.clear_slide),
                ]

                if ENABLE_FLEXIBLE_MULTIPOINT:
                    self.napari_connections["napariMosaicDisplayWidget"].extend(
                        [
                            (
                                self.flexibleMultiPointWidget.signal_acquisition_channels,
                                self.napariMosaicDisplayWidget.initChannels,
                            ),
                            (
                                self.flexibleMultiPointWidget.signal_acquisition_shape,
                                self.napariMosaicDisplayWidget.initLayersShape,
                            ),
                        ]
                    )

                if ENABLE_WELLPLATE_MULTIPOINT:
                    self.napari_connections["napariMosaicDisplayWidget"].extend(
                        [
                            (
                                self.wellplateMultiPointWidget.signal_acquisition_channels,
                                self.napariMosaicDisplayWidget.initChannels,
                            ),
                            (
                                self.wellplateMultiPointWidget.signal_acquisition_shape,
                                self.napariMosaicDisplayWidget.initLayersShape,
                            ),
                            (
                                self.wellplateMultiPointWidget.signal_manual_shape_mode,
                                self.napariMosaicDisplayWidget.enable_shape_drawing,
                            ),
                            (
                                self.napariMosaicDisplayWidget.signal_shape_drawn,
                                self.wellplateMultiPointWidget.update_manual_shape,
                            ),
                        ]
                    )

                if RUN_FLUIDICS:
                    self.napari_connections["napariMosaicDisplayWidget"].extend(
                        [
                            (
                                self.multiPointWithFluidicsWidget.signal_acquisition_channels,
                                self.napariMosaicDisplayWidget.initChannels,
                            ),
                            (
                                self.multiPointWithFluidicsWidget.signal_acquisition_shape,
                                self.napariMosaicDisplayWidget.initLayersShape,
                            ),
                        ]
                    )

            # Setup plate view widget connections (independent of mosaic display)
            # Use Qt.QueuedConnection explicitly for thread safety since these signals
            # are emitted from the acquisition worker thread and received on the main thread.
            # This ensures the slot is invoked in the receiver's thread event loop.
            if self.napariPlateViewWidget is not None:
                self.napari_connections["napariPlateViewWidget"] = [
                    (
                        self.multipointController.plate_view_init,
                        self.napariPlateViewWidget.initPlateLayout,
                        Qt.QueuedConnection,
                    ),
                    (
                        self.multipointController.plate_view_update,
                        self.napariPlateViewWidget.updatePlateView,
                        Qt.QueuedConnection,
                    ),
                ]

            # Make initial connections
            self.updateNapariConnections()

    def updateNapariConnections(self):
        # Update Napari connections based on performance mode. Live widget connections are preserved
        # Connection tuples can be:
        #   (signal, slot) - uses default Qt.AutoConnection
        #   (signal, slot, connection_type) - uses specified connection type (e.g., Qt.QueuedConnection)
        for widget_name, connections in self.napari_connections.items():
            if widget_name != "napariLiveWidget":  # Always keep the live widget connected
                widget = getattr(self, widget_name, None)
                if widget:
                    for conn in connections:
                        signal = conn[0]
                        slot = conn[1]
                        connection_type = conn[2] if len(conn) > 2 else None
                        if self.performance_mode:
                            try:
                                signal.disconnect(slot)
                            except TypeError:
                                # Connection might not exist, which is fine
                                pass
                        else:
                            try:
                                if connection_type is not None:
                                    signal.connect(slot, connection_type)
                                else:
                                    signal.connect(slot)
                            except TypeError:
                                # Connection might already exist, which is fine
                                pass

    def toggleNapariTabs(self):
        # Enable/disable Napari tabs based on performance mode
        for i in range(1, self.imageDisplayTabs.count()):
            if self.imageDisplayTabs.tabText(i) != self.LASER_BASED_FOCUS_TAB_NAME:
                self.imageDisplayTabs.setTabEnabled(i, not self.performance_mode)

        if self.performance_mode:
            # Switch to the NapariLiveWidget tab if it exists
            for i in range(self.imageDisplayTabs.count()):
                if isinstance(self.imageDisplayTabs.widget(i), widgets.NapariLiveWidget):
                    self.imageDisplayTabs.setCurrentIndex(i)
                    break

    def togglePerformanceMode(self):
        self.performance_mode = self.performanceModeToggle.isChecked()
        button_txt = "Disable" if self.performance_mode else "Enable"
        self.performanceModeToggle.setText(button_txt + " Performance Mode")
        self.updateNapariConnections()
        self.toggleNapariTabs()
        self.signal_performance_mode_changed.emit(self.performance_mode)
        print(f"Performance mode {'enabled' if self.performance_mode else 'disabled'}")

    def setAcquisitionDisplayTabs(self, selected_configurations, Nz, xy_mode=None):
        if self.performance_mode:
            self.imageDisplayTabs.setCurrentIndex(0)
        elif not self.live_only_mode:
            configs = [config.name for config in selected_configurations]
            print(configs)
            # For well-based acquisitions (Select Wells or Load Coordinates), use Plate View if enabled
            is_well_based = xy_mode is not None and xy_mode in ("Select Wells", "Load Coordinates")
            if is_well_based and self.napariPlateViewWidget is not None and Nz == 1:
                self.imageDisplayTabs.setCurrentWidget(self.napariPlateViewWidget)
            elif USE_NAPARI_FOR_MOSAIC_DISPLAY and Nz == 1:
                self.imageDisplayTabs.setCurrentWidget(self.napariMosaicDisplayWidget)
            else:
                self.imageDisplayTabs.setCurrentWidget(self.napariMultiChannelWidget)

    def openLedMatrixSettings(self):
        if SUPPORT_SCIMICROSCOPY_LED_ARRAY:
            dialog = widgets.LedMatrixSettingsDialog(self.liveController.led_array)
            dialog.exec_()

    def openPreferences(self):
        if CACHED_CONFIG_FILE_PATH and os.path.exists(CACHED_CONFIG_FILE_PATH):
            config = ConfigParser()
            config.read(CACHED_CONFIG_FILE_PATH)
            dialog = widgets.PreferencesDialog(
                config,
                CACHED_CONFIG_FILE_PATH,
                parent=self,
                on_restart=self.restart_application,
            )
            dialog.signal_config_changed.connect(self._update_ram_monitor_visibility)
            dialog.exec_()
        else:
            self.log.warning("No configuration file found")

    def _setup_slack_notifier(self):
        """Initialize the Slack notifier and wire up connections."""
        # Create the slack notifier
        self.slackNotifier = SlackNotifier()

        # Set slack notifier on multipoint controller
        if self.multipointController is not None:
            self.multipointController.set_slack_notifier(self.slackNotifier)
            # Connect Slack notification signals to handlers (runs on main thread for proper ordering)
            self.multipointController.signal_slack_timepoint.connect(self._handle_slack_timepoint_notification)
            self.multipointController.signal_slack_acq_finished.connect(self._handle_slack_acquisition_finished)

        self.log.info("Slack notifier initialized")

    def _handle_slack_timepoint_notification(self, stats: TimepointStats):
        """Handle Slack timepoint notification on the main Qt thread.

        Captures screenshot from mosaic widget (if available) and sends notification.
        """
        try:
            # Capture screenshot from mosaic widget (must be done on main Qt thread)
            mosaic_image = None
            if self.napariMosaicDisplayWidget is not None:
                mosaic_image = self.napariMosaicDisplayWidget.get_screenshot()

            # Send notification with screenshot
            if self.slackNotifier is not None:
                self.slackNotifier.notify_timepoint_complete(stats, mosaic_image)
        except Exception as e:
            self.log.warning(f"Failed to send Slack timepoint notification: {e}")

    def _handle_slack_acquisition_finished(self, stats: AcquisitionStats):
        """Handle Slack acquisition finished notification on the main Qt thread."""
        try:
            if self.slackNotifier is not None:
                self.slackNotifier.notify_acquisition_finished(stats)
        except Exception as e:
            self.log.warning(f"Failed to send Slack acquisition finished notification: {e}")

    def openSlackSettings(self):
        """Open the Slack notifications settings dialog."""
        if self.slackSettingsDialog is None:
            self.slackSettingsDialog = SlackSettingsDialog(
                slack_notifier=self.slackNotifier,
                parent=self,
            )
        self.slackSettingsDialog.show()
        self.slackSettingsDialog.raise_()
        self.slackSettingsDialog.activateWindow()

    def openWorkflowRunner(self):
        """Open the Workflow Runner dialog."""
        from gui.widgets.workflow import WorkflowRunnerDialog
        from control.workflow_runner import WorkflowRunner

        if self.workflowRunnerDialog is None:
            self.workflowRunnerDialog = WorkflowRunnerDialog(parent=self)
            self.workflowRunnerDialog.signal_run_workflow.connect(self._start_workflow)
            self.workflowRunnerDialog.signal_pause_workflow.connect(self._pause_workflow)
            self.workflowRunnerDialog.signal_resume_workflow.connect(self._resume_workflow)
            self.workflowRunnerDialog.signal_stop_workflow.connect(self._stop_workflow)

        self.workflowRunnerDialog.show()
        self.workflowRunnerDialog.raise_()
        self.workflowRunnerDialog.activateWindow()

    def _get_actual_acquisition_path(self) -> str:
        """Get the actual acquisition path (base_path + experiment_ID with timestamp)."""
        if hasattr(self, "multipointController") and self.multipointController:
            base = self.multipointController.base_path
            exp_id = self.multipointController.experiment_ID
            if base and exp_id:
                return os.path.join(base, exp_id)
        return None

    def _start_workflow(self, workflow):
        """Start executing a workflow."""
        from control.workflow_runner import WorkflowRunner

        # Validate: if any acquisition has config_path, current widget must support YAML loading
        has_config_path = any(seq.is_acquisition() and seq.config_path for seq in workflow.get_included_sequences())
        if has_config_path:
            widget = self.recordTabWidget.currentWidget()
            if not hasattr(widget, "_load_acquisition_yaml"):
                from qtpy.QtWidgets import QMessageBox

                QMessageBox.warning(
                    self,
                    "Incompatible Tab",
                    f"This workflow has acquisition sequences with config files, but the current "
                    f"tab ({type(widget).__name__}) does not support loading YAML settings.\n\n"
                    f"Either:\n"
                    f"• Switch to Wellplate or Flexible Multipoint tab, or\n"
                    f"• Edit the acquisition sequences to remove config file paths",
                )
                return

        # Create runner if needed
        if self.workflowRunner is None:
            self.workflowRunner = WorkflowRunner(self)
            self.workflowRunner.signal_workflow_started.connect(self._on_workflow_started)
            self.workflowRunner.signal_workflow_finished.connect(self._on_workflow_finished)
            self.workflowRunner.signal_workflow_paused.connect(self._on_workflow_paused)
            self.workflowRunner.signal_workflow_resumed.connect(self._on_workflow_resumed)
            self.workflowRunner.signal_sequence_started.connect(self._on_sequence_started)
            self.workflowRunner.signal_sequence_finished.connect(self._on_sequence_finished)
            self.workflowRunner.signal_request_acquisition.connect(self._run_acquisition_for_workflow)
            self.workflowRunner.signal_error.connect(self._on_workflow_error)
            self.workflowRunner.signal_script_output.connect(self._on_script_output)
            # Connect acquisition finished signal (permanent connection)
            self.multipointController.acquisition_finished.connect(self.workflowRunner.on_acquisition_finished)

        self.workflowRunner.set_workflow(workflow)
        self.workflowRunner.set_acquisition_path_getter(self._get_actual_acquisition_path)

        # Enable running state immediately (before thread starts)
        # This ensures Pause/Stop buttons are enabled right away
        self._set_workflow_controls_enabled(False)
        if self.workflowRunnerDialog:
            self.workflowRunnerDialog.set_running_state(True)

        self.workflowRunner.start()

    def _on_workflow_started(self):
        """Called when workflow thread starts (from background thread signal)."""
        self.log.info("Workflow started signal received")

    def _on_workflow_finished(self, success: bool):
        """Re-enable main window controls when workflow finishes."""
        self.log.info(f"Workflow finished, success={success}")
        self._set_workflow_controls_enabled(True)
        if self.workflowRunnerDialog:
            self.workflowRunnerDialog.on_workflow_finished(success)

    def _on_sequence_started(self, index: int, name: str):
        """Handle sequence start."""
        self.log.info(f"Sequence started: {name} (index {index})")
        if self.workflowRunnerDialog:
            self.workflowRunnerDialog.on_sequence_started(index, name)

    def _on_sequence_finished(self, index: int, name: str, success: bool):
        """Handle sequence completion."""
        self.log.info(f"Sequence finished: {name}, success={success}")

        # Disable acquisition widget after acquisition completes (if workflow still running)
        if not (self.workflowRunner and self.workflowRunner.is_running()):
            return
        workflow = self.workflowRunner._workflow
        if not workflow or index >= len(workflow.sequences):
            return
        if workflow.sequences[index].is_acquisition():
            widget = self.recordTabWidget.currentWidget()
            if widget:
                widget.setEnabled(False)

    def _on_workflow_error(self, error_msg: str):
        """Handle workflow error."""
        self.log.error(f"Workflow error: {error_msg}")
        if self.workflowRunnerDialog:
            self.workflowRunnerDialog.on_error(error_msg)

    def _on_script_output(self, line: str):
        """Handle script output."""
        if self.workflowRunnerDialog:
            self.workflowRunnerDialog.on_script_output(line)

    def _on_workflow_paused(self):
        """Handle workflow paused."""
        self.log.info("Workflow paused - enabling GUI controls")
        self._set_workflow_controls_enabled(True)  # Re-enable GUI while paused
        if self.workflowRunnerDialog:
            self.workflowRunnerDialog.on_workflow_paused()

    def _on_workflow_resumed(self):
        """Handle workflow resumed."""
        self.log.info("Workflow resumed - disabling GUI controls")
        self._set_workflow_controls_enabled(False)  # Disable GUI when resumed
        if self.workflowRunnerDialog:
            self.workflowRunnerDialog.on_workflow_resumed()

    def _pause_workflow(self):
        """Pause the workflow after current sequence."""
        if self.workflowRunner:
            self.workflowRunner.request_pause()

    def _resume_workflow(self):
        """Resume the paused workflow."""
        if self.workflowRunner:
            self.workflowRunner.request_resume()

    def _stop_workflow(self):
        """Stop the workflow after current sequence."""
        if self.workflowRunner:
            self.workflowRunner.request_stop()

    def _set_workflow_controls_enabled(self, enabled: bool):
        """Enable/disable main window controls during workflow.

        Note: imageDisplayWindow stays enabled for live updates during acquisition.
        """
        # Disable control widgets
        widget_names = ["navigationWidget", "liveControlWidget", "autofocusWidget", "objectivesWidget"]
        for name in widget_names:
            widget = getattr(self, name, None)
            if widget:
                widget.setEnabled(enabled)

        # Disable tab switching and current tab content
        record_tab = getattr(self, "recordTabWidget", None)
        if record_tab:
            record_tab.tabBar().setEnabled(enabled)
            current_widget = record_tab.currentWidget()
            if current_widget:
                current_widget.setEnabled(enabled)

    def _fail_workflow_acquisition(self, error_msg: str):
        """Fail the current workflow acquisition step with an error."""
        self.log.error(error_msg)
        if self.workflowRunner:
            self.workflowRunner.signal_error.emit(error_msg)
            self.workflowRunner.on_acquisition_finished()

    def _run_acquisition_for_workflow(self, config_path: str = ""):
        """Called by workflow runner to start acquisition.

        Args:
            config_path: Optional path to acquisition.yaml file. If provided,
                        settings are loaded from the file before starting acquisition.
        """
        self.log.info(f"Workflow requesting acquisition start (config_path={config_path or 'None'})")
        widget = self.recordTabWidget.currentWidget()

        # Check if current tab supports acquisition
        has_acquisition = hasattr(widget, "btn_startAcquisition") and hasattr(widget, "toggle_acquisition")
        if not has_acquisition:
            self._handle_acquisition_tab_error()
            return

        # Load settings from YAML if provided
        if config_path:
            if not hasattr(widget, "_load_acquisition_yaml"):
                self._fail_workflow_acquisition(
                    f"Widget {type(widget).__name__} does not support loading YAML settings."
                )
                return
            try:
                self.log.info(f"Loading acquisition settings from: {config_path}")
                if not widget._load_acquisition_yaml(config_path):
                    self._fail_workflow_acquisition(f"Failed to load settings from '{config_path}'")
                    return
            except Exception as e:
                self._fail_workflow_acquisition(f"Error loading '{config_path}': {e}")
                return

        # Re-enable widget and start acquisition
        widget.setEnabled(True)
        if not widget.btn_startAcquisition.isChecked():
            widget.btn_startAcquisition.setChecked(True)
            widget.toggle_acquisition(True)

    def _handle_acquisition_tab_error(self):
        """Handle error when current tab does not support acquisition."""
        error_msg = "Current tab does not support acquisition - switch to a multipoint tab"
        self.log.error(error_msg)
        if self.workflowRunner:
            self.workflowRunner.signal_error.emit(error_msg)
            self.workflowRunner.on_acquisition_finished()

    def openChannelConfigurationEditor(self):
        """Open the illumination channel configurator dialog"""
        from control.core.config import ConfigRepository

        config_repo = ConfigRepository()
        dialog = widgets.IlluminationChannelConfiguratorDialog(config_repo, self)
        dialog.signal_channels_updated.connect(self._refresh_channel_lists)
        dialog.exec_()

    def openObservationStateConfigEditor(self):
        """Open the acquisition channel configurator dialog for editing user profiles."""
        dialog = widgets.ObservationStateConfiguratorDialog(self.microscope.config_repo, self)
        dialog.signal_channels_updated.connect(self._refresh_channel_lists)
        dialog.exec_()

    def saveObservationStatePreset(self):
        """Save current imaging state (Observation State) as a named profile preset."""
        from gui.widgets.observation_state_dialogs import run_save_observation_state_dialog

        run_save_observation_state_dialog(
            self,
            self.microscope.config_repo,
            self.liveController,
            self.objectiveStore,
            self.emission_filter_wheel,
            on_success=self._on_observation_state_changed,
        )

    def loadObservationStatePreset(self):
        """Load a saved Observation State preset into general.yaml and live hardware."""
        from gui.widgets.observation_state_dialogs import run_load_observation_state

        run_load_observation_state(
            self,
            self.microscope.config_repo,
            self.liveController,
            self.objectiveStore,
            self.emission_filter_wheel,
            preset_name=None,
            on_success=self._on_observation_state_changed,
        )

    def openAdvancedChannelMapping(self):
        """Open the advanced channel hardware mapping dialog"""
        dialog = widgets.AdvancedChannelMappingDialog(self.microscope.config_repo, self)
        dialog.signal_mappings_updated.connect(self._refresh_channel_lists)
        dialog.exec_()

    def openFilterWheelConfigEditor(self):
        """Open the filter wheel configuration dialog"""
        dialog = widgets.FilterWheelConfiguratorDialog(self.microscope.config_repo, self)
        dialog.signal_config_updated.connect(self._refresh_channel_lists)
        dialog.exec_()

    def _refresh_channel_lists(self):
        """Refresh live mode lists and multipoint observation-preset lists after config or profile changes."""
        if self.liveControlWidget:
            self.liveControlWidget.refresh_mode_list()
        if self.napariLiveWidget:
            self.napariLiveWidget.refresh_mode_list()
        if self.flexibleMultiPointWidget:
            self.flexibleMultiPointWidget.refresh_channel_list()
        if self.wellplateMultiPointWidget:
            self.wellplateMultiPointWidget.refresh_channel_list()
        if getattr(self, "multiPointWithFluidicsWidget", None):
            self.multiPointWithFluidicsWidget.refresh_channel_list()

    def _on_observation_state_changed(self):
        """After Observation State save/load: refresh channel lists and sync Camera tab to hardware."""
        self._refresh_channel_lists()
        if self.cameraSettingWidget:
            self.cameraSettingWidget.sync_controls_from_hardware()

    def onTabChanged(self, index):
        is_flexible_acquisition = (
            (index == self.recordTabWidget.indexOf(self.flexibleMultiPointWidget))
            if ENABLE_FLEXIBLE_MULTIPOINT
            else False
        )
        is_wellplate_acquisition = (
            (index == self.recordTabWidget.indexOf(self.wellplateMultiPointWidget))
            if ENABLE_WELLPLATE_MULTIPOINT
            else False
        )
        self.scanCoordinates.clear_regions()

        if is_wellplate_acquisition:
            if self.wellplateMultiPointWidget.combobox_xy_mode.currentText() == "Manual":
                # trigger manual shape update
                if self.wellplateMultiPointWidget.shapes_mm:
                    self.wellplateMultiPointWidget.update_manual_shape(self.wellplateMultiPointWidget.shapes_mm)
            else:
                # trigger wellplate update
                self.wellplateMultiPointWidget.update_coordinates()
        elif is_flexible_acquisition:
            # trigger flexible regions update
            self.flexibleMultiPointWidget.update_fov_positions()

        self.toggleWellSelector(is_wellplate_acquisition and self.wellSelectionWidget.format != "glass slide")
        acquisitionWidget = self.recordTabWidget.widget(index)
        acquisitionWidget.emit_selected_channels()

    def resizeCurrentTab(self, tabWidget):
        current_widget = tabWidget.currentWidget()
        if current_widget:
            total_height = current_widget.sizeHint().height() + tabWidget.tabBar().height()
            tabWidget.resize(tabWidget.width(), total_height)
            tabWidget.setMaximumHeight(total_height)
            tabWidget.updateGeometry()
            self.updateGeometry()

    def onDisplayTabChanged(self, index):
        current_widget = self.imageDisplayTabs.widget(index)
        if hasattr(current_widget, "viewer"):
            current_widget.activate()

        # Stop focus camera live if not on laser focus tab
        if self.microscope.addons.camera_focus:
            is_laser_focus_tab = self.imageDisplayTabs.tabText(index) == self.LASER_BASED_FOCUS_TAB_NAME

            if hasattr(self, "dock_wellSelection"):
                self.dock_wellSelection.setVisible(not is_laser_focus_tab)

            if not is_laser_focus_tab:
                self.laserAutofocusSettingWidget.stop_live()

        # Only show well selector in Live View tab if it was previously shown
        if self.imageDisplayTabs.tabText(index) == "Live View":
            self.toggleWellSelector(self.well_selector_visible)  # Use stored visibility state
        else:
            self.toggleWellSelector(False)

    def onWellplateChanged(self, format_):
        if isinstance(format_, QVariant):
            format_ = format_.value()

        # TODO(imo): Not sure why glass slide is so special here?  It seems like it's just a "1 well plate".
        if format_ == "glass slide":
            self.toggleWellSelector(False)
            self.stageUtils.is_wellplate = False
        else:
            self.toggleWellSelector(True)
            self.stageUtils.is_wellplate = True

            # replace and reconnect new well selector
            if format_ == "1536 well plate":
                self.replaceWellSelectionWidget(widgets.Well1536SelectionWidget(self.wellplateFormatWidget))
                self.connectWellSelectionWidget()
            elif isinstance(self.wellSelectionWidget, widgets.Well1536SelectionWidget):
                self.replaceWellSelectionWidget(widgets.WellSelectionWidget(format_, self.wellplateFormatWidget))
                self.connectWellSelectionWidget()

        if ENABLE_FLEXIBLE_MULTIPOINT:  # clear regions
            self.flexibleMultiPointWidget.clear_only_location_list()
        if ENABLE_WELLPLATE_MULTIPOINT:  # reset regions onto new wellplate with default size/shape
            self.scanCoordinates.clear_regions()
            self.wellplateMultiPointWidget.set_default_scan_size()

    def toggle_live_scan_grid(self, on):
        if on:
            self.movement_updater.position_after_move.connect(self.wellplateMultiPointWidget.update_live_coordinates)
            self.is_live_scan_grid_on = True
        else:
            try:
                self.movement_updater.position_after_move.disconnect(
                    self.wellplateMultiPointWidget.update_live_coordinates
                )
            except TypeError:
                # Signal was not connected, ignore
                pass
            self.is_live_scan_grid_on = False

    def connectSlidePositionController(self):
        if ENABLE_FLEXIBLE_MULTIPOINT:
            self.stageUtils.signal_loading_position_reached.connect(
                self.flexibleMultiPointWidget.disable_the_start_aquisition_button
            )
        if ENABLE_WELLPLATE_MULTIPOINT:
            self.stageUtils.signal_loading_position_reached.connect(
                self.wellplateMultiPointWidget.disable_the_start_aquisition_button
            )
        if RUN_FLUIDICS:
            self.stageUtils.signal_loading_position_reached.connect(
                self.multiPointWithFluidicsWidget.disable_the_start_aquisition_button
            )

        if ENABLE_FLEXIBLE_MULTIPOINT:
            self.stageUtils.signal_scanning_position_reached.connect(
                self.flexibleMultiPointWidget.enable_the_start_aquisition_button
            )
        if ENABLE_WELLPLATE_MULTIPOINT:
            self.stageUtils.signal_scanning_position_reached.connect(
                self.wellplateMultiPointWidget.enable_the_start_aquisition_button
            )
        if RUN_FLUIDICS:
            self.stageUtils.signal_scanning_position_reached.connect(
                self.multiPointWithFluidicsWidget.enable_the_start_aquisition_button
            )

        self.stageUtils.signal_scanning_position_reached.connect(self.navigationViewer.clear_slide)

    def replaceWellSelectionWidget(self, new_widget):
        self.wellSelectionWidget.setParent(None)
        self.wellSelectionWidget.deleteLater()
        self.wellSelectionWidget = new_widget
        self.scanCoordinates.add_well_selector(self.wellSelectionWidget)
        if USE_NAPARI_WELL_SELECTION and not self.performance_mode and not self.live_only_mode:
            self.napariLiveWidget.replace_well_selector(self.wellSelectionWidget)
        else:
            self.dock_wellSelection.addWidget(self.wellSelectionWidget)

    def connectWellSelectionWidget(self):
        self.wellSelectionWidget.signal_wellSelectedPos.connect(self.move_to_mm)
        self.wellplateFormatWidget.signalWellplateSettings.connect(self.wellSelectionWidget.onWellplateChanged)
        if ENABLE_WELLPLATE_MULTIPOINT:
            self.wellSelectionWidget.signal_wellSelected.connect(self.wellplateMultiPointWidget.update_well_coordinates)

    def toggleWellSelector(self, show, remember_state=True):
        if show and self.imageDisplayTabs.tabText(self.imageDisplayTabs.currentIndex()) == "Live View":
            self.dock_wellSelection.setVisible(True)
        else:
            self.dock_wellSelection.setVisible(False)

        # Only update visibility state if we're in Live View tab and we want to remember the state
        # remember_state is False when we're toggling the well selector for starting/stopping an acquisition
        if self.imageDisplayTabs.tabText(self.imageDisplayTabs.currentIndex()) == "Live View" and remember_state:
            self.well_selector_visible = show

        # Update button text
        if hasattr(self.imageDisplayWindow, "btn_well_selector"):
            self.imageDisplayWindow.btn_well_selector.setText("Hide Well Selector" if show else "Show Well Selector")

    def toggleAcquisitionStart(self, acquisition_started):
        self.log.debug(f"toggleAcquisitionStarted({acquisition_started=})")
        if acquisition_started:
            self.log.info("STARTING ACQUISITION")
            # NDViewer is now configured via push-based API signals (ndviewer_start_acquisition)
            if self.is_live_scan_grid_on:
                self.toggle_live_scan_grid(on=False)
                self.live_scan_grid_was_on = True
            else:
                self.live_scan_grid_was_on = False
            # NOTE: RAM monitor widget is connected via multipointController.signal_acquisition_start
            # which fires AFTER the memory monitor is created (see make_connections)
        else:
            self.log.info("FINISHED ACQUISITION")
            if self.live_scan_grid_was_on:
                self.toggle_live_scan_grid(on=True)
                self.live_scan_grid_was_on = False
            # NOTE: RAM monitor widget is disconnected via multipointController.acquisition_finished

        # click to move off during acquisition
        self.navigationWidget.set_click_to_move(not acquisition_started)

        # disable other acqusiition tabs during acquisition
        current_index = self.recordTabWidget.currentIndex()
        for index in range(self.recordTabWidget.count()):
            self.recordTabWidget.setTabEnabled(index, not acquisition_started or index == current_index)

        # disable autolevel once acquisition started
        if acquisition_started:
            self.liveControlWidget.toggle_autolevel(not acquisition_started)

        # hide well selector during acquisition
        is_wellplate_acquisition = (
            (current_index == self.recordTabWidget.indexOf(self.wellplateMultiPointWidget))
            if ENABLE_WELLPLATE_MULTIPOINT
            else False
        )
        if is_wellplate_acquisition and self.wellSelectionWidget.format != "glass slide":
            self.toggleWellSelector(not acquisition_started, remember_state=False)
        else:
            self.toggleWellSelector(False)

        # display acquisition progress bar during acquisition
        self.recordTabWidget.currentWidget().display_progress_bar(acquisition_started)

    def _update_ram_monitor_visibility(self):
        """Update RAM monitor widget visibility based on setting."""
        import control._def

        if self.ramMonitorWidget is None:
            return

        if control._def.ENABLE_MEMORY_PROFILING:
            self._ram_monitor_should_show = True
            self.ramMonitorWidget.setVisible(True)
            self.ramMonitorWidget.start_monitoring()
            self.log.info("RAM monitor: enabled, showing widget")
        else:
            self._ram_monitor_should_show = False
            self.ramMonitorWidget.stop_monitoring()
            self.ramMonitorWidget.setVisible(False)
            self.log.debug("RAM monitor: disabled, hiding widget")

        self._update_status_bar_visibility()

    def _update_status_bar_visibility(self):
        """Show or hide status bar based on whether any monitor widgets should be visible."""
        warning_has_messages = self.warningErrorWidget is not None and self.warningErrorWidget.has_messages()
        self.statusBar().setVisible(
            self._ram_monitor_should_show or self._bp_monitor_should_show or warning_has_messages
        )

    def _connect_ram_monitor_widget(self):
        """Connect RAM monitor widget to memory monitor during acquisition."""
        import control._def

        if not control._def.ENABLE_MEMORY_PROFILING:
            return

        if self.ramMonitorWidget is None:
            return

        # Connect to the memory monitor from the multipointController for more detailed acquisition tracking
        if self.multipointController is not None and self.multipointController._memory_monitor is not None:
            self.log.info("RAM monitor: connecting widget to memory monitor for acquisition")
            self.ramMonitorWidget.connect_monitor(self.multipointController._memory_monitor)

    def _disconnect_ram_monitor_widget(self):
        """Disconnect RAM monitor widget from acquisition memory monitor."""
        import control._def

        if self.ramMonitorWidget is not None:
            self.ramMonitorWidget.disconnect_monitor()
            # Control monitoring based on current profiling setting
            if control._def.ENABLE_MEMORY_PROFILING:
                # Resume background monitoring, preserve peak from acquisition
                self.ramMonitorWidget.start_monitoring(reset_peak=False)
                self.log.debug("RAM monitor: disconnected from acquisition, continuing background monitoring")
            else:
                # Stop monitoring entirely when profiling is disabled
                self.ramMonitorWidget.stop_monitoring()
                self.log.debug("RAM monitor: disconnected from acquisition, monitoring stopped (profiling disabled)")

    def _connect_backpressure_monitor_widget(self):
        """Connect backpressure monitor widget during acquisition."""
        import control._def

        if not control._def.ACQUISITION_THROTTLING_ENABLED:
            self.log.debug("Backpressure monitor: throttling disabled, skipping")
            return

        if self.backpressureMonitorWidget is None:
            self.log.warning("Backpressure monitor: widget not initialized")
            return

        # Get the backpressure controller from the multipoint worker
        bp_controller = self.multipointController.backpressure_controller
        if bp_controller is None:
            self.log.debug("Backpressure monitor: no controller available from worker")
            return

        if not bp_controller.enabled:
            self.log.debug("Backpressure monitor: controller exists but is disabled")
            return

        self.log.info("Backpressure monitor: connecting widget to backpressure controller")
        self._bp_monitor_should_show = True
        self.backpressureMonitorWidget.start_monitoring(bp_controller)
        self.backpressureMonitorWidget.setVisible(True)
        self._update_status_bar_visibility()

    def _disconnect_backpressure_monitor_widget(self):
        """Disconnect backpressure monitor widget after acquisition."""
        if self.backpressureMonitorWidget is None:
            return

        self._bp_monitor_should_show = False
        self.backpressureMonitorWidget.stop_monitoring()
        self.backpressureMonitorWidget.setVisible(False)
        self.log.debug("Backpressure monitor: disconnected from acquisition")
        self._update_status_bar_visibility()

    def _connect_warning_handler(self):
        """Connect logging handler to warning/error widget."""
        if self.warningErrorWidget is None:
            return

        self._warning_handler = squid.logging.BufferingHandler()
        squid.logging.get_logger().addHandler(self._warning_handler)
        self.warningErrorWidget.connect_handler(self._warning_handler)
        self.log.debug("Warning/error widget: connected logging handler")

    def _disconnect_warning_handler(self):
        """Disconnect logging handler from warning/error widget.

        Uses robust error handling to ensure cleanup completes even if
        individual operations fail (e.g., handler already removed).
        """
        if self.warningErrorWidget is not None:
            self.warningErrorWidget.disconnect_handler()

        if self._warning_handler is not None:
            try:
                squid.logging.get_logger().removeHandler(self._warning_handler)
            except Exception as e:
                self.log.debug(f"Error removing warning handler (may already be removed): {e}")

            try:
                self._warning_handler.close()
            except Exception as e:
                self.log.debug(f"Error closing warning handler: {e}")

            self._warning_handler = None
            self.log.debug("Warning/error widget: disconnected logging handler")

    def onStartLive(self):
        self.imageDisplayTabs.setCurrentIndex(0)
        if self.alignmentWidget is not None:
            self.alignmentWidget.enable()

    def move_from_click_image(self, click_x, click_y, image_width, image_height):
        if self.navigationWidget.get_click_to_move_enabled():
            pixel_size_um = self.objectiveStore.get_pixel_size_factor() * self.camera.get_pixel_size_binned_um()

            pixel_sign_x = 1
            pixel_sign_y = 1 if INVERTED_OBJECTIVE else -1

            delta_x = pixel_sign_x * pixel_size_um * click_x / 1000.0
            delta_y = pixel_sign_y * pixel_size_um * click_y / 1000.0

            self.log.debug(
                f"Click to move enabled, click at {click_x=}, {click_y=} results in relative move of {delta_x=} [mm], {delta_y=} [mm]"
            )
            self.stage.move_x(delta_x)
            self.stage.move_y(delta_y)
        else:
            self.log.debug(f"Click to move disabled, ignoring click at {click_x=}, {click_y=}")

    def move_from_click_mm(self, x_mm, y_mm):
        if self.navigationWidget.get_click_to_move_enabled():
            self.log.debug(f"Click to move enabled, moving to {x_mm=}, {y_mm=}")
            self.move_to_mm(x_mm, y_mm)
        else:
            self.log.debug(f"Click to move disabled, ignoring click request for {x_mm=}, {y_mm=}")

    def move_to_mm(self, x_mm, y_mm):
        self.stage.move_x_to(x_mm)
        self.stage.move_y_to(y_mm)

    def showEvent(self, event):
        """Handle window show event to initialize visibility-dependent widgets."""
        super().showEvent(event)
        # Initialize visibility-dependent widgets now that window is shown
        if hasattr(self, "_show_event_initialized") and self._show_event_initialized:
            return  # Only initialize once
        self._show_event_initialized = True
        self._update_ram_monitor_visibility()
        self._connect_warning_handler()

    def _on_plate_view_fov_clicked(self, well_id: str, fov_index: int) -> None:
        """Handle double-click on plate view: navigate NDViewer to FOV and switch tab."""
        if self.ndviewerTab is None:
            self.log.debug("FOV click ignored: NDViewer tab not available")
            return

        if not self.ndviewerTab.go_to_fov(well_id, fov_index):
            self.log.debug(f"Could not navigate to FOV well={well_id}, fov={fov_index} - may not exist in dataset")
            return

        ndviewer_tab_idx = self.imageDisplayTabs.indexOf(self.ndviewerTab)
        if ndviewer_tab_idx >= 0:
            self.imageDisplayTabs.setCurrentIndex(ndviewer_tab_idx)
        else:
            self.log.warning("NDViewer tab exists but not found in tab widget")

    def restart_application(self):
        """Restart the application with --skip-init flag.

        Performs hardware cleanup, spawns a new process with --skip-init flag,
        then quits the current application. Hardware initialization is skipped in the new
        process since hardware is already in a known state.
        """
        self.log.info("Restarting application with --skip-init...")

        # Build new args list, preserving original arguments but adding --skip-init
        args = [sys.executable] + sys.argv
        if "--skip-init" not in args:
            args.append("--skip-init")

        # Clean up hardware BEFORE spawning new process to release resources
        self._cleanup_for_restart()

        # Spawn new process AFTER cleanup so it can acquire hardware
        try:
            subprocess.Popen(args)
        except OSError as e:
            self.log.exception("Failed to spawn new process for restart")
            QMessageBox.critical(
                self,
                "Restart Failed",
                f"Failed to restart the application.\n\nError: {e}\n\n"
                "The application will now close. Please restart manually.",
            )
            # Still quit since hardware is already cleaned up
            QApplication.instance().quit()
            return

        # Quit the application
        QApplication.instance().quit()

    def _cleanup_common(self, for_restart: bool = False):
        """Common cleanup logic shared between closeEvent and restart.

        Args:
            for_restart: If True, wrap operations in try-except to ensure cleanup completes.
                        Z retraction and objective reset still run when using Xeryon
                        (Xeryon must be zeroed before re-init), but are skipped otherwise.
        """
        context = "restart" if for_restart else "shutdown"

        # Cache position and settings
        try:
            squid.stage.utils.cache_position(pos=self.stage.get_pos(), stage_config=self.stage.get_config())
        except ValueError as e:
            # ValueError is expected when position is out of bounds
            self.log.error(f"Couldn't cache position while closing for {context}. Error: {e}")
        except Exception:
            if for_restart:
                self.log.exception(f"Unexpected error caching position during {context}")
            else:
                raise

        try:
            squid.camera.settings_cache.save_camera_settings(self.camera)
        except Exception:
            if for_restart:
                self.log.exception(f"Error saving camera settings during {context}")
            else:
                raise

        try:
            control.illumination_settings_cache.save_illumination_settings(self.microscope.illumination_controller)
        except Exception:
            if for_restart:
                self.log.exception(f"Error saving illumination settings during {context}")
            else:
                raise

        try:
            active_name = self.liveController.get_active_channel_name()
            self.microscope.config_repo.persist_general_config(active_channel_name=active_name)
        except Exception:
            if for_restart:
                self.log.exception(f"Error persisting general config during {context}")
            else:
                raise

        try:
            self._disconnect_warning_handler()
        except Exception:
            if for_restart:
                self.log.exception(f"Error disconnecting warning handler during {context}")
            else:
                raise

        # Clean up multipoint controller
        if self.multipointController is not None:
            try:
                self.multipointController.close()
            except Exception:
                self.log.exception(f"Error closing multipoint controller during {context}")

        # Clean up NDViewer
        if self.ndviewerTab is not None:
            try:
                self.ndviewerTab.close()
            except Exception:
                self.log.exception(f"Error closing NDViewer tab during {context}")

        # Close napari viewers (they run background threads that prevent clean exit)
        for widget_name in [
            "napariLiveWidget",
            "napariMultiChannelWidget",
            "napariMosaicDisplayWidget",
            "napariPlateViewWidget",
        ]:
            widget = getattr(self, widget_name, None)
            if widget is not None and hasattr(widget, "viewer"):
                try:
                    widget.viewer.close()
                except Exception:
                    self.log.exception(f"Error closing {widget_name} viewer during {context}")
                    if not for_restart:
                        raise

        try:
            self.movement_update_timer.stop()
        except Exception:
            if for_restart:
                self.log.exception(f"Error stopping movement update timer during {context}")
            else:
                raise

        # Close filter wheel
        if self.emission_filter_wheel:
            try:
                if not for_restart:
                    self.emission_filter_wheel.set_filter_wheel_position({1: 1})
                self.emission_filter_wheel.close()
            except Exception:
                if for_restart:
                    self.log.exception(f"Error closing filter wheel during {context}")
                else:
                    raise

        # Stop laser autofocus
        if self.microscope.addons.camera_focus:
            try:
                self.liveController_focus_camera.stop_live()
                self.imageDisplayWindow_focus.close()
            except Exception:
                if for_restart:
                    self.log.exception(f"Error closing laser AF during {context}")
                else:
                    raise

        # Stop live view and close camera
        try:
            self.liveController.stop_live()
            self.camera.stop_streaming()
            self.camera.close()
        except Exception:
            if for_restart:
                self.log.exception(f"Error closing camera during {context}")
            else:
                raise

        try:
            _ic = getattr(self.microscope, "illumination_controller", None)
            if _ic is not None:
                _ic.turn_off_all()
        except Exception:
            if for_restart:
                self.log.exception(f"Error turning off illumination during {context}")
            else:
                raise

        # Retract Z and reset objective changer on full shutdown.
        # On restart, only retract Z and reset if Xeryon objective changer is present
        # (Xeryon must be zeroed before re-init; Z must retract first for safety).
        if not for_restart or USE_XERYON:
            z_retracted = False
            try:
                self.stage.move_z_to(OBJECTIVE_RETRACTED_POS_MM)
                z_retracted = True
            except Exception:
                if for_restart:
                    self.log.exception(f"Error retracting Z during {context}")
                else:
                    raise

            if USE_XERYON and self.objective_changer and z_retracted:
                try:
                    self.objective_changer.moveToZero()
                except Exception:
                    if for_restart:
                        self.log.exception(f"Error resetting objective changer during {context}")
                    else:
                        raise

        if not for_restart:
            self.microcontroller.turn_off_all_pid()

        # Turn off CellX lasers
        if ENABLE_CELLX:
            try:
                for channel in [1, 2, 3, 4]:
                    self.cellx.turn_off(channel)
                self.cellx.close()
            except Exception:
                if for_restart:
                    self.log.exception(f"Error closing CellX during {context}")
                else:
                    raise

        # Close fluidics
        if RUN_FLUIDICS:
            try:
                self.fluidics.close()
            except Exception:
                if for_restart:
                    self.log.exception(f"Error closing fluidics during {context}")
                else:
                    raise

        # Close image display resources
        try:
            self.imageSaver.close()
            self.imageDisplay.close()
        except Exception:
            if for_restart:
                self.log.exception(f"Error closing display windows during {context}")
            else:
                raise

        # Close microcontroller last (releases serial port)
        try:
            self.microcontroller.close()
        except Exception:
            if for_restart:
                self.log.exception(f"Error closing microcontroller during {context}")
            else:
                raise

    def _cleanup_for_restart(self):
        """Clean up hardware and resources for restart. Retracts Z and resets Xeryon if present."""
        self._cleanup_common(for_restart=True)

    def closeEvent(self, event):
        # Show confirmation dialog
        reply = QMessageBox.question(
            self,
            "Confirm Exit",
            "Are you sure you want to exit the software?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if reply == QMessageBox.No:
            event.ignore()
            return

        self._cleanup_common(for_restart=False)

        try:
            self.cswWindow.closeForReal(event)
        except AttributeError:
            pass

        try:
            self.cswfcWindow.closeForReal(event)
        except AttributeError:
            pass

        event.accept()
