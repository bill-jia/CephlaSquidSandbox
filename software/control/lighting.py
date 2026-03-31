"""
Illumination control system for the microscope.

``IlluminationController`` is a compositor that aggregates any number of
``IlluminationDevice`` instances.  Each device — whether a multi-channel serial
light source (CoolLED pE-400, Lumencor LDI/CELESTA), IO-routed individual
lasers, or an LED matrix — exposes a uniform channel-name API.  Consumers
(LiveController, FastAcquisitionController, widgets) call
``set_channel_intensity`` / ``set_channel_state`` without
knowing the underlying hardware.

Primary API (channel-name based, device-agnostic)::

    controller.set_channel_intensity("Fluorescence 488 nm Ex", 50)
    controller.set_channel_state("Fluorescence 488 nm Ex", True)
    controller.set_channel_state("Fluorescence 488 nm Ex", False)
    snapshot = controller.snapshot()
    ...
    controller.restore(snapshot)

The legacy wavelength-integer API (``set_intensity``, ``turn_on_illumination``,
``turn_off_illumination``) is preserved as a backward-compatible shim.
"""

from __future__ import annotations

import abc
import logging
import re
import squid.logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING
import time

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from control.core.io_controller import IORegistry, BoundEndpoint
    from control.microcontroller import Microcontroller
    from squid.abc import LightSource
    from control.serial_peripherals import SciMicroscopyLEDArray

logger = logging.getLogger(__name__)

# Number of illumination ports supported (matches firmware)
NUM_ILLUMINATION_PORTS = 16

_WL_RE = re.compile(r"(\d{3,4})(?:nm)?$", re.IGNORECASE)


def _extract_wavelength(channel_name: str) -> Optional[int]:
    """Extract a wavelength integer from a channel name like '488nm' or 'Fluorescence 488 nm Ex'."""
    m = _WL_RE.search(channel_name)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Deprecated enumerations
# Kept for backward-compatible imports in microscope.py and test code.
# Device-type decisions are now encapsulated inside IlluminationDevice subclasses.
# ---------------------------------------------------------------------------

class LightSourceType(Enum):
    """Deprecated: device type is encapsulated in IlluminationDevice subclasses."""
    SquidLED = 0
    SquidLaser = 1
    LDI = 2
    CELESTA = 3
    VersaLase = 4
    SCI = 5
    AndorLaser = 6
    CoolLED = 7


class IntensityControlMode(Enum):
    """Deprecated: per-channel routing is handled inside IlluminationDevice subclasses."""
    SquidControllerDAC = 0
    Software = 1
    IOEndpoint = 2


class ShutterControlMode(Enum):
    """Deprecated: see IntensityControlMode."""
    TTL = 0
    Software = 1
    IOEndpoint = 2


# ---------------------------------------------------------------------------
# State dataclasses (public API — unchanged)
# ---------------------------------------------------------------------------

@dataclass
class ChannelState:
    """Runtime intensity and on/off state for a single illumination channel."""
    intensity: float = 0.0  # 0–100 %
    is_on: bool = False


@dataclass
class IlluminationSnapshot:
    """A point-in-time capture of all channel states, keyed by channel name."""
    channel_states: Dict[str, ChannelState] = field(default_factory=dict)

    def copy(self) -> "IlluminationSnapshot":
        return IlluminationSnapshot(
            {name: ChannelState(s.intensity, s.is_on) for name, s in self.channel_states.items()}
        )


@dataclass
class IlluminationPreset:
    """A named, persistable illumination configuration."""
    name: str
    snapshot: IlluminationSnapshot


# ---------------------------------------------------------------------------
# IlluminationDevice ABC
# ---------------------------------------------------------------------------

class IlluminationDevice(abc.ABC):
    """Abstract base for a single illumination source (single- or multi-channel).

    A device owns one or more named channels.  All operations take a channel
    name string.  ``IlluminationController`` aggregates multiple devices and
    routes calls to the correct one.

    Implementors:
        - ``IORoutedIlluminationDevice``: MCU / NI-DAQ IO endpoint channels
        - ``SerialIlluminationDevice``: CoolLED, Lumencor LDI/CELESTA, etc.
        - ``LEDMatrixIlluminationDevice``: SciMicroscopy LED array or plain MCU matrix
    """

    @property
    @abc.abstractmethod
    def channel_names(self) -> List[str]:
        """Return the list of channel names provided by this device."""

    @abc.abstractmethod
    def initialize(self) -> None:
        """Initialize the device (open serial port, configure modes, etc.)."""

    @abc.abstractmethod
    def shut_down(self) -> None:
        """Release device resources and turn off all channels."""

    @abc.abstractmethod
    def set_intensity(self, channel: str, intensity: float) -> None:
        """Set intensity for *channel* (0–100 %)."""

    @abc.abstractmethod
    def turn_on(self, channel: str) -> None:
        """Open shutter / assert TTL / enable *channel*."""

    @abc.abstractmethod
    def turn_off(self, channel: str) -> None:
        """Close shutter / de-assert TTL / disable *channel*."""

    def turn_off_all(self) -> None:
        """Turn off all channels.  Default implementation iterates ``channel_names``."""
        for ch in self.channel_names:
            try:
                self.turn_off(ch)
            except Exception as exc:
                logger.warning(f"[{self.__class__.__name__}] turn_off('{ch}') failed: {exc}")

    def set_on_off_state(self, channel: str, is_on: bool) -> None:
        """Set the on/off state for a named channel.  Default implementation calls ``turn_on`` or ``turn_off``."""
        if is_on:
            self.turn_on(channel)
        else:
            self.turn_off(channel)

    @abc.abstractmethod
    def get_intensity(self, channel: str) -> float:
        """Return last-set intensity for *channel*."""

    @abc.abstractmethod
    def is_on(self, channel: str) -> bool:
        """Return True if *channel* is currently enabled."""


# ---------------------------------------------------------------------------
# Concrete device: IO-routed channels (MCU / NI-DAQ endpoints)
# ---------------------------------------------------------------------------

