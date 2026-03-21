import enum
import math
from typing import Any, Dict, List, Optional, Tuple, Union

import pydantic

from control.utils import FlipVariant
from control.models import DeviceEntry, MachineConfig


class FilterWheelControllerVariant(enum.Enum):
    SQUID = "SQUID"
    ZABER = "ZABER"
    OPTOSPIN = "OPTOSPIN"
    DRAGONFLY = "DRAGONFLY"
    XLIGHT = "XLIGHT"

    @staticmethod
    def from_string(filter_wheel_controller_string: str) -> Optional["FilterWheelControllerVariant"]:
        """
        Attempts to convert the given string to a filter wheel controller variant.  This ignores all letter cases.
        """
        try:
            return FilterWheelControllerVariant[filter_wheel_controller_string.upper()]
        except KeyError:
            return None


class SquidFilterWheelConfig(pydantic.BaseModel):
    """Configuration for SQUID filter wheel controller."""

    max_index: int
    min_index: int
    offset: float
    motor_slot_index: int
    transitions_per_revolution: int


class ZaberFilterWheelConfig(pydantic.BaseModel):
    """Configuration for Zaber filter wheel controller."""

    serial_number: str
    delay_ms: int
    blocking_call: bool


class OptospinFilterWheelConfig(pydantic.BaseModel):
    """Configuration for Optospin filter wheel controller."""

    serial_number: str
    speed_hz: int
    delay_ms: int
    ttl_trigger: bool


class FilterWheelConfig(pydantic.BaseModel):
    """
    Configuration for filter wheel controller system.
    """

    # The type of filter wheel controller
    controller_type: FilterWheelControllerVariant

    # List of filter wheel indices to use (e.g., [1] for single wheel, [1, 2, 3, 4] for Optospin with 4 wheels)
    indices: list[int]

    # Controller-specific configuration (single config for backward compatibility)
    controller_config: Optional[Union[SquidFilterWheelConfig, ZaberFilterWheelConfig, OptospinFilterWheelConfig]] = None

    # Per-wheel configs for multi-wheel setups (wheel_id -> config)
    # Used by SQUID multi-wheel support
    squid_wheel_configs: Optional[Dict[int, SquidFilterWheelConfig]] = None


def _primary_camera_id_for_bindings(repo: Any) -> int:
    """First registered camera id for hardware bindings (defaults to 1)."""
    cr = repo.get_camera_registry()
    if cr and cr.cameras:
        ids: List[int] = [c.id for c in cr.cameras if c.id is not None]
        if ids:
            return min(ids)
    return 1 # Should this be 0?


def _nested_squid_wheel_cfg(sq_map: Any, wheel_id: int) -> Dict[str, Any]:
    if not isinstance(sq_map, dict):
        return {}
    if wheel_id in sq_map:
        v = sq_map[wheel_id]
        return v if isinstance(v, dict) else {}
    s = str(wheel_id)
    if s in sq_map:
        v = sq_map[s]
        return v if isinstance(v, dict) else {}
    return {}


