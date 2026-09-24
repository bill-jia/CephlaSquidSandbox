"""A standalone, scoped stage-Z and camera capture session."""

import time
import sys
import threading

from control._def import Acquisition, TriggerMode
from control.core.contrast_autofocus.search import FocusSample, StageFault, CaptureFailure, CaptureCancelled, SearchStopped
from control._sdk_watchdog import CameraTimeoutError
from squid.abc import CameraError
from control import utils
import cv2


class StageCaptureSession:
    def __init__(self, controller, state, lower_um, upper_um, cancelled, max_time_s=None,
                 travel_lower_um=None, travel_upper_um=None,
                 apply_camera_live_geometry=False, apply_optical_state=False,
                 wait_for_hardware=None):
        self.controller = controller
        self.camera = controller.camera
        capture_lock = getattr(self.camera, "_capture_frame_lock", None)
        if isinstance(capture_lock, type(threading.Lock())) and capture_lock.locked():
            raise RuntimeError("Another camera capture is in progress")
        self.stage = controller.stage
        self.live = controller.liveController
        self.state = state
        self.crop_width = controller.crop_width
        self.crop_height = controller.crop_height
        self.lower_um, self.upper_um = lower_um, upper_um
        self.travel_lower_um = lower_um if travel_lower_um is None else travel_lower_um
        self.travel_upper_um = upper_um if travel_upper_um is None else travel_upper_um
        self.cancelled = cancelled
        self.deadline = time.monotonic() + max_time_s if max_time_s is not None else None
        self.was_live = self.live.is_live
        self.was_streaming = self.camera.get_is_streaming()
        self.callbacks_enabled = self.camera.get_callbacks_enabled()
        self.mode = self.live.trigger_mode
        self.original_exposure_ms = float(self.camera.get_exposure_time())
        try:
            self.original_gain = float(self.camera.get_analog_gain())
        except NotImplementedError:
            # Some cameras expose no analog-gain API. Leave their gain untouched.
            self.original_gain = None
            if state.camera_settings is not None:
                controller._log.warning(
                    "Contrast AF is leaving analog gain unchanged: this camera does not support gain control")
        self.capture_exposure_ms = state.exposure_time if state.camera_settings else self.original_exposure_ms
        self.capture_gain = (
            state.analog_gain if state.camera_settings else self.original_gain
        ) if self.original_gain is not None else None
        self.geometry = (self.camera.get_binning(), self.camera.get_region_of_interest(),
                         self.camera.get_pixel_format(), self.camera.get_camera_mode())
        self.original_geometry = self.geometry
        self.apply_camera_live_geometry = apply_camera_live_geometry
        self.apply_optical_state = apply_optical_state
        self.wait_for_hardware = wait_for_hardware
        self.original_optical_state = None
        self.original_logical_state = None
        self.entered = False
        self.cleanup_errors = []

    def __enter__(self):
        try:
            if self.was_live:
                self.live.stop_live()
            self.camera.enable_callbacks(False)
            # Drain live delivery and give this session a fresh trigger stream.
            if self.camera.get_is_streaming():
                self.camera.stop_streaming()
            if self.apply_optical_state:
                obs = self.live.obs_controller
                self.original_logical_state = obs.current_observation_state
                self.original_optical_state = obs.read_capture_optical_state()
                self._apply_optical_state(self.state)
            if self.apply_camera_live_geometry and self.state.camera_live is not None:
                snap = self.state.camera_live
                if snap.pixel_format is not None:
                    from squid.config import CameraPixelFormat
                    pixel_format = next((value for value in CameraPixelFormat
                                         if snap.pixel_format in (value.name, value.value)), None)
                    if pixel_format is None:
                        raise ValueError(f"Unknown AF camera pixel format {snap.pixel_format!r}")
                    if self.camera.get_pixel_format() != pixel_format:
                        self.camera.set_pixel_format(pixel_format)
                if snap.camera_mode is not None and self.camera.get_camera_mode() != snap.camera_mode:
                    self.camera.set_camera_mode(snap.camera_mode)
                if self.camera.get_binning() != (snap.binning_x, snap.binning_y):
                    self.camera.set_binning(snap.binning_x, snap.binning_y)
                if snap.roi_width > 0 and snap.roi_height > 0:
                    roi = (snap.roi_offset_x, snap.roi_offset_y, snap.roi_width, snap.roi_height)
                    if self.camera.get_region_of_interest() != roi:
                        self.camera.set_region_of_interest(*roi)
                self.geometry = (self.camera.get_binning(), self.camera.get_region_of_interest(),
                                 self.camera.get_pixel_format(), self.camera.get_camera_mode())
                if self.geometry[0] != (snap.binning_x, snap.binning_y):
                    raise RuntimeError("AF camera binning did not apply")
                if snap.roi_width > 0 and snap.roi_height > 0 and self.geometry[1] != roi:
                    raise RuntimeError("AF camera ROI did not apply")
                if snap.pixel_format is not None and self.geometry[2] != pixel_format:
                    raise RuntimeError("AF camera pixel format did not apply")
                if snap.camera_mode is not None and self.geometry[3] != snap.camera_mode:
                    raise RuntimeError("AF camera mode did not apply")
            if self.mode != TriggerMode.SOFTWARE:
                self.live.set_trigger_mode(TriggerMode.SOFTWARE)
            if self.live.trigger_mode != TriggerMode.SOFTWARE:
                raise RuntimeError("AF software trigger mode did not apply")
            if self.camera.get_exposure_time() != self.capture_exposure_ms:
                self.camera.set_exposure_time(self.capture_exposure_ms)
            if self.capture_gain is not None and self.camera.get_analog_gain() != self.capture_gain:
                self.camera.set_analog_gain(self.capture_gain)
            self.capture_exposure_ms = float(self.camera.get_exposure_time())
            if self.capture_gain is not None:
                self.capture_gain = float(self.camera.get_analog_gain())
            self.camera.start_streaming()
            self.entered = True
            return self
        except BaseException as exc:
            self.__exit__(type(exc), exc, exc.__traceback__)
            raise

    def _apply_optical_state(self, state):
        """Apply requested optics and verify device readback before exposure."""
        obs = self.live.obs_controller
        obs.apply_capture_optical_state(state)
        if self.wait_for_hardware is not None:
            self.wait_for_hardware()
        actual = obs.read_capture_optical_state()
        requested_filters = obs.capture_filter_targets(state)
        requested_confocal = ({key: value for key, value in state.confocal_hardware_settings.model_dump().items()
                               if value is not None} if state.confocal_hardware_settings is not None else {})
        before = self.original_optical_state
        if (actual["confocal_mode"] != state.confocal_mode or
                (state.confocal_mode and actual["motor_running"] is False) or
                any(actual["filters"].get(key) != value for key, value in requested_filters.items()) or
                any(actual["confocal"].get(key) != value for key, value in requested_confocal.items()) or
                any(actual["filters"].get(key) != value for key, value in before["filters"].items()
                    if key not in requested_filters) or
                any(actual["confocal"].get(key) != value for key, value in before["confocal"].items()
                    if key not in requested_confocal)):
            raise RuntimeError("Autofocus optical state did not apply")

    def _restore_optical_state(self):
        obs = self.live.obs_controller
        obs.restore_capture_optical_state(self.original_optical_state)
        if self.wait_for_hardware is not None:
            self.wait_for_hardware()
        actual = obs.read_capture_optical_state()
        before = self.original_optical_state
        if (any(actual[key] != before[key] for key in
                ("confocal_mode", "filters", "confocal", "auto_switch")) or
                (before["confocal_mode"] and actual["motor_running"] is False)):
            raise RuntimeError("Imaging optical state restoration did not verify")

    def __exit__(self, exc_type, exc, tb):
        errors = []
        # Camera ownership ends inside capture_frame, after its delivery barrier.
        try:
            if self.camera.get_is_streaming():
                self.camera.stop_streaming()
        except (CameraTimeoutError, Exception) as e:
            errors.append(e)
        if self.original_optical_state is not None:
            try:
                self._restore_optical_state()
            except Exception as e:
                errors.append(e)
            finally:
                self.live.obs_controller.current_observation_state = self.original_logical_state
        if self.apply_camera_live_geometry:
            original_binning, original_roi, original_format, original_mode = self.original_geometry
            for getter, setter, args in (
                (self.camera.get_pixel_format, self.camera.set_pixel_format, (original_format,)),
                (self.camera.get_camera_mode, self.camera.set_camera_mode, (original_mode,)),
                (self.camera.get_binning, self.camera.set_binning, original_binning),
                (self.camera.get_region_of_interest, self.camera.set_region_of_interest, original_roi),
            ):
                try:
                    if getter() != (args if len(args) > 1 else args[0]):
                        setter(*args)
                except (CameraTimeoutError, Exception) as e:
                    errors.append(e)
            try:
                restored = (self.camera.get_binning(), self.camera.get_region_of_interest(),
                            self.camera.get_pixel_format(), self.camera.get_camera_mode())
                if restored != self.original_geometry:
                    errors.append(RuntimeError("AF camera geometry restoration did not verify"))
            except (CameraTimeoutError, Exception) as e:
                errors.append(e)
        if self.live.trigger_mode != self.mode:
            try:
                self.live.set_trigger_mode(self.mode)
            except (CameraTimeoutError, Exception) as e:
                errors.append(e)
        if self.live.trigger_mode != self.mode:
            errors.append(RuntimeError("AF trigger mode restoration did not verify"))
        settings_to_restore = [
            (self.camera.get_exposure_time, self.camera.set_exposure_time, self.original_exposure_ms),
        ]
        if self.original_gain is not None:
            settings_to_restore.append((self.camera.get_analog_gain, self.camera.set_analog_gain, self.original_gain))
        for getter, setter, original in settings_to_restore:
            try:
                if getter() != original:
                    setter(original)
                if getter() != original:
                    raise RuntimeError("AF exposure or gain restoration did not verify")
            except (CameraTimeoutError, Exception) as e:
                errors.append(e)
        # A failed geometry restore must not restart live capture or release
        # callbacks into acquisition. The owner will abort the run.
        if not errors:
            try:
                self.camera.enable_callbacks(self.callbacks_enabled)
                if self.camera.get_callbacks_enabled() != self.callbacks_enabled:
                    raise RuntimeError("AF camera callback restoration did not verify")
            except (CameraTimeoutError, Exception) as e:
                errors.append(e)
        if self.was_live and not errors:
            try:
                self.live.start_live()
            except (CameraTimeoutError, Exception) as e:
                errors.append(e)
        elif self.was_streaming and not errors:
            try:
                self.camera.start_streaming()
            except (CameraTimeoutError, Exception) as e:
                errors.append(e)
        self.cleanup_errors.extend(str(e) for e in errors)
        if errors and exc is None:
            result = getattr(self.controller, "last_result", None)
            if result is None or result.status == "success":
                if result is not None:
                    result.status = "hardware_failed"
                    result.error = "Autofocus capture cleanup failed"
                raise RuntimeError("Autofocus capture cleanup failed: " + "; ".join(self.cleanup_errors))

    def _move(self, z_um, check_cancel=True):
        if check_cancel and self.cancelled():
            raise InterruptedError("Autofocus cancelled")
        if not self.lower_um <= z_um <= self.upper_um:
            raise ValueError("Autofocus move exceeds bounded interval")
        axis = self.stage.get_config().Z_AXIS
        backlash_value = getattr(self.stage, "_BACKLASH_COMPENSATION_DISTANCE_MM", 0)
        backlash_mm = float(backlash_value) if isinstance(backlash_value, (int, float)) else 0
        if backlash_mm:
            raw_target = axis.canonical_to_raw(z_um / 1000)
            raw_current = axis.canonical_to_raw(self.stage.get_pos().z_mm)
            if raw_target < raw_current:
                raw_overshoot = max(raw_target - backlash_mm, axis.MIN_POSITION)
                overshoot_um = axis.raw_to_canonical(raw_overshoot) * 1000
                if not self.travel_lower_um <= overshoot_um <= self.travel_upper_um:
                    raise ValueError("Backlash compensation would exceed autofocus travel bounds")
        try:
            self.stage.move_z_to(z_um / 1000)
            self.stage.wait_for_idle(2.0)
            return self.stage.get_pos().z_mm * 1000
        except Exception as exc:
            raise StageFault(str(exc)) from exc

    def move_to(self, z_um):
        return self._move(z_um)

    def restore_to(self, z_um):
        return self._move(z_um, check_cancel=False)

    def capture_at(self, z_um):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise SearchStopped("range_exhausted", "Autofocus time budget exhausted")
        if (self.camera.get_binning(), self.camera.get_region_of_interest(),
                self.camera.get_pixel_format(), self.camera.get_camera_mode()) != self.geometry:
            raise CaptureFailure("Camera geometry or pixel format changed during autofocus")
        if (self.camera.get_exposure_time() != self.capture_exposure_ms or
                (self.capture_gain is not None and self.camera.get_analog_gain() != self.capture_gain)):
            raise CaptureFailure("Camera exposure or gain changed during autofocus")
        actual = self._move(z_um)
        if self.cancelled():
            raise CaptureCancelled(actual)
        try:
            timeout_s = 5 * self.camera.get_total_frame_time() / 1000 + 2
        except (CameraError, CameraTimeoutError, OSError) as exc:
            raise CaptureFailure(str(exc), actual_z_um=actual) from exc
        if self.deadline is not None:
            timeout_s = min(timeout_s, max(0, self.deadline - time.monotonic()))
        deadline = time.monotonic() + timeout_s
        try:
            ready = self.camera.get_ready_for_trigger()
        except (CameraError, CameraTimeoutError, OSError) as exc:
            raise CaptureFailure(str(exc), actual_z_um=actual) from exc
        while not ready:
            if self.cancelled():
                raise CaptureCancelled(actual)
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise SearchStopped("range_exhausted", "Autofocus time budget exhausted", actual_z_um=actual)
            if time.monotonic() >= deadline:
                raise CaptureFailure("Autofocus camera not ready for trigger", actual_z_um=actual)
            time.sleep(0.005)
            try:
                ready = self.camera.get_ready_for_trigger()
            except (CameraError, CameraTimeoutError, OSError) as exc:
                raise CaptureFailure(str(exc), actual_z_um=actual) from exc
        obs = self.live.obs_controller
        cleanup = None
        try:
            if self.state.is_waveform_driven:
                from control.core.waveform_capture import apply_illumination_for_waveform_capture, arm_nidaq_pulse_for_capture
                apply_illumination_for_waveform_capture(obs.microscope, self.state, self.controller._log)
                cleanup = arm_nidaq_pulse_for_capture(obs.microscope, self.state, log=self.controller._log)
            else:
                obs.ic.apply_observation_illumination(
                    self.state.active_illuminator_states, turn_on=True, force_hardware=True)
            settle = Acquisition.ILLUMINATION_SETTLE_MS / 1000
            if settle:
                end = time.monotonic() + settle
                while time.monotonic() < end:
                    if self.cancelled():
                        raise CaptureCancelled(actual)
                    if self.deadline is not None and time.monotonic() >= self.deadline:
                        raise SearchStopped("range_exhausted", "Autofocus time budget exhausted", actual_z_um=actual)
                    time.sleep(min(0.01, end - time.monotonic()))
            try:
                image = self.camera.capture_frame(
                    lambda: self.camera.send_trigger(illumination_time=self.camera.get_exposure_time()),
                    timeout_s, cancelled=lambda: self.cancelled() or (
                        self.deadline is not None and time.monotonic() >= self.deadline))
            except InterruptedError as exc:
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    raise SearchStopped("range_exhausted", "Autofocus time budget exhausted",
                                        actual_z_um=actual, frame_attempted=True) from exc
                raise CaptureCancelled(actual) from exc
            except (CameraError, CameraTimeoutError, OSError, TimeoutError) as exc:
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    raise SearchStopped("range_exhausted", "Autofocus time budget exhausted",
                                        actual_z_um=actual, frame_attempted=True) from exc
                raise CaptureFailure(str(exc), actual_z_um=actual, frame_attempted=True) from exc
            image = utils.crop_image(image, self.crop_width, self.crop_height)
            if image.ndim == 3:
                image = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
            return FocusSample(image, actual)
        finally:
            primary = sys.exc_info()[1]
            cleanup_errors = []
            if cleanup is not None:
                try:
                    cleanup()
                except (CameraTimeoutError, Exception) as exc:
                    cleanup_errors.append(exc)
            try:
                obs.ic.turn_off_all(preserve_logical_state=True)
            except (CameraTimeoutError, Exception) as exc:
                cleanup_errors.append(exc)
            self.cleanup_errors.extend(str(exc) for exc in cleanup_errors)
            if cleanup_errors and primary is None:
                raise CaptureFailure("Autofocus illumination cleanup failed", actual_z_um=actual,
                                     frame_attempted=True) from cleanup_errors[0]
