"""
Unit tests for acquisition configuration models.

Tests the Pydantic models in control/models/ for:
- IlluminationChannelConfig
- ConfocalDeviceSettings
- CameraMappingsConfig
- AcquisitionChannel, GeneralChannelConfig, ObjectiveChannelConfig
- LaserAFConfig
"""

import pytest
from pydantic import ValidationError

from control.models import (
    CameraHardwareInfo,
    CameraMappingsConfig,
    CameraPropertyBindings,
    CameraSettings,
    ConfocalDeviceSettings,
    ConfocalSettings,
    FilterWheelDefinition,
    FilterWheelType,
    LaserAFConfig,
)
from control.models.illumination_config import (
    IlluminationChannel,
    IlluminationChannelConfig,
)
from control.models.machine_config import DeviceEntry
from control.models.observation_state import (
    ObservationState,
    IlluminatorState,
)
from control.models.illumination_config import (
    DEFAULT_LED_COLOR,
    DEFAULT_WAVELENGTH_COLORS,
    IlluminationType,
)
from control._def import SpotDetectionMode


class TestIlluminationChannelConfig:
    """Tests for IlluminationChannel and IlluminationChannelConfig models."""

    def test_illumination_channel_creation(self):
        """Test creating an illumination channel with required fields."""
        channel = IlluminationChannel(
            name="Fluorescence 488nm",
            type=IlluminationType.EPI_ILLUMINATION,
            wavelength_nm=488,
            controller_port="D2",
        )
        assert channel.name == "Fluorescence 488nm"
        assert channel.type == IlluminationType.EPI_ILLUMINATION
        assert channel.wavelength_nm == 488
        assert channel.controller_port == "D2"
        assert channel.intensity_calibration_file is None  # Default

    def test_illumination_channel_led_matrix(self):
        """Test creating an LED matrix channel without wavelength."""
        channel = IlluminationChannel(
            name="BF LED matrix full",
            type=IlluminationType.TRANSILLUMINATION,
            wavelength_nm=None,
            controller_port="USB1",  # USB port encodes the LED pattern
        )
        assert channel.wavelength_nm is None
        assert channel.type == IlluminationType.TRANSILLUMINATION
        assert channel.controller_port == "USB1"

    def test_illumination_channel_with_calibration(self):
        """Test channel with intensity calibration file."""
        channel = IlluminationChannel(
            name="Fluorescence 405nm",
            type=IlluminationType.EPI_ILLUMINATION,
            wavelength_nm=405,
            controller_port="D1",
            intensity_calibration_file="405.csv",
        )
        assert channel.intensity_calibration_file == "405.csv"

    def test_illumination_channel_config_get_by_name(self):
        """Test getting channel by name from config."""
        config = IlluminationChannelConfig(
            version=1,
            channels=[
                IlluminationChannel(
                    name="Channel A",
                    type=IlluminationType.EPI_ILLUMINATION,
                    controller_port="D1",
                    wavelength_nm=405,
                ),
                IlluminationChannel(
                    name="Channel B",
                    type=IlluminationType.TRANSILLUMINATION,
                    controller_port="USB1",
                    led_matrix_pattern="full",
                ),
            ],
        )

        found = config.get_channel_by_name("Channel A")
        assert found is not None
        assert found.name == "Channel A"

        not_found = config.get_channel_by_name("Nonexistent")
        assert not_found is None

    def test_illumination_channel_config_get_source_code(self):
        """Test resolving source code from controller port mapping."""
        config = IlluminationChannelConfig(
            version=1,
            controller_port_mapping={
                "D1": 11,
                "D2": 12,
                "USB1": 0,
                "USB4": 3,  # USB ports encode LED patterns
            },
            channels=[
                IlluminationChannel(
                    name="Laser 405",
                    type=IlluminationType.EPI_ILLUMINATION,
                    controller_port="D1",
                    wavelength_nm=405,
                ),
                IlluminationChannel(
                    name="BF LED",
                    type=IlluminationType.TRANSILLUMINATION,
                    controller_port="USB4",  # dark_field pattern
                ),
            ],
        )

        # Test laser channel - should get source code from controller_port_mapping
        laser = config.get_channel_by_name("Laser 405")
        assert config.get_source_code(laser) == 11

        # Test LED channel - should get source code from controller_port_mapping (USB port)
        led = config.get_channel_by_name("BF LED")
        assert config.get_source_code(led) == 3

    def test_illumination_channel_config_get_by_source_code(self):
        """Test getting channel by source code."""
        config = IlluminationChannelConfig(
            version=1,
            controller_port_mapping={"D1": 11},
            channels=[
                IlluminationChannel(
                    name="Channel A",
                    type=IlluminationType.EPI_ILLUMINATION,
                    controller_port="D1",
                    wavelength_nm=405,
                ),
            ],
        )

        found = config.get_channel_by_source_code(11)
        assert found is not None
        assert found.name == "Channel A"

        not_found = config.get_channel_by_source_code(99)
        assert not_found is None

    def test_default_wavelength_colors(self):
        """Test default color mapping for common wavelengths."""
        assert 405 in DEFAULT_WAVELENGTH_COLORS
        assert 488 in DEFAULT_WAVELENGTH_COLORS
        assert 561 in DEFAULT_WAVELENGTH_COLORS
        assert 638 in DEFAULT_WAVELENGTH_COLORS
        assert DEFAULT_LED_COLOR == "#FFFFFF"


