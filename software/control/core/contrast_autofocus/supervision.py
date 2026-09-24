"""Pure cadence and conservative image-quality decisions for laser supervision."""

from collections import deque
from dataclasses import dataclass, field
import math
import os
import statistics
import time

import numpy as np

from control.core.contrast_autofocus.metrics import grayscale
from control.models.contrast_autofocus import ContrastSupervisionPolicy


def require_new_supervised_run(experiment_path, policy):
    """A journal from a prior run cannot be paired with fresh in-memory maps."""
    if policy.mode != "off" and os.path.exists(os.path.join(experiment_path, "contrast_supervision.jsonl")):
        raise RuntimeError("Resuming a supervised acquisition is unsupported; start a new acquisition")


@dataclass(frozen=True)
class QualityObservation:
    score: float | None
    reason: str
    brightness: float
    saturation_fraction: float
    tile_coverage: float
    valid_fraction: float
    noise: float
    tile_contrast_median: float = 0
    tile_contrast_mad: float = 0


def inspect_image(image, score, *, sensor_full_scale=None, policy=None):
    """Reject unsafe comparisons using version-1 4×4 tile QC and policy thresholds."""
    policy = policy or ContrastSupervisionPolicy()
    raw = np.asarray(image)
    if not raw.size:
        return QualityObservation(None, "empty", 0, 0, 0, 0, 0)
    valid_fraction = float(np.mean(np.isfinite(raw)))
    if valid_fraction < 1:
        return QualityObservation(None, "invalid_pixels", 0, 0, 0, valid_fraction, 0)
    x = grayscale(raw)
    limit = float(sensor_full_scale or np.iinfo(image.dtype).max) if np.issubdtype(image.dtype, np.integer) else float(sensor_full_scale or 1)
    if np.issubdtype(raw.dtype, np.integer) and limit > np.iinfo(raw.dtype).max * 1.01:
        return QualityObservation(None, "scale_mismatch", float(np.median(x)), 0, 0,
                                  valid_fraction, 0)
    brightness = float(np.median(x))
    saturation = float(np.mean(x >= 0.99 * limit))
    tiles = [t for row in np.array_split(x, 4, axis=0) for t in np.array_split(row, 4, axis=1)]
    contrasts = [float(np.ptp(t)) for t in tiles]
    coverage = sum(value >= max(policy.min_absolute_signal_adu,
                                policy.min_tile_contrast_fraction * limit)
                   for value in contrasts) / len(contrasts)
    contrast_center = statistics.median(contrasts)
    contrast_mad = statistics.median(abs(value - contrast_center) for value in contrasts)
    noise = float(np.median(np.abs(x - brightness)))
    reason = "good"
    if not math.isfinite(score) or score <= 0:
        reason = "invalid_sharpness"
    elif saturation > policy.max_saturation_fraction:
        reason = "saturated"
    elif brightness < max(policy.min_absolute_signal_adu, policy.min_brightness_fraction * limit):
        reason = "dim"
    elif coverage < policy.min_tile_coverage:
        reason = "low_texture"
    return QualityObservation(float(score) if reason == "good" else None, reason,
                              brightness, saturation, coverage, valid_fraction, noise,
                              contrast_center, contrast_mad)