class IORoutedIlluminationDevice(IlluminationDevice):
    """Illumination device whose channels are wired via ``IORegistry`` endpoints.

    Each channel has an optional intensity ``BoundEndpoint`` (analog) and an
    optional shutter ``BoundEndpoint`` (digital).  When a shutter endpoint is
    absent the microcontroller's global TTL command is used as a fallback.
    Intensity LUT calibration is applied per channel when provided.

    This device also exposes the multi-port MCU API (firmware v1.0+) so that
    callers needing to synchronise multiple ports at once can do so.
    """

    def __init__(
        self,
        channel_endpoints: Dict[str, Tuple[Optional["BoundEndpoint"], Optional["BoundEndpoint"]]],
        microcontroller: Optional["Microcontroller"] = None,
        luts: Optional[Dict[str, Dict]] = None,
    ):
        """
        Args:
            channel_endpoints: ``{channel_name: (intensity_ep, shutter_ep)}``.
                Either endpoint may be ``None``.
            microcontroller: MCU used as shutter fallback and for multi-port API.
            luts: Per-channel calibration tables
                ``{name: {"power_percent": [...], "dac_percent": [...]}}``.
        """
        self._channel_endpoints = channel_endpoints
        self._microcontroller = microcontroller
        self._luts: Dict[str, Dict] = luts or {}
        self._intensity: Dict[str, float] = {n: 0.0 for n in channel_endpoints}
        self._is_on_state: Dict[str, bool] = {n: False for n in channel_endpoints}
        self._port_intensity: Dict[int, float] = {i: 0.0 for i in range(NUM_ILLUMINATION_PORTS)}
        self._port_is_on: Dict[int, bool] = {i: False for i in range(NUM_ILLUMINATION_PORTS)}

    @property
    def channel_names(self) -> List[str]:
        return list(self._channel_endpoints.keys())

    def initialize(self) -> None:
        pass

    def shut_down(self) -> None:
        self.turn_off_all()

    def set_intensity(self, channel: str, intensity: float) -> None:
        intensity = float(np.clip(intensity, 0, 100))
        intensity_ep, _ = self._channel_endpoints[channel]
        effective = self._apply_lut(channel, intensity)
        if intensity_ep is not None:
            intensity_ep.set_analog(effective)
        self._intensity[channel] = intensity

    def turn_on(self, channel: str) -> None:
        _, shutter_ep = self._channel_endpoints[channel]
        if shutter_ep is not None:
            shutter_ep.set_digital(True)
            shutter_ep.wait()
        elif self._microcontroller is not None:
            self._microcontroller.turn_on_illumination()
            self._microcontroller.wait_till_operation_is_completed()
        self._is_on_state[channel] = True

    def turn_off(self, channel: str) -> None:
        _, shutter_ep = self._channel_endpoints[channel]
        if shutter_ep is not None:
            shutter_ep.set_digital(False)
            shutter_ep.wait()
        elif self._microcontroller is not None:
            self._microcontroller.turn_off_illumination()
            self._microcontroller.wait_till_operation_is_completed()
        self._is_on_state[channel] = False

    def get_intensity(self, channel: str) -> float:
        return self._intensity.get(channel, 0.0)

    def is_on(self, channel: str) -> bool:
        return self._is_on_state.get(channel, False)

    # -- LUT helper ----------------------------------------------------------

    def _apply_lut(self, channel: str, intensity: float) -> float:
        lut = self._luts.get(channel)
        if lut is None:
            return float(np.clip(intensity, 0, 100))
        intensity = float(np.clip(intensity, 0, 100))
        dac = float(np.interp(intensity, lut["power_percent"], lut["dac_percent"]))
        return float(np.clip(dac, 0, 100))

    # -- Multi-port MCU API (firmware v1.0+) ---------------------------------

    def _require_multi_port(self) -> None:
        if self._microcontroller is None or not self._microcontroller.supports_multi_port():
            raise RuntimeError(
                "Firmware does not support multi-port illumination commands. "
                "Update firmware to version 1.0 or later."
            )

    def set_port_intensity(self, port_index: int, intensity: float) -> None:
        """Set intensity for a specific MCU port without changing on/off state."""
        self._require_multi_port()
        if not 0 <= port_index < NUM_ILLUMINATION_PORTS:
            raise ValueError(f"Invalid port index: {port_index}")
        self._microcontroller.set_port_intensity(port_index, intensity)
        self._microcontroller.wait_till_operation_is_completed()
        self._port_intensity[port_index] = intensity

    def turn_on_port(self, port_index: int) -> None:
        """Turn on a specific MCU illumination port."""
        self._require_multi_port()
        if not 0 <= port_index < NUM_ILLUMINATION_PORTS:
            raise ValueError(f"Invalid port index: {port_index}")
        self._microcontroller.turn_on_port(port_index)
        self._microcontroller.wait_till_operation_is_completed()
        self._port_is_on[port_index] = True

    def turn_off_port(self, port_index: int) -> None:
        """Turn off a specific MCU illumination port."""
        self._require_multi_port()
        if not 0 <= port_index < NUM_ILLUMINATION_PORTS:
            raise ValueError(f"Invalid port index: {port_index}")
        self._microcontroller.turn_off_port(port_index)
        self._microcontroller.wait_till_operation_is_completed()
        self._port_is_on[port_index] = False

    def set_port_illumination(self, port_index: int, intensity: float, turn_on: bool) -> None:
        """Set intensity and on/off for an MCU port in one command."""
        self._require_multi_port()
        if not 0 <= port_index < NUM_ILLUMINATION_PORTS:
            raise ValueError(f"Invalid port index: {port_index}")
        self._microcontroller.set_port_illumination(port_index, intensity, turn_on)
        self._microcontroller.wait_till_operation_is_completed()
        self._port_intensity[port_index] = intensity
        self._port_is_on[port_index] = turn_on

    def turn_on_multiple_ports(self, port_indices: List[int]) -> None:
        """Turn on multiple MCU ports simultaneously."""
        if not port_indices:
            return
        self._require_multi_port()
        port_mask = on_mask = 0
        for i in port_indices:
            if not 0 <= i < NUM_ILLUMINATION_PORTS:
                raise ValueError(f"Invalid port index: {i}")
            port_mask |= 1 << i
            on_mask |= 1 << i
        self._microcontroller.set_multi_port_mask(port_mask, on_mask)
        self._microcontroller.wait_till_operation_is_completed()
        for i in port_indices:
            self._port_is_on[i] = True

    def turn_off_all_ports(self) -> None:
        """Turn off all MCU illumination ports."""
        self._require_multi_port()
        self._microcontroller.turn_off_all_ports()
        self._microcontroller.wait_till_operation_is_completed()
        for i in range(NUM_ILLUMINATION_PORTS):
            self._port_is_on[i] = False

    def get_active_ports(self) -> List[int]:
        """Return list of currently active port indices."""
        return [i for i in range(NUM_ILLUMINATION_PORTS) if self._port_is_on[i]]


