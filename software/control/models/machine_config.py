"""
Unified machine configuration model.

Describes the entire microscope in a single YAML file: devices, IO wiring,
and software settings.  Replaces the scattered INI + _def.py + io_endpoints.yaml
approach with a declarative, Pydantic-validated configuration.

IO endpoints are owned by the devices that use them (via ``io:`` blocks on
each device or channel), not pre-declared on the low-level controllers.
At startup, ``MachineConfig.collect_io_endpoints()`` walks all devices and
channels to build the ``IOEndpointConfig`` consumed by ``IORegistry``.

The optional ``illumination_devices`` list supports composing multiple
illumination sources (multi-channel serial devices such as CoolLED pE-400 or
Lumencor SPECTRA, individual IO-routed lasers, LED matrices) under a single
``IlluminationController``.  Example::

    illumination_devices:
      - id: squid_lasers
        driver: squid_builtin
        channels:
          "Fluorescence 488 nm Ex":
            wavelength_nm: 488
            type: epi_illumination
            io:
              intensity: { controller: teensy, signal_type: analog, channel_id: "port:1" }
              shutter:   { controller: teensy, signal_type: digital, channel_id: "port:1" }

      - id: coolled
        driver: coolled_pe400
        connection: { port: "COM5" }
        channels:
          "BF 470 nm":
            wavelength_nm: 470
            type: transillumination
            serial_key: "A"
            io:
              shutter: { controller: nidaq, signal_type: digital, channel_id: "port0/line5" }

      - id: led_matrix
        driver: led_matrix
        config:
          unified: true
          unified_channel_name: "LED matrix"
        channels: {}
        # Classic (one GUI row per pattern): omit unified and list channels with source_code.
"""

import logging
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from control.models.io_endpoint_config import (
    IOControllerType,
    IODirection,
    IOEndpoint,
    IOEndpointConfig,
    IOSignalType,
)
from control.models.filter_wheel_config import FilterWheelRegistryConfig
from control.models.hardware_bindings import HardwareBindingsConfig

logger = logging.getLogger(__name__)

# Maps driver names to IOControllerType for IO endpoint collection.
_DRIVER_TO_IO_CONTROLLER: Dict[str, IOControllerType] = {
    "teensy": IOControllerType.MCU,
    "nidaq": IOControllerType.NIDAQ,
}

_SPECIAL_CONTROLLER_NAMES: Dict[str, IOControllerType] = {
    "serial": IOControllerType.SERIAL,
}


# ═══════════════════════════════════════════════════════════════════════════════
# Device IO and channel models
# ═══════════════════════════════════════════════════════════════════════════════


class DeviceIOLine(BaseModel):
    """A single IO line declared by a device.

    The ``controller`` field references a device name in the same config
    (e.g. "teensy", "nidaq") or the special keyword "serial" for lines
    controlled via a device's own serial connection.
    """

    controller: str
    signal_type: IOSignalType = IOSignalType.DIGITAL
    direction: IODirection = IODirection.OUTPUT
    channel_id: str = Field(..., min_length=1)
    # Optional human-readable label; NIDAQ will own runtime names, this seeds defaults.
    display_name: Optional[str] = None


class DeviceChannel(BaseModel):
    """A channel within a multi-channel device (e.g. one wavelength of a light source)."""

    wavelength_nm: Optional[int] = None
    io: Dict[str, DeviceIOLine] = Field(default_factory=dict)
    config: Dict[str, Any] = Field(default_factory=dict)


class DeviceConnection(BaseModel):
    """How to reach a hardware device — strictly addressing information."""

    type: str = "serial"
    serial_number: Optional[str] = None
    port: Optional[str] = None
    vid: Optional[int] = None
    pid: Optional[int] = None

    model_config = {"extra": "allow"}


# ═══════════════════════════════════════════════════════════════════════════════
# Illumination device models (for the illumination_devices list)
# ═══════════════════════════════════════════════════════════════════════════════