def _build_filter_wheel_config_from_machine(mc: MachineConfig) -> Optional[FilterWheelConfig]:
    """Build runtime filter wheel config from ``machine_config`` + ConfigRepository.

    Requires ``devices.emission_filter_wheel`` enabled and a resolvable emission wheel
    from the filter wheel registry / bindings (see ConfigRepository).
    """
    from control.core.config.repository import ConfigRepository
    from control.models import FilterWheelType

    dev = mc.get_device("emission_filter_wheel")
    if dev is None or not dev.enabled:
        return None

    repo = ConfigRepository()
    cam_id = _primary_camera_id_for_bindings(repo)
    ew = repo.get_effective_emission_wheel(camera_id=cam_id)
    if ew is None:
        ew = repo.get_effective_emission_wheel(camera_id=0)
    if ew is None or ew.type != FilterWheelType.EMISSION:
        return None

    cfg = dev.config or {}
    ctype_str = str(cfg.get("controller_type", "SQUID")).upper()
    try:
        ctype = FilterWheelControllerVariant[ctype_str]
    except KeyError:
        ctype = FilterWheelControllerVariant.SQUID

    indices = cfg.get("indices")
    if not indices:
        reg = repo.get_filter_wheel_registry()
        if reg:
            em_wheels = [w for w in reg.filter_wheels if w.type == FilterWheelType.EMISSION]
            if len(em_wheels) > 1:
                indices = []
                for i, w in enumerate(em_wheels):
                    if w.id is not None:
                        indices.append(w.id)
                    else:
                        indices.append(i + 1)
            else:
                indices = [ew.id if ew.id is not None else 1]
        else:
            indices = [ew.id if ew.id is not None else 1]
    else:
        indices = list(indices)

    serial_from_dev = dev.connection.serial_number if dev.connection else None

    if ctype == FilterWheelControllerVariant.ZABER:
        sn = serial_from_dev or str(cfg.get("serial_number", ""))
        return FilterWheelConfig(
            controller_type=ctype,
            indices=indices,
            controller_config=ZaberFilterWheelConfig(
                serial_number=sn,
                delay_ms=int(cfg.get("delay_ms", 70)),
                blocking_call=bool(cfg.get("blocking_call", False)),
            ),
        )

    if ctype == FilterWheelControllerVariant.OPTOSPIN:
        sn = serial_from_dev or str(cfg.get("serial_number", ""))
        return FilterWheelConfig(
            controller_type=ctype,
            indices=indices,
            controller_config=OptospinFilterWheelConfig(
                serial_number=sn,
                speed_hz=int(cfg.get("speed_hz", 50)),
                delay_ms=int(cfg.get("delay_ms", 70)),
                ttl_trigger=bool(cfg.get("ttl_trigger", False)),
            ),
        )

    def _find_wheel_def(wheel_id: int):
        reg = repo.get_filter_wheel_registry()
        if not reg:
            if len(indices) == 1 and (ew.id == wheel_id or (ew.id is None and wheel_id == 1)):
                return ew
            return None
        for w in reg.filter_wheels:
            if w.type != FilterWheelType.EMISSION:
                continue
            wid = w.id if w.id is not None else 1
            if wid == wheel_id:
                return w
        return None

    def _squid_cfg_for_wheel(wheel_id: int) -> SquidFilterWheelConfig:
        wheel_def = _find_wheel_def(wheel_id)
        pos = (wheel_def.positions if wheel_def else None) or (
            ew.positions if len(indices) == 1 else None
        ) or {}
        min_i = min(pos.keys()) if pos else 1
        max_i = max(pos.keys()) if pos else 8
        sq_map = cfg.get("squid_wheel_configs")
        wcfg = _nested_squid_wheel_cfg(sq_map, wheel_id)
        default_slot = 3 if wheel_id == 1 else 4
        return SquidFilterWheelConfig(
            max_index=int(wcfg.get("max_index", max_i)),
            min_index=int(wcfg.get("min_index", min_i)),
            offset=float(wcfg.get("offset", cfg.get("offset", 0.008))),
            motor_slot_index=int(wcfg.get("motor_slot_index", cfg.get("motor_slot_index", default_slot))),
            transitions_per_revolution=int(
                wcfg.get("transitions_per_revolution", cfg.get("transitions_per_revolution", 4000))
            ),
        )

    if len(indices) == 1:
        sc = _squid_cfg_for_wheel(indices[0])
        return FilterWheelConfig(
            controller_type=ctype,
            indices=indices,
            controller_config=sc,
            squid_wheel_configs={indices[0]: sc},
        )

    squid_cfgs = {wid: _squid_cfg_for_wheel(wid) for wid in indices}
    return FilterWheelConfig(
        controller_type=ctype,
        indices=indices,
        squid_wheel_configs=squid_cfgs,
    )


_filter_wheel_config: Optional[FilterWheelConfig] = None


def get_filter_wheel_config() -> Optional[FilterWheelConfig]:
    """
    Returns the active FilterWheelConfig after ``reconfigure_from_machine_config``,
    or None if no emission filter wheel is configured.
    """
    return _filter_wheel_config


class DirectionSign(enum.IntEnum):
    DIRECTION_SIGN_POSITIVE = 1
    DIRECTION_SIGN_NEGATIVE = -1


class PIDConfig(pydantic.BaseModel):
    ENABLED: bool
    P: float
    I: float
    D: float