# ---------------------------------------------------------------------------
# Concrete device: single-channel NI-DAQ lines (analog intensity + digital shutter)
# ---------------------------------------------------------------------------

class NIDAQIlluminationDevice(IlluminationDevice):
    """Illumination device backed by one NI-DAQ AO line and one DO line.

    This is a convenience wrapper for the common case where a single
    fluorescence channel is driven directly from NI-DAQ hardware:

    - Analog output (AO) line for intensity control
    - Digital output (DO) line for shutter/on-off control

    The endpoints are provided as ``BoundEndpoint`` instances obtained from
    ``IORegistry`` (typically via an ``illumination_devices`` entry).
    """

    def __init__(
        self,
        channel_name: str,
        intensity_endpoint: "BoundEndpoint",
        shutter_endpoint: "BoundEndpoint",
    ) -> None:
        self._channel_name = channel_name
        self._intensity_ep = intensity_endpoint
        self._shutter_ep = shutter_endpoint
        self._intensity: float = 0.0
        self._is_on: bool = False

    # IlluminationDevice API -------------------------------------------------

    @property
    def channel_names(self) -> List[str]:
        return [self._channel_name]

    def initialize(self) -> None:
        # NI-DAQ endpoints are configured at IORegistry construction time.
        # Nothing to do here.
        pass

    def shut_down(self) -> None:
        try:
            self.turn_off(self._channel_name)
        except Exception as exc:
            logger.warning(
                f"[NIDAQIlluminationDevice] shut_down failed for '{self._channel_name}': {exc}"
            )

    def set_intensity(self, channel: str, intensity: float) -> None:
        if channel != self._channel_name:
            logger.warning(
                f"[NIDAQIlluminationDevice] set_intensity called for unknown channel '{channel}'"
            )
            return
        intensity = float(np.clip(intensity, 0, 100))
        self._intensity_ep.set_analog(intensity)
        self._intensity = intensity

    def turn_on(self, channel: str) -> None:
        if channel != self._channel_name:
            logger.warning(
                f"[NIDAQIlluminationDevice] turn_on called for unknown channel '{channel}'"
            )
            return
        self._shutter_ep.set_digital(True)
        self._shutter_ep.wait()
        self._is_on = True

    def turn_off(self, channel: str) -> None:
        if channel != self._channel_name:
            logger.warning(
                f"[NIDAQIlluminationDevice] turn_off called for unknown channel '{channel}'"
            )
            return
        self._shutter_ep.set_digital(False)
        self._shutter_ep.wait()
        self._is_on = False

    def get_intensity(self, channel: str) -> float:
        if channel != self._channel_name:
            logger.warning(
                f"[NIDAQIlluminationDevice] get_intensity called for unknown channel '{channel}'"
            )
            return 0.0
        return self._intensity

    def is_on(self, channel: str) -> bool:
        if channel != self._channel_name:
            logger.warning(
                f"[NIDAQIlluminationDevice] is_on called for unknown channel '{channel}'"
            )
            return False
        return self._is_on


# ---------------------------------------------------------------------------
# Concrete device: serial light source (CoolLED, LDI, CELESTA, Andor, ...)
# ---------------------------------------------------------------------------

class SerialIlluminationDevice(IlluminationDevice):
    """Illumination device backed by a serial ``LightSource``.

    Intensity is always controlled via the serial protocol.  Shutter control
    can use either the LightSource's own API or a per-channel
    ``BoundEndpoint`` (e.g. an NI-DAQ TTL line), whichever is available.

    Args:
        light_source: The ``squid.abc.LightSource`` driver instance.
        channel_serial_keys: ``{channel_name: device_internal_key}``.
            ``device_internal_key`` is whatever key the LightSource accepts in
            ``set_intensity`` / ``set_shutter_state`` (letter for CoolLED,
            wavelength int for some others).
        shutter_endpoints: Optional per-channel shutter ``BoundEndpoint``;
            when present the TTL path is used instead of the serial API.
    """

    def __init__(
        self,
        light_source: "LightSource",
        channel_serial_keys: Dict[str, object],
        shutter_endpoints: Optional[Dict[str, Optional["BoundEndpoint"]]] = None,
    ):
        self._light_source = light_source
        self._channel_serial_keys = channel_serial_keys
        self._shutter_endpoints: Dict[str, Optional["BoundEndpoint"]] = shutter_endpoints or {}
        self._intensity: Dict[str, float] = {n: 0.0 for n in channel_serial_keys}
        self._is_on_state: Dict[str, bool] = {n: False for n in channel_serial_keys}

    @property
    def channel_names(self) -> List[str]:
        return list(self._channel_serial_keys.keys())

    def initialize(self) -> None:
        self._light_source.initialize()

    def shut_down(self) -> None:
        self._light_source.shut_down()

    def set_intensity(self, channel: str, intensity: float) -> None:
        intensity = float(np.clip(intensity, 0, 100))
        key = self._channel_serial_keys[channel]
        self._light_source.set_intensity(key, intensity)
        self._intensity[channel] = intensity

    def turn_on(self, channel: str) -> None:
        ep = self._shutter_endpoints.get(channel)
        if ep is not None:
            ep.set_digital(True)
            ep.wait()
        else:
            key = self._channel_serial_keys[channel]
            self._light_source.set_shutter_state(key, on=True)
        self._is_on_state[channel] = True

    def turn_off(self, channel: str) -> None:
        ep = self._shutter_endpoints.get(channel)
        if ep is not None:
            ep.set_digital(False)
            ep.wait()
        else:
            key = self._channel_serial_keys[channel]
            self._light_source.set_shutter_state(key, on=False)
        self._is_on_state[channel] = False

    def get_intensity(self, channel: str) -> float:
        return self._intensity.get(channel, 0.0)

    def is_on(self, channel: str) -> bool:
        return self._is_on_state.get(channel, False)

    @property
    def light_source(self) -> "LightSource":
        """Expose the underlying LightSource for callers that need direct access."""
        return self._light_source


