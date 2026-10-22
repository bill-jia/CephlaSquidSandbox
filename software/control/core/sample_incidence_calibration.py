"""Bounded, cancellable sample/illumination calibration acquisition workflow."""

from dataclasses import dataclass
import threading
import time
import traceback
import sys

import numpy as np

from control.core.sample_incidence_analysis import (
    assess_flatness,
    focus_peaks,
    incidence_from_planes,
    preview,
    register_fields,
    separate_illumination,
    texture_scores,
)
from control.core.sample_incidence_recording import position_snapshot, utc_now


class CalibrationCancelled(Exception):
    pass


@dataclass(frozen=True)
class AlignmentSettings:
    dither_fraction: float = 0.33
    focus_half_range_um: float = 2.0
    focus_steps: int = 9
    z_baseline_um: float = 4.0
    max_plane_span_um: float = 0.5
    max_residual_um: float = 0.25
    black_level: float = 0.0
    settle_s: float = 0.05
    repeat: bool = True

    def validate(self):
        numbers = (
            self.dither_fraction,
            self.focus_half_range_um,
            self.z_baseline_um,
            self.max_plane_span_um,
            self.max_residual_um,
            self.black_level,
            self.settle_s,
        )
        if not np.isfinite(numbers).all():
            raise ValueError("Calibration settings must be finite.")
        if not 0.05 <= self.dither_fraction <= 0.45:
            raise ValueError("Dither radius must be 0.05–0.45 FOV, preserving overlap and staying within one FOV.")
        if not 0.1 <= self.focus_half_range_um <= 20 or not 0.2 <= self.z_baseline_um <= 40:
            raise ValueError("Focus range or incidence Z baseline is outside supported bounds.")
        if self.focus_steps < 5 or self.focus_steps > 31 or self.focus_steps % 2 != 1:
            raise ValueError("Use an odd focus plane count between 5 and 31.")
        if min(self.max_plane_span_um, self.max_residual_um) <= 0 or self.black_level < 0 or self.settle_s < 0:
            raise ValueError("Tolerances must be positive, and black level/settling nonnegative.")


def dither_offsets(radius_um):
    """Center first, then an overlapping 3×3 neighborhood. Never walk cumulatively."""
    return np.asarray([(0, 0), (-1, -1), (0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0)], float) * radius_um


