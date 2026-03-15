"""
IO controller abstraction layer.

Provides a uniform interface for digital and analog IO operations across
different physical controllers (Teensy MCU, NI-DAQ).  Higher-level code
(IlluminationController, PiezoStage, camera trigger logic) calls into the
IORegistry to obtain a controller for a given logical endpoint and then
issues set_digital / set_analog / send_trigger through the common API.

Typical startup flow:
    1. ConfigRepository loads IOEndpointConfig from io_endpoints.yaml
    2. IORegistry is constructed with the endpoint config + hardware handles
    3. Registry maps each endpoint name -> (AbstractIOController, IOEndpoint)
    4. Higher-level code calls  registry.get("illum_D1_shutter").set_digital(True)
"""

from __future__ import annotations

import abc
import logging
from typing import Dict, List, Optional, Protocol, TYPE_CHECKING, runtime_checkable

from control.models.io_endpoint_config import (
    IOControllerType,
    IODirection,
    IOEndpoint,
    IOEndpointConfig,
    IOSignalType,
)

if TYPE_CHECKING:
    from control.microcontroller import Microcontroller
    from control.ni_daq import AbstractNIDAQ
    from squid.abc import LightSource

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Abstract IO controller
# ═══════════════════════════════════════════════════════════════════════════════


class AbstractIOController(abc.ABC):
    """Uniform interface to a physical IO controller (MCU or NI-DAQ)."""

    @abc.abstractmethod
    def set_digital(self, endpoint: IOEndpoint, level: bool) -> None:
        """Set a digital output high (True) or low (False)."""

    @abc.abstractmethod
    def set_analog(self, endpoint: IOEndpoint, value: float) -> None:
        """Set an analog output.

        *value* semantics depend on the endpoint role:
        - intensity endpoints: 0-100 percentage
        - piezo endpoints: position in micrometres
        - raw DAC: 0-65535
        """

    @abc.abstractmethod
    def send_trigger(
        self,
        endpoint: IOEndpoint,
        *,
        control_illumination: bool = False,
        illumination_on_time_us: int = 0,
    ) -> None:
        """Send a hardware trigger pulse through *endpoint*."""

    @abc.abstractmethod
    def set_strobe_delay(self, endpoint: IOEndpoint, delay_us: int) -> None:
        """Configure strobe delay for a trigger endpoint."""

    def wait_until_ready(self, timeout: float = 5.0) -> None:
        """Block until the last operation completes (default: no-op)."""

    def close(self) -> None:
        """Release resources (default: no-op)."""


# ═══════════════════════════════════════════════════════════════════════════════
# MCU IO controller
# ═══════════════════════════════════════════════════════════════════════════════


