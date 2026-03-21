"""
Acquisition Metadata — per-run reproducibility record (includes objective).

Written next to experiment outputs. Large/binary payloads should live in
instrument_state.h5 (or NPZ) with paths referenced from this manifest.
"""

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field

from control.models.observation_state import ObservationState


class AcquisitionMetadata(BaseModel):
    """
    Canonical manifest for an acquisition experiment folder.

    Supplements acquisition_channels.yaml and legacy acquisition parameters.json.
    """

    version: Union[int, float] = Field(1, description="Acquisition Metadata schema version")
    experiment_id: str = Field(..., description="Experiment folder / run identifier")
    recording_start_time: float = Field(..., description="Unix time when recording folder was created")
    objective: str = Field(..., description="Software objective name at acquisition start")
    objective_details: Dict[str, Any] = Field(
        default_factory=dict,
        description="Objective store metadata (magnification, pixel size, etc.)",
    )
    confocal_mode: bool = Field(False, description="Confocal vs widefield at acquisition start")
    sensor_pixel_size_um: Optional[float] = Field(None, description="Binned pixel size on sample")
    tube_lens_mm: Optional[float] = None
    trigger_mode: Optional[str] = Field(None, description="Camera/live trigger mode string")
    binning_x: Optional[int] = Field(None, ge=1, description="Horizontal camera binning at acquisition")
    binning_y: Optional[int] = Field(None, ge=1, description="Vertical camera binning at acquisition")
    camera_mode: Optional[str] = Field(None, description="Camera acquisition mode string")
    selected_channel_names: List[str] = Field(
        default_factory=list,
        description="Acquisition channel names selected for this run",
    )
    scan_parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Legacy-compatible scan grid/time parameters (dx, Nx, AF flags, etc.)",
    )
    instrument_state_h5: Optional[str] = Field(
        None,
        description="Relative path to instrument_state.h5 when large/binary blobs are stored",
    )
    observation_state: Optional[ObservationState] = Field(
        None,
        description="Live imaging snapshot (channels, camera_live, etc.) when saved with snap or similar",
    )

    model_config = {"extra": "forbid"}
