"""
Configuration management for Squid microscope.

This module provides:
- ConfigRepository: Centralized config I/O and caching
- Utility functions for config manipulation

Example usage:
    from control.core.config import ConfigRepository, get_effective_observation_states

    repo = ConfigRepository()
    repo.set_profile("default")

    general = repo.get_general_config()
    objective = repo.get_objective_config("20x")

    # Get observation states with objective overrides applied
    states = get_effective_observation_states(general, objective)
"""

from control.core.config.repository import ConfigRepository
from control.core.config.utils import (
    # Re-exports from models
    merge_observation_configs,
    validate_illumination_references,
    get_illumination_channel_names,
    # Utilities
    copy_profile_configs,
    get_effective_observation_states,
)

__all__ = [
    "ConfigRepository",
    # Re-exports from models
    "merge_observation_configs",
    "validate_illumination_references",
    "get_illumination_channel_names",
    # Utilities
    "copy_profile_configs",
    "get_effective_observation_states",
]
