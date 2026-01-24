"""
Photometrics Camera Driver

Supports:
- Kinetix 22 (3200x3200, 6.5µm pixels)
- Prime BSI Express (2048x2048, 6.5µm pixels)

Both cameras use the PVCAM SDK (pyvcam) and share the same API interface
but have different specifications for resolution, readout modes, and timing.
"""

from pyvcam import pvc
from pyvcam.camera import Camera as PVCam
from typing import Callable, Optional, Tuple, Sequence, Dict, List
from dataclasses import dataclass, field
from enum import Enum
import numpy as np
import threading
import time

import squid.logging
from squid.config import CameraConfig, CameraPixelFormat, PhotometricsCameraModel, CameraReadoutMode
from squid.abc import (
    AbstractCamera,
    CameraAcquisitionMode,
    CameraFrameFormat,
    CameraFrame,
    CameraGainRange,
    CameraError,
)
from control._def import *


# ============================================================================
# Camera Specification Data Structures
# ============================================================================

@dataclass
class CameraModeSpec:
    """Specification for a single camera readout mode."""
    name: str
    bit_depth: int
    port_value: int
    speed_index: int = 0
    gain_index: int = 1
    line_time_us: float = 0.0  # Microseconds per line for strobe calculation
    max_fps_full_frame: float = 0.0
    read_noise_electrons: float = 0.0
    full_well_electrons: int = 0


@dataclass
class PhotometricsCameraSpec:
    """Model-specific specifications for Photometrics cameras."""
    name: str
    model_id: str
    resolution: Tuple[int, int]  # (width, height)
    pixel_size_um: float
    temperature_range: Tuple[float, float]  # (min, max) in Celsius
    camera_modes: Dict[str, CameraModeSpec]  # mode_name -> spec
    default_mode: str
    supports_binning: bool = False
    binning_options: List[Tuple[int, int]] = field(default_factory=lambda: [(1, 1)])


# ============================================================================
# Camera Specifications
# ============================================================================

KINETIX_SPEC = PhotometricsCameraSpec(
    name="Kinetix",
    model_id="KINETIX",
    resolution=(3200, 3200),
    pixel_size_um=6.5,
    temperature_range=(-15.0, 15.0),
    default_mode="dynamic_range_16bit",
    camera_modes={
        "sensitivity_12bit": CameraModeSpec(
            name="Sensitivity",
            bit_depth=12,
            port_value=0,
            speed_index=0,
            gain_index=1,
            line_time_us=3.53125,
            max_fps_full_frame=88,
            read_noise_electrons=1.0,
            full_well_electrons=6500,
        ),
        "speed_8bit": CameraModeSpec(
            name="Speed",
            bit_depth=8,
            port_value=1,
            speed_index=0,
            gain_index=1,
            line_time_us=0.625,
            max_fps_full_frame=500,
            read_noise_electrons=1.6,
            full_well_electrons=10000,
        ),
        "dynamic_range_16bit": CameraModeSpec(
            name="Dynamic Range",
            bit_depth=16,
            port_value=2,
            speed_index=0,
            gain_index=1,
            line_time_us=3.75,
            max_fps_full_frame=82,
            read_noise_electrons=1.6,
            full_well_electrons=45000,
        ),
        "sub_electron_16bit": CameraModeSpec(
            name="Sub-Electron",
            bit_depth=16,
            port_value=3,
            speed_index=0,
            gain_index=1,
            line_time_us=60.1,
            max_fps_full_frame=5,
            read_noise_electrons=0.7,
            full_well_electrons=1600,
        ),
    },
)

KINETIX_22_SPEC = PhotometricsCameraSpec(
    name="Kinetix 22",
    model_id="KINETIX_22",
    resolution=(2400, 2400),
    pixel_size_um=6.5,
    temperature_range=(-15.0, 15.0),
    default_mode="dynamic_range_16bit",
    camera_modes={
        "sensitivity_12bit": CameraModeSpec(
            name="Sensitivity",
            bit_depth=12,
            port_value=0,
            speed_index=0,
            gain_index=1,
            line_time_us=3.53125,
            max_fps_full_frame=88,
            read_noise_electrons=1.0,
            full_well_electrons=6500,
        ),
        "speed_8bit": CameraModeSpec(
            name="Speed",
            bit_depth=8,
            port_value=1,
            speed_index=0,
            gain_index=1,
            line_time_us=0.625,
            max_fps_full_frame=500,
            read_noise_electrons=1.6,
            full_well_electrons=10000,
        ),
        "dynamic_range_16bit": CameraModeSpec(
            name="Dynamic Range",
            bit_depth=16,
            port_value=2,
            speed_index=0,
            gain_index=1,
            line_time_us=3.75,
            max_fps_full_frame=82,
            read_noise_electrons=1.6,
            full_well_electrons=45000,
        ),
        "sub_electron_16bit": CameraModeSpec(
            name="Sub-Electron",
            bit_depth=16,
            port_value=3,
            speed_index=0,
            gain_index=1,
            line_time_us=60.1,
            max_fps_full_frame=5,
            read_noise_electrons=0.7,
            full_well_electrons=1600,
        ),
    },
)

