"""
Illumination control system for the microscope.

This module provides a unified interface for controlling various illumination sources
(LEDs, lasers) with support for:
- Intensity control via DAC (Digital-to-Analog Converter) or software control
- Shutter control via TTL signals or software commands
- Intensity calibration using lookup tables (LUTs) for power-linear control
- Multiple light source types (Squid LEDs, lasers, LDI, CELESTA, etc.)

The controller maps wavelength channels (405nm, 488nm, 561nm, etc.) to hardware
channels and applies calibration curves to convert desired optical power percentages
to DAC values for accurate intensity control.
"""

from enum import Enum
import json
import os
import numpy as np
import pandas as pd
from pathlib import Path

from control.microcontroller import Microcontroller


class LightSourceType(Enum):
    """Enumeration of supported light source types."""
    SquidLED = 0      # Built-in LED array on Squid controller
    SquidLaser = 1    # Built-in laser on Squid controller
    LDI = 2           # Lumencor Light Engine
    CELESTA = 3       # Lumencor CELESTA light engine
    VersaLase = 4    # VersaLase laser system
    SCI = 5           # SciMicroscopy LED array
    AndorLaser = 6    # Andor laser system


class IntensityControlMode(Enum):
    """Methods for controlling illumination intensity."""
    SquidControllerDAC = 0  # Control via DAC on microcontroller (analog voltage)
    Software = 1            # Control via software API of light source


class ShutterControlMode(Enum):
    """Methods for controlling illumination shutters."""
    TTL = 0       # Control via TTL signals from microcontroller
    Software = 1  # Control via software API of light source


