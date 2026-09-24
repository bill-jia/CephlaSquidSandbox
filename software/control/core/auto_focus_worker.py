import threading
from typing import Callable, Optional, TypeVar

import time
import numpy as np

import squid.logging
from control import utils
import control._def
from control._sdk_watchdog import CameraTimeoutError
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
            if getattr(self.autofocusController, "_frequency_request", None) is not None and \
                    self.autofocusController._frequency_request[0].contrast_af.method == "frequency_assisted":
                self._run_frequency_assisted()
            else:
                self.run_autofocus()
        except (CameraTimeoutError, Exception) as exc:
            self.autofocusController.last_error = exc
            self._log.exception("Autofocus failed")
            raise
        finally:
            self._finished_fn()

    def _run_frequency_assisted(self):
        from control.core.contrast_autofocus.capture import StageCaptureSession
        from control.core.contrast_autofocus.metrics import energy_factor
        from control.core.contrast_autofocus.service import run_contrast_search
        state, settings, start, lower, upper, increment, travel_lower, travel_upper = (
            self.autofocusController._frequency_request)
        cancelled = lambda: not self._keep_running.is_set()
        session = StageCaptureSession(self.autofocusController, state, lower, upper, cancelled,
                                      max_time_s=settings.max_time_s,
                                      travel_lower_um=travel_lower, travel_upper_um=travel_upper,
                                      apply_optical_state=True)
        try:
            with session:
                def capture_and_display(z_um):
                    sample = session.capture_at(z_um)
                    self._image_to_display_fn(sample.image)
                    return sample

                result = run_contrast_search(
                    capture_at=capture_and_display, move_to=session.move_to, settings=settings,
                    legacy_options={},
                    score=lambda image: utils.calculate_focus_measure(image, self.focus_measure_operator),
                    energy=lambda image: energy_factor(image, threshold=settings.energy_threshold,
                                                       sensor_full_scale=settings.sensor_full_scale),
                    cancelled=cancelled, starting_z_um=start, lower_z_um=lower, upper_z_um=upper,
                    increment_um=increment, exposure_ms=session.capture_exposure_ms,
                    restore_to=session.restore_to,
                    metric=self.focus_measure_operator.value + "/existing-v1",
                    trigger_route=self.camera.describe_trigger_routing())
                self.autofocusController.last_result = result
        finally:
            if self.autofocusController.last_result is not None:
                self.autofocusController.last_result.cleanup_errors.extend(
                    getattr(session, "cleanup_errors", []))

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
        from control.core.contrast_autofocus.search import FocusSample, StageFault
        from control.core.contrast_autofocus.service import run_legacy_scan

        z_mm = self.stage.get_pos().z_mm
        start_um = float(z_mm) * 1000 if isinstance(z_mm, (int, float)) else 0.0
        current_um = start_um

        def move_to(target_um):
            nonlocal current_um
            try:
                self.stage.move_z((target_um - current_um) / 1000)
                current_um = target_um
                return current_um
            except Exception as exc:
                raise StageFault(str(exc)) from exc

        def capture_at(target_um):
            actual_um = move_to(target_um)
            image = self._acquire_frame()
            if image is None:
                return FocusSample(None, actual_um)
            image = utils.crop_image(image, self.crop_width, self.crop_height)
            self._image_to_display_fn(image)
            return FocusSample(image, actual_um)

        result = run_legacy_scan(
            capture_at=capture_at, move_to=move_to, restore_to=move_to,
            score=lambda image: utils.calculate_focus_measure(image, self.focus_measure_operator),
            cancelled=lambda: not self._keep_running.is_set(),
            starting_z_um=start_um, step_um=self.deltaZ * 1000, count=self.N,
            stop_threshold=control._def.AF.STOP_THRESHOLD,
            metric=self.focus_measure_operator.value,
            settings={"method": "legacy", "count": self.N,
                      "step_um": self.deltaZ * 1000,
                      "crop_width": self.crop_width, "crop_height": self.crop_height},
            trigger_route=getattr(self.camera, "describe_trigger_routing", lambda: "unknown")())
        self.autofocusController.last_result = result
        if result.status == "cancelled":
            return
        if result.status != "success":
            if result.error and "timed out" in result.error.lower():
                raise TimeoutError(result.error)
            raise RuntimeError(result.error or result.status)