# ---------------------------------------------------------------------------
# LED matrix defaults (Teensy / SciMicroscopy channel-name conventions)
# ---------------------------------------------------------------------------

_DEFAULT_UNIFIED_MODES: Dict[str, Dict[str, Any]] = {
    "bf_full": {
        "source_code": 0,
        "label": "BF full",
        "matrix_channel_name": "BF LED matrix full",
    },
    "df": {
        "source_code": 3,
        "label": "Dark field",
        "matrix_channel_name": "DF LED matrix",
    },
    "low_na": {
        "source_code": 4,
        "label": "BF low NA",
        "matrix_channel_name": "BF LED matrix low NA",
    },
    "left_half": {
        "source_code": 1,
        "label": "BF left half",
        "matrix_channel_name": "BF LED matrix left half",
    },
    "right_half": {
        "source_code": 2,
        "label": "BF right half",
        "matrix_channel_name": "BF LED matrix right half",
    },
    "top_half": {
        "source_code": 7,
        "label": "BF top half",
        "matrix_channel_name": "BF LED matrix top half",
    },
    "bottom_half": {
        "source_code": 8,
        "label": "BF bottom half",
        "matrix_channel_name": "BF LED matrix bottom half",
    },
    "bf_r": {
        "source_code": 0,
        "label": "BF (red)",
        "matrix_channel_name": "BF LED matrix full_R",
    },
    "bf_g": {
        "source_code": 0,
        "label": "BF (green)",
        "matrix_channel_name": "BF LED matrix full_G",
    },
    "bf_b": {
        "source_code": 0,
        "label": "BF (blue)",
        "matrix_channel_name": "BF LED matrix full_B",
    },
}

_DEFAULT_LEGACY_TO_MODE: Dict[str, str] = {
    "BF LED matrix full": "bf_full",
    "DF LED matrix": "df",
    "BF LED matrix low NA": "low_na",
    "BF LED matrix left half": "left_half",
    "BF LED matrix right half": "right_half",
    "BF LED matrix top half": "top_half",
    "BF LED matrix bottom half": "bottom_half",
    "BF LED matrix full_R": "bf_r",
    "BF LED matrix full_G": "bf_g",
    "BF LED matrix full_B": "bf_b",
    "BF LED matrix full_RGB": "bf_full",
}


# ---------------------------------------------------------------------------
# Concrete device: LED matrix (SciMicroscopy array or plain MCU patterns)
# ---------------------------------------------------------------------------

