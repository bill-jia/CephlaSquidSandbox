"""
Camera controller for Teledyne QImaging Retiga Electro camera.

This module provides the RetigaElectroCamera class, which implements the AbstractCamera
interface for the Retiga Electro camera using PyVCAM (PVCAM SDK wrapper).

The Retiga Electro is a scientific CCD camera featuring:
- Sensor: 4096 x 3286 pixels
- Pixel size: 4.54 µm (Retiga Electro SRV) or 6.45 µm (standard)
- Readout modes: Multiple speed/sensitivity modes
- Thermoelectric cooling
- USB 3.0 interface

Trigger modes supported:
- SOFTWARE_TRIGGER: Camera waits for software command
- HARDWARE_TRIGGER: Camera responds to external hardware trigger
- CONTINUOUS: Free-running internal trigger
"""

from pyvcam import pvc
from pyvcam.camera import Camera as PVCam
from typing import Callable, Optional, Tuple, Sequence
import numpy as np
import threading
import time

import squid.logging
from squid.config import CameraConfig, CameraPixelFormat
from squid.abc import (
    AbstractCamera,
    CameraAcquisitionMode,
    CameraFrameFormat,
    CameraFrame,
    CameraGainRange,
    CameraError,
)


class RetigaElectroCamera(AbstractCamera):
    """
    Camera controller for Teledyne QImaging Retiga Electro camera.
    
    Uses PyVCAM for PVCAM SDK communication. Supports multiple readout modes,
    hardware/software triggering, and temperature control.
    """
    
    # Pixel sizes in micrometers for different Retiga Electro models
    PIXEL_SIZE_UM_STANDARD = 6.45  # Standard Retiga Electro
    PIXEL_SIZE_UM_SRV = 4.54       # Retiga Electro SRV
    
    # Default sensor dimensions (can be overridden based on actual camera)
    DEFAULT_WIDTH = 1376
    DEFAULT_HEIGHT = 1024
    
    # Temperature limits in Celsius
    TEMP_MIN_C = 0
    TEMP_MAX_C = 30

    @staticmethod
    def _open(sn: Optional[str] = None, index: Optional[int] = None) -> PVCam:
        """
        Open a Retiga Electro camera and return the camera object.
        
        Args:
            sn: Serial number of the camera to open (optional)
            index: Index of the camera to open (optional, used if sn not provided)
            
        Returns:
            Opened PVCam camera object
            
        Raises:
            CameraError: If camera cannot be opened
        """
        log = squid.logging.get_logger("RetigaElectroCamera._open")

        pvc.init_pvcam()

        try:
            cameras = list(PVCam.detect_camera())
            
            if not cameras:
                raise CameraError("No PVCAM-compatible cameras found.")
            
            if sn is not None:
                # Try to find camera by serial number
                cam = None
                for camera_candidate in cameras:
                    camera_candidate.open()
                    try:
                        if hasattr(camera_candidate, 'serial_no') and camera_candidate.serial_no == sn:
                            cam = camera_candidate
                            break
                    except:
                        pass
                    camera_candidate.close()
                
                if cam is None:
                    raise CameraError(f"Camera with serial number {sn} not found.")
            elif index is not None:
                if index >= len(cameras):
                    raise CameraError(f"Camera index {index} out of range. Found {len(cameras)} cameras.")
                cam = cameras[index]
                cam.open()
            else:
                # Open first available camera
                cam = cameras[0]
                cam.open()

            log.info(f"Retiga Electro camera opened successfully: {cam.name if hasattr(cam, 'name') else 'Unknown'}")
            return cam

        except Exception as e:
            pvc.uninit_pvcam()
            raise CameraError(f"Failed to open Retiga Electro camera: {e}")

    def __init__(
        self,
        camera_config: CameraConfig,
        hw_trigger_fn: Optional[Callable[[Optional[float]], bool]],
        hw_set_strobe_delay_ms_fn: Optional[Callable[[float], bool]],
    ):
        """
        Initialize the Retiga Electro camera.
        
        Args:
            camera_config: Camera configuration including pixel format, ROI, etc.
            hw_trigger_fn: Function to call for hardware triggering
            hw_set_strobe_delay_ms_fn: Function to set strobe delay for hardware triggering
        """
        super().__init__(camera_config, hw_trigger_fn, hw_set_strobe_delay_ms_fn)

        # Threading for frame reading
        self._read_thread_lock = threading.Lock()
        self._read_thread: Optional[threading.Thread] = None
        self._read_thread_keep_running = threading.Event()
        self._read_thread_keep_running.clear()
        self._read_thread_wait_period_s = 1.0
        self._read_thread_running = threading.Event()
        self._read_thread_running.clear()

        # Frame management
        self._frame_lock = threading.Lock()
        self._current_frame: Optional[CameraFrame] = None
        self._last_trigger_timestamp = 0
        self._trigger_sent = threading.Event()
        self._is_streaming = threading.Event()

        # Open camera
        self._camera = RetigaElectroCamera._open(
            sn=self._config.serial_number,
            index=None
        )

        # Query actual sensor dimensions from camera
        try:
            self._sensor_width, self._sensor_height = self._camera.shape(0)
        except:
            self._sensor_width = self.DEFAULT_WIDTH
            self._sensor_height = self.DEFAULT_HEIGHT
            self._log.warning(f"Could not query sensor size, using defaults: {self._sensor_width}x{self._sensor_height}")

        # Camera configuration
        self._exposure_time_ms = 20  # Default exposure time
        self._pixel_format = CameraPixelFormat.MONO14  # Default to 16-bit
        self._crop_roi = (0, 0, self._sensor_width, self._sensor_height)
        self._binning = (1, 1)  # Default no binning


        
        self._configure_camera()

    def _configure_camera(self):
        """Configure camera with default settings from config."""
        # Set exposure mode to 0 (Timed) early to prevent it from being reset by other operations
        # This will be set again later in _prepare_for_use() based on DEFAULT_TRIGGER_MODE,
        # but setting it here ensures it doesn't get changed to an unexpected value during configuration
        try:
            self._camera.exp_mode = 0  # 0 = Timed mode (used for CONTINUOUS and SOFTWARE_TRIGGER)
            self._log.info(f"Set exp_mode to 0 in _configure_camera: {self._camera.exp_mode}")
        except Exception as e:
            self._log.warning(f"Could not set exposure mode: {e}")
        
        # Set exposure resolution to milliseconds
        try:
            self._camera.exp_res = 0  # 0 = milliseconds
        except Exception as e:
            self._log.warning(f"Could not set exposure resolution: {e}")
        
        # Set speed table index (readout speed mode)
        try:
            self._camera.speed_table_index = 0
        except Exception as e:
            self._log.warning(f"Could not set speed table index: {e}")
        
        # Configure readout mode (if available)
        self._log.warning(f"Retiga Electro camera only supports global shutter mode, no alternative exp_out_mode available")
        
        # Set default ROI if specified in config
        if self._config.default_roi is not None:
            try:
                self.set_region_of_interest(*self._config.default_roi)
            except Exception as e:
                self._log.error(f"Failed to set default ROI: {e}")

        # Query the current shape after ROI setting
        try:
            current_shape = self._camera.shape(0)
            self._log.info(f"Camera shape after configuration: {current_shape}")
        except Exception as e:
            self._log.warning(f"Could not query camera shape: {e}")
        
        # Set pixel format
        self.set_pixel_format(self._config.default_pixel_format)
        
        # Set temperature if specified
        if self._config.default_temperature is not None:
            self.set_temperature(self._config.default_temperature)
        
        # Verify and reset exp_mode if it was changed by any of the above operations
        # Some PyVCAM operations (like set_roi) may reset exp_mode to a default value
        try:
            current_exp_mode = self._camera.exp_mode
            self.camera.exp_mode = 0
            if current_exp_mode != 0:
                self._log.warning(f"exp_mode was changed to {current_exp_mode} during configuration, resetting to 0")
                self._camera.exp_mode = 0
                # Verify it was set correctly
                if self._camera.exp_mode != 0:
                    self._log.error(f"Failed to reset exp_mode to 0, current value: {self._camera.exp_mode}")
        except Exception as e:
            self._log.warning(f"Could not verify/reset exp_mode: {e}")
        
        # Calculate strobe delay
        self._calculate_strobe_delay()

    def start_streaming(self):
        """Start continuous frame acquisition."""
        if self._is_streaming.is_set():
            self._log.debug("Already streaming, start_streaming is noop")
            return

        try:
            self._camera.start_live()
            self._ensure_read_thread_running()
            self._trigger_sent.clear()
            self._is_streaming.set()
            self._log.info("Retiga Electro camera started streaming")
        except Exception as e:
            raise CameraError(f"Failed to start streaming: {e}")

    def stop_streaming(self):
        """Stop frame acquisition."""
        if not self._is_streaming.is_set():
            self._log.debug("Already stopped, stop_streaming is noop")
            return

        try:
            self._cleanup_read_thread()
            self._camera.finish()
            self._trigger_sent.clear()
            self._is_streaming.clear()
            self._log.info("Retiga Electro camera streaming stopped")
        except Exception as e:
            raise CameraError(f"Failed to stop streaming: {e}")

    def get_is_streaming(self):
        """Check if camera is currently streaming."""
        return self._is_streaming.is_set()

    def close(self):
        """Close camera and release resources."""
        try:
            if self._is_streaming.is_set():
                self.stop_streaming()
            self._camera.close()
        except Exception as e:
            self._log.warning(f"Error closing camera: {e}")
        finally:
            try:
                pvc.uninit_pvcam()
            except:
                pass

    def _ensure_read_thread_running(self):
        """Start the frame reading thread if not already running."""
        with self._read_thread_lock:
            if self._read_thread is not None and self._read_thread_running.is_set():
                self._log.debug("Read thread exists and is running.")
                return True

            elif self._read_thread is not None:
                self._log.warning("Read thread exists but not running. Attempting restart.")

            self._read_thread = threading.Thread(target=self._wait_for_frame, daemon=True)
            self._read_thread_keep_running.set()
            self._read_thread.start()

    def _cleanup_read_thread(self):
        """Stop and clean up the frame reading thread."""
        self._log.debug("Cleaning up read thread.")
        with self._read_thread_lock:
            if self._read_thread is None:
                self._log.warning("No read thread to clean up.")
                return True

            self._read_thread_keep_running.clear()

            try:
                self._camera.abort()
            except Exception as e:
                self._log.warning(f"Failed to abort camera: {e}")

            self._read_thread.join(1.1 * self._read_thread_wait_period_s)

            if self._read_thread.is_alive():
                self._log.warning("Read thread refused to exit!")

            self._read_thread = None
            self._read_thread_running.clear()

    def _wait_for_frame(self):
        """Thread function to wait for and process frames."""
        self._log.info("Starting Retiga Electro read thread.")
        self._read_thread_running.set()

        while self._read_thread_keep_running.is_set():
            try:
                wait_time = int(self._read_thread_wait_period_s * 1000)
                frame, _, _ = self._camera.poll_frame(timeout_ms=wait_time)
                if frame is None:
                    time.sleep(0.001)
                    continue

                raw_data = frame["pixel_data"]
                processed_frame = self._process_raw_frame(raw_data)

                with self._frame_lock:
                    camera_frame = CameraFrame(
                        frame_id=self._current_frame.frame_id + 1 if self._current_frame else 1,
                        timestamp=time.time(),
                        frame=processed_frame,
                        frame_format=self.get_frame_format(),
                        frame_pixel_format=self.get_pixel_format(),
                    )
                    self._current_frame = camera_frame

                self._propogate_frame(camera_frame)
                self._trigger_sent.clear()

                time.sleep(0.001)

            except Exception as e:
                self._log.debug(f"Exception in read loop: {e}, continuing...")
                time.sleep(0.001)

        self._read_thread_running.clear()

    def read_camera_frame(self) -> Optional[CameraFrame]:
        """Read the most recent camera frame."""
        if not self.get_is_streaming():
            self._log.error("Cannot read camera frame when not streaming.")
            return None

        if not self._read_thread_running.is_set():
            self._log.error("Fatal camera error: read thread not running!")
            return None

        starting_id = self.get_frame_id()
        timeout_s = (1.04 * self.get_total_frame_time() + 1000) / 1000.0
        timeout_time_s = time.time() + timeout_s

        while self.get_frame_id() == starting_id:
            if time.time() > timeout_time_s:
                self._log.warning(
                    f"Timed out after waiting {timeout_s=}[s] for frame ({starting_id=}), total_frame_time={self.get_total_frame_time()}."
                )
                return None
            time.sleep(0.001)

        with self._frame_lock:
            return self._current_frame

    def get_frame_id(self) -> int:
        """Get the ID of the current frame."""
        with self._frame_lock:
            return self._current_frame.frame_id if self._current_frame else -1

    def set_exposure_time(self, exposure_time_ms: float):
        """Set the exposure time in milliseconds."""
        if exposure_time_ms == self._exposure_time_ms:
            return
        self._set_exposure_time_imp(exposure_time_ms)

    def _set_exposure_time_imp(self, exposure_time_ms: float):
        """Internal implementation for setting exposure time."""
        if self.get_acquisition_mode() == CameraAcquisitionMode.HARDWARE_TRIGGER:
            strobe_time_ms = self.get_strobe_time()
            adjusted_exposure_time = exposure_time_ms + strobe_time_ms
            if self._hw_set_strobe_delay_ms_fn:
                self._log.debug(f"Setting hw strobe time to {strobe_time_ms} [ms]")
                self._hw_set_strobe_delay_ms_fn(strobe_time_ms)
        else:
            adjusted_exposure_time = exposure_time_ms

        with self._pause_streaming():
            try:
                self._camera.exp_time = int(adjusted_exposure_time)
                self._exposure_time_ms = exposure_time_ms
                self._trigger_sent.clear()
            except Exception as e:
                raise CameraError(f"Failed to set exposure time: {e}")

    def get_exposure_time(self) -> float:
        """Get the current exposure time in milliseconds."""
        return self._exposure_time_ms

    def get_exposure_limits(self) -> Tuple[float, float]:
        """Get the valid range of exposure times in milliseconds."""
        # Retiga Electro typical limits
        return 0.01, 60000.0  # 10 µs to 60 seconds

    def get_strobe_time(self) -> float:
        """Get the strobe delay time in milliseconds."""
        return self._strobe_delay_ms

    def set_frame_format(self, frame_format: CameraFrameFormat):
        """Set the frame format (only RAW is supported)."""
        if frame_format != CameraFrameFormat.RAW:
            raise ValueError("Only RAW frame format is supported by Retiga Electro.")

    def get_frame_format(self) -> CameraFrameFormat:
        """Get the current frame format."""
        return CameraFrameFormat.RAW

    def set_pixel_format(self, pixel_format: CameraPixelFormat):
        """
        Set the pixel format/bit depth.
        
        Retiga Electro supports different readout modes through port selection:
        - Port 0: High sensitivity (12-bit)
        - Port 1: High speed (8-bit or 12-bit depending on mode)
        - Port 2: High dynamic range (16-bit)
        """
        with self._pause_streaming():
            try:
                if pixel_format != CameraPixelFormat.MONO14:
                    raise ValueError(f"Unsupported pixel format: {pixel_format}")

                self._pixel_format = pixel_format
                self._calculate_strobe_delay()

            except Exception as e:
                raise CameraError(f"Failed to set pixel format: {e}")

    def get_pixel_format(self) -> CameraPixelFormat:
        """Get the current pixel format."""
        return self._pixel_format

    def get_available_pixel_formats(self) -> Sequence[CameraPixelFormat]:
        """Get the list of supported pixel formats."""
        return [CameraPixelFormat.MONO14]

    def set_binning(self, binning_factor_x: int, binning_factor_y: int):
        """
        Set hardware binning.
        
        Retiga Electro supports binning modes (check camera for specific options).
        """
        with self._pause_streaming():
            try:
                if binning_factor_x != binning_factor_y:
                    raise ValueError("Retiga Electro only supports symmetric binning (x == y)")
                
                # PVCAM binning is set through the ROI with bin factors
                # Update internal binning state
                self._binning = (binning_factor_x, binning_factor_y)
                
                # Re-apply ROI with new binning
                offset_x, offset_y, width, height = self._crop_roi
                self._camera.set_roi(offset_x, offset_y, width, height)
                
                self._calculate_strobe_delay()
                
            except Exception as e:
                raise CameraError(f"Failed to set binning: {e}")

    def get_binning(self) -> Tuple[int, int]:
        """Get the current binning factors."""
        return self._binning

    def get_binning_options(self) -> Sequence[Tuple[int, int]]:
        """Get available binning options."""
        # Typical binning options for Retiga Electro
        return [(1, 1), (2, 2), (4, 4)]

    def get_resolution(self) -> Tuple[int, int]:
        """Get the sensor resolution (considering binning)."""
        return (
            self._sensor_width // self._binning[0],
            self._sensor_height // self._binning[1]
        )

    def get_pixel_size_unbinned_um(self) -> float:
        """Get the unbinned pixel size in micrometers."""
        return self.PIXEL_SIZE_UM_STANDARD

    def get_pixel_size_binned_um(self) -> float:
        """Get the effective pixel size after binning in micrometers."""
        return self.PIXEL_SIZE_UM_STANDARD * self._binning[0]

    def set_analog_gain(self, analog_gain: float):
        """Analog gain is not user-adjustable on Retiga Electro."""
        raise NotImplementedError("Analog gain is not adjustable on Retiga Electro. Use readout modes instead.")

    def get_analog_gain(self) -> float:
        """Analog gain is not user-adjustable on Retiga Electro."""
        raise NotImplementedError("Analog gain is not adjustable on Retiga Electro.")

    def get_gain_range(self) -> CameraGainRange:
        """Analog gain is not user-adjustable on Retiga Electro."""
        raise NotImplementedError("Analog gain is not adjustable on Retiga Electro.")

    def get_white_balance_gains(self) -> Tuple[float, float, float]:
        """White balance is not supported (monochrome camera)."""
        raise NotImplementedError("White balance gains are not supported on Retiga Electro (monochrome camera).")

    def set_white_balance_gains(self, red_gain: float, green_gain: float, blue_gain: float):
        """White balance is not supported (monochrome camera)."""
        raise NotImplementedError("White balance gains are not supported on Retiga Electro (monochrome camera).")

    def set_auto_white_balance_gains(self, on: bool):
        """White balance is not supported (monochrome camera)."""
        raise NotImplementedError("Auto white balance is not supported on Retiga Electro (monochrome camera).")

    def set_black_level(self, black_level: float):
        """Black level adjustment is not available through this interface."""
        raise NotImplementedError("Black level adjustment is not supported on Retiga Electro.")

    def get_black_level(self) -> float:
        """Black level adjustment is not available through this interface."""
        raise NotImplementedError("Black level adjustment is not supported on Retiga Electro.")

    def _set_acquisition_mode_imp(self, acquisition_mode: CameraAcquisitionMode):
        """Set the acquisition/trigger mode."""
        with self._pause_streaming():
            try:
                if acquisition_mode == CameraAcquisitionMode.CONTINUOUS:
                    self._camera.exp_mode = 0
                elif acquisition_mode == CameraAcquisitionMode.HARDWARE_TRIGGER:
                    self._camera.exp_mode = 1
                elif acquisition_mode == CameraAcquisitionMode.BULB:
                    self._camera.exp_mode = 2
                elif acquisition_mode == CameraAcquisitionMode.HARDWARE_TRIGGER_FIRST:
                    self._camera.exp_mode = 3
                elif acquisition_mode == CameraAcquisitionMode.VARIABLE_TIMED:
                    self._camera.exp_mode = 5
                else:
                    raise ValueError(f"Unsupported acquisition mode: {acquisition_mode}")

                self._acquisition_mode = acquisition_mode
                self._set_exposure_time_imp(self._exposure_time_ms)

            except Exception as e:
                raise CameraError(f"Failed to set acquisition mode: {e}")

    # Trigger mode code mapping for Retiga Electro
    _TRIGGER_CODE_MAPPING = {
        0: "Timed",
        1: "Strobed",
        2: "Bulb",
        3: "Trigger First",
        5: "Variable Timed"
    }

    def get_acquisition_mode(self) -> CameraAcquisitionMode:
        """Get the current acquisition/trigger mode."""
        try:
            exp_mode_code = self._camera.exp_mode
            exp_mode_name = self._TRIGGER_CODE_MAPPING.get(exp_mode_code, str(exp_mode_code))
        except:
            # If we can't read the mode, return the cached value
            return getattr(self, '_acquisition_mode', CameraAcquisitionMode.CONTINUOUS)
        
        # For exp_mode 0 (Timed), we need to check the cached value to distinguish
        # between CONTINUOUS and SOFTWARE_TRIGGER since they both use exp_mode 0
        if exp_mode_name == "Timed":
            return CameraAcquisitionMode.CONTINUOUS
        elif exp_mode_name == "Strobed":
            return CameraAcquisitionMode.HARDWARE_TRIGGER
        elif exp_mode_name == "Bulb":
            return CameraAcquisitionMode.BULB
        elif exp_mode_name == "Trigger First":
            return CameraAcquisitionMode.HARDWARE_TRIGGER_FIRST
        elif exp_mode_name == "Variable Timed":
            return CameraAcquisitionMode.VARIABLE_TIMED
        else:
            self._log.warning(f"Unknown acquisition mode: {exp_mode_name}")
            return getattr(self, '_acquisition_mode', CameraAcquisitionMode.CONTINUOUS)

    def send_trigger(self, illumination_time: Optional[float] = None):
        """Send a trigger to capture a frame."""
        if self.get_acquisition_mode() == CameraAcquisitionMode.HARDWARE_TRIGGER and not self._hw_trigger_fn:
            raise CameraError("In HARDWARE_TRIGGER mode, but no hw trigger function given.")

        if not self.get_is_streaming():
            raise CameraError("Camera is not streaming, cannot send trigger.")

        if not self.get_ready_for_trigger():
            raise CameraError(
                f"Requested trigger too early (last trigger was {time.time() - self._last_trigger_timestamp} [s] ago), refusing."
            )

        if self.get_acquisition_mode() == CameraAcquisitionMode.HARDWARE_TRIGGER:
            self._hw_trigger_fn(illumination_time)
        elif self.get_acquisition_mode() == CameraAcquisitionMode.SOFTWARE_TRIGGER:
            try:
                self._camera.sw_trigger()
                self._last_trigger_timestamp = time.time()
                self._trigger_sent.set()
            except Exception as e:
                raise CameraError(f"Failed to send software trigger: {e}")

    def get_ready_for_trigger(self) -> bool:
        """Check if camera is ready for another trigger."""
        if time.time() - self._last_trigger_timestamp > 1.5 * ((self.get_total_frame_time() + 4) / 1000.0):
            self._trigger_sent.clear()
        return not self._trigger_sent.is_set()

    def set_region_of_interest(self, offset_x: int, offset_y: int, width: int, height: int):
        """Set the region of interest for image capture."""
        with self._pause_streaming():
            try:
                self._camera.set_roi(offset_x, offset_y, width, height)
                self._crop_roi = (offset_x, offset_y, width, height)
                self._calculate_strobe_delay()
            except Exception as e:
                raise CameraError(f"Failed to set ROI: {e}")

    def get_region_of_interest(self) -> Tuple[int, int, int, int]:
        """Get the current region of interest."""
        return self._crop_roi

    def set_temperature(self, temperature_deg_c: Optional[float]):
        """
        Set the target temperature for camera cooling.
        
        Retiga Electro supports thermoelectric cooling.
        Temperature range is typically -20°C to +20°C.
        """
        with self._pause_streaming():
            try:
                if temperature_deg_c is None:
                    temperature_deg_c = 0  # Default to 0°C
                    
                if temperature_deg_c < self.TEMP_MIN_C or temperature_deg_c > self.TEMP_MAX_C:
                    raise ValueError(
                        f"Temperature must be between {self.TEMP_MIN_C} and {self.TEMP_MAX_C} C, got {temperature_deg_c} C"
                    )
                self._camera.temp_setpoint = int(temperature_deg_c * 100)  # PVCAM uses centi-degrees
            except Exception as e:
                raise CameraError(f"Failed to set temperature: {e}")

    def get_temperature(self) -> float:
        """Get the current sensor temperature in Celsius."""
        with self._pause_streaming():
            try:
                # PVCAM returns temperature in centi-degrees
                return self._camera.temp / 100.0
            except Exception as e:
                raise CameraError(f"Failed to get temperature: {e}")

    def set_temperature_reading_callback(self, callback: Callable):
        """Temperature reading callback is not supported."""
        raise NotImplementedError("Temperature reading callback is not supported by this camera.")

    def _calculate_strobe_delay(self):
        """
        Retiga Electro is global shutter camera, so no strobe delay is needed.
        """
        self._strobe_delay_ms = 0.0