class AxisConfig(pydantic.BaseModel):
    MOVEMENT_SIGN: DirectionSign
    USE_ENCODER: bool
    ENCODER_SIGN: DirectionSign
    # If this is a linear axis, this is the distance the axis must move to see 1 encoder step.  If this
    # is a rotary axis, this is the radians travelled by the axis to see 1 encoder step.
    ENCODER_STEP_SIZE: float
    FULL_STEPS_PER_REV: float

    # For linear axes, this is the mm traveled by the axis when 1 full step is taken by the motor.  For rotary
    # axes, this is the rad traveled by the axis when 1 full step is taken by the motor.
    SCREW_PITCH: float

    # The number of microsteps per full step the axis uses (or should use if we can set it).
    # If MICROSTEPS_PER_STEP == 8, and SCREW_PITCH=2, then in 8 commanded steps the motor will do 1 full
    # step and so will travel a distance of 2.
    MICROSTEPS_PER_STEP: int

    # The Max speed the axis is allowed to travel in denoted in its native units.  This means mm/s for
    # linear axes, and radians/s for rotary axes.
    MAX_SPEED: float
    MAX_ACCELERATION: float

    # The min and maximum position of this axis in its native units.  This means mm for linear axes, and
    # radians for rotary.  `inf` is allowed (for something like a continuous rotary axis)
    MIN_POSITION: float
    MAX_POSITION: float

    # Some axes have a PID controller.  This says whether or not to use the PID control loop, and if so what
    # gains to use.
    PID: Optional[PIDConfig]

    def convert_to_real_units(self, usteps: float):
        if self.USE_ENCODER:
            return usteps * self.MOVEMENT_SIGN.value * self.ENCODER_STEP_SIZE * self.ENCODER_SIGN.value
        else:
            return (
                usteps
                * self.MOVEMENT_SIGN.value
                * self.SCREW_PITCH
                / (self.MICROSTEPS_PER_STEP * self.FULL_STEPS_PER_REV)
            )

    def convert_real_units_to_ustep(self, real_unit: float):
        return round(
            real_unit
            / (self.MOVEMENT_SIGN.value * self.SCREW_PITCH / (self.MICROSTEPS_PER_STEP * self.FULL_STEPS_PER_REV))
        )


class StageConfig(pydantic.BaseModel):
    X_AXIS: AxisConfig
    Y_AXIS: AxisConfig
    Z_AXIS: AxisConfig
    THETA_AXIS: AxisConfig


def _default_stage_config() -> StageConfig:
    """Build a generic stage config used before MachineConfig is applied."""

    def _axis_default() -> AxisConfig:
        return AxisConfig(
            MOVEMENT_SIGN=DirectionSign.DIRECTION_SIGN_POSITIVE,
            USE_ENCODER=False,
            ENCODER_SIGN=DirectionSign.DIRECTION_SIGN_POSITIVE,
            ENCODER_STEP_SIZE=100e-6,
            FULL_STEPS_PER_REV=200,
            SCREW_PITCH=1.0,
            MICROSTEPS_PER_STEP=8,
            MAX_SPEED=25.0,
            MAX_ACCELERATION=500.0,
            MIN_POSITION=-0.5,
            MAX_POSITION=56.0,
            PID=None,
        )

    return StageConfig(
        X_AXIS=_axis_default(),
        Y_AXIS=_axis_default(),
        Z_AXIS=_axis_default(),
        THETA_AXIS=AxisConfig(
            MOVEMENT_SIGN=DirectionSign.DIRECTION_SIGN_POSITIVE,
            USE_ENCODER=False,
            ENCODER_SIGN=DirectionSign.DIRECTION_SIGN_POSITIVE,
            ENCODER_STEP_SIZE=1.0,
            FULL_STEPS_PER_REV=200,
            SCREW_PITCH=2.0 * math.pi / 200,
            MICROSTEPS_PER_STEP=256,
            MAX_SPEED=2.0 * math.pi / 4,
            MAX_ACCELERATION=500.0,
            MIN_POSITION=0.0,
            MAX_POSITION=2.0 * math.pi / 4,
            PID=None,
        ),
    )


_stage_config = _default_stage_config()


def get_stage_config() -> StageConfig:
    """
    Returns the StageConfig that existed at process startup.
    """
    return _stage_config