class TestConfocalDeviceSettings:
    """Tests for ConfocalDeviceSettings (devices.xlight.config / devices.dragonfly.config)."""

    def test_defaults(self):
        """An empty config block yields the model defaults."""
        settings = ConfocalDeviceSettings()
        assert settings.sleep_time_for_wheel == 0.25
        assert settings.validate_wheel_pos is False
        assert settings.illumination_iris_default == 100.0
        assert settings.emission_iris_default == 100.0
        assert settings.emission_filter_wheel.positions == {}
        # No declared wheel: fall back to the X-Light V3 slot count.
        assert settings.emission_filter_positions == 8
        assert settings.build_emission_wheel_definition() is None

    def test_from_device_entry_none(self):
        """No device entry gives defaults."""
        settings = ConfocalDeviceSettings.from_device_entry(None)
        assert settings.sleep_time_for_wheel == 0.25
        assert settings.emission_filter_positions == 8

    def test_from_device_entry_empty_config(self):
        """A device entry with no config block gives defaults."""
        entry = DeviceEntry(driver="xlight")
        settings = ConfocalDeviceSettings.from_device_entry(entry)
        assert settings.validate_wheel_pos is False
        assert settings.illumination_iris_default == 100.0

    def test_from_device_entry_full_block(self):
        """A full config block is read verbatim."""
        entry = DeviceEntry(
            driver="xlight",
            config={
                "sleep_time_for_wheel": 0.4,
                "validate_wheel_pos": True,
                "illumination_iris_default": 80,
                "emission_iris_default": 60,
                "emission_filter_wheel": {
                    "name": "XLight emission wheel",
                    "positions": {1: "Empty", 2: "BP 525/50", 3: "LP 600"},
                },
            },
        )
        settings = ConfocalDeviceSettings.from_device_entry(entry)
        assert settings.sleep_time_for_wheel == 0.4
        assert settings.validate_wheel_pos is True
        assert settings.illumination_iris_default == 80.0
        assert settings.emission_iris_default == 60.0
        assert settings.emission_filter_positions == 3

        wheel = settings.build_emission_wheel_definition()
        assert wheel is not None
        assert wheel.id == 1
        assert wheel.type == FilterWheelType.EMISSION
        assert wheel.name == "XLight emission wheel"
        assert wheel.get_filter_name(2) == "BP 525/50"

    def test_wheel_name_defaults_when_omitted(self):
        """An unnamed wheel gets the single-wheel default name."""
        settings = ConfocalDeviceSettings.model_validate(
            {"emission_filter_wheel": {"positions": {1: "Empty", 2: "BP 525/50"}}}
        )
        wheel = settings.build_emission_wheel_definition()
        assert wheel is not None
        assert wheel.name == "Emission Wheel"
        assert wheel.id == 1
        assert settings.emission_filter_positions == 2

    def test_unknown_key_is_rejected(self):
        """Typos in the config block are caught, not silently ignored."""
        with pytest.raises(ValidationError):
            ConfocalDeviceSettings.model_validate({"emission_filter_positions": 8})