PRIME_BSI_EXPRESS_SPEC = PhotometricsCameraSpec(
    name="Prime BSI Express",
    model_id="PRIME_BSI_EXPRESS",
    resolution=(2048, 2048),
    pixel_size_um=6.5,
    temperature_range=(-20.0, 20.0),
    default_mode="hdr_16bit",
    camera_modes={
        # HDR 16-bit mode - maximum dynamic range
        "hdr_16bit": CameraModeSpec(
            name="HDR (Dynamic Range)",
            bit_depth=16,
            port_value=0,  # Verify from camera's port_speed_gain_table
            speed_index=1,
            gain_index=1,
            line_time_us=11.4,  # 100 MHz clock
            max_fps_full_frame=43.5,
            read_noise_electrons=1.6,
            full_well_electrons=45000,
        ),
        # CMS 12-bit mode - lowest noise, correlated multi-sampling
        "cms_12bit": CameraModeSpec(
            name="CMS (Correlated Multi-Sampling)",
            bit_depth=12,
            port_value=0,  # Verify from camera's port_speed_gain_table
            speed_index=1,
            gain_index=2,
            line_time_us=11.4,  # 100 MHz clock
            max_fps_full_frame=43.5,
            read_noise_electrons=1.0,
            full_well_electrons=1000,
        ),
        # Speed 11-bit mode - high frame rate
        "fullwell_11bit": CameraModeSpec(
            name="Full-Well",
            bit_depth=11,
            port_value=0,  # Verify from camera's port_speed_gain_table
            speed_index=0,
            gain_index=1,
            line_time_us=5.14,  # 200 MHz clock
            max_fps_full_frame=94.5,
            read_noise_electrons=1.6,
            full_well_electrons=10000,
        ),
        # Sensitivity 11-bit mode - high sensitivity variant
        "balanced_11bit": CameraModeSpec(
            name="Balanced",
            bit_depth=11,
            port_value=0,  # Same port as speed, different gain
            speed_index=0,
            gain_index=2,
            line_time_us=5.14,  # 200 MHz clock
            max_fps_full_frame=94.5,
            read_noise_electrons=1.6,
            full_well_electrons=5000,
        ),
        # Full-Well 11-bit mode - maximum well depth
        "sensitivity_11bit": CameraModeSpec(
            name="Sensitivity",
            bit_depth=11,
            port_value=0,  # Same port as speed, different gain
            speed_index=0,
            gain_index=3,
            line_time_us=5.14,  # 200 MHz clock
            max_fps_full_frame=94.5,
            read_noise_electrons=1.6,
            full_well_electrons=2500
        ),
    },
)

# Lookup table by model ID
PHOTOMETRICS_CAMERA_SPECS: Dict[str, PhotometricsCameraSpec] = {
    "KINETIX": KINETIX_SPEC,
    "KINETIX_22": KINETIX_22_SPEC,
    "PRIME_BSI_EXPRESS": PRIME_BSI_EXPRESS_SPEC
}


# ============================================================================
# PhotometricsCamera Class
# ============================================================================

