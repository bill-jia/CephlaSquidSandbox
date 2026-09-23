import threading
from typing import Callable, Optional, TypeVar

import time
import numpy as np

import squid.logging
from control import utils
import control._def
from control.core.live_controller import LiveController
from control.microcontroller import Microcontroller
from control.NL5 import NL5
from squid.abc import AbstractCamera, AbstractStage

AutoFocusController = TypeVar("AutoFocusController")


class AutofocusWorker:
    def __init__(
        self,
        autofocusController,
        finished_fn: Callable[[], None],
        image_to_display_fn: Callable[[np.ndarray], None],
        keep_running: threading.Event,
    ):
        self.autofocusController: AutoFocusController = autofocusController
        self._finished_fn = finished_fn
        self._image_to_display_fn = image_to_display_fn
        self._keep_running: threading.Event = keep_running
        self._log = squid.logging.get_logger(self.__class__.__name__)

        self.camera: AbstractCamera = self.autofocusController.camera
        self.microcontroller: Microcontroller = self.autofocusController.microcontroller
        self.stage: AbstractStage = self.autofocusController.stage
        self.liveController: LiveController = self.autofocusController.liveController
        self.nl5: Optional[NL5] = self.autofocusController.nl5

        self.N = self.autofocusController.N
        self.focus_measure_operator = self.autofocusController.focus_measure_operator
        self.deltaZ = self.autofocusController.deltaZ

        self.crop_width = self.autofocusController.crop_width
        self.crop_height = self.autofocusController.crop_height

    def run(self):
        try:
            self.run_autofocus()
        finally:
            self._finished_fn()

    def _acquire_frame(self):
        obs = self.liveController.obs_controller
        state = obs.current_observation_state
        ic = obs.ic
        cleanup = None
        timeout_s = 5 * self.camera.get_total_frame_time() / 1000.0 + 2
        continuous = self.liveController.trigger_mode == control._def.TriggerMode.CONTINUOUS
        try:
            if not continuous:
                deadline = time.monotonic() + timeout_s
                while not self.camera.get_ready_for_trigger():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Autofocus camera did not become ready for a trigger")
                    time.sleep(0.001)

            if state is not None and state.is_waveform_driven and not continuous:
                from control.core.waveform_capture import (
                    apply_illumination_for_waveform_capture,
                    arm_nidaq_pulse_for_capture,
                )
                apply_illumination_for_waveform_capture(obs.microscope, state, self._log)
                cleanup = arm_nidaq_pulse_for_capture(obs.microscope, state, log=self._log)
            if cleanup is None and state is not None:
                ic.apply_observation_illumination(
                    state.active_illuminator_states, turn_on=True, force_hardware=True
                )
            settle_s = control._def.Acquisition.ILLUMINATION_SETTLE_MS / 1000.0
            if continuous:
                # Let the exposure that overlapped the Z move finish first.
                settle_s += self.camera.get_total_frame_time() / 1000.0
            if settle_s > 0:
                time.sleep(settle_s)
            trigger = None if continuous else lambda: self.camera.send_trigger(
                illumination_time=self.camera.get_exposure_time()
            )
            return self.camera.capture_frame(trigger, timeout_s)
        finally:
            try:
                if cleanup is not None:
                    cleanup()
            finally:
                ic.turn_off_all(preserve_logical_state=True)

    def run_autofocus(self):
        was_streaming = self.camera.get_is_streaming()
        try:
            if not was_streaming:
                self.camera.start_streaming()
            self._run_autofocus_scan()
        finally:
            if not was_streaming:
                self.camera.stop_streaming()

    def _run_autofocus_scan(self):
        # @@@ to add: increase gain, decrease exposure time
        # @@@ can move the execution into a thread - done 08/21/2021
        self._log.info(f"Starting autofocus with {self.N} steps and deltaZ={self.deltaZ} mm")
        measurements = []
        focus_measure_max = 0
        z_af_offset = self.deltaZ * round(self.N / 2)
        steps_moved = 0
        self.stage.move_z(-z_af_offset)
        try:
            for i in range(self.N):
                if not self._keep_running.is_set():
                    self._log.warning("Signal to abort autofocus received, aborting!")
                    break
                self.stage.move_z(self.deltaZ)
                steps_moved += 1
                image = self._acquire_frame()
                if image is None:
                    raise RuntimeError("Autofocus received no camera frame")
                image = utils.crop_image(image, self.crop_width, self.crop_height)
                self._image_to_display_fn(image)

                timestamp_0 = time.time()
                focus_measure = utils.calculate_focus_measure(image, self.focus_measure_operator)
                self._log.info(
                    "Calculating focus measure %s took %.3f seconds",
                    self.focus_measure_operator, time.time() - timestamp_0,
                )
                measurements.append((i, focus_measure))
                self._log.info(
                    "%s %s vs max focus measure %s at z=%s mm",
                    i, focus_measure, focus_measure_max, self.stage.get_pos().z_mm,
                )
                focus_measure_max = max(focus_measure, focus_measure_max)
                if focus_measure < focus_measure_max * control._def.AF.STOP_THRESHOLD:
                    break
        except BaseException:
            # A missing frame is a failed scan, never an in-focus plane.
            self.stage.move_z(z_af_offset - steps_moved * self.deltaZ)
            raise

        # Approach the chosen plane in the same direction as the scan.
        self.stage.move_z(-steps_moved * self.deltaZ)
        if not measurements:
            self.stage.move_z(z_af_offset)
            return
        idx_in_focus = max(measurements, key=lambda item: item[1])[0]
        self.stage.move_z((idx_in_focus + 1) * self.deltaZ)
        if idx_in_focus == 0:
            self._log.info("moved to the bottom end of the AF range")
        if idx_in_focus == self.N - 1:
            self._log.info("moved to the top end of the AF range")