class TestCameraMappingsConfig:
    """Tests for CameraMappingsConfig model."""

    def test_camera_mappings_creation(self):
        """Test creating camera mappings config."""
        config = CameraMappingsConfig(
            version=1,
            hardware_connection_info={
                "camera_1": CameraHardwareInfo(filter_wheel_id=1),
            },
            property_bindings={
                "camera_1": CameraPropertyBindings(dichroic_position=1),
            },
        )
        assert config.version == 1
        assert "camera_1" in config.hardware_connection_info

    def test_camera_mappings_get_hardware_info(self):
        """Test getting hardware info for a camera."""
        config = CameraMappingsConfig(
            hardware_connection_info={
                "camera_1": CameraHardwareInfo(filter_wheel_id=1),
            },
        )

        hw = config.get_hardware_info("camera_1")
        assert hw is not None
        assert hw.filter_wheel_id == 1

        assert config.get_hardware_info("camera_2") is None

    def test_camera_mappings_has_confocal(self):
        """Test checking if confocal is in light path."""
        from control.models.camera_config import ConfocalCameraSettings

        # Without confocal
        config_no_confocal = CameraMappingsConfig(
            hardware_connection_info={
                "camera_1": CameraHardwareInfo(filter_wheel_id=1),
            },
        )
        assert config_no_confocal.has_confocal_in_light_path("camera_1") is False

        # With confocal
        config_with_confocal = CameraMappingsConfig(
            hardware_connection_info={
                "camera_1": CameraHardwareInfo(confocal_settings=ConfocalCameraSettings(filter_wheel_id=1)),
            },
        )
        assert config_with_confocal.has_confocal_in_light_path("camera_1") is True


class TestAcquisitionConfig:
    """Tests for v3 acquisition configuration models."""

    def test_camera_settings_required_fields(self):
        """Test that exposure_time_ms and gain_mode are required."""
        settings = CameraSettings(
            exposure_time_ms=20.0,
            gain_mode=10.0,
        )
        assert settings.exposure_time_ms == 20.0
        assert settings.gain_mode == 10.0

        with pytest.raises(ValidationError):
            CameraSettings()

    def test_confocal_settings_defaults(self):
        """Test confocal settings have correct defaults."""
        settings = ConfocalSettings()
        assert settings.illumination_iris is None
        assert settings.emission_iris is None

    def test_observation_state_creation(self):
        """Test creating an ObservationState."""
        state = ObservationState(
            version=3,
            name="488 nm",
            display_color="#00FF00",
            camera_settings=CameraSettings(exposure_time_ms=25.0, gain_mode=10.0),
            illuminator_states=[
                IlluminatorState(
                    illumination_channel="Fluorescence 488nm",
                    intensity=20.0,
                    on=False,
                ),
            ],
        )
        assert state.name == "488 nm"
        assert state.display_color == "#00FF00"
        assert state.camera_settings.exposure_time_ms == 25.0
        assert state.z_offset_um == 0.0
        assert len(state.illuminator_states) == 1

    def test_observation_state_with_confocal(self):
        """Test ObservationState with confocal hardware settings."""
        state = ObservationState(
            version=3,
            name="488 nm",
            display_color="#00FF00",
            camera_settings=CameraSettings(exposure_time_ms=25.0, gain_mode=10.0),
            illuminator_states=[
                IlluminatorState(
                    illumination_channel="Fluorescence 488nm",
                    intensity=20.0,
                    on=False,
                ),
            ],
            confocal_hardware_settings=ConfocalSettings(
                illumination_iris=50.0,
                emission_iris=75.0,
            ),
        )
        assert state.confocal_hardware_settings is not None
        assert state.confocal_hardware_settings.illumination_iris == 50.0
        assert state.confocal_hardware_settings.emission_iris == 75.0