class LEDMatrixIlluminationDevice(IlluminationDevice):
    """Illumination device for a programmable LED array.

    **Classic mode:** Each channel name maps to a ``source_code`` (MCU pattern).

    **Unified mode (Teensy / single logical channel):** One channel name (e.g.
    ``"LED matrix"``) with a separate **mode** (BF, DF, halves, RGB, etc.).
    Configure with ``illumination_devices[].config``::

        unified: true
        unified_channel_name: "LED matrix"
        modes: { bf_full: { source_code: 0, label: "BF full", ... }, ... }

    Either a ``SciMicroscopyLEDArray`` or a plain ``Microcontroller`` can
    back this device:

    - SciMicroscopy array: ``apply_channel_configuration(name, intensity)`` /
      ``turn_on_illumination()`` / ``turn_off_illumination()``
    - Plain MCU: ``apply_led_matrix_channel_configuration(name, code, intensity)`` /
      ``turn_on_illumination()`` / ``turn_off_illumination()``
    """

    def __init__(
        self,
        channel_source_codes: Dict[str, int],
        microcontroller: Optional["Microcontroller"] = None,
        sci_array: Optional["SciMicroscopyLEDArray"] = None,
        *,
        unified: bool = False,
        unified_channel_name: str = "LED matrix",
        modes: Optional[Dict[str, Dict[str, Any]]] = None,
        legacy_channel_to_mode: Optional[Dict[str, str]] = None,
    ):
        """
        Args:
            channel_source_codes: ``{channel_name: source_code}`` (ignored when unified)
            microcontroller: MCU for plain LED matrix control.
            sci_array: SciMicroscopyLEDArray instance (takes priority over MCU).
            unified: If True, expose a single channel and use ``set_matrix_mode``.
            unified_channel_name: Logical channel name when unified.
            modes: ``{mode_key: {source_code, label, matrix_channel_name?}}``
            legacy_channel_to_map: Map old channel names to mode keys (for acquisition).
        """
        if microcontroller is None and sci_array is None:
            raise ValueError(
                "LEDMatrixIlluminationDevice requires microcontroller or sci_array"
            )
        self._microcontroller = microcontroller
        self._sci_array = sci_array
        self._unified = bool(unified)
        self._unified_channel_name = unified_channel_name

        if self._unified:
            self._modes: Dict[str, Dict[str, Any]] = dict(modes) if modes else dict(_DEFAULT_UNIFIED_MODES)
            self._legacy_to_mode: Dict[str, str] = (
                dict(legacy_channel_to_mode)
                if legacy_channel_to_mode
                else dict(_DEFAULT_LEGACY_TO_MODE)
            )
            keys = list(self._modes.keys())
            if not keys:
                raise ValueError("unified LED matrix requires at least one mode")
            self._active_mode_key: str = keys[0]
            self._channel_source_codes = {}
            self._intensity = {unified_channel_name: 0.0}
            self._is_on_state = {unified_channel_name: False}
        else:
            self._modes = {}
            self._legacy_to_mode = {}
            self._active_mode_key = ""
            self._channel_source_codes = channel_source_codes
            self._intensity = {n: 0.0 for n in channel_source_codes}
            self._is_on_state = {n: False for n in channel_source_codes}

        self._active_channel: Optional[str] = None

    @property
    def is_unified_mode(self) -> bool:
        return self._unified

    @property
    def unified_channel_name(self) -> str:
        return self._unified_channel_name

    @property
    def legacy_channel_to_mode(self) -> Dict[str, str]:
        return self._legacy_to_mode

    def matrix_mode_items(self) -> List[Tuple[str, str]]:
        """Return ``(mode_key, label)`` for UI combo boxes."""
        out: List[Tuple[str, str]] = []
        for key, spec in self._modes.items():
            label = str(spec.get("label", key))
            out.append((key, label))
        return out

    def get_matrix_mode(self) -> str:
        return self._active_mode_key

    def set_matrix_mode(self, mode_key: str) -> None:
        if not self._unified:
            logger.warning("set_matrix_mode: device is not in unified mode")
            return
        if mode_key not in self._modes:
            logger.warning(f"set_matrix_mode: unknown mode '{mode_key}'")
            return
        self._active_mode_key = mode_key
        u = self._unified_channel_name
        if self._is_on_state.get(u, False):
            self.set_intensity(u, self._intensity.get(u, 0.0))

    def _apply_unified_intensity(self, intensity: float) -> None:
        spec = self._modes[self._active_mode_key]
        source_code = int(spec["source_code"])
        mcu_name = str(spec.get("matrix_channel_name", "BF LED matrix full"))
        if self._sci_array is not None:
            self._sci_array.apply_channel_configuration(mcu_name, intensity)
        elif self._microcontroller is not None:
            self._microcontroller.apply_led_matrix_channel_configuration(
                mcu_name, source_code, intensity
            )

    @property
    def channel_names(self) -> List[str]:
        if self._unified:
            return [self._unified_channel_name]
        return list(self._channel_source_codes.keys())

    def initialize(self) -> None:
        pass

    def shut_down(self) -> None:
        self.turn_off_all()

    def set_intensity(self, channel: str, intensity: float) -> None:
        """Configure intensity. In unified mode *channel* must be the unified name."""
        intensity = float(np.clip(intensity, 0, 100))
        if self._unified:
            if channel != self._unified_channel_name:
                logger.warning(
                    f"set_intensity: expected unified channel '{self._unified_channel_name}', got '{channel}'"
                )
                return
            self._intensity[channel] = intensity
            self._active_channel = channel
            self._apply_unified_intensity(intensity)
            return

        self._intensity[channel] = intensity
        self._active_channel = channel
        if self._sci_array is not None:
            self._sci_array.apply_channel_configuration(channel, intensity)
        elif self._microcontroller is not None:
            source_code = self._channel_source_codes[channel]
            self._microcontroller.apply_led_matrix_channel_configuration(
                channel, source_code, intensity
            )

    def turn_on(self, channel: str) -> None:
        """Activate the LED pattern for *channel*."""
        if self._unified and channel != self._unified_channel_name:
            logger.warning(f"turn_on: expected unified channel '{self._unified_channel_name}'")
            return
        self._active_channel = channel
        key = self._unified_channel_name if self._unified else channel
        self._is_on_state[key] = True
        if self._unified:
            self._apply_unified_intensity(self._intensity.get(self._unified_channel_name, 0.0))
        if self._sci_array is not None:
            self._sci_array.turn_on_illumination()
        elif self._microcontroller is not None:
            self._microcontroller.turn_on_illumination()

    def turn_off(self, channel: str) -> None:
        """Deactivate LED illumination."""
        if self._unified and channel != self._unified_channel_name:
            logger.warning(f"turn_off: expected unified channel '{self._unified_channel_name}'")
            return
        if self._sci_array is not None:
            self._sci_array.turn_off_illumination()
        elif self._microcontroller is not None:
            self._microcontroller.turn_off_illumination()
        self._is_on_state[self._unified_channel_name if self._unified else channel] = False

    def get_intensity(self, channel: str) -> float:
        if self._unified:
            return self._intensity.get(self._unified_channel_name, 0.0)
        return self._intensity.get(channel, 0.0)

    def is_on(self, channel: str) -> bool:
        if self._unified:
            return self._is_on_state.get(self._unified_channel_name, False)
        return self._is_on_state.get(channel, False)


# ---------------------------------------------------------------------------
# LUT loading helper
# ---------------------------------------------------------------------------

def load_intensity_luts(
    calibrations_dir: Path,
    channel_wavelengths: Dict[str, Optional[int]],
) -> Dict[str, Dict]:
    """Load intensity calibration LUTs for a set of channels.

    Args:
        calibrations_dir: Directory containing ``{wavelength}.csv`` files.
        channel_wavelengths: ``{channel_name: wavelength_nm}`` mapping.

    Returns:
        ``{channel_name: {"power_percent": [...], "dac_percent": [...]}}``
        for channels where a calibration file was found.
    """
    luts: Dict[str, Dict] = {}
    if not calibrations_dir.exists():
        return luts
    for ch_name, wl in channel_wavelengths.items():
        if wl is None:
            continue
        csv_path = calibrations_dir / f"{wl}.csv"
        if not csv_path.exists():
            continue
        try:
            df = pd.read_csv(csv_path)
            if "DAC Percent" in df.columns and "Optical Power (mW)" in df.columns:
                max_power = df["Optical Power (mW)"].max()
                if max_power > 0:
                    luts[ch_name] = {
                        "power_percent": (df["Optical Power (mW)"] / max_power * 100).values,
                        "dac_percent": np.clip(df["DAC Percent"].values, 0, 100),
                    }
        except Exception as exc:
            logger.warning(f"Could not load calibration from {csv_path}: {exc}")
    return luts