class IlluminationDeviceChannel(BaseModel):
    """A single channel within an illumination device.

    Extends ``DeviceChannel`` with illumination-specific metadata:

    - ``type``: ``"epi_illumination"`` or ``"transillumination"``
    - ``source_code``: MCU pattern code (LED matrix devices only)
    - ``serial_key``: Device-internal channel key (serial devices, e.g. ``"A"``
      for CoolLED channel A)
    """

    wavelength_nm: Optional[int] = None
    type: str = "epi_illumination"
    source_code: Optional[int] = None
    serial_key: Optional[str] = None
    io: Dict[str, DeviceIOLine] = Field(default_factory=dict)
    config: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}


class IlluminationDeviceEntry(BaseModel):
    """An entry in the ``illumination_devices`` list.

    Represents one physical illumination source (may have multiple channels).
    Channel keys in ``channels`` are the canonical channel names used
    throughout the system (must match ``IlluminationChannelConfig`` names and
    ``AcquisitionChannel.illumination_settings.illumination_channel``).

    Attributes:
        id: Unique identifier for this device (used to prefix IO endpoint names).
        driver: Driver name: ``"squid_builtin"``, ``"coolled_pe400"``, ``"ldi"``,
            ``"celesta"``, ``"andor_laser"``, ``"versalase"``, ``"led_matrix"``.
        enabled: Whether to construct this device at startup.
        connection: Serial / USB connection details.
        channels: ``{canonical_channel_name: IlluminationDeviceChannel}``.
        config: Driver-specific parameters.
    """

    id: str
    driver: str
    enabled: bool = True
    connection: Optional[DeviceConnection] = None
    channels: Dict[str, IlluminationDeviceChannel] = Field(default_factory=dict)
    config: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}


class DeviceEntry(BaseModel):
    """A single device in the machine configuration.

    Attributes:
        driver: Registered driver name (maps to a Python class via DriverRegistry).
        enabled: Whether to construct this device at startup.
        simulate: Whether to use the simulation variant.
        role: Semantic role (e.g. "main" or "focus" for cameras).
        controller: Reference to another device (e.g. stage -> teensy).
        connection: How to reach the device.
        io: Device-level IO lines (e.g. camera trigger, piezo output).
        channels: For multi-channel devices (e.g. light source wavelengths).
        config: Driver-specific parameters.
    """

    driver: str = ""
    enabled: bool = True
    simulate: bool = False
    role: Optional[str] = None
    controller: Optional[str] = None
    connection: Optional[DeviceConnection] = None
    io: Dict[str, DeviceIOLine] = Field(default_factory=dict)
    channels: Dict[str, DeviceChannel] = Field(default_factory=dict)
    config: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}


# ═══════════════════════════════════════════════════════════════════════════════
# Software / GUI settings
# ═══════════════════════════════════════════════════════════════════════════════


class DisplaySettings(BaseModel):
    default_crop: int = 100
    use_napari_for_live_view: bool = False
    use_napari_for_mosaic: bool = True

    model_config = {"extra": "allow"}


class AcquisitionSettings(BaseModel):
    image_format: str = "bmp"
    scaling_factor: float = 0.85
    dx: float = 0.9
    dy: float = 0.9
    dz: float = 1.5
    fovs_per_af: int = 3
    flexible_multipoint: bool = True
    wellplate_multipoint: bool = True
    recording: bool = False
    fast_acquisition: bool = False
    default_nx: int = 1
    default_ny: int = 1

    model_config = {"extra": "allow"}


class AutofocusSettings(BaseModel):
    channel: str = "BF LED matrix full"
    enable_by_default: bool = False
    bf_saving_option: str = "Raw"
    stop_threshold: float = 0.85
    crop_width: int = 800
    crop_height: int = 800

    model_config = {"extra": "allow"}


class TrackingSettings(BaseModel):
    enabled: bool = False
    default_tracker: str = "csrt"

    model_config = {"extra": "allow"}


class OpticsSettings(BaseModel):
    tube_lens_mm: float = 180
    inverted_objective: bool = True

    model_config = {"extra": "allow"}