class AlignmentHardware:
    """Own streaming/illumination only during this session and restore on every exit.

    Invoked by the GUI only after excluding other acquisition. The original
    laser-AF reference is used without replacing/reinitializing it. AF corrections
    are bounded near that reference; deliberately offset Z planes are not AF locked.
    """

    def __init__(self, microscope, laser_af=None, cancel=None):
        self.scope = microscope
        self.camera = microscope.camera
        self.stage = microscope.stage
        self.live = microscope.live_controller
        self.af = laser_af
        self.cancel = cancel if cancel is not None else threading.Event()
        self.piezo = getattr(microscope.addons, "piezo_stage", None)
        self.focus_live = getattr(microscope, "live_controller_focus", None)
        drivers = getattr(microscope, "low_level_drivers", None)
        self.mcu = getattr(drivers, "microcontroller", None)
        self.illumination = getattr(microscope, "illumination_controller", None)
        self._saved = None

    def _lights_off(self):
        if self.illumination is not None:
            self.illumination.turn_off_all_hardware_preserving_state()
        else:
            self.scope.obs_controller.turn_off_illumination()

    def _restore_logical_illumination(self, snapshot):
        # Do not use IlluminationController.restore here: it logs individual
        # failures instead of propagating them. Restoration must be observable.
        for name, state in snapshot.channel_states.items():
            self._wait_mcu()
            self.illumination.set_channel_intensity(name, state.intensity)
            self._wait_mcu()
            self.illumination.set_channel_state(name, state.is_on)
            self._wait_mcu()

    def _wait_mcu(self):
        # Stage moves already block, but illumination and a piezo DAC write may
        # return before the MCU acknowledges them. Never leave their command in
        # flight when handing control back to the GUI. Do not reset/abort the MCU.
        if self.mcu is not None:
            self.mcu.wait_till_operation_is_completed(timeout_limit_s=10)

    def _stop_live_and_drain(self, controller):
        timer = getattr(controller, "timer_trigger", None)
        controller.stop_live()
        if timer is not None and timer is not threading.current_thread():
            timer.join(timeout=max(10, 3 * self.camera.get_exposure_time() / 1000))
            if timer.is_alive():
                raise TimeoutError(
                    "A previous live trigger is still completing; calibration cannot take over the camera."
                )
        # A trigger that was already running can have scheduled one last timer
        # just as stop_live cancelled the old one. Its thread has now returned.
        trailing = getattr(controller, "timer_trigger", None)
        if trailing is not None and trailing is not timer:
            trailing.cancel()
            trailing.join(timeout=2)
            if trailing.is_alive():
                raise TimeoutError("A pending live trigger could not be drained.")
        if hasattr(controller, "timer_trigger"):
            controller.timer_trigger = None

    @property
    def af_ready(self):
        props = getattr(self.af, "laser_af_properties", None)
        return bool(
            self.af is not None
            and self.af.is_initialized
            and props is not None
            and props.has_reference
            and props.x_reference is not None
        )

    def check_cancelled(self):
        if self.cancel.is_set():
            raise CalibrationCancelled()

    def __enter__(self):
        from squid.abc import CameraAcquisitionMode

        pos = self.stage.get_pos()
        self._saved = dict(
            x=pos.x_mm,
            y=pos.y_mm,
            z=pos.z_mm,
            piezo=None if self.piezo is None else self.piezo.position,
            mode=self.camera.get_acquisition_mode(),
            streaming=self.camera.get_is_streaming(),
            callbacks=self.camera.get_callbacks_enabled(),
            live=self.live.is_live,
            focus_live=bool(self.focus_live is not None and self.focus_live.is_live),
            illumination=None if self.illumination is None else self.illumination.snapshot(),
            illumination_streaming=False if self.illumination is None else self.illumination.is_streaming_active(),
            # The compositor maintains this authoritative hardware cache
            # separately from logical/UI on/off flags, including timed gates.
            illumination_hardware={} if self.illumination is None else dict(self.illumination._hardware_asserted),
        )
        try:
            self._stop_live_and_drain(self.live)
            if self._saved["focus_live"]:
                self._stop_live_and_drain(self.focus_live)
            self._wait_mcu()
            if self.illumination is not None:
                self.illumination.set_streaming_active(False)
                self._wait_mcu()
            self.camera.stop_streaming()
            self.camera.enable_callbacks(False)
            self.camera.set_acquisition_mode(CameraAcquisitionMode.SOFTWARE_TRIGGER)
            self.camera.start_streaming()
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._saved is None:
            return
        saved = self._saved
        errors = []

        def restore(action):
            try:
                action()
                return True
            except Exception as error:
                errors.append(str(error))
                return False

        hardware_idle = restore(self._wait_mcu)
        if hardware_idle:
            restore(self._lights_off)
            hardware_idle = restore(self._wait_mcu)
        if hardware_idle:
            moves = [
                lambda: self.stage.move_x_to(saved["x"]),
                lambda: self.stage.move_y_to(saved["y"]),
                lambda: self.stage.move_z_to(saved["z"]),
            ]
            if self.piezo is not None:
                moves.append(lambda: self.piezo.move_to(saved["piezo"]))
            for move in moves:
                if not restore(self._wait_mcu):
                    break
                restore(move)
            restore(self._wait_mcu)
        restore(self.camera.stop_streaming)
        restore(lambda: self.camera.set_acquisition_mode(saved["mode"]))
        restore(lambda: self.camera.enable_callbacks(saved["callbacks"]))
        if not errors and saved["illumination"] is not None:
            restore(lambda: self._restore_logical_illumination(saved["illumination"]))
            if not saved["live"]:
                if saved["illumination_streaming"]:
                    restore(lambda: self.illumination.set_streaming_active(True))
                elif any(saved["illumination_hardware"].values()):
                    # Restore manually asserted illumination, then put logical
                    # UI flags back while the streaming gate remains inactive.
                    restore(
                        lambda: self.illumination.set_channel_states_batch(
                            saved["illumination_hardware"], force_hardware=True
                        )
                    )
                    restore(lambda: self._restore_logical_illumination(saved["illumination"]))
                restore(self._wait_mcu)
        if not errors and (saved["streaming"] or saved["live"]):
            restore(self.camera.start_streaming)
        if not errors and saved["live"]:
            restore(self.live.start_live)
        if not errors and saved["focus_live"]:
            restore(self.focus_live.start_live)
        self._saved = None
        if errors:
            raise RuntimeError("Calibration restoration failed: " + "; ".join(errors)) from exc

    def _check_limit(self, axis_name, target_mm):
        config = self.stage.get_config()
        axis = getattr(config, axis_name + "_AXIS")
        raw = axis.canonical_to_raw(float(target_mm))
        if not axis.MIN_POSITION <= raw <= axis.MAX_POSITION:
            raise ValueError(f"{axis_name} target {target_mm:.6f} mm exceeds configured stage travel.")

    def validate_path(self, offsets_um, z_margin_um):
        pos = self.stage.get_pos()
        for dx, dy in offsets_um:
            self._check_limit("X", pos.x_mm + dx / 1000)
            self._check_limit("Y", pos.y_mm + dy / 1000)
        for offset in (-z_margin_um, z_margin_um):
            self._check_limit("Z", pos.z_mm + offset / 1000)

    def move_xy(self, x_um, y_um):
        self.check_cancelled()
        self._check_limit("X", x_um / 1000)
        self._check_limit("Y", y_um / 1000)
        self._wait_mcu()
        self.stage.move_x_to(x_um / 1000)
        self.stage.move_y_to(y_um / 1000)

    def move_z(self, z_um):
        self.check_cancelled()
        self._check_limit("Z", z_um / 1000)
        self._wait_mcu()
        self.stage.move_z_to(z_um / 1000)

    def refocus(self, max_correction_um):
        self.check_cancelled()
        if self.af_ready:
            # Explicitly darken the specimen before AF, including manually
            # asserted light. The main live trigger stays stopped throughout
            # the session, so it cannot reassert fluorescence during AF frames.
            self._wait_mcu()
            self._lights_off()
            self._wait_mcu()
            displacement = self.af.measure_displacement()
            if not np.isfinite(displacement) or abs(displacement) > max_correction_um:
                raise ValueError("Laser AF is too far from its reference; refocus near the reference before starting.")
            if not self.af.move_to_target(0):
                raise ValueError("Laser autofocus correction failed; scan stopped.")
            self._wait_mcu()
        return self.stage.get_pos().z_mm * 1000, 0 if self.piezo is None else self.piezo.position

    def capture(self, settle_s=0.05):
        self.check_cancelled()
        self._wait_mcu()
        if self.cancel.wait(settle_s):
            raise CalibrationCancelled()
        deadline = time.monotonic() + max(3, 4 * self.camera.get_exposure_time() / 1000)
        while not self.camera.get_ready_for_trigger():
            self.check_cancelled()
            if time.monotonic() > deadline:
                raise TimeoutError("Camera did not become ready for calibration capture.")
            self.cancel.wait(0.005)
        last_id = self.camera.get_frame_id()
        cleanup = None
        try:
            config = self.scope.obs_controller.current_observation_state
            if config is not None and getattr(config, "is_waveform_driven", False):
                from control.core.waveform_capture import (
                    apply_illumination_for_waveform_capture,
                    arm_nidaq_pulse_for_capture,
                )
                import squid.logging

                if getattr(self.scope.addons, "nidaq", None) is None:
                    raise ValueError("Timed illumination needs a configured NIDAQ for this calibration.")
                log = squid.logging.get_logger(__name__)
                apply_illumination_for_waveform_capture(self.scope, config, log)

                def failed(*args):
                    raise RuntimeError("The timed illumination pulse did not fire.")

                cleanup = arm_nidaq_pulse_for_capture(self.scope, config, log=log, on_wait_failure=failed)
            else:
                if self.illumination is not None:
                    if config is None or not config.active_illuminator_states:
                        raise ValueError("Select an active fluorescence illumination state before calibration.")
                    self.illumination.apply_observation_illumination(
                        config.active_illuminator_states, turn_on=True, force_hardware=True
                    )
                else:
                    self.scope.obs_controller.turn_on_illumination()
            self._wait_mcu()
            self.camera.send_trigger(self.camera.get_exposure_time())
            while time.monotonic() < deadline:
                self.check_cancelled()
                frame = self.camera.read_camera_frame()
                if frame is not None and frame.frame_id != last_id:
                    image = np.array(frame.frame, copy=True)
                    self.last_capture_metadata = {
                        "frame_id": frame.frame_id,
                        "camera_timestamp": getattr(frame, "timestamp", None),
                        "received_utc": utc_now(),
                    }
                    if np.issubdtype(image.dtype, np.integer):
                        if np.mean(image >= np.iinfo(image.dtype).max) > 0.001:
                            raise ValueError("Calibration image is saturated; reduce exposure or laser intensity.")
                    return image
                self.cancel.wait(0.002)
            raise TimeoutError("No fresh post-move camera frame arrived.")
        finally:
            try:
                if cleanup is not None:
                    cleanup()
            finally:
                self._lights_off()
                self._wait_mcu()