class MCUIOController(AbstractIOController):
    """Routes IO operations to the Teensy microcontroller.

    Uses the port-specific multi-port API (firmware v1.0+) when available
    and falls back to the legacy single-channel API for older firmware.
    """

    def __init__(self, microcontroller: "Microcontroller"):
        self._mc = microcontroller
        self._multi_port = microcontroller.supports_multi_port()

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _parse_channel_id(channel_id: str) -> tuple[str, int]:
        """Parse 'kind:N' -> ('kind', N).  e.g. 'port:3' -> ('port', 3)."""
        kind, _, num = channel_id.partition(":")
        return kind, int(num)

    # -- digital ---------------------------------------------------------------

    def set_digital(self, endpoint: IOEndpoint, level: bool) -> None:
        kind, num = self._parse_channel_id(endpoint.channel_id)
        if kind == "port":
            if self._multi_port:
                if level:
                    self._mc.turn_on_port(num)
                else:
                    self._mc.turn_off_port(num)
            else:
                if level:
                    self._mc.turn_on_illumination()
                else:
                    self._mc.turn_off_illumination()
        elif kind == "pin":
            self._mc.set_pin_level(num, int(level))
        elif kind == "trigger":
            if level:
                self._mc.send_hardware_trigger(False, 0, trigger_output_ch=num)
        else:
            raise ValueError(f"MCU digital: unsupported channel_id '{endpoint.channel_id}'")

    # -- analog ----------------------------------------------------------------

    def set_analog(self, endpoint: IOEndpoint, value: float) -> None:
        kind, num = self._parse_channel_id(endpoint.channel_id)
        if kind == "port":
            if self._multi_port:
                self._mc.set_port_intensity(num, value)
            else:
                from control._def import port_index_to_source_code
                source_code = port_index_to_source_code(num)
                self._mc.set_illumination(source_code, value)
        elif kind == "dac":
            self._mc.analog_write_onboard_DAC(num, int(value))
        else:
            raise ValueError(f"MCU analog: unsupported channel_id '{endpoint.channel_id}'")

    # -- trigger ---------------------------------------------------------------

    def send_trigger(
        self,
        endpoint: IOEndpoint,
        *,
        control_illumination: bool = False,
        illumination_on_time_us: int = 0,
    ) -> None:
        kind, num = self._parse_channel_id(endpoint.channel_id)
        if kind != "trigger":
            raise ValueError(f"MCU send_trigger: expected trigger channel, got '{endpoint.channel_id}'")
        self._mc.send_hardware_trigger(control_illumination, illumination_on_time_us, trigger_output_ch=num)

    def set_strobe_delay(self, endpoint: IOEndpoint, delay_us: int) -> None:
        kind, num = self._parse_channel_id(endpoint.channel_id)
        if kind != "trigger":
            raise ValueError(f"MCU set_strobe_delay: expected trigger channel, got '{endpoint.channel_id}'")
        self._mc.set_strobe_delay_us(delay_us, camera_channel=num)

    # -- lifecycle -------------------------------------------------------------

    def wait_until_ready(self, timeout: float = 5.0) -> None:
        self._mc.wait_till_operation_is_completed(timeout)


# ═══════════════════════════════════════════════════════════════════════════════
# NI-DAQ IO controller
# ═══════════════════════════════════════════════════════════════════════════════


class NIDAQIOController(AbstractIOController):
    """Routes IO operations to the NI-DAQ for live (DC) output.

    For synchronized / waveform acquisition the caller should work with the
    underlying AbstractNIDAQ directly (set_waveforms / arm / start_trigger).
    This wrapper provides *live-mode* single-value output only, so that
    shutter/intensity controls work identically regardless of backend.
    """

    def __init__(self, nidaq: "AbstractNIDAQ"):
        self._daq = nidaq
        self._live_ao: Dict[str, float] = {}
        self._live_do: Dict[int, bool] = {}

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _parse_do_channel(channel_id: str) -> int:
        """Extract line number from 'portP/lineL' -> L."""
        parts = channel_id.split("/")
        for p in parts:
            if p.startswith("line"):
                return int(p[4:])
        raise ValueError(f"Cannot parse DO line from '{channel_id}'")

    @staticmethod
    def _parse_ao_channel(channel_id: str) -> str:
        """Return NI-DAQ channel string (e.g. 'ao0')."""
        return channel_id

    # -- digital ---------------------------------------------------------------

    def set_digital(self, endpoint: IOEndpoint, level: bool) -> None:
        line = self._parse_do_channel(endpoint.channel_id)
        self._live_do[line] = level
        self._daq.start_live_output(do_values=self._live_do)

    # -- analog ----------------------------------------------------------------

    def set_analog(self, endpoint: IOEndpoint, value: float) -> None:
        ch = self._parse_ao_channel(endpoint.channel_id)
        self._live_ao[ch] = value
        self._daq.start_live_output(ao_values=self._live_ao)

    # -- trigger ---------------------------------------------------------------

    def send_trigger(
        self,
        endpoint: IOEndpoint,
        *,
        control_illumination: bool = False,
        illumination_on_time_us: int = 0,
    ) -> None:
        line = self._parse_do_channel(endpoint.channel_id)
        self._live_do[line] = True
        self._daq.start_live_output(do_values=self._live_do)
        # Pulse high then low — for a real timed pulse the acquisition waveform
        # path should be used instead.
        import time
        time.sleep(max(illumination_on_time_us / 1e6, 0.001))
        self._live_do[line] = False
        self._daq.start_live_output(do_values=self._live_do)

    def set_strobe_delay(self, endpoint: IOEndpoint, delay_us: int) -> None:
        # Strobe delay is built into the waveform when using NI-DAQ for
        # synchronized acquisition.  For live-mode triggers it is a no-op.
        pass

    # -- lifecycle -------------------------------------------------------------

    def close(self) -> None:
        self._daq.stop_live_output()


