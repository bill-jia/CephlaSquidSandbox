"""
Illumination control system for the microscope.

This module provides a unified interface for controlling various illumination sources
(LEDs, lasers) with support for:
- Per-channel routing to the correct physical controller (Teensy or NI-DAQ), as
  declared in the machine config's per-channel ``io:`` blocks
- Intensity calibration using lookup tables (LUTs) for power-linear control
- Named illumination presets and full snapshot/restore for acquisition workflows
- Multiple light source types (Squid LEDs, lasers, LDI, CELESTA, etc.)

When ``channel_config`` and ``io_registry`` are both provided, each illumination
channel is routed independently to whichever physical controller is declared for it
in the machine config (Teensy MCU, NI-DAQ, or serial device), rather than using a
single global control mode.  The primary API is channel-name based:

    controller.set_channel_intensity("488nm", 50)
    controller.turn_on_channel("488nm")
    snapshot = controller.snapshot()
    ...
    controller.restore(snapshot)

The legacy wavelength-integer API (``set_intensity``, ``turn_on_illumination``, etc.)
is preserved as a backward-compatible shim.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

import numpy as np
import pandas as pd

from control.microcontroller import Microcontroller
from control.core.config import ConfigRepository
from control._def import ILLUMINATION_CODE

if TYPE_CHECKING:
    from control.core.io_controller import IORegistry, BoundEndpoint
    from control.models.illumination_config import IlluminationChannelConfig

logger = logging.getLogger(__name__)

# Number of illumination ports supported (matches firmware)
NUM_ILLUMINATION_PORTS = 16

_WL_RE = re.compile(r"(\d{3,4})(?:nm)?$", re.IGNORECASE)


def _extract_wavelength(channel_name: str) -> Optional[int]:
    """Extract a wavelength integer from a channel name like '488nm' or '488'."""
    m = _WL_RE.search(channel_name)
    if m:
        return int(m.group(1))
    return None


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class LightSourceType(Enum):
    """Enumeration of supported light source types."""
    SquidLED = 0      # Built-in LED array on Squid controller
    SquidLaser = 1    # Built-in laser on Squid controller
    LDI = 2           # Lumencor Light Engine
    CELESTA = 3       # Lumencor CELESTA light engine
    VersaLase = 4     # VersaLase laser system
    SCI = 5           # SciMicroscopy LED array
    AndorLaser = 6    # Andor laser system
    CoolLED = 7       # coolLED pE-400 / pE-400max


class IntensityControlMode(Enum):
    """Global intensity control mode.

    Deprecated: when ``channel_config`` and ``io_registry`` are both provided to
    ``IlluminationController``, routing is determined per-channel from the machine
    config.  These values are retained for backward compatibility with light-source
    drivers (LDI, CELESTA, etc.) that still require an explicit mode.
    """
    SquidControllerDAC = 0  # Control via DAC on microcontroller (analog voltage)
    Software = 1            # Control via software API of light source
    IOEndpoint = 2          # Control via IO endpoint abstraction (MCU or NI-DAQ)


class ShutterControlMode(Enum):
    """Global shutter control mode.

    Deprecated: see :class:`IntensityControlMode` for details.
    """
    TTL = 0       # Control via TTL signals from microcontroller
    Software = 1  # Control via software API of light source
    IOEndpoint = 2  # Control via IO endpoint abstraction (MCU or NI-DAQ)


# ---------------------------------------------------------------------------
# State dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ChannelState:
    """Runtime intensity and on/off state for a single illumination channel."""
    intensity: float = 0.0  # 0–100 %
    is_on: bool = False


@dataclass
class IlluminationSnapshot:
    """A point-in-time capture of all channel states, keyed by channel name.

    Keying by ``IlluminationChannel.name`` (from ``IlluminationChannelConfig``)
    makes the snapshot agnostic to whether the underlying controller is a Teensy
    MCU, NI-DAQ, or a serial light source.
    """
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
# Main controller
# ---------------------------------------------------------------------------

class IlluminationController:
    """Controls microscope illumination (LEDs, lasers, LED matrix).

    **Primary API (channel-name based, controller-agnostic):**

        set_channel_intensity(name, intensity)  — route to correct physical controller
        turn_on_channel(name)                   — open shutter / assert TTL
        turn_off_channel(name)                  — close shutter / de-assert TTL
        turn_off_all()                          — turn off every configured channel
        snapshot() -> IlluminationSnapshot      — capture current state
        restore(snapshot)                       — apply a previously captured state
        save_preset(name) / load_preset(name)   — named cached configurations

    **Backward-compatible wavelength API (delegates to primary API when possible):**

        set_intensity(wavelength, intensity)
        turn_on_illumination(channel=wavelength)
        turn_off_illumination(channel=wavelength)

    **Multi-port API (firmware v1.0+):**

        set_port_intensity / turn_on_port / turn_off_port
        turn_on_multiple_ports / turn_off_all_ports

    When ``channel_config`` and ``io_registry`` are both provided, each channel is
    routed to whichever physical controller (Teensy or NI-DAQ) is declared for it in
    the machine config, rather than using a global control mode.
    """

    def __init__(
        self,
        microcontroller: Microcontroller,
        intensity_control_mode: IntensityControlMode = IntensityControlMode.SquidControllerDAC,
        shutter_control_mode: ShutterControlMode = ShutterControlMode.TTL,
        light_source_type=None,
        light_source=None,
        disable_intensity_calibration: bool = False,
        io_registry: Optional["IORegistry"] = None,
        channel_config: Optional["IlluminationChannelConfig"] = None,
    ):
        """
        Initialize the illumination controller.

        Args:
            microcontroller: MCU interface for hardware communication
            intensity_control_mode: Legacy global intensity mode.  Ignored when
                ``channel_config`` and ``io_registry`` are both provided and a
                per-channel endpoint is found in the registry.
            shutter_control_mode: Legacy global shutter mode.  Same caveat as above.
            light_source_type: Type of external light source (LDI, CELESTA, etc.)
            light_source: External light source object for Software-mode control
            disable_intensity_calibration: Skip LUT-based calibration
            io_registry: IORegistry for per-channel endpoint routing
            channel_config: IlluminationChannelConfig defining available channels;
                enables per-channel routing, name-based API, and snapshot/preset support
        """
        self.microcontroller = microcontroller
        self.light_source_type = light_source_type
        self.light_source = light_source
        self.disable_intensity_calibration = disable_intensity_calibration
        self.io_registry = io_registry
        self.channel_config = channel_config

        # Legacy global modes — used as fallback for channels with no IORegistry endpoint
        if io_registry is not None:
            self.intensity_control_mode = (
                IntensityControlMode.IOEndpoint
                if intensity_control_mode == IntensityControlMode.SquidControllerDAC
                else intensity_control_mode
            )
            self.shutter_control_mode = (
                ShutterControlMode.IOEndpoint
                if shutter_control_mode == ShutterControlMode.TTL
                else shutter_control_mode
            )
        else:
            self.intensity_control_mode = intensity_control_mode
            self.shutter_control_mode = shutter_control_mode

        # Per-channel endpoint map: channel_name -> (intensity_ep, shutter_ep)
        # Built from IORegistry + channel_config; empty when either is absent.
        self._channel_endpoints: Dict[str, Tuple[Optional["BoundEndpoint"], Optional["BoundEndpoint"]]] = {}
        self._build_channel_endpoint_map()

        # Legacy wavelength-prefix map (kept as fallback when channel_config is absent)
        self._wavelength_to_endpoint_prefix: Dict[int, str] = {}
        if io_registry is not None and not self._channel_endpoints:
            self._build_wavelength_endpoint_map()

        # Default wavelength -> source code mappings (legacy MCU TTL path)
        default_mappings = {
            405: ILLUMINATION_CODE.ILLUMINATION_D1,
            470: ILLUMINATION_CODE.ILLUMINATION_D2,
            488: ILLUMINATION_CODE.ILLUMINATION_D2,
            545: ILLUMINATION_CODE.ILLUMINATION_D3,
            550: ILLUMINATION_CODE.ILLUMINATION_D3,
            555: ILLUMINATION_CODE.ILLUMINATION_D3,
            561: ILLUMINATION_CODE.ILLUMINATION_D3,
            638: ILLUMINATION_CODE.ILLUMINATION_D4,
            640: ILLUMINATION_CODE.ILLUMINATION_D4,
            730: ILLUMINATION_CODE.ILLUMINATION_D5,
            735: ILLUMINATION_CODE.ILLUMINATION_D5,
            750: ILLUMINATION_CODE.ILLUMINATION_D5,
        }
        self.channel_mappings_TTL = self._load_channel_mappings(default_mappings)

        # Legacy state dicts — kept for backward compat; updated alongside _channel_state
        self.channel_mappings_software: Dict = {}
        self.is_on: Dict = {}
        self.intensity_settings: Dict = {}
        self.current_channel = None
        self.intensity_luts: Dict = {}
        self.max_power: Dict = {}

        # Multi-port state (16 ports max, firmware v1.0+)
        self.port_is_on: Dict[int, bool] = {i: False for i in range(NUM_ILLUMINATION_PORTS)}
        self.port_intensity: Dict[int, float] = {i: 0.0 for i in range(NUM_ILLUMINATION_PORTS)}

        # Primary per-channel runtime state — keyed by channel name
        self._channel_state: Dict[str, ChannelState] = {}
        if channel_config is not None:
            for ch in channel_config.channels:
                self._channel_state[ch.name] = ChannelState()

        # Named preset storage
        self.presets: Dict[str, IlluminationPreset] = {}

        if self.light_source_type is not None:
            self._configure_light_source()

        if self.light_source_type is None and not self.disable_intensity_calibration:
            self._load_intensity_calibrations()

    # -----------------------------------------------------------------------
    # Endpoint map construction
    # -----------------------------------------------------------------------

    def _build_channel_endpoint_map(self) -> None:
        """Build per-channel endpoint map from IORegistry + channel_config.

        For each channel in ``channel_config``, looks up
        ``illumination.{ch.name}.intensity`` and ``illumination.{ch.name}.shutter``
        in the IORegistry.  Channels without registry entries are omitted and fall
        back to the legacy MCU path.
        """
        if self.io_registry is None or self.channel_config is None:
            return
        for ch in self.channel_config.channels:
            intensity_ep = self.io_registry.get(f"illumination.{ch.name}.intensity")
            shutter_ep = self.io_registry.get(f"illumination.{ch.name}.shutter")
            if intensity_ep is not None or shutter_ep is not None:
                self._channel_endpoints[ch.name] = (intensity_ep, shutter_ep)

    def _build_wavelength_endpoint_map(self) -> None:
        """Fallback: map wavelengths to IO endpoint prefixes when channel_config is absent.

        Strategy 1: Scan IORegistry endpoint names for wavelength substrings.
        Strategy 2: Legacy ``illum_D{n}`` naming from illumination_channel_config.yaml.
        Strategy 3: Hardcoded default D-port map.
        """
        if self.io_registry is not None:
            for ep_name in self.io_registry.list_endpoint_names():
                if ".intensity" not in ep_name and ".shutter" not in ep_name:
                    continue
                parts = ep_name.rsplit(".", 1)
                if len(parts) != 2:
                    continue
                prefix = parts[0]
                mid_parts = prefix.split(".")
                if len(mid_parts) >= 2:
                    ch_name = mid_parts[-1]
                    wl = _extract_wavelength(ch_name)
                    if wl is not None:
                        self._wavelength_to_endpoint_prefix[wl] = prefix
            if self._wavelength_to_endpoint_prefix:
                return

        from control._def import source_code_to_port_index
        default_port_map = {
            405: "D1", 470: "D2", 488: "D2", 545: "D3", 550: "D3",
            555: "D3", 561: "D3", 638: "D4", 640: "D4", 730: "D5",
            735: "D5", 750: "D5",
        }
        try:
            config_repo = ConfigRepository()
            illum_cfg = config_repo.get_illumination_config()
            if illum_cfg is not None:
                for ch in illum_cfg.channels:
                    if ch.wavelength_nm is not None:
                        source_code = illum_cfg.get_source_code(ch)
                        port_idx = source_code_to_port_index(source_code)
                        if port_idx >= 0:
                            self._wavelength_to_endpoint_prefix[ch.wavelength_nm] = f"illum_D{port_idx + 1}"
        except Exception:
            pass

        if not self._wavelength_to_endpoint_prefix:
            for wl, port_name in default_port_map.items():
                self._wavelength_to_endpoint_prefix[wl] = f"illum_{port_name}"

    def _get_io_endpoints_legacy(self, wavelength: int):
        """Return (intensity_ep, shutter_ep) for a wavelength via the legacy prefix map."""
        prefix = self._wavelength_to_endpoint_prefix.get(wavelength)
        if prefix is None:
            raise KeyError(f"No IO endpoint mapping for wavelength {wavelength}nm")
        if "." in prefix:
            intensity_ep = self.io_registry.get(f"{prefix}.intensity")
            shutter_ep = self.io_registry.get(f"{prefix}.shutter")
        else:
            intensity_ep = self.io_registry.get(f"{prefix}_intensity")
            shutter_ep = self.io_registry.get(f"{prefix}_shutter")
        return intensity_ep, shutter_ep

    # -----------------------------------------------------------------------
    # Channel name / wavelength helpers
    # -----------------------------------------------------------------------

    def _wavelength_to_channel_name(self, wavelength: int) -> Optional[str]:
        """Resolve a wavelength integer to a channel name via channel_config."""
        if self.channel_config is None:
            return None
        for ch in self.channel_config.channels:
            if ch.wavelength_nm == wavelength:
                return ch.name
        return None

    def _name_to_wavelength(self, channel_name: str) -> Optional[int]:
        """Look up wavelength for a channel name via channel_config or name pattern."""
        if self.channel_config is not None:
            ch = self.channel_config.get_channel_by_name(channel_name)
            if ch is not None and ch.wavelength_nm is not None:
                return ch.wavelength_nm
        return _extract_wavelength(channel_name)

    # -----------------------------------------------------------------------
    # Primary channel-name-based API
    # -----------------------------------------------------------------------

    def set_channel_intensity(self, channel_name: str, intensity: float) -> None:
        """Set intensity for a named channel, routing to the correct physical controller.

        Routing priority per channel:
        1. Per-channel IORegistry endpoint declared in the machine config ``io:`` block
        2. Legacy global mode (Software via light_source, or MCU DAC)

        Args:
            channel_name: Name matching ``IlluminationChannel.name`` in channel_config
            intensity: Intensity percentage 0–100
        """
        intensity = float(np.clip(intensity, 0, 100))

        # Update name-keyed state
        if channel_name not in self._channel_state:
            self._channel_state[channel_name] = ChannelState()
        self._channel_state[channel_name].intensity = intensity

        # Keep legacy wavelength-keyed state in sync
        wl = self._name_to_wavelength(channel_name)
        if wl is not None:
            self.intensity_settings[wl] = intensity

        intensity_ep, _ = self._channel_endpoints.get(channel_name, (None, None))
        if intensity_ep is not None:
            effective = self._apply_lut_by_name(channel_name, intensity)
            intensity_ep.set_analog(effective)
            return

        self._set_intensity_legacy(channel_name, intensity)

    def turn_on_channel(self, channel_name: str) -> None:
        """Turn on illumination for a named channel.

        Args:
            channel_name: Name matching ``IlluminationChannel.name`` in channel_config
        """
        if channel_name not in self._channel_state:
            self._channel_state[channel_name] = ChannelState()
        self._channel_state[channel_name].is_on = True

        wl = self._name_to_wavelength(channel_name)
        if wl is not None:
            self.is_on[wl] = True

        _, shutter_ep = self._channel_endpoints.get(channel_name, (None, None))
        if shutter_ep is not None:
            shutter_ep.set_digital(True)
            shutter_ep.wait()
            return

        self._turn_on_legacy(channel_name)

    def turn_off_channel(self, channel_name: str) -> None:
        """Turn off illumination for a named channel.

        Args:
            channel_name: Name matching ``IlluminationChannel.name`` in channel_config
        """
        if channel_name not in self._channel_state:
            self._channel_state[channel_name] = ChannelState()
        self._channel_state[channel_name].is_on = False

        wl = self._name_to_wavelength(channel_name)
        if wl is not None:
            self.is_on[wl] = False

        _, shutter_ep = self._channel_endpoints.get(channel_name, (None, None))
        if shutter_ep is not None:
            shutter_ep.set_digital(False)
            shutter_ep.wait()
            return

        self._turn_off_legacy(channel_name)

    def turn_off_all(self) -> None:
        """Turn off all illumination channels.

        If ``channel_config`` is available, iterates channels by name via
        ``turn_off_channel``.  Falls back to the multi-port MCU command or legacy
        per-wavelength calls when no config is present.
        """
        if self.channel_config is not None:
            for ch in self.channel_config.channels:
                try:
                    self.turn_off_channel(ch.name)
                except Exception as e:
                    logger.warning(f"Failed to turn off channel '{ch.name}': {e}")
            return

        # Fallback: MCU multi-port or legacy per-wavelength
        try:
            if self.microcontroller is not None and self.microcontroller.supports_multi_port():
                self.turn_off_all_ports()
                return
        except Exception:
            pass

        for ch in list(self.is_on.keys()):
            try:
                self.turn_off_illumination(ch)
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # Legacy fallback helpers (used when no per-channel endpoint is available)
    # -----------------------------------------------------------------------

    def _set_intensity_legacy(self, channel_name: str, intensity: float) -> None:
        """Route set_intensity through the legacy global modes."""
        wl = self._name_to_wavelength(channel_name)

        if self.intensity_control_mode == IntensityControlMode.Software:
            ch_key = self.channel_mappings_software.get(channel_name) or (
                self.channel_mappings_software.get(wl) if wl is not None else None
            )
            if ch_key is not None and intensity != self.intensity_settings.get(wl, -1):
                self.light_source.set_intensity(ch_key, intensity)
            if self.shutter_control_mode == ShutterControlMode.TTL:
                if wl is not None and wl in self.channel_mappings_TTL:
                    self.microcontroller.set_illumination(self.channel_mappings_TTL[wl], intensity)
            elif self.shutter_control_mode == ShutterControlMode.IOEndpoint:
                try:
                    if wl is not None:
                        intensity_ep, _ = self._get_io_endpoints_legacy(wl)
                        if intensity_ep is not None:
                            intensity_ep.set_analog(intensity)
                except (KeyError, TypeError):
                    pass
            return

        if self.intensity_control_mode == IntensityControlMode.IOEndpoint:
            try:
                if wl is not None:
                    effective = self._apply_lut(wl, intensity) if wl in self.intensity_luts else intensity
                    intensity_ep, _ = self._get_io_endpoints_legacy(wl)
                    if intensity_ep is not None:
                        intensity_ep.set_analog(effective)
            except (KeyError, TypeError):
                pass
            return

        # SquidControllerDAC fallback
        if wl is not None and wl in self.channel_mappings_TTL:
            dac = self._apply_lut(wl, intensity) if wl in self.intensity_luts else intensity
            self.microcontroller.set_illumination(self.channel_mappings_TTL[wl], dac)

    def _turn_on_legacy(self, channel_name: str) -> None:
        """Route turn_on through the legacy global modes."""
        wl = self._name_to_wavelength(channel_name)

        if self.shutter_control_mode == ShutterControlMode.Software:
            ch_key = self.channel_mappings_software.get(channel_name) or (
                self.channel_mappings_software.get(wl) if wl is not None else None
            )
            if ch_key is not None:
                self.light_source.set_shutter_state(ch_key, on=True)
            return

        if self.shutter_control_mode == ShutterControlMode.IOEndpoint:
            try:
                if wl is not None:
                    _, shutter_ep = self._get_io_endpoints_legacy(wl)
                    if shutter_ep is not None:
                        shutter_ep.set_digital(True)
                        shutter_ep.wait()
            except (KeyError, TypeError):
                pass
            return

        # TTL
        self.microcontroller.turn_on_illumination()
        self.microcontroller.wait_till_operation_is_completed()

    def _turn_off_legacy(self, channel_name: str) -> None:
        """Route turn_off through the legacy global modes."""
        wl = self._name_to_wavelength(channel_name)

        if self.shutter_control_mode == ShutterControlMode.Software:
            ch_key = self.channel_mappings_software.get(channel_name) or (
                self.channel_mappings_software.get(wl) if wl is not None else None
            )
            if ch_key is not None:
                self.light_source.set_shutter_state(ch_key, on=False)
            return

        if self.shutter_control_mode == ShutterControlMode.IOEndpoint:
            try:
                if wl is not None:
                    _, shutter_ep = self._get_io_endpoints_legacy(wl)
                    if shutter_ep is not None:
                        shutter_ep.set_digital(False)
                        shutter_ep.wait()
            except (KeyError, TypeError):
                pass
            return

        # TTL
        self.microcontroller.turn_off_illumination()
        self.microcontroller.wait_till_operation_is_completed()

    # -----------------------------------------------------------------------
    # Backward-compatible wavelength API (shims)
    # -----------------------------------------------------------------------

    def turn_on_illumination(self, channel=None) -> None:
        """Turn on illumination.  Delegates to turn_on_channel() when a per-channel
        endpoint is available; otherwise falls back to the legacy global-mode path.

        Args:
            channel: Wavelength channel (e.g. 488). If None, uses current_channel.
        """
        if channel is None:
            channel = self.current_channel

        name = self._wavelength_to_channel_name(channel) if channel is not None else None
        if name is not None and name in self._channel_endpoints:
            self.turn_on_channel(name)
            return

        # Legacy path
        if self.shutter_control_mode == ShutterControlMode.Software:
            self.light_source.set_shutter_state(self.channel_mappings_software[channel], on=True)
        elif self.shutter_control_mode == ShutterControlMode.IOEndpoint:
            try:
                _, shutter_ep = self._get_io_endpoints_legacy(channel)
                if shutter_ep is not None:
                    shutter_ep.set_digital(True)
                    shutter_ep.wait()
            except (KeyError, TypeError):
                pass
        else:
            self.microcontroller.turn_on_illumination()
            self.microcontroller.wait_till_operation_is_completed()

        if channel is not None:
            self.is_on[channel] = True
            if name is not None and name in self._channel_state:
                self._channel_state[name].is_on = True

    def turn_off_illumination(self, channel=None) -> None:
        """Turn off illumination.  Delegates to turn_off_channel() when a per-channel
        endpoint is available; otherwise falls back to the legacy global-mode path.

        Args:
            channel: Wavelength channel. If None, uses current_channel.
        """
        if channel is None:
            channel = self.current_channel

        name = self._wavelength_to_channel_name(channel) if channel is not None else None
        if name is not None and name in self._channel_endpoints:
            self.turn_off_channel(name)
            return

        # Legacy path
        if self.shutter_control_mode == ShutterControlMode.Software:
            self.light_source.set_shutter_state(self.channel_mappings_software[channel], on=False)
        elif self.shutter_control_mode == ShutterControlMode.IOEndpoint:
            try:
                _, shutter_ep = self._get_io_endpoints_legacy(channel)
                if shutter_ep is not None:
                    shutter_ep.set_digital(False)
                    shutter_ep.wait()
            except (KeyError, TypeError):
                pass
        else:
            self.microcontroller.turn_off_illumination()
            self.microcontroller.wait_till_operation_is_completed()

        if channel is not None:
            self.is_on[channel] = False
            if name is not None and name in self._channel_state:
                self._channel_state[name].is_on = False

    def set_intensity(self, channel, intensity) -> None:
        """Set illumination intensity.  Delegates to set_channel_intensity() when a
        per-channel endpoint is available; otherwise falls back to the legacy path.

        Args:
            channel: Wavelength channel (e.g. 405, 488, 561)
            intensity: Intensity percentage 0–100
        """
        if channel not in self.intensity_settings:
            self.intensity_settings[channel] = -1

        name = self._wavelength_to_channel_name(channel)
        if name is not None and name in self._channel_endpoints:
            self.set_channel_intensity(name, intensity)
            return

        # Legacy path (unchanged behaviour)
        if self.intensity_control_mode == IntensityControlMode.Software:
            if intensity != self.intensity_settings[channel]:
                self.light_source.set_intensity(self.channel_mappings_software[channel], intensity)
                self.intensity_settings[channel] = intensity
            if self.shutter_control_mode == ShutterControlMode.TTL:
                self.microcontroller.set_illumination(self.channel_mappings_TTL[channel], intensity)
            elif self.shutter_control_mode == ShutterControlMode.IOEndpoint:
                try:
                    intensity_ep, _ = self._get_io_endpoints_legacy(channel)
                    if intensity_ep is not None:
                        intensity_ep.set_analog(intensity)
                except (KeyError, TypeError):
                    pass

        elif self.intensity_control_mode == IntensityControlMode.IOEndpoint:
            effective = self._apply_lut(channel, intensity) if channel in self.intensity_luts else intensity
            try:
                intensity_ep, _ = self._get_io_endpoints_legacy(channel)
                if intensity_ep is not None:
                    intensity_ep.set_analog(effective)
            except (KeyError, TypeError):
                pass
            self.intensity_settings[channel] = intensity

        else:
            # SquidControllerDAC
            if channel in self.intensity_luts:
                dac_percent = self._apply_lut(channel, intensity)
                self.microcontroller.set_illumination(self.channel_mappings_TTL[channel], dac_percent)
            else:
                self.microcontroller.set_illumination(self.channel_mappings_TTL[channel], intensity)
            self.intensity_settings[channel] = intensity

        # Sync _channel_state
        if name is not None and name in self._channel_state:
            self._channel_state[name].intensity = intensity

    # -----------------------------------------------------------------------
    # LUT helpers
    # -----------------------------------------------------------------------

    def _apply_lut_by_name(self, channel_name: str, intensity_percent: float) -> float:
        """Apply calibration LUT for a named channel."""
        wl = self._name_to_wavelength(channel_name)
        if wl is not None and wl in self.intensity_luts:
            return float(self._apply_lut(wl, intensity_percent))
        return float(np.clip(intensity_percent, 0, 100))

    def _apply_lut(self, channel, intensity_percent) -> float:
        """Convert desired optical power percentage to DAC percentage using calibration LUT."""
        lut = self.intensity_luts[channel]
        intensity_percent = np.clip(intensity_percent, 0, 100)
        dac_percent = np.interp(intensity_percent, lut["power_percent"], lut["dac_percent"])
        return float(np.clip(dac_percent, 0, 100))

    # -----------------------------------------------------------------------
    # Snapshot / restore / preset
    # -----------------------------------------------------------------------

    def snapshot(self) -> IlluminationSnapshot:
        """Capture current illumination state for all configured channels.

        Returns a deep copy of the current ``_channel_state``, keyed by channel name.
        Falls back to capturing wavelength-keyed state when ``channel_config`` is absent.
        """
        if self.channel_config is not None:
            states = {}
            for ch in self.channel_config.channels:
                s = self._channel_state.get(ch.name, ChannelState())
                states[ch.name] = ChannelState(s.intensity, s.is_on)
            return IlluminationSnapshot(states)

        # Fallback when no channel_config
        states = {}
        for wl, intensity in self.intensity_settings.items():
            states[str(wl)] = ChannelState(
                intensity=intensity,
                is_on=self.is_on.get(wl, False),
            )
        return IlluminationSnapshot(states)

    def restore(self, snapshot: IlluminationSnapshot) -> None:
        """Restore illumination state from a snapshot.

        Applies intensity and on/off state for each channel in the snapshot via the
        primary name-based API.
        """
        for name, state in snapshot.channel_states.items():
            try:
                self.set_channel_intensity(name, state.intensity)
                if state.is_on:
                    self.turn_on_channel(name)
                else:
                    self.turn_off_channel(name)
            except Exception as e:
                logger.warning(f"Failed to restore channel '{name}': {e}")

    def save_preset(self, name: str) -> IlluminationPreset:
        """Save the current illumination state as a named preset.

        Args:
            name: Preset name
        Returns:
            The saved IlluminationPreset
        """
        preset = IlluminationPreset(name=name, snapshot=self.snapshot())
        self.presets[name] = preset
        return preset

    def load_preset(self, name: str) -> None:
        """Load and apply a named preset.

        Args:
            name: Preset name (must exist in self.presets)
        Raises:
            KeyError: if preset name is not found
        """
        preset = self.presets.get(name)
        if preset is None:
            raise KeyError(f"Illumination preset '{name}' not found")
        self.restore(preset.snapshot)

    def delete_preset(self, name: str) -> None:
        """Delete a named preset."""
        self.presets.pop(name, None)

    def list_presets(self) -> List[str]:
        """Return sorted list of preset names."""
        return sorted(self.presets.keys())

    def save_presets_to_file(self, path: str) -> None:
        """Persist all presets to a YAML file.

        Args:
            path: Path to the YAML file to write
        """
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
        """Load presets from a YAML file, merging with any existing presets.

        Args:
            path: Path to the YAML file to read
        """
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
        except Exception as e:
            logger.warning(f"Failed to load presets from '{path}': {e}")

    # -----------------------------------------------------------------------
    # Light source helpers
    # -----------------------------------------------------------------------

    def get_intensity(self, channel):
        if self.intensity_control_mode == IntensityControlMode.Software:
            intensity = self.light_source.get_intensity(self.channel_mappings_software[channel])
            self.intensity_settings[channel] = intensity
            return intensity

    def get_shutter_state(self):
        return self.is_on

    def _load_channel_mappings(self, default_mappings: Dict[int, int]) -> Dict[int, int]:
        """Load channel mappings from illumination_channel_config.yaml; fall back to defaults."""
        try:
            config_repo = ConfigRepository()
            illumination_config = config_repo.get_illumination_config()
            if illumination_config is None:
                return default_mappings
            mappings = {}
            for channel in illumination_config.channels:
                if channel.wavelength_nm is not None:
                    source_code = illumination_config.get_source_code(channel)
                    mappings[channel.wavelength_nm] = source_code
            return mappings if mappings else default_mappings
        except Exception:
            return default_mappings

    def _configure_light_source(self):
        self.light_source.initialize()
        self._set_intensity_control_mode(self.intensity_control_mode)
        self._set_shutter_control_mode(self.shutter_control_mode)
        self.channel_mappings_software = self.light_source.channel_mappings
        for ch in self.channel_mappings_software:
            self.intensity_settings[ch] = self.get_intensity(ch)
            self.is_on[ch] = self.light_source.get_shutter_state(self.channel_mappings_software[ch])

    def _set_intensity_control_mode(self, mode):
        self.light_source.set_intensity_control_mode(mode)
        self.intensity_control_mode = mode

    def _set_shutter_control_mode(self, mode):
        self.light_source.set_shutter_control_mode(mode)
        self.shutter_control_mode = mode

    def _load_intensity_calibrations(self):
        """Load intensity calibrations for all available wavelengths."""
        calibrations_dir = Path(__file__).parent.parent / "machine_configs" / "intensity_calibrations"
        if not calibrations_dir.exists():
            return
        for calibration_file in calibrations_dir.glob("*.csv"):
            try:
                wavelength = int(calibration_file.stem)
                calibration_data = pd.read_csv(calibration_file)
                if "DAC Percent" in calibration_data.columns and "Optical Power (mW)" in calibration_data.columns:
                    self.max_power[wavelength] = calibration_data["Optical Power (mW)"].max()
                    normalized_power = (
                        calibration_data["Optical Power (mW)"] / self.max_power[wavelength] * 100
                    )
                    dac_percent = np.clip(calibration_data["DAC Percent"].values, 0, 100)
                    self.intensity_luts[wavelength] = {
                        "power_percent": normalized_power.values,
                        "dac_percent": dac_percent,
                    }
            except (ValueError, KeyError) as e:
                logger.warning(f"Could not load calibration from {calibration_file}: {e}")

    # -----------------------------------------------------------------------
    # Multi-port illumination API (firmware v1.0+)
    # -----------------------------------------------------------------------

    def _check_multi_port_support(self):
        """Raise if firmware does not support multi-port commands."""
        if not self.microcontroller.supports_multi_port():
            raise RuntimeError(
                "Firmware does not support multi-port illumination commands. "
                "Update firmware to version 1.0 or later."
            )

    def set_port_intensity(self, port_index: int, intensity: float):
        """Set intensity for a specific port without changing on/off state."""
        self._check_multi_port_support()
        if port_index < 0 or port_index >= NUM_ILLUMINATION_PORTS:
            raise ValueError(f"Invalid port index: {port_index}")
        self.microcontroller.set_port_intensity(port_index, intensity)
        self.microcontroller.wait_till_operation_is_completed()
        self.port_intensity[port_index] = intensity

    def turn_on_port(self, port_index: int):
        """Turn on a specific illumination port."""
        self._check_multi_port_support()
        if port_index < 0 or port_index >= NUM_ILLUMINATION_PORTS:
            raise ValueError(f"Invalid port index: {port_index}")
        self.microcontroller.turn_on_port(port_index)
        self.microcontroller.wait_till_operation_is_completed()
        self.port_is_on[port_index] = True

    def turn_off_port(self, port_index: int):
        """Turn off a specific illumination port."""
        self._check_multi_port_support()
        if port_index < 0 or port_index >= NUM_ILLUMINATION_PORTS:
            raise ValueError(f"Invalid port index: {port_index}")
        self.microcontroller.turn_off_port(port_index)
        self.microcontroller.wait_till_operation_is_completed()
        self.port_is_on[port_index] = False

    def set_port_illumination(self, port_index: int, intensity: float, turn_on: bool):
        """Set intensity and on/off state for a specific port in one command."""
        self._check_multi_port_support()
        if port_index < 0 or port_index >= NUM_ILLUMINATION_PORTS:
            raise ValueError(f"Invalid port index: {port_index}")
        self.microcontroller.set_port_illumination(port_index, intensity, turn_on)
        self.microcontroller.wait_till_operation_is_completed()
        self.port_intensity[port_index] = intensity
        self.port_is_on[port_index] = turn_on

    def turn_on_multiple_ports(self, port_indices: List[int]):
        """Turn on multiple ports simultaneously."""
        if not port_indices:
            return
        self._check_multi_port_support()
        port_mask = 0
        on_mask = 0
        for port_index in port_indices:
            if port_index < 0 or port_index >= NUM_ILLUMINATION_PORTS:
                raise ValueError(f"Invalid port index: {port_index}")
            port_mask |= 1 << port_index
            on_mask |= 1 << port_index
        self.microcontroller.set_multi_port_mask(port_mask, on_mask)
        self.microcontroller.wait_till_operation_is_completed()
        for port_index in port_indices:
            self.port_is_on[port_index] = True

    def turn_off_all_ports(self):
        """Turn off all illumination ports."""
        self._check_multi_port_support()
        self.microcontroller.turn_off_all_ports()
        self.microcontroller.wait_till_operation_is_completed()
        for i in range(NUM_ILLUMINATION_PORTS):
            self.port_is_on[i] = False

    def get_active_ports(self) -> List[int]:
        """Get list of currently active (on) port indices."""
        return [i for i in range(NUM_ILLUMINATION_PORTS) if self.port_is_on[i]]

    def close(self):
        if self.light_source is not None:
            self.light_source.shut_down()
