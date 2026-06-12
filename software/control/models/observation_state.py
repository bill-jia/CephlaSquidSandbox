"""
Observation State — the fundamental acquisition unit.

An ObservationState represents the complete light-path configuration needed to
capture images at one or more cameras through a single objective.  It is the
microscopy equivalent of a "channel" but properly separates:

  * **Per-camera settings** (exposure, gain, pixel format) — one set per camera.
  * **Per-illuminator runtime state** (which source, intensity, on/off) — via
    :class:`IlluminatorState` entries.
  * **Global optical-path state** (emission filters, confocal iris, z-offset).

Hardware-level illumination source definitions (port mapping, wavelength,
calibration files) remain in :mod:`control.models.illumination_config`
(:class:`IlluminationChannel`).  ``IlluminatorState.illumination_channel``
references those definitions by name.

Saved under the active profile; objective-free by design.
"""

import logging
from enum import Enum
from typing import Dict, List, Optional, Union

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Camera & Confocal Settings (shared with acquisition config)
# ─────────────────────────────────────────────────────────────────────────────


class CameraSettings(BaseModel):
    """Per-camera settings for an observation state."""

    exposure_time_ms: float = Field(..., gt=0, description="Exposure time in milliseconds")
    gain_mode: float = Field(
        ...,
        ge=0,
        description="Gain setting (currently analog gain value, may become enum in future)",
    )
    pixel_format: Optional[str] = Field(None, description="Pixel format (e.g., 'Mono12')")

    model_config = {"extra": "forbid"}


class ConfocalSettings(BaseModel):
    """Confocal iris aperture settings.

    Note: Filter wheel selection is handled via hardware_bindings.yaml, not here.
    The camera's bound filter wheel (confocal or standalone) is resolved at runtime.
    """

    illumination_iris: Optional[float] = Field(
        None, ge=0, le=100, description="Illumination iris aperture percentage (0-100)"
    )
    emission_iris: Optional[float] = Field(None, ge=0, le=100, description="Emission iris aperture percentage (0-100)")

    model_config = {"extra": "forbid"}


# ─────────────────────────────────────────────────────────────────────────────
# Channel Groups (multi-camera acquisition)
# ─────────────────────────────────────────────────────────────────────────────


class SynchronizationMode(str, Enum):
    """Synchronization mode for channel groups."""

    SIMULTANEOUS = "simultaneous"
    SEQUENTIAL = "sequential"


class ChannelGroupEntry(BaseModel):
    """A channel entry within a channel group."""

    name: str = Field(..., min_length=1, description="Channel name (must exist in channels list)")
    offset_us: float = Field(
        0.0,
        ge=0,
        description="Trigger offset in microseconds (only used for simultaneous mode)",
    )

    model_config = {"extra": "forbid"}


class ChannelGroup(BaseModel):
    """A group of channels to be acquired together.

    For simultaneous mode, each channel must use a different camera.
    """

    name: str = Field(..., min_length=1, description="Group name for UI")
    synchronization: SynchronizationMode = Field(
        SynchronizationMode.SEQUENTIAL,
        description="Capture mode: simultaneous or sequential",
    )
    channels: List[ChannelGroupEntry] = Field(..., min_length=1, description="Channels in this group")

    model_config = {"extra": "forbid"}

    def get_channel_names(self) -> List[str]:
        return [entry.name for entry in self.channels]

    def get_channel_offset(self, channel_name: str) -> float:
        for entry in self.channels:
            if entry.name == channel_name:
                return entry.offset_us
        return 0.0

    def get_channels_sorted_by_offset(self) -> List[ChannelGroupEntry]:
        return sorted(self.channels, key=lambda c: c.offset_us)


# ─────────────────────────────────────────────────────────────────────────────
# Illuminator State
# ─────────────────────────────────────────────────────────────────────────────