# ═══════════════════════════════════════════════════════════════════════════════
# Serial IO device protocol
# ═══════════════════════════════════════════════════════════════════════════════


@runtime_checkable
class SerialIODevice(Protocol):
    """Protocol for devices whose analog/digital-equivalent signals are set via serial.

    This covers ONLY the subset of a device's serial commands that replace what
    would otherwise be an analog or digital IO line.  All other operations
    (queries, bulk transfers, configuration) live on the full device controller
    class and are accessed directly by higher-level code.
    """

    def set_analog_value(self, channel: str, value: float) -> None:
        """Set an analog-equivalent value on a channel (e.g. intensity 0-100)."""
        ...

    def set_digital_value(self, channel: str, level: bool) -> None:
        """Set a digital-equivalent value on a channel (e.g. shutter on/off)."""
        ...

    def shut_down(self) -> None:
        """Release resources."""
        ...


class LightSourceSerialAdapter:
    """Adapts a LightSource to the SerialIODevice protocol.

    Maps set_analog_value -> set_intensity, set_digital_value -> set_shutter_state.
    """

    def __init__(self, light_source: "LightSource"):
        self._ls = light_source

    @property
    def light_source(self) -> "LightSource":
        """Access the underlying LightSource for rich operations."""
        return self._ls

    def set_analog_value(self, channel: str, value: float) -> None:
        self._ls.set_intensity(channel, value)

    def set_digital_value(self, channel: str, level: bool) -> None:
        self._ls.set_shutter_state(channel, level)

    def shut_down(self) -> None:
        self._ls.shut_down()


# ═══════════════════════════════════════════════════════════════════════════════
# Serial IO controller (routes IO endpoint ops to a SerialIODevice)
# ═══════════════════════════════════════════════════════════════════════════════


class SerialIOController(AbstractIOController):
    """Routes IO operations to a serial peripheral implementing SerialIODevice.

    channel_id format: "device_prefix:channel_key"  (e.g. "coolled:A").
    The device_prefix selects the device instance; channel_key is passed
    to set_analog_value() / set_digital_value().

    set_analog  -> device.set_analog_value(channel_key, value)
    set_digital -> device.set_digital_value(channel_key, level)
    """

    def __init__(self, device_prefix: str, device: SerialIODevice):
        self._prefix = device_prefix
        self._device = device

    @property
    def device(self) -> SerialIODevice:
        return self._device

    @staticmethod
    def _parse_channel_id(channel_id: str) -> tuple[str, str]:
        """Parse 'prefix:channel' -> ('prefix', 'channel')."""
        prefix, _, channel = channel_id.partition(":")
        if not channel:
            raise ValueError(
                f"Serial channel_id must be 'prefix:channel', got '{channel_id}'"
            )
        return prefix, channel

    def set_digital(self, endpoint: IOEndpoint, level: bool) -> None:
        _, channel_key = self._parse_channel_id(endpoint.channel_id)
        self._device.set_digital_value(channel_key, level)

    def set_analog(self, endpoint: IOEndpoint, value: float) -> None:
        _, channel_key = self._parse_channel_id(endpoint.channel_id)
        self._device.set_analog_value(channel_key, value)

    def send_trigger(
        self,
        endpoint: IOEndpoint,
        *,
        control_illumination: bool = False,
        illumination_on_time_us: int = 0,
    ) -> None:
        logger.warning(
            f"send_trigger called on serial endpoint '{endpoint.name}' — "
            "serial devices do not support hardware trigger pulses"
        )

    def set_strobe_delay(self, endpoint: IOEndpoint, delay_us: int) -> None:
        pass

    def close(self) -> None:
        try:
            self._device.shut_down()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# Bound endpoint — convenient handle returned by IORegistry