class CameraVariant(enum.Enum):
    TOUPCAM = "TOUPCAM"
    FLIR = "FLIR"
    HAMAMATSU = "HAMAMATSU"
    IDS = "IDS"
    TUCSEN = "TUCSEN"
    PHOTOMETRICS = "PHOTOMETRICS"
    TIS = "TIS"
    GXIPY = "GXIPY"
    ANDOR = "ANDOR"
    RETIGA = "RETIGA"

    @staticmethod
    def from_string(cam_string: str) -> Optional["CameraVariant"]:
        """
        Attempts to convert the given string to a camera variant.  This ignores all letter cases.
        """
        try:
            return CameraVariant[cam_string.upper()]
        except KeyError:
            return None


class GxipyCameraModel(enum.Enum):
    MER2_1220_32U3M = "MER2-1220-32U3M"
    MER2_1220_32U3C = "MER2-1220-32U3C"
    MER2_630_60U3M = "MER2-630-60U3M"

    @staticmethod
    def from_string(cam_string: str) -> Optional["GxipyCameraModel"]:
        """
        Attempts to convert the given string to a Gxipy camera model.  This ignores all letter cases.
        """
        try:
            return GxipyCameraModel[cam_string.upper()]
        except KeyError:
            return None


class FLIRCameraModel(enum.Enum):
    BFS_U3_63S4M_C = "BFS-U3-63S4M-C"

    @staticmethod
    def from_string(cam_string: str) -> Optional["FLIRCameraModel"]:
        """
        Attempts to convert the given string to a FLIR camera model.  This ignores all letter cases.
        """
        try:
            return FLIRCameraModel[cam_string.upper()]
        except KeyError:
            return None


class ToupcamCameraModel(enum.Enum):
    ITR3CMOS26000KMA = "ITR3CMOS26000KMA"
    ITR3CMOS09000KMA = "ITR3CMOS09000KMA"
    ITR3CMOS26000KPA = "ITR3CMOS26000KPA"

    @staticmethod
    def from_string(cam_string: str) -> Optional["ToupcamCameraModel"]:
        """
        Attempts to convert the given string to a Toupcam camera model.  This ignores all letter cases.
        """
        try:
            return ToupcamCameraModel[cam_string.upper()]
        except KeyError:
            return None


class TucsenCameraModel(enum.Enum):
    FL26_BW = "FL26-BW"
    DHYANA_400BSI_V3 = "DHYANA-400BSI-V3"
    ARIES_6506 = "ARIES-6506"
    ARIES_6510 = "ARIES-6510"
    LIBRA_25 = "LIBRA-25"
    LIBRA_22 = "LIBRA-22"

    @staticmethod
    def from_string(cam_string: str) -> Optional["TucsenCameraModel"]:
        """
        Attempts to convert the given string to a Tucsen camera model.  This ignores all letter cases.
        """
        try:
            return TucsenCameraModel[cam_string.upper()]
        except KeyError:
            return None


class HamamatsuCameraModel(enum.Enum):
    C15440_20UP = "C15440-20UP"
    C14440_20UP = "C14440-20UP"

    @staticmethod
    def from_string(cam_string: str) -> Optional["HamamatsuCameraModel"]:
        """
        Attempts to convert the given string to a Hamamatsu camera model.  This ignores all letter cases.
        """
        try:
            return HamamatsuCameraModel[cam_string.upper()]
        except KeyError:
            return None


class PhotometricsCameraModel(enum.Enum):
    """Photometrics camera models supported by the system."""
    KINETIX = "KINETIX"
    KINETIX_22 = "KINETIX_22"  # Alias for KINETIX
    PRIME_BSI_EXPRESS = "PRIME_BSI_EXPRESS"

    @staticmethod
    def from_string(cam_string: str) -> Optional["PhotometricsCameraModel"]:
        """
        Attempts to convert the given string to a Photometrics camera model.
        This ignores letter cases and handles common naming variations.
        """
        if cam_string is None:
            return None
        try:
            # Normalize the string: uppercase, replace hyphens/spaces with underscores
            normalized = cam_string.upper().replace("-", "_").replace(" ", "_")
            # Handle common aliases
            if normalized in ("PRIME_BSI_EXPRESS", "BSIEXPRESS", "BSI_EXPRESS", "PRIMEBSIEXPRESS"):
                normalized = "PRIME_BSI_EXPRESS"
            elif normalized in ("KINETIX22", "KINETIX_22"):
                normalized = "KINETIX_22"  # Map to base KINETIX
            elif normalized in ("KINETIX"):
                normalized = "KINETIX"
            return PhotometricsCameraModel[normalized]
        except KeyError:
            return None


