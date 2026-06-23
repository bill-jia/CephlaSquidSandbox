"""
Main controller for fast acquisition mode.

This module coordinates all components of fast acquisition:
- Camera frame acquisition
- Frame buffering
- Frame writing to disk
- NI DAQ waveform-based triggering
- DAQ waveform recording and synchronization
"""

import os
import threading
import time
from enum import Enum
from typing import Any, Optional, Dict, Callable, Union
import numpy as np
from scipy import ndimage
import squid.logging
import matplotlib.pyplot as plt

from squid.abc import AbstractCamera, CameraAcquisitionMode
from squid.config import CameraVariant
from control.core.fast_acquisition_buffer import FastAcquisitionFrameBuffer
from control.core.fast_acquisition_writer import FastAcquisitionWriter
from control.nidaq import AbstractNIDAQ, WaveformData, TriggerSource
from control.nidaq import generate_pulse_train
from control.nidaq import (
    NIDAQConfigSnapshot,
    write_waveform_datasets_h5,
    write_nidaq_snapshot_h5,
)


# Upper bound on RAM for the in-memory frame ring during fast acquisition. The ring
# is sized to hold the whole capture (so a slow disk writer never has to keep up in
# real time), bounded here so very long captures don't exhaust host memory.
FAST_ACQ_RING_BUFFER_MAX_BYTES = 4 * 1024**3  # 4 GiB


class AcquisitionCompletionStatus(Enum):
    """Status of acquisition completion."""
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED_SUCCESS = "completed_success"
    COMPLETED_ERROR = "completed_error"
    STOPPED_MANUAL = "stopped_manual"


