"""
Default configuration generator.

Generates default acquisition configuration files when a user has no
existing configs. Uses illumination_channel_config.yaml as the source
for available channels and creates appropriate defaults.
"""

import logging
from pathlib import Path
from typing import List, Optional

from control.core.config import ConfigRepository
from control.models import (
    CameraSettings,
    ConfocalSettings,
)
from control.models.observation_state import (
    IlluminatorState,
    ObservationState,
)
from control.models.illumination_config import (
    DEFAULT_LED_COLOR,
    DEFAULT_WAVELENGTH_COLORS,
    IlluminationChannel,
    IlluminationChannelConfig,
    IlluminationType,
)
from control.models.machine_config import ConfocalDeviceSettings

logger = logging.getLogger(__name__)

# Default values for acquisition settings
DEFAULT_EXPOSURE_TIME_MS = 20.0
DEFAULT_GAIN_MODE = 10.0
DEFAULT_ILLUMINATION_INTENSITY = 20.0
DEFAULT_LED_ILLUMINATION_INTENSITY = 5.0  # Lower intensity for USB LED sources


def build_confocal_settings(
    device_settings: Optional[ConfocalDeviceSettings] = None,
) -> ConfocalSettings:
    """Build ConfocalSettings seeded from the confocal device's iris defaults.

    Args:
        device_settings: Settings from ``devices.xlight.config`` (or
            ``devices.dragonfly.config``).  None falls back to the model defaults.

    Returns:
        ConfocalSettings with both iris fields set.
    """
    device_settings = device_settings or ConfocalDeviceSettings()
    return ConfocalSettings(
        illumination_iris=float(device_settings.illumination_iris_default),
        emission_iris=float(device_settings.emission_iris_default),
    )


def get_display_color_for_channel(channel: IlluminationChannel) -> str:
    """Get the display color for an illumination channel based on wavelength."""
    if channel.wavelength_nm is not None:
        return DEFAULT_WAVELENGTH_COLORS.get(channel.wavelength_nm, DEFAULT_LED_COLOR)
    return DEFAULT_LED_COLOR


def generate_default_observation_state(
    illumination_config: IlluminationChannelConfig,
    confocal_settings: Optional[ConfocalDeviceSettings] = None,
) -> ObservationState:
    """
    Generate a default ObservationState with all illumination channels.

    Creates a single ObservationState with one IlluminatorState per channel.
    All channels start with on=False except the first.

    Args:
        illumination_config: Available illumination channels
        confocal_settings: Confocal device settings; None = no confocal block

    Returns:
        ObservationState with default settings for all channels
    """
    illuminator_states = []
    display_color = "#FFFFFF"
    for i, ill_channel in enumerate(illumination_config.channels):
        is_led = ill_channel.type == IlluminationType.TRANSILLUMINATION
        intensity = DEFAULT_LED_ILLUMINATION_INTENSITY if is_led else DEFAULT_ILLUMINATION_INTENSITY
        illuminator_states.append(
            IlluminatorState(
                illumination_channel=ill_channel.name,
                intensity=intensity,
                on=(i == 0),  # First channel on by default
            )
        )
        if i == 0:
            display_color = get_display_color_for_channel(ill_channel)

    confocal_hw = build_confocal_settings(confocal_settings) if confocal_settings is not None else None

    return ObservationState(
        version=3,
        name="live",
        display_color=display_color,
        camera_settings=CameraSettings(
            exposure_time_ms=DEFAULT_EXPOSURE_TIME_MS,
            gain_mode=DEFAULT_GAIN_MODE,
        ),
        illuminator_states=illuminator_states,
        confocal_hardware_settings=confocal_hw,
    )


def has_legacy_configs_to_migrate(profile: str, base_path: Optional[Path] = None) -> bool:
    """
    Check if there are legacy configs (XML/JSON) that need migration.

    Legacy configs are in acquisition_configurations/{profile}/{objective}/ with:
    - channel_configurations.xml (non-confocal systems)
    - widefield_configurations.xml (confocal systems)
    - confocal_configurations.xml (confocal overrides, optional)
    - laser_af_settings.json (optional)

    If these exist, we should NOT generate default configs - migration should run first.

    Args:
        profile: Profile name to check
        base_path: Base path to software directory (auto-detected if None)

    Returns:
        True if legacy configs exist that need migration
    """
    if base_path is None:
        base_path = Path(__file__).parent.parent

    legacy_path = base_path / "acquisition_configurations" / profile

    if not legacy_path.exists():
        return False

    # Check for channel XML files in any subdirectory (objective folders)
    for item in legacy_path.iterdir():
        if item.is_dir() and not item.name.startswith("."):
            if (item / "channel_configurations.xml").exists():
                return True
            if (item / "widefield_configurations.xml").exists():
                return True

    return False


def ensure_default_configs(
    config_repo: ConfigRepository,
    profile: str,
) -> bool:
    """
    Ensure a profile has default configurations.

    If the profile doesn't have a general.yaml, generates default configs
    based on the illumination_channel_config.

    NOTE: This function will NOT generate defaults if there are legacy
    configs (XML/JSON) that need migration. The migration script should run first.

    Args:
        config_repo: ConfigRepository instance
        profile: Profile name

    Returns:
        True if configs were generated, False if they already existed or migration is pending
    """
    if config_repo.profile_has_configs(profile):
        logger.debug(f"Profile '{profile}' already has configs")
        return False

    if has_legacy_configs_to_migrate(profile):
        logger.info(
            f"Profile '{profile}' has legacy configs pending migration. "
            "Skipping default generation - run migration first."
        )
        return False

    illumination_config = config_repo.get_illumination_config()
    if illumination_config is None:
        logger.error("Cannot generate defaults: illumination_channel_config.yaml not found")
        raise FileNotFoundError("illumination_channel_config.yaml is required to generate default configs")

    logger.info(f"Generating default configs for profile '{profile}'")
    general_config = generate_default_observation_state(
        illumination_config, confocal_settings=config_repo.get_confocal_settings()
    )

    config_repo.ensure_profile_directories(profile)
    config_repo.save_observation_state(profile, general_config)

    logger.info(f"Generated default configs for profile '{profile}': general.yaml")
    return True