class IlluminationController:
    def __init__(
        self,
        microcontroller: Microcontroller,
        intensity_control_mode=IntensityControlMode.SquidControllerDAC,
        shutter_control_mode=ShutterControlMode.TTL,
        light_source_type=None,
        light_source=None,
        disable_intensity_calibration=False,
    ):
        """
        Initialize the illumination controller.
        
        Args:
            microcontroller: Microcontroller instance for hardware communication
            intensity_control_mode: How to control intensity (DAC or software)
            shutter_control_mode: How to control shutters (TTL or software)
            light_source_type: Type of light source if using external device
            light_source: Light source object if using external device
            disable_intensity_calibration: If True, control LED/laser current directly
                                          without applying calibration LUTs
        """
        self.microcontroller = microcontroller
        self.intensity_control_mode = intensity_control_mode
        self.shutter_control_mode = shutter_control_mode
        self.light_source_type = light_source_type
        self.light_source = light_source
        self.disable_intensity_calibration = disable_intensity_calibration
        
        # Default channel mappings: wavelength (nm) -> TTL channel number
        # These map fluorescence excitation wavelengths to hardware control channels
        default_mappings = {
            405: 11,  # UV/violet LED
            470: 12,  # Blue LED
            488: 12,  # Blue laser (same channel as 470nm)
            545: 14,  # Green LED
            550: 14,  # Green LED
            555: 14,  # Green LED
            561: 14,  # Green laser (same channel as 555nm)
            638: 13,  # Red LED
            640: 13,  # Red LED
            730: 15,  # Far-red LED
            735: 15,  # Far-red LED
            750: 15,  # Far-red LED
        }

        # Try to load custom channel mappings from file, fallback to defaults
        self.channel_mappings_TTL = self._load_channel_mappings(default_mappings)

        # State tracking
        self.channel_mappings_software = {}  # Software-controlled channel mappings
        self.is_on = {}                      # Track which channels are currently on
        self.intensity_settings = {}         # Current intensity for each channel (0-100%)
        self.current_channel = None          # Currently selected channel
        self.intensity_luts = {}            # Lookup tables for each wavelength (power -> DAC)
        self.max_power = {}                  # Maximum measured power for each wavelength

        # Configure external light source if provided
        if self.light_source_type is not None:
            self._configure_light_source()

        # Load intensity calibration files if using DAC control
        # Calibration files map DAC percentage to actual optical power (mW)
        # This allows power-linear control instead of voltage-linear control
        if self.light_source_type is None and self.disable_intensity_calibration is False:
            self._load_intensity_calibrations()

    def _load_channel_mappings(self, default_mappings):
        """
        Load channel mappings from JSON file, fallback to default if file not found.
        
        Channel mappings define which hardware TTL channel controls each wavelength.
        This allows customization of the illumination setup without code changes.
        """
        try:
            # Get the parent directory of the current file
            current_dir = Path(__file__).parent.parent
            mapping_file = current_dir / "channel_mappings.json"

            if mapping_file.exists():
                with open(mapping_file, "r") as f:
                    mappings = json.load(f)
                    # Convert string keys to integers
                    return {int(k): v for k, v in mappings["Illumination Code Map"].items()}
            return default_mappings
        except (json.JSONDecodeError, KeyError, FileNotFoundError):
            return default_mappings

    def _configure_light_source(self):
        """
        Initialize and configure an external light source (LDI, CELESTA, etc.).
        Sets up intensity and shutter control modes and reads current states.
        """
        self.light_source.initialize()
        self._set_intensity_control_mode(self.intensity_control_mode)
        self._set_shutter_control_mode(self.shutter_control_mode)
        self.channel_mappings_software = self.light_source.channel_mappings
        # Read current intensity and shutter state for each channel
        for ch in self.channel_mappings_software:
            self.intensity_settings[ch] = self.get_intensity(ch)
            self.is_on[ch] = self.light_source.get_shutter_state(self.channel_mappings_software[ch])

    def _set_intensity_control_mode(self, mode):
        self.light_source.set_intensity_control_mode(mode)
        self.intensity_control_mode = mode

    def _set_shutter_control_mode(self, mode):
        self.light_source.set_shutter_control_mode(mode)
        self.shutter_control_mode = mode

    def get_intensity(self, channel):
        if self.intensity_control_mode == IntensityControlMode.Software:
            intensity = self.light_source.get_intensity(self.channel_mappings_software[channel])
            self.intensity_settings[channel] = intensity
            return intensity  # 0 - 100

    def turn_on_illumination(self, channel=None):
        """
        Turn on illumination for the specified channel.
        
        Args:
            channel: Wavelength channel (e.g., 405, 488, 561). If None, uses current_channel.
        """
        if channel is None:
            channel = self.current_channel

        if self.shutter_control_mode == ShutterControlMode.Software:
            # Control shutter via light source API
            self.light_source.set_shutter_state(self.channel_mappings_software[channel], on=True)
        elif self.shutter_control_mode == ShutterControlMode.TTL:
            # Control shutter via TTL signal from microcontroller
            # Note: Intensity should already be set via set_intensity() before turning on
            self.microcontroller.turn_on_illumination()

        self.is_on[channel] = True

    def turn_off_illumination(self, channel=None):
        """
        Turn off illumination for the specified channel.
        
        Args:
            channel: Wavelength channel. If None, uses current_channel.
        """
        if channel is None:
            channel = self.current_channel

        if self.shutter_control_mode == ShutterControlMode.Software:
            self.light_source.set_shutter_state(self.channel_mappings_software[channel], on=False)
        elif self.shutter_control_mode == ShutterControlMode.TTL:
            # Send TTL signal to close shutter
            self.microcontroller.turn_off_illumination()

        self.is_on[channel] = False

    def _load_intensity_calibrations(self):
        """
        Load intensity calibration files for all available wavelengths.
        
        Calibration files are CSV files named by wavelength (e.g., "405.csv", "488.csv")
        containing columns:
        - "DAC Percent": DAC setting (0-100%)
        - "Optical Power (mW)": Measured optical power at that DAC setting
        
        These calibrations create lookup tables that convert desired power percentage
        to the required DAC percentage, accounting for non-linear LED/laser responses.
        """
        calibrations_dir = Path(__file__).parent.parent / "intensity_calibrations"
        if not calibrations_dir.exists():
            return

        for calibration_file in calibrations_dir.glob("*.csv"):
            try:
                # Extract wavelength from filename (e.g., "405.csv" -> 405)
                wavelength = int(calibration_file.stem)
                calibration_data = pd.read_csv(calibration_file)
                if "DAC Percent" in calibration_data.columns and "Optical Power (mW)" in calibration_data.columns:
                    # Store maximum power for normalization
                    self.max_power[wavelength] = calibration_data["Optical Power (mW)"].max()
                    # Normalize power to 0-100% range
                    normalized_power = calibration_data["Optical Power (mW)"] / self.max_power[wavelength] * 100
                    # Ensure DAC values are in valid range
                    dac_percent = np.clip(calibration_data["DAC Percent"].values, 0, 100)
                    # Create lookup table: power_percent -> dac_percent
                    self.intensity_luts[wavelength] = {
                        "power_percent": normalized_power.values,
                        "dac_percent": dac_percent,
                    }
            except (ValueError, KeyError) as e:
                print(f"Warning: Could not load calibration from {calibration_file}: {e}")

    def _apply_lut(self, channel, intensity_percent):
        """
        Convert desired optical power percentage to DAC percentage using calibration LUT.
        
        Args:
            channel: Wavelength channel
            intensity_percent: Desired power percentage (0-100%)
            
        Returns:
            DAC percentage (0-100%) needed to achieve the desired power
        """
        lut = self.intensity_luts[channel]
        # Ensure intensity is within bounds
        intensity_percent = np.clip(intensity_percent, 0, 100)
        # Interpolate to get DAC value from calibration curve
        dac_percent = np.interp(intensity_percent, lut["power_percent"], lut["dac_percent"])
        # Ensure DAC value is in range 0-100
        return np.clip(dac_percent, 0, 100)

    def set_intensity(self, channel, intensity):
        """
        Set the illumination intensity for a specific channel.
        
        Args:
            channel: Wavelength channel (e.g., 405, 488, 561)
            intensity: Intensity percentage (0-100%), where 100% = maximum calibrated power
            
        Note: For DAC control with calibration, this converts power percentage to DAC
        percentage using the calibration lookup table. This ensures linear power control
        even if the LED/laser response is non-linear with voltage.
        """
        # Initialize intensity setting for this channel if it doesn't exist
        if channel not in self.intensity_settings:
            self.intensity_settings[channel] = -1
            
        if self.intensity_control_mode == IntensityControlMode.Software:
            # Control intensity via light source API
            if intensity != self.intensity_settings[channel]:
                self.light_source.set_intensity(self.channel_mappings_software[channel], intensity)
                self.intensity_settings[channel] = intensity
            if self.shutter_control_mode == ShutterControlMode.TTL:
                # Still need to set channel on microcontroller for TTL shutter control
                # This selects which channel will be opened when turn_on_illumination() is called
                self.microcontroller.set_illumination(self.channel_mappings_TTL[channel], intensity)
        else:
            # Control intensity via DAC on microcontroller
            if channel in self.intensity_luts:
                # Apply calibration LUT to convert power percentage to DAC percentage
                # This accounts for non-linear LED/laser response
                dac_percent = self._apply_lut(channel, intensity)
                self.microcontroller.set_illumination(self.channel_mappings_TTL[channel], dac_percent)
            else:
                # No calibration available, use intensity directly as DAC percentage
                self.microcontroller.set_illumination(self.channel_mappings_TTL[channel], intensity)
            self.intensity_settings[channel] = intensity

    def get_shutter_state(self):
        return self.is_on

    def close(self):
        if self.light_source is not None:
            self.light_source.shut_down()