class AndorCameraModel(enum.Enum):
    ZYLA_4_2P_USB3_C = "ZYLA-4.2P-USB3-C"  # ZL41 Cell 4.2

    @staticmethod
    def from_string(cam_string: str) -> Optional["AndorCameraModel"]:
        """
        Attempts to convert the given string to an Andor camera model.  This ignores all letter cases.
        """
        try:
            return AndorCameraModel[cam_string.upper()]
        except KeyError:
            return None


class RetigaCameraModel(enum.Enum):
    """Teledyne QImaging Retiga camera models."""
    RETIGA_ELECTRO = "RETIGA-ELECTRO"  # Standard Retiga Electro
    RETIGA_ELECTRO_SRV = "RETIGA-ELECTRO-SRV"  # Retiga Electro SRV (smaller pixel size)

    @staticmethod
    def from_string(cam_string: str) -> Optional["RetigaCameraModel"]:
        """
        Attempts to convert the given string to a Retiga camera model.  This ignores all letter cases.
        """
        try:
            return RetigaCameraModel[cam_string.upper().replace("-", "_")]
        except KeyError:
            return None

class FLIRCameraModel(enum.Enum):
    """Teledyne FLIR Blackfly S camera models."""
    BFS_U3_23S3M = "BFS-U3-23S3M"
    BFS_U3_23S4M = "BFS-U3-23S4M"
    BFS_U3_23S5M = "BFS-U3-23S5M"
    BFS_U3_23S6M = "BFS-U3-23S6M"

    @staticmethod
    def from_string(cam_string: str) -> Optional["FLIRCameraModel"]:
        """
        Attempts to convert the given string to a FLIR camera model.  This ignores all letter cases.
        """
        try:
            return FLIRCameraModel[cam_string.upper().replace("-", "_")]
        except KeyError:
            return None


class CameraSensor(enum.Enum):
    """
    Some camera sensors may not be included here.
    """

    IMX290 = "IMX290"
    IMX178 = "IMX178"
    IMX226 = "IMX226"
    IMX250 = "IMX250"
    IMX252 = "IMX252"
    IMX273 = "IMX273"
    IMX264 = "IMX264"
    IMX265 = "IMX265"
    IMX571 = "IMX571"
    IMX392 = "IMX392"
    ICX825 = "ICX825"
    PYTHON300 = "PYTHON300"


class CameraPixelFormat(enum.Enum):
    """
    This is all known Pixel Formats in the Cephla world, but not all cameras will support
    all of these.
    """

    MONO8 = "MONO8"
    MONO10 = "MONO10"
    MONO12 = "MONO12"
    MONO14 = "MONO14"
    MONO16 = "MONO16"
    RGB24 = "RGB24"
    RGB32 = "RGB32"
    RGB48 = "RGB48"
    BAYER_RG8 = "BAYER_RG8"
    BAYER_RG12 = "BAYER_RG12"

    @staticmethod
    def is_color_format(pixel_format):
        return pixel_format in (
            CameraPixelFormat.RGB24,
            CameraPixelFormat.RGB32,
            CameraPixelFormat.RGB48,
            CameraPixelFormat.BAYER_RG8,
            CameraPixelFormat.BAYER_RG12,
        )

    @staticmethod
    def from_string(pixel_format_string):
        return CameraPixelFormat[pixel_format_string]


class CameraReadoutMode(enum.Enum):
    """
    Readout modes for scientific cameras. Different cameras may support different subsets of these modes.
    
    - GLOBAL: Global shutter mode where all pixels are exposed and read out simultaneously
    - ROLLING: Rolling shutter mode where pixels are exposed and read out row by row
    - ROLLING_WITH_GLOBAL_RESET: Rolling shutter with global reset, where all pixels start exposure
      simultaneously but are read out row by row
    """
    GLOBAL = "GLOBAL"
    ROLLING = "ROLLING"
    ROLLING_WITH_GLOBAL_RESET = "ROLLING_WITH_GLOBAL_RESET"


class RGBValue(pydantic.BaseModel):
    r: float
    g: float
    b: float


