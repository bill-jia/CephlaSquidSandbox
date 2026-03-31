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
    ConfocalSettings,
    ObservationState,
    IlluminatorState,
    SynchronizationMode,
    ChannelGroupEntry,
    ChannelGroup,
)

if TYPE_CHECKING:
    from control.models.illumination_config import IlluminationChannelConfig

logger = logging.getLogger(__name__)


class GeneralObservationConfig(BaseModel):
    """general.yaml — defines available observation states shared across objectives."""

    version: Union[int, float] = Field(3)
    observation_states: List[ObservationState] = Field(default_factory=list)
    channel_groups: List[ChannelGroup] = Field(default_factory=list)
    model_config = {"extra": "forbid"}

    def get_by_name(self, name: str) -> Optional[ObservationState]:
        for s in self.observation_states:
            if s.name == name:
                return s
        return None

    def get_group_by_name(self, name: str) -> Optional[ChannelGroup]:
        for g in self.channel_groups:
            if g.name == name:
                return g
        return None

    def get_group_names(self) -> List[str]:
        return [g.name for g in self.channel_groups]


class ObjectiveOverride(BaseModel):
    """Per-observation-state objective overrides."""

    name: str = Field(..., min_length=1)
    camera_settings: Optional[CameraSettings] = None
    confocal_hardware_settings: Optional[ConfocalSettings] = None
    model_config = {"extra": "forbid"}


class ObjectiveOverrideConfig(BaseModel):
    """{objective}.yaml — per-objective overrides."""

    version: Union[int, float] = Field(3)
    overrides: List[ObjectiveOverride] = Field(default_factory=list)
    model_config = {"extra": "forbid"}

    def get_by_name(self, name: str) -> Optional[ObjectiveOverride]:
        for o in self.overrides:
            if o.name == name:
                return o
        return None


def merge_observation_configs(
    general: GeneralObservationConfig,
    objective: ObjectiveOverrideConfig,
) -> List[ObservationState]:
    """Merge general.yaml and objective.yaml into final ObservationStates."""
    merged = []
    for state in general.observation_states:
        override = objective.get_by_name(state.name)
        if override is None:
            merged.append(state)
            continue
        updates = {}
        if override.camera_settings is not None:
            updates["camera_settings"] = override.camera_settings
        if override.confocal_hardware_settings is not None:
            updates["confocal_hardware_settings"] = override.confocal_hardware_settings
        merged.append(state.model_copy(update=updates) if updates else state)
    return merged


class AcquisitionOutputConfig(BaseModel):
    """Output format for acquisition settings saved alongside acquired images."""

    version: Union[int, float] = Field(3)
    objective: str = Field(...)
    confocal_mode: bool = Field(False)
    observation_state_names: List[str] = Field(default_factory=list)
    model_config = {"extra": "forbid"}


def validate_illumination_references(
    config: GeneralObservationConfig,
    illumination_config: "IlluminationChannelConfig",
) -> List[str]:
    """Validate that all illumination_channel references in observation states exist."""
    errors = []
    valid_names: Set[str] = {ch.name for ch in illumination_config.channels}

    for state in config.observation_states:
        for ist in state.illuminator_states:
            if ist.illumination_channel and ist.illumination_channel not in valid_names:
                errors.append(
                    f"Observation state '{state.name}' references illumination channel "
                    f"'{ist.illumination_channel}' which does not exist"
                )

    return errors


def get_illumination_channel_names(config: GeneralObservationConfig) -> Set[str]:
    """Get all unique illumination channel names referenced in a config."""
    names: Set[str] = set()
    for state in config.observation_states:
        for ist in state.illuminator_states:
            if ist.illumination_channel:
                names.add(ist.illumination_channel)
    return names


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
