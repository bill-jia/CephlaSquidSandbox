"""
Acquisition channel configuration models (v1.0 schema).

DEPRECATED: AcquisitionChannel is being replaced by ObservationState.
This file retains AcquisitionChannel and related types during the transition.
New code should use ObservationState from control.models.observation_state.

Shared types (CameraSettings, ConfocalSettings, ChannelGroup, etc.) have moved
to control.models.observation_state and are re-exported here for compatibility.
"""

import logging
from typing import List, Optional, Set, Union, TYPE_CHECKING

from pydantic import BaseModel, Field

# Shared types now live in observation_state.py; re-export for existing imports
from control.models.observation_state import (
    CameraSettings,
    ConfocalSettings,
    SynchronizationMode,
    ChannelGroupEntry,
    ChannelGroup,
)

if TYPE_CHECKING:
    from control.models.illumination_config import IlluminationChannelConfig

logger = logging.getLogger(__name__)


class IlluminationSettings(BaseModel):
    """Illumination configuration for an acquisition channel.

    Note: illumination_channel is required in general.yaml but should be
    omitted in objective-specific files (which only contain overrides).
    """

    illumination_channel: Optional[str] = Field(
        None, description="Illumination channel name from illumination_channel_config (only in general.yaml)"
    )
    intensity: float = Field(..., ge=0, le=100, description="Illumination intensity percentage (0-100)")
    on: bool = Field(False, description="Logical illumination on/off for this acquisition channel")
    led_matrix_mode: Optional[str] = Field(
        None,
        description="LED matrix pattern key when using unified LED matrix (e.g. bf_full, df, left_half)",
    )

    model_config = {"extra": "forbid"}


class AcquisitionChannelOverride(BaseModel):
    """
    Override settings for confocal mode (objective-specific).

    When confocal mode is active, these settings override the base camera
    and illumination settings. Confocal hardware settings (iris) are at
    the channel level in confocal_hardware_settings, not here.
    """

    illumination_settings: Optional[IlluminationSettings] = Field(
        None, description="Override illumination settings for confocal mode"
    )
    camera_settings: Optional[CameraSettings] = Field(None, description="Override camera settings for confocal mode")

    model_config = {"extra": "forbid"}