# TODO/NOTE(imo): We may need to add a model attrib here.
class CameraConfig(pydantic.BaseModel):
    """
    Most camera parameters are runtime configurable, so CameraConfig is more about defining what
    camera must be available and used for a particular function in the system.

    If we want to capture the settings a camera used for a particular capture, another model called
    CameraState, or something, might be more appropriate.
    """

    # NOTE(imo): Not "type" because that's a python builtin and can cause confusion
    camera_type: CameraVariant

    # Specific camera model. This will be used to determine the model-specific parameters, because one camera class may
    # support multiple models from the same brand.
    camera_model: Optional[
        Union[
            FLIRCameraModel,
            GxipyCameraModel,
            TucsenCameraModel,
            ToupcamCameraModel,
            HamamatsuCameraModel,
            PhotometricsCameraModel,
            AndorCameraModel,
            RetigaCameraModel,
            FLIRCameraModel,
        ]
    ] = None

    # The serial number of the camera. You may use this to select a specific camera to open if there are multiple
    # cameras using the same SDK/driver.
    serial_number: Optional[str] = None

    # The default readout data bit depth of the camera. Note that this may depend on the gain mode being used.
    default_pixel_format: CameraPixelFormat

    # The binning factor of the camera.  If None, the camera is not using binning, or use 1x1 as default.
    default_binning: Optional[Tuple[int, int]] = None

    # The default ROI of the camera for hardware cropping. Input should be: offset_x, offset_y, width, height
    default_roi: Optional[Tuple[Optional[int], Optional[int], Optional[int], Optional[int]]] = None

    # The angle the camera should rotate this image right as it comes off the camera,
    # and before giving it to the rest of the system.
    #
    # NOTE(imo): As of 2025-feb-17, this feature is inconsistently implemented!
    rotate_image_angle: Optional[float] = None

    # After rotation, the flip we should do to the image.
    #
    # NOTE(imo): As of 2025-feb-17, this feature is inconsistently implemented!
    flip: Optional[FlipVariant] = None

    # The width of the crop region of the camera. This will be used for cropping the image in software. Value should be relative to the unbinned image size.
    crop_width: Optional[int] = None

    # The height of the crop region of the camera. This will be used for cropping the image in software. Value should be relative to the unbinned image size.
    crop_height: Optional[int] = None

    # Set the temperature of the camera to this value once on initialization.
    default_temperature: Optional[float] = None

    # Set the fan speed of the camera to this value once on initialization.
    default_fan_speed: Optional[int] = None

    # Set the black level of the camera to this value once on initialization.
    default_black_level: Optional[int] = None

    # After initialization, set the white balance gains to this once. Only valid for color cameras.
    default_white_balance_gains: Optional[RGBValue] = None

    # Set the hardware trigger mode of the camera to this value once on initialization.
    hardware_triggering_enabled: Optional[bool] = None

    # Set the readout mode of the camera to this value once on initialization.
    # If None, the camera will use its default readout mode or the mode will be set from _def.py.
    default_readout_mode: Optional[str] = None  # String representation to avoid circular imports


def _old_camera_variant_to_enum(old_string) -> CameraVariant:
    if old_string == "Toupcam":
        return CameraVariant.TOUPCAM
    elif old_string == "FLIR":
        return CameraVariant.FLIR
    elif old_string == "Hamamatsu":
        return CameraVariant.HAMAMATSU
    elif old_string == "iDS":
        return CameraVariant.IDS
    elif old_string == "TIS":
        return CameraVariant.TIS
    elif old_string == "Tucsen":
        return CameraVariant.TUCSEN
    elif old_string == "Photometrics":
        return CameraVariant.PHOTOMETRICS
    elif old_string == "Andor":
        return CameraVariant.ANDOR
    elif old_string == "Retiga":
        return CameraVariant.RETIGA
    elif old_string == "Default":
        return CameraVariant.GXIPY
    raise ValueError(f"Unknown old camera type {old_string=}")


def _default_camera_config() -> CameraConfig:
    """Build a generic main camera config used before MachineConfig is applied."""

    return CameraConfig(
        camera_type=CameraVariant.GXIPY,
        camera_model=None,
        default_pixel_format=CameraPixelFormat.MONO12,
        default_binning=(1, 1),
        default_roi=None,
        rotate_image_angle=None,
        flip=None,
        crop_width=None,
        crop_height=None,
        default_temperature=None,
        default_fan_speed=None,
        default_black_level=None,
        default_white_balance_gains=None,
        hardware_triggering_enabled=True,
        default_readout_mode=None,
    )


