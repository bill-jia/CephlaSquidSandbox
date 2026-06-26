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
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import serial


from .enums import NDViewerMode


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


class MovementUpdater(QObject):
    position_after_move = Signal(squid.abc.Pos)
    position = Signal(squid.abc.Pos)
    piezo_z_um = Signal(float)

    def __init__(
        self, stage: AbstractStage, piezo: Optional[PiezoStage], movement_threshhold_mm=0.0001, *args, **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.stage: AbstractStage = stage
        self.piezo: Optional[PiezoStage] = piezo
        self.movement_threshhold_mm = movement_threshhold_mm
        self.previous_pos: Optional[squid.abc.Pos] = None
        self.previous_piezo_pos: Optional[float] = None
        self.sent_after_stopped = False

    def do_update(self):
        if self.piezo:
            if not self.previous_piezo_pos:
                self.previous_piezo_pos = self.piezo.position
            else:
                current_piezo_position = self.piezo.position
                if self.previous_piezo_pos != current_piezo_position:
                    self.previous_piezo_pos = current_piezo_position
                    self.piezo_z_um.emit(current_piezo_position)

        pos = self.stage.get_pos()
        # Doing previous_pos initialization like this means we technically miss the first real update,
        # but that's okay since this is intended to be run frequently in the background.
        if not self.previous_pos:
            self.previous_pos = pos
            return

        abs_delta_x = abs(self.previous_pos.x_mm - pos.x_mm)
        abs_delta_y = abs(self.previous_pos.y_mm - pos.y_mm)

        if (
            abs_delta_y < self.movement_threshhold_mm
            and abs_delta_x < self.movement_threshhold_mm
            and not self.stage.get_state().busy
        ):
            # In here, send all the signals that must be sent once per stop of movement.  AKA once per arriving at a
            # new position for a while.
            self.sent_after_stopped = True
            self.position_after_move.emit(pos)
        else:
            self.sent_after_stopped = False

        # Here, emit all the signals that want higher fidelity movement updates.
        self.position.emit(pos)

        self.previous_pos = pos


class QtAutoFocusController(AutoFocusController, QObject):
    autofocusFinished = Signal()
    image_to_display = Signal(np.ndarray)

    def __init__(
        self,
        camera: AbstractCamera,
        stage: AbstractStage,
        liveController: LiveController,
        microcontroller: Microcontroller,
        nl5: Optional[NL5],
    ):
        QObject.__init__(self)
        AutoFocusController.__init__(
            self,
            camera,
            stage,
            liveController,
            microcontroller,
            lambda: self.autofocusFinished.emit(),
            lambda image: self.image_to_display.emit(image),
            nl5,
        )


class QtMultiPointController(MultiPointController, QObject):
    acquisition_finished = Signal()
    # Fires after acquisition_finished, once background writeback/finalize is done
    # (all zarr writers flushed and on disk; safe to move/copy the dataset).
    data_writing_complete = Signal()
    signal_acquisition_start = Signal()
    image_to_display = Signal(np.ndarray)
    image_to_display_multi = Signal(np.ndarray, int)
    signal_current_configuration = Signal(ObservationState)
    signal_register_current_fov = Signal(float, float)
    napari_layers_init = Signal(int, int, object)
    napari_layers_update = Signal(np.ndarray, float, float, int, str)  # image, x_mm, y_mm, k, channel
    signal_set_display_tabs = Signal(list, int, str)  # configs: list, Nz: int, xy_mode: str
    signal_acquisition_progress = Signal(int, int, int)
    signal_region_progress = Signal(int, int)
    signal_coordinates = Signal(float, float, float, int)  # x, y, z, region
    # Plate view signals
    plate_view_init = Signal(int, int, tuple, tuple, list)  # rows, cols, well_slot_shape, fov_grid_shape, channel_names
    plate_view_update = Signal(int, str, np.ndarray)  # channel_idx, channel_name, plate_image
    # Slack notification signals (allows main thread to capture screenshot and maintain ordering)
    signal_slack_timepoint = Signal(object)  # TimepointStats
    signal_slack_acq_finished = Signal(object)  # AcquisitionStats
    # NDViewer push-based API signals (TIFF mode)
    ndviewer_start_acquisition = Signal(list, int, int, int, list)  # channels, num_z, height, width, fov_labels
    ndviewer_register_image = Signal(int, int, int, str, str)  # t, fov_idx, z, channel, filepath
    # NDViewer push-based API signals (Zarr mode)
    ndviewer_start_zarr_acquisition = Signal(
        list, list, int, list, int, int
    )  # fov_paths, channels, num_z, fov_labels, height, width
    ndviewer_notify_zarr_frame = Signal(int, int, int, str, int)  # t, fov_idx, z, channel, region_idx
    ndviewer_end_zarr_acquisition = Signal()
    # Fires at the start of each timepoint so napari views can flush per-timepoint caches.
    signal_new_time_point = Signal(int)  # time_point index
    # Internal plumbing: per-frame hot path is hit from the acquisition worker thread
    # (plain threading.Thread, not QThread). Qt's AutoConnection can't detect that as
    # cross-thread and degrades to DirectConnection, so downstream emits/slots would run
    # inline on the worker thread. Posting via this signal with Qt.QueuedConnection forces
    # the handler onto the GUI thread's event loop, keeping the worker's per-capture path
    # lean and restoring Qt thread-affinity invariants for the display/napari slots.
    _new_image_work_request = Signal(object, object)  # CameraFrame, CaptureInfo

    def __init__(
        self,
        microscope: Microscope,
        live_controller: LiveController,
        autofocus_controller: AutoFocusController,
        objective_store: ObjectiveStore,
        scan_coordinates: Optional[ScanCoordinates] = None,
        laser_autofocus_controller: Optional[LaserAutofocusController] = None,
        fluidics: Optional[Any] = None,
        alignment_widget=None,
    ):
        MultiPointController.__init__(
            self,
            microscope=microscope,
            live_controller=live_controller,
            autofocus_controller=autofocus_controller,
            objective_store=objective_store,
            callbacks=MultiPointControllerFunctions(
                signal_acquisition_start=self._signal_acquisition_start_fn,
                signal_acquisition_finished=self._signal_acquisition_finished_fn,
                signal_new_image=self._signal_new_image_fn,
                signal_current_configuration=self._signal_current_configuration_fn,
                signal_current_fov=self._signal_current_fov_fn,
                signal_overall_progress=self._signal_overall_progress_fn,
                signal_region_progress=self._signal_region_progress_fn,
                signal_plate_view_init=self._signal_plate_view_init_fn,
                signal_plate_view_update=self._signal_plate_view_update_fn,
                signal_slack_timepoint_notification=self._signal_slack_timepoint_notification_fn,
                signal_slack_acquisition_finished=self._signal_slack_acquisition_finished_fn,
                signal_zarr_frame_written=self._signal_zarr_frame_written_fn,
                signal_new_time_point=self._signal_new_time_point_fn,
                signal_data_writing_complete=self._signal_data_writing_complete_fn,
            ),
            scan_coordinates=scan_coordinates,
            laser_autofocus_controller=laser_autofocus_controller,
            alignment_widget=alignment_widget,
        )
        QObject.__init__(self)

        self._napari_inited_for_this_acquisition = False
        # NDViewer push-based API state
        self._ndviewer_fov_labels: list = []  # ["A1:0", "A1:1", "A2:0", ...]
        self._ndviewer_region_fov_offset: dict = {}  # {"A1": 0, "A2": 5, ...} for flat FOV index
        self._ndviewer_region_idx_offset: list = []  # [0, 5, ...] region_idx -> flat FOV offset
        self._ndviewer_mode: NDViewerMode = NDViewerMode.INACTIVE  # Current viewer mode
        self._ndviewer_region_index_map: dict = {}  # {region_name: region_idx} for 6D mode

        # When False, skip the per-frame display/napari emits during multipoint
        # and replay one last frame per channel at acquisition end. NDViewer
        # bookkeeping still runs per-frame (data-tracking, not display).
        # The flag is read live on every frame, so toggling the checkbox during
        # an active acquisition takes effect on the next capture.
        self._show_live_during_acquisition: bool = True
        # {observation_state_name: (CameraFrame, CaptureInfo)} — last seen frame
        # per channel while live preview is suppressed. Replayed at acq end so
        # every viewer layer gets populated with its most recent image.
        self._last_frames_per_channel: Dict[str, Tuple[Any, CaptureInfo]] = {}

        # Route per-frame work to the GUI thread's event loop. Worker calls
        # signal_new_image → _signal_new_image_fn emits this signal → queued dispatch
        # to _handle_new_image_on_gui_thread runs on the GUI thread.
        self._new_image_work_request.connect(
            self._handle_new_image_on_gui_thread, Qt.QueuedConnection
        )

    def set_show_live_during_acquisition(self, enabled: bool) -> None:
        # Flipping ON mid-run invalidates the off-period cache — the live path
        # will now handle new frames directly, and we don't want stale frames
        # from the previous off-period to get replayed at acquisition end.
        enabled = bool(enabled)
        if enabled and not self._show_live_during_acquisition:
            self._last_frames_per_channel.clear()
        self._show_live_during_acquisition = enabled

    def _signal_acquisition_start_fn(self, parameters: AcquisitionParameters):
        # TODO mpc napari signals
        self._napari_inited_for_this_acquisition = False
        if not self.run_acquisition_current_fov:
            self.signal_set_display_tabs.emit(self.selected_configurations, self.NZ, self.xy_mode)
        else:
            self.signal_set_display_tabs.emit(self.selected_configurations, 2, self.xy_mode)
        self.signal_acquisition_start.emit()

        # NDViewer push-based API: emit start_acquisition signal
        scan_info = parameters.scan_position_information
        channels = parameters.selected_observation_state_names
        num_z = parameters.NZ

        # Build FOV labels and region offset mapping
        self._ndviewer_fov_labels = []
        self._ndviewer_region_fov_offset = {}
        self._ndviewer_region_idx_offset = []
        fov_idx = 0
        for region_name in scan_info.scan_region_names:
            self._ndviewer_region_fov_offset[region_name] = fov_idx
            self._ndviewer_region_idx_offset.append(fov_idx)  # region_idx -> flat offset
            num_fovs = len(scan_info.scan_region_fov_coords_mm.get(region_name, []))
            for i in range(num_fovs):
                self._ndviewer_fov_labels.append(f"{region_name}:{i}")
                fov_idx += 1

        # Get image dimensions from camera (after binning and software crop).
        # get_crop_size falls back to the camera's current resolution when no crop is set.
        width, height = self.microscope.camera.get_crop_size()

        # Check save format to determine which API to use. Pull the per-acquisition value
        # off the parameters rather than the global so the dropdown in the multipoint widget
        # takes effect without mutating control._def.
        if parameters.file_saving_option == control._def.FileSavingOption.ZARR_V3:
            # Always 5D per-FOV (HCS or non-HCS). Both use the same push API.
            self._ndviewer_mode = NDViewerMode.ZARR_5D
            fov_paths = self._build_zarr_fov_paths(parameters)
            self.ndviewer_start_zarr_acquisition.emit(
                fov_paths, channels, num_z, self._ndviewer_fov_labels, height, width
            )
        else:
            self._ndviewer_mode = NDViewerMode.TIFF
            self.ndviewer_start_acquisition.emit(channels, num_z, height, width, self._ndviewer_fov_labels)

    def _signal_acquisition_finished_fn(self):
        # End zarr acquisition if active (before general acquisition_finished)
        if self._ndviewer_mode == NDViewerMode.ZARR_5D:
            self.ndviewer_end_zarr_acquisition.emit()
            self._ndviewer_region_index_map = {}
        self._ndviewer_mode = NDViewerMode.INACTIVE

        # If live preview was suppressed during any part of the run, replay the
        # cached per-channel frames so every layer in the multichannel / mosaic
        # viewers gets populated with its most recent image. We replay through
        # _new_image_work_request so the normal queued path handles layer-init,
        # napari updates, and ndviewer registration uniformly.
        if self._last_frames_per_channel:
            for frame, info in list(self._last_frames_per_channel.values()):
                try:
                    self._new_image_work_request.emit(frame, info)
                except Exception:
                    self.log.exception("Failed to replay final frame to display")
            self._last_frames_per_channel.clear()

        self.acquisition_finished.emit()
        finish_pos = self.stage.get_pos()
        self.signal_register_current_fov.emit(finish_pos.x_mm, finish_pos.y_mm)

    def _signal_data_writing_complete_fn(self):
        # Called from the worker's background writeback-coordinator thread. Emit
        # a Qt signal so the GUI-thread handler runs via a queued connection.
        self.data_writing_complete.emit()

    def _signal_new_image_fn(self, frame: squid.abc.CameraFrame, info: CaptureInfo):
        # Hot path — called on the acquisition worker thread.
        #
        # The flag is read on every frame so the user can flip the "Show live
        # preview" checkbox mid-acquisition and the next capture honours it.
        # When enabled, hand off via queued signal so all display/napari/ndviewer
        # work runs on the GUI thread. When disabled, cache the frame per-channel
        # for end-of-acquisition replay and do only the ndviewer filepath
        # registration (data-tracking, not display).
        if self._show_live_during_acquisition:
            self._new_image_work_request.emit(frame, info)
            return
        channel_key = getattr(info.observation_state, "name", "") or ""
        self._last_frames_per_channel[channel_key] = (frame, info)
        self._register_ndviewer_filepath(frame, info)

    def _register_ndviewer_filepath(self, frame: squid.abc.CameraFrame, info: CaptureInfo) -> None:
        region_offset = self._ndviewer_region_fov_offset.get(info.region_id)
        if region_offset is None:
            return
        flat_fov_idx = region_offset + info.fov
        if self._ndviewer_mode == NDViewerMode.ZARR_5D:
            # Zarr path notifies via signal_zarr_frame_written on subprocess completion.
            return
        filepath = control.utils_acquisition.get_image_filepath(
            info.save_directory, info.file_id, info.observation_state.name, frame.frame.dtype
        )
        self.ndviewer_register_image.emit(
            info.time_point, flat_fov_idx, info.z_index, info.observation_state.name, filepath
        )

    def _handle_new_image_on_gui_thread(self, frame: squid.abc.CameraFrame, info: CaptureInfo):
        self.image_to_display.emit(frame.frame)
        # Z for plot in μm: piezo-only uses piezo position, mixed mode combines stepper + piezo
        stepper_z_um = info.position.z_mm * 1000
        if IS_PIEZO_ONLY:
            z_for_plot = info.z_piezo_um if info.z_piezo_um is not None else 0
        elif info.z_piezo_um is not None:
            z_for_plot = stepper_z_um + info.z_piezo_um
        else:
            z_for_plot = stepper_z_um
        self.signal_coordinates.emit(info.position.x_mm, info.position.y_mm, z_for_plot, info.region_id)

        if not self._napari_inited_for_this_acquisition:
            self._napari_inited_for_this_acquisition = True
            self.napari_layers_init.emit(frame.frame.shape[0], frame.frame.shape[1], frame.frame.dtype)

        objective_magnification = str(int(self.objectiveStore.get_current_objective_info()["magnification"]))
        napri_layer_name = objective_magnification + "x " + info.observation_state.name
        self.napari_layers_update.emit(
            frame.frame, info.position.x_mm, info.position.y_mm, info.z_index, napri_layer_name
        )

        # NDViewer push-based API: register image
        # Compute flat FOV index from region and fov within region
        region_offset = self._ndviewer_region_fov_offset.get(info.region_id)
        if region_offset is None:
            # This should not happen if start_acquisition was called correctly
            self.log.warning(
                f"Unknown region_id '{info.region_id}' in NDViewer registration. "
                f"Available: {list(self._ndviewer_region_fov_offset.keys())}. Skipping."
            )
            return
        flat_fov_idx = region_offset + info.fov

        if self._ndviewer_mode == NDViewerMode.ZARR_5D:
            # Zarr mode: notification happens via signal_zarr_frame_written callback
            # when the subprocess completes writing, not here (too early).
            pass
        else:
            # TIFF mode: register with filepath (synchronous write, notification is correct here)
            filepath = control.utils_acquisition.get_image_filepath(
                info.save_directory, info.file_id, info.observation_state.name, frame.frame.dtype
            )
            self.ndviewer_register_image.emit(
                info.time_point, flat_fov_idx, info.z_index, info.observation_state.name, filepath
            )

    def _signal_current_configuration_fn(self, config: ObservationState):
        self.signal_current_configuration.emit(config)

    def _signal_current_fov_fn(self, x_mm: float, y_mm: float):
        self.signal_register_current_fov.emit(x_mm, y_mm)

    def _signal_overall_progress_fn(self, overall_progress: OverallProgressUpdate):
        self.signal_acquisition_progress.emit(
            overall_progress.current_region, overall_progress.total_regions, overall_progress.current_timepoint
        )

    def _signal_region_progress_fn(self, region_progress: RegionProgressUpdate):
        self.signal_region_progress.emit(region_progress.current_fov, region_progress.region_fovs)

    def _signal_plate_view_init_fn(self, plate_view_init: PlateViewInit):
        self.plate_view_init.emit(
            plate_view_init.num_rows,
            plate_view_init.num_cols,
            plate_view_init.well_slot_shape,
            plate_view_init.fov_grid_shape,
            plate_view_init.channel_names,
        )

    def _signal_plate_view_update_fn(self, plate_view_update: PlateViewUpdate):
        self.plate_view_update.emit(
            plate_view_update.channel_idx,
            plate_view_update.channel_name,
            plate_view_update.plate_image,
        )

    def _signal_slack_timepoint_notification_fn(self, stats: TimepointStats):
        self.signal_slack_timepoint.emit(stats)

    def _signal_slack_acquisition_finished_fn(self, stats: AcquisitionStats):
        self.signal_slack_acq_finished.emit(stats)

    def _signal_new_time_point_fn(self, time_point: int):
        self.signal_new_time_point.emit(time_point)

    def _signal_zarr_frame_written_fn(
        self, fov: int, time_point: int, z_index: int, channel_name: str, region_idx: int
    ):
        """Called when subprocess completes writing a zarr frame.

        This is the correct time to notify the viewer - after data is on disk.

        Args:
            fov: Local FOV index within the region (not flat/global index)
            time_point: Time point index
            z_index: Z slice index
            channel_name: Channel name string
            region_idx: Index of the region in scan order
        """
        if self._ndviewer_mode == NDViewerMode.ZARR_5D:
            # 5D per-FOV: compute flat FOV index from local FOV + region offset.
            if region_idx < len(self._ndviewer_region_idx_offset):
                flat_fov = self._ndviewer_region_idx_offset[region_idx] + fov
            else:
                flat_fov = fov
            self.ndviewer_notify_zarr_frame.emit(time_point, flat_fov, z_index, channel_name, 0)

    # -------------------------------------------------------------------------
    # Helper methods for Zarr FOV path building
    # -------------------------------------------------------------------------

    def _build_zarr_fov_paths(self, parameters: AcquisitionParameters) -> List[str]:
        """Build the per-FOV OME-NGFF zarr group paths for display.

        Returns one path per FOV in scan order (flattened across regions).
        """
        base_path = os.path.join(parameters.base_path, parameters.experiment_ID)
        scan_info = parameters.scan_position_information
        is_hcs = self._detect_hcs_mode(scan_info)

        fov_paths: List[str] = []
        for region_name in scan_info.scan_region_names:
            num_fovs = len(scan_info.scan_region_fov_coords_mm.get(region_name, []))
            for fov in range(num_fovs):
                if is_hcs:
                    path = control.utils.build_hcs_zarr_fov_path(base_path, region_name, fov)
                else:
                    path = control.utils.build_per_fov_zarr_path(base_path, region_name, fov)
                fov_paths.append(path)

        return fov_paths

    def _detect_hcs_mode(self, scan_info: ScanCoordinates) -> bool:
        """Detect if this is an HCS (wellplate) acquisition.

        Args:
            scan_info: Scan coordinates with region names.

        Returns:
            True if all region names match well ID pattern (e.g., A1, B12).
        """
        well_pattern = re.compile(r"^[A-Z]+\d+$")

        for region_name in scan_info.scan_region_names:
            if not well_pattern.match(region_name):
                return False
        return len(scan_info.scan_region_names) > 0