# ═══════════════════════════════════════════════════════════════════════════════


class BoundEndpoint:
    """A (controller, endpoint) pair with convenience methods.

    Returned by IORegistry.get() so callers don't have to pass the endpoint
    metadata every time.
    """

    def __init__(self, controller: AbstractIOController, endpoint: IOEndpoint):
        self.controller = controller
        self.endpoint = endpoint

    # --- forwarding helpers ---------------------------------------------------

    def set_digital(self, level: bool) -> None:
        self.controller.set_digital(self.endpoint, level)

    def set_analog(self, value: float) -> None:
        self.controller.set_analog(self.endpoint, value)

    def send_trigger(
        self,
        *,
        control_illumination: bool = False,
        illumination_on_time_us: int = 0,
    ) -> None:
        self.controller.send_trigger(
            self.endpoint,
            control_illumination=control_illumination,
            illumination_on_time_us=illumination_on_time_us,
        )

    def set_strobe_delay(self, delay_us: int) -> None:
        self.controller.set_strobe_delay(self.endpoint, delay_us)

    def wait(self, timeout: float = 5.0) -> None:
        self.controller.wait_until_ready(timeout)

    # --- introspection --------------------------------------------------------

    @property
    def name(self) -> str:
        return self.endpoint.name

    @property
    def controller_type(self) -> IOControllerType:
        return self.endpoint.controller

    @property
    def signal_type(self) -> IOSignalType:
        return self.endpoint.signal_type

    @property
    def role(self) -> str:
        return self.endpoint.role


# ═══════════════════════════════════════════════════════════════════════════════
# IO Registry — the single entry point for higher-level code
# ═══════════════════════════════════════════════════════════════════════════════


