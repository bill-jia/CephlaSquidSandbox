"""
Tucsen Camera Driver

Supports:
- DHYANA 400BSI V3 (2048x2048, 6.5µm pixels)
- FL26 BW (6240x4168, 3.76µm pixels)
- Aries 6506 (2400x2400) and Aries 6510 (3200x3200), 6.5µm pixels

Uses the TUCam SDK (TUCam.py) with model-specific properties for resolution,
readout modes, and triggering. GenICam-based models (Aries) use a different
parameter interface than native TUCam models.
"""

from ctypes import *
import numpy as np
import threading
import time
from dataclasses import dataclass
from typing import Optional, Callable, Sequence, Tuple, Dict, List, Union

import pydantic

from squid.abc import (
    AbstractCamera,
    CameraAcquisitionMode,
    CameraFrameFormat,
    CameraFrame,
    CameraGainRange,
    CameraError,
)
from squid.config import CameraConfig, CameraPixelFormat, CameraReadoutMode, TucsenCameraModel
import squid.logging
from control.TUCam import *
import control.utils
from control._def import *


# ============================================================================
# Camera mode enums (model-specific)
# ============================================================================

class Mode400BSIV3(Enum):
    """
    HDR is the default gain mode of 400BSI V3 camera.
    Store setting values for (TUCIDC_IMGMODESELECT, TUCIDP_GLOBALGAIN) here
    Other combinations of image mode and gain mode are possible, but we don't support them yet.
    """

    HDR = (2, 0)  # 16bit
    CMS = (1, 0)  # 12bit
    HIGH_SPEED = (3, 1)  # 11bit


class ModeFL26BW(Enum):
    # TODO: Add support for FL26BW model
    """
    FL26BW modes values are a combination of image mode and binning.
    Store setting values for (TUCIDC_IMGMODESELECT, TUIDC_RESOLUTION) here
    """
    STANDARD = (0, 0)
    LOW_NOISE = (1, 0)
    SENBIN = (0, 1)


class ModeLibra(Enum):
    # TODO: Add support for Libra25 model
    """
    Store setting values for TUIDC_RESOLUTION here.
    Libra25 has two binning modes: Sensitive (2600 x 2048), and Resolution (5200 x 4096).
    Libra22: Sensitive (2048 x 2048), Resolution (4096 x 4096)
    These 4 modes should be available in each of the binning modes as well.
    """
    RESOLUTION = 0
    SENSITIVE = 1


class ModeAries(Enum):
    """
    Aries modes. Values used for GenICam or TUCam image mode when supported.
    """

    HDR = 0
    SPEED = 1
    SENSITIVITY = 2


@dataclass
class TucsenCameraModeSpec:
    """Specification for a single camera readout mode (Tucsen)."""
    name: str
    bit_depth: int
    line_time_us: float
    display_name: str = ""


class TucsenModelProperties(pydantic.BaseModel):
    binning_to_resolution: Dict[Tuple[int, int], Tuple[int, int]]
    binning_to_set_value: Dict[Tuple[int, int], int]
    mode_to_line_rate_us: Dict[Union[Mode400BSIV3, ModeFL26BW, ModeAries, ModeLibra], float]
    pixel_size_um: float
    has_temperature_control: bool
    is_genicam: bool


# Mode name -> (enum member, spec) per model. Used by set_camera_mode / get_camera_mode_spec.
TUCSEN_CAMERA_MODES: Dict[TucsenCameraModel, Dict[str, Tuple[Union[Mode400BSIV3, ModeFL26BW, ModeAries], TucsenCameraModeSpec]]] = {
    TucsenCameraModel.DHYANA_400BSI_V3: {
        "hdr": (
            Mode400BSIV3.HDR,
            TucsenCameraModeSpec(name="hdr", bit_depth=16, line_time_us=11.2, display_name="HDR (16-bit)"),
        ),
        "cms": (
            Mode400BSIV3.CMS,
            TucsenCameraModeSpec(name="cms", bit_depth=12, line_time_us=11.2, display_name="CMS (12-bit)"),
        ),
        "high_speed": (
            Mode400BSIV3.HIGH_SPEED,
            TucsenCameraModeSpec(name="high_speed", bit_depth=11, line_time_us=7.2, display_name="High Speed (11-bit)"),
        ),
    },
    TucsenCameraModel.FL26_BW: {
        "standard": (
            ModeFL26BW.STANDARD,
            TucsenCameraModeSpec(name="standard", bit_depth=16, line_time_us=34.67, display_name="Standard"),
        ),
        "low_noise": (
            ModeFL26BW.LOW_NOISE,
            TucsenCameraModeSpec(name="low_noise", bit_depth=16, line_time_us=69.3, display_name="Low Noise"),
        ),
        "senbin": (
            ModeFL26BW.SENBIN,
            TucsenCameraModeSpec(name="senbin", bit_depth=16, line_time_us=12.58, display_name="SenBin"),
        ),
    },
    TucsenCameraModel.ARIES_6506: {
        "hdr": (
            ModeAries.HDR,
            TucsenCameraModeSpec(name="hdr", bit_depth=16, line_time_us=11.2, display_name="HDR"),
        ),
        "speed": (
            ModeAries.SPEED,
            TucsenCameraModeSpec(name="speed", bit_depth=16, line_time_us=7.2, display_name="Speed"),
        ),
        "sensitivity": (
            ModeAries.SENSITIVITY,
            TucsenCameraModeSpec(name="sensitivity", bit_depth=16, line_time_us=11.2, display_name="Sensitivity"),
        ),
    },
    TucsenCameraModel.ARIES_6510: {
        "hdr": (
            ModeAries.HDR,
            TucsenCameraModeSpec(name="hdr", bit_depth=16, line_time_us=11.2, display_name="HDR"),
        ),
        "speed": (
            ModeAries.SPEED,
            TucsenCameraModeSpec(name="speed", bit_depth=16, line_time_us=7.2, display_name="Speed"),
        ),
        "sensitivity": (
            ModeAries.SENSITIVITY,
            TucsenCameraModeSpec(name="sensitivity", bit_depth=16, line_time_us=11.2, display_name="Sensitivity"),
        ),
    },
}


# ============================================================================
# Fast acquisition: raw byte packing (HDR 16-bit, CMS 12-bit, HS 11-bit)
# ============================================================================


def camera_mode_name_to_packing(mode_name: Optional[str]) -> str:
    """Map Tucsen get_camera_mode() string to a packing tag."""
    if not mode_name:
        return "hdr16"
    m = str(mode_name).lower().strip()
    if m in ("hdr", "standard", "low_noise", "senbin"):
        return "hdr16"
    if m in ("cms", "sensitivity"):
        return "cms12"
    if m in ("high_speed", "speed"):
        return "hs11"
    return "hdr16"


def max_frame_bytes_for_tucsen_mode(height: int, width: int, packing: str) -> int:
    """Upper bound on bytes per frame for the fast-acquisition ring buffer."""
    n = int(height) * int(width)
    p = packing.lower()
    if p == "hdr16":
        return n * 2
    if p in ("cms12", "hs11"):
        return (n * 3 + 1) // 2
    return n * 2


def decode_tucsen_hdr16(raw: bytes, height: int, width: int) -> np.ndarray:
    """Decode HDR16 using fixed (height, width). ``raw`` must be at least ``height*width*2`` bytes (pad upstream)."""
    n = height * width
    expected = n * 2
    if len(raw) < expected:
        raise ValueError(f"HDR16: need {expected} bytes, got {len(raw)}")
    return np.frombuffer(raw[:expected], dtype=np.uint16).reshape(height, width)

def decode_tucsen_cms12(raw: bytes, height: int, width: int) -> np.ndarray:
    """Vectorized decoding of 12-bit pixels packed into 3 bytes."""
    n = height * width
    expected = (n * 3 + 1) // 2
    if len(raw) < expected:
        # Extend the raw bytes to the expected length
        raw = raw + b'\x00' * (expected - len(raw))
    
    # Calculate how many full pairs of pixels we have
    pairs = n // 2
    
    # 1. Map the relevant raw bytes directly into a fast, read-only 1D uint8 array
    # 2. Reshape it into a 2D array where each row is a 3-byte chunk [b0, b1, b2]
    data = np.frombuffer(raw[:pairs * 3], dtype=np.uint8).reshape(-1, 3)
    
    # Cast the columns to uint16 BEFORE bit-shifting to prevent 8-bit overflow
    b0 = data[:, 0].astype(np.uint16)
    b1 = data[:, 1].astype(np.uint16)
    b2 = data[:, 2].astype(np.uint16)
    
    # Allocate a 2D array for the output pixel pairs
    out = np.empty((pairs, 2), dtype=np.uint16)
    
    # Perform the bitwise operations on all pixels at once
    out[:, 0] = (b0 << 4) | (b1 >> 4)
    out[:, 1] = (b2 << 4) | (b1 & 0x0F)
    
    # Flatten the array back to 1D
    out_flat = out.ravel()
    
    # Handle the odd trailing pixel if the total pixel count 'n' is not an even number
    if n % 2 != 0:
        out_final = np.empty(n, dtype=np.uint16)
        out_final[:n-1] = out_flat
        i = pairs * 3
        out_final[-1] = int.from_bytes(raw[i : i + 2], "little") & 0xFFF
        return out_final.reshape(height, width)
    
    return out_flat.reshape(height, width)


