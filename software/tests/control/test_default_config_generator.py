"""
Unit tests for default_config_generator.py.

Tests default configuration generation functions.
"""

import pytest

from control.default_config_generator import (
    ALL_IRIS_DEFAULTS,
    DEFAULT_EXPOSURE_TIME_MS,
    DEFAULT_GAIN_MODE,
    DEFAULT_ILLUMINATION_INTENSITY,
    build_confocal_settings_from_config,
    create_general_observation_state,
    create_objective_override,
    generate_default_configs,
    generate_general_config,
    get_display_color_for_channel,
)
from control.models import (
    IlluminationChannel,
    IlluminationChannelConfig,
)
from control.models.confocal_config import ConfocalConfig
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

    def test_create_general_observation_state(self):
        ill_channel = IlluminationChannel(
            name="Fluorescence 488nm",
            type=IlluminationType.EPI_ILLUMINATION,
            wavelength_nm=488,
            controller_port="D1",
            source_code=11,
        )
        state = create_general_observation_state(ill_channel, include_confocal=False)
        assert state.name == "Fluorescence 488nm"
        assert state.camera_settings.exposure_time_ms == DEFAULT_EXPOSURE_TIME_MS
        assert state.camera_settings.gain_mode == DEFAULT_GAIN_MODE
        assert len(state.illuminator_states) == 1
        assert state.illuminator_states[0].intensity == DEFAULT_ILLUMINATION_INTENSITY

    def test_create_objective_override_with_confocal(self):
        ill_channel = IlluminationChannel(
            name="Fluorescence 488nm",
            type=IlluminationType.EPI_ILLUMINATION,
            wavelength_nm=488,
            controller_port="D1",
            source_code=11,
        )
        override = create_objective_override(ill_channel, include_confocal=True)
        assert override.confocal_hardware_settings is not None
        assert override.confocal_hardware_settings.illumination_iris == ALL_IRIS_DEFAULTS["illumination_iris"]
        assert override.confocal_hardware_settings.emission_iris == ALL_IRIS_DEFAULTS["emission_iris"]

    def test_generate_general_config(self):
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
        general_config = generate_general_config(illumination_config)
        assert len(general_config.observation_states) == 2

    def test_generate_default_configs(self):
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
        general, objectives = generate_default_configs(
            illumination_config,
            objectives=["10x", "20x"],
        )
        assert len(general.observation_states) == 1
        assert "10x" in objectives
        assert "20x" in objectives

    def test_generate_default_configs_with_confocal(self):
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
        general, objectives = generate_default_configs(
            illumination_config,
            include_confocal=True,
            objectives=["20x"],
        )
        assert general.observation_states[0].confocal_hardware_settings is None
        assert objectives["20x"].overrides[0].confocal_hardware_settings is not None


class TestBuildConfocalSettingsFromConfig:
    """Tests for build_confocal_settings_from_config()."""

    def test_no_config_returns_all_iris_defaults(self):
        settings = build_confocal_settings_from_config(None)
        assert settings.illumination_iris == ALL_IRIS_DEFAULTS["illumination_iris"]
        assert settings.emission_iris == ALL_IRIS_DEFAULTS["emission_iris"]

    def test_model_xlight_v3_returns_both_iris(self):
        config = ConfocalConfig(model="xlight_v3")
        settings = build_confocal_settings_from_config(config)
        assert settings.illumination_iris == 100.0
        assert settings.emission_iris == 100.0

    def test_model_cicero_returns_empty_settings(self):
        config = ConfocalConfig(model="cicero")
        settings = build_confocal_settings_from_config(config)
        assert settings.illumination_iris is None
        assert settings.emission_iris is None

    def test_model_xlight_v2_returns_empty_settings(self):
        config = ConfocalConfig(model="xlight_v2")
        settings = build_confocal_settings_from_config(config)
        assert settings.illumination_iris is None
        assert settings.emission_iris is None

    def test_unknown_model_falls_back_to_string_list(self):
        config = ConfocalConfig(
            model="unknown_model",
            objective_specific_properties=["illumination_iris"],
        )
        settings = build_confocal_settings_from_config(config)
        assert settings.illumination_iris == ALL_IRIS_DEFAULTS["illumination_iris"]
        assert settings.emission_iris is None

    def test_config_with_both_iris_properties(self):
        config = ConfocalConfig(
            objective_specific_properties=["illumination_iris", "emission_iris"],
        )
        settings = build_confocal_settings_from_config(config)
        assert settings.illumination_iris == ALL_IRIS_DEFAULTS["illumination_iris"]
        assert settings.emission_iris == ALL_IRIS_DEFAULTS["emission_iris"]

    def test_config_with_only_illumination_iris(self):
        config = ConfocalConfig(
            objective_specific_properties=["illumination_iris"],
        )
        settings = build_confocal_settings_from_config(config)
        assert settings.illumination_iris == ALL_IRIS_DEFAULTS["illumination_iris"]
        assert settings.emission_iris is None

    def test_config_with_only_emission_iris(self):
        config = ConfocalConfig(
            objective_specific_properties=["emission_iris"],
        )
        settings = build_confocal_settings_from_config(config)
        assert settings.illumination_iris is None
        assert settings.emission_iris == ALL_IRIS_DEFAULTS["emission_iris"]

    def test_config_with_empty_properties_no_iris(self):
        config = ConfocalConfig(
            objective_specific_properties=[],
        )
        settings = build_confocal_settings_from_config(config)
        assert settings.illumination_iris is None
        assert settings.emission_iris is None

    def test_config_ignores_non_iris_properties(self):
        config = ConfocalConfig(
            objective_specific_properties=["emission_filter_wheel_position", "illumination_iris"],
        )
        settings = build_confocal_settings_from_config(config)
        assert settings.illumination_iris == ALL_IRIS_DEFAULTS["illumination_iris"]
        assert settings.emission_iris is None