class PlateReaderSettings(BaseModel):
    rows: int = 8
    columns: int = 12
    row_spacing_mm: float = 9
    column_spacing_mm: float = 9
    offset_column_1_mm: float = 20
    offset_row_a_mm: float = 20

    model_config = {"extra": "allow"}


class WellplateCalibration(BaseModel):
    upper_left_x_mm: float = 0
    upper_left_y_mm: float = 0
    offset_x_mm: float = 0
    offset_y_mm: float = 0

    model_config = {"extra": "allow"}


class SoftwareConfig(BaseModel):
    """Non-hardware settings for the microscope GUI and acquisition engine."""

    is_hcs: bool = False
    wellplate_format: int = 384
    default_saving_path: str = ""
    display: DisplaySettings = Field(default_factory=DisplaySettings)
    acquisition: AcquisitionSettings = Field(default_factory=AcquisitionSettings)
    autofocus: AutofocusSettings = Field(default_factory=AutofocusSettings)
    tracking: TrackingSettings = Field(default_factory=TrackingSettings)
    optics: OpticsSettings = Field(default_factory=OpticsSettings)
    plate_reader: PlateReaderSettings = Field(default_factory=PlateReaderSettings)
    wellplate_calibrations: Dict[str, WellplateCalibration] = Field(default_factory=dict)

    model_config = {"extra": "allow"}


# ═══════════════════════════════════════════════════════════════════════════════
# Root machine config
# ═══════════════════════════════════════════════════════════════════════════════