def decode_tucsen_hs11(raw: bytes, height: int, width: int) -> np.ndarray:
    """11-bit values in 12-bit packing with LSB zero; unpack as CMS12 then shift right by one."""
    u12 = decode_tucsen_cms12(raw, height, width)
    return u12.astype(np.uint16) 
    # return (u12 >> 1).astype(np.uint16)

def tucsen_raw_bytes_to_uint16(raw: bytes, meta: dict, packing: str = "hdr16") -> np.ndarray:
    """Decode one frame; packing comes from the camera (see byte_decoding_fn closure), not metadata."""
    height = int(meta["height"])
    width = int(meta["width"])
    p = packing.lower()
    if p == "hdr16":
        return decode_tucsen_hdr16(raw, height, width)
    if p == "cms12":
        return decode_tucsen_cms12(raw, height, width)
    if p == "hs11":
        return decode_tucsen_hs11(raw, height, width)
    raise ValueError(f"Unknown Tucsen packing for fast-acquisition decode: {packing!r}")


def decode_tucsen_raw_bytes(packing: str, raw: bytes, height: int, width: int) -> np.ndarray:
    """Test/helper: decode using explicit dimensions (same as ``tucsen_raw_bytes_to_uint16``)."""
    meta = {"height": int(height), "width": int(width)}
    return tucsen_raw_bytes_to_uint16(raw, meta, packing=packing)


class TucsenCameraCallBack:
    """SDK callback: must call TUCAM_Buf_GetData to dequeue each frame (vendor contract)."""

    def __init__(
        self,
        camera_handle,
        callback_function: Optional[Callable[..., None]],
        log=None,
    ):
        self._camera_handle = camera_handle
        self.callback_function = callback_function

    def OnCallbackBuffer(self):
        m_rawHeader = TUCAM_RAWIMG_HEADER()
        try:
            result = TUCAM_Buf_GetData(self._camera_handle, pointer(m_rawHeader))
            if result != TUCAMRET.TUCAMRET_SUCCESS:
                return
            if self.callback_function is None:
                return
            size = int(m_rawHeader.uiImgSize)
            if size == 0 or not m_rawHeader.pImgData:
                return
            buf = create_string_buffer(size)
            memmove(buf, m_rawHeader.pImgData, size)
            frame_bytes = bytes(buf)
            metadata: Dict[str, object] = {
                "timestamp": m_rawHeader.dblTimeLast,
                "frame_index": int(m_rawHeader.uiIndex),
                "exposure_s": float(m_rawHeader.dblExposure),
                "height": int(m_rawHeader.usHeight),
                "width": int(m_rawHeader.usWidth),
                "ui_img_size": size,
            }
            self.callback_function(frame_bytes, metadata)
        except Exception as e:
            print(f"TucsenCameraCallBack: {e}", exc_info=True)


# ============================================================================
# TucsenCamera Class
# ============================================================================

