"""Tests for control._def module, specifically ZMotorConfig enum and HardwareTriggerMode."""

import pytest
from control._def import ZMotorConfig, HardwareTriggerMode


class TestZMotorConfig:
    """Tests for ZMotorConfig enum."""

    def test_enum_values(self):
        """Test that enum has expected values."""
        assert ZMotorConfig.STEPPER.value == "STEPPER"
        assert ZMotorConfig.STEPPER_PIEZO.value == "STEPPER + PIEZO"
        assert ZMotorConfig.PIEZO.value == "PIEZO"

    def test_convert_to_enum_from_string(self):
        """Test conversion from string values."""
        assert ZMotorConfig.convert_to_enum("STEPPER") == ZMotorConfig.STEPPER
        assert ZMotorConfig.convert_to_enum("STEPPER + PIEZO") == ZMotorConfig.STEPPER_PIEZO
        assert ZMotorConfig.convert_to_enum("PIEZO") == ZMotorConfig.PIEZO

    def test_convert_to_enum_from_enum(self):
        """Test that convert_to_enum returns enum unchanged."""
        assert ZMotorConfig.convert_to_enum(ZMotorConfig.STEPPER) == ZMotorConfig.STEPPER
        assert ZMotorConfig.convert_to_enum(ZMotorConfig.PIEZO) == ZMotorConfig.PIEZO

    def test_convert_to_enum_invalid_value(self):
        """Test that invalid values raise ValueError."""
        with pytest.raises(ValueError, match="Invalid Z motor config"):
            ZMotorConfig.convert_to_enum("INVALID")
        with pytest.raises(ValueError, match="Invalid Z motor config"):
            ZMotorConfig.convert_to_enum("stepper")  # Case sensitive

    def test_has_piezo(self):
        """Test has_piezo() method."""
        assert ZMotorConfig.STEPPER.has_piezo() is False
        assert ZMotorConfig.STEPPER_PIEZO.has_piezo() is True
        assert ZMotorConfig.PIEZO.has_piezo() is True

    def test_is_piezo_only(self):
        """Test is_piezo_only() method."""
        assert ZMotorConfig.STEPPER.is_piezo_only() is False
        assert ZMotorConfig.STEPPER_PIEZO.is_piezo_only() is False
        assert ZMotorConfig.PIEZO.is_piezo_only() is True


class TestHardwareTriggerMode:
    """Tests for HardwareTriggerMode class."""

    def test_enum_values(self):
        """Test that enum has expected values matching firmware."""
        assert HardwareTriggerMode.EDGE == 0
        assert HardwareTriggerMode.LEVEL == 1

    def test_values_match_firmware_protocol(self):
        """Test that values can be passed directly to microcontroller."""
        # These values are sent directly to firmware via set_trigger_mode()
        # EDGE (0) = fixed pulse width (TRIGGER_PULSE_LENGTH_us)
        # LEVEL (1) = variable pulse width (illumination_on_time)
        assert isinstance(HardwareTriggerMode.EDGE, int)
        assert isinstance(HardwareTriggerMode.LEVEL, int)
        assert HardwareTriggerMode.EDGE in (0, 1)
        assert HardwareTriggerMode.LEVEL in (0, 1)
        assert HardwareTriggerMode.EDGE != HardwareTriggerMode.LEVEL


class TestParseSimSetting:
    """Tests for _parse_sim_setting() simulation setting parser.

    Note: _parse_sim_setting is defined inside a try block in _def.py at module load time,
    so we test it by recreating the logic.
    """

    @staticmethod
    def _parse_sim_setting(value_str):
        """Recreate _parse_sim_setting logic for testing (original is local to try block)."""
        val = value_str.strip().lower()
        if val in ("true", "1", "yes", "simulate"):
            return True
        # Everything else = False (real hardware)
        return False

    def test_parses_true_values(self):
        """Test that true/simulate values return True."""
        assert self._parse_sim_setting("true") is True
        assert self._parse_sim_setting("True") is True
        assert self._parse_sim_setting("TRUE") is True
        assert self._parse_sim_setting("1") is True
        assert self._parse_sim_setting("yes") is True
        assert self._parse_sim_setting("simulate") is True

    def test_parses_false_values(self):
        """Test that false/real values return False."""
        assert self._parse_sim_setting("false") is False
        assert self._parse_sim_setting("False") is False
        assert self._parse_sim_setting("FALSE") is False
        assert self._parse_sim_setting("0") is False
        assert self._parse_sim_setting("no") is False
        assert self._parse_sim_setting("real") is False

    def test_legacy_auto_values_default_to_false(self):
        """Test that legacy auto/none values default to False (real hardware)."""
        # For backwards compatibility with old configs
        assert self._parse_sim_setting("none") is False
        assert self._parse_sim_setting("auto") is False
        assert self._parse_sim_setting("") is False

    def test_strips_whitespace(self):
        """Test that leading/trailing whitespace is stripped."""
        assert self._parse_sim_setting("  true  ") is True
        assert self._parse_sim_setting("  false  ") is False

    def test_unrecognized_values_default_to_false(self):
        """Test that unrecognized values default to False (real hardware)."""
        # Typos and invalid values default to real hardware for safety
        assert self._parse_sim_setting("treu") is False
        assert self._parse_sim_setting("fasle") is False
        assert self._parse_sim_setting("simualte") is False
        assert self._parse_sim_setting("invalid") is False