class IlluminatorTiming(BaseModel):
    """Regular pulse-comb timing for an NIDAQ-gated illuminator.

    A single pulse is a comb with ``num_pulses=1`` (``period_ms`` is then
    ignored). For a capture-window timed pulse the comb must fit within the
    camera exposure; for a stimulus-only step it must fit within
    :attr:`ObservationState.stimulus_duration_ms`.

    Drives the illuminator's digital gating line via the per-frame NIDAQ
    waveform built by :func:`build_pulse_waveform_for_state`. The DC analog
    intensity (set via the regular ``intensity`` field) is held at level
    throughout the window; only the digital gate is pulsed. Channels that
    lack an NIDAQ digital gating line (LED matrix, serial-only) cannot use
    this and will fail validation when an acquisition runs.
    """

    start_offset_ms: float = Field(0.0, ge=0, description="First pulse start, ms after the step begins")
    pulse_width_ms: float = Field(..., gt=0, description="HIGH width of each pulse in ms")
    period_ms: float = Field(0.0, ge=0, description="Pulse-to-pulse period in ms (ignored when num_pulses=1)")
    num_pulses: int = Field(1, ge=1, description="Number of pulses in the comb")

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _check_comb(self) -> "IlluminatorTiming":
        if self.num_pulses > 1 and self.period_ms <= self.pulse_width_ms:
            raise ValueError(
                "IlluminatorTiming: period_ms must exceed pulse_width_ms for a multi-pulse comb"
            )
        return self

    @property
    def end_ms(self) -> float:
        """Falling edge of the last pulse, ms relative to step start."""
        return self.start_offset_ms + max(0, self.num_pulses - 1) * self.period_ms + self.pulse_width_ms


class IlluminatorState(BaseModel):
    """Runtime state of a single illumination source within an ObservationState.

    References an :class:`~control.models.illumination_config.IlluminationChannel`
    by *name*.  The :class:`~control.lighting.IlluminationController` dispatches
    to the correct hardware device (Teensy LED matrix, NIDAQ, serial laser, etc.)
    based on this name.
    """

    illumination_channel: str = Field(
        ..., min_length=1, description="Name of the illumination source (references IlluminationChannelConfig)"
    )
    intensity: float = Field(0.0, ge=0, le=100, description="Illumination intensity percentage (0-100)")
    on: bool = Field(False, description="Logical on/off state for this source")
    led_matrix_mode: Optional[str] = Field(
        None,
        description="LED matrix pattern key when using unified LED matrix (e.g. bf_full, df, left_half)",
    )
    led_matrix_na: Optional[float] = Field(
        None,
        ge=0,
        le=1,
        description="LED matrix array NA (bf/df/dpc illumination radius) for the unified SciMicroscopy LED matrix",
    )
    timing: Optional[IlluminatorTiming] = Field(
        None,
        description="Optional NIDAQ-driven pulse timing within camera exposure (None = on for full exposure)",
    )

    model_config = {"extra": "forbid"}


# ─────────────────────────────────────────────────────────────────────────────
# Camera Live Snapshot
# ─────────────────────────────────────────────────────────────────────────────


class CameraLiveSnapshot(BaseModel):
    """Runtime camera parameters: ROI, binning, trigger mode."""

    exposure_time_ms: float = Field(..., gt=0, description="Exposure time in milliseconds")
    analog_gain: float = Field(0.0, ge=0, description="Analog gain")
    pixel_format: Optional[str] = Field(None, description="Pixel format string (camera-specific)")
    camera_mode: Optional[str] = Field(None, description="Camera mode string")
    binning_x: int = Field(1, ge=1, description="Horizontal binning factor")
    binning_y: int = Field(1, ge=1, description="Vertical binning factor")
    roi_offset_x: int = Field(0, ge=0, description="ROI offset X (pixels)")
    roi_offset_y: int = Field(0, ge=0, description="ROI offset Y (pixels)")
    roi_width: int = Field(0, ge=0, description="ROI width (pixels)")
    roi_height: int = Field(0, ge=0, description="ROI height (pixels)")
    trigger_mode: Optional[str] = Field(
        None,
        description="Live trigger mode string (e.g. Software Trigger / Hardware Trigger / Continuous Acquisition)",
    )
    trigger_fps: Optional[float] = Field(None, gt=0, description="Target FPS for software/hardware trigger")
    roi_centered: Optional[bool] = Field(
        None,
        description="Whether ROI offsets match centered layout (inferred from geometry when saving)",
    )

    model_config = {"extra": "forbid"}


# ─────────────────────────────────────────────────────────────────────────────
# ObservationState
# ─────────────────────────────────────────────────────────────────────────────