class IORegistry:
    """Maps logical endpoint names to (controller, metadata) pairs.

    Constructed once at startup from the loaded IOEndpointConfig and the
    available hardware handles (Microcontroller, NIDAQ).
    """

    def __init__(
        self,
        config: IOEndpointConfig,
        microcontroller: Optional["Microcontroller"] = None,
        nidaq: Optional["AbstractNIDAQ"] = None,
        serial_devices: Optional[Dict[str, SerialIODevice]] = None,
    ):
        self._config = config
        self._controllers: Dict[IOControllerType, AbstractIOController] = {}
        self._bound: Dict[str, BoundEndpoint] = {}

        if microcontroller is not None:
            self._controllers[IOControllerType.MCU] = MCUIOController(microcontroller)
        if nidaq is not None:
            self._controllers[IOControllerType.NIDAQ] = NIDAQIOController(nidaq)

        # Build per-prefix SerialIOControllers from serial_devices dict
        self._serial_controllers: Dict[str, SerialIOController] = {}
        if serial_devices:
            for prefix, device in serial_devices.items():
                self._serial_controllers[prefix] = SerialIOController(prefix, device)

        for ep in config.get_all():
            if ep.controller == IOControllerType.SERIAL:
                ctrl = self._resolve_serial_controller(ep)
            else:
                ctrl = self._controllers.get(ep.controller)
            if ctrl is None:
                logger.warning(
                    f"IO endpoint '{ep.name}' assigned to {ep.controller.value} "
                    "but that controller is not available — endpoint will be inert"
                )
                continue
            self._bound[ep.name] = BoundEndpoint(ctrl, ep)

    def _resolve_serial_controller(self, ep: IOEndpoint) -> Optional[SerialIOController]:
        """Find the SerialIOController matching an endpoint's channel_id prefix."""
        prefix, _, _ = ep.channel_id.partition(":")
        ctrl = self._serial_controllers.get(prefix)
        if ctrl is None:
            logger.warning(
                f"IO endpoint '{ep.name}' uses serial prefix '{prefix}' "
                f"but no serial device registered with that prefix"
            )
        return ctrl

    # -- public API ------------------------------------------------------------

    def get(self, name: str) -> Optional[BoundEndpoint]:
        """Look up a bound endpoint by logical name."""
        return self._bound.get(name)

    def require(self, name: str) -> BoundEndpoint:
        """Look up a bound endpoint; raise KeyError if not found."""
        ep = self.get(name)
        if ep is None:
            raise KeyError(
                f"No IO endpoint named '{name}' "
                "(check io_endpoints.yaml and available hardware)"
            )
        return ep

    def get_by_role(self, role: str) -> List[BoundEndpoint]:
        """Get all bound endpoints with a given semantic role."""
        return [b for b in self._bound.values() if b.role == role]

    def get_controller(self, controller_type: IOControllerType) -> Optional[AbstractIOController]:
        """Get the raw controller for a given type (MCU, NIDAQ)."""
        return self._controllers.get(controller_type)

    def get_serial_controller(self, prefix: str) -> Optional[SerialIOController]:
        """Get a serial IO controller by device prefix (e.g. 'coolled')."""
        return self._serial_controllers.get(prefix)

    @property
    def endpoint_config(self) -> IOEndpointConfig:
        return self._config

    # -- validation & summary --------------------------------------------------

    def validate(self) -> List[str]:
        """Run startup validation and return a list of warning/error messages.

        Checks:
        - Every endpoint has its controller available
        - NIDAQ endpoints use valid NIDAQ-style channel IDs
        - MCU endpoints use valid MCU-style channel IDs
        - SERIAL endpoints use valid prefix:channel format with registered device
        """
        issues: List[str] = []
        for ep in self._config.get_all():
            if ep.controller == IOControllerType.SERIAL:
                if ":" not in ep.channel_id:
                    issues.append(
                        f"Endpoint '{ep.name}' assigned to SERIAL but channel_id "
                        f"'{ep.channel_id}' doesn't look like 'prefix:channel'"
                    )
                else:
                    prefix = ep.channel_id.split(":")[0]
                    if prefix not in self._serial_controllers:
                        issues.append(
                            f"Endpoint '{ep.name}' uses serial prefix '{prefix}' "
                            "but no serial device registered with that prefix"
                        )
                continue

            if ep.controller not in self._controllers:
                issues.append(
                    f"Endpoint '{ep.name}' assigned to {ep.controller.value} "
                    "but that controller is not initialised"
                )
            if ep.controller == IOControllerType.MCU and ":" not in ep.channel_id:
                issues.append(
                    f"Endpoint '{ep.name}' assigned to MCU but channel_id "
                    f"'{ep.channel_id}' doesn't look like 'kind:N'"
                )
            if ep.controller == IOControllerType.NIDAQ:
                cid = ep.channel_id
                valid = (
                    cid.startswith("ao") or cid.startswith("ai")
                    or "/" in cid  # portP/lineL
                )
                if not valid:
                    issues.append(
                        f"Endpoint '{ep.name}' assigned to NIDAQ but channel_id "
                        f"'{cid}' doesn't look like a NIDAQ channel"
                    )
        return issues

    def log_summary(self) -> None:
        """Log a human-readable summary of all endpoint assignments."""
        logger.info("=== IO Endpoint Routing Summary ===")
        for ep in self._config.get_all():
            available = ep.name in self._bound
            status = "OK" if available else "UNAVAILABLE"
            logger.info(
                f"  {ep.name:30s}  {ep.controller.value:6s}  "
                f"{ep.signal_type.value:7s}  {ep.direction.value:6s}  "
                f"ch={ep.channel_id:15s}  role={ep.role:15s}  [{status}]"
            )
        issues = self.validate()
        if issues:
            for issue in issues:
                logger.warning(f"  IO CONFIG WARNING: {issue}")
        else:
            logger.info("  All endpoints validated OK")
        logger.info("=== End IO Summary ===")
