"""
CoolLED pE-400 / pE-400max serial driver.

Communicates via USB virtual COM port using ASCII commands (\\r\\n terminated).
Intensity is controlled via serial; shutter on/off can be handled either via
serial commands or via an external TTL line routed through the IO endpoint system.

Protocol reference: DOC-072 Rev 02 — pE-400 Series Essential Commands Manual.
"""

import serial
import time
from typing import Dict, Optional

from control.lighting import IntensityControlMode, ShutterControlMode
from control.serial_peripherals import SerialDevice
from squid.abc import LightSource

import squid.logging

log = squid.logging.get_logger(__name__)

COOLLED_CHANNELS = ("A", "B", "C", "D")


class CoolLEDpE400(LightSource):
    """Driver for coolLED pE-400 and pE-400max light sources.

    Intensity is always controlled via serial commands.  Shutter (on/off) is
    reported as ShutterControlMode.TTL so that the IlluminationController can
    route it through the IO endpoint system when an IORegistry is present.
    A software-serial fallback (set_shutter_state) is also available.
    """

    def __init__(self, SN: Optional[str] = None, port: Optional[str] = None):
        self.log = squid.logging.get_logger(self.__class__.__name__)

        self.serial_connection = SerialDevice(
            port=port,
            SN=SN,
            baudrate=9600,
            read_timeout=0.5,
            bytesize=serial.EIGHTBITS,
            stopbits=serial.STOPBITS_ONE,
            parity=serial.PARITY_NONE,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        self.serial_connection.open_ser()

        self.intensity_mode = IntensityControlMode.Software
        self.shutter_mode = ShutterControlMode.TTL

        # channel letter -> wavelength (populated by _discover_channels)
        self._channel_wavelengths: Dict[str, int] = {}
        # wavelength -> channel letter (for LightSource interface)
        self.channel_mappings: Dict[int, str] = {}
        # intensity cache per channel letter
        self._intensities: Dict[str, int] = {ch: 0 for ch in COOLLED_CHANNELS}
        # on/off state per channel letter
        self._shutter_states: Dict[str, bool] = {ch: False for ch in COOLLED_CHANNELS}

        self._discover_channels()
        self.log.info(
            f"CoolLED pE-400 initialised: {self._channel_wavelengths}, "
            f"port={self.serial_connection.port}"
        )

    # -- discovery -------------------------------------------------------------

    def _send(self, cmd: str, read_lines: int = 1, read_delay: float = 0.15) -> list[str]:
        """Send an ASCII command and return response lines."""
        full = cmd + "\r\n"
        self.serial_connection.serial.write(full.encode("ascii"))
        time.sleep(read_delay)
        lines = []
        for _ in range(read_lines):
            raw = self.serial_connection.serial.readline()
            if raw:
                lines.append(raw.decode("ascii", errors="replace").strip())
        # drain any extra bytes
        while self.serial_connection.serial.in_waiting:
            self.serial_connection.serial.readline()
        return lines

    def _discover_channels(self) -> None:
        """Query LAMS to learn which wavelengths are installed in slots A-D."""
        try:
            lines = self._send("LAMS", read_lines=4, read_delay=0.3)
            for line in lines:
                # Expected: LAM:A:635
                if line.startswith("LAM:") and line.count(":") >= 2:
                    parts = line.split(":")
                    ch_letter = parts[1].strip().upper()
                    wavelength = int(parts[2].strip())
                    if ch_letter in COOLLED_CHANNELS:
                        self._channel_wavelengths[ch_letter] = wavelength
                        self.channel_mappings[wavelength] = ch_letter
        except Exception as e:
            self.log.warning(f"Could not auto-discover coolLED channels: {e}")

        if not self._channel_wavelengths:
            self.log.warning("No channels discovered; falling back to A=405,B=488,C=561,D=638")
            fallback = {"A": 405, "B": 488, "C": 561, "D": 638}
            self._channel_wavelengths = fallback
            self.channel_mappings = {v: k for k, v in fallback.items()}

    # -- LightSource interface -------------------------------------------------

    def initialize(self):
        self._send("MODE=0")
        # Select all channels
        for ch in COOLLED_CHANNELS:
            self._send(f"C{ch}S", read_lines=1)

    def set_intensity_control_mode(self, mode):
        self.intensity_mode = mode

    def get_intensity_control_mode(self):
        return self.intensity_mode

    def set_shutter_control_mode(self, mode):
        self.shutter_mode = mode

    def get_shutter_control_mode(self):
        return self.shutter_mode

    def set_intensity(self, channel, intensity):
        """Set intensity for a channel.

        Args:
            channel: Channel identifier — either a wavelength int (e.g. 488)
                     or a channel letter string (e.g. "A").
            intensity: 0-100 percentage.
        """
        ch_letter = self._resolve_channel(channel)
        intensity_int = max(0, min(100, int(round(intensity))))
        cmd = f"C{ch_letter}I{intensity_int:03d}"
        self._send(cmd, read_lines=1)
        self._intensities[ch_letter] = intensity_int

    def get_intensity(self, channel) -> float:
        ch_letter = self._resolve_channel(channel)
        return float(self._intensities.get(ch_letter, 0))

    def set_shutter_state(self, channel, on):
        """Software serial shutter control (fallback when no TTL line configured)."""
        ch_letter = self._resolve_channel(channel)
        state_char = "N" if on else "F"
        self._send(f"C{ch_letter}{state_char}", read_lines=1)
        self._shutter_states[ch_letter] = bool(on)

    def get_shutter_state(self, channel):
        ch_letter = self._resolve_channel(channel)
        return self._shutter_states.get(ch_letter, False)

    def shut_down(self):
        try:
            self._send("CSF", read_lines=1)  # all channels off
        except Exception:
            pass
        try:
            self.serial_connection.close()
        except Exception:
            pass

    # -- helpers ---------------------------------------------------------------

    def _resolve_channel(self, channel) -> str:
        """Accept either wavelength int or channel letter string."""
        if isinstance(channel, str) and channel.upper() in COOLLED_CHANNELS:
            return channel.upper()
        # Treat as wavelength
        ch = self.channel_mappings.get(int(channel))
        if ch is None:
            raise KeyError(f"No coolLED channel for identifier '{channel}'")
        return ch

    def get_model(self) -> str:
        lines = self._send("XMODEL", read_lines=1)
        return lines[0] if lines else "unknown"

    def get_serial_number(self) -> str:
        lines = self._send("XSERIAL", read_lines=1)
        return lines[0] if lines else "unknown"


class CoolLEDpE400_Simulation(LightSource):
    """Simulated coolLED pE-400 for testing without hardware."""

    def __init__(self, SN: Optional[str] = None, port: Optional[str] = None):
        self.log = squid.logging.get_logger(self.__class__.__name__)
        self.intensity_mode = IntensityControlMode.Software
        self.shutter_mode = ShutterControlMode.TTL

        self._channel_wavelengths = {"A": 405, "B": 488, "C": 561, "D": 638}
        self.channel_mappings = {405: "A", 488: "B", 561: "C", 638: "D"}
        self._intensities: Dict[str, int] = {ch: 0 for ch in COOLLED_CHANNELS}
        self._shutter_states: Dict[str, bool] = {ch: False for ch in COOLLED_CHANNELS}
        self.log.info("CoolLED pE-400 simulation initialised")

    def initialize(self):
        pass

    def set_intensity_control_mode(self, mode):
        self.intensity_mode = mode

    def get_intensity_control_mode(self):
        return self.intensity_mode

    def set_shutter_control_mode(self, mode):
        self.shutter_mode = mode

    def get_shutter_control_mode(self):
        return self.shutter_mode

    def set_intensity(self, channel, intensity):
        ch = self._resolve_channel(channel)
        self._intensities[ch] = max(0, min(100, int(round(intensity))))
        self.log.debug(f"[SIM] set_intensity {ch}={self._intensities[ch]}")

    def get_intensity(self, channel) -> float:
        return float(self._intensities.get(self._resolve_channel(channel), 0))

    def set_shutter_state(self, channel, on):
        ch = self._resolve_channel(channel)
        self._shutter_states[ch] = bool(on)
        self.log.debug(f"[SIM] set_shutter_state {ch}={on}")

    def get_shutter_state(self, channel):
        return self._shutter_states.get(self._resolve_channel(channel), False)

    def shut_down(self):
        for ch in COOLLED_CHANNELS:
            self._intensities[ch] = 0
            self._shutter_states[ch] = False

    def _resolve_channel(self, channel) -> str:
        if isinstance(channel, str) and channel.upper() in COOLLED_CHANNELS:
            return channel.upper()
        ch = self.channel_mappings.get(int(channel))
        if ch is None:
            raise KeyError(f"No coolLED channel for identifier '{channel}'")
        return ch