class ObservationState(BaseModel):
    """Complete light-path configuration for a single acquisition step.

    Analogous to a "channel" in typical microscopy acquisition software, but
    correctly separates per-camera settings from per-illuminator state and
    supports multi-camera setups through one objective.
    """

    version: Union[int, float] = Field(3, description="Observation State schema version")
    name: str = Field("live", description="Preset name (used for filenames, UI display)")
    confocal_mode: bool = Field(False, description="Whether confocal imaging mode is active")

    # Per-camera settings (single camera; multi-camera uses channel_groups)
    camera_settings: Optional[CameraSettings] = Field(
        None, description="Camera exposure, gain, pixel format (one per camera)"
    )
    camera_live: Optional[CameraLiveSnapshot] = Field(
        None, description="Camera ROI, binning, trigger mode snapshot"
    )

    # Illumination (per-source runtime state)
    illuminator_states: List[IlluminatorState] = Field(
        default_factory=list,
        description="Per-illumination-source state (intensity, on/off, LED matrix mode)",
    )

    # Optical path
    emission_filter_positions: Dict[str, Union[str, int]] = Field(
        default_factory=dict,
        description="Emission filter wheel id → slot name or index",
    )
    z_offset_um: float = Field(0.0, description="Z offset in micrometers")
    confocal_hardware_settings: Optional[ConfocalSettings] = Field(
        None, description="Confocal iris aperture settings"
    )

    # Presentation
    display_color: str = Field("#FFFFFF", description="Hex color for UI visualization", pattern=r"^#[0-9A-Fa-f]{6}$")

    # Multi-camera groups
    channel_groups: List[ChannelGroup] = Field(
        default_factory=list,
        description="Multi-camera channel groups",
    )

    # UI state
    enable_channel_auto_filter_switching: Optional[bool] = Field(
        None,
        description="If set, emission filter follows observation state selection",
    )

    # Stimulus-only steps (no camera capture; NIDAQ pulse comb only)
    is_stimulus_only: bool = Field(
        False,
        description=(
            "When True, this step runs an NIDAQ pulse comb at a multipoint FOV and "
            "produces no camera frame. The active illuminators with `timing` set "
            "describe the comb; their digital gates are driven by the per-FOV NIDAQ "
            "waveform."
        ),
    )
    stimulus_duration_ms: Optional[float] = Field(
        None,
        gt=0,
        description="Total NIDAQ task window when is_stimulus_only=True (ms)",
    )

    model_config = {"extra": "forbid"}

    # Convenience properties

    @property
    def exposure_time(self) -> float:
        """Exposure time in ms from camera_settings (falls back to camera_live)."""
        if self.camera_settings is not None:
            return self.camera_settings.exposure_time_ms
        if self.camera_live is not None:
            return self.camera_live.exposure_time_ms
        return 1.0

    @property
    def analog_gain(self) -> float:
        """Analog gain from camera_settings (falls back to camera_live)."""
        if self.camera_settings is not None:
            return self.camera_settings.gain_mode
        if self.camera_live is not None:
            return self.camera_live.analog_gain
        return 0.0

    @property
    def active_illuminator_states(self) -> List[IlluminatorState]:
        """IlluminatorStates where on=True."""
        return [ist for ist in self.illuminator_states if ist.on]

    @property
    def is_waveform_driven(self) -> bool:
        """True if any active illuminator has NIDAQ pulse timing configured,
        or if this is a stimulus-only step (always NIDAQ-driven)."""
        if self.is_stimulus_only:
            return True
        return any(ist.on and ist.timing is not None for ist in self.illuminator_states)

    @property
    def step_window_ms(self) -> float:
        """Total duration of this step's NIDAQ window (ms).

        For stimulus-only steps that's ``stimulus_duration_ms``; for capture
        steps it's the camera exposure. Used by the waveform builder to size
        the NIDAQ task and validate pulse-comb fit.
        """
        if self.is_stimulus_only:
            if self.stimulus_duration_ms is None:
                raise ValueError(
                    f"ObservationState '{self.name}' is_stimulus_only=True but stimulus_duration_ms is unset"
                )
            return float(self.stimulus_duration_ms)
        return float(self.exposure_time)