class MachineConfig(BaseModel):
    """Root configuration for a microscope.

    Loaded from ``machine_configs/machine_config.yaml``.
    """

    version: float = Field(3.0, description="Configuration format version")
    devices: Dict[str, DeviceEntry] = Field(default_factory=dict)
    illumination_channels_file: Optional[str] = "illumination_channel_config.yaml"
    illumination_devices: List[IlluminationDeviceEntry] = Field(
        default_factory=list,
        description=(
            "Composable illumination sources.  When present, "
            "IlluminationController is built from this list instead of the "
            "legacy devices.illumination entry."
        ),
    )
    software: SoftwareConfig = Field(default_factory=SoftwareConfig)
    filter_wheel_registry: Optional[FilterWheelRegistryConfig] = Field(
        default=None,
        description=(
            "Standalone filter wheel definitions (position names).  When set with a "
            "non-empty ``filter_wheels`` list, overrides ``filter_wheels.yaml``."
        ),
    )
    hardware_bindings: Optional[HardwareBindingsConfig] = Field(
        default=None,
        description=(
            "Camera to filter wheel bindings.  When set, overrides "
            "``hardware_bindings.yaml``."
        ),
    )

    model_config = {"extra": "allow"}

    # ── IO endpoint collection ────────────────────────────────────────────────

    def collect_io_endpoints(self) -> IOEndpointConfig:
        """Walk all devices and channels, collect ``io:`` declarations into IOEndpoints.

        Endpoint names are auto-generated as ``device.io_key`` for device-level
        IO and ``device.channel.io_key`` for channel-level IO.

        Also walks ``illumination_devices`` entries, using
        ``{device.id}.{channel_name}.{io_key}`` as endpoint names.

        Returns an ``IOEndpointConfig`` ready for the ``IORegistry``.
        """
        endpoints: List[IOEndpoint] = []

        for dev_name, dev in self.devices.items():
            if not dev.enabled:
                continue

            for io_key, io_line in dev.io.items():
                ep = self._make_endpoint(
                    f"{dev_name}.{io_key}", io_line, io_key,
                )
                if ep is not None:
                    endpoints.append(ep)

            for ch_name, ch in dev.channels.items():
                for io_key, io_line in ch.io.items():
                    ep = self._make_endpoint(
                        f"{dev_name}.{ch_name}.{io_key}", io_line, io_key,
                    )
                    if ep is not None:
                        endpoints.append(ep)

        for illum_dev in self.illumination_devices:
            if not illum_dev.enabled:
                continue
            for ch_name, ch in illum_dev.channels.items():
                for io_key, io_line in ch.io.items():
                    ep = self._make_endpoint(
                        f"{illum_dev.id}.{ch_name}.{io_key}", io_line, io_key,
                    )
                    if ep is not None:
                        endpoints.append(ep)

        return IOEndpointConfig(version=self.version, endpoints=endpoints)

    def _make_endpoint(
        self, name: str, io_line: DeviceIOLine, role: str,
    ) -> Optional[IOEndpoint]:
        """Convert a DeviceIOLine to an IOEndpoint, resolving the controller type."""
        ctrl_type = self._resolve_controller_type(io_line.controller)
        if ctrl_type is None:
            logger.warning(
                f"IO line '{name}' references unknown controller '{io_line.controller}'"
            )
            return None
        return IOEndpoint(
            name=name,
            controller=ctrl_type,
            signal_type=io_line.signal_type,
            direction=io_line.direction,
            channel_id=io_line.channel_id,
            role=role,
            display_name=io_line.display_name,
        )

    def _resolve_controller_type(self, controller_name: str) -> Optional[IOControllerType]:
        """Map a controller device name to an IOControllerType."""
        if controller_name in _SPECIAL_CONTROLLER_NAMES:
            return _SPECIAL_CONTROLLER_NAMES[controller_name]

        dev = self.devices.get(controller_name)
        if dev is None:
            return None
        return _DRIVER_TO_IO_CONTROLLER.get(dev.driver)

    # ── Validation ────────────────────────────────────────────────────────────

    @model_validator(mode="after")
    def _validate_controller_references(self) -> "MachineConfig":
        """Check that ``controller`` fields reference existing devices."""
        for dev_name, dev in self.devices.items():
            if dev.controller and dev.controller not in self.devices:
                raise ValueError(
                    f"Device '{dev_name}' references controller "
                    f"'{dev.controller}' which is not defined in devices"
                )
        return self

    def validate_io_lines(self) -> List[str]:
        """Check for channel conflicts and missing controller references.

        Returns a list of warning strings (empty = all OK).
        """
        issues: List[str] = []
        seen_channels: Dict[str, str] = {}

        for dev_name, dev in self.devices.items():
            if not dev.enabled:
                continue

            for io_key, io_line in dev.io.items():
                ep_name = f"{dev_name}.{io_key}"
                self._check_io_line(ep_name, io_line, seen_channels, issues)

            for ch_name, ch in dev.channels.items():
                for io_key, io_line in ch.io.items():
                    ep_name = f"{dev_name}.{ch_name}.{io_key}"
                    self._check_io_line(ep_name, io_line, seen_channels, issues)

        for illum_dev in self.illumination_devices:
            if not illum_dev.enabled:
                continue
            for ch_name, ch in illum_dev.channels.items():
                for io_key, io_line in ch.io.items():
                    ep_name = f"{illum_dev.id}.{ch_name}.{io_key}"
                    self._check_io_line(ep_name, io_line, seen_channels, issues)

        return issues

    def _check_io_line(
        self,
        ep_name: str,
        io_line: DeviceIOLine,
        seen_channels: Dict[str, str],
        issues: List[str],
    ) -> None:
        ctrl_type = self._resolve_controller_type(io_line.controller)
        if ctrl_type is None:
            issues.append(
                f"'{ep_name}' references unknown controller '{io_line.controller}'"
            )

        # Same controller+channel_id can have both analog and digital (e.g. one MCU port
        # drives DAC intensity and TTL shutter). Only conflict when same signal_type.
        conflict_key = (
            f"{io_line.controller}:{io_line.channel_id}:{io_line.signal_type.value}"
        )
        if conflict_key in seen_channels:
            other = seen_channels[conflict_key]
            issues.append(
                f"Channel conflict: '{ep_name}' and '{other}' both claim "
                f"{io_line.controller}:{io_line.channel_id} ({io_line.signal_type.value})"
            )
        else:
            seen_channels[conflict_key] = ep_name

    # ── Device lookup helpers ─────────────────────────────────────────────────

    def get_device(self, name: str) -> Optional[DeviceEntry]:
        """Get a device entry by name."""
        return self.devices.get(name)

    def get_enabled_devices(self) -> Dict[str, DeviceEntry]:
        """Get all enabled device entries."""
        return {k: v for k, v in self.devices.items() if v.enabled}

    def get_devices_by_driver(self, driver: str) -> Dict[str, DeviceEntry]:
        """Get all enabled devices with a given driver name."""
        return {
            k: v for k, v in self.devices.items()
            if v.enabled and v.driver == driver
        }

    def get_devices_by_role(self, role: str) -> Dict[str, DeviceEntry]:
        """Get all enabled devices with a given role."""
        return {
            k: v for k, v in self.devices.items()
            if v.enabled and v.role == role
        }


