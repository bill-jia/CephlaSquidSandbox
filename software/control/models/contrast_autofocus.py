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


class ContrastSupervisionPolicy(BaseModel):
    """Versioned, opt-in run policy. Thresholds are provisional sample defaults."""

    version: Literal[1] = 1
    mode: Literal["off", "monitor", "quality"] = "off"
    check_on_entry: bool = True
    every_n_completed_fovs: int = Field(0, ge=0, le=10000)
    relative_drop: float = Field(0.35, gt=0, lt=1)
    min_baseline_fields: int = Field(3, ge=1)
    bad_checks_required: int = Field(2, ge=1)
    verify_baseline_once: bool = False
    operator_confirms_reference_focus: bool = False
    max_baseline_age_s: float = Field(3600, gt=0)
    max_laser_age_s: float = Field(5, gt=0)
    min_correlation: float = Field(0.5, ge=-1, le=1)
    max_residual_um: float = Field(2, gt=0)
    min_brightness_fraction: float = Field(0.005, ge=0, le=1)
    min_absolute_signal_adu: float = Field(3, ge=0)
    min_absolute_noise_adu: float = Field(1, ge=0)
    max_saturation_fraction: float = Field(0.02, ge=0, le=1)
    min_tile_coverage: float = Field(0.25, ge=0, le=1)
    min_tile_contrast_fraction: float = Field(0.005, ge=0, le=1)
    min_brightness_ratio: float = Field(0.7, gt=0, le=1)
    max_brightness_ratio: float = Field(1.3, ge=1)
    max_noise_ratio: float = Field(2.0, ge=1)
    min_peak_margin_fraction: float = Field(0.05, ge=0, le=1)
    max_good_learning_drop_fraction: float = Field(0.02, ge=0, le=1)
    max_correction_um: float = Field(20, gt=0)
    max_scan_travel_um: float = Field(60, gt=0)
    confirmation_spacing_um: float = Field(1, gt=0)
    max_attempts_per_region: int = Field(1, ge=0)
    cooldown_completed_fovs: int = Field(10, ge=0)
    max_frames_per_correction: int = Field(120, ge=1)
    max_frames_per_region: int = Field(250, ge=1)
    max_frames_per_run: int = Field(1000, ge=1)
    max_exposure_ms_per_correction: float = Field(10000, gt=0)
    max_exposure_ms_per_region: float = Field(30000, gt=0)
    max_exposure_ms_per_run: float = Field(120000, gt=0)
    max_laser_exposure_ms_per_correction: float = Field(10000, gt=0)
    max_laser_exposure_ms_per_region: float = Field(30000, gt=0)
    max_laser_exposure_ms_per_run: float = Field(120000, gt=0)
    max_time_s_per_correction: float = Field(120, gt=0)
    max_time_s_per_region: float = Field(600, gt=0)
    max_time_s_per_run: float = Field(3600, gt=0)
    nominal_plane_offset_um: float = 0
    failure_action: Literal["stop", "continue_restored"] = "stop"

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def migrate_removed_scheduled_correction(cls, value):
        if isinstance(value, dict):
            value = value.copy()
            value.pop("full_verification_every_n_checks", None)
            if value.get("mode") == "scheduled":
                value["mode"] = "off"
        return value

    @model_validator(mode="after")
    def validate_policy(self):
        for name in ("relative_drop", "max_baseline_age_s", "max_laser_age_s",
                     "min_correlation", "max_residual_um", "max_correction_um",
                     "confirmation_spacing_um", "max_scan_travel_um",
                     "min_brightness_fraction", "min_absolute_signal_adu", "min_absolute_noise_adu",
                     "max_saturation_fraction", "min_tile_coverage",
                     "min_tile_contrast_fraction", "min_brightness_ratio", "max_brightness_ratio",
                     "max_noise_ratio", "min_peak_margin_fraction", "max_good_learning_drop_fraction",
                     "max_exposure_ms_per_correction", "max_time_s_per_correction",
                     "max_exposure_ms_per_region", "max_exposure_ms_per_run",
                     "max_laser_exposure_ms_per_correction", "max_laser_exposure_ms_per_region",
                     "max_laser_exposure_ms_per_run",
                     "max_time_s_per_region", "max_time_s_per_run",
                     "nominal_plane_offset_um"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        if self.mode != "off" and not (self.check_on_entry or self.every_n_completed_fovs):
            raise ValueError("Supervision needs an entry or periodic check")
        if self.mode == "quality" and self.confirmation_spacing_um >= self.max_correction_um:
            raise ValueError("Confirmation spacing must be smaller than correction limit")
        if self.mode == "quality" and self.max_scan_travel_um < self.max_correction_um:
            raise ValueError("Scan travel must cover the maximum final correction")
        return self