class AcquisitionChannel(BaseModel):
    """A single acquisition channel configuration (v1.0 schema).

    Key changes in v1.0:
    - camera field is integer ID (references cameras.yaml), null for single-camera
    - filter_wheel resolved via hardware_bindings.yaml based on camera ID
    - z_offset_um at channel level (not in illumination_settings)
    - confocal_hardware_settings: iris settings at channel level (applies in both modes)
    - confocal_override: camera/illumination differences for confocal mode only
    """

    name: str = Field(..., min_length=1, description="Display name for this acquisition channel")
    enabled: bool = Field(True, description="Whether channel is enabled for selection in UI")
    display_color: str = Field(
        "#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$", description="Hex color for UI visualization (e.g., '#FF0000')"
    )

    # Camera assignment (optional for single-camera systems)
    camera: Optional[int] = Field(
        None, ge=1, description="Camera ID (references cameras.yaml). Null for single-camera systems."
    )
    camera_settings: CameraSettings = Field(..., description="Camera settings for this channel")

    # Filter wheel - resolved via hardware_bindings.yaml based on camera ID
    # "auto" = use camera's bound wheel from hardware_bindings, or specific name to override
    filter_wheel: Optional[str] = Field(
        None,
        description="Filter wheel override. 'auto' = use camera's hardware binding, "
        "or specify wheel name to override. Null = no filter wheel.",
    )
    filter_position: Optional[int] = Field(None, ge=1, description="Position in filter wheel")

    # Z offset applied when switching to this channel
    z_offset_um: float = Field(0.0, description="Z offset in micrometers")

    # Illumination
    illumination_settings: IlluminationSettings = Field(..., description="Illumination configuration")

    # Confocal hardware settings (iris, etc.) — applies in both confocal and widefield modes
    confocal_hardware_settings: Optional[ConfocalSettings] = Field(
        None, description="Confocal hardware settings (iris, etc.) — applies in both confocal and widefield modes"
    )

    # Objective-specific override for confocal mode (camera/illumination differences)
    confocal_override: Optional[AcquisitionChannelOverride] = Field(
        None, description="Camera/illumination overrides for confocal mode (objective-specific)"
    )

    model_config = {"extra": "forbid"}

    # ─────────────────────────────────────────────────────────────────────────────
    # Convenience properties for single-camera, single-illumination access
    # ─────────────────────────────────────────────────────────────────────────────

    @property
    def id(self) -> str:
        """Unique identifier derived from channel name (for UI compatibility)."""
        import hashlib

        return hashlib.sha256(self.name.encode()).hexdigest()[:16]

    @property
    def exposure_time(self) -> float:
        """Camera exposure time in ms."""
        return self.camera_settings.exposure_time_ms

    @exposure_time.setter
    def exposure_time(self, value: float) -> None:
        """Set camera exposure time in ms."""
        self.camera_settings.exposure_time_ms = value

    @property
    def analog_gain(self) -> float:
        """Camera analog gain."""
        return self.camera_settings.gain_mode

    @analog_gain.setter
    def analog_gain(self, value: float) -> None:
        """Set camera analog gain."""
        self.camera_settings.gain_mode = value

    # Note: display_color is now a field, not a property

    @property
    def illumination_intensity(self) -> float:
        """Illumination intensity percentage."""
        return self.illumination_settings.intensity

    @illumination_intensity.setter
    def illumination_intensity(self, value: float) -> None:
        """Set illumination intensity percentage."""
        self.illumination_settings.intensity = value

    @property
    def primary_illumination_channel(self) -> Optional[str]:
        """Name of the illumination channel."""
        return self.illumination_settings.illumination_channel

    @property
    def z_offset(self) -> float:
        """Z offset in micrometers."""
        return self.z_offset_um

    @property
    def emission_filter_position(self) -> Optional[int]:
        """Body filter wheel position (for backward compatibility)."""
        return self.filter_position

    def get_illumination_source_code(self, illumination_config: "IlluminationChannelConfig") -> int:
        """Get the illumination source code for the primary illumination channel.

        Args:
            illumination_config: The machine's illumination channel configuration.

        Returns:
            Source code (int) for the primary illumination channel, or 0 if not found.
        """
        ill_channel_name = self.primary_illumination_channel
        if not ill_channel_name:
            return 0
        ill_channel = illumination_config.get_channel_by_name(ill_channel_name)
        if not ill_channel:
            return 0
        return illumination_config.get_source_code(ill_channel)

    def get_illumination_wavelength(self, illumination_config: "IlluminationChannelConfig") -> Optional[int]:
        """Get the wavelength for the primary illumination channel.

        Args:
            illumination_config: The machine's illumination channel configuration.

        Returns:
            Wavelength in nm, or None if not a fluorescence channel.
        """
        ill_channel_name = self.primary_illumination_channel
        if not ill_channel_name:
            return None
        ill_channel = illumination_config.get_channel_by_name(ill_channel_name)
        if not ill_channel:
            return None
        return ill_channel.wavelength_nm

    def get_effective_settings(self, confocal_mode: bool) -> "AcquisitionChannel":
        """
        Get effective settings based on confocal mode.

        If confocal_mode is True and confocal_override exists, merge the
        override settings with the base settings.

        When confocal_mode is True and confocal_override exists, returns a NEW
        AcquisitionChannel with copied settings so that mutations don't corrupt
        the stored override. Otherwise returns self (no copy).
        """
        if not confocal_mode or not self.confocal_override:
            return self

        # Merge illumination: take only intensity from the override, preserving
        # illumination_channel from the base (the override has it as None since
        # it is only set in the base config).
        # Merge camera: full replacement (all camera fields are tunable).
        if self.confocal_override.illumination_settings:
            merged_illumination = self.illumination_settings.model_copy(
                update={"intensity": self.confocal_override.illumination_settings.intensity}
            )
        else:
            merged_illumination = self.illumination_settings.model_copy()

        merged_camera = self.camera_settings.model_copy()
        if self.confocal_override.camera_settings:
            merged_camera = self.confocal_override.camera_settings.model_copy()

        return AcquisitionChannel(
            name=self.name,
            enabled=self.enabled,
            display_color=self.display_color,
            camera=self.camera,
            camera_settings=merged_camera,
            filter_wheel=self.filter_wheel,
            filter_position=self.filter_position,
            z_offset_um=self.z_offset_um,
            illumination_settings=merged_illumination,
            confocal_hardware_settings=(
                self.confocal_hardware_settings.model_copy() if self.confocal_hardware_settings else None
            ),
            confocal_override=self.confocal_override.model_copy(deep=True) if self.confocal_override else None,
        )