class TucsenCamera(AbstractCamera):
    @staticmethod
    def _get_sn_by_model(camera_model: TucsenCameraModel) -> str:
        TUCAMINIT = TUCAM_INIT(0, "./".encode("utf-8"))
        TUCAM_Api_Init(pointer(TUCAMINIT))

        for i in range(TUCAMINIT.uiCamCount):
            TUCAMOPEN = TUCAM_OPEN(i, 0)
            TUCAM_Dev_Open(pointer(TUCAMOPEN))
            TUCAMVALUEINFO = TUCAM_VALUE_INFO(TUCAM_IDINFO.TUIDI_CAMERA_MODEL.value, 0, 0, 0)
            TUCAM_Dev_GetInfo(TUCAMOPEN.hIdxTUCam, pointer(TUCAMVALUEINFO))
            if TUCAMVALUEINFO.pText == camera_model.value:
                sn = TucsenCamera._read_camera_sn(TUCAMOPEN.hIdxTUCam)
                TUCAM_Dev_Close(TUCAMOPEN.hIdxTUCam)
                TUCAM_Api_Uninit()
                return sn

            TUCAM_Dev_Close(TUCAMOPEN.hIdxTUCam)

        TUCAM_Api_Uninit()
        return None

    @staticmethod
    def _read_camera_sn(camera_handle: c_void_p) -> str:
        cSN = (c_char * 64)()
        pSN = cast(cSN, c_char_p)
        TUCAMREGRW = TUCAM_REG_RW(1, pSN, 64)
        TUSDKdll.TUCAM_Reg_Read(camera_handle, TUCAMREGRW)
        sn = string_at(pSN).decode("utf-8")
        return sn

    @staticmethod
    def _open(index: Optional[int] = None, sn: Optional[str] = None) -> c_void_p:
        log = squid.logging.get_logger("TucsenCamera._open")

        if index is None and sn is None:
            raise ValueError("You must specify one of either index or sn.")
        elif index is not None and sn is not None:
            raise ValueError("You must specify only 1 of index or sn")

        TUCAMINIT = TUCAM_INIT(0, "./control".encode("utf-8"))
        TUCAM_Api_Init(pointer(TUCAMINIT))
        log.info(f"Connect {TUCAMINIT.uiCamCount} camera(s)")

        if index >= TUCAMINIT.uiCamCount:
            raise CameraError("Camera index out of range. Is the camera connected?")

        if sn is not None:
            for i in range(TUCAMINIT.uiCamCount):
                # We have to open each camera to read the serial number
                TUCAMOPEN = TUCAM_OPEN(i, 0)
                TUCAM_Dev_Open(pointer(TUCAMOPEN))

                if TucsenCamera._read_camera_sn(TUCAMOPEN.hIdxTUCam) == sn:
                    index = i
                    break
                else:
                    TUCAM_Dev_Close(TUCAMOPEN.hIdxTUCam)
            TUCAM_Api_Uninit()
            raise CameraError(f"Camera with serial number {sn} not found")
        else:
            TUCAMOPEN = TUCAM_OPEN(index, 0)
            TUCAM_Dev_Open(pointer(TUCAMOPEN))

        if TUCAMOPEN.hIdxTUCam == 0:
            raise CameraError("Open Tucsen camera failure!")
        else:
            log.info("Open Tucsen camera success!")

        return TUCAMOPEN.hIdxTUCam

    def __init__(
        self,
        camera_config: CameraConfig,
        hw_trigger_fn: Optional[Callable[[Optional[float]], bool]],
        hw_set_strobe_delay_ms_fn: Optional[Callable[[float], bool]],
    ):
        super().__init__(camera_config, hw_trigger_fn, hw_set_strobe_delay_ms_fn)

        # TODO: Open camera by model (We don't need it for Tucsen camera right now)

        self._read_thread_lock = threading.Lock()
        self._read_thread: Optional[threading.Thread] = None
        self._read_thread_keep_running = threading.Event()
        self._read_thread_keep_running.clear()
        self._read_thread_wait_period_s = 1.0
        self._read_thread_running = threading.Event()
        self._read_thread_running.clear()

        self._frame_lock = threading.Lock()
        self._current_frame: Optional[CameraFrame] = None
        self._last_trigger_timestamp = 0
        self._trigger_sent = threading.Event()
        self._is_streaming = threading.Event()

        # Fast acquisition support
        self._fast_acquisition_callback: Optional[Callable[[np.ndarray, Optional[dict]], None]] = None
        self._fast_acquisition_thread: Optional[threading.Thread] = None
        self._fast_acquisition_thread_keep_running = threading.Event()
        self.fast_acquisition_timeout_ms: Optional[int] = None
        self._fast_acquisition_capture_active = False
        self._fast_acq_buffer_callback_obj: Optional[TucsenCameraCallBack] = None
        self._fast_acq_buffer_callback_fn = None
        self._byte_decoding_fn = None

        self._camera = TucsenCamera._open(index=0)
        self._model_properties = self._get_model_properties(self._config.camera_model)

        self._binning = self._config.default_binning
        if self._config.camera_model == TucsenCameraModel.FL26_BW:
            self._camera_mode = ModeFL26BW.STANDARD if self._config.default_binning == (1, 1) else ModeFL26BW.SENBIN
        elif self._config.camera_model == TucsenCameraModel.DHYANA_400BSI_V3:
            self._camera_mode = Mode400BSIV3.HDR
            self._max_acquisition_rate_hz = 100.0
        elif (
            self._config.camera_model == TucsenCameraModel.LIBRA_25
            or self._config.camera_model == TucsenCameraModel.LIBRA_22
        ):
            self._camera_mode = ModeLibra.SENSITIVE
        elif (
            self._config.camera_model == TucsenCameraModel.ARIES_6506
            or self._config.camera_model == TucsenCameraModel.ARIES_6510
        ):
            self._camera_mode = ModeAries.HDR
            self._max_acquisition_rate_hz = self._get_genicam_parameter("AcquisitionMaxFrameRate")["value"]

        packing = camera_mode_name_to_packing(self.get_camera_mode())
        self._byte_decoding_fn = lambda raw, meta: tucsen_raw_bytes_to_uint16(raw, meta, packing=packing)

        if not hasattr(self, "_max_acquisition_rate_hz"):
            self._max_acquisition_rate_hz = 100.0

        self._m_frame = None
        self.frames_polled = 0
        self._trigger_duration_us = 40
        self._trigger_attr = TUCAM_TRIGGER_ATTR()
        self._capture_mode_genicam = TUCAM_CAPTURE_MODES.TUCCM_SEQUENCE.value
        self.temperature_reading_callback = None
        self._exposure_time_ms: float = (
            self._get_genicam_parameter("ExposureTime")["value"]
            if self._model_properties.is_genicam
            else 20.0
        )

        self._configure_camera()

    @staticmethod
    def _get_model_properties(camera_model: TucsenCameraModel) -> TucsenModelProperties:
        if camera_model == TucsenCameraModel.DHYANA_400BSI_V3:
            binning_to_resolution = {
                (1, 1): (2048, 2048),
                # 1: (2048, 2048),  # Code 1 is enhance mode, which will modify pixel values. We don't use it.
                (2, 2): (1024, 1024),
                (4, 4): (512, 512),
            }
            binning_to_set_value = {
                (1, 1): 0,
                (2, 2): 2,
                (4, 4): 3,
            }
            mode_to_line_rate_us = {
                Mode400BSIV3.HDR: 11.2,
                Mode400BSIV3.CMS: 11.2,
                Mode400BSIV3.HIGH_SPEED: 7.2,
            }
            pixel_size_um = 6.5
            has_temperature_control = True
            is_genicam = False
        elif camera_model == TucsenCameraModel.FL26_BW:
            # TODO: Support binning for FL26BW model
            binning_to_resolution = {
                (1, 1): (6240, 4168),
                (2, 2): (3120, 2084),
            }
            binning_to_set_value = {
                (1, 1): 0,
                (2, 2): 1,
            }
            mode_to_line_rate_us = {
                ModeFL26BW.STANDARD: 34.67,
                ModeFL26BW.LOW_NOISE: 69.3,
                ModeFL26BW.SENBIN: 12.58,
            }
            pixel_size_um = 3.76
            has_temperature_control = True
            is_genicam = False
        elif camera_model == TucsenCameraModel.ARIES_6506 or camera_model == TucsenCameraModel.ARIES_6510:
            binning_to_set_value = {
                (1, 1): 0,
                (2, 2): 1,
                (4, 4): 2,
            }
            mode_to_line_rate_us = {
                ModeAries.HDR: 11.2,
                ModeAries.SPEED: 7.2,
                ModeAries.SENSITIVITY: 11.2,
            }
            pixel_size_um = 6.5
            has_temperature_control = False
            is_genicam = True
            if camera_model == TucsenCameraModel.ARIES_6506:
                binning_to_resolution = {
                    (1, 1): (2400, 2400),
                    (2, 2): (1200, 1200),
                    (4, 4): (600, 600),
                }
            elif camera_model == TucsenCameraModel.ARIES_6510:
                binning_to_resolution = {
                    (1, 1): (3200, 3200),
                    (2, 2): (1600, 1600),
                    (4, 4): (800, 800),
                }
        elif camera_model == TucsenCameraModel.LIBRA_25 or camera_model == TucsenCameraModel.LIBRA_22:
            # TODO: Support binning for LIBRA_25 and LIBRA_22 model
            binning_to_resolution = {
                (1, 1): (5200, 4096),
                (2, 2): (2600, 2048),  # 2x2 binning should be the default
            }
            binning_to_set_value = {
                (1, 1): ModeLibra.RESOLUTION,
                (2, 2): ModeLibra.SENSITIVE,
            }
            mode_to_line_rate_us = {
                ModeLibra.RESOLUTION: 34.67,
                ModeLibra.SENSITIVE: 6.31,
            }
            pixel_size_um = 3.76
            has_temperature_control = True
            is_genicam = False
        else:
            raise ValueError(f"Unsupported camera model: {camera_model}")

        model_properties = TucsenModelProperties(
            binning_to_resolution=binning_to_resolution,
            binning_to_set_value=binning_to_set_value,
            mode_to_line_rate_us=mode_to_line_rate_us,
            pixel_size_um=pixel_size_um,
            has_temperature_control=has_temperature_control,
            is_genicam=is_genicam,
        )
        return model_properties

    def _configure_camera(self):
        if self._model_properties.has_temperature_control:
            self.set_temperature(self._config.default_temperature)

        if self._model_properties.is_genicam:
            for port in TUCAM_OUTPUTTRG_PORT:
                self._set_genicam_parameter("TriggerPort", port.value, TUELEM_TYPE.TU_ElemInteger.value)
                if port == TUCAM_OUTPUTTRG_PORT.TUPORT_OUT_ONE:
                    self._set_genicam_parameter("TriggerPortEnable", 1, TUELEM_TYPE.TU_ElemInteger.value)
                else:
                    self._set_genicam_parameter("TriggerPortEnable", 0, TUELEM_TYPE.TU_ElemInteger.value)
                self._set_genicam_parameter("TriggerOutputWidth", self._trigger_duration_us, TUELEM_TYPE.TU_ElemInteger.value)

        self.set_binning(*self._config.default_binning)
        self.set_acquisition_mode(CameraAcquisitionMode.CONTINUOUS)

        self._terminate_temperature_event = threading.Event()
        self.temperature_reading_thread = threading.Thread(target=self._check_temperature, daemon=True)
        self.temperature_reading_thread.start()

    # =========================================================================
    # Streaming Control
    # =========================================================================

    def start_streaming(self):
        if self._is_streaming.is_set():
            self._log.debug("Already streaming, start_streaming is noop")
            return

        self._set_acquisition_mode_imp(CameraAcquisitionMode.CONTINUOUS)

        if self._m_frame is None:
            self._allocate_buffer()

        trigger_mode = self._capture_mode_genicam if self._model_properties.is_genicam else self._trigger_attr.nTgrMode
        if TUCAM_Cap_Start(self._camera, trigger_mode) != TUCAMRET.TUCAMRET_SUCCESS:
            TUCAM_Buf_Release(self._camera)
            raise CameraError("Failed to start streaming")

        self._ensure_read_thread_running()

        self._trigger_sent.clear()
        self._is_streaming.set()
        self._log.info(
            f"TUCam Camera starts streaming in camera mode: {self.get_camera_mode()}, "
            f"max acquisition rate: {self._max_acquisition_rate_hz} Hz"
        )

    def _allocate_buffer(self):
        self._m_frame = TUCAM_FRAME()
        self._m_frame.pBuffer = 0
        self._m_frame.ucFormatGet = TUFRM_FORMATS.TUFRM_FMT_USUAl.value
        self._m_frame.uiRsdSize = 1

        if TUCAM_Buf_Alloc(self._camera, pointer(self._m_frame)) != TUCAMRET.TUCAMRET_SUCCESS:
            raise CameraError("Failed to allocate buffer")

    def stop_streaming(self):
        if not self._is_streaming.is_set():
            self._log.debug("Already stopped, stop_streaming is noop")
            return

        self._cleanup_read_thread()

        if TUCAM_Cap_Stop(self._camera) != TUCAMRET.TUCAMRET_SUCCESS:
            raise CameraError("Failed to stop streaming")

        self._trigger_sent.clear()
        self._is_streaming.clear()
        self._log.info("TUCam Camera streaming stopped")

    def get_is_streaming(self):
        return self._is_streaming.is_set()

    # =========================================================================
    # Fast Acquisition Support
    # =========================================================================

    def start_fast_acquisition_frame_grabbing(
        self,
        frame_rate_hz: float,
        n_frames_expected=0,
        frame_callback: Optional[Callable[..., None]] = None,
        acquisition_mode: Optional[CameraAcquisitionMode] = None,
    ):
        """
        Start fast acquisition using the SDK buffer callback (TUCAM_Buf_DataCallBack).

        Call after setting the camera to HARDWARE_TRIGGER or HARDWARE_TRIGGER_FIRST
        and before firing DAQ waveforms. Each frame is dequeued with TUCAM_Buf_GetData
        in the SDK thread, then passed to frame_callback.

        Args:
            frame_rate_hz: Expected frame rate (used for internal buffer sizing).
            n_frames_expected: Hint for expected number of frames (informational).
            frame_callback: Receives (frame: bytes, metadata: dict). Metadata includes
                timestamp, frame_index, exposure_s, height, width, ui_img_size.
            acquisition_mode: Optional; use when GenICam cannot distinguish HARDWARE_TRIGGER
                vs HARDWARE_TRIGGER_FIRST from get_acquisition_mode() alone.
        """
        if self._is_streaming.is_set():
            self._log.warning("Camera is already streaming. Stop streaming before starting fast acquisition.")
            return

        if acquisition_mode is None:
            acquisition_mode = self.get_acquisition_mode()
        elif acquisition_mode != self.get_acquisition_mode():
            self._set_acquisition_mode_imp(acquisition_mode)
            self._log.info(f"Acquisition mode changed to: {acquisition_mode}")

        if self._model_properties.is_genicam:
            self._max_acquisition_rate_hz = self._get_genicam_parameter("AcquisitionMaxFrameRate")["value"]
            self.set_acquisition_frame_rate(self._max_acquisition_rate_hz)

        self._log.info(
            f"Starting fast acquisition with mode: {acquisition_mode}, "
            f"camera mode: {self.get_camera_mode()}, "
            f"camera max acquisition rate: {self._max_acquisition_rate_hz} Hz"
        )

        if acquisition_mode not in [CameraAcquisitionMode.HARDWARE_TRIGGER, CameraAcquisitionMode.HARDWARE_TRIGGER_FIRST]:
            raise CameraError("Fast acquisition requires HARDWARE_TRIGGER or HARDWARE_TRIGGER_FIRST mode")

        self._trigger_attr.nBufFrames = int(np.ceil(0.5 * frame_rate_hz))
        if TUCAM_Cap_SetTrigger(self._camera, self._trigger_attr) != TUCAMRET.TUCAMRET_SUCCESS:
            raise CameraError(f"Failed to set trigger buffer for fast acquisition to {self._trigger_attr.nBufFrames}")

        if TUCAM_Buf_Release(self._camera) != TUCAMRET.TUCAMRET_SUCCESS:
            raise CameraError("Failed to release buffer")
        self._allocate_buffer()

        self._fast_acq_buffer_callback_obj = TucsenCameraCallBack(
            self._camera, frame_callback, log=self._log
        )
        self._fast_acq_buffer_callback_fn = BUFFER_CALLBACK(
            self._fast_acq_buffer_callback_obj.OnCallbackBuffer
        )
        CALL_BACK_USER = CONTEXT_CALLBACK(self._fast_acq_buffer_callback_obj.__class__)
        TUCAM_Buf_DataCallBack(self._camera, self._fast_acq_buffer_callback_fn, CALL_BACK_USER)

        machine_acquisition_mode = self._capture_mode_genicam if self._model_properties.is_genicam else self._trigger_attr.nTgrMode
        if TUCAM_Cap_Start(self._camera, machine_acquisition_mode) != TUCAMRET.TUCAMRET_SUCCESS:
            self._fast_acq_buffer_callback_obj = None
            self._fast_acq_buffer_callback_fn = None
            raise CameraError("Failed to start capture for fast acquisition")

        self._log.info("Capture started for fast acquisition")
        self._fast_acquisition_capture_active = True
        self._fast_acquisition_callback = frame_callback

    def stop_fast_acquisition_frame_grabbing(self):
        """Stop fast acquisition (SDK callback or poll thread) and release buffers."""
        if self._fast_acquisition_thread is not None:
            self._log.info("Stopping fast acquisition (poll thread)...")
            self._fast_acquisition_thread_keep_running.clear()
            if self._fast_acquisition_thread.is_alive():
                self._fast_acquisition_thread.join(timeout=2.0)
                if self._fast_acquisition_thread.is_alive():
                    self._log.warning("Fast acquisition thread did not exit in time")
            self._fast_acquisition_thread = None

        try:
            TUCAM_Buf_AbortWait(self._camera)
        except Exception:
            pass

        if not self._fast_acquisition_capture_active:
            return

        self._log.info("Stopping fast acquisition frame grabbing...")
        if TUCAM_Cap_Stop(self._camera) != TUCAMRET.TUCAMRET_SUCCESS:
            self._log.debug("TUCAM_Cap_Stop returned non-success during fast acq cleanup")
        if TUCAM_Buf_Release(self._camera) != TUCAMRET.TUCAMRET_SUCCESS:
            self._log.debug("TUCAM_Buf_Release returned non-success during fast acq cleanup")

        self._m_frame = None
        self._fast_acquisition_capture_active = False
        self._fast_acq_buffer_callback_obj = None
        self._fast_acq_buffer_callback_fn = None
        self._fast_acquisition_callback = None

        if self._model_properties.is_genicam:
            self._trigger_attr.nBufFrames = 4
            if TUCAM_Cap_SetTrigger(self._camera, self._trigger_attr) != TUCAMRET.TUCAMRET_SUCCESS:
                raise CameraError(f"Failed to reset trigger buffer after fast acquisition to {self._trigger_attr.nBufFrames}")
        else:
            if TUCAM_Cap_GetTrigger(self._camera, pointer(self._trigger_attr)) == TUCAMRET.TUCAMRET_SUCCESS:
                self._trigger_attr.nBufFrames = 1
                if TUCAM_Cap_SetTrigger(self._camera, self._trigger_attr) != TUCAMRET.TUCAMRET_SUCCESS:
                    self._log.debug("TUCAM_Cap_SetTrigger restore after fast acq failed")

        # Vendor SDK workaround: close and reopen camera to reset internal state
        if self.temperature_reading_thread is not None:
            self._terminate_temperature_event.set()
            self.temperature_reading_thread.join()
        if TUCAM_Dev_Close(self._camera) != TUCAMRET.TUCAMRET_SUCCESS:
            raise CameraError("Failed to close camera")
        else:
            self._log.info("Closed camera successfully")
        TUCAM_Api_Uninit()
        time.sleep(1.0)
        self._camera = TucsenCamera._open(index=0)
        if self._camera is None:
            raise CameraError("Failed to reopen camera after fast acquisition")
        self._configure_camera()

        self._log.info(f"Fast acquisition frame grabbing stopped, {self.frames_polled} frames polled")

    def _grab_frames_fast_acquisition(self):
        """Thread function: poll for frames and pass raw data to the fast acquisition callback."""
        self._log.debug("Fast acquisition grab thread running")

        while self._fast_acquisition_thread_keep_running.is_set():
            try:
                wait_ms = self.fast_acquisition_timeout_ms if self.fast_acquisition_timeout_ms is not None else 100
                ret = TUCAM_Buf_WaitForFrame(self._camera, pointer(self._m_frame), c_int32(wait_ms))
                if ret != TUCAMRET.TUCAMRET_SUCCESS or self._m_frame.pBuffer is None or self._m_frame.pBuffer == 0:
                    continue
                else:
                    self.frames_polled += 1
                raw_data = self._convert_frame_to_numpy(self._m_frame).copy()
                if self._fast_acquisition_callback is not None:
                    try:
                        self._fast_acquisition_callback(raw_data, None)
                    except Exception as e:
                        self._log.error(f"Fast acquisition callback error: {e}")
            except Exception as e:
                if self._fast_acquisition_thread_keep_running.is_set():
                    self._log.debug(f"Fast acquisition loop: {e}")

        self._log.debug("Fast acquisition grab thread stopped")

    # =========================================================================
    # Thread Management
    # =========================================================================

    def close(self):
        try:
            self.stop_fast_acquisition_frame_grabbing()
        except Exception:
            pass
        if self.temperature_reading_thread is not None:
            self._terminate_temperature_event.set()
            self.temperature_reading_thread.join()
        if TUCAM_Dev_Close(self._camera) != TUCAMRET.TUCAMRET_SUCCESS:
            raise CameraError("Failed to close camera")
        TUCAM_Api_Uninit()
        self._log.info("Close Tucsen camera success")

    def _ensure_read_thread_running(self):
        with self._read_thread_lock:
            if self._read_thread is not None and self._read_thread_running.is_set():
                self._log.debug("Read thread exists and thread is marked as running.")
                return True

            elif self._read_thread is not None:
                self._log.warning("Read thread already exists, but not marked as running.  Still attempting start.")

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

            if TUCAM_Buf_AbortWait(self._camera) != TUCAMRET.TUCAMRET_SUCCESS:
                self._log.error("Failed to abort wait for frame")

            self._read_thread.join(1.1 * self._read_thread_wait_period_s)

            success = not self._read_thread.is_alive()
            if not success:
                self._log.warning("Read thread refused to exit!")

            self._read_thread = None
            self._read_thread_running.clear()

    def _wait_for_frame(self):
        self._log.info("Starting Tucsen read thread.")
        self._read_thread_running.set()
        while self._read_thread_keep_running.is_set():
            try:
                wait_time_ms = int(self._read_thread_wait_period_s * 1000)  # ms, convert to int
                try:
                    TUCAM_Buf_WaitForFrame(self._camera, pointer(self._m_frame), c_int32(wait_time_ms))
                except Exception:
                    pass

                if self._m_frame is None or self._m_frame.pBuffer is None or self._m_frame.pBuffer == 0:
                    self._log.error("Invalid frame buffer")
                    continue

                np_image = self._convert_frame_to_numpy(self._m_frame)

                processed_frame = self._process_raw_frame(np_image)
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
                self._log.exception(f"Exception: {e} in read loop, ignoring and trying to continue.")
        self._read_thread_running.clear()

    def _convert_frame_to_numpy(self, frame: TUCAM_FRAME) -> np.ndarray:
        # TODO: In the latest version of 400BSI V3, the readout data will match the actual bit depth.
        # We are not able to tell the firmware version from SN yet. Need to figure out if it's safe to assume
        # all users have the latest firmware. We use 16-bit buffer for the old demo units for now.
        buf = create_string_buffer(frame.uiImgSize)
        pointer_data = c_void_p(frame.pBuffer + frame.usHeader)
        memmove(buf, pointer_data, frame.uiImgSize)

        data = bytes(buf)
        image_np = np.frombuffer(data, dtype=np.uint16)
        image_np = image_np.reshape((frame.usHeight, frame.usWidth))

        return image_np

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
        # TBD: properly handle hardware strobing, for now just asusme that user is giving some buffer time for strobing.
        # if self.get_acquisition_mode() in (
        #     CameraAcquisitionMode.HARDWARE_TRIGGER,
        #     CameraAcquisitionMode.HARDWARE_TRIGGER_FIRST,
        # ):
        #     strobe_time_ms = self.get_strobe_time()
        #     adjusted_exposure_time = exposure_time_ms + strobe_time_ms
        #     if self._hw_set_strobe_delay_ms_fn:
        #         self._log.debug(f"Setting hw strobe time to {strobe_time_ms} [ms]")
        #         self._hw_set_strobe_delay_ms_fn(strobe_time_ms)
        # else:
        adjusted_exposure_time = exposure_time_ms

        if self._model_properties.is_genicam:
            self._set_genicam_parameter(
                "ExposureTime", int(adjusted_exposure_time * 1000), TUELEM_TYPE.TU_ElemInteger.value
            )
        else:
            if (
                TUCAM_Prop_SetValue(
                    self._camera, TUCAM_IDPROP.TUIDP_EXPOSURETM.value, c_double(adjusted_exposure_time), 0
                )
                != TUCAMRET.TUCAMRET_SUCCESS
            ):
                raise CameraError("Failed to set exposure time")

        self._exposure_time_ms = exposure_time_ms
        self._trigger_sent.clear()

    def get_exposure_time(self) -> float:
        return self._exposure_time_ms

    def set_acquisition_frame_rate(self, frame_rate_hz: float):
        if self._model_properties.is_genicam:
            self._set_genicam_parameter("AcquisitionFrameRate", frame_rate_hz, TUELEM_TYPE.TU_ElemFloat.value)
        else:
            self._trigger_attr.nFrameRate = frame_rate_hz

    def get_acquisition_frame_rate(self) -> float:
        if self._model_properties.is_genicam:
            return self._get_genicam_parameter("AcquisitionFrameRate")["value"]
        else:
            return self._trigger_attr.nFrameRate

    def get_exposure_limits(self) -> Tuple[float, float]:
        if self._model_properties.is_genicam:
            param_info = self._get_genicam_parameter("ExposureTime")
            return param_info["min"] / 1000.0, param_info["max"] / 1000.0  # read in us, convert to ms
        else:
            prop = TUCAM_PROP_ATTR()
            prop.idProp = TUCAM_IDPROP.TUIDP_EXPOSURETM.value
            prop.nIdxChn = 0
            if TUCAM_Prop_GetAttr(self._camera, pointer(prop)) != TUCAMRET.TUCAMRET_SUCCESS:
                raise CameraError("Failed to get exposure time limits")
            self._log.info(f"Exposure limits: {prop.dbValMin}, {prop.dbValMax}")
            return prop.dbValMin, prop.dbValMax

    def _calculate_strobe_delay(self):
        # Line rate: FL 26BW: 34.67 us for standard resolution; 69.3 us for low noise; 12.58 us for SenBin
        #            400BSI V3: 7.2 us for high speed; 11.2 us for other gain modes
        # Right now we are only using 400BSI V3's HDR mode.
        # TODO: Support more modes.
        _, _, _, height = self.get_region_of_interest()
        readout_time_ms = (
            self._model_properties.mode_to_line_rate_us[self._camera_mode] * height * self._binning[1] / 1000.0
        )

        if self._model_properties.is_genicam:
            param_info = self._get_genicam_parameter("TriggerInputDelay")
            trigger_delay_ms = param_info["value"] / 1000.0  # read in us, convert to ms
        else:
            trigger_attr = TUCAM_TRIGGER_ATTR()
            if TUCAM_Cap_GetTrigger(self._camera, pointer(trigger_attr)) != TUCAMRET.TUCAMRET_SUCCESS:
                raise CameraError("Failed to get trigger delay")
            trigger_delay_ms = trigger_attr.nDelayTm

        self._strobe_delay_ms = readout_time_ms + trigger_delay_ms

    def get_strobe_time(self) -> float:
        return self._strobe_delay_ms

    def set_frame_format(self, frame_format: CameraFrameFormat):
        if frame_format != CameraFrameFormat.RAW:
            raise ValueError("Only the RAW frame format is supported by this camera.")

    def get_frame_format(self) -> CameraFrameFormat:
        return CameraFrameFormat.RAW

    def set_pixel_format(self, pixel_format: CameraPixelFormat):
        # TODO: This is temporary before we move to support the new version of 400BSI V3 hardware and FL26BW model.
        if pixel_format != CameraPixelFormat.MONO16:
            raise ValueError(f"Pixel format {pixel_format} is not supported by this camera.")

    def get_pixel_format(self) -> CameraPixelFormat:
        # TODO: This is temporary before we move to support the new version of 400BSI V3 hardware and FL26BW model.
        return CameraPixelFormat.MONO16

    def get_available_pixel_formats(self) -> Sequence[CameraPixelFormat]:
        return [CameraPixelFormat.MONO16]

    # =========================================================================
    # Readout Mode (AbstractCamera interface)
    # =========================================================================

    def set_readout_mode(self, readout_mode: CameraReadoutMode):
        """Set readout mode. Tucsen cameras support GLOBAL only."""
        if self._config.camera_model == TucsenCameraModel.ARIES_6506 or self._config.camera_model == TucsenCameraModel.ARIES_6510:
            if readout_mode != CameraReadoutMode.ROLLING:
                raise ValueError(f"Tucsen camera {self._config.camera_model} does not support readout mode {readout_mode}")
                # TBD: add support for global with reset (grayed out in SamplePro for some reason, figure it out)
        if readout_mode != CameraReadoutMode.GLOBAL:
            raise ValueError(f"Tucsen camera only supports GLOBAL readout mode, got {readout_mode}")

    def get_readout_mode(self) -> CameraReadoutMode:
        """Get current readout mode."""
        if self._config.camera_model == TucsenCameraModel.ARIES_6506 or self._config.camera_model == TucsenCameraModel.ARIES_6510:
            return CameraReadoutMode.ROLLING
        return CameraReadoutMode.GLOBAL

    def get_available_readout_modes(self) -> Sequence[CameraReadoutMode]:
        """Get available readout modes."""
        if self._config.camera_model == TucsenCameraModel.ARIES_6506 or self._config.camera_model == TucsenCameraModel.ARIES_6510:
            return [CameraReadoutMode.ROLLING]
        return [CameraReadoutMode.GLOBAL]

    # =========================================================================
    # Camera Mode API (model-specific, TUCam low-level)
    # =========================================================================

    def get_available_camera_modes(self) -> List[str]:
        """Get list of available camera readout mode names for this model."""
        modes = TUCSEN_CAMERA_MODES.get(self._config.camera_model)
        if modes is None:
            return []
        return list(modes.keys())

    def get_camera_mode(self) -> Optional[str]:
        """Get the current camera readout mode name."""
        if self._config.camera_model == TucsenCameraModel.DHYANA_400BSI_V3:
            for name, (enum_val, _) in TUCSEN_CAMERA_MODES[TucsenCameraModel.DHYANA_400BSI_V3].items():
                if enum_val == self._camera_mode:
                    return name
        elif self._config.camera_model == TucsenCameraModel.FL26_BW:
            for name, (enum_val, _) in TUCSEN_CAMERA_MODES[TucsenCameraModel.FL26_BW].items():
                if enum_val == self._camera_mode:
                    return name
        elif self._config.camera_model in (TucsenCameraModel.ARIES_6506, TucsenCameraModel.ARIES_6510):
            for name, (enum_val, _) in TUCSEN_CAMERA_MODES[self._config.camera_model].items():
                if enum_val == self._camera_mode:
                    return name
        return None

    def get_fast_acquisition_max_frame_bytes(self) -> int:
        # Seems like frame buffer still sending as if it's 16 bits
        roi = self.get_region_of_interest()
        h, w = roi[3], roi[2]
        packing = camera_mode_name_to_packing(self.get_camera_mode())
        n_bytes = max_frame_bytes_for_tucsen_mode(h, w, packing)
        return n_bytes

    def set_camera_mode(self, mode_name: str):
        """
        Set the camera readout mode by name (Tucsen model-specific).

        Uses TUCam TUIDC_IMGMODESELECT (capability) and TUIDP_GLOBALGAIN (property)
        for native models; GenICam parameters for Aries.

        Args:
            mode_name: One of the mode names from get_available_camera_modes(),
                       e.g. "hdr", "cms", "high_speed" (400BSI V3); "standard", "low_noise", "senbin" (FL26);
                       "hdr", "speed", "sensitivity" (Aries).
        """
        modes = TUCSEN_CAMERA_MODES.get(self._config.camera_model)
        if modes is None:
            raise ValueError(f"No camera modes defined for model {self._config.camera_model}")
        if mode_name not in modes:
            raise ValueError(
                f"Unknown camera mode '{mode_name}' for this model. "
                f"Available: {list(modes.keys())}"
            )
        enum_member, spec = modes[mode_name]
        with self._pause_streaming():
            if self._config.camera_model == TucsenCameraModel.DHYANA_400BSI_V3:
                img_mode, gain = enum_member.value
                if TUCAM_Capa_SetValue(
                    self._camera, TUCAM_IDCAPA.TUIDC_IMGMODESELECT.value, img_mode
                ) != TUCAMRET.TUCAMRET_SUCCESS:
                    raise CameraError("Failed to set image mode (TUIDC_IMGMODESELECT)")
                if TUCAM_Prop_SetValue(
                    self._camera, TUCAM_IDPROP.TUIDP_GLOBALGAIN.value, c_double(gain), 0
                ) != TUCAMRET.TUCAMRET_SUCCESS:
                    raise CameraError("Failed to set global gain (TUIDP_GLOBALGAIN)")
            elif self._config.camera_model == TucsenCameraModel.FL26_BW:
                img_mode, res_value = enum_member.value
                if TUCAM_Capa_SetValue(
                    self._camera, TUCAM_IDCAPA.TUIDC_IMGMODESELECT.value, img_mode
                ) != TUCAMRET.TUCAMRET_SUCCESS:
                    raise CameraError("Failed to set image mode (TUIDC_IMGMODESELECT)")
                if TUCAM_Capa_SetValue(
                    self._camera, TUCAM_IDCAPA.TUIDC_RESOLUTION.value, res_value
                ) != TUCAMRET.TUCAMRET_SUCCESS:
                    raise CameraError("Failed to set resolution (TUIDC_RESOLUTION)")
                self._binning = (2, 2) if res_value != 0 else (1, 1)
            else:
                try:
                    self._set_genicam_parameter(
                        "SensorOperationMode", enum_member.value, TUELEM_TYPE.TU_ElemEnumeration.value
                    )
                except (CameraError, Exception):
                    pass
            self._camera_mode = enum_member
            self._update_internal_settings()
        self._log.info(f"Set camera mode to '{spec.display_name or mode_name}' ({spec.bit_depth}-bit)")

    def get_camera_mode_spec(self, mode_name: str) -> Optional[TucsenCameraModeSpec]:
        """Get the specification for a camera mode by name."""
        modes = TUCSEN_CAMERA_MODES.get(self._config.camera_model)
        if modes is None or mode_name not in modes:
            return None
        return modes[mode_name][1]

    def _update_internal_settings(self):
        if TUCAM_Buf_Release(self._camera) != TUCAMRET.TUCAMRET_SUCCESS:
            raise CameraError("Failed to release buffer")
        self._allocate_buffer()
        self._calculate_strobe_delay()
        if self._model_properties.is_genicam:
            self._max_acquisition_rate_hz = self._get_genicam_parameter("AcquisitionMaxFrameRate")["value"]
        packing = camera_mode_name_to_packing(self.get_camera_mode())
        self._byte_decoding_fn = lambda raw, meta: tucsen_raw_bytes_to_uint16(raw, meta, packing=packing)

    def _raw_set_resolution(self, bin_value: int):
        with self._pause_streaming():
            if (
                TUCAM_Capa_SetValue(self._camera, TUCAM_IDCAPA.TUIDC_RESOLUTION.value, c_int(bin_value))
                != TUCAMRET.TUCAMRET_SUCCESS
            ):
                raise CameraError("Cannot set camera binning.")
            if self._config.camera_model == TucsenCameraModel.FL26_BW:
                self._camera_mode = ModeFL26BW.STANDARD if bin_value == 0 else ModeFL26BW.SENBIN
            self._update_internal_settings()

    def _raw_set_binning_genicam(self, binning_value: int):
        with self._pause_streaming():
            self._set_genicam_parameter("BinningSelector", binning_value, TUELEM_TYPE.TU_ElemEnumeration.value)
            self._update_internal_settings()

    def set_binning(self, binning_factor_x: int, binning_factor_y: int):
        # TODO: Add support for FL26BW model
        if not (binning_factor_x, binning_factor_y) in self._model_properties.binning_to_set_value:
            raise CameraError(f"No binning option exists for {binning_factor_x}x{binning_factor_y}")
        if self._model_properties.is_genicam:
            self._raw_set_binning_genicam(
                self._model_properties.binning_to_set_value[(binning_factor_x, binning_factor_y)]
            )
        else:
            self._raw_set_resolution(self._model_properties.binning_to_set_value[(binning_factor_x, binning_factor_y)])
        self._binning = (binning_factor_x, binning_factor_y)

    def get_binning(self) -> Tuple[int, int]:
        return self._binning

    def get_binning_options(self) -> Sequence[Tuple[int, int]]:
        # TODO: Add support for FL26BW model
        return self._model_properties.binning_to_set_value.keys()

    def get_resolution(self) -> Tuple[int, int]:
        # TODO: Add support for FL26BW model
        if self._model_properties.is_genicam:
            return self._model_properties.binning_to_resolution[self._binning]
        else:
            idx = c_int(0)
            if (
                TUCAM_Capa_GetValue(self._camera, TUCAM_IDCAPA.TUIDC_RESOLUTION.value, pointer(idx))
                != TUCAMRET.TUCAMRET_SUCCESS
            ):
                raise CameraError("Failed to get resolution")
            return self._model_properties.binning_to_resolution[self._binning]

    def get_pixel_size_unbinned_um(self) -> float:
        return self._model_properties.pixel_size_um

    def get_pixel_size_binned_um(self) -> float:
        return self.get_pixel_size_unbinned_um() * self.get_binning()[0]

    def set_analog_gain(self, analog_gain: float):
        if self._config.camera_model == TucsenCameraModel.FL26_BW:
            self._raw_set_analog_gain_fl26bw(analog_gain)
        else:
            raise NotImplementedError("Analog gain is not implemented for this camera.")

    def get_analog_gain(self) -> float:
        if self._config.camera_model == TucsenCameraModel.FL26_BW:
            return self._raw_get_analog_gain_fl26bw()
        else:
            raise NotImplementedError("Analog gain is not implemented for this camera.")

    def get_gain_range(self) -> CameraGainRange:
        if self._config.camera_model == TucsenCameraModel.FL26_BW:
            # These values are not accurate gain values. They are for selecting gain mode for FL26BW model.
            return CameraGainRange(min_gain=0, max_gain=3, gain_step=1)
        else:
            raise NotImplementedError("Analog gain is not implemented for this camera.")

    def get_white_balance_gains(self) -> Tuple[float, float, float]:
        raise NotImplementedError("White Balance Gains not implemented for the Tucsen driver.")

    def set_white_balance_gains(self, red_gain: float, green_gain: float, blue_gain: float):
        raise NotImplementedError("White Balance Gains not implemented for the Tucsen driver.")

    def set_black_level(self, black_level: float):
        raise NotImplementedError("Black levels are not implemented for the Tucsen driver.")

    def get_black_level(self) -> float:
        raise NotImplementedError("Black levels are not implemented for the Tucsen driver.")

    def set_region_of_interest(self, offset_x: int, offset_y: int, width: int, height: int):
        # TODO: limit range of values to be within the camera's capabilities
        if self._model_properties.is_genicam:
            nHOffset = control.utils.truncate_to_interval(offset_x, 8)
            nVOffset = control.utils.truncate_to_interval(offset_y, 2)
            nWidth = control.utils.truncate_to_interval(width, 8)
            nHeight = control.utils.truncate_to_interval(height, 2)
        else:
            roi_attr = TUCAM_ROI_ATTR()
            roi_attr.bEnable = 1
            # These values must be a multiple of 4. When using 11bit mode, they must be a multiple of 32 (not supported yet).
            roi_attr.nHOffset = control.utils.truncate_to_interval(offset_x, 4)
            roi_attr.nVOffset = control.utils.truncate_to_interval(offset_y, 4)
            roi_attr.nWidth = control.utils.truncate_to_interval(width, 4)
            roi_attr.nHeight = control.utils.truncate_to_interval(height, 4)

        with self._pause_streaming():
            if self._model_properties.is_genicam:
                self._set_genicam_parameter("MultiROIOffsetX", nHOffset, TUELEM_TYPE.TU_ElemInteger.value)
                self._set_genicam_parameter("MultiROIOffsetY", nVOffset, TUELEM_TYPE.TU_ElemInteger.value)
                self._set_genicam_parameter("MultiROIWidth", nWidth, TUELEM_TYPE.TU_ElemInteger.value)
                self._set_genicam_parameter("MultiROIHeight", nHeight, TUELEM_TYPE.TU_ElemInteger.value)
            else:
                if TUCAM_Cap_SetROI(self._camera, roi_attr) != TUCAMRET.TUCAMRET_SUCCESS:
                    raise CameraError(
                        f"Failed to set ROI: {roi_attr.nHOffset}, {roi_attr.nVOffset}, {roi_attr.nWidth}, {roi_attr.nHeight}"
                    )
            self._update_internal_settings()

    def get_region_of_interest(self) -> Tuple[int, int, int, int]:
        if self._model_properties.is_genicam:
            h_offset = self._get_genicam_parameter("MultiROIOffsetX")["value"]
            v_offset = self._get_genicam_parameter("MultiROIOffsetY")["value"]
            width = self._get_genicam_parameter("MultiROIWidth")["value"]
            height = self._get_genicam_parameter("MultiROIHeight")["value"]
            return (h_offset, v_offset, width, height)
        else:
            roi_attr = TUCAM_ROI_ATTR()
            if TUCAM_Cap_GetROI(self._camera, pointer(roi_attr)) != TUCAMRET.TUCAMRET_SUCCESS:
                raise CameraError("Failed to get ROI")
            return (roi_attr.nHOffset, roi_attr.nVOffset, roi_attr.nWidth, roi_attr.nHeight)

    # =========================================================================
    # Acquisition Mode
    # =========================================================================

    def _set_acquisition_mode_imp(self, acquisition_mode: CameraAcquisitionMode):
        self._log.debug(f"Setting acquisition mode to {acquisition_mode}")
        with self._pause_streaming():
            if (
                not self._model_properties.is_genicam
                and TUCAM_Cap_GetTrigger(self._camera, pointer(self._trigger_attr)) != TUCAMRET.TUCAMRET_SUCCESS
            ):
                raise CameraError("Failed to get trigger attributes")
            if acquisition_mode == CameraAcquisitionMode.SOFTWARE_TRIGGER:
                if self._model_properties.is_genicam:
                    self._set_genicam_parameter("TriggerMode", 2, TUELEM_TYPE.TU_ElemEnumeration.value)
                    self._capture_mode_genicam = TUCAM_CAPTURE_MODES.TUCCM_TRIGGER_SOFTWARE.value
                else:
                    self._trigger_attr.nTgrMode = TUCAM_CAPTURE_MODES.TUCCM_TRIGGER_SOFTWARE.value
            elif acquisition_mode == CameraAcquisitionMode.CONTINUOUS:
                if self._model_properties.is_genicam:
                    self._set_genicam_parameter("TriggerMode", 0, TUELEM_TYPE.TU_ElemEnumeration.value)
                    self._capture_mode_genicam = TUCAM_CAPTURE_MODES.TUCCM_SEQUENCE.value
                else:
                    self._trigger_attr.nTgrMode = TUCAM_CAPTURE_MODES.TUCCM_SEQUENCE.value
            elif acquisition_mode == CameraAcquisitionMode.HARDWARE_TRIGGER:
                if self._model_properties.is_genicam:
                    self._set_genicam_parameter("TriggerMode", 1, TUELEM_TYPE.TU_ElemEnumeration.value)
                    self._capture_mode_genicam = TUCAM_CAPTURE_MODES.TUCCM_TRIGGER_STANDARD.value
                else:
                    self._trigger_attr.nTgrMode = TUCAM_CAPTURE_MODES.TUCCM_TRIGGER_STANDARD.value
            elif acquisition_mode == CameraAcquisitionMode.HARDWARE_TRIGGER_FIRST:
                if self._model_properties.is_genicam:
                    self._set_genicam_parameter("TriggerMode", 1, TUELEM_TYPE.TU_ElemEnumeration.value)
                    self._capture_mode_genicam = TUCAM_CAPTURE_MODES.TUCCM_TRIGGER_STANDARD.value
                else:
                    self._trigger_attr.nTgrMode = TUCAM_CAPTURE_MODES.TUCCM_TRIGGER_GLOBAL.value
            else:
                raise ValueError(f"Unhandled {acquisition_mode=}")
            if not self._model_properties.is_genicam:
                self._trigger_attr.nBufFrames = 1
                if TUCAM_Cap_SetTrigger(self._camera, self._trigger_attr) != TUCAMRET.TUCAMRET_SUCCESS:
                    raise CameraError("Failed to set acquisition mode")
            self._update_internal_settings()
            self.set_exposure_time(self._exposure_time_ms)

    def get_acquisition_mode(self) -> CameraAcquisitionMode:
        if self._model_properties.is_genicam:
            trigger_value = self._get_genicam_parameter("TriggerMode")["value"]
            if trigger_value == "Software":
                return CameraAcquisitionMode.SOFTWARE_TRIGGER
            if trigger_value == "FreeRunning":
                return CameraAcquisitionMode.CONTINUOUS
            if trigger_value == "Standard":
                return CameraAcquisitionMode.HARDWARE_TRIGGER
            raise ValueError(f"Unknown Tucsen GenICam trigger mode: {trigger_value}")
        trigger_attr = TUCAM_TRIGGER_ATTR()
        if TUCAM_Cap_GetTrigger(self._camera, pointer(trigger_attr)) != TUCAMRET.TUCAMRET_SUCCESS:
            raise CameraError("Failed to get acquisition mode")
        if trigger_attr.nTgrMode == TUCAM_CAPTURE_MODES.TUCCM_TRIGGER_SOFTWARE.value:
            return CameraAcquisitionMode.SOFTWARE_TRIGGER
        if trigger_attr.nTgrMode == TUCAM_CAPTURE_MODES.TUCCM_SEQUENCE.value:
            return CameraAcquisitionMode.CONTINUOUS
        if trigger_attr.nTgrMode == TUCAM_CAPTURE_MODES.TUCCM_TRIGGER_STANDARD.value:
            return CameraAcquisitionMode.HARDWARE_TRIGGER
        if trigger_attr.nTgrMode == TUCAM_CAPTURE_MODES.TUCCM_TRIGGER_GLOBAL.value:
            return CameraAcquisitionMode.HARDWARE_TRIGGER_FIRST
        raise ValueError(f"Unknown Tucsen trigger mode: {trigger_attr.nTgrMode=}")

    def set_temperature_reading_callback(self, func: Callable):
        self.temperature_reading_callback = func

    def set_temperature(self, temperature: float):
        t = temperature * 10 + 500
        if (
            TUCAM_Prop_SetValue(self._camera, TUCAM_IDPROP.TUIDP_TEMPERATURE.value, c_double(t), 0)
            != TUCAMRET.TUCAMRET_SUCCESS
        ):
            self._log.exception(f"Failed to set temperature to {temperature}C")
            raise

    def get_temperature(self) -> float:
        if self._model_properties.is_genicam:
            return self._get_genicam_parameter("DeviceTemperature")["value"]
        else:
            t = c_double(0)
            if (
                TUCAM_Prop_GetValue(self._camera, TUCAM_IDPROP.TUIDP_TEMPERATURE.value, pointer(t), 0)
                != TUCAMRET.TUCAMRET_SUCCESS
            ):
                self._log.exception("Failed to get temperature")
                raise
            return t.value

    def _check_temperature(self):
        while not self._terminate_temperature_event.is_set():
            time.sleep(2)
            try:
                temperature = self.get_temperature()
                if self.temperature_reading_callback is not None:
                    try:
                        self.temperature_reading_callback(temperature)
                    except Exception as ex:
                        self._log.exception(f"Temperature read callback failed: {ex}")
                        pass
            except Exception as e:
                self._log.exception(f"Failed to read temperature in callback: {e}")
                pass
    
    def set_trigger_duration_us(self, trigger_duration_us: int):
        self._trigger_duration_us = trigger_duration_us
        if self._model_properties.is_genicam:
            for port in TUCAM_OUTPUTTRG_PORT:
                self._set_genicam_parameter("TriggerPort", port.value, TUELEM_TYPE.TU_ElemInteger.value)
                self._set_genicam_parameter("TriggerOutputWidth", trigger_duration_us, TUELEM_TYPE.TU_ElemInteger.value)
        else:
            self._trigger_attr.nDelayTm = trigger_duration_us
        self._update_internal_settings()

    def send_trigger(self, illumination_time: Optional[float] = None):
        if self.get_acquisition_mode() == CameraAcquisitionMode.HARDWARE_TRIGGER and not self._hw_trigger_fn:
            raise CameraError("In HARDWARE_TRIGGER mode, but no hw trigger function given.")

        if not self.get_is_streaming():
            raise CameraError(f"Camera is not streaming, cannot send trigger.")

        if not self.get_ready_for_trigger():
            raise CameraError(
                f"Requested trigger too early (last trigger was {time.time() - self._last_trigger_timestamp} [s] ago), refusing."
            )
        if self.get_acquisition_mode() == CameraAcquisitionMode.HARDWARE_TRIGGER:
            self._hw_trigger_fn(illumination_time)
        elif self.get_acquisition_mode() == CameraAcquisitionMode.SOFTWARE_TRIGGER:
            if self._model_properties.is_genicam:
                self._set_genicam_parameter("TriggerSoftwarePulse", 1, TUELEM_TYPE.TU_ElemCommand.value)
            else:
                TUCAM_Cap_DoSoftwareTrigger(self._camera)
            self._last_trigger_timestamp = time.time()
            self._trigger_sent.set()

    def get_ready_for_trigger(self) -> bool:
        if time.time() - self._last_trigger_timestamp > 1.5 * ((self.get_total_frame_time() + 4) / 1000.0):
            self._trigger_sent.clear()
        return not self._trigger_sent.is_set()

    def set_auto_exposure(self, enable: bool = False):
        value = 1 if enable else 0
        if self._model_properties.is_genicam:
            self._set_genicam_parameter("ExposureAuto", value, TUELEM_TYPE.TU_ElemEnumeration.value)
        else:
            if (
                TUCAM_Capa_SetValue(self._camera, TUCAM_IDCAPA.TUIDC_ATEXPOSURE.value, value)
                != TUCAMRET.TUCAMRET_SUCCESS
            ):
                raise CameraError("Failed to set auto exposure")
        self._log.info("Auto exposure " + ("enabled" if enable else "disabled"))

    def set_auto_white_balance_gains(self, on: bool = False):
        raise NotImplementedError("White Balance Gains not implemented for the Tucsen driver.")

    def _raw_set_analog_gain_fl26bw(self, gain: float):
        # For FL26BW model
        # Gain0: System Gain (DN/e-): 1.28; Full Well Capacity (e-): 49000; Readout Noise (e-): 2.7(Median), 3.3(RMS)
        # Gain1: System Gain (DN/e-): 3.98; Full Well Capacity (e-): 15700; Readout Noise (e-): 1.0(Median), 1.3(RMS)
        # Gain2: System Gain (DN/e-): 8.0; Full Well Capacity (e-): 7800; Readout Noise (e-): 0.95(Median), 1.2(RMS)
        # Gain3: System Gain (DN/e-): 20; Full Well Capacity (e-): 3000; Readout Noise (e-): 0.85(Median), 1.0(RMS)
        if (
            TUCAM_Prop_SetValue(self._camera, TUCAM_IDPROP.TUIDP_GLOBALGAIN.value, c_double(gain), 0)
            != TUCAMRET.TUCAMRET_SUCCESS
        ):
            raise CameraError("Failed to set analog gain")

    def _raw_get_analog_gain_fl26bw(self) -> float:
        # For FL26BW model
        gain_value = c_double(0)
        if (
            TUCAM_Prop_GetValue(self._camera, TUCAM_IDPROP.TUIDP_GLOBALGAIN.value, pointer(gain_value), 0)
            != TUCAMRET.TUCAMRET_SUCCESS
        ):
            raise CameraError("Failed to get analog gain")

        return gain_value.value

    def _get_genicam_parameter(self, param_name: str) -> Dict[str, any]:
        """
        Get a GenICam parameter value and its attributes.

        Args:
            param_name: Name of the parameter (e.g., "ExposureTime", "AnalogGain")

        Returns:
            Dictionary containing parameter info including type, value, min, max, access rights, etc.

        Raises:
            CameraError: If the camera doesn't support GenICam or if parameter retrieval fails
        """
        if not self._model_properties.is_genicam:
            raise CameraError("This camera model does not support GenICam interface")

        # Element type names for logging
        elem_type_names = [
            "Value",
            "Base",
            "Integer",
            "Boolean",
            "Command",
            "Float",
            "String",
            "Register",
            "Category",
            "Enumeration",
            "EnumEntry",
            "Port",
        ]

        # Access mode names
        access_names = ["NI", "NA", "WO", "RO", "RW"]

        # Create element structure
        node = TUCAM_ELEMENT()
        node.pName = param_name.encode("utf-8")

        # Get element attributes
        result = TUCAM_GenICam_ElementAttr(self._camera, pointer(node), node.pName, TUXML_DEVICE.TU_CAMERA_XML.value)
        if result != TUCAMRET.TUCAMRET_SUCCESS:
            raise CameraError(f"Failed to get GenICam parameter attributes for '{param_name}'")

        # Prepare return dictionary
        param_info = {
            "name": param_name,
            "type": elem_type_names[node.Type] if node.Type < len(elem_type_names) else "Unknown",
            "type_value": node.Type,
            "access": access_names[node.Access] if node.Access < len(access_names) else "Unknown",
            "level": node.Level,
        }

        # Get value based on type
        elemtype = TUELEM_TYPE

        try:
            # Boolean type
            if node.Type == elemtype.TU_ElemBoolean.value:
                param_info["value"] = bool(node.uValue.Int64.nVal)
                param_info["min"] = 0
                param_info["max"] = 1

            # Integer or Command type
            elif node.Type in [elemtype.TU_ElemInteger.value, elemtype.TU_ElemCommand.value]:
                param_info["value"] = node.uValue.Int64.nVal
                param_info["min"] = node.uValue.Int64.nMin
                param_info["max"] = node.uValue.Int64.nMax

            # Float type
            elif node.Type == elemtype.TU_ElemFloat.value:
                param_info["value"] = node.uValue.Double.dbVal
                param_info["min"] = node.uValue.Double.dbMin
                param_info["max"] = node.uValue.Double.dbMax

            # String or Register type
            elif node.Type in [elemtype.TU_ElemString.value, elemtype.TU_ElemRegister.value]:
                # Allocate buffer for string value
                buf = create_string_buffer(node.uValue.Int64.nMax + 1)
                memset(buf, 0, node.uValue.Int64.nMax + 1)
                node.pTransfer = cast(buf, c_char_p)

                # Get the string value
                result = TUCAM_GenICam_GetElementValue(self._camera, pointer(node), TUXML_DEVICE.TU_CAMERA_XML.value)
                if result != TUCAMRET.TUCAMRET_SUCCESS:
                    raise CameraError(f"Failed to get string value for parameter '{param_name}'")

                param_info["value"] = node.pTransfer.decode("utf-8") if node.pTransfer else ""
                param_info["max_length"] = node.uValue.Int64.nMax

            # Enumeration type
            elif node.Type == elemtype.TU_ElemEnumeration.value:
                param_info["value_index"] = node.uValue.Int64.nVal
                param_info["min"] = node.uValue.Int64.nMin
                param_info["max"] = node.uValue.Int64.nMax

                # Get enum entries
                if node.pEntries:
                    strlist = ctypes.cast(node.pEntries, ctypes.POINTER(ctypes.c_char_p))
                    entries = []
                    num_entries = node.uValue.Int64.nMax - node.uValue.Int64.nMin + 1
                    for i in range(num_entries):
                        if strlist[i]:
                            entries.append(strlist[i].decode("utf-8"))
                    param_info["enum_entries"] = entries
                    param_info["value"] = (
                        entries[node.uValue.Int64.nVal] if 0 <= node.uValue.Int64.nVal < len(entries) else None
                    )

            else:
                param_info["value"] = None
                self._log.warning(f"Unsupported GenICam parameter type: {node.Type}")

        except Exception as e:
            self._log.exception(f"Error getting GenICam parameter '{param_name}': {e}")
            raise CameraError(f"Failed to get GenICam parameter '{param_name}': {str(e)}")

        return param_info

    def _set_genicam_parameter(self, param_name: str, value: any, param_type: int, log_info: bool = False) -> bool:
        """
        Set a GenICam parameter value.

        Args:
            param_name: Name of the parameter (e.g., "ExposureTime", "AnalogGain")
            value: Value to set (type depends on parameter)
            param_type: Parameter type from TUELEM_TYPE (e.g., TU_ElemFloat)

        Returns:
            True if successful

        Raises:
            CameraError: If the camera doesn't support GenICam or if parameter setting fails

        Example:
            from control.TUCam import TUELEM_TYPE

            camera.set_genicam_parameter("ExposureTime", 5.0, TUELEM_TYPE.TU_ElemFloat.value)
            camera.set_genicam_parameter("BlackLevel", 100, TUELEM_TYPE.TU_ElemInteger.value)
            camera.set_genicam_parameter("ReverseX", True, TUELEM_TYPE.TU_ElemBoolean.value)
            camera.set_genicam_parameter("AnalogGain", 1, TUELEM_TYPE.TU_ElemEnumeration.value)
        """
        if not self._model_properties.is_genicam:
            raise CameraError("This camera model does not support GenICam interface")

        # Element type names for logging
        elem_type_names = [
            "Value",
            "Base",
            "Integer",
            "Boolean",
            "Command",
            "Float",
            "String",
            "Register",
            "Category",
            "Enumeration",
            "EnumEntry",
            "Port",
        ]

        # Create element structure
        node = TUCAM_ELEMENT()
        node.pName = param_name.encode("utf-8")
        node.Type = param_type

        elemtype = TUELEM_TYPE

        try:
            # Boolean type
            if node.Type == elemtype.TU_ElemBoolean.value:
                node.uValue.Int64.nVal = 1 if value else 0
                result = TUCAM_GenICam_SetElementValue(self._camera, pointer(node), TUXML_DEVICE.TU_CAMERA_XML.value)
                if result != TUCAMRET.TUCAMRET_SUCCESS:
                    raise CameraError(f"Failed to set boolean parameter '{param_name}'")

            # Command type
            elif node.Type == elemtype.TU_ElemCommand.value:
                node.uValue.Int64.nVal = int(value)
                result = TUCAM_GenICam_SetElementValue(self._camera, pointer(node), TUXML_DEVICE.TU_CAMERA_XML.value)
                if result != TUCAMRET.TUCAMRET_SUCCESS:
                    raise CameraError(f"Failed to execute command '{param_name}'")

            # Integer type
            elif node.Type == elemtype.TU_ElemInteger.value:
                node.uValue.Int64.nVal = int(value)
                result = TUCAM_GenICam_SetElementValue(self._camera, pointer(node), TUXML_DEVICE.TU_CAMERA_XML.value)
                if result != TUCAMRET.TUCAMRET_SUCCESS:
                    raise CameraError(f"Failed to set integer parameter '{param_name}'")

            # Float type
            elif node.Type == elemtype.TU_ElemFloat.value:
                node.uValue.Double.dbVal = float(value)

                result = TUCAM_GenICam_SetElementValue(self._camera, pointer(node), TUXML_DEVICE.TU_CAMERA_XML.value)
                if result != TUCAMRET.TUCAMRET_SUCCESS:
                    raise CameraError(f"Failed to set float parameter '{param_name}'")

            # String or Register type
            elif node.Type in [elemtype.TU_ElemString.value, elemtype.TU_ElemRegister.value]:
                # Convert value to bytes and set
                node.pTransfer = str(value).encode("utf-8")
                result = TUCAM_GenICam_SetElementValue(self._camera, pointer(node), TUXML_DEVICE.TU_CAMERA_XML.value)
                if result != TUCAMRET.TUCAMRET_SUCCESS:
                    raise CameraError(f"Failed to set string parameter '{param_name}'")

            # Enumeration type
            elif node.Type == elemtype.TU_ElemEnumeration.value:
                # For enums without querying, we can only set by index
                if not isinstance(value, int):
                    raise ValueError(
                        f"When setting enum parameter '{param_name}' without querying, value must be an integer index"
                    )

                node.uValue.Int64.nVal = int(value)

                result = TUCAM_GenICam_SetElementValue(self._camera, pointer(node), TUXML_DEVICE.TU_CAMERA_XML.value)
                if result != TUCAMRET.TUCAMRET_SUCCESS:
                    raise CameraError(f"Failed to set enum parameter '{param_name}'")
            else:
                raise ValueError(f"Unsupported GenICam parameter type: {node.Type}")

        except Exception as e:
            if isinstance(e, CameraError):
                raise
            self._log.exception(f"Error setting GenICam parameter '{param_name}': {e}")
            raise CameraError(f"Failed to set GenICam parameter '{param_name}': {str(e)}")

        if log_info:
            self._log.info(f"[{elem_type_names[node.Type]}] Set {param_name} = {value}")
        
        time.sleep(0.1) # Sleep to avoid sequential parameter setting from causing errors
        return True
