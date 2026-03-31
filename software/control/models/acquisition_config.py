"""
Acquisition configuration models (v3 schema).

All acquisition channels are now ObservationState objects.
AcquisitionChannel has been removed.

Shared types (CameraSettings, ConfocalSettings, ChannelGroup, etc.) live
in control.models.observation_state.
"""

import logging
from typing import List, Optional, Set, Union, TYPE_CHECKING

from pydantic import BaseModel, Field

from control.models.observation_state import (
    CameraSettings,
    ObservationState,
    IlluminatorState,
    SynchronizationMode,
    ChannelGroupEntry,
    ChannelGroup,
)

if TYPE_CHECKING:
    from control.models.illumination_config import IlluminationChannelConfig

logger = logging.getLogger(__name__)


class AcquisitionOutputConfig(BaseModel):
    """Output format for acquisition settings saved alongside acquired images."""

    version: Union[int, float] = Field(3)
    objective: str = Field(...)
    confocal_mode: bool = Field(False)
    observation_state_names: List[str] = Field(default_factory=list)
    model_config = {"extra": "forbid"}


def validate_illumination_references(
    state: ObservationState,
    illumination_config: "IlluminationChannelConfig",
) -> List[str]:
    """Validate that all illumination_channel references in the observation state exist."""
    errors = []
    valid_names: Set[str] = {ch.name for ch in illumination_config.channels}
    for ist in state.illuminator_states:
        if ist.illumination_channel and ist.illumination_channel not in valid_names:
            errors.append(
                f"Observation state references illumination channel "
                f"'{ist.illumination_channel}' which does not exist"
            )
    return errors


def get_illumination_channel_names(state: ObservationState) -> Set[str]:
    """Get all unique illumination channel names referenced in an observation state."""
    return {
        ist.illumination_channel
        for ist in state.illuminator_states
        if ist.illumination_channel
    }


def validate_channel_group(
    group: ChannelGroup,
    observation_states: List[ObservationState],
) -> List[str]:
    """Validate channel group configuration against observation states."""
    errors = []
    for entry in group.channels:
        state = next((s for s in observation_states if s.name == entry.name), None)
        if state is None:
            errors.append(f"Observation state '{entry.name}' not found in states list")
            continue
        if group.synchronization == SynchronizationMode.SEQUENTIAL and entry.offset_us != 0:
            errors.append(
                f"Observation state '{entry.name}' has offset_us={entry.offset_us} "
                f"but group '{group.name}' is sequential (offset will be ignored)"
            )
    return errors