class TestLaserAFConfig:
    """Tests for LaserAFConfig model."""

    def test_laser_af_config_defaults(self):
        """Test LaserAFConfig has correct defaults."""
        config = LaserAFConfig()
        assert config.version == 1
        assert config.x_offset == 0
        assert config.y_offset == 0
        assert config.width == 1536
        assert config.height == 256
        assert config.pixel_to_um == 1.0
        assert config.has_reference is False
        assert config.laser_af_range == 100.0
        assert config.spot_detection_mode == SpotDetectionMode.DUAL_RIGHT

    def test_laser_af_config_custom_values(self):
        """Test LaserAFConfig with custom values."""
        config = LaserAFConfig(
            x_offset=100,
            y_offset=200,
            width=1024,
            height=512,
            pixel_to_um=0.5,
            has_reference=True,
            spot_detection_mode="single",
        )
        assert config.x_offset == 100
        assert config.y_offset == 200
        assert config.width == 1024
        assert config.height == 512
        assert config.pixel_to_um == 0.5
        assert config.has_reference is True
        assert config.spot_detection_mode == SpotDetectionMode.SINGLE

    def test_laser_af_config_with_reference_image(self):
        """Test LaserAFConfig with reference image data."""
        config = LaserAFConfig(
            has_reference=True,
            reference_image="base64encodeddata",
            reference_image_shape=[256, 1536],
            reference_image_dtype="float32",
        )
        assert config.reference_image == "base64encodeddata"
        assert config.reference_image_shape == [256, 1536]
        assert config.reference_image_dtype == "float32"

    def test_laser_af_config_spot_detection_mode(self):
        """Test spot detection mode getter."""
        from control._def import SpotDetectionMode

        config = LaserAFConfig(spot_detection_mode="dual_left")
        mode = config.get_spot_detection_mode()
        assert mode == SpotDetectionMode.DUAL_LEFT


class TestValidateIlluminationReferences:
    """Tests for validate_illumination_references function."""

    def test_valid_references(self):
        """Test validation passes with valid references."""
        from control.models import validate_illumination_references

        ill_config = IlluminationChannelConfig(
            version=1.0,
            channels=[
                IlluminationChannel(name="Fluorescence 488nm", type=IlluminationType.EPI_ILLUMINATION, controller_port="D2", wavelength_nm=488),
                IlluminationChannel(name="BF LED full", type=IlluminationType.TRANSILLUMINATION, controller_port="USB1"),
            ],
        )
        state = ObservationState(
            version=3,
            name="live",
            camera_settings=CameraSettings(exposure_time_ms=20.0, gain_mode=10.0),
            illuminator_states=[
                IlluminatorState(illumination_channel="Fluorescence 488nm", intensity=20.0, on=False),
                IlluminatorState(illumination_channel="BF LED full", intensity=5.0, on=False),
            ],
        )
        errors = validate_illumination_references(state, ill_config)
        assert len(errors) == 0

    def test_invalid_illumination_channel_reference(self):
        """Test validation fails with invalid illumination_channel reference."""
        from control.models import validate_illumination_references

        ill_config = IlluminationChannelConfig(
            version=1.0,
            channels=[
                IlluminationChannel(name="Fluorescence 488nm", type=IlluminationType.EPI_ILLUMINATION, controller_port="D2", wavelength_nm=488),
            ],
        )
        state = ObservationState(
            version=3,
            name="live",
            camera_settings=CameraSettings(exposure_time_ms=20.0, gain_mode=10.0),
            illuminator_states=[
                IlluminatorState(illumination_channel="Fluorescence 561nm", intensity=20.0, on=False),
            ],
        )
        errors = validate_illumination_references(state, ill_config)
        assert len(errors) == 1
        assert "Fluorescence 561nm" in errors[0]


