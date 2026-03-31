"""
Utility functions for configuration management.

Pure functions that operate on config models without side effects.
"""

import shutil
from typing import List, TYPE_CHECKING

from control.models import (
    GeneralObservationConfig,
    ObjectiveOverrideConfig,
    merge_observation_configs,
    validate_illumination_references,
    get_illumination_channel_names,
)
from control.models.observation_state import ObservationState

if TYPE_CHECKING:
    from control.core.config.repository import ConfigRepository

# Re-export from models for convenience
__all__ = [
    # Re-exports from models
    "merge_observation_configs",
    "validate_illumination_references",
    "get_illumination_channel_names",
    # New utilities
    "get_effective_observation_states",
    "copy_profile_configs",
]


def get_effective_observation_states(
    general: GeneralObservationConfig,
    objective: ObjectiveOverrideConfig,
) -> List[ObservationState]:
    """
    Get the effective observation states for a given objective.

    This is a convenience function that calls merge_observation_configs().

    Args:
        general: General observation configuration
        objective: Objective-specific override configuration

    Returns:
        List of merged ObservationState objects
    """
    return merge_observation_configs(general, objective)


def copy_profile_configs(
    repo: "ConfigRepository",
    source_profile: str,
    dest_profile: str,
) -> None:
    """
    Copy all configuration files from source profile to destination profile.

    Copies both channel_configs/ and laser_af_configs/ directories.
    The destination profile must already exist (created via repo.create_profile()).

    Args:
        repo: ConfigRepository instance
        source_profile: Name of source profile
        dest_profile: Name of destination profile

    Raises:
        ValueError: If source or destination profile doesn't exist
    """
    if not repo.profile_exists(source_profile):
        raise ValueError(f"Source profile '{source_profile}' does not exist")
    if not repo.profile_exists(dest_profile):
        raise ValueError(f"Destination profile '{dest_profile}' does not exist")

    source_path = repo.user_profiles_path / source_profile
    dest_path = repo.user_profiles_path / dest_profile

    # Copy channel_configs
    source_channels = source_path / "channel_configs"
    dest_channels = dest_path / "channel_configs"
    if source_channels.exists():
        for yaml_file in source_channels.glob("*.yaml"):
            shutil.copy2(yaml_file, dest_channels / yaml_file.name)

    # Copy laser_af_configs
    source_laser_af = source_path / "laser_af_configs"
    dest_laser_af = dest_path / "laser_af_configs"
    if source_laser_af.exists():
        for yaml_file in source_laser_af.glob("*.yaml"):
            shutil.copy2(yaml_file, dest_laser_af / yaml_file.name)