class PhotometricsCamera(AbstractCamera):
    """
    Unified driver for Photometrics Prime BSI Express and Kinetix 22 cameras.
    
    This class auto-detects the camera model and loads appropriate specifications.
    Both cameras use the PVCAM SDK and share the same triggering interface.
    """
    
    # Trigger code mapping (common to all Photometrics cameras)
    _EXP_CODE_MAPPING = {
        1792: "Internal Trigger",
        2304: "Edge Trigger",
        2048: "Trigger First",
        2560: "Level Trigger",
        3328: "Level Trigger Overlap",
        3072: "Software Trigger Edge",
        2816: "Software Trigger First",
        "Internal Trigger": 1792,
        "Edge Trigger": 2304,
        "Trigger First": 2048,
        "Level Trigger": 2560,
        "Level Trigger Overlap": 3328,
        "Software Trigger Edge": 3072,
        "Software Trigger First": 2816,
    }

    @staticmethod
    def _open(index: Optional[int] = None) -> PVCam:
        """Open a Photometrics camera and return the camera object."""
        log = squid.logging.get_logger("PhotometricsCamera._open")

        pvc.init_pvcam()

        try:
            if index is not None:
                cameras = list(PVCam.detect_camera())
                if index >= len(cameras):
                    raise CameraError(f"Camera index {index} out of range. Found {len(cameras)} cameras.")
                cam = cameras[index]
            else:
                cam = next(PVCam.detect_camera())

            cam.open()
            log.info("Photometrics camera opened successfully")
            return cam

        except Exception as e:
            pvc.uninit_pvcam()
            raise CameraError(f"Failed to open Photometrics camera: {e}")

    def __init__(
        self,
        camera_config: CameraConfig,
        hw_trigger_fn: Optional[Callable[[Optional[float]], bool]],
        hw_set_strobe_delay_ms_fn: Optional[Callable[[float], bool]],
    ):
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

        # Fast acquisition support
        self._fast_acquisition_callback: Optional[Callable[[np.ndarray], None]] = None
        self._fast_acquisition_thread: Optional[threading.Thread] = None
        self._fast_acquisition_thread_keep_running = threading.Event()
        self.fast_acquisition_timeout_ms: Optional[int] = None

        # Open camera
        self._camera = PhotometricsCamera._open()
        self._query_port_speed_gain_table()

        # Set exposure time resolution to maximum
        self._camera.exp_res = max(self._camera.exp_resolutions.values())

        # Detect camera model and load specifications
        self._spec = self._detect_and_load_spec()
        self._log.info(f"Detected camera: {self._spec.name} ({self._spec.model_id})")
        self._log.info(f"Resolution: {self._spec.resolution}, Pixel size: {self._spec.pixel_size_um}µm")

        # Initialize state
        self._exposure_time_ms = 20.0
        self._pixel_format = CameraPixelFormat.MONO16
        self._current_camera_mode: Optional[CameraModeSpec] = None
        
        # Initialize ROI to full frame based on detected model
        self._crop_roi = (0, 0, *self._spec.resolution)

        # Configure camera
        self._configure_camera()

    def _detect_and_load_spec(self) -> PhotometricsCameraSpec:
        """Detect camera model and return appropriate specification."""
        # First check if model specified in config
        if self._config.camera_model is not None:
            model_str = (
                self._config.camera_model.value 
                if hasattr(self._config.camera_model, 'value') 
                else str(self._config.camera_model)
            ).upper()
            
            if model_str in PHOTOMETRICS_CAMERA_SPECS:
                self._log.info(f"Using camera model from config: {model_str}")
                return PHOTOMETRICS_CAMERA_SPECS[model_str]
        
        # Auto-detect from camera properties
        try:
            sensor_size = self._camera.sensor_size  # (width, height)
            self._log.info(f"Auto-detecting camera: sensor_size={sensor_size}")
            
            # Detection based on sensor resolution
            if sensor_size[0] == 2048 and sensor_size[1] == 2048:
                self._log.info("Detected Prime BSI Express (2048x2048)")
                return PRIME_BSI_EXPRESS_SPEC
            elif sensor_size[0] == 2400 and sensor_size[1] == 2400:
                self._log.info("Detected Kinetix 22 (2400x2400)")
                return KINETIX_22_SPEC
            elif sensor_size[0] == 3200 and sensor_size[1] == 3200:
                self._log.info("Detected Kinetix (3200x3200)")
                return KINETIX_SPEC
            
            # Try chip name detection
            try:
                chip_name = self._camera.chip_name.upper()
                self._log.info(f"Camera chip name: {chip_name}")
                if "2020" in chip_name or "BSI" in chip_name:
                    return PRIME_BSI_EXPRESS_SPEC
            except Exception:
                pass
                
        except Exception as e:
            self._log.warning(f"Auto-detection failed: {e}, defaulting to Kinetix")
        
        # Default fallback
        return KINETIX_22_SPEC

    def _query_port_speed_gain_table(self) -> Dict:
        """
        Query the camera's port_speed_gain_table to verify port mappings.
        
        Useful for debugging and verifying the spec's port_value assignments.
        """
        # TODO: need to confirm if we can get temperature during live mode
        # Temperature monitoring
        # self.temperature_reading_callback = None
        # self._terminate_temperature_event = threading.Event()
        # self.temperature_reading_thread = threading.Thread(target=self._check_temperature, daemon=True)
        # self.temperature_reading_thread.start()
        try:
            table = self._camera.port_speed_gain_table
            self._log.debug(f"Camera port_speed_gain_table: {table}")
            return table
        except Exception as e:
            self._log.warning(f"Could not query port_speed_gain_table: {e}")
            return {}

    def _configure_camera(self):
        """Configure camera with default settings."""
        self._camera.speed_table_index = 0
        self._camera.exp_out_mode = 0  # Row first mode
        self._camera.metadata_enabled = True # enable metadata

        
        # Set default ROI from config or use full sensor
        try:
            if self._config.default_roi and all(v is not None for v in self._config.default_roi):
                self._log.info(f"Setting default ROI: {self._config.default_roi}")
                self.set_region_of_interest(*self._config.default_roi)
            else:
                # Use full sensor as default
                self._log.info(f"Setting default ROI to full sensor: {self._spec.resolution}")
                self.set_region_of_interest(0, 0, *self._spec.resolution)
        except Exception as e:
            self._log.error(f"Failed to set crop ROI: {e}")
        
        self._log.info(f"Cropped area: {self._camera.shape(0)}")
        
        # Set default readout mode based on spec
        self._set_camera_mode_by_spec(self._spec.camera_modes[self._spec.default_mode])
        
        # Set pixel format from config (for backward compatibility)
        if self._config.default_pixel_format:
            self.set_pixel_format(self._config.default_pixel_format)
        
        # Set temperature
        if self._config.default_temperature is not None:
            self.set_temperature(self._config.default_temperature)
        
        self._calculate_strobe_delay()
        
        # Log available readout modes
        self._log.info(f"Available camera modes for {self._spec.name}: {list(self._spec.camera_modes.keys())}")

    # =========================================================================
    # Readout Mode API (Camera-Specific)
    # =========================================================================

    def set_camera_mode(self, mode_name: str):
        """
        Set the camera readout mode by name (Photometrics-specific).
        
        This provides full control over all available readout modes for Photometrics cameras.
        For the standard AbstractCamera interface, use set_camera_mode() with CameraReadoutMode.
        
        Args:
            mode_name: One of the readout mode names defined in the camera spec.
                       For Kinetix: sensitivity_12bit, speed_8bit, dynamic_range_16bit, sub_electron_16bit
                       For BSI Express: hdr_16bit, cms_12bit, fullwell_11bit, balanced_11bit, sensitivity_11bit
        """
        if mode_name not in self._spec.camera_modes:
            available = list(self._spec.camera_modes.keys())
            raise ValueError(
                f"Unknown readout mode '{mode_name}' for {self._spec.name}. "
                f"Available modes: {available}"
            )
        
        mode_spec = self._spec.camera_modes[mode_name]
        self._set_camera_mode_by_spec(mode_spec)
        self._log.info(f"Set camera mode to '{mode_spec.name}' ({mode_spec.bit_depth}-bit)")

    def _set_camera_mode_by_spec(self, mode_spec: CameraModeSpec):
        """Internal method to set readout mode from a CameraModeSpec."""
        with self._pause_streaming():
            try:
                # Set readout port
                self._camera.readout_port = mode_spec.port_value
                
                # Set speed table index
                self._camera.speed_table_index = mode_spec.speed_index
                
                # Set gain if the SDK supports it
                try:
                    self._camera.gain = mode_spec.gain_index
                except (AttributeError, Exception) as e:
                    self._log.debug(f"Could not set gain index: {e}")
                
                self._current_camera_mode = mode_spec
                
                # Update pixel format based on bit depth
                if mode_spec.bit_depth <= 8:
                    self._pixel_format = CameraPixelFormat.MONO8
                elif mode_spec.bit_depth <= 12: # TBD: check significance of pixel format
                    self._pixel_format = CameraPixelFormat.MONO12
                else:
                    self._pixel_format = CameraPixelFormat.MONO16
                
                self._calculate_strobe_delay()
                
            except Exception as e:
                raise CameraError(f"Failed to set readout mode: {e}")

    def get_camera_mode(self) -> Optional[str]:
        """Get the current readout mode name (Photometrics-specific)."""
        if self._current_camera_mode:
            for name, spec in self._spec.camera_modes.items():
                if spec == self._current_camera_mode:
                    return name
        return None

    def get_available_camera_modes(self) -> List[str]:
        """Get list of available readout mode names for this camera (Photometrics-specific)."""
        return list(self._spec.camera_modes.keys())

    def get_camera_mode_spec(self, mode_name: str) -> Optional[CameraModeSpec]:
        """Get the specification for a readout mode."""
        return self._spec.camera_modes.get(mode_name)

    # =========================================================================
    # Streaming Control
    # =========================================================================

    def start_streaming(self):
        if self._is_streaming.is_set():
            self._log.debug("Already streaming, start_streaming is noop")
            return

        try:
            self._camera.start_live()
            self._ensure_read_thread_running()
            self._trigger_sent.clear()
            self._is_streaming.set()
            self._log.info(f"{self._spec.name} camera starts streaming")
        except Exception as e:
            raise CameraError(f"Failed to start streaming: {e}")

    def stop_streaming(self):
        if not self._is_streaming.is_set():
            self._log.debug("Already stopped, stop_streaming is noop")
            return

        try:
            self._cleanup_read_thread()
            self._camera.finish()
            self._trigger_sent.clear()
            self._is_streaming.clear()
            self._log.info(f"{self._spec.name} camera streaming stopped")
        except Exception as e:
            raise CameraError(f"Failed to stop streaming: {e}")

    def get_is_streaming(self):
        return self._is_streaming.is_set()

    def close(self):
        try:
            # Stop fast acquisition if running
            self.stop_fast_acquisition_frame_grabbing()
            # Stop streaming
            if self.get_is_streaming():
                self.stop_streaming()
            self._camera.close()
        except Exception as e:
            raise CameraError(f"Failed to close camera: {e}")
        pvc.uninit_pvcam()

    # =========================================================================
    # Fast Acquisition Support
    # =========================================================================

    def start_fast_acquisition_frame_grabbing(
        self, 
        frame_rate_hz,
        frame_callback: Optional[Callable[[np.ndarray], None]] = None,
    ):
        """
        Start dedicated fast acquisition frame grabbing thread.
        
        This method should be called after:
        1. Setting camera to HARDWARE_TRIGGER or HARDWARE_TRIGGER_FIRST mode
        2. Before firing DAQ waveforms
        
        The frame grabbing thread continuously reads frames from the camera
        using poll_frame with minimal timeout for non-blocking operation.
        
        Args:
            frame_callback: Optional callback function receiving raw frame data (numpy array).
                           Frame IDs and timestamps will be determined from DAQ synchronization.
                           If None, frames are stored in self._current_frame only.
        """
        if self._is_streaming.is_set():
            self._log.warning("Camera is already streaming. Stop streaming before starting fast acquisition.")
            return
        
        # Check acquisition mode
        acquisition_mode = self.get_acquisition_mode()
        self._log.info(f"Starting fast acquisition with mode: {acquisition_mode} (code {self._camera.exp_mode}). Exposure signal mode: {self._camera.exp_out_mode}")
        
        if acquisition_mode not in [CameraAcquisitionMode.HARDWARE_TRIGGER, CameraAcquisitionMode.HARDWARE_TRIGGER_FIRST]:
            raise CameraError("Fast acquisition requires HARDWARE_TRIGGER or HARDWARE_TRIGGER_FIRST mode")
        
        # Start camera live mode
        try:
            if acquisition_mode == CameraAcquisitionMode.HARDWARE_TRIGGER_FIRST:
                self._camera.start_sequence(reset_frame_counter=True) # Implementation TBD
            else:
                self._camera.start_live(buffer_frame_count=int(frame_rate_hz*1.5), reset_frame_counter=True)
                self._log.info(f"{self._spec.name} acquisition started for fast mode with PVCam buffer size {int(frame_rate_hz*1.5)}")
        except Exception as e:
            raise CameraError(f"Failed to start camera acquisition: {e}")
        
        # Start dedicated frame grabbing thread
        self._fast_acquisition_callback = frame_callback
        self._fast_acquisition_thread_keep_running = threading.Event()
        self._fast_acquisition_thread_keep_running.set()
        
        self._fast_acquisition_thread = threading.Thread(
            target=self._grab_frames_fast_acquisition,
            daemon=True
        )
        self._fast_acquisition_thread.start()
        self._log.info("Fast acquisition frame grabbing thread started")

    def stop_fast_acquisition_frame_grabbing(self):
        """Stop the fast acquisition frame grabbing thread and end camera acquisition."""
        if not hasattr(self, '_fast_acquisition_thread') or self._fast_acquisition_thread is None:
            return
        
        self._log.info("Stopping fast acquisition frame grabbing...")
        
        # Signal thread to stop
        self._fast_acquisition_thread_keep_running.clear()
        
        # Abort to wake up poll_frame if blocking
        try:
            self._camera.finish()
        except Exception as e:
            self._log.warning(f"Failed to abort camera: {e}")
        
        # Wait for thread to finish
        if self._fast_acquisition_thread.is_alive():
            self._fast_acquisition_thread.join(timeout=2.0)
            if self._fast_acquisition_thread.is_alive():
                self._log.warning("Fast acquisition thread refused to exit!")
        
        # End acquisition
        try:
            self._camera.finish()
        except Exception as e:
            self._log.warning(f"Failed to finish acquisition: {e}")
        
        self._fast_acquisition_thread = None
        self._fast_acquisition_callback = None
        self._log.info("Fast acquisition frame grabbing stopped")

    def _grab_frames_fast_acquisition(self):
        """
        Dedicated thread function for fast acquisition frame grabbing.
        
        Uses poll_frame with minimal timeout for non-blocking operation.
        Frames are passed to the callback or stored in _current_frame.
        """
        self._log.info("Fast acquisition frame grabbing thread started")
        
        while self._fast_acquisition_thread_keep_running.is_set():
            try:
                # Get timeout - use fast_acquisition_timeout_ms if set, otherwise use a short default
                wait_time_ms = self.fast_acquisition_timeout_ms if self.fast_acquisition_timeout_ms else 100
                
                frame, _, _ = self._camera.poll_frame(timeout_ms=wait_time_ms)
                
                if frame is None:
                    continue
                
                raw_data = frame["pixel_data"]
                
                # Call callback if provided (for fast acquisition buffer)
                if self._fast_acquisition_callback is not None:
                    try:
                        self._fast_acquisition_callback(raw_data, frame["meta_data"])
                    except Exception as e:
                        self._log.error(f"Error in fast acquisition callback: {e}")
                
            except Exception as e:
                if self._fast_acquisition_thread_keep_running.is_set():
                    self._log.debug(f"Exception in fast acquisition loop: {e}")
        
        self._log.info("Fast acquisition frame grabbing thread stopped")

    # =========================================================================
    # Thread Management
    # =========================================================================

    def _ensure_read_thread_running(self):
        with self._read_thread_lock:
            if self._read_thread is not None and self._read_thread_running.is_set():
                self._log.debug("Read thread exists and is marked as running.")
                return True

            elif self._read_thread is not None:
                self._log.warning("Read thread already exists but not marked as running. Attempting start.")

            self._read_thread = threading.Thread(target=self._wait_for_frame, daemon=True)
            self._read_thread_keep_running.set()
            self._read_thread.start()

    def _cleanup_read_thread(self):
        self._log.debug("Cleaning up read thread.")
        with self._read_thread_lock:
            if self._read_thread is None:
                self._log.warning("No read thread, already not running?")
                return True

            self._read_thread_keep_running.clear()

            try:
                self._camera.abort()
            except Exception as e:
                self._log.warning(f"Failed to abort camera: {e}")

            self._read_thread.join(1.1 * self._read_thread_wait_period_s)

            success = not self._read_thread.is_alive()
            if not success:
                self._log.warning("Read thread refused to exit!")

            self._read_thread = None
            self._read_thread_running.clear()

    def _wait_for_frame(self):
        """Thread function to wait for and process frames."""
        self._log.info(f"Starting {self._spec.name} read thread.")
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
        with self._frame_lock:
            return self._current_frame.frame_id if self._current_frame else -1

    # =========================================================================
    # Exposure and Timing
    # =========================================================================

    def set_exposure_time(self, exposure_time_ms: float):
        # Kinetix camera set_exposure_time is slow, so we don't want to call it unnecessarily.
        if exposure_time_ms == self._exposure_time_ms:
            return
        self._set_exposure_time_imp(exposure_time_ms)

    def _set_exposure_time_imp(self, exposure_time_ms: float):
        # if self.get_acquisition_mode() == CameraAcquisitionMode.HARDWARE_TRIGGER: # TBD different exposure times for different triggering modes
        #     strobe_time_ms = self.get_strobe_time()
        #     adjusted_exposure_time = exposure_time_ms + strobe_time_ms
        #     if self._hw_set_strobe_delay_ms_fn:
        #         self._log.debug(f"Setting hw strobe time to {strobe_time_ms} [ms]")
        #         self._hw_set_strobe_delay_ms_fn(strobe_time_ms)
        # else:
        adjusted_exposure_time_ms = exposure_time_ms

        with self._pause_streaming():
            try:
                self._camera.exp_time = int(adjusted_exposure_time_ms*(1000**(self._camera.exp_res))) # Accounts for different exposure time resolutions which are encoded by an index (see PyVCAM documentation)
                self._exposure_time_ms = exposure_time_ms
                self._trigger_sent.clear()
            except Exception as e:
                raise CameraError(f"Failed to set exposure time: {e}")

    def get_exposure_time(self) -> float:
        return self._exposure_time_ms

    def get_exposure_limits(self) -> Tuple[float, float]:
        return 0.0, 10000.0 # TBD check if this is different

    def get_strobe_time(self) -> float:
        return self._strobe_delay_ms

    def _calculate_strobe_delay(self):
        """Calculate strobe delay based on current readout mode and ROI."""
        _, height = self._camera.shape(0)
        
        if self._current_camera_mode:
            line_time_us = self._current_camera_mode.line_time_us
        else:
            # Fallback to pixel format-based calculation (legacy)
            # These are Kinetix defaults for backward compatibility
            line_times = {
                CameraPixelFormat.MONO8: 0.625,
                CameraPixelFormat.MONO12: 3.53125,
                CameraPixelFormat.MONO16: 3.75,
            }
            line_time_us = line_times.get(self._pixel_format, 3.75)
        
        self._strobe_delay_ms = (line_time_us * height) / 1000.0
        self._log.info(f"Calculated strobe delay for {height} rows: {self._strobe_delay_ms} ms")
    
    def get_readout_time(self) -> float:
        """Get the readout time of the camera in milliseconds."""
        self._calculate_strobe_delay()
        return self._strobe_delay_ms

    # =========================================================================
    # Pixel Format
    # =========================================================================

    def set_frame_format(self, frame_format: CameraFrameFormat):
        if frame_format != CameraFrameFormat.RAW:
            raise ValueError("Only the RAW frame format is supported by this camera.")

    def get_frame_format(self) -> CameraFrameFormat:
        return CameraFrameFormat.RAW

    def set_pixel_format(self, pixel_format: CameraPixelFormat):
        """
        Set pixel format by adjusting the readout port.
        
        Note: For more control over readout parameters, use set_camera_mode() instead.
        This method provides backward compatibility with the pixel format API.
        """
        with self._pause_streaming():
            try:
                # Map pixel format to appropriate readout mode
                if self._spec.model_id in ("KINETIX", "KINETIX_22"):
                    # Kinetix mapping
                    if pixel_format == CameraPixelFormat.MONO8:
                        self.set_camera_mode("speed_8bit")
                    elif pixel_format == CameraPixelFormat.MONO12:
                        self.set_camera_mode("sensitivity_12bit")
                    elif pixel_format == CameraPixelFormat.MONO16:
                        self.set_camera_mode("dynamic_range_16bit")
                    else:
                        raise ValueError(f"Unsupported pixel format: {pixel_format}")
                else:
                    # Prime BSI Express mapping
                    if pixel_format == CameraPixelFormat.MONO8:
                        # No 8-bit mode on BSI Express, use full well 11-bit
                        self.set_camera_mode("fullwell_11bit")
                    elif pixel_format == CameraPixelFormat.MONO12:
                        self.set_camera_mode("cms_12bit")
                    elif pixel_format == CameraPixelFormat.MONO16:
                        self.set_camera_mode("hdr_16bit")
                    else:
                        raise ValueError(f"Unsupported pixel format: {pixel_format}")

                self._pixel_format = pixel_format
                self._calculate_strobe_delay()

            except Exception as e:
                raise CameraError(f"Failed to set pixel format: {e}")

    def get_pixel_format(self) -> CameraPixelFormat:
        return self._pixel_format

    def get_available_pixel_formats(self) -> Sequence[CameraPixelFormat]:
        if self._spec.model_id in ("KINETIX", "KINETIX_22"):
            return [CameraPixelFormat.MONO8, CameraPixelFormat.MONO12, CameraPixelFormat.MONO16]
        else:
            # BSI Express doesn't have true 8-bit mode
            return [CameraPixelFormat.MONO12, CameraPixelFormat.MONO16]

    # =========================================================================
    # Binning and Resolution
    # =========================================================================

    def set_binning(self, binning_factor_x: int, binning_factor_y: int):
        if self._spec.model_id in ("KINETIX", "KINETIX_22"):
            if binning_factor_x != 1 or binning_factor_y != 1:
                raise ValueError(f"{self._spec.name} camera does not support binning")
        elif self._spec.model_id == "PRIME_BSI_EXPRESS":
            if binning_factor_x != binning_factor_y or binning_factor_x not in [1, 2]:
                raise ValueError(f"{self._spec.name} camera only supports 1x1 and 2x2 binning")
            else:
                self._camera.binning = binning_factor_x

    def get_binning(self) -> Tuple[int, int]:
        return self._camera.binning

    def get_binning_options(self) -> Sequence[Tuple[int, int]]:
        return self._camera.binnings

    def get_resolution(self) -> Tuple[int, int]:
        return self._spec.resolution

    def get_pixel_size_unbinned_um(self) -> float:
        return self._spec.pixel_size_um

    def get_pixel_size_binned_um(self) -> float:
        return self._spec.pixel_size_um  # No binning supported

    # =========================================================================
    # Gain (not supported)
    # =========================================================================

    def set_analog_gain(self, analog_gain: float):
        raise NotImplementedError("Analog gain is not supported by this camera.")

    def get_analog_gain(self) -> float:
        raise NotImplementedError("Analog gain is not supported by this camera.")

    def get_gain_range(self) -> CameraGainRange:
        raise NotImplementedError("Analog gain is not supported by this camera.")

    # =========================================================================
    # White Balance (not supported)
    # =========================================================================

    def get_white_balance_gains(self) -> Tuple[float, float, float]:
        raise NotImplementedError("White balance gains are not supported by this camera.")

    def set_white_balance_gains(self, red_gain: float, green_gain: float, blue_gain: float):
        raise NotImplementedError("White balance gains are not supported by this camera.")

    def set_auto_white_balance_gains(self, on: bool):
        raise NotImplementedError("Auto white balance gains are not supported by this camera.")

    # =========================================================================
    # Black Level (not supported)
    # =========================================================================

    def set_black_level(self, black_level: float):
        raise NotImplementedError("Black level adjustment is not supported by this camera.")

    def get_black_level(self) -> float:
        raise NotImplementedError("Black level adjustment is not supported by this camera.")

    # =========================================================================
    # Acquisition Mode
    # =========================================================================

    def _set_acquisition_mode_imp(self, acquisition_mode: CameraAcquisitionMode):
        with self._pause_streaming():
            try:
                # self._log.info(f"Setting acquisition mode to {acquisition_mode}")
                if acquisition_mode == CameraAcquisitionMode.CONTINUOUS:
                    self._camera.exp_mode = PhotometricsCamera._EXP_CODE_MAPPING["Internal Trigger"]
                elif acquisition_mode == CameraAcquisitionMode.SOFTWARE_TRIGGER:
                    self._camera.exp_mode = PhotometricsCamera._EXP_CODE_MAPPING["Software Trigger Edge"]
                elif acquisition_mode == CameraAcquisitionMode.HARDWARE_TRIGGER:
                    self._camera.exp_mode = PhotometricsCamera._EXP_CODE_MAPPING["Edge Trigger"]
                elif acquisition_mode == CameraAcquisitionMode.HARDWARE_TRIGGER_FIRST:
                    self._camera.exp_mode = PhotometricsCamera._EXP_CODE_MAPPING["Trigger First"]
                else:
                    raise ValueError(f"Unsupported acquisition mode: {acquisition_mode}")

                self._acquisition_mode = acquisition_mode
                self._set_exposure_time_imp(self._exposure_time_ms)

            except Exception as e:
                raise CameraError(f"Failed to set acquisition mode: {e}")

    def get_acquisition_mode(self) -> CameraAcquisitionMode:
        mode_name = PhotometricsCamera._EXP_CODE_MAPPING.get(self._camera.exp_mode)
        
        if mode_name == "Internal Trigger":
            return CameraAcquisitionMode.CONTINUOUS
        elif mode_name == "Software Trigger Edge":
            return CameraAcquisitionMode.SOFTWARE_TRIGGER
        elif mode_name == "Edge Trigger":
            return CameraAcquisitionMode.HARDWARE_TRIGGER
        elif mode_name == "Trigger First":
            return CameraAcquisitionMode.HARDWARE_TRIGGER_FIRST
        elif mode_name == "Level Trigger":
            return CameraAcquisitionMode.HARDWARE_TRIGGER # TBD add level trigger mode
        else:
            raise ValueError(f"Unknown acquisition mode: {mode_name}")

    # =========================================================================
    # Trigger
    # =========================================================================

    def send_trigger(self, illumination_time: Optional[float] = None):
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
        if time.time() - self._last_trigger_timestamp > 1.5 * ((self.get_total_frame_time() + 4) / 1000.0):
            self._trigger_sent.clear()
        return not self._trigger_sent.is_set()

    # =========================================================================
    # Region of Interest
    # =========================================================================

    def set_region_of_interest(self, offset_x: int, offset_y: int, width: int, height: int):
        # try:
        # curr_live_roi = self._camera.live_roi
        # self._log.info(f"Current live ROI: {curr_live_roi}")
        # new_roi = {'s1': offset_x, 's2': offset_x + width - 1, 'p1': offset_y, 'p2': offset_y + height - 1, 'sbin': curr_live_roi['sbin'], 'pbin': curr_live_roi['pbin']}
        # self._log.info(f"New ROI: {new_roi}")
        # self._camera.live_roi = new_roi
        self._camera.set_roi(offset_x, offset_y, width, height)
        self._crop_roi = (offset_x, offset_y, width, height)
        self._calculate_strobe_delay()
        # except Exception as e:
        #     self._log.error(f"Failed to set ROI live: {e}. Pausing streaming and trying again.")
        #     with self._pause_streaming():
        #         # self._log.info(f"Sensor size: {self._camera.sensor_size}")
        #         # self._log.info(f"Current ROI: {self._camera.shape(0)}")
        #         # self._camera.reset_rois()
        #         # self._log.info(f"Current ROI: {self._camera.live_roi}")
        #         try:
        #             curr_live_roi = self._camera.live_roi
        #             self._camera.live_roi = {'s1': offset_x, 's2': offset_x + width - 1, 'p1': offset_y, 'p2': offset_y + height - 1, 'sbin': curr_live_roi['sbin'], 'pbin': curr_live_roi['pbin']}
        #             self._camera.set_roi(offset_x, offset_y, width, height)
        #             self._crop_roi = (offset_x, offset_y, width, height)
        #             self._calculate_strobe_delay()
        #         except Exception as e:
        #             raise CameraError(f"Failed to set ROI: {e}")

    def get_region_of_interest(self) -> Tuple[int, int, int, int]:
        roi = self._camera.rois[0]
        w = roi.s2 - roi.s1 + 1
        h = roi.p2 - roi.p1 + 1
        return roi.s1, roi.p1, w, h

    # =========================================================================
    # Temperature
    # =========================================================================

    def set_temperature(self, temperature_deg_c: Optional[float]):
        """Set temperature with model-specific range validation."""
        if temperature_deg_c is None:
            return
            
        min_temp, max_temp = self._spec.temperature_range
        
        with self._pause_streaming():
            try:
                if temperature_deg_c < min_temp or temperature_deg_c > max_temp:
                    raise ValueError(
                        f"Temperature must be between {min_temp}°C and {max_temp}°C "
                        f"for {self._spec.name}, got {temperature_deg_c}°C"
                    )
                self._camera.temp_setpoint = int(temperature_deg_c)
            except Exception as e:
                raise CameraError(f"Failed to set temperature: {e}")

    def get_temperature(self) -> float:
        # Right now we need to pause streaming to get temperature. This is very slow, so we will not update real-time temperature in gui.
        with self._pause_streaming():
            try:
                return self._camera.temp
            except Exception as e:
                raise CameraError(f"Failed to get temperature: {e}")

    def set_temperature_reading_callback(self, callback: Callable):
        raise NotImplementedError("Temperature reading callback is not supported by this camera.")

    # =========================================================================
    # AbstractCamera Readout Mode Interface
    # =========================================================================

    def set_readout_mode(self, readout_mode: CameraReadoutMode):
        """
        Set readout mode using the standard CameraReadoutMode enum.
        
        Note: For full control over Photometrics-specific readout modes,
        use set_camera_mode() with mode name strings.
        """
        # Map CameraReadoutMode to appropriate camera-specific mode
        self._log.info(f"Setting Photometrics readout mode to {readout_mode}")
        if readout_mode == CameraReadoutMode.GLOBAL:
            # Use default mode (usually highest quality)
            self._camera.exp_out_mode = 3 # "Rolling Shutter" mode, counterintuitive but correct to get pseudo-global behavior
        elif readout_mode == CameraReadoutMode.ROLLING:
            self._camera.exp_out_mode = 0 #"First Row" mode, allows rolling behavior to be captured in frames to maximize frame rate


    def get_readout_mode(self) -> CameraReadoutMode:
        """Get the current readout mode as CameraReadoutMode enum."""
        # All Photometrics cameras use rolling shutter, other modes are emulated by taking the exposure out signal and using it for illuminatiion.
        return self._camera.exp_out_mode

    def get_available_readout_modes(self) -> Sequence[CameraReadoutMode]:
        """Get available readout modes as CameraReadoutMode enum."""
        return self._camera.exp_out_modes