class TestGetIlluminationChannelNames:
    """Tests for get_illumination_channel_names function."""

    def test_get_names(self):
        """Test extracting illumination channel names from config."""
        from control.models import get_illumination_channel_names

        state = ObservationState(
            version=3,
            name="live",
            camera_settings=CameraSettings(exposure_time_ms=20.0, gain_mode=10.0),
            illuminator_states=[
                IlluminatorState(illumination_channel="Fluorescence 488nm", intensity=20.0, on=False),
                IlluminatorState(illumination_channel="BF LED full", intensity=5.0, on=False),
            ],
        )
        names = get_illumination_channel_names(state)
        assert "Fluorescence 488nm" in names
        assert "BF LED full" in names
        assert len(names) == 2


class TestFieldValidationConstraints:
    """Tests for Pydantic field validation constraints."""

    def test_display_color_valid_hex(self):
        """Test that valid hex colors are accepted."""
        state = ObservationState(version=3, name="Test", display_color="#FF0000",
            camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=0.0), illuminator_states=[])
        assert state.display_color == "#FF0000"

    def test_display_color_lowercase_hex_accepted(self):
        """Test that lowercase hex colors are accepted."""
        state = ObservationState(version=3, name="Test", display_color="#aabbcc",
            camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=0.0), illuminator_states=[])
        assert state.display_color == "#aabbcc"

    def test_display_color_invalid_format_rejected(self):
        """Test that invalid color format is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ObservationState(version=3, name="Test", display_color="FF0000",
                camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=0.0), illuminator_states=[])
        assert "pattern" in str(exc_info.value).lower() or "string" in str(exc_info.value).lower()

    def test_display_color_short_hex_rejected(self):
        """Test that short hex colors (3 digits) are rejected."""
        with pytest.raises(ValidationError):
            ObservationState(version=3, name="Test", display_color="#F00",
                camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=0.0), illuminator_states=[])

    def test_confocal_iris_valid_range(self):
        """Test that iris values in 0-100 range are accepted."""
        settings = ConfocalSettings(illumination_iris=50.0, emission_iris=75.0)
        assert settings.illumination_iris == 50.0
        assert settings.emission_iris == 75.0

    def test_confocal_iris_boundary_values(self):
        """Test iris accepts boundary values (0 and 100)."""
        settings = ConfocalSettings(illumination_iris=0.0, emission_iris=100.0)
        assert settings.illumination_iris == 0.0
        assert settings.emission_iris == 100.0

    def test_confocal_iris_out_of_range_rejected(self):
        """Test that iris values outside 0-100 are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ConfocalSettings(illumination_iris=150.0)
        assert "less than or equal to 100" in str(exc_info.value)
        with pytest.raises(ValidationError) as exc_info:
            ConfocalSettings(emission_iris=-10.0)
        assert "greater than or equal to 0" in str(exc_info.value)

    def test_illumination_intensity_valid_range(self):
        """Test that intensity in 0-100 range is accepted."""
        ist = IlluminatorState(illumination_channel="test", intensity=50.0)
        assert ist.intensity == 50.0

    def test_illumination_intensity_out_of_range_rejected(self):
        """Test that intensity outside 0-100 is rejected."""
        with pytest.raises(ValidationError):
            IlluminatorState(illumination_channel="test", intensity=150.0)
        with pytest.raises(ValidationError):
            IlluminatorState(illumination_channel="test", intensity=-10.0)

    def test_exposure_time_must_be_positive(self):
        """Test that exposure_time_ms must be > 0."""
        with pytest.raises(ValidationError):
            CameraSettings(exposure_time_ms=0.0, gain_mode=0.0)
        with pytest.raises(ValidationError):
            CameraSettings(exposure_time_ms=-10.0, gain_mode=0.0)

    def test_gain_mode_must_be_non_negative(self):
        """Test that gain_mode must be >= 0."""
        settings = CameraSettings(exposure_time_ms=10.0, gain_mode=0.0)
        assert settings.gain_mode == 0.0
        with pytest.raises(ValidationError):
            CameraSettings(exposure_time_ms=10.0, gain_mode=-1.0)


