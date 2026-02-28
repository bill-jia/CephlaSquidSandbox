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
from typing import Optional, Dict, Callable
import numpy as np
from scipy import ndimage
import squid.logging
import matplotlib.pyplot as plt

from squid.abc import AbstractCamera, CameraAcquisitionMode, CameraFrame
from control.core.fast_acquisition_buffer import FastAcquisitionFrameBuffer
from control.core.fast_acquisition_writer import FastAcquisitionWriter
from control.ni_daq import AbstractNIDAQ, WaveformData, TriggerSource
from control.ni_daq import generate_pulse_train
from control._def import NIDAQ_CONFIG


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
                 trigger_dio_line: int = 1,
                 camera_frame_dio_line: int = 0):
        """
        Initialize fast acquisition controller.

        When camera is None (DAQ-only mode), no frame buffer or writer is created;
        only DAQ waveform output and recording are performed.

        Args:
            camera: Camera instance, or None for DAQ-only (waveform output/recording only)
            ni_daq: NI DAQ instance (for triggering and waveform recording)
            output_path: Base directory for saving data
            buffer_size: Number of frames to buffer in memory (ignored when camera is None)
            file_format: File format for saving ("tiff", "zarr", or "hdf5") (ignored when camera is None)
            trigger_dio_line: Digital output line for camera triggers (default: 1); unused in DAQ-only
            camera_frame_dio_line: Digital input line for camera frame signal (default: 0); unused in DAQ-only
        """
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self._camera = camera
        self._ni_daq = ni_daq
        self._output_path = output_path
        self._trigger_dio_line = trigger_dio_line
        self._camera_frame_dio_line = camera_frame_dio_line
        self._daq_only = camera is None

        if camera is not None:
            # Get frame shape from camera
            roi = camera.get_region_of_interest()
            frame_shape = (roi[3], roi[2])  # (height, width)
            pixel_format = camera.get_pixel_format()
            dtype_map = {
                "MONO8": np.uint8,
                "MONO10": np.uint8,
                "MONO12": np.uint16,
                "MONO14": np.uint16,
                "MONO16": np.uint16,
            }
            dtype = dtype_map.get(pixel_format.name, np.uint16)
            self._frame_shape = frame_shape
            self._dtype = dtype
            self._frame_buffer = FastAcquisitionFrameBuffer(
                buffer_size=buffer_size,
                frame_shape=frame_shape,
                dtype=dtype,
                overwrite_when_full=True
            )
            self._frame_writer = FastAcquisitionWriter(
                frame_buffer=self._frame_buffer,
                output_path=output_path,
                file_format=file_format
            )
        else:
            self._frame_shape = None
            self._dtype = None
            self._frame_buffer = None
            self._frame_writer = None

        # State
        self._is_acquiring = False
        self._frame_count = 0
        self._start_time = None
        self._stop_event = threading.Event()
        self._expected_duration_s: Optional[float] = None
        self._timeout_s: Optional[float] = None
        self._stop_called = False  # Flag to prevent duplicate stop_acquisition calls

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
        else:
            self._log.info(
                f"Initialized fast acquisition controller: "
                f"buffer_size={buffer_size}, format={file_format}, "
                f"output={output_path}, trigger_line={trigger_dio_line}, "
                f"frame_signal_line={camera_frame_dio_line}"
            )
    
    def start_acquisition(self, num_frames: Optional[int] = None,
                         frame_rate_hz: float = 10.0,
                         exposure_time_ms: float = 20.0,
                         sample_rate_hz: float = 10000.0,
                         ai_channels: Optional[list] = None,
                         ao_channels: Optional[list] = None,
                         di_lines: Optional[list] = None,
                         acquisition_mode: Optional[CameraAcquisitionMode] = None,
                         waveforms: Optional[WaveformData] = None,
                         trigger_dio_line: Optional[int] = None,
                         camera_frame_dio_line: Optional[int] = None,
                         duration_s: Optional[float] = None):
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
            trigger_dio_line: Optional trigger line number (overrides default); ignored in DAQ-only
            camera_frame_dio_line: Optional camera frame counter line; ignored in DAQ-only
            duration_s: Duration in seconds. Required when camera is None (DAQ-only mode).
        """
        if self._is_acquiring:
            self._log.warning("Acquisition already running")
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
        else:
            duration_s = num_frames / frame_rate_hz
            num_frames_estimate = num_frames

        # Store expected duration and timeout
        self._expected_duration_s = duration_s
        self._timeout_s = duration_s + 10
        self._log.info(f"Expected acquisition duration: {duration_s:.2f}s, timeout: {self._timeout_s:.2f}s")

        if not self._daq_only:
            if trigger_dio_line is not None:
                self._trigger_dio_line = trigger_dio_line
            if camera_frame_dio_line is not None:
                self._camera_frame_dio_line = camera_frame_dio_line

        n_samples_offset = 1
        samples_per_channel = int(sample_rate_hz * duration_s)

        # Get waveforms: in DAQ-only use as-is; with camera add trigger pattern
        if self._daq_only:
            if waveforms is None:
                waveforms = WaveformData()
        else:
            if waveforms is None:
                frame_period_samples = int(sample_rate_hz / frame_rate_hz)
                pulse_width_samples = 4
                trigger_pattern = generate_pulse_train(
                    pulse_width_samples=pulse_width_samples,
                    period_samples=frame_period_samples,
                    num_samples=samples_per_channel,
                    n_samples_offset=n_samples_offset,
                    inverted=False
                )
                waveforms = WaveformData(
                    digital_output={self._trigger_dio_line: trigger_pattern}
                )
            else:
                frame_period_samples = int(sample_rate_hz / frame_rate_hz)
                pulse_width_samples = 4
                trigger_pattern = generate_pulse_train(
                    pulse_width_samples=pulse_width_samples,
                    period_samples=frame_period_samples,
                    num_samples=samples_per_channel,
                    n_samples_offset=n_samples_offset,
                    inverted=False
                )
                waveforms.digital_output[self._trigger_dio_line] = trigger_pattern

        # Digital input lines to record
        if self._daq_only:
            di_lines_to_record = list(di_lines) if di_lines else []
        else:
            di_lines_to_record = [self._camera_frame_dio_line]
            if di_lines:
                di_lines_to_record.extend(di_lines)
            di_lines_to_record = list(set(di_lines_to_record))

        do_lines_from_waveforms = list(waveforms.digital_output.keys())

        config = NIDAQ_CONFIG(
            device_name=self._ni_daq.config.device_name,
            sample_rate_hz=sample_rate_hz,
            samples_per_channel=samples_per_channel,
            do_port="port0",
            do_lines=do_lines_from_waveforms,
            di_port="port0",
            di_lines=di_lines_to_record,
            ai_channels=ai_channels or [],
            ao_channels=ao_channels or [],
            trigger_source=self._ni_daq.config.trigger_source,
            continuous=False,
            do_logic_family=self._ni_daq.config.do_logic_family,
        )

        self._ni_daq.configure(config)
        self._ni_daq.set_waveforms(waveforms)
        self._ni_daq.arm()

        if not self._daq_only:
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
            self._camera.set_exposure_time(exposure_time_ms)
            self._camera.fast_acquisition_timeout_ms = int(np.ceil(1 / frame_rate_hz * 1000 * 1.1))
            if hasattr(self._camera, '_optimize_for_fast_acquisition'):
                try:
                    self._camera._optimize_for_fast_acquisition()
                except Exception as e:
                    self._log.warning(f"Could not optimize camera for fast acquisition: {e}")
            self._frame_writer.start()

        self._is_acquiring = True
        self._frame_count = 0
        self._start_time = time.time()
        self._stop_event.clear()
        self._stop_called = False
        self._completion_status = AcquisitionCompletionStatus.IN_PROGRESS
        self._completion_error_message = None

        if not self._daq_only:
            def frame_callback(frame: np.ndarray, metadata: dict = None):
                placeholder_frame_id = self._frame_count
                timestamp = time.time() if metadata is None else float(metadata["frame_header"]["timestampEofPs"]) / 1e9
                success = self._frame_buffer.write_frame(frame, placeholder_frame_id, timestamp)
                if success:
                    self._frame_count += 1
                    with self._stats_lock:
                        self._last_frame_time = time.time()
                else:
                    self._log.warning(f"Failed to write frame {placeholder_frame_id} to buffer")

            if hasattr(self._camera, 'start_fast_acquisition_frame_grabbing'):
                self._camera.start_fast_acquisition_frame_grabbing(frame_rate_hz, frame_callback=frame_callback)
            else:
                raise NotImplementedError(
                    "Camera does not support fast acquisition frame grabbing. "
                    "This requires a camera implementation with start_fast_acquisition_frame_grabbing() method."
                )

        self._monitor_thread = threading.Thread(
            target=self._monitor_acquisition,
            args=(num_frames if not self._daq_only else None,),
            daemon=True
        )
        self._monitor_thread.start()

        self._ni_daq.start_trigger()
        self._log.info(f"NIDAQ is running: {self._ni_daq.is_running}")
        self._log.info("Fast acquisition started with NI DAQ waveforms" + (" (DAQ-only)" if self._daq_only else ""))
    
    def stop_acquisition(self, manual_stop: bool = False, error_message: Optional[str] = None):
        """
        Stop fast acquisition.
        
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
        
        # Signal stop
        self._stop_event.set()
        self._is_acquiring = False
        
        completion_status = None
        completion_error = error_message
        
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

                # Detect frame edges from camera frame signal (camera mode only)
                if not self._daq_only and self._daq_result and len(self._daq_result.digital_input) > 0:
                    camera_signal = self._daq_result.digital_input.get(self._camera_frame_dio_line)
                    if camera_signal is not None:
                        self._frame_sample_indices = self._detect_frame_edges(camera_signal)
                        self._log.info(f"Detected {len(self._frame_sample_indices)} frames from camera signal")

            if not self._daq_only:
                if hasattr(self._camera, 'stop_fast_acquisition_frame_grabbing'):
                    self._camera.stop_fast_acquisition_frame_grabbing()
                self._frame_writer.stop()
                try:
                    writer_stats = self._frame_writer.get_write_statistics()
                    frames_written = int(writer_stats.get("frames_written", 0))
                    expected_frames = int(self._frame_count)
                    dropped_frames = max(expected_frames - frames_written, 0)
                    self._log.info(
                        f"Fast acquisition frame summary: "
                        f"expected={expected_frames}, written={frames_written}, "
                        f"dropped={dropped_frames}"
                    )
                except Exception as e:
                    self._log.warning(f"Failed to compute dropped frame statistics: {e}", exc_info=True)
                if getattr(self._frame_writer, "_file_format", "").lower() == "tiff":
                    self._start_tiff_stack_conversion_thread()

            # Save DAQ data and metadata (both camera and DAQ-only)
            self._save_daq_data()
            self._save_metadata()
            
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
    
    def _monitor_acquisition(self, num_frames: Optional[int]):
        """Monitor acquisition and stop when frame limit or duration is reached, timeout occurs, or stop event is set."""
        try:
            while not self._stop_event.is_set() and self._is_acquiring:
                elapsed_time = time.time() - self._start_time if self._start_time else 0

                # Check timeout
                if self._timeout_s is not None and self._start_time is not None:
                    if elapsed_time >= self._timeout_s:
                        timeout_message = (
                            f"Acquisition timeout reached: {elapsed_time:.2f}s >= {self._timeout_s:.2f}s "
                            f"(expected duration: {self._expected_duration_s:.2f}s + 10s buffer). "
                            f"Frames acquired: {self._frame_count}"
                        )
                        self._log.error(timeout_message)
                        self._stop_event.set()
                        self.stop_acquisition(manual_stop=False, error_message=timeout_message)
                        break

                # DAQ-only: stop when expected duration has elapsed
                if self._daq_only and self._expected_duration_s is not None:
                    if elapsed_time >= self._expected_duration_s:
                        self._log.info("DAQ-only: expected duration reached, stopping acquisition")
                        self._stop_event.set()
                        break
                # Camera mode: check frame limit
                elif num_frames is not None and self._frame_count >= num_frames:
                    self._log.info(f"Reached frame limit ({num_frames}), stopping acquisition")
                    self._stop_event.set()
                    break

                time.sleep(0.1)  # Check every 100 ms (finer for DAQ-only duration)
        except Exception as e:
            self._log.error(f"Error in monitor thread: {e}", exc_info=True)
            self._stop_event.set()
            self.stop_acquisition(manual_stop=False, error_message=f"Monitor thread error: {e}")
        finally:
            if self._stop_event.is_set() and not self._stop_called:
                self.stop_acquisition(manual_stop=False)
    
    def _save_daq_data(self):
        """Save DAQ waveform data to file."""
        if not self._daq_result:
            return
        
        import os
        waveforms_dir = os.path.join(self._output_path, "waveforms")
        os.makedirs(waveforms_dir, exist_ok=True)
        
        try:
            import h5py
            
            h5_path = os.path.join(waveforms_dir, "daq_data.h5")
            with h5py.File(h5_path, 'w') as f:
                # Save analog input
                for channel, data in self._daq_result.analog_input.items():
                    f.create_dataset(f'analog_input/{channel}', data=data)
                
                # Save digital input
                for line, data in self._daq_result.digital_input.items():
                    f.create_dataset(f'digital_input/line{line}', data=data)
                
                # Save frame sample indices
                if self._frame_sample_indices:
                    f.create_dataset('frame_sample_indices', data=np.array(self._frame_sample_indices))

                for channel, data in self._daq_result.analog_output.items():
                    f.create_dataset(f'analog_output/{channel}', data=data)

                for line, data in self._daq_result.digital_output.items():
                    f.create_dataset(f'digital_output/line{line}', data=data)
                
                # Save metadata
                f.attrs['sample_rate_hz'] = self._daq_result.sample_rate_hz
                f.attrs['samples_acquired'] = self._daq_result.samples_acquired
                f.attrs['trigger_dio_line'] = self._trigger_dio_line
                f.attrs['camera_frame_dio_line'] = self._camera_frame_dio_line
                f.attrs['num_frames_detected'] = len(self._frame_sample_indices)
            
            self._log.info(f"Saved DAQ data to {h5_path}")
        
        except ImportError:
            # Fallback to NumPy format
            np_path = os.path.join(waveforms_dir, "frame_sync_map.npy")
            np.save(np_path, np.array(self._frame_sample_indices))
            self._log.info(f"Saved frame sync map to {np_path} (HDF5 not available)")
    
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
                writer_stats = self._frame_writer.get_write_statistics()
                frames_written = int(writer_stats.get("frames_written", 0))
                expected_frames = int(self._frame_count)
                dropped_frames = max(expected_frames - frames_written, 0)
            except Exception as e:
                self._log.warning(f"Could not compute writer statistics for metadata: {e}", exc_info=True)
            metadata["frame_count"] = self._frame_count
            metadata["frames_written"] = frames_written
            metadata["frames_dropped"] = dropped_frames
            metadata["trigger_dio_line"] = self._trigger_dio_line
            metadata["camera_frame_dio_line"] = self._camera_frame_dio_line
            metadata["buffer_size"] = self._frame_buffer.get_buffer_status()["buffer_size"]
            metadata["file_format"] = self._frame_writer._file_format
            metadata["frame_shape_hw"] = list(self._frame_shape) if self._frame_shape is not None else None
            metadata["dtype"] = str(self._dtype) if self._dtype is not None else None
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

    def _start_tiff_stack_conversion_thread(self):
        """
        Start a background thread that converts the raw bytestream written
        during acquisition into a 3D TIFF stack.

        This is intentionally decoupled from the acquisition so that heavy I/O
        and compression do not interfere with frame capture.
        """

        def _worker():
            try:
                self._convert_raw_to_tiff_stack()
            except Exception as e:
                self._log.error(f"Error converting raw frames to TIFF stack: {e}", exc_info=True)

        t = threading.Thread(target=_worker, name="FastAcq-TIFF-Conversion", daemon=True)
        t.start()

    def _convert_raw_to_tiff_stack(self):
        """
        Convert the raw bytestream file produced by FastAcquisitionWriter
        (TIFF mode) into a single 3D TIFF stack.
        """
        import os
        import imageio as iio

        # Raw file is written by FastAcquisitionWriter in frames/frames.raw
        raw_path = os.path.join(self._output_path, "frames", "frames.raw")
        if not os.path.exists(raw_path):
            self._log.warning(f"Raw frame file not found at {raw_path}, skipping TIFF stack conversion")
            return

        if not hasattr(self, "_frame_shape") or not hasattr(self, "_dtype"):
            self._log.error("Frame shape or dtype not available, cannot convert raw data to TIFF stack")
            return

        height, width = self._frame_shape
        dtype = self._dtype

        # Compute expected bytes per frame
        pixels_per_frame = int(height * width)
        bytes_per_pixel = np.dtype(dtype).itemsize
        bytes_per_frame = pixels_per_frame * bytes_per_pixel

        file_size = os.path.getsize(raw_path)
        if bytes_per_frame == 0:
            self._log.error("Computed bytes per frame is zero, cannot convert raw data")
            return

        # Use the smaller of: frame_count and file_size-derived frame count
        max_frames_from_file = file_size // bytes_per_frame
        n_frames = min(self._frame_count, max_frames_from_file)

        if n_frames <= 0:
            self._log.warning(
                f"No frames to convert (frame_count={self._frame_count}, "
                f"file_size={file_size}, bytes_per_frame={bytes_per_frame})"
            )
            return

        if self._frame_count != max_frames_from_file:
            self._log.warning(
                "Mismatch between recorded frame_count and raw file size: "
                f"frame_count={self._frame_count}, "
                f"file_size={file_size}, "
                f"bytes_per_frame={bytes_per_frame}, "
                f"frames_from_file={max_frames_from_file}. "
                f"Using n_frames={n_frames}."
            )

        self._log.info(
            f"Converting raw frames to 3D TIFF stack: {n_frames} frames, "
            f"shape=({height},{width}), dtype={dtype}, raw_path={raw_path}"
        )

        # Read raw data and reshape into (n_frames, height, width)
        with open(raw_path, "rb") as f:
            raw = np.fromfile(f, dtype=dtype, count=n_frames * pixels_per_frame)

        if raw.size != n_frames * pixels_per_frame:
            self._log.warning(
                f"Read {raw.size} pixels, expected {n_frames * pixels_per_frame}; "
                "resulting stack may be truncated."
            )
            n_frames = raw.size // pixels_per_frame
            raw = raw[: n_frames * pixels_per_frame]

        volume = raw.reshape((n_frames, height, width))

        # Write 3D TIFF stack next to raw file
        stack_path = os.path.join(self._output_path, "frames", "frames_stack.tiff")
        try:
            iio.mimwrite(stack_path, volume, format="tiff")
            self._log.info(f"Wrote 3D TIFF stack to {stack_path}")
            
            # Delete raw file after successful conversion to save disk space
            try:
                os.remove(raw_path)
                self._log.info(f"Deleted raw frame file {raw_path} after successful TIFF stack conversion")
            except Exception as e:
                self._log.warning(f"Failed to delete raw file {raw_path}: {e}", exc_info=True)
                # Non-fatal: conversion succeeded, just couldn't clean up raw file
        except Exception as e:
            self._log.error(f"Failed to write TIFF stack to {stack_path}: {e}", exc_info=True)
            # Don't delete raw file if conversion failed - user may want to retry
    
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
