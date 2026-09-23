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