class FastAcquisitionController:
    """
    Main controller for fast acquisition mode.
    
    Coordinates:
    - Camera acquisition in fast mode
    - Frame buffering and writing
    - NI DAQ waveform-based triggering (preloaded waveforms)
    - DAQ waveform recording and synchronization
    """
    
    def __init__(self, camera: Optional[AbstractCamera],
                 ni_daq: Optional[AbstractNIDAQ],
                 output_path: str,
                 buffer_size: int = 500,
                 file_format: str = "tiff",
                 camera_trigger_dio_line: int = 1,
                 frame_counter_dio_line: int = 0,
                 illumination_controller=None,
                 microscope: Optional[Any] = None,
                 live_controller: Optional[Any] = None):
        """
        Initialize fast acquisition controller.

        When camera is None (DAQ-only mode), no frame buffer or writer is used.
        With a camera, the ring buffer and writer are created when each acquisition
        starts and released when it finishes.

        Args:
            camera: Camera instance, or None for DAQ-only (waveform output/recording only)
            ni_daq: NI DAQ instance (for triggering and waveform recording)
            output_path: Base directory for saving data
            buffer_size: Ring buffer capacity in frames (used when a camera acquisition starts)
            file_format: File format for saving ("tiff", "zarr", "hdf5", or "raw") (ignored when camera is None)
            camera_trigger_dio_line: Digital output line for camera triggers (default: 1); unused in DAQ-only
            frame_counter_dio_line: Digital input line for camera frame signal (default: 0); unused in DAQ-only
            illumination_controller: Optional IlluminationController; when provided its
                state is snapshotted before acquisition and restored afterwards so that
                the higher-level channel state stays in sync with the hardware lines
                that the NI-DAQ restores via restore_after_acquisition().
            microscope: Optional Microscope; used to write acquisition_metadata.yaml on stop.
            live_controller: Optional LiveController; used with microscope for metadata YAML.
        """
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self._camera = camera
        self._ni_daq = ni_daq
        self._output_path = output_path
        self._camera_trigger_dio_line = camera_trigger_dio_line
        self._frame_counter_dio_line = frame_counter_dio_line
        self._daq_only = camera is None
        # External frame grabbing: the camera is present (so we still output the camera-trigger
        # pulse train and record the frame-readout line) but a separate application owns the
        # physical sensor, so we do not grab/buffer/save frames here. Driven by the simulated
        # camera's external_frame_grabbing flag (CameraConfig.external_frame_grabbing).
        self._external_frame_grabbing = (
            camera is not None and bool(getattr(camera, "external_frame_grabbing", False))
        )
        self._illumination_controller = illumination_controller
        self._illumination_snapshot = None
        self._microscope = microscope
        self._live_controller = live_controller

        self._buffer_size = buffer_size
        self._file_format = file_format
        self._frame_shape = None
        self._dtype = None
        self._frame_buffer = None
        self._frame_writer = None
        self._num_frames = None

        # State
        self._is_acquiring = False
        self._frame_count = 0
        self._start_time = None
        self._stop_event = threading.Event()
        self._expected_duration_s: Optional[float] = None
        self._timeout_s: Optional[float] = None
        self._stop_called = False  # Flag to prevent duplicate stop_acquisition calls
        self._writer_shutdown_lock = threading.Lock()
        self._writer_shutdown_thread: Optional[threading.Thread] = None

        # Completion tracking
        self._completion_status = AcquisitionCompletionStatus.NOT_STARTED
        self._completion_error_message: Optional[str] = None
        self._completion_callback: Optional[Callable[[AcquisitionCompletionStatus, Optional[str]], None]] = None

        # Statistics
        self._stats_lock = threading.Lock()
        self._last_frame_time = None
        self._frame_times = []

        # Frame synchronization data
        self._frame_sample_indices: list = []
        self._daq_result = None

        if self._daq_only:
            self._log.info(
                f"Initialized fast acquisition controller (DAQ-only): output={output_path}"
            )
        elif self._external_frame_grabbing:
            self._log.info(
                f"Initialized fast acquisition controller (external frame grabbing): "
                f"output={output_path}, trigger_line={camera_trigger_dio_line}, "
                f"frame_signal_line={frame_counter_dio_line}. The NI-DAQ trigger pulse train and "
                f"frame-readout recording run, but frames are grabbed by a separate application."
            )
        else:
            self._log.info(
                f"Initialized fast acquisition controller: "
                f"buffer_size={buffer_size}, format={file_format}, "
                f"output={output_path}, trigger_line={camera_trigger_dio_line}, "
                f"frame_signal_line={frame_counter_dio_line}"
            )

    def _create_camera_acquisition_resources(self) -> None:
        """Allocate the ring buffer and writer for the current camera ROI and format."""
        if self._daq_only or self._camera is None:
            return
        roi = self._camera.get_region_of_interest()
        frame_shape = (roi[3], roi[2])
        pixel_format = self._camera.get_pixel_format()
        dtype_map = {
            "MONO8": np.uint8,
            "MONO10": np.uint8,
            "MONO12": np.uint16,
            "MONO14": np.uint16,
            "MONO16": np.uint16,
        }
        dtype = dtype_map.get(pixel_format.name, np.uint16)
        max_frame_bytes = int(self._camera.get_fast_acquisition_max_frame_bytes())
        self._frame_shape = frame_shape
        self._dtype = dtype

        # Size the ring to hold the entire capture (bounded by a RAM budget) so the
        # disk writer, which is slower than the camera, never has to keep up in real
        # time — it drains the ring during and after the burst. Honor the user's
        # buffer_size as a floor.
        ring_size = self._buffer_size
        if self._num_frames:
            ring_cap = max(1, FAST_ACQ_RING_BUFFER_MAX_BYTES // max(1, max_frame_bytes))
            ring_size = max(self._buffer_size, min(int(self._num_frames), ring_cap))
            if ring_size != self._buffer_size:
                self._log.info(
                    f"Ring buffer sized to {ring_size} frames to cover the {self._num_frames}-frame "
                    f"capture (requested buffer_size={self._buffer_size}, RAM cap={ring_cap})"
                )

        self._frame_buffer = FastAcquisitionFrameBuffer(
            buffer_size=ring_size,
            max_frame_bytes=max_frame_bytes,
            frame_shape=frame_shape,
            dtype=dtype,
            # Drop the newest frame with a warning if the ring ever fills, rather than
            # silently overwriting already-captured frames.
            overwrite_when_full=False,
        )
        self._frame_writer = FastAcquisitionWriter(
            frame_buffer=self._frame_buffer,
            output_path=self._output_path,
            file_format=self._file_format,
            byte_decoding_fn=self._camera._byte_decoding_fn,
            frame_shape=frame_shape,
            dtype=dtype,
        )

    def _cleanup_camera_acquisition_resources(self) -> None:
        """Drop references to the ring buffer and writer after an acquisition ends."""
        if self._daq_only:
            return
        self._frame_buffer = None
        self._frame_writer = None

    def _is_previous_writer_busy(self) -> bool:
        """True if a prior FastAcquisitionWriter thread is still running (capture or conversion)."""
        if self._daq_only:
            return False
        w = self._frame_writer
        if w is None:
            return False
        return w.is_alive() or bool(getattr(w, "is_converting_frames", False))

    def _schedule_writer_shutdown_and_cleanup(self) -> None:
        """
        Join the frame writer in a daemon thread, then log stats and release buffer/writer refs.
        Used so stop_acquisition can return before TIFF/Zarr/HDF5 conversion finishes.
        """
        writer = self._frame_writer
        if writer is None:
            return
        with self._writer_shutdown_lock:
            if self._writer_shutdown_thread is not None and self._writer_shutdown_thread.is_alive():
                return

            def _join_log_cleanup() -> None:
                try:
                    writer.join()
                finally:
                    try:
                        if self._frame_writer is writer:
                            writer_stats = writer.get_write_statistics()
                            frames_written = int(writer_stats.get("frames_written", 0))
                            expected_frames = int(self._frame_count)
                            dropped_frames = max(expected_frames - frames_written, 0)
                            self._log.info(
                                f"Fast acquisition frame summary (after writer finished): "
                                f"expected={expected_frames}, written={frames_written}, "
                                f"dropped={dropped_frames}"
                            )
                            # metadata.json was written during stop, before this drain
                            # finished, so correct its (otherwise stale) frame counters.
                            self._patch_metadata_frame_counts(
                                expected_frames, frames_written, dropped_frames
                            )
                    except Exception as e:
                        self._log.warning(
                            f"Failed to compute dropped frame statistics after writer join: {e}",
                            exc_info=True,
                        )
                    try:
                        if self._frame_writer is writer:
                            self._cleanup_camera_acquisition_resources()
                    except Exception as e:
                        self._log.warning(
                            f"Failed to release frame buffer/writer after join: {e}", exc_info=True
                        )
                    with self._writer_shutdown_lock:
                        self._writer_shutdown_thread = None

            self._writer_shutdown_thread = threading.Thread(
                target=_join_log_cleanup,
                daemon=True,
                name="FastAcqWriterJoin",
            )
            self._writer_shutdown_thread.start()

    def _cleanup_after_failed_camera_start(self) -> None:
        """Stop writer and release buffer/writer if starting the camera acquisition failed."""
        if self._daq_only:
            return
        if self._frame_writer is not None:
            try:
                self._frame_writer.stop(wait=True)
            except Exception as e:
                self._log.warning(f"Failed to stop frame writer after failed start: {e}", exc_info=True)
        if self._camera is not None and hasattr(self._camera, "stop_fast_acquisition_frame_grabbing"):
            try:
                self._camera.stop_fast_acquisition_frame_grabbing()
            except Exception as e:
                self._log.warning(f"Failed to stop camera grab after failed start: {e}", exc_info=True)
        self._cleanup_camera_acquisition_resources()
        self._frame_shape = None
        self._dtype = None

    def start_acquisition(self, num_frames: Optional[int] = None,
                         frame_rate_hz: float = 10.0,
                         exposure_time_ms: float = 20.0,
                         sample_rate_hz: float = 10000.0,
                         ai_channels: Optional[list] = None,
                         ao_channels: Optional[list] = None,
                         di_lines: Optional[list] = None,
                         acquisition_mode: Optional[CameraAcquisitionMode] = None,
                         waveforms: Optional[WaveformData] = None,
                         camera_trigger_dio_line: Optional[int] = None,
                         frame_counter_dio_line: Optional[int] = None,
                         duration_s: Optional[float] = None,
                         camera_offset_ms: float = 0):
        """
        Start fast acquisition with preloaded NI DAQ waveforms.

        In DAQ-only mode (camera is None), use duration_s to set acquisition length;
        no camera or frame recording is performed.

        Args:
            num_frames: Number of frames to acquire (None for continuous); ignored in DAQ-only mode
            frame_rate_hz: Target frame rate; ignored in DAQ-only mode
            exposure_time_ms: Exposure time per frame; ignored in DAQ-only mode
            sample_rate_hz: NI DAQ sample rate for waveforms
            ai_channels: Optional analog input channels to record
            ao_channels: Optional analog output channels
            di_lines: Optional digital input lines to record (in DAQ-only, only these are recorded)
            acquisition_mode: Camera acquisition mode; ignored in DAQ-only mode
            waveforms: Optional WaveformData from NIDAQWidget. In DAQ-only mode used as-is.
            camera_trigger_dio_line: Optional trigger line number (overrides default); ignored in DAQ-only
            frame_counter_dio_line: Optional camera frame counter line; ignored in DAQ-only
            duration_s: Duration in seconds. Required when camera is None (DAQ-only mode).
        """
        if self._is_acquiring:
            self._log.warning("Acquisition already running")
            return
        if self._is_previous_writer_busy():
            self._log.warning(
                "Cannot start acquisition: the previous run is still converting frames "
                "to the output format (TIFF/Zarr/HDF5); wait for it to finish"
            )
            return

        if self._ni_daq is None:
            raise ValueError("NI DAQ is required for fast acquisition")

        if self._daq_only:
            if duration_s is None or duration_s <= 0:
                raise ValueError("duration_s must be positive when using DAQ-only mode")
            self._log.info(f"Starting DAQ-only fast acquisition: duration={duration_s:.2f}s, rate={sample_rate_hz} Hz")
        else:
            self._log.info(
                f"Starting fast acquisition: frames={num_frames}, "
                f"rate={frame_rate_hz} Hz, exposure={exposure_time_ms} ms"
            )

        # Calculate duration and samples
        if self._daq_only:
            duration_s = float(duration_s)
            num_frames_estimate = None
        elif num_frames is None:
            duration_s = 1
            num_frames_estimate = int(frame_rate_hz * duration_s)
        elif duration_s is None:
            duration_s = num_frames / frame_rate_hz
            num_frames_estimate = num_frames

        # Remember the requested frame count so the ring buffer can be sized to the
        # whole capture and the monitor can detect completion.
        self._num_frames = None if self._daq_only else num_frames

        # Store expected duration and timeout. For camera mode, allow extra time for
        # the consumer/disk to drain the ring after the trigger burst (assume a
        # conservative >=200 Hz sustained drain) so large captures aren't killed by a
        # fixed wall-clock. Completion is normally reached via the frame count or the
        # stall detector in _monitor_acquisition; this is only the hard backstop.
        self._expected_duration_s = duration_s
        if not self._daq_only and num_frames:
            drain_estimate_s = num_frames / 200.0
            self._timeout_s = duration_s + max(15.0, drain_estimate_s + 5.0)
        else:
            self._timeout_s = duration_s + 10
        self._log.info(f"Expected acquisition duration: {duration_s:.2f}s, timeout: {self._timeout_s:.2f}s")

        samples_per_channel = int(sample_rate_hz * duration_s)
        n_samples_offset = int(np.ceil(camera_offset_ms * sample_rate_hz / 1000)) + 1
        
        # Load waveforms and add camera triggers if needed
        if self._daq_only:
            if waveforms is None:
                local_waveforms = WaveformData()
            else:
                ao_copy = {
                    ch: np.array(data, copy=True)
                    for ch, data in (waveforms.analog_output or {}).items()
                }
                do_copy = {
                    line: np.array(data, copy=True)
                    for line, data in (waveforms.digital_output or {}).items()
                }
                local_waveforms = WaveformData(analog_output=ao_copy, digital_output=do_copy)
                di_lines_to_record = list(di_lines) if di_lines else []
        else:
            if camera_trigger_dio_line is not None:
                self._camera_trigger_dio_line = camera_trigger_dio_line
            if frame_counter_dio_line is not None:
                self._frame_counter_dio_line = frame_counter_dio_line

            pulse_width_samples = 4
            trigger_duration_us = int(pulse_width_samples/sample_rate_hz*1e6)
            self._camera.set_trigger_duration_us(trigger_duration_us)
            frame_period_samples = int(sample_rate_hz / frame_rate_hz)

            trigger_pattern = generate_pulse_train(
                pulse_width_samples=pulse_width_samples,
                period_samples=frame_period_samples,
                num_samples=samples_per_channel,
                n_samples_offset=n_samples_offset,
                inverted=False,
                max_num_pulses=num_frames,
            )
            if waveforms is None:
                local_waveforms = WaveformData(digital_output={self._camera_trigger_dio_line: trigger_pattern})
            else:
                ao_copy = {
                    ch: np.array(data, copy=True)
                    for ch, data in (waveforms.analog_output or {}).items()
                }
                do_copy = {
                    line: np.array(data, copy=True)
                    for line, data in (waveforms.digital_output or {}).items()
                }
                local_waveforms = WaveformData(analog_output=ao_copy, digital_output=do_copy)
                local_waveforms.digital_output[self._camera_trigger_dio_line] = trigger_pattern
            
            di_lines_to_record = [self._frame_counter_dio_line]
            if di_lines:
                di_lines_to_record.extend(di_lines)
            di_lines_to_record = list(set(di_lines_to_record))

        do_lines_from_waveforms = list(local_waveforms.digital_output.keys())

        # Configure which endpoints participate in this acquisition on the NI DAQ.
        # This narrows the task IO to the selected subsets, without overwriting the
        # full available-channel collections on the device.
        try:
            # Necessary?
            self._ni_daq.configure_task_io(
                ao_channels=ao_channels or [],
                do_lines=do_lines_from_waveforms,
                di_lines=di_lines_to_record,
                ai_channels=ai_channels or [],
            )
        except AttributeError:
            # Backwards compatibility: older NI DAQ implementations may not provide
            # configure_task_io; in that case we fall back to using the config dict.
            pass


        # Snapshot illumination state before handing off to the DAQ task, so the
        # higher-level channel state can be restored after the acquisition completes.
        if self._illumination_controller is not None:
            try:
                self._illumination_snapshot = self._illumination_controller.snapshot()
            except Exception as e:
                self._log.warning(f"Failed to snapshot illumination state: {e}")
                self._illumination_snapshot = None

        # Prepare NI DAQ to hand off from any active live outputs to this task.
        self._ni_daq.prepare_for_acquisition()
        # self._ni_daq.configure(**config)
        self._ni_daq.set_waveforms(local_waveforms)
        self._ni_daq.arm()

        if not self._daq_only and not self._external_frame_grabbing:
            if self._camera.get_is_streaming():
                self._log.info("Stopping existing camera streaming for fast acquisition")
                self._camera.stop_streaming()
            if acquisition_mode is None:
                acquisition_mode = CameraAcquisitionMode.HARDWARE_TRIGGER
            if acquisition_mode not in [CameraAcquisitionMode.HARDWARE_TRIGGER, CameraAcquisitionMode.HARDWARE_TRIGGER_FIRST]:
                raise ValueError(f"Invalid acquisition mode for fast acquisition: {acquisition_mode}")
            try:
                self._camera.set_acquisition_mode(acquisition_mode)
                self._log.info(f"Camera set to {acquisition_mode.value} mode")
            except (NotImplementedError, ValueError) as e:
                self._log.error(f"Camera does not support {acquisition_mode.value} mode: {e}")
                raise
            if self._camera._config.camera_type == CameraVariant.TOUPCAM:
                self._camera.start_streaming()

            self._camera.set_exposure_time(exposure_time_ms)
            self._camera.fast_acquisition_timeout_ms = int(np.ceil(1 / frame_rate_hz * 1000 * 1.1))
            if hasattr(self._camera, '_optimize_for_fast_acquisition'):
                try:
                    self._camera._optimize_for_fast_acquisition()
                except Exception as e:
                    self._log.warning(f"Could not optimize camera for fast acquisition: {e}")
            self._create_camera_acquisition_resources()
            try:
                self._frame_writer.start()
            except Exception:
                self._cleanup_after_failed_camera_start()
                raise

        self._frame_count = 0
        self._start_time = time.time()
        self._stop_event.clear()
        self._stop_called = False
        self._completion_status = AcquisitionCompletionStatus.IN_PROGRESS
        self._completion_error_message = None
        expected_decode_bytes = None

        if not self._daq_only and not self._external_frame_grabbing:
            expected_decode_bytes = int(self._camera.get_fast_acquisition_max_frame_bytes())
            def frame_callback(
                frame: Union[bytes, np.ndarray], metadata: Optional[dict] = None
            ):
                md = dict(metadata) if metadata else {}
                if isinstance(frame, np.ndarray):
                    frame_bytes = np.ascontiguousarray(frame).tobytes()
                    if "height" not in md:
                        md["height"] = int(frame.shape[0])
                    if "width" not in md:
                        md["width"] = int(frame.shape[1])
                else:
                    frame_bytes = frame

                if "expected_decode_bytes" not in md:
                    md["expected_decode_bytes"] = expected_decode_bytes
                placeholder_frame_id = self._frame_count
                if not md:
                    timestamp = time.time()
                elif "frame_header" in md:
                    timestamp = float(md["frame_header"]["timestampEofPs"]) / 1e9
                elif "timestamp" in md:
                    timestamp = float(md["timestamp"])
                else:
                    timestamp = time.time()

                success = self._frame_buffer.write_frame(
                    frame_bytes, placeholder_frame_id, timestamp, md
                )
                if success:
                    self._frame_count += 1
                    with self._stats_lock:
                        self._last_frame_time = time.time()
                else:
                    self._log.warning(
                        f"Failed to write frame {placeholder_frame_id} to buffer"
                    )

            def frame_sink(src_ptr, n_bytes, metadata):
                # Preferred single-copy path (cameras that support it, e.g. Tucsen).
                # Runs on the camera SDK callback thread once per frame; the ring buffer
                # copies the frame exactly once, straight from the SDK DMA buffer into
                # its slab — no intermediate Python bytes object on this hot path.
                md = dict(metadata) if metadata else {}
                md.setdefault("expected_decode_bytes", expected_decode_bytes)
                # The SDK header carries height/width; fall back to wall-clock arrival
                # time when the firmware leaves the frame timestamp at 0.
                ts = md.get("timestamp")
                if not ts:
                    ts = time.time()
                placeholder_frame_id = self._frame_count
                success = self._frame_buffer.write_frame_from_ptr(
                    src_ptr, n_bytes, placeholder_frame_id, ts, md
                )
                if success:
                    self._frame_count += 1
                    with self._stats_lock:
                        self._last_frame_time = time.time()
                else:
                    self._log.warning(
                        f"Failed to write frame {placeholder_frame_id} to buffer (ring full)"
                    )

            try:
                if hasattr(self._camera, 'start_fast_acquisition_frame_grabbing'):
                    self._camera.start_fast_acquisition_frame_grabbing(
                        frame_rate_hz,
                        n_frames_expected=num_frames,
                        frame_callback=frame_callback,
                        acquisition_mode=acquisition_mode,
                        frame_sink=frame_sink,
                    )
                else:
                    raise NotImplementedError(
                        "Camera does not support fast acquisition frame grabbing. "
                        "This requires a camera implementation with start_fast_acquisition_frame_grabbing() method."
                    )
            except Exception:
                self._cleanup_after_failed_camera_start()
                raise

        self._is_acquiring = True

        self._monitor_thread = threading.Thread(
            target=self._monitor_acquisition,
            args=(
                num_frames if (not self._daq_only and not self._external_frame_grabbing) else None,
                frame_rate_hz,
            ),
            daemon=True
        )
        self._monitor_thread.start()

        self._ni_daq.start_trigger()
        self._log.info(f"NIDAQ is running: {self._ni_daq.is_running}")
        self._log.info("Fast acquisition started with NI DAQ waveforms" + (" (DAQ-only)" if self._daq_only else ""))
    
    def stop_acquisition(self, manual_stop: bool = False, error_message: Optional[str] = None):
        """
        Stop fast acquisition.

        Returns after DAQ/camera teardown and metadata save; TIFF/Zarr/HDF5 conversion of
        raw frames (when applicable) continues on a background thread so the UI is not blocked.

        Args:
            manual_stop: If True, indicates this is a manual stop by user.
                        If False, indicates automatic completion (e.g., frame limit reached).
            error_message: Optional error message if stopping due to an error.
        """
        if not self._is_acquiring:
            self._log.warning("Acquisition not running")
            return
        
        # Prevent duplicate calls
        if self._stop_called:
            self._log.debug("stop_acquisition already called, ignoring duplicate call")
            return
        
        self._stop_called = True
        
        self._log.info(f"Stopping fast acquisition (manual={manual_stop}, error={error_message is not None})...")
        
        # Signal stop (writer thread may continue converting in the background)
        self._stop_event.set()

        completion_status = None
        completion_error = error_message
        writer_stopped = False

        try:
            # Stop NI DAQ
            if self._ni_daq:
                # Wait for completion and get data
                # Use expected duration + buffer for timeout (same as acquisition timeout)
                timeout_s = self._timeout_s if self._timeout_s is not None else 10.0
                daq_success = self._ni_daq.wait_until_done(timeout_s=timeout_s)
                if not daq_success and error_message is None:
                    completion_error = f"DAQ did not complete within timeout ({timeout_s:.2f}s)"
                
                self._daq_result = self._ni_daq.get_acquired_data()

                # Release acquisition tasks so DO/AO lines are free for live output.
                self._ni_daq.release_tasks()

                # Restore any live-output state that was active before this acquisition.
                restore_fn = getattr(self._ni_daq, "restore_after_acquisition", None)
                if callable(restore_fn):
                    try:
                        restore_fn()
                    except Exception as e:
                        self._log.warning(f"Failed to restore NI DAQ live-output state: {e}", exc_info=True)

                # Restore the higher-level illumination controller state so it stays
                # in sync with the hardware lines restored above by the NI-DAQ.
                if self._illumination_controller is not None and self._illumination_snapshot is not None:
                    try:
                        self._illumination_controller.restore(self._illumination_snapshot, force_hardware=True)
                    except Exception as e:
                        self._log.warning(f"Failed to restore illumination controller state: {e}", exc_info=True)
                    finally:
                        self._illumination_snapshot = None

                # Detect frame edges from camera frame signal (camera mode only)
                if not self._daq_only and self._daq_result and len(self._daq_result.digital_input) > 0:
                    camera_signal = self._daq_result.digital_input.get(self._frame_counter_dio_line)
                    if camera_signal is not None and max(camera_signal) > 0:
                        self._frame_sample_indices = self._detect_frame_edges(camera_signal)
                        self._frame_count = len(self._frame_sample_indices)
                        self._log.info(f"Detected {len(self._frame_sample_indices)} frames from camera signal")
                    else:
                        self._log.warning(
                            "No trigger detected on camera signal line; "
                            f"falling back to callback frame count: {self._frame_count}"
                        )
            # Save DAQ data and metadata (both camera and DAQ-only)
            self._save_daq_data()
            self._save_metadata()
            
            if not self._daq_only and not self._external_frame_grabbing:
                if hasattr(self._camera, 'stop_fast_acquisition_frame_grabbing'):
                    self._camera.stop_fast_acquisition_frame_grabbing()
                if self._frame_writer is not None:
                    self._frame_writer.stop(wait=False)
                    self._log.info(
                        "Frame writer signaled to stop; TIFF/Zarr/HDF5 conversion (if any) "
                        "continues in the background"
                    )
                    self._schedule_writer_shutdown_and_cleanup()
                    writer_stopped = True
            # TIFF / Zarr / HDF5 post-decode runs inside FastAcquisitionWriter after raw closes.


            
            # Determine completion status
            if completion_error:
                completion_status = AcquisitionCompletionStatus.COMPLETED_ERROR
            elif manual_stop:
                completion_status = AcquisitionCompletionStatus.STOPPED_MANUAL
            else:
                completion_status = AcquisitionCompletionStatus.COMPLETED_SUCCESS
            
            self._log.info(f"Fast acquisition stopped: {completion_status.value}")
            
        except Exception as e:
            self._log.error(f"Error during acquisition stop: {e}", exc_info=True)
            completion_status = AcquisitionCompletionStatus.COMPLETED_ERROR
            if not completion_error:
                completion_error = str(e)
        finally:
            if not self._daq_only and not self._external_frame_grabbing:
                if not writer_stopped and self._frame_writer is not None:
                    try:
                        self._frame_writer.stop(wait=False)
                        self._schedule_writer_shutdown_and_cleanup()
                    except Exception as e:
                        self._log.warning(
                            f"Failed to stop frame writer during cleanup: {e}", exc_info=True
                        )
            self._is_acquiring = False

        # Notify completion
        self._notify_completion(completion_status, completion_error)
    
    def _detect_frame_edges(self, digital_signal: np.ndarray, edge_type: str = "rising") -> list:
        """
        Detect frame edges in digital input signal.
        
        Args:
            digital_signal: 1D boolean array of digital input samples
            edge_type: "rising", "falling", or "both"
            
        Returns:
            List of sample indices where frame edges detected
        """
        if len(digital_signal) < 2:
            return []
        
        signal_int = digital_signal.astype(bool)

        # Clean up single samples that might have dropped due to hardware behavior
        signal_int = ndimage.binary_closing(signal_int, structure=np.ones((3,), dtype=bool)).astype(int)
        
        if edge_type == "rising":
            edges = np.where(np.diff(signal_int) > 0)[0]
        elif edge_type == "falling":
            edges = np.where(np.diff(signal_int) < 0)[0]
        else:  # "both"
            edges = np.where(np.abs(np.diff(signal_int)) > 0)[0]
        
        return edges.tolist()
    
    def _monitor_acquisition(self, num_frames: Optional[int], frame_rate_hz: float = 0.0):
        """Monitor acquisition; complete on frame limit, stall, duration, or a hard timeout.

        Camera mode normally completes when all expected frames have been delivered to
        the ring. If delivery stalls after the trigger burst is over (e.g. the camera
        produced fewer frames than requested), it finishes cleanly rather than waiting
        out the full wall-clock timeout. The hard timeout remains as a backstop for a
        genuinely stuck camera/DAQ and is reported as an error.
        """
        try:
            last_progress_count = -1
            last_progress_time = self._start_time if self._start_time else time.time()
            stall_s = max(3.0, 5.0 / frame_rate_hz) if frame_rate_hz and frame_rate_hz > 0 else 3.0
            while not self._stop_event.is_set() and self._is_acquiring:
                now = time.time()
                elapsed_time = now - self._start_time if self._start_time else 0

                # Hard backstop: a genuinely stuck camera/DAQ. Reported as an error.
                if self._timeout_s is not None and self._start_time is not None:
                    if elapsed_time >= self._timeout_s:
                        timeout_message = (
                            f"Acquisition timeout reached: {elapsed_time:.2f}s >= {self._timeout_s:.2f}s "
                            f"(expected duration: {self._expected_duration_s:.2f}s). "
                            f"Frames delivered: {self._frame_count}"
                        )
                        self._log.error(timeout_message)
                        self._stop_event.set()
                        self.stop_acquisition(manual_stop=False, error_message=timeout_message)
                        break

                # DAQ-only / external frame grabbing: stop when expected duration has elapsed
                # (we don't grab frames here, so there is no callback frame count to wait on).
                if (self._daq_only or self._external_frame_grabbing) and self._expected_duration_s is not None:
                    if elapsed_time >= self._expected_duration_s:
                        self._log.info("Expected duration reached, stopping acquisition")
                        self._stop_event.set()
                        break
                elif num_frames is not None:
                    fc = self._frame_count
                    if fc > last_progress_count:
                        last_progress_count = fc
                        last_progress_time = now
                    # Normal completion: all expected frames delivered to the ring.
                    if fc >= num_frames:
                        self._log.info(f"Reached frame limit ({num_frames}), stopping acquisition")
                        self._stop_event.set()
                        break
                    # Stall completion: delivery stopped after the trigger burst is over.
                    # Finish as a success (the DAQ readout count is recorded as the truth);
                    # any shortfall shows up in the dropped-frame summary.
                    if (
                        self._expected_duration_s is not None
                        and elapsed_time >= self._expected_duration_s
                        and fc > 0
                        and (now - last_progress_time) >= stall_s
                    ):
                        self._log.warning(
                            f"Fast acquisition stalled: no new frame for {now - last_progress_time:.1f}s "
                            f"after the trigger burst; delivered {fc} of {num_frames} frames. Finishing."
                        )
                        self._stop_event.set()
                        self.stop_acquisition(manual_stop=False)
                        break

                time.sleep(0.02)  # Check every 20 ms (fine enough for stall detection)
        except Exception as e:
            self._log.error(f"Error in monitor thread: {e}", exc_info=True)
            self._stop_event.set()
            self.stop_acquisition(manual_stop=False, error_message=f"Monitor thread error: {e}")
        finally:
            if self._stop_event.is_set() and not self._stop_called:
                self.stop_acquisition(manual_stop=False)
    
    def _save_daq_data(self):
        """Save DAQ waveform data to file.

        Layout matches what the NIDAQ widget save/load helpers expect, so the
        resulting ``daq_data.h5`` can be loaded back into the widget.
        """
        if not self._daq_result:
            return

        import os
        waveforms_dir = os.path.join(self._output_path, "waveforms")
        os.makedirs(waveforms_dir, exist_ok=True)

        try:
            import h5py

            h5_path = os.path.join(waveforms_dir, "daq_data.h5")
            # Channel descriptions for HDF5 dataset attributes
            descriptions = {}
            if self._ni_daq and hasattr(self._ni_daq, "get_channel_descriptions"):
                descriptions = self._ni_daq.get_channel_descriptions()

            with h5py.File(h5_path, 'w') as f:
                # Save analog input
                for channel, data in self._daq_result.analog_input.items():
                    ds = f.create_dataset(f'analog_input/{channel}', data=data)
                    if channel in descriptions:
                        ds.attrs['description'] = descriptions[channel]

                # Save digital input
                for line, data in self._daq_result.digital_input.items():
                    ds = f.create_dataset(f'digital_input/line{line}', data=data)
                    if f"line{line}" in descriptions:
                        ds.attrs['description'] = descriptions[f"line{line}"]

                # Save frame sample indices
                if self._frame_sample_indices:
                    f.create_dataset('frame_sample_indices', data=np.array(self._frame_sample_indices))

                # AO/DO output waveforms (shared helper -> matches widget save format)
                write_waveform_datasets_h5(
                    f,
                    WaveformData(
                        analog_output=self._daq_result.analog_output,
                        digital_output=self._daq_result.digital_output,
                    ),
                    descriptions=descriptions,
                )

                # NIDAQ widget snapshot so loading from this file restores
                # trigger/port/AI-terminal/task-IO selection.
                snapshot = self._build_nidaq_snapshot()
                if snapshot is not None:
                    write_nidaq_snapshot_h5(f, snapshot)

                # Camera-specific & acquisition-level metadata
                f.attrs['sample_rate_hz'] = self._daq_result.sample_rate_hz
                f.attrs['samples_acquired'] = self._daq_result.samples_acquired
                f.attrs['camera_trigger_dio_line'] = self._camera_trigger_dio_line
                f.attrs['frame_counter_dio_line'] = self._frame_counter_dio_line
                f.attrs['num_frames_detected'] = len(self._frame_sample_indices)

            self._log.info(f"Saved DAQ data to {h5_path}")

        except ImportError:
            # Fallback to NumPy format
            np_path = os.path.join(waveforms_dir, "frame_sync_map.npy")
            np.save(np_path, np.array(self._frame_sample_indices))
            self._log.info(f"Saved frame sync map to {np_path} (HDF5 not available)")

    def _build_nidaq_snapshot(self) -> Optional[NIDAQConfigSnapshot]:
        """Build a NIDAQConfigSnapshot from ``self._ni_daq`` for HDF5 sidecar attrs.

        Returns None when no NIDAQ is attached. Pulls task-IO selections via
        ``get_task_io()`` so the saved snapshot reflects what was actually used
        for this acquisition.
        """
        if self._ni_daq is None:
            return None
        try:
            task_io = self._ni_daq.get_task_io() if hasattr(self._ni_daq, "get_task_io") else {}
            trigger_source = getattr(self._ni_daq, "trigger_source", None)
            trigger_edge = getattr(self._ni_daq, "trigger_edge", None)
            return NIDAQConfigSnapshot(
                device_name=str(getattr(self._ni_daq, "device_name", "") or "") or None,
                sample_rate_hz=float(self._ni_daq.sample_rate_hz),
                samples_per_channel=int(self._ni_daq.samples_per_channel),
                do_port=str(getattr(self._ni_daq, "do_port", "") or "") or None,
                trigger_source=getattr(trigger_source, "name", None) or (str(trigger_source) if trigger_source else None),
                trigger_edge=getattr(trigger_edge, "name", None) or (str(trigger_edge) if trigger_edge else None),
                external_trigger_terminal=str(getattr(self._ni_daq, "external_trigger_terminal", "") or "") or None,
                ai_terminal_config=str(getattr(self._ni_daq, "ai_terminal_config", "") or "") or None,
                do_logic_family=str(getattr(self._ni_daq, "do_logic_family", "") or "") or None,
                continuous=bool(getattr(self._ni_daq, "continuous", False)),
                selected_ao_channels=[str(x) for x in task_io.get("ao_channels", [])] or None,
                selected_ai_channels=[str(x) for x in task_io.get("ai_channels", [])] or None,
                selected_do_lines=[int(x) for x in task_io.get("do_lines", [])] or None,
                selected_di_lines=[int(x) for x in task_io.get("di_lines", [])] or None,
            )
        except Exception as e:
            self._log.warning(f"Could not build NIDAQ config snapshot: {e}", exc_info=True)
            return None
    
    def _patch_metadata_frame_counts(self, frame_count: int, frames_written: int, dropped_frames: int) -> None:
        """Rewrite the frame counters in metadata.json with the final post-drain values.

        metadata.json is written during stop_acquisition, before the writer's background
        drain finishes flushing the ring, so its frames_written/frames_dropped would
        otherwise be a stale mid-drain snapshot.
        """
        import json

        path = os.path.join(self._output_path, "metadata.json")
        try:
            if not os.path.isfile(path):
                return
            with open(path, "r") as f:
                meta = json.load(f)
            meta["frame_count"] = int(frame_count)
            meta["frames_written"] = int(frames_written)
            meta["frames_dropped"] = int(dropped_frames)
            with open(path, "w") as f:
                json.dump(meta, f, indent=2)
        except Exception as e:
            self._log.warning(f"Could not update frame counts in metadata.json: {e}", exc_info=True)

    def _save_metadata(self):
        """Save acquisition metadata."""
        import json

        duration = time.time() - self._start_time if self._start_time else 0
        metadata = {
            "daq_only": self._daq_only,
            "start_time": self._start_time,
            "duration": duration,
            "trigger_source": "NI_DAQ",
        }
        if not self._daq_only:
            frames_written = None
            dropped_frames = None
            try:
                if self._frame_writer is not None:
                    writer_stats = self._frame_writer.get_write_statistics()
                    frames_written = int(writer_stats.get("frames_written", 0))
                    expected_frames = int(self._frame_count)
                    dropped_frames = max(expected_frames - frames_written, 0)
            except Exception as e:
                self._log.warning(f"Could not compute writer statistics for metadata: {e}", exc_info=True)
            metadata["frame_count"] = self._frame_count
            metadata["frames_written"] = frames_written
            metadata["frames_dropped"] = dropped_frames
            metadata["camera_trigger_dio_line"] = self._camera_trigger_dio_line
            metadata["frame_counter_dio_line"] = self._frame_counter_dio_line
            metadata["buffer_size"] = (
                self._frame_buffer.get_buffer_status()["buffer_size"]
                if self._frame_buffer is not None
                else self._buffer_size
            )
            metadata["file_format"] = (
                self._frame_writer._file_format
                if self._frame_writer is not None
                else self._file_format
            )
            metadata["frame_shape_hw"] = list(self._frame_shape) if self._frame_shape is not None else None
            metadata["dtype"] = str(self._dtype) if self._dtype is not None else None
            metadata["frame_metadata_jsonl"] = os.path.join("frames", "frame_metadata.jsonl").replace(
                "\\", "/"
            )
            try:
                metadata["camera_settings"] = {
                    "exposure_time_ms": self._camera.get_exposure_time(),
                    "pixel_format": self._camera.get_pixel_format().name,
                    "roi": self._camera.get_region_of_interest(),
                }
            except Exception as e:
                self._log.warning(f"Could not get camera settings: {e}")
        if self._daq_result:
            metadata["daq_settings"] = {
                "sample_rate_hz": self._daq_result.sample_rate_hz,
                "samples_acquired": self._daq_result.samples_acquired,
                "frames_detected": len(self._frame_sample_indices) if self._frame_sample_indices else 0,
            }
        metadata_path = os.path.join(self._output_path, "metadata.json")
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)
        self._log.info(f"Saved metadata to {metadata_path}")
        self._save_acquisition_metadata_yaml_sidecar(metadata)

    def _save_acquisition_metadata_yaml_sidecar(self, metadata_json: dict) -> None:
        """Write acquisition_metadata.yaml next to metadata.json when microscope context is available."""
        scope = self._microscope
        lc = self._live_controller
        if scope is None or lc is None:
            return
        try:
            from control.core.acquisition_metadata_helpers import build_acquisition_metadata
            from control.core.observation_state_service import collect_emission_filter_positions

            repo = scope.config_repo
            exp_id = os.path.basename(self._output_path.rstrip(os.sep))
            obs_state = None
            if repo.current_profile:
                try:
                    wheel = getattr(scope, "emission_filter_wheel", None)
                    emission = collect_emission_filter_positions(wheel)
                    obs_state = lc.obs_controller.collect_observation_state(
                        emission_filter_positions=emission or None,
                    )
                except Exception as e:
                    self._log.warning("Fast acquisition: could not collect observation state: %s", e)
            scan_parameters = dict(metadata_json)
            scan_parameters["source"] = "fast_acquisition"
            scan_parameters.setdefault("metadata_json", "metadata.json")
            h5_rel = os.path.join("waveforms", "daq_data.h5")
            npy_rel = os.path.join("waveforms", "frame_sync_map.npy")
            if os.path.isfile(os.path.join(self._output_path, h5_rel)):
                scan_parameters["waveforms_h5"] = h5_rel.replace("\\", "/")
            elif os.path.isfile(os.path.join(self._output_path, npy_rel)):
                scan_parameters["waveforms_frame_sync_map"] = npy_rel.replace("\\", "/")

            am = build_acquisition_metadata(
                experiment_id=exp_id,
                recording_start_time=self._start_time or time.time(),
                objective_store=scope.objective_store,
                live_controller=lc,
                camera=scope.camera,
                scan_parameters=scan_parameters,
                observation_state=obs_state,
            )
            out = repo.save_acquisition_metadata(self._output_path, am)
            self._log.info("Saved acquisition metadata YAML to %s", out)
        except Exception as e:
            self._log.warning("Could not save acquisition_metadata.yaml for fast acquisition: %s", e, exc_info=True)

    def get_statistics(self) -> Dict:
        """Get acquisition statistics."""
        elapsed = time.time() - self._start_time if self._start_time else 1.0
        if self._daq_only:
            return {
                "duration_s": elapsed,
                "frame_count": 0,
                "frame_rate": 0.0,
                "buffer_fill_percent": 0,
                "frames_written": 0,
                "write_rate": 0,
                "avg_write_time_ms": 0,
            }
        if self._frame_buffer is None or self._frame_writer is None:
            with self._stats_lock:
                frame_rate = self._frame_count / elapsed if elapsed > 0 else 0.0
            return {
                "frame_count": self._frame_count,
                "frame_rate": frame_rate,
                "buffer_fill_percent": 0,
                "frames_written": 0,
                "write_rate": 0,
                "avg_write_time_ms": 0,
            }
        with self._stats_lock:
            buffer_status = self._frame_buffer.get_buffer_status()
            writer_stats = self._frame_writer.get_write_statistics()
            frame_rate = self._frame_count / elapsed if elapsed > 0 else 0.0
            return {
                "frame_count": self._frame_count,
                "frame_rate": frame_rate,
                "buffer_fill_percent": buffer_status["fill_percent"],
                "frames_written": writer_stats["frames_written"],
                "write_rate": writer_stats["write_rate"],
                "avg_write_time_ms": writer_stats["avg_write_time"],
            }
    
    @property
    def is_acquiring(self) -> bool:
        """Check if acquisition is running."""
        return self._is_acquiring
    
    def set_completion_callback(self, callback: Optional[Callable[[AcquisitionCompletionStatus, Optional[str]], None]]):
        """
        Set callback function to be called when acquisition completes.
        
        Args:
            callback: Function that takes (status: AcquisitionCompletionStatus, error_message: Optional[str])
                     Called when acquisition completes (success, error, or manual stop)
        """
        self._completion_callback = callback
    
    @property
    def completion_status(self) -> AcquisitionCompletionStatus:
        """
        Get the completion status of the last acquisition.
        
        Returns:
            AcquisitionCompletionStatus enum value indicating the status
        """
        return self._completion_status
    
    @property
    def last_completion_error(self) -> Optional[str]:
        """
        Get the error message from the last acquisition, if any.
        
        Returns:
            Error message string if last acquisition failed, None otherwise
        """
        return self._completion_error_message
    
    def was_last_acquisition_successful(self) -> bool:
        """
        Check if the last acquisition completed successfully.
        
        Returns:
            True if last acquisition completed successfully, False otherwise
        """
        return self._completion_status == AcquisitionCompletionStatus.COMPLETED_SUCCESS
    
    def _notify_completion(self, status: AcquisitionCompletionStatus, error_message: Optional[str] = None):
        """
        Notify that acquisition has completed.
        
        Args:
            status: Completion status
            error_message: Optional error message if status indicates an error
        """
        self._completion_status = status
        self._completion_error_message = error_message
        
        if self._completion_callback:
            try:
                self._completion_callback(status, error_message)
            except Exception as e:
                self._log.error(f"Error in completion callback: {e}", exc_info=True)