class SampleIncidenceSession:
    """Run flatness first, gate incidence, then repeat with periodic flatness checks."""

    def __init__(
        self,
        hardware,
        settings,
        pixel_size_um,
        progress=lambda *a: None,
        flatness_ready=lambda *a: None,
        incidence_ready=lambda *a: None,
        recorder=None,
    ):
        self.hardware, self.settings, self.pixel_size_um = hardware, settings, pixel_size_um
        self.progress, self.flatness_ready, self.incidence_ready = progress, flatness_ready, incidence_ready
        self.recorder = recorder
        self.sweep = 0

    def _record(self, kind, **data):
        if self.recorder is not None:
            self.recorder.record(kind, sweep=self.sweep, **data)

    def _capture(self, phase, **context):
        started_utc = utc_now()
        image = self.hardware.capture(self.settings.settle_s)
        self.last_capture_monotonic_s = time.monotonic()
        if self.recorder is not None:
            self.recorder.capture(
                image,
                phase=phase,
                sweep=self.sweep,
                started_utc=started_utc,
                completed_monotonic_s=self.last_capture_monotonic_s,
                **context,
                **getattr(self.hardware, "last_capture_metadata", {}),
                **position_snapshot(self.hardware),
            )
        return image

    def run(self):
        s, hw = self.settings, self.hardware
        s.validate()
        if not np.isfinite(self.pixel_size_um) or self.pixel_size_um <= 0:
            raise ValueError("A calibrated objective/camera pixel size is required.")
        if self.recorder is None:
            return self._run()
        objective = getattr(hw.scope, "objective_store", None)
        self.recorder.start(
            dict(
                settings=s,
                pixel_size_um=self.pixel_size_um,
                python_version=sys.version,
                numpy_version=np.__version__,
                stage_config=hw.stage.get_config(),
                camera_config=getattr(hw.camera, "_config", None),
                camera_driver=type(hw.camera).__name__,
                exposure_ms=hw.camera.get_exposure_time(),
                observation_state=hw.scope.obs_controller.current_observation_state,
                objective=getattr(objective, "current_objective", None),
                objective_info=None if objective is None else getattr(objective, "objectives_dict", None),
                laser_af_ready=hw.af_ready,
                laser_af_properties=getattr(hw.af, "laser_af_properties", None),
                starting_position=position_snapshot(hw),
                coordinate_convention="Stage API canonical XYZ; +X right, +Y down in plots; Z follows stage controls.",
            )
        )
        self.progress(f"Troubleshooting data: {self.recorder.path}", None)
        try:
            self._run()
        except BaseException as error:
            try:
                self.recorder.finish(
                    "cancelled" if isinstance(error, CalibrationCancelled) else "failed",
                    error=str(error),
                    traceback=traceback.format_exc(),
                    final_position=position_snapshot(hw),
                )
            except Exception:
                # Preserve acquisition/restoration failures if the disk itself
                # also fails. Already written images/events remain available.
                import squid.logging

                squid.logging.get_logger(__name__).exception("Could not finalize alignment troubleshooting data")
            raise
        else:
            self.recorder.finish("completed", final_position=position_snapshot(hw))

    def _run(self):
        s, hw = self.settings, self.hardware
        with hw:
            self._record("hardware_state", saved_state=hw._saved)
            first = self._capture("initial")
            fov_um = min(first.shape[:2]) * self.pixel_size_um
            offsets = dither_offsets(s.dither_fraction * fov_um)
            hw.validate_path(offsets, max(5, 2 * s.focus_half_range_um) + s.focus_half_range_um + s.z_baseline_um / 2)
            origin = hw.stage.get_pos()
            positions = offsets + np.array([origin.x_mm, origin.y_mm]) * 1000
            self._record("scan_plan", positions_um=positions, offsets_um=offsets, fov_um=fov_um)
            while True:
                self.sweep += 1
                hw.check_cancelled()
                scans, images, nominal_heights = [], [], []
                for index, xy in enumerate(positions):
                    hw.move_xy(*xy)
                    base_z, piezo = hw.refocus(max(5, 2 * s.focus_half_range_um))
                    z = base_z + np.linspace(-s.focus_half_range_um, s.focus_half_range_um, s.focus_steps)
                    scores, best_image, best_score = [], None, -np.inf
                    self._record("focus_scan_start", field=index + 1, z_um=z, piezo_um=piezo, base_z_um=base_z)
                    try:
                        for plane, target in enumerate(z):
                            hw.move_z(target)
                            image = self._capture("flatness", field=index + 1, plane=plane + 1, target_z_um=target)
                            score = texture_scores(image, s.black_level)
                            scores.append(score)
                            small = preview(image)
                            if np.mean(score) > best_score:
                                best_image, best_score = small, np.mean(score)
                            self.progress(f"Flatness: field {index+1}/9, Z plane {plane+1}/{s.focus_steps}", small)
                    finally:
                        # Restore the local nominal focus before the next XY move.
                        hw.stage.move_z_to(base_z / 1000)
                    peak = focus_peaks(z + piezo, scores)
                    self._record("focus_scan", field=index + 1, z_um=z + piezo, scores=np.asarray(scores), peaks=peak)
                    if not np.any(peak.valid):
                        raise ValueError(
                            f"Field {index+1}: no reliable texture focus peak, including the center; adjust Z range/signal."
                        )
                    scans.append(peak)
                    images.append(best_image)
                    nominal_heights.append(base_z + piezo)
                self._record("flatness_registration_input", images=images, positions_um=positions)
                shifts, transform, error = register_fields(images, positions)
                self._record("flatness_registration", shifts=shifts, transform=transform, error_px=error)
                flatness = assess_flatness(
                    positions - positions[0], scans, transform, images[0].shape, s.max_plane_span_um, s.max_residual_um
                )
                # Keep incidence centered on the specimen plane rather than
                # shifting nominal Z toward the objective's curved edge focus.
                focus_offsets = (positions - positions[0]) @ flatness.plane[:2] + flatness.plane[2] - nominal_heights
                try:
                    specimen_check = separate_illumination(images, shifts, s.black_level)
                except ValueError as error:
                    self._record("specimen_illumination_unavailable", error=str(error))
                    flatness.coating_message = (
                        "Specimen brightness separation is inconclusive at these focus conditions."
                    )
                else:
                    self._record("specimen_illumination", result=specimen_check)
                    flatness.sample_variation = specimen_check.sample_variation
                    flatness.coating_message = (
                        "Specimen fluorescence variation after removing camera-fixed illumination; "
                        "brightness variation alone is not evidence of height or holder tilt."
                    )
                self._record("flatness", result=flatness, focus_offsets_um=focus_offsets)
                self.flatness_ready(flatness)
                if not flatness.accepted:
                    self.progress(
                        "Flatness did not pass. Adjust holder/coating and repeat; incidence is gated.", images[0]
                    )
                    if not s.repeat:
                        return
                    hw.move_xy(*positions[0])
                    if hw.cancel.wait(1):
                        raise CalibrationCancelled()
                    continue
                # Revalidate flatness after three incidence sweeps, so subsequent
                # mechanical changes cannot silently invalidate the flatness gate.
                for cycle in range(3 if s.repeat else 1):
                    planes = [[], [], []]
                    times = [[], [], []]
                    for index, xy in enumerate(positions):
                        hw.move_xy(*xy)
                        base_z, _ = hw.refocus(max(5, 2 * s.focus_half_range_um))
                        base_z += focus_offsets[index]
                        try:
                            for plane, delta in enumerate((-s.z_baseline_um / 2, 0, s.z_baseline_um / 2)):
                                hw.move_z(base_z + delta)
                                small = preview(
                                    self._capture(
                                        "incidence",
                                        cycle=cycle + 1,
                                        field=index + 1,
                                        plane=plane + 1,
                                        delta_z_um=delta,
                                        target_z_um=base_z + delta,
                                    )
                                )
                                planes[plane].append(small)
                                times[plane].append(self.last_capture_monotonic_s)
                                self.progress(f"Incidence sweep {cycle+1}: field {index+1}/9, Z {delta:+.2f} µm", small)
                        finally:
                            hw.stage.move_z_to((base_z - focus_offsets[index]) / 1000)
                    # Repeat the center reference at each Z. Free per-frame gains
                    # would make the illumination gradient unidentifiable; instead
                    # measure and bound actual drift, correcting only a small,
                    # smooth change between matching reference frames.
                    hw.move_xy(*positions[0])
                    base_z, _ = hw.refocus(max(5, 2 * s.focus_half_range_um))
                    base_z += focus_offsets[0]
                    drift = 0.0
                    try:
                        for plane, delta in enumerate((-s.z_baseline_um / 2, 0, s.z_baseline_um / 2)):
                            hw.move_z(base_z + delta)
                            reference = preview(
                                self._capture(
                                    "incidence_reference",
                                    cycle=cycle + 1,
                                    field=1,
                                    plane=plane + 1,
                                    delta_z_um=delta,
                                    target_z_um=base_z + delta,
                                )
                            )
                            first_signal = np.median(planes[plane][0]) - s.black_level
                            last_signal = np.median(reference) - s.black_level
                            if min(first_signal, last_signal) <= 1:
                                raise ValueError("Insufficient signal in repeated center reference.")
                            ratio = last_signal / first_signal
                            reference_time = self.last_capture_monotonic_s
                            duration = max(reference_time - times[plane][0], 1e-6)
                            self._record(
                                "reference_drift",
                                cycle=cycle + 1,
                                plane=plane + 1,
                                first_signal=first_signal,
                                last_signal=last_signal,
                                ratio=ratio,
                                capture_times_monotonic_s=np.asarray(times[plane]),
                                reference_time_monotonic_s=reference_time,
                                duration_s=duration,
                            )
                            drift = max(drift, abs(ratio - 1))
                            if abs(ratio - 1) > 0.03:
                                raise ValueError(
                                    "Center reference drift exceeded 3%; bleaching/power/focus changed. Angle withheld."
                                )
                            for field_id, capture_time in enumerate(times[plane]):
                                gain = np.exp(np.log(ratio) * (capture_time - times[plane][0]) / duration)
                                planes[plane][field_id] = (
                                    planes[plane][field_id] - s.black_level
                                ) / gain + s.black_level
                    finally:
                        hw.stage.move_z_to((base_z - focus_offsets[0]) / 1000)
                    # Re-register each Z plane: defocused texture and Z cross-talk
                    # must not be mistaken for the known lateral dither.
                    fits, transforms = [], []
                    for plane, fields in enumerate(planes):
                        self._record("incidence_registration_input", cycle=cycle + 1, plane=plane + 1, images=fields)
                        plane_shifts, plane_transform, _ = register_fields(fields, positions)
                        self._record(
                            "incidence_registration",
                            cycle=cycle + 1,
                            plane=plane + 1,
                            shifts=plane_shifts,
                            transform=plane_transform,
                        )
                        transforms.append(plane_transform)
                        fits.append(separate_illumination(fields, plane_shifts, s.black_level))
                        self._record("incidence_illumination", cycle=cycle + 1, plane=plane + 1, result=fits[-1])
                    if any(np.linalg.norm(t - transform) / np.linalg.norm(transform) > 0.05 for t in transforms):
                        raise ValueError("Image scale/orientation changed across Z; incidence angle is inconclusive.")
                    result = incidence_from_planes(*fits, s.z_baseline_um, transform)
                    result.reference_drift = drift
                    self._record("incidence", cycle=cycle + 1, result=result)
                    self.incidence_ready(result)
                    hw.move_xy(*positions[0])
                if not s.repeat:
                    return