# ---------------------------------------------------------------------------
# IlluminationController — compositor
# ---------------------------------------------------------------------------

class IlluminationController:
    """Aggregates multiple ``IlluminationDevice`` instances behind a uniform API.

    All channels from all devices are merged into a single flat
    ``channel_name → IlluminationDevice`` map.  Callers use channel names
    directly without knowing which device owns each one.

    **Primary channel-name API**::

        set_channel_intensity(name, intensity)
        set_channel_state(name, is_on)
        turn_off_all()
        snapshot() -> IlluminationSnapshot
        restore(snapshot)
        save_preset(name) / load_preset(name)

    **Backward-compatible wavelength shims** (delegate to primary API)::

        set_intensity(wavelength, intensity)
        turn_on_illumination(channel=wavelength)
        turn_off_illumination(channel=wavelength)

    **Multi-port forwarding** (delegates to the first IORoutedIlluminationDevice)::

        set_port_intensity / turn_on_port / turn_off_port
        set_port_illumination / turn_on_multiple_ports / turn_off_all_ports
    """

    def __init__(self, devices: List[IlluminationDevice]):
        """
        Args:
            devices: All illumination devices for this microscope.
                     Channel names must be unique across all devices.
        """
        self._devices = list(devices)
        self._channel_map: Dict[str, IlluminationDevice] = {}
        self._log = squid.logging.get_logger(__class__.__name__)
        for dev in devices:
            for ch in dev.channel_names:
                if ch in self._channel_map:
                    raise ValueError(
                        f"Duplicate illumination channel name '{ch}' from "
                        f"{dev.__class__.__name__} conflicts with "
                        f"{self._channel_map[ch].__class__.__name__}"
                    )
                self._channel_map[ch] = dev

        self._channel_state: Dict[str, ChannelState] = {
            ch: ChannelState() for ch in self._channel_map
        }
        self.presets: Dict[str, IlluminationPreset] = {}

        self._led_matrix_unified: Optional[LEDMatrixIlluminationDevice] = None
        for _dev in self._devices:
            if isinstance(_dev, LEDMatrixIlluminationDevice) and _dev.is_unified_mode:
                self._led_matrix_unified = _dev
                break

        # Live view: when False, channel toggles update logical state only; hardware
        # follows when set_streaming_active(True). Acquisition paths use force_hardware=True.
        self._streaming_active: bool = False
        self._hardware_asserted: Dict[str, bool] = {ch: False for ch in self._channel_map}

    # -- Device access -------------------------------------------------------

    @property
    def devices(self) -> List[IlluminationDevice]:
        return list(self._devices)

    def get_device_for_channel(self, channel_name: str) -> Optional[IlluminationDevice]:
        """Return the device that owns *channel_name*, or None."""
        return self._channel_map.get(channel_name)

    def has_unified_led_matrix(self) -> bool:
        """True when the LED matrix is configured as one channel + mode switching."""
        return self._led_matrix_unified is not None

    def unified_led_matrix_channel_name(self) -> Optional[str]:
        """Logical channel name for unified LED matrix (e.g. 'LED matrix'), or None."""
        if self._led_matrix_unified is None:
            return None
        return self._led_matrix_unified.unified_channel_name

    def set_led_matrix_mode(self, mode_key: str) -> bool:
        """Select LED matrix pattern (unified device only). Returns False if N/A."""
        if self._led_matrix_unified is None:
            return False
        self._led_matrix_unified.set_matrix_mode(mode_key)
        return True

    def get_led_matrix_mode(self) -> Optional[str]:
        """Current LED matrix mode key, or None if not using unified matrix."""
        if self._led_matrix_unified is None:
            return None
        return self._led_matrix_unified.get_matrix_mode()

    def illumination_maps_to_unified_led_matrix(self, illumination_channel_name: Optional[str]) -> bool:
        """True if *illumination_channel_name* (from acquisition config) maps to the unified LED matrix.

        Matches the unified logical name and legacy per-mode aliases (see ``_resolve_led_matrix_channel``).
        """
        if not illumination_channel_name or self._led_matrix_unified is None:
            return False
        return self._resolve_led_matrix_channel(illumination_channel_name) is not None

    def snapshot_key_for_acquisition_illumination_channel(self, illumination_channel_name: Optional[str]) -> Optional[str]:
        """Return the ``_channel_state`` / snapshot dict key for an acquisition ``illumination_channel`` string.

        Legacy aliases (per-mode names) map to the unified logical channel name used in ``snapshot()``.
        """
        if not illumination_channel_name:
            return None
        if illumination_channel_name in self._channel_state:
            return illumination_channel_name
        lm = self._resolve_led_matrix_channel(illumination_channel_name)
        if lm is not None:
            unified_name, _mode_key = lm
            return unified_name
        return None

    def led_matrix_mode_items(self) -> List[Tuple[str, str]]:
        """``(mode_key, label)`` pairs for unified LED matrix, or empty list."""
        if self._led_matrix_unified is None:
            return []
        return self._led_matrix_unified.matrix_mode_items()

    def _resolve_led_matrix_channel(
        self, channel_name: str
    ) -> Optional[Tuple[str, Optional[str]]]:
        """If *channel_name* refers to unified LED matrix, return ``(unified_name, mode_key)``.

        *mode_key* is None when *channel_name* is already the unified channel (keep current mode).
        """
        dev = self._led_matrix_unified
        if dev is None:
            return None
        if channel_name == dev.unified_channel_name:
            return (dev.unified_channel_name, None)
        mode_key = dev.legacy_channel_to_mode.get(channel_name)
        if mode_key is not None:
            return (dev.unified_channel_name, mode_key)
        return None

    # -- Primary channel-name API --------------------------------------------

    @property
    def channel_names(self) -> List[str]:
        """Return all channel names across all devices."""
        return list(self._channel_map.keys())

    def set_channel_intensity(self, channel_name: str, intensity: float) -> None:
        """Set intensity for a named channel (0–100 %).

        Routes to whichever device owns *channel_name*.
        """
        intensity = float(np.clip(intensity, 0, 100))
        lm = self._resolve_led_matrix_channel(channel_name)
        if lm is not None:
            unified_name, mode_key = lm
            dev = self._channel_map.get(unified_name)
            if isinstance(dev, LEDMatrixIlluminationDevice):
                if mode_key is not None:
                    dev.set_matrix_mode(mode_key)
                dev.set_intensity(unified_name, intensity)
                self._channel_state[unified_name].intensity = intensity
            return

        dev = self._channel_map.get(channel_name)
        if dev is None:
            logger.warning(f"set_channel_intensity: unknown channel '{channel_name}'")
            return
        dev.set_intensity(channel_name, intensity)
        self._channel_state[channel_name].intensity = intensity

    def set_streaming_active(self, active: bool) -> None:
        """Gate manual illumination hardware to camera live streaming.

        When *active* is True, logical ``is_on`` channels are asserted on hardware.
        When False, all hardware outputs are turned off while preserving logical
        on/off and intensity in :attr:`_channel_state` (UI unchanged).
        """
        self._streaming_active = bool(active)
        if not self._streaming_active:
            self.turn_off_all_hardware_preserving_state()
        else:
            self.apply_logical_state_to_hardware()

    def is_streaming_active(self) -> bool:
        """True when live view has enabled illumination hardware gating."""
        return self._streaming_active

    def apply_logical_state_to_hardware(self) -> None:
        """Assert hardware for every channel that is logically on (requires streaming active)."""
        if not self._streaming_active:
            return
        for name, st in self._channel_state.items():
            if st.is_on:
                self.set_channel_state(name, True)

    def turn_off_all_hardware_preserving_state(self) -> None:
        """Turn off every device output without changing logical on/off flags."""
        for dev in self._devices:
            try:
                dev.turn_off_all()
            except Exception as exc:
                logger.warning(f"turn_off_all_hardware_preserving_state on {dev.__class__.__name__} failed: {exc}")
        for ch in self._hardware_asserted:
            self._hardware_asserted[ch] = False

    def set_channel_state(self, channel_name: str, is_on: bool, force_hardware: bool = False) -> None:
        """Set the on/off state for a named channel.

        Args:
            channel_name: Logical channel name (or LED matrix alias).
            is_on: True to turn on, False to turn off.
            force_hardware: If True, always command hardware (acquisition / legacy).
                If False, hardware is commanded only when :meth:`set_streaming_active`
                has enabled streaming (live view).
        """
        apply_hw = force_hardware or self._streaming_active
        self._channel_state[channel_name].is_on = is_on
        if apply_hw and not self._hardware_asserted[channel_name]:
            lm = self._resolve_led_matrix_channel(channel_name)
            if lm is not None:
                unified_name, mode_key = lm
                dev = self._channel_map.get(unified_name)
                if isinstance(dev, LEDMatrixIlluminationDevice):
                    if mode_key is not None:
                        dev.set_matrix_mode(mode_key)
                    dev.set_on_off_state(unified_name, is_on)
                    self._hardware_asserted[unified_name] = is_on
                return
            dev = self._channel_map.get(channel_name)
            if dev is None:
                logger.warning(f"set_channel_state: unknown channel '{channel_name}'")
                return
            dev.set_on_off_state(channel_name, is_on)
            self._hardware_asserted[channel_name] = is_on

    def turn_off_all(self, *, preserve_logical_state: bool = False) -> None:
        """Turn off all channels on all devices.

        Args:
            preserve_logical_state: If True, only hardware is turned off; logical
                ``is_on`` flags and intensities are unchanged (e.g. stop live view).
        """
        if preserve_logical_state:
            self.turn_off_all_hardware_preserving_state()
            return
        for dev in self._devices:
            try:
                dev.turn_off_all()
            except Exception as exc:
                logger.warning(f"turn_off_all on {dev.__class__.__name__} failed: {exc}")
        for state in self._channel_state.values():
            state.is_on = False
        for ch in self._hardware_asserted:
            self._hardware_asserted[ch] = False

    # -- Snapshot / restore / preset -----------------------------------------

    def snapshot(self) -> IlluminationSnapshot:
        """Capture current illumination state for all channels."""
        states = {
            ch: ChannelState(s.intensity, s.is_on)
            for ch, s in self._channel_state.items()
        }
        return IlluminationSnapshot(states)

    def restore(self, snapshot: IlluminationSnapshot, *, force_hardware: bool = False) -> None:
        """Restore illumination state from a snapshot.

        Args:
            force_hardware: When True, always drive hardware (e.g. after fast acquisition).
                When False, obeys streaming gate (startup cache: logical + UI only until live).
        """
        for name, state in snapshot.channel_states.items():
            try:
                self.set_channel_intensity(name, state.intensity)
                if state.is_on:
                    self.set_channel_state(name, True, force_hardware=force_hardware)
                else:
                    self.set_channel_state(name, False, force_hardware=force_hardware)
            except Exception as exc:
                logger.warning(f"Failed to restore channel '{name}': {exc}")

    def save_preset(self, name: str) -> IlluminationPreset:
        """Save the current illumination state as a named preset."""
        preset = IlluminationPreset(name=name, snapshot=self.snapshot())
        self.presets[name] = preset
        return preset

    def load_preset(self, name: str) -> None:
        """Load and apply a named preset.

        Raises:
            KeyError: if preset name is not found.
        """
        preset = self.presets.get(name)
        if preset is None:
            raise KeyError(f"Illumination preset '{name}' not found")
        self.restore(preset.snapshot, force_hardware=True)

    def delete_preset(self, name: str) -> None:
        """Delete a named preset."""
        self.presets.pop(name, None)

    def list_presets(self) -> List[str]:
        """Return sorted list of preset names."""
        return sorted(self.presets.keys())

    def save_presets_to_file(self, path: str) -> None:
        """Persist all presets to a YAML file."""
        import yaml
        data = {
            preset_name: {
                ch_name: {"intensity": s.intensity, "is_on": s.is_on}
                for ch_name, s in preset.snapshot.channel_states.items()
            }
            for preset_name, preset in self.presets.items()
        }
        with open(path, "w") as f:
            yaml.safe_dump(data, f)

    def load_presets_from_file(self, path: str) -> None:
        """Load presets from a YAML file, merging with existing presets."""
        import yaml
        try:
            with open(path, "r") as f:
                data = yaml.safe_load(f) or {}
            for preset_name, channels in data.items():
                states = {
                    ch_name: ChannelState(
                        intensity=float(v.get("intensity", 0)),
                        is_on=bool(v.get("is_on", False)),
                    )
                    for ch_name, v in channels.items()
                }
                self.presets[preset_name] = IlluminationPreset(
                    name=preset_name,
                    snapshot=IlluminationSnapshot(states),
                )
        except Exception as exc:
            logger.warning(f"Failed to load presets from '{path}': {exc}")

    # -- Backward-compatible wavelength API shims ----------------------------

    def _channel_name_for_wavelength(self, wavelength: int) -> Optional[str]:
        """Resolve a wavelength integer to a channel name.

        Searches channel names for an embedded wavelength number.
        """
        for ch_name in self._channel_map:
            if _extract_wavelength(ch_name) == wavelength:
                return ch_name
        return None

    def turn_on_illumination(self, channel=None) -> None:
        """Legacy: turn on illumination by wavelength integer.

        Delegates to ``set_channel_state`` when the wavelength can be resolved
        to a channel name.
        """
        if channel is not None:
            name = self._channel_name_for_wavelength(channel)
            if name is not None:
                self.set_channel_state(name, True, force_hardware=True)
                return
        logger.warning(f"turn_on_illumination: could not resolve channel={channel}")

    def turn_off_illumination(self, channel=None) -> None:
        """Legacy: turn off illumination by wavelength integer."""
        if channel is not None:
            name = self._channel_name_for_wavelength(channel)
            if name is not None:
                self.set_channel_state(name, False, force_hardware=True)
                return
        logger.warning(f"turn_off_illumination: could not resolve channel={channel}")

    def set_intensity(self, channel, intensity) -> None:
        """Legacy: set intensity by wavelength integer or channel name."""
        if isinstance(channel, int):
            name = self._channel_name_for_wavelength(channel)
        else:
            if channel in self._channel_map:
                name = channel
            elif self._resolve_led_matrix_channel(channel) is not None:
                self.set_channel_intensity(channel, intensity)
                return
            else:
                name = None
        if name is not None:
            self.set_channel_intensity(name, intensity)
        else:
            logger.warning(f"set_intensity: could not resolve channel={channel}")

    # -- Legacy state access -------------------------------------------------

    @property
    def is_on(self) -> Dict[str, bool]:
        """Legacy: ``{channel_name: is_on}`` for all channels."""
        return {ch: s.is_on for ch, s in self._channel_state.items()}

    @property
    def intensity_settings(self) -> Dict[str, float]:
        """Legacy: ``{channel_name: intensity}`` for all channels."""
        return {ch: s.intensity for ch, s in self._channel_state.items()}

    def get_intensity(self, channel) -> Optional[float]:
        """Legacy: get intensity for a wavelength or channel name."""
        if isinstance(channel, int):
            name = self._channel_name_for_wavelength(channel)
        else:
            name = channel
        if not name:
            return None
        lm = self._resolve_led_matrix_channel(name)
        if lm is not None:
            unified_name, _ = lm
            dev = self._channel_map.get(unified_name)
            if dev is not None:
                return dev.get_intensity(unified_name)
        if name in self._channel_map:
            return self._channel_map[name].get_intensity(name)
        return None

    def get_shutter_state(self) -> Dict[str, bool]:
        """Legacy: return on/off state dict for all channels."""
        return {ch: s.is_on for ch, s in self._channel_state.items()}

    # -- Multi-port forwarding (delegates to IORoutedIlluminationDevice) -----

    def _get_io_routed_device(self) -> Optional[IORoutedIlluminationDevice]:
        """Return the first IORoutedIlluminationDevice, or None."""
        for dev in self._devices:
            if isinstance(dev, IORoutedIlluminationDevice):
                return dev
        return None

    def set_port_intensity(self, port_index: int, intensity: float) -> None:
        """Multi-port: set intensity for an MCU port."""
        dev = self._get_io_routed_device()
        if dev:
            dev.set_port_intensity(port_index, intensity)

    def turn_on_port(self, port_index: int) -> None:
        """Multi-port: turn on an MCU port."""
        dev = self._get_io_routed_device()
        if dev:
            dev.turn_on_port(port_index)

    def turn_off_port(self, port_index: int) -> None:
        """Multi-port: turn off an MCU port."""
        dev = self._get_io_routed_device()
        if dev:
            dev.turn_off_port(port_index)

    def set_port_illumination(self, port_index: int, intensity: float, turn_on: bool) -> None:
        """Multi-port: set intensity and on/off state for an MCU port."""
        dev = self._get_io_routed_device()
        if dev:
            dev.set_port_illumination(port_index, intensity, turn_on)

    def turn_on_multiple_ports(self, port_indices: List[int]) -> None:
        """Multi-port: turn on multiple MCU ports simultaneously."""
        dev = self._get_io_routed_device()
        if dev:
            dev.turn_on_multiple_ports(port_indices)

    def turn_off_all_ports(self) -> None:
        """Multi-port: turn off all MCU ports."""
        dev = self._get_io_routed_device()
        if dev:
            dev.turn_off_all_ports()

    def get_active_ports(self) -> List[int]:
        """Multi-port: return list of currently active port indices."""
        dev = self._get_io_routed_device()
        return dev.get_active_ports() if dev else []

    # -- Cleanup -------------------------------------------------------------

    def close(self) -> None:
        """Shut down all devices."""
        for dev in self._devices:
            try:
                dev.shut_down()
            except Exception as exc:
                logger.warning(f"Device {dev.__class__.__name__} shut_down failed: {exc}")
