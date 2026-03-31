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
    GeneralObservationConfig,
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
from control._def import XLIGHT_EMISSION_IRIS_DEFAULT, XLIGHT_ILLUMINATION_IRIS_DEFAULT
from control.models.confocal_config import ConfocalConfig

logger = logging.getLogger(__name__)

# Default values for acquisition settings
DEFAULT_EXPOSURE_TIME_MS = 20.0
DEFAULT_GAIN_MODE = 10.0
DEFAULT_ILLUMINATION_INTENSITY = 20.0
DEFAULT_LED_ILLUMINATION_INTENSITY = 5.0  # Lower intensity for USB LED sources
DEFAULT_Z_OFFSET_UM = 0.0

# Confocal iris properties and their defaults from _def.py
ALL_IRIS_DEFAULTS = {
    "illumination_iris": float(XLIGHT_ILLUMINATION_IRIS_DEFAULT),
    "emission_iris": float(XLIGHT_EMISSION_IRIS_DEFAULT),
}


def build_confocal_settings_from_config(
    confocal_config: Optional[ConfocalConfig] = None,
) -> ConfocalSettings:
    """Build ConfocalSettings with iris fields driven by confocal_config.yaml.

    Resolution order:
    1. Model registry: if confocal_config has a model field, use its objective_properties
    2. Backwards compat: use objective_specific_properties string list
    3. Fallback (no config): include all iris properties at default value

    Args:
        confocal_config: Confocal hardware config (None = include all iris fields)

    Returns:
        ConfocalSettings with matching iris fields set to defaults
    """
    if confocal_config is not None:
        # Try model registry first
        model_def = confocal_config.get_model_def()
        if model_def is not None:
            return ConfocalSettings(**model_def.objective_properties)
        # Backwards compat: use objective_specific_properties string list
        iris_props = set(ALL_IRIS_DEFAULTS) & set(confocal_config.objective_specific_properties)
        kwargs = {prop: ALL_IRIS_DEFAULTS[prop] for prop in iris_props}
        return ConfocalSettings(**kwargs)
    # No config: fallback to all iris properties
    return ConfocalSettings(**ALL_IRIS_DEFAULTS)


def get_display_color_for_channel(channel: IlluminationChannel) -> str:
    """Get the display color for an illumination channel based on wavelength."""
    if channel.wavelength_nm is not None:
        return DEFAULT_WAVELENGTH_COLORS.get(channel.wavelength_nm, DEFAULT_LED_COLOR)
    return DEFAULT_LED_COLOR


def create_general_observation_state(
    illumination_channel: IlluminationChannel,
    include_confocal: bool = False,
) -> ObservationState:
    """
    Create an ObservationState for general.yaml.

    Args:
        illumination_channel: The illumination channel to create from
        include_confocal: Whether to include confocal settings

    Returns:
        ObservationState for general.yaml
    """
    display_color = get_display_color_for_channel(illumination_channel)

    camera_settings = CameraSettings(
        exposure_time_ms=DEFAULT_EXPOSURE_TIME_MS,
        gain_mode=DEFAULT_GAIN_MODE,
    )

    illuminator_state = IlluminatorState(
        illumination_channel=illumination_channel.name,
        intensity=DEFAULT_ILLUMINATION_INTENSITY,
        on=False,
    )

    return ObservationState(
        version=3,
        name=illumination_channel.name,
        confocal_mode=False,
        camera_settings=camera_settings,
        illuminator_states=[illuminator_state],
        z_offset_um=DEFAULT_Z_OFFSET_UM,
        display_color=display_color,
    )


def generate_general_config(
    illumination_config: IlluminationChannelConfig,
    include_confocal: bool = False,
    camera_id: Optional[int] = None,
) -> GeneralObservationConfig:
    """
    Generate a general.yaml configuration from illumination channels.

    Args:
        illumination_config: Available illumination channels
        include_confocal: Whether to include confocal settings
        camera_id: Camera ID (unused in v3 — kept for signature compatibility)

    Returns:
        GeneralObservationConfig with default observation states
    """
    observation_states = []
    for ill_channel in illumination_config.channels:
        state = create_general_observation_state(
            ill_channel, include_confocal=include_confocal
        )
        observation_states.append(state)

    return GeneralObservationConfig(version=3, observation_states=observation_states, channel_groups=[])


def generate_default_configs(
    illumination_config: IlluminationChannelConfig,
    include_confocal: bool = False,
) -> GeneralObservationConfig:
    """
    Generate default general.yaml config.

    Args:
        illumination_config: Available illumination channels
        include_confocal: Whether to include confocal settings

    Returns:
        GeneralObservationConfig with default observation states
    """
    return generate_general_config(illumination_config, include_confocal=include_confocal)


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
    include_confocal: bool = False,
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
        include_confocal: Whether to include confocal-related settings

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
    general_config = generate_default_configs(illumination_config, include_confocal=include_confocal)

    config_repo.ensure_profile_directories(profile)
    config_repo.save_general_config(profile, general_config)

    logger.info(f"Generated default configs for profile '{profile}': general.yaml")
    return True
