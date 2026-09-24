"""Opt-in, manually bounded contrast autofocus settings."""

from typing import Literal, Optional
import math

from pydantic import BaseModel, Field, model_validator


class ContrastAFSettings(BaseModel):
    method: Literal["legacy", "frequency_assisted"] = "legacy"
    coarse_step_um: Optional[float] = None
    medium_step_um: Optional[float] = None
    fine_step_um: Optional[float] = None
    window_below_um: Optional[float] = None
    window_above_um: Optional[float] = None
    energy_threshold: float = Field(0.5, ge=0, le=1)
    energy_margin: float = Field(0, ge=0)
    sensor_full_scale: Optional[float] = None
    max_frames: int = Field(100, ge=5, le=10000)
    max_moves: int = Field(130, ge=5, le=10000)
    max_time_s: float = Field(120, gt=0)
    max_exposure_ms: float = Field(10000, gt=0)
    verification_tolerance: float = Field(0.25, ge=0, le=1)
    dense_fallback: bool = True

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def check(self):
        for name in ("energy_threshold", "energy_margin", "max_time_s", "max_exposure_ms", "verification_tolerance"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        if self.sensor_full_scale is not None and (not math.isfinite(self.sensor_full_scale) or self.sensor_full_scale <= 0):
            raise ValueError("sensor_full_scale must be finite and positive")
        if self.method == "frequency_assisted":
            names = ("coarse_step_um", "medium_step_um", "fine_step_um", "window_below_um", "window_above_um")
            if any(getattr(self, name) is None for name in names):
                raise ValueError("Frequency-assisted autofocus requires all three steps and both window extents")
            if any(not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0 for name in names):
                raise ValueError("Frequency-assisted steps and window extents must be finite and positive")
            if not self.coarse_step_um >= self.medium_step_um >= self.fine_step_um:
                raise ValueError("Require coarse >= medium >= fine step")
        return self


def default_20x_contrast_af_settings() -> ContrastAFSettings:
    """Provisional manual-scan starting point for a 20× objective.

    The window is deliberately narrow and still clips to stage limits. Pixel
    encoding and specimen-dependent thresholds must be checked on the rig.
    """
    return ContrastAFSettings(
        method="frequency_assisted",
        coarse_step_um=10, medium_step_um=2, fine_step_um=1,
        window_below_um=30, window_above_um=30,
        energy_threshold=0.5, energy_margin=0,
        sensor_full_scale=65535,
        max_frames=100, max_moves=130, max_time_s=120,
        max_exposure_ms=10000, verification_tolerance=0.25,
        dense_fallback=True,
    )


class AcquisitionContrastAFOverride(BaseModel):
    """Accepted acquisition-only AF values, bound to one source preset."""

    state_name: str
    settings: ContrastAFSettings
    metric: Literal["LAPE", "GLVA", "TENENGRAD"]
    legacy_step_um: float = Field(gt=0)
    legacy_count: int = Field(ge=1)
    crop_width: int = Field(ge=1)
    crop_height: int = Field(ge=1)
    failure_policy: Literal["stop", "continue_restored"] = "stop"

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def check_override(self):
        if not self.state_name.strip():
            raise ValueError("An autofocus observation state is required")
        if not math.isfinite(self.legacy_step_um):
            raise ValueError("legacy_step_um must be finite")
        return self
