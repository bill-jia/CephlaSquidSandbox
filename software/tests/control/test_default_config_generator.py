"""
Unit tests for default_config_generator.py.

Tests default configuration generation functions.
"""

import pytest

from control.default_config_generator import (
    DEFAULT_EXPOSURE_TIME_MS,
    DEFAULT_GAIN_MODE,
    DEFAULT_ILLUMINATION_INTENSITY,
    DEFAULT_LED_ILLUMINATION_INTENSITY,
    build_confocal_settings,
    generate_default_observation_state,
    get_display_color_for_channel,
)
from control.models import (
    IlluminationChannel,
    IlluminationChannelConfig,
)
from control.models.machine_config import ConfocalDeviceSettings, DeviceEntry
from control.models.illumination_config import (
    DEFAULT_LED_COLOR,
    DEFAULT_WAVELENGTH_COLORS,
    IlluminationType,
)


class TestDefaultConfigGenerator:
    """Tests for default_config_generator.py functions."""

    def test_get_display_color_for_fluorescence(self):
        channel = IlluminationChannel(
            name="Fluorescence 488nm",
            type=IlluminationType.EPI_ILLUMINATION,
            wavelength_nm=488,
            controller_port="D1",
            source_code=11,
        )
        color = get_display_color_for_channel(channel)
        assert color == DEFAULT_WAVELENGTH_COLORS[488]

    def test_get_display_color_for_led(self):
        channel = IlluminationChannel(
            name="BF LED matrix",
            type=IlluminationType.TRANSILLUMINATION,
            wavelength_nm=None,
            controller_port="USB1",
            source_code=0,
        )
        color = get_display_color_for_channel(channel)
        assert color == DEFAULT_LED_COLOR

    def test_generate_default_observation_state(self):
        illumination_config = IlluminationChannelConfig(
            version=1,
            channels=[
                IlluminationChannel(
                    name="Channel A",
                    type=IlluminationType.EPI_ILLUMINATION,
                    wavelength_nm=488,
                    controller_port="D1",
                    source_code=11,
                ),
                IlluminationChannel(
                    name="Channel B",
                    type=IlluminationType.TRANSILLUMINATION,
                    controller_port="USB1",
                    source_code=0,
                ),
            ],
        )
        state = generate_default_observation_state(illumination_config)

        assert state.camera_settings.exposure_time_ms == DEFAULT_EXPOSURE_TIME_MS
        assert state.camera_settings.gain_mode == DEFAULT_GAIN_MODE
        assert len(state.illuminator_states) == 2
        assert state.illuminator_states[0].on is True
        assert state.illuminator_states[0].intensity == DEFAULT_ILLUMINATION_INTENSITY
        assert state.illuminator_states[1].on is False
        assert state.illuminator_states[1].intensity == DEFAULT_LED_ILLUMINATION_INTENSITY
        # No confocal device: no confocal block at all
        assert state.confocal_hardware_settings is None

    def test_generate_default_observation_state_with_confocal(self):
        """Iris defaults come from the confocal device entry, not a hard-coded 100."""
        illumination_config = IlluminationChannelConfig(
            version=1,
            channels=[
                IlluminationChannel(
                    name="Channel A",
                    type=IlluminationType.EPI_ILLUMINATION,
                    wavelength_nm=488,
                    controller_port="D1",
                    source_code=11,
                ),
            ],
        )
        state = generate_default_observation_state(
            illumination_config,
            confocal_settings=ConfocalDeviceSettings(
                illumination_iris_default=80,
                emission_iris_default=60,
            ),
        )

        assert state.confocal_hardware_settings is not None
        assert state.confocal_hardware_settings.illumination_iris == 80.0
        assert state.confocal_hardware_settings.emission_iris == 60.0


class TestBuildConfocalSettings:
    """Tests for build_confocal_settings()."""

    def test_no_settings_returns_model_defaults(self):
        settings = build_confocal_settings(None)
        assert settings.illumination_iris == 100.0
        assert settings.emission_iris == 100.0

    def test_device_defaults_are_used(self):
        device_settings = ConfocalDeviceSettings(
            illumination_iris_default=80,
            emission_iris_default=60,
        )
        settings = build_confocal_settings(device_settings)
        assert settings.illumination_iris == 80.0
        assert settings.emission_iris == 60.0

    def test_settings_from_device_entry(self):
        entry = DeviceEntry(
            driver="xlight",
            config={"illumination_iris_default": 80, "emission_iris_default": 45},
        )
        settings = build_confocal_settings(ConfocalDeviceSettings.from_device_entry(entry))
        assert settings.illumination_iris == 80.0
        assert settings.emission_iris == 45.0