class GeneralChannelConfig(BaseModel):
    """
    general.yaml - shared settings across all objectives.

    This file defines the base acquisition channels that are available.
    Objective-specific files can override these settings.

    v1.0 adds channel_groups for multi-camera acquisition support.
    """

    version: Union[int, float] = Field(1, description="Configuration format version")
    channels: List[AcquisitionChannel] = Field(default_factory=list, description="List of acquisition channels")
    channel_groups: List["ChannelGroup"] = Field(
        default_factory=list, description="Channel groups for multi-camera acquisition (v1.0+)"
    )

    model_config = {"extra": "forbid"}

    def get_channel_by_name(self, name: str) -> Optional[AcquisitionChannel]:
        """Get an acquisition channel by name."""
        for ch in self.channels:
            if ch.name == name:
                return ch
        return None

    def get_group_by_name(self, name: str) -> Optional["ChannelGroup"]:
        """Get a channel group by name."""
        for group in self.channel_groups:
            if group.name == name:
                return group
        return None

    def get_group_names(self) -> List[str]:
        """Get list of all channel group names."""
        return [group.name for group in self.channel_groups]


class ObjectiveChannelConfig(BaseModel):
    """
    {objective}.yaml - objective-specific overrides.

    This file contains objective-specific settings that override the
    general.yaml settings. It can also include confocal_override sections.

    Note: channel_groups are NOT included here - they are defined only in general.yaml.
    """

    version: Union[int, float] = Field(1, description="Configuration format version")
    channels: List[AcquisitionChannel] = Field(
        default_factory=list, description="List of acquisition channel overrides"
    )

    model_config = {"extra": "forbid"}

    def get_channel_by_name(self, name: str) -> Optional[AcquisitionChannel]:
        """Get an acquisition channel override by name."""
        for ch in self.channels:
            if ch.name == name:
                return ch
        return None


def merge_channel_configs(
    general: GeneralChannelConfig,
    objective: ObjectiveChannelConfig,
) -> List[AcquisitionChannel]:
    """
    Merge general.yaml and objective.yaml into final acquisition channels (v1.0 schema).

    The merge takes:
    - From general: name, display_color, camera (ID), illumination_channel, filter_wheel,
                    filter_position, z_offset_um
    - From objective: intensity, exposure_time_ms, gain_mode, pixel_format, confocal_hardware_settings, confocal_override

    Args:
        general: General channel configuration (defines channel identity)
        objective: Objective-specific configuration (defines per-objective settings)

    Returns:
        List of merged AcquisitionChannel objects ready for use
    """
    merged_channels = []

    for gen_channel in general.channels:
        # Find matching objective channel by name
        obj_channel = objective.get_channel_by_name(gen_channel.name)

        if obj_channel is None:
            # No objective override - use general settings as-is
            merged_channels.append(gen_channel)
            continue

        # Merge illumination settings
        # general: illumination_channel
        # objective: intensity
        merged_illumination = IlluminationSettings(
            illumination_channel=gen_channel.illumination_settings.illumination_channel,
            intensity=obj_channel.illumination_settings.intensity,
            led_matrix_mode=gen_channel.illumination_settings.led_matrix_mode,
        )

        # Merge camera settings (v1.0: single object, not Dict)
        # general: (nothing - display_color is now at channel level)
        # objective: exposure_time_ms, gain_mode, pixel_format
        merged_camera = CameraSettings(
            exposure_time_ms=obj_channel.camera_settings.exposure_time_ms,
            gain_mode=obj_channel.camera_settings.gain_mode,
            pixel_format=obj_channel.camera_settings.pixel_format,
        )

        merged_channel = AcquisitionChannel(
            name=gen_channel.name,
            enabled=gen_channel.enabled,  # From general
            display_color=gen_channel.display_color,  # From general
            camera=gen_channel.camera,  # From general (camera ID)
            camera_settings=merged_camera,
            filter_wheel=gen_channel.filter_wheel,  # From general
            filter_position=gen_channel.filter_position,  # From general
            z_offset_um=gen_channel.z_offset_um,  # From general
            illumination_settings=merged_illumination,
            confocal_hardware_settings=obj_channel.confocal_hardware_settings,  # From objective
            confocal_override=obj_channel.confocal_override,  # From objective
        )
        merged_channels.append(merged_channel)

    return merged_channels


