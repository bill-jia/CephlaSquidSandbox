import threading
import time
import math
from threading import Thread
from typing import Optional, Callable

import numpy as np

import squid.logging
from control import utils
import control._def
from control._sdk_watchdog import CameraTimeoutError
from control.core.auto_focus_worker import AutofocusWorker
from control.core.live_controller import LiveController
from control.microcontroller import Microcontroller
from control.NL5 import NL5
from squid.abc import AbstractCamera, AbstractStage
from squid.stage.utils import move_xy_with_z_retract


class AutoFocusController:
    def __init__(
        self,
        camera: AbstractCamera,
        stage: AbstractStage,
        liveController: LiveController,
        microcontroller: Microcontroller,
        finished_fn: Callable[[], None],
        image_to_display_fn: Callable[[np.ndarray], None],
        nl5: Optional[NL5],
    ):
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self._autofocus_worker: Optional[AutofocusWorker] = None
        self._focus_thread: Optional[Thread] = None
        self._keep_running = threading.Event()
        self._completion = threading.Event()
        self.last_result = None
        self.last_error = None
        self.acquisition_active = lambda: False
        self.piezo_active = lambda: False
        self.camera: AbstractCamera = camera
        self.stage: AbstractStage = stage
        self.microcontroller: Microcontroller = microcontroller
        self.liveController: LiveController = liveController
        self._finished_fn = finished_fn
        self._image_to_display_fn = image_to_display_fn
        self.nl5: Optional[NL5] = nl5

        # Start with "Reasonable" defaults. deltaZ is stored in mm
        # (set_deltaZ takes µm) — keep the constructor default in mm too.
        self.N: int = 10
        self.deltaZ: float = 1.524 / 1000  # legacy scan stores millimetres
        self.crop_width = control._def.AF.CROP_WIDTH
        self.crop_height = control._def.AF.CROP_HEIGHT
        self.autofocus_in_progress = False
        self.focus_map_coords = []
        self.use_focus_map = False

    @property
    def focus_measure_operator(self):
        state = self.liveController.obs_controller.current_observation_state
        if state is None:
            return control._def.FOCUS_MEASURE_OPERATOR
        return control._def.FocusMeasureOperator.convert_to_enum(state.focus_measure_operator)

    def set_focus_measure_operator(self, value):
        self.liveController.obs_controller.set_focus_measure_operator(value)

    def set_N(self, N):
        self.N = N

    def set_deltaZ(self, delta_z_um):
        self.deltaZ = delta_z_um / 1000

    def set_crop(self, crop_width, crop_height):
        self.crop_width = crop_width
        self.crop_height = crop_height

    def autofocus(self, focus_map_override=False):
        if self.autofocus_in_progress or (self._focus_thread and self._focus_thread.is_alive()):
            raise RuntimeError("Autofocus is already running")
        state = self.liveController.obs_controller.current_observation_state
        method = state.contrast_af.method if state and state.contrast_af else "legacy"
        if method == "frequency_assisted":
            if self.acquisition_active():
                raise RuntimeError("Frequency-assisted autofocus is manual-only during phase 1")
            if self.piezo_active():
                raise RuntimeError("Frequency-assisted autofocus supports stage Z only; disable piezo focus")
            if state.is_stimulus_only:
                raise ValueError("Stimulus-only observation cannot capture autofocus frames")
            if self.use_focus_map and not focus_map_override:
                raise RuntimeError("Frequency-assisted autofocus needs a measured scan; disable the focus map")
            self._frequency_request = self._resolve_frequency_request(state)
            self._completion.clear()
            self.last_result = self.last_error = None
            self._keep_running.set()
            self.autofocus_in_progress = True
            self._autofocus_worker = AutofocusWorker(
                self, self._on_frequency_completed, self._image_to_display_fn, self._keep_running)
            self._focus_thread = Thread(target=self._run_worker, daemon=True)
            self._focus_thread.start()
            return
        self._frequency_request = None
        self._completion.clear()
        self.last_result = self.last_error = None
        if self.use_focus_map and (not focus_map_override):
            self.autofocus_in_progress = True
            try:
                self.stage.wait_for_idle(1.0)
                pos = self.stage.get_pos()
                # Z is in mm because that is what the navigation controller stores.
                target_z = utils.interpolate_plane(*self.focus_map_coords[:3], (pos.x_mm, pos.y_mm))
                self._log.info(f"Interpolated target z as {target_z} mm from focus map, moving there.")
                self.stage.move_z_to(target_z)
            except (CameraTimeoutError, Exception) as exc:
                self.last_error = exc
                raise
            finally:
                self.autofocus_in_progress = False
                self._completion.set()
                self._finished_fn()
            return
        # stop live
        if self.liveController.is_live:
            self.was_live_before_autofocus = True
            self.liveController.stop_live()
        else:
            self.was_live_before_autofocus = False

        # temporarily disable call back -> image does not go through streamHandler
        if self.camera.get_callbacks_enabled():
            self.callback_was_enabled_before_autofocus = True
            self.camera.enable_callbacks(False)
        else:
            self.callback_was_enabled_before_autofocus = False

        self.autofocus_in_progress = True

        # create a QThread object
        if self._focus_thread and self._focus_thread.is_alive():
            self._keep_running.clear()
            try:
                self._focus_thread.join(1.0)
            except RuntimeError as e:
                self._log.exception("Critical error joining previous autofocus thread.")
                self._finished_fn()
                raise e
            if self._focus_thread.is_alive():
                self._log.error("Previous focus thread failed to join!")
                self._finished_fn()
                raise RuntimeError("Previous focus thread failed to join")

        self._keep_running.set()
        self._autofocus_worker = AutofocusWorker(
            self, self._on_autofocus_completed, self._image_to_display_fn, self._keep_running
        )
        self._focus_thread = Thread(target=self._run_worker, daemon=True)
        self._focus_thread.start()

    def _run_worker(self):
        try:
            self._autofocus_worker.run()
        except (CameraTimeoutError, Exception):
            # The worker already published last_error and logged the traceback.
            pass

    def _on_autofocus_completed(self):
        # re-enable callback
        if self.callback_was_enabled_before_autofocus:
            self.camera.enable_callbacks(True)

        # re-enable live if it's previously on
        if self.was_live_before_autofocus:
            self.liveController.start_live()

        # emit the autofocus finished signal to enable the UI
        self.autofocus_in_progress = False
        self._completion.set()
        self._finished_fn()
        self._log.info("autofocus finished")

    def _on_frequency_completed(self):
        self.autofocus_in_progress = False
        self._completion.set()
        self._finished_fn()

    def cancel_autofocus(self):
        self._keep_running.clear()

    def _resolve_frequency_request(self, state):
        from control.models.contrast_autofocus import ContrastAFSettings
        settings = ContrastAFSettings.model_validate(state.contrast_af.model_dump())
        # The sensor format alone does not distinguish unshifted 12-bit from
        # shifted 16-bit pixels. Require an explicit scale for this experiment.
        if settings.sensor_full_scale is None:
            raise ValueError("Set the sensor full scale for frequency-assisted autofocus")
        axis = self.stage.get_config().Z_AXIS
        if not all(math.isfinite(v) for v in (axis.MIN_POSITION, axis.MAX_POSITION)):
            raise ValueError("Finite stage Z limits are required")
        physical = sorted((axis.raw_to_canonical(axis.MIN_POSITION) * 1000,
                           axis.raw_to_canonical(axis.MAX_POSITION) * 1000))
        start = self.stage.get_pos().z_mm * 1000
        travel_lower = max(start - settings.window_below_um, physical[0])
        travel_upper = min(start + settings.window_above_um, physical[1])
        # CephlaStage's blocking absolute Z move can overshoot a target by this
        # amount in the raw-negative direction to clear backlash. Keep every
        # sampled/final/rollback target one compensation distance inside the
        # user's explicitly permitted travel interval.
        backlash_value = getattr(self.stage, "_BACKLASH_COMPENSATION_DISTANCE_MM", 0)
        backlash_um = float(backlash_value) * 1000 if isinstance(backlash_value, (int, float)) else 0
        if not math.isfinite(backlash_um) or backlash_um < 0:
            raise ValueError("Invalid stage backlash compensation distance")
        lower = travel_lower + backlash_um
        upper = travel_upper - backlash_um
        increment = abs(axis.convert_to_real_units(1)) * 1000
        if not math.isfinite(increment) or increment <= 0 or lower >= upper or not lower <= start <= upper:
            raise ValueError("Requested autofocus window has no usable stage travel")
        if min(settings.coarse_step_um, settings.medium_step_um, settings.fine_step_um) < increment:
            raise ValueError("Autofocus step is below one stage microstep")
        return (state.model_copy(deep=True), settings, start, lower, upper, increment,
                travel_lower, travel_upper)

    def wait_till_autofocus_has_completed(self, timeout_s=180):
        if not self._completion.wait(timeout_s):
            raise TimeoutError("Autofocus did not finish within the wait limit")
        if self.last_error is not None:
            raise RuntimeError("Autofocus failed") from self.last_error
        if self.last_result is not None and self.last_result.status != "success":
            raise RuntimeError(f"Autofocus {self.last_result.status}: {self.last_result.error}")
        self._log.info("autofocus wait has completed, exit wait")

    def set_focus_map_use(self, enable):
        if not enable:
            self._log.info("Disabling focus map.")
            self.use_focus_map = False
            return
        if len(self.focus_map_coords) < 3:
            self._log.error("Not enough coordinates (less than 3) for focus map generation, disabling focus map.")
            self.use_focus_map = False
            return
        x1, y1, _ = self.focus_map_coords[0]
        x2, y2, _ = self.focus_map_coords[1]
        x3, y3, _ = self.focus_map_coords[2]

        detT = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
        if detT == 0:
            self._log.error("Your 3 x-y coordinates are linear, cannot use to interpolate, disabling focus map.")
            self.use_focus_map = False
            return

        if enable:
            self._log.info("Enabling focus map.")
            self.use_focus_map = True

    def clear_focus_map(self):
        self.focus_map_coords = []
        self.set_focus_map_use(False)

    def gen_focus_map(self, coord1, coord2, coord3, retract_between_corners: bool = False):
        """
        Navigate to 3 coordinates and get your focus-map coordinates
        by autofocusing there and saving the z-values.
        :param coord1-3: Tuples of (x,y) values, coordinates in mm.
        :param retract_between_corners: bracket each corner move with a Z
            retract to the loading height (``MultiPointController.
            retract_z_between_regions``); the caller owns that flag since this
            controller has no acquisition-parameters access of its own.
        :raise: ValueError if coordinates are all on the same line
        """
        x1, y1 = coord1
        x2, y2 = coord2
        x3, y3 = coord3
        detT = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
        if detT == 0:
            raise ValueError("Your 3 x-y coordinates are linear")

        self.focus_map_coords = []

        for coord in [coord1, coord2, coord3]:
            self._log.info(f"Navigating to coordinates ({coord[0]},{coord[1]}) to sample for focus map")
            move_xy_with_z_retract(
                self.stage,
                coord[0],
                coord[1],
                retract=retract_between_corners,
                z_target_mm=None,
                home_z_mm=control._def.OBJECTIVE_RETRACTED_POS_MM,
                log=self._log,
            )

            self._log.info("Autofocusing")
            self.autofocus(True)
            self.wait_till_autofocus_has_completed()
            pos = self.stage.get_pos()

            self._log.info(f"Adding coordinates ({pos.x_mm},{pos.y_mm},{pos.z_mm}) to focus map")
            self.focus_map_coords.append((pos.x_mm, pos.y_mm, pos.z_mm))

        self._log.info("Generated focus map.")

    def add_current_coords_to_focus_map(self):
        if len(self.focus_map_coords) >= 3:
            self._log.info("Replacing last coordinate on focus map.")
        self.stage.wait_for_idle(timeout_s=0.5)
        self._log.info("Autofocusing")
        self.autofocus(True)
        self.wait_till_autofocus_has_completed()
        pos = self.stage.get_pos()
        x = pos.x_mm
        y = pos.y_mm
        z = pos.z_mm
        if len(self.focus_map_coords) >= 2:
            x1, y1, _ = self.focus_map_coords[0]
            x2, y2, _ = self.focus_map_coords[1]
            x3 = x
            y3 = y

            detT = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
            if detT == 0:
                raise ValueError(
                    "Your 3 x-y coordinates are linear. Navigate to a different coordinate or clear and try again."
                )
        if len(self.focus_map_coords) >= 3:
            self.focus_map_coords.pop()
        self.focus_map_coords.append((x, y, z))
        self._log.info(f"Added triple ({x},{y},{z}) to focus map")
