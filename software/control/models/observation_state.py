"""
Observation State — user-facing, objective-free imaging presets.

Saved under the active profile (see ConfigRepository observation preset helpers).
Does not include objective: presets apply across software objectives; merge with
objective YAML at runtime via ObjectiveStore + merge_channel_configs.
"""

from typing import Dict, List, Optional, Union

from pydantic import BaseModel, Field

from control.models.acquisition_config import AcquisitionChannel, ChannelGroup


class CameraLiveSnapshot(BaseModel):
    """Runtime camera parameters not fully represented in merged channel YAML (ROI, binning, etc.)."""

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


class ObservationState(BaseModel):
    """
    Objective lens-free snapshot of channel definitions and tunables for image acquisition.

    Aligns with general.yaml channel content plus UI state (active channel, confocal).
    """

    version: Union[int, float] = Field(1, description="Observation State schema version")
    confocal_mode: bool = Field(False, description="Whether confocal imaging mode is active")
    active_channel_name: Optional[str] = Field(
        None, description="Selected live/acquisition channel name, if applicable"
    )
    channels: List[AcquisitionChannel] = Field(
        default_factory=list,
        description="Channel rows (general-layer; objective field must not appear)",
    )
    channel_groups: List[ChannelGroup] = Field(
        default_factory=list,
        description="Multi-camera channel groups (from general.yaml)",
    )
    emission_filter_positions: Dict[str, Union[str, int]] = Field(
        default_factory=dict,
        description="Optional global emission filter wheel id → slot name or index",
    )
    illumination_channel_states: Dict[str, bool] = Field(
        default_factory=dict,
        description="Saved illumination controller on/off state keyed by logical illumination channel name",
    )
    camera_live: Optional[CameraLiveSnapshot] = Field(
        None,
        description="Live camera ROI/binning/mode snapshot (applied to hardware on load)",
    )
    binning_x: Optional[int] = Field(
        None,
        ge=1,
        description="Horizontal binning (mirrors camera_live when set; used if camera_live is absent)",
    )
    binning_y: Optional[int] = Field(
        None,
        ge=1,
        description="Vertical binning (mirrors camera_live when set; used if camera_live is absent)",
    )
    camera_mode: Optional[str] = Field(
        None,
        description="Camera acquisition mode string (mirrors camera_live when set; used if camera_live is absent)",
    )
    enable_channel_auto_filter_switching: Optional[bool] = Field(
        None,
        description="If set, mirrors LiveController.enable_channel_auto_filter_switching (emission filter follows channel)",
    )

    model_config = {"extra": "forbid"}