_camera_config = _default_camera_config()


def get_camera_config() -> CameraConfig:
    """Returns the CameraConfig (may be rebuilt after ``reconfigure_from_machine_config``)."""
    return _camera_config


def _default_autofocus_camera_config() -> CameraConfig:
    """Build a generic autofocus camera config used before MachineConfig is applied."""

    return CameraConfig(
        camera_type=CameraVariant.GXIPY,
        camera_model=None,
        default_pixel_format=CameraPixelFormat.MONO8,
        default_binning=(1, 1),
        default_roi=None,
        rotate_image_angle=None,
        flip=None,
        crop_width=None,
        crop_height=None,
        default_temperature=None,
        default_fan_speed=None,
        default_black_level=None,
        default_white_balance_gains=None,
        hardware_triggering_enabled=True,
        default_readout_mode=None,
    )


_autofocus_camera_config = _default_autofocus_camera_config()


def get_autofocus_camera_config() -> CameraConfig:
    """Returns the CameraConfig for the laser autofocus camera."""
    return _autofocus_camera_config


# ═══════════════════════════════════════════════════════════════════════════════
# MachineConfig bridge — rebuild configs from the unified YAML
# ═══════════════════════════════════════════════════════════════════════════════

def reconfigure_from_machine_config(mc: "MachineConfig") -> None:  # noqa: F821 (forward ref)
    """Rebuild camera, stage, and filter wheel singletons from a :class:`MachineConfig`.

    Called by ``apply_machine_config()`` after _def.py globals have been
    populated.  This ensures that ``get_camera_config()``,
    ``get_stage_config()``, and ``get_filter_wheel_config()`` return values
    derived from ``machine_config.yaml`` rather than import-time defaults.
    """
    global _camera_config, _autofocus_camera_config, _stage_config, _filter_wheel_config

    _filter_wheel_config = _build_filter_wheel_config_from_machine(mc)

    main_cam = mc.get_device("main_camera")
    if main_cam and main_cam.enabled:
        _camera_config = _build_camera_config_from_device(main_cam)

    focus_cam = mc.get_device("focus_camera")
    if focus_cam and focus_cam.enabled:
        _autofocus_camera_config = _build_camera_config_from_device(
            focus_cam,
            default_pixel_format=CameraPixelFormat.MONO8,
            default_binning=(1, 1),
        )

    stage_dev = mc.get_device("stage")
    if stage_dev and stage_dev.enabled:
        _stage_config = _build_stage_config_from_device(stage_dev)


def _build_camera_config_from_device(
    dev: "DeviceEntry",  # noqa: F821
    default_pixel_format: Optional[CameraPixelFormat] = None,
    default_binning: Optional[Tuple[int, int]] = None,
) -> CameraConfig:
    """Build a CameraConfig from a MachineConfig DeviceEntry."""
    from control.utils import FlipVariant

    driver = dev.driver
    legacy_type_map = {
        "toupcam": "Toupcam", "daheng": "Default", "flir": "FLIR",
        "hamamatsu": "Hamamatsu", "tucsen": "Tucsen", "photometrics": "Photometrics",
        "andor_camera": "Andor", "retiga": "Retiga", "ids": "iDS", "tis": "TIS",
    }
    cam_type = _old_camera_variant_to_enum(legacy_type_map.get(driver, "Default"))
    cfg = dev.config
    model_str = cfg.get("model")

    pixel_fmt = default_pixel_format
    if pixel_fmt is None:
        pf_str = cfg.get("pixel_format", "MONO12")
        try:
            pixel_fmt = CameraPixelFormat[pf_str]
        except KeyError:
            pixel_fmt = CameraPixelFormat.MONO12

    binning = default_binning
    if binning is None:
        b = cfg.get("binning", 1)
        binning = (b, b)

    roi_dict = cfg.get("roi")
    roi = None
    if roi_dict:
        roi = (
            roi_dict.get("offset_x"), roi_dict.get("offset_y"),
            roi_dict.get("width"), roi_dict.get("height"),
        )

    crop_dict = cfg.get("crop")
    crop_w = crop_dict.get("width") if crop_dict else None
    crop_h = crop_dict.get("height") if crop_dict else None

    flip_str = cfg.get("flip")
    flip = None
    if flip_str:
        try:
            flip = FlipVariant(flip_str)
        except (ValueError, KeyError):
            pass

    wb = cfg.get("awb_ratios")
    wb_gains = None
    if wb:
        wb_gains = RGBValue(r=wb.get("r", 1), g=wb.get("g", 1), b=wb.get("b", 1))

    return CameraConfig(
        camera_type=cam_type,
        camera_model=model_str,
        serial_number=dev.connection.serial_number if dev.connection else None,
        default_pixel_format=pixel_fmt,
        default_binning=binning,
        default_roi=roi,
        rotate_image_angle=cfg.get("rotate_angle"),
        flip=flip,
        crop_width=crop_w,
        crop_height=crop_h,
        default_temperature=cfg.get("temperature"),
        default_fan_speed=cfg.get("fan_speed"),
        default_black_level=cfg.get("black_level"),
        default_white_balance_gains=wb_gains,
        hardware_triggering_enabled=cfg.get("hardware_triggering_enabled", True),
        default_readout_mode=cfg.get("readout_mode"),
    )


