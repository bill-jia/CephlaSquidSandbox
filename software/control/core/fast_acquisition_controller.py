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
from control.ni_daq import AbstractNIDAQ, NIDAQConfig, WaveformData, TriggerSource
from control.ni_daq import generate_pulse_train


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
    
    def __init__(self, camera: AbstractCamera,
                 ni_daq: Optional[AbstractNIDAQ],
                 output_path: str,
                 buffer_size: int = 500,
                 file_format: str = "tiff",
                 trigger_dio_line: int = 1,
                 camera_frame_dio_line: int = 0):
        """
        Initialize fast acquisition controller.
        
        Args:
            camera: Camera instance
            ni_daq: NI DAQ instance (for triggering and waveform recording)
            output_path: Base directory for saving data
            buffer_size: Number of frames to buffer in memory
            file_format: File format for saving ("tiff", "zarr", or "hdf5")
            trigger_dio_line: Digital output line for camera triggers (default: 1)
            camera_frame_dio_line: Digital input line for camera frame signal (default: 0)
        """
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self._camera = camera
        self._ni_daq = ni_daq
        self._output_path = output_path
        self._trigger_dio_line = trigger_dio_line
        self._camera_frame_dio_line = camera_frame_dio_line
        
        # Get frame shape from camera
        roi = camera.get_region_of_interest()
        frame_shape = (roi[3], roi[2])  # (height, width)
        pixel_format = camera.get_pixel_format()
        
        # Determine dtype from pixel format
        dtype_map = {
            "MONO8": np.uint8,
            "MONO10": np.uint8,  # Packed format
            "MONO12": np.uint16,
            "MONO14": np.uint16,
            "MONO16": np.uint16,
        }
        dtype = dtype_map.get(pixel_format.name, np.uint16)
        
        # Initialize frame buffer
        self._frame_buffer = FastAcquisitionFrameBuffer(
            buffer_size=buffer_size,
            frame_shape=frame_shape,
            dtype=dtype,
            overwrite_when_full=True
        )
        
        # Initialize frame writer
        self._frame_writer = FastAcquisitionWriter(
            frame_buffer=self._frame_buffer,
            output_path=output_path,
            file_format=file_format
        )
        
        # State
        self._is_acquiring = False
        self._frame_count = 0
        self._start_time = None
        self._stop_event = threading.Event()
        
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
                         di_lines: Optional[list] = None,
                         acquisition_mode: Optional[CameraAcquisitionMode] = None):
        """
        Start fast acquisition with preloaded NI DAQ waveforms.
        
        Args:
            num_frames: Number of frames to acquire (None for continuous)
            frame_rate_hz: Target frame rate
            exposure_time_ms: Exposure time per frame
            sample_rate_hz: NI DAQ sample rate for waveforms
            ai_channels: Optional analog input channels to record
            di_lines: Optional additional digital input lines to record
            acquisition_mode: Camera acquisition mode (HARDWARE_TRIGGER or HARDWARE_TRIGGER_FIRST).
                            If None, defaults to HARDWARE_TRIGGER.
        """
        if self._is_acquiring:
            self._log.warning("Acquisition already running")
            return
        
        if self._ni_daq is None:
            raise ValueError("NI DAQ is required for fast acquisition")
        
        self._log.info(
            f"Starting fast acquisition: frames={num_frames}, "
            f"rate={frame_rate_hz} Hz, exposure={exposure_time_ms} ms"
        )
        
        # Calculate duration and samples
        if num_frames is None:
            # Continuous mode - use a long duration (e.g., 1 hour)
            duration_s = 1
            num_frames_estimate = int(frame_rate_hz * duration_s)
        else:
            duration_s = num_frames / frame_rate_hz
            num_frames_estimate = num_frames
        
        n_samples_offset = 5
        samples_per_channel = int(sample_rate_hz * duration_s) + n_samples_offset
        
        # Generate trigger waveform (pulse train on DIO 1)
        frame_period_samples = int(sample_rate_hz / frame_rate_hz)
        pulse_width_samples = 4
        
        trigger_pattern = generate_pulse_train(
            pulse_width_samples=pulse_width_samples,
            period_samples=frame_period_samples,
            num_samples=samples_per_channel,
            n_samples_offset=n_samples_offset,
            inverted=False
        )
        
        # Set up NI DAQ configuration
        di_lines_to_record = [self._camera_frame_dio_line]
        if di_lines:
            di_lines_to_record.extend(di_lines)
        di_lines_to_record = list(set(di_lines_to_record))  # Remove duplicates
        
        config = NIDAQConfig(
            device_name=self._ni_daq.config.device_name,
            sample_rate_hz=sample_rate_hz,
            samples_per_channel=samples_per_channel,
            do_port="port0",
            do_lines=[self._trigger_dio_line],
            di_port="port0",
            di_lines=di_lines_to_record,
            ai_channels=ai_channels or [],
            trigger_source=TriggerSource.SOFTWARE,
            continuous=False,
        )
        
        # Create waveform data
        waveforms = WaveformData(
            digital_output={self._trigger_dio_line: trigger_pattern}
        )
        
        # Configure and arm NI DAQ
        self._ni_daq.configure(config)
        self._ni_daq.set_waveforms(waveforms)
        self._ni_daq.arm()
        
        # Stop any existing streaming
        if self._camera.get_is_streaming():
            self._log.info("Stopping existing camera streaming for fast acquisition")
            self._camera.stop_streaming()
        
        # Set camera to hardware trigger mode (required for fast acquisition)
        # Use provided acquisition_mode or default to HARDWARE_TRIGGER
        if acquisition_mode is None:
            acquisition_mode = CameraAcquisitionMode.HARDWARE_TRIGGER
        
        if acquisition_mode not in [CameraAcquisitionMode.HARDWARE_TRIGGER, CameraAcquisitionMode.HARDWARE_TRIGGER_FIRST]:
            raise ValueError(f"Invalid acquisition mode for fast acquisition: {acquisition_mode}. Must be HARDWARE_TRIGGER or HARDWARE_TRIGGER_FIRST")
        
        try:
            self._camera.set_acquisition_mode(acquisition_mode)
            self._log.info(f"Camera set to {acquisition_mode.value} mode")
        except (NotImplementedError, ValueError) as e:
            self._log.error(f"Camera does not support {acquisition_mode.value} mode: {e}")
            raise
        
        # Set exposure time
        self._camera.set_exposure_time(exposure_time_ms)
        self._camera.fast_acquisition_timeout_ms = int(np.ceil(1/frame_rate_hz*1000*1.1))
        
        # Optimize camera buffer settings for fast acquisition
        if hasattr(self._camera, '_optimize_for_fast_acquisition'):
            try:
                self._camera._optimize_for_fast_acquisition()
            except Exception as e:
                self._log.warning(f"Could not optimize camera for fast acquisition: {e}")
        
        # Start frame writer thread
        self._frame_writer.start()
        
        # Start acquisition state
        self._is_acquiring = True
        self._frame_count = 0
        self._start_time = time.time()
        self._stop_event.clear()
        self._completion_status = AcquisitionCompletionStatus.IN_PROGRESS
        self._completion_error_message = None
        
        # Define frame callback for fast acquisition
        # Frame IDs and timestamps will be determined from DAQ synchronization
        def frame_callback(frame: np.ndarray):
            """Callback to write frames to buffer as they arrive.
            
            Frame IDs and timestamps are not tracked here - they will be determined
            from DAQ digital input synchronization after acquisition completes.
            """
            # Use sequential buffer index as placeholder frame_id
            # Real frame mapping will come from DAQ edge detection
            placeholder_frame_id = self._frame_count
            
            success = self._frame_buffer.write_frame(
                frame,
                placeholder_frame_id,
                time.time()
            )
            
            if success:
                self._frame_count += 1
                with self._stats_lock:
                    self._last_frame_time = time.time()
            else:
                self._log.warning(f"Failed to write frame {placeholder_frame_id} to buffer")
        
        # Start fast acquisition frame grabbing (this starts camera acquisition)
        if hasattr(self._camera, 'start_fast_acquisition_frame_grabbing'):
            self._camera.start_fast_acquisition_frame_grabbing(frame_callback=frame_callback)
        else:
            raise NotImplementedError(
                "Camera does not support fast acquisition frame grabbing. "
                "This requires a camera implementation with start_fast_acquisition_frame_grabbing() method."
            )
        
        # Start monitoring thread to check for stop conditions
        self._monitor_thread = threading.Thread(
            target=self._monitor_acquisition,
            args=(num_frames,),
            daemon=True
        )
        self._monitor_thread.start()
        
        # Start NI DAQ (this triggers the preloaded waveform sequence)
        self._ni_daq.start_trigger()
        self._log.info(f"NIDAQ is running: {self._ni_daq.is_running}")
        self._log.info("Fast acquisition started with NI DAQ waveforms")
    
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
        
        self._log.info(f"Stopping fast acquisition (manual={manual_stop}, error={error_message is not None})...")
        
        # Signal stop
        self._stop_event.set()
        self._is_acquiring = False
        
        completion_status = None
        completion_error = error_message
        
        try:
            # Stop NI DAQ
            self._log.info(f"NIDAQ is running: {self._ni_daq.is_running}")
            if self._ni_daq:
                # Wait for completion and get data
                daq_success = self._ni_daq.wait_until_done(timeout_s=10.0)
                if not daq_success and error_message is None:
                    completion_error = "DAQ did not complete within timeout"
                
                self._daq_result = self._ni_daq.get_acquired_data()

                self._log.info(f"DAQ result: {self._daq_result}")
                # Detect frame edges from camera frame signal
                if self._daq_result and len(self._daq_result.digital_input) > 0:
                    camera_signal = self._daq_result.digital_input.get(self._camera_frame_dio_line)
                    if camera_signal is not None:
                        self._frame_sample_indices = self._detect_frame_edges(camera_signal)
                        self._log.info(f"Detected {len(self._frame_sample_indices)} frames from camera signal")
            
            # Stop fast acquisition frame grabbing
            if hasattr(self._camera, 'stop_fast_acquisition_frame_grabbing'): 
                self._camera.stop_fast_acquisition_frame_grabbing()
            
            # Stop frame writer (will flush remaining frames)
            self._frame_writer.stop()
            
            # Save DAQ data and metadata
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
        """Monitor acquisition and stop when frame limit is reached or stop event is set."""
        try:
            while not self._stop_event.is_set() and self._is_acquiring:
                # Check frame limit
                if num_frames is not None and self._frame_count >= num_frames:
                    self._log.info(f"Reached frame limit ({num_frames}), stopping acquisition")
                    self._stop_event.set()
                    break
                
                time.sleep(1)  # Check every 1 s
        except Exception as e:
            self._log.error(f"Error in monitor thread: {e}", exc_info=True)
            # Stop with error
            self._stop_event.set()
            threading.Thread(
                target=lambda: self.stop_acquisition(manual_stop=False, error_message=f"Monitor thread error: {e}"),
                daemon=True
            ).start()
        finally:
            if self._stop_event.is_set() and self._is_acquiring:
                # Stop acquisition automatically (not manual stop)
                threading.Thread(
                    target=lambda: self.stop_acquisition(manual_stop=False),
                    daemon=True
                ).start()
    
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
        
        metadata = {
            "frame_count": self._frame_count,
            "start_time": self._start_time,
            "duration": time.time() - self._start_time if self._start_time else 0,
            "trigger_source": "NI_DAQ",
            "trigger_dio_line": self._trigger_dio_line,
            "camera_frame_dio_line": self._camera_frame_dio_line,
            "buffer_size": self._frame_buffer.get_buffer_status()["buffer_size"],
            "file_format": self._frame_writer._file_format,
        }
        
        # Add camera settings
        try:
            metadata["camera_settings"] = {
                "exposure_time_ms": self._camera.get_exposure_time(),
                "pixel_format": self._camera.get_pixel_format().name,
                "roi": self._camera.get_region_of_interest(),
            }
        except Exception as e:
            self._log.warning(f"Could not get camera settings: {e}")
        
        # Add DAQ settings if available
        if self._daq_result:
            metadata["daq_settings"] = {
                "sample_rate_hz": self._daq_result.sample_rate_hz,
                "samples_acquired": self._daq_result.samples_acquired,
                "frames_detected": len(self._frame_sample_indices),
            }
        
        metadata_path = os.path.join(self._output_path, "metadata.json")
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)
        
        self._log.info(f"Saved metadata to {metadata_path}")
    
    def get_statistics(self) -> Dict:
        """Get acquisition statistics."""
        with self._stats_lock:
            buffer_status = self._frame_buffer.get_buffer_status()
            writer_stats = self._frame_writer.get_write_statistics()
            
            elapsed = time.time() - self._start_time if self._start_time else 1.0
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