def validate_illumination_references(
    config: GeneralChannelConfig,
    illumination_config: "IlluminationChannelConfig",
) -> List[str]:
    """
    Validate that all illumination_channel references in acquisition config
    exist in illumination_channel_config.yaml.

    Args:
        config: Acquisition channel configuration to validate
        illumination_config: Illumination channel configuration with available channels

    Returns:
        List of error messages. Empty list if all references are valid.
    """
    errors = []
    valid_names: Set[str] = {ch.name for ch in illumination_config.channels}

    for acq_channel in config.channels:
        ill_channel = acq_channel.illumination_settings.illumination_channel
        if ill_channel and ill_channel not in valid_names:
            errors.append(
                f"Acquisition channel '{acq_channel.name}' references "
                f"illumination channel '{ill_channel}' which does not exist in "
                f"illumination_channel_config.yaml"
            )

    return errors


def get_illumination_channel_names(config: GeneralChannelConfig) -> Set[str]:
    """
    Get all unique illumination channel names referenced in a config.

    Args:
        config: Acquisition channel configuration

    Returns:
        Set of illumination channel names
    """
    names: Set[str] = set()
    for acq_channel in config.channels:
        if acq_channel.illumination_settings.illumination_channel:
            names.add(acq_channel.illumination_settings.illumination_channel)
    return names


class AcquisitionOutputConfig(BaseModel):
    """
    Output format for acquisition settings saved alongside acquired images.

    This is written to acquisition_channels.yaml in the experiment output directory
    to record what settings were used during acquisition.
    """

    version: Union[int, float] = Field(1, description="Configuration format version")
    objective: str = Field(..., description="Objective used for acquisition")
    confocal_mode: bool = Field(False, description="Whether confocal mode was active")
    channels: List[AcquisitionChannel] = Field(
        default_factory=list,
        description="Acquisition channels when applicable; empty when only observation_state_names is used",
    )
    observation_state_names: Optional[List[str]] = Field(
        None,
        description="When multipoint uses Observation State presets, names of selected presets",
    )

    model_config = {"extra": "forbid"}


def validate_channel_group(
    group: ChannelGroup,
    channels: List[AcquisitionChannel],
) -> List[str]:
    """Validate channel group configuration."""
    errors = []
    cameras_used: List[Optional[int]] = []
    for entry in group.channels:
        channel = next((c for c in channels if c.name == entry.name), None)
        if channel is None:
            errors.append(f"Channel '{entry.name}' not found in channels list")
            continue
        cameras_used.append(channel.camera)
        if group.synchronization == SynchronizationMode.SIMULTANEOUS and channel.camera is None:
            errors.append(f"Channel '{entry.name}' has no camera ID but is in simultaneous group '{group.name}'")
        if group.synchronization == SynchronizationMode.SEQUENTIAL and entry.offset_us != 0:
            errors.append(
                f"Channel '{entry.name}' has offset_us={entry.offset_us} "
                f"but group '{group.name}' is sequential (offset will be ignored)"
            )
    if group.synchronization == SynchronizationMode.SIMULTANEOUS:
        non_null_cameras = [c for c in cameras_used if c is not None]
        if len(non_null_cameras) != len(set(non_null_cameras)):
            duplicate_cameras = [c for c in set(non_null_cameras) if non_null_cameras.count(c) > 1]
            errors.append(
                f"Group '{group.name}' uses simultaneous mode but has "
                f"multiple channels on same camera ID: {duplicate_cameras}"
            )
    return errors
