"""
Configuration management for Squid microscope.

This module provides:
- ConfigRepository: Centralized config I/O and caching
- Utility functions for config manipulation

Example usage:
    from control.core.config import ConfigRepository

    repo = ConfigRepository()
    repo.set_profile("default")

    state = repo.get_observation_state()
"""

from control.core.config.repository import ConfigRepository
from control.core.config.utils import (
    validate_illumination_references,
    get_illumination_channel_names,
    copy_profile_configs,
)

__all__ = [
    "ConfigRepository",
    "validate_illumination_references",
    "get_illumination_channel_names",
    "copy_profile_configs",
]