@dataclass
class TrustedHistory:
    samples: deque = field(default_factory=lambda: deque(maxlen=64))
    anchors: dict = field(default_factory=dict)
    reference_log_score: float | None = None
    bad_streak: int = 0
    revision: int = 0

    def seed(self, field, observation, timestamp=None):
        if observation.score is None:
            raise ValueError("Trusted focus seed requires a usable image")
        row = (field, math.log(observation.score), time.time() if timestamp is None else timestamp,
               observation.brightness, observation.noise)
        if self.reference_log_score is None:
            self.reference_log_score = row[1]
        self.samples.append(row)
        # Only an independently verified seed can establish a focus anchor.
        # Ordinary good checks must never move it down over time.
        if field in self.anchors or len(self.anchors) < self.samples.maxlen:
            self.anchors[field] = row

    def classify(self, field, observation, policy, timestamp=None):
        now = time.time() if timestamp is None else timestamp
        if observation.score is None:
            self.bad_streak = 0
            return "unknown", observation.reason
        supported = [row for row in self.anchors.values()
                     if 0 <= now - row[2] <= policy.max_baseline_age_s]
        if len({row[0] for row in supported}) < policy.min_baseline_fields:
            self.bad_streak = 0
            return "unknown", ("baseline_expired" if self.anchors and not supported
                               else "insufficient_trusted_baseline")
        brightness = statistics.median(row[3] for row in supported)
        noise = statistics.median(row[4] for row in supported)
        if not policy.min_brightness_ratio <= observation.brightness / max(brightness, 1e-9) <= policy.max_brightness_ratio:
            self.bad_streak = 0
            return "unknown", "brightness_drift"
        if observation.noise > max(policy.max_noise_ratio * noise, policy.min_absolute_noise_adu):
            self.bad_streak = 0
            return "unknown", "noise_drift"
        matching = [row for row in supported if row[0] == field]
        center = statistics.median(row[1] for row in (matching or supported))
        current = math.log(observation.score)
        # The relative threshold is provisional and deliberately never learns
        # from an alarm. A repeated field alone does not increase support.
        poor = current < center + math.log1p(-policy.relative_drop)
        if poor:
            self.bad_streak += 1
            return ("poor" if self.bad_streak >= policy.bad_checks_required else "suspect"), "sharpness_drop"
        self.bad_streak = 0
        return "good", "within_baseline"

    def extend_verified_basis(self, field, observation, policy):
        """Add a distinct laser-verified field after a contrast-verified seed."""
        now = time.time()
        supported = [row for row in self.anchors.values()
                     if 0 <= now - row[2] <= policy.max_baseline_age_s]
        if not supported or observation.score is None or field in self.anchors or len(self.anchors) >= self.samples.maxlen:
            return False
        center = statistics.median(row[1] for row in supported)
        brightness = statistics.median(row[3] for row in supported)
        noise = statistics.median(row[4] for row in supported)
        if (not policy.min_brightness_ratio <= observation.brightness / max(brightness, 1e-9) <= policy.max_brightness_ratio or
                observation.noise > max(policy.max_noise_ratio * noise, policy.min_absolute_noise_adu)):
            return False
        # New fields establish support, but cannot ratchet the reference down
        # one field at a time. Use the original verified score as the floor.
        if (math.log(observation.score) < self.reference_log_score + math.log1p(-policy.max_good_learning_drop_fraction) or
                math.log(observation.score) > center - math.log1p(-policy.relative_drop)):
            return False
        self.seed(field, observation)
        return True

    def record_good(self, field, observation, policy=None):
        """Keep good history bounded without following gradual score decline."""
        if observation.score is None:
            return False
        policy = policy or ContrastSupervisionPolicy()
        supported = [row for row in self.anchors.values()
                     if 0 <= time.time() - row[2] <= policy.max_baseline_age_s]
        if not supported:
            return False
        matching = [row for row in supported if row[0] == field]
        center = statistics.median(row[1] for row in (matching or supported))
        if math.log(observation.score) < center + math.log1p(-policy.max_good_learning_drop_fraction):
            return False
        self.samples.append((field, math.log(observation.score), time.time(),
                             observation.brightness, observation.noise))
        return True

    @property
    def support(self):
        return len(self.anchors)

    def statistics(self, now, max_age_s):
        current = [row for row in self.anchors.values() if 0 <= now - row[2] <= max_age_s]
        values = [row[1] for row in current]
        if not values:
            return {"log_center": None, "log_mad": None, "support": 0}
        center = statistics.median(values)
        return {"log_center": center,
                "log_mad": statistics.median(abs(v - center) for v in values),
                "support": len({row[0] for row in current})}


@dataclass
class VisitCadence:
    last_scope: str | None = None
    last_timepoint: int | None = None
    completed: int = 0
    last_check_completed: int = 0
    checked_entry: bool = False

    def due(self, scope, timepoint, policy):
        if scope != self.last_scope or timepoint != self.last_timepoint:
            self.last_scope, self.last_timepoint = scope, timepoint
            self.completed = self.last_check_completed = 0
            self.checked_entry = False
        entry = policy.check_on_entry and not self.checked_entry
        periodic = bool(policy.every_n_completed_fovs and self.completed > 0 and
                        self.completed - self.last_check_completed >= policy.every_n_completed_fovs)
        if entry or periodic:
            self.checked_entry = True
            self.last_check_completed = self.completed
            return True
        return False

    def complete(self):
        self.completed += 1