class TestIlluminationChannelValidation:
    """Tests for IlluminationChannel validation constraints."""

    def test_illumination_channel_empty_name_rejected(self):
        """Test that empty illumination channel name is rejected."""
        from pydantic import ValidationError
        from control.models.illumination_config import IlluminationChannel, IlluminationType

        with pytest.raises(ValidationError) as exc_info:
            IlluminationChannel(
                name="",
                type=IlluminationType.EPI_ILLUMINATION,
                controller_port="D1",
            )
        assert "at least 1 character" in str(exc_info.value)

    def test_illumination_channel_invalid_port_rejected(self):
        """Test that invalid controller port is rejected."""
        from pydantic import ValidationError
        from control.models.illumination_config import IlluminationChannel, IlluminationType

        with pytest.raises(ValidationError) as exc_info:
            IlluminationChannel(
                name="Test",
                type=IlluminationType.EPI_ILLUMINATION,
                controller_port="INVALID",  # Should be D1-D8 or USB1-USB8
            )
        assert "pattern" in str(exc_info.value).lower() or "string" in str(exc_info.value).lower()

    def test_illumination_channel_valid_ports(self):
        """Test that valid controller ports are accepted."""
        from control.models.illumination_config import IlluminationChannel, IlluminationType

        # Laser ports
        for port in ["D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8"]:
            channel = IlluminationChannel(
                name=f"Channel {port}",
                type=IlluminationType.EPI_ILLUMINATION,
                controller_port=port,
            )
            assert channel.controller_port == port

        # USB ports
        for port in ["USB1", "USB2", "USB3", "USB4", "USB5", "USB6", "USB7", "USB8"]:
            channel = IlluminationChannel(
                name=f"LED {port}",
                type=IlluminationType.TRANSILLUMINATION,
                controller_port=port,
            )
            assert channel.controller_port == port

    def test_illumination_channel_positive_wavelength(self):
        """Test that wavelength must be positive if provided."""
        from pydantic import ValidationError
        from control.models.illumination_config import IlluminationChannel, IlluminationType

        # Valid: positive wavelength
        channel = IlluminationChannel(
            name="488nm Laser",
            type=IlluminationType.EPI_ILLUMINATION,
            controller_port="D2",
            wavelength_nm=488,
        )
        assert channel.wavelength_nm == 488

        # Invalid: zero wavelength
        with pytest.raises(ValidationError):
            IlluminationChannel(
                name="Invalid",
                type=IlluminationType.EPI_ILLUMINATION,
                controller_port="D2",
                wavelength_nm=0,
            )

        # Invalid: negative wavelength
        with pytest.raises(ValidationError):
            IlluminationChannel(
                name="Invalid",
                type=IlluminationType.EPI_ILLUMINATION,
                controller_port="D2",
                wavelength_nm=-488,
            )

    def test_illumination_channel_excitation_filter_wheel(self):
        """Test illumination channel with excitation filter wheel fields."""
        from control.models.illumination_config import IlluminationChannel, IlluminationType

        channel = IlluminationChannel(
            name="488nm with filter",
            type=IlluminationType.EPI_ILLUMINATION,
            controller_port="D2",
            wavelength_nm=488,
            excitation_filter_wheel="Excitation Filter Wheel",
            excitation_filter_position=2,
        )
        assert channel.excitation_filter_wheel == "Excitation Filter Wheel"
        assert channel.excitation_filter_position == 2

    def test_illumination_channel_excitation_filter_optional(self):
        """Test that excitation filter fields are optional."""
        from control.models.illumination_config import IlluminationChannel, IlluminationType

        channel = IlluminationChannel(
            name="488nm Laser",
            type=IlluminationType.EPI_ILLUMINATION,
            controller_port="D2",
            wavelength_nm=488,
        )
        assert channel.excitation_filter_wheel is None
        assert channel.excitation_filter_position is None

    def test_illumination_channel_excitation_filter_position_must_be_positive(self):
        """Test that excitation filter position must be >= 1."""
        from pydantic import ValidationError
        from control.models.illumination_config import IlluminationChannel, IlluminationType

        with pytest.raises(ValidationError) as exc_info:
            IlluminationChannel(
                name="Invalid",
                type=IlluminationType.EPI_ILLUMINATION,
                controller_port="D2",
                excitation_filter_wheel="Test",
                excitation_filter_position=0,  # Must be >= 1
            )
        assert "greater than or equal to 1" in str(exc_info.value)