def _build_stage_config_from_device(dev: "DeviceEntry") -> StageConfig:  # noqa: F821
    """Build a StageConfig from a MachineConfig stage DeviceEntry."""
    cfg = dev.config

    def _axis(axis_key: str, defaults: dict) -> AxisConfig:
        a = cfg.get(axis_key, {})
        enc_cfg = cfg.get("encoders", {}).get(axis_key, {})
        pid_cfg = cfg.get("pid", {}).get(axis_key, {})
        limits = cfg.get("software_limits", {}).get(axis_key, {})

        return AxisConfig(
            MOVEMENT_SIGN=a.get("movement_sign", defaults.get("movement_sign", 1)),
            USE_ENCODER=enc_cfg.get("enabled", False),
            ENCODER_SIGN=a.get("encoder_sign", 1),
            ENCODER_STEP_SIZE=a.get("encoder_step_size", defaults.get("encoder_step_size", 100e-6)),
            FULL_STEPS_PER_REV=a.get("fullsteps_per_rev", defaults.get("fullsteps_per_rev", 200)),
            SCREW_PITCH=a.get("screw_pitch_mm", defaults.get("screw_pitch_mm", 1)),
            MICROSTEPS_PER_STEP=a.get("microstepping", defaults.get("microstepping", 8)),
            MAX_SPEED=a.get("max_velocity_mm", defaults.get("max_velocity_mm", 25)),
            MAX_ACCELERATION=a.get("max_acceleration_mm", defaults.get("max_acceleration_mm", 500)),
            MIN_POSITION=limits.get("negative", defaults.get("min_pos", -0.5)),
            MAX_POSITION=limits.get("positive", defaults.get("max_pos", 56)),
            PID=PIDConfig(
                ENABLED=pid_cfg.get("enabled", False),
                P=pid_cfg.get("p", 1 << 12),
                I=pid_cfg.get("i", 0),
                D=pid_cfg.get("d", 0),
            ) if pid_cfg.get("enabled", False) else None,
        )

    return StageConfig(
        X_AXIS=_axis("x", {"movement_sign": 1, "screw_pitch_mm": 2.54, "max_velocity_mm": 30}),
        Y_AXIS=_axis("y", {"movement_sign": 1, "screw_pitch_mm": 2.54, "max_velocity_mm": 30}),
        Z_AXIS=_axis("z", {"movement_sign": -1, "screw_pitch_mm": 0.3, "max_velocity_mm": 2, "min_pos": 0.05, "max_pos": 7}),
        THETA_AXIS=_axis("theta", {"screw_pitch_mm": 2 * 3.14159 / 200, "max_velocity_mm": 1.57}) if "theta" in cfg else AxisConfig(
            MOVEMENT_SIGN=1,
            USE_ENCODER=False,
            ENCODER_SIGN=1,
            ENCODER_STEP_SIZE=1,
            FULL_STEPS_PER_REV=200,
            SCREW_PITCH=2 * 3.14159 / 200,
            MICROSTEPS_PER_STEP=256,
            MAX_SPEED=1.57,
            MAX_ACCELERATION=500,
            MIN_POSITION=0,
            MAX_POSITION=1.57,
            PID=None,
        ),
    )