def build_default_machine_config() -> MachineConfig:
    """Generate a default MachineConfig matching legacy MCU-only wiring.

    Mirrors ``build_default_io_endpoint_config()`` but in the new
    device-centric format.
    """
    devices: Dict[str, DeviceEntry] = {}

    devices["teensy"] = DeviceEntry(
        driver="teensy",
        config={
            "illumination_intensity_factor": 0.6,
        },
    )

    # Default NI-DAQ device (no IO lines by default; endpoints are added by users)
    devices["nidaq"] = DeviceEntry(
        driver="nidaq",
        config={
            "device_name": "Dev1",
            "sample_rate": 10000.0,
            "samples_per_channel": 1000,
            "ao_min_voltage": -10.0,
            "ao_max_voltage": 10.0,
            "ai_min_voltage": -10.0,
            "ai_max_voltage": 10.0,
            "ai_terminal_config": "RSE",
            "trigger_source": "SOFTWARE",
            "external_trigger_terminal": "/Dev1/PFI0",
            "trigger_edge": "RISING",
            "continuous": False,
            # Digital logic family; may be overridden per-machine (e.g. 3.3V for FLIR).
            "logic_family": "THREE_POINT_THREE_V",
        },
    )

    devices["main_camera"] = DeviceEntry(
        driver="daheng",
        role="main",
        io={
            "trigger": DeviceIOLine(
                controller="teensy",
                signal_type=IOSignalType.DIGITAL,
                direction=IODirection.OUTPUT,
                channel_id="trigger:0",
            ),
        },
    )

    illum_channels: Dict[str, DeviceChannel] = {}
    default_wavelengths = {0: 405, 1: 488, 2: 561, 3: 638, 4: 730}
    for i, wl in default_wavelengths.items():
        illum_channels[f"{wl}nm"] = DeviceChannel(
            wavelength_nm=wl,
            io={
                "intensity": DeviceIOLine(
                    controller="teensy",
                    signal_type=IOSignalType.ANALOG,
                    direction=IODirection.OUTPUT,
                    channel_id=f"port:{i}",
                ),
                "shutter": DeviceIOLine(
                    controller="teensy",
                    signal_type=IOSignalType.DIGITAL,
                    direction=IODirection.OUTPUT,
                    channel_id=f"port:{i}",
                ),
            },
        )

    devices["illumination"] = DeviceEntry(
        driver="squid_builtin",
        channels=illum_channels,
    )

    devices["laser_af"] = DeviceEntry(
        driver="laser_af",
        enabled=False,
        io={
            "laser_gate": DeviceIOLine(
                controller="teensy",
                signal_type=IOSignalType.DIGITAL,
                direction=IODirection.OUTPUT,
                channel_id="pin:15",
            ),
        },
    )

    devices["piezo"] = DeviceEntry(
        driver="objective_piezo",
        enabled=False,
        io={
            "output": DeviceIOLine(
                controller="teensy",
                signal_type=IOSignalType.ANALOG,
                direction=IODirection.OUTPUT,
                channel_id="dac:7",
            ),
        },
        config={
            "range_um": 300,
            "home_um": 150,
            "control_voltage_range": 10,
            "flip_direction": False,
        },
    )

    return MachineConfig(
        version=3.0,
        devices=devices,
        software=SoftwareConfig(),
    )
