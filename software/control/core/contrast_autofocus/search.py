"""Bounded, synchronous stage-independent frequency-assisted focus search.

Positions and distances in this module are micrometres. The caller resolves
the intersection of user and physical travel limits before entering.
"""

from dataclasses import dataclass, field
import math
import time
from typing import Callable, Literal

import numpy as np
from control._sdk_watchdog import CameraTimeoutError

from control.models.contrast_autofocus import ContrastAFSettings
from .metrics import usable_image


class SearchStopped(Exception):
    def __init__(self, status, message, *, actual_z_um=None, frame_attempted=False):
        super().__init__(message)
        self.status = status
        self.actual_z_um = actual_z_um
        self.frame_attempted = frame_attempted


class StageFault(Exception):
    """Motion or position readback failed; compensating motion is unsafe."""


class CaptureFailure(Exception):
    """A trigger, readiness check, or frame wait failed after known motion."""

    def __init__(self, message, *, actual_z_um=None, frame_attempted=False):
        super().__init__(message)
        self.actual_z_um = actual_z_um
        self.frame_attempted = frame_attempted


class CaptureCancelled(InterruptedError):
    def __init__(self, actual_z_um):
        super().__init__("Autofocus cancelled during capture")
        self.actual_z_um = actual_z_um


@dataclass(frozen=True)
class FocusSample:
    image: np.ndarray
    actual_z_um: float


@dataclass
class SearchResult:
    status: Literal["success", "cancelled", "no_texture", "ambiguous_peak", "no_bracket",
                    "range_exhausted", "capture_failed", "hardware_failed"]
    starting_z_um: float
    final_z_um: float | None = None
    accepted_z_um: float | None = None
    best_measured_z_um: float | None = None
    best_score: float | None = None
    verification_score: float | None = None
    verification_passed: bool = False
    settings: dict = field(default_factory=dict)
    metric: str = ""
    logical_mode: str = "software_trigger"
    trigger_route: str = ""
    frames: int = 0
    moves: int = 0
    reversals: int = 0
    exposure_ms: float = 0
    elapsed_s: float = 0
    fallback_used: bool = False
    trace: list = field(default_factory=list)
    error: str | None = None
    cleanup_errors: list[str] = field(default_factory=list)


def run_search(
    *, capture_at: Callable[[float], FocusSample], move_to: Callable[[float], float],
    settings: ContrastAFSettings, sharpness: Callable, energy: Callable,
    cancelled: Callable[[], bool], starting_z_um: float, lower_z_um: float,
    upper_z_um: float, increment_um: float, exposure_ms: float = 0,
    metric: str = "", trigger_route: str = "", restore_to: Callable[[float], float] | None = None,
) -> SearchResult:
    """Search a preflighted physical interval; return typed operational failures.

    `capture_at` moves and captures one frame, returning owned image and Z
    readback. `move_to` performs a final or rollback move and returns readback.
    Both adapters must enforce physical bounds independently.
    """
    if settings.method != "frequency_assisted":
        raise ValueError("frequency_assisted settings required")
    numbers = (starting_z_um, lower_z_um, upper_z_um, increment_um, exposure_ms)
    if not all(math.isfinite(v) for v in numbers) or increment_um <= 0 or exposure_ms < 0:
        raise ValueError("Nonfinite search position, increment, or exposure")
    if lower_z_um >= upper_z_um or not lower_z_um <= starting_z_um <= upper_z_um:
        raise ValueError("Starting Z must lie in the nonempty search interval")
    if min(settings.fine_step_um, settings.medium_step_um, settings.coarse_step_um) < increment_um:
        raise ValueError("A requested step is smaller than one stage increment")
    origin = lower_z_um
    last_tick = math.floor((upper_z_um - origin) / increment_um)
    if last_tick < 2 or origin + last_tick * increment_um > upper_z_um + 1e-8:
        raise ValueError("Search interval cannot provide two-sided samples")
    tick_steps = [round(s / increment_um) for s in (settings.coarse_step_um, settings.medium_step_um, settings.fine_step_um)]
    if any(t < 1 for t in tick_steps):
        raise ValueError("Steps collapse after quantization")
    start_time = time.monotonic()
    result = SearchResult("no_bracket", starting_z_um, settings=settings.model_dump(mode="json"), metric=metric,
                          trigger_route=trigger_route)
    result.settings.update({
        "lower_z_um": lower_z_um, "upper_z_um": origin + last_tick * increment_um,
        "stage_increment_um": increment_um,
        "effective_steps_um": [t * increment_um for t in tick_steps],
    })
    current = starting_z_um
    previous_direction = 0
    stage_healthy = True
    best = None

    def check(moving=False, exposing=False):
        if cancelled():
            raise SearchStopped("cancelled", "Autofocus cancelled")
        if time.monotonic() - start_time >= settings.max_time_s:
            raise SearchStopped("range_exhausted", "Time budget exhausted")
        if moving and result.moves >= settings.max_moves:
            raise SearchStopped("range_exhausted", "Move budget exhausted")
        if exposing and (result.frames >= settings.max_frames or
                         result.exposure_ms + exposure_ms > settings.max_exposure_ms):
            raise SearchStopped("range_exhausted", "Frame or exposure budget exhausted")

    def target(tick):
        if not 0 <= tick <= last_tick:
            raise SearchStopped("range_exhausted", "Requested move outside search interval")
        return origin + tick * increment_um

    def record_move(actual, requested):
        nonlocal current, previous_direction, stage_healthy
        if not math.isfinite(actual) or not lower_z_um - increment_um / 2 <= actual <= upper_z_um + increment_um / 2:
            stage_healthy = False
            raise StageFault("Stage readback outside search interval")
        direction = 1 if actual > current else -1 if actual < current else 0
        if direction and previous_direction and direction != previous_direction:
            result.reversals += 1
        previous_direction = direction or previous_direction
        current = actual
        result.moves += int(direction != 0)
        result.trace.append({"phase": "move", "requested_z_um": requested, "actual_z_um": actual})

    def sample(tick, phase):
        nonlocal stage_healthy, best
        requested = target(tick)
        check(moving=True, exposing=True)
        try:
            value = capture_at(requested)
        except SearchStopped as exc:
            if exc.actual_z_um is not None:
                record_move(float(exc.actual_z_um), requested)
            if exc.frame_attempted:
                result.frames += 1
                result.exposure_ms += exposure_ms
            raise
        except CaptureCancelled as exc:
            record_move(float(exc.actual_z_um), requested)
            raise SearchStopped("cancelled", str(exc)) from exc
        except InterruptedError as exc:
            raise SearchStopped("cancelled", str(exc)) from exc
        except CaptureFailure as exc:
            if exc.actual_z_um is not None:
                record_move(float(exc.actual_z_um), requested)
            if exc.frame_attempted:
                result.frames += 1
                result.exposure_ms += exposure_ms
            raise SearchStopped("capture_failed", str(exc)) from exc
        except (TimeoutError, OSError, CameraTimeoutError) as exc:
            raise SearchStopped("capture_failed", str(exc)) from exc
        except StageFault:
            stage_healthy = False  # movement may have failed; never make a blind rollback
            raise
        actual = float(value.actual_z_um)
        record_move(actual, requested)
        # A capture may move without producing a frame; its adapter must either
        # return a frame/readback or raise a typed operational error.
        result.frames += 1
        result.exposure_ms += exposure_ms
        if abs(actual - requested) > increment_um / 2 + 1e-6:
            raise StageFault("Stage did not reach requested Z")
        try:
            usable = usable_image(value.image)
        except ValueError:
            usable = False
        if not usable:
            score = None
        else:
            score = float(sharpness(value.image))
            if not math.isfinite(score) or score < 0:
                score = None
        entry = {"phase": phase, "requested_z_um": requested, "actual_z_um": actual,
                 "score": score, "reason": "sampled" if score is not None else "unusable_image_or_metric"}
        result.trace.append(entry)
        if phase != "verify" and score is not None and (best is None or score > best[1]):
            best = (actual, score)
            result.best_measured_z_um, result.best_score = best
        return tick, actual, score, value.image

    def positions(lo, hi, step, descending=False):
        seq = list(range(lo, hi + 1, step))
        if seq[-1] != hi:
            seq.append(hi)
        return list(reversed(seq)) if descending else seq

    def interior_maxima(samples):
        return [i for i in range(1, len(samples) - 1)
                if all(s[2] is not None for s in samples[i - 1:i + 2])
                and samples[i][2] > samples[i - 1][2] and samples[i][2] > samples[i + 1][2]]

    try:
        coarse = []
        accepted = []
        previous = middle = None
        for tick in positions(0, last_tick, tick_steps[0]):
            current_sample = sample(tick, "coarse")
            coarse.append(current_sample[:3])  # scalar trace only
            if previous is not None and middle is not None and all(
                s[2] is not None for s in (previous, middle, current_sample)
            ) and middle[2] > previous[2] and middle[2] > current_sample[2]:
                try:
                    eh, en = float(energy(middle[3])), float(energy(current_sample[3]))
                except ValueError:
                    eh = en = float("nan")
                if not all(math.isfinite(v) and v >= 0 for v in (eh, en)):
                    decision = "invalid_energy"
                elif eh > en + settings.energy_margin:
                    decision = "accepted"
                    accepted.append(len(coarse) - 2)
                elif abs(eh - en) <= settings.energy_margin:
                    decision = "tie"
                else:
                    decision = "rejected"
                result.trace.append({"phase": "candidate", "requested_z_um": middle[1],
                                     "actual_z_um": middle[1], "score": middle[2],
                                     "energy_here": eh, "energy_next": en, "decision": decision})
            previous, middle = middle, current_sample
        good = [s for s in coarse if s[2] is not None]
        if not good:
            raise SearchStopped("no_texture", "No usable textured frames")
        if len(accepted) > 1:
            raise SearchStopped("ambiguous_peak", "Multiple accepted coarse peaks")
        if accepted:
            idx = accepted[0]
            left, right = coarse[idx - 1][0], coarse[idx + 1][0]
            # Half-coarse backtrack, then reverse-medium and forward-fine.
            half = min(right, coarse[idx][0] + max(1, tick_steps[0] // 2))
            medium = [sample(t, "medium")[:3] for t in positions(left, half, tick_steps[1], descending=True)]
            medium_valid = [s for s in medium if s[2] is not None]
            if not medium_valid:
                raise SearchStopped("no_texture", "No usable medium frames")
            medium_best = max(medium_valid, key=lambda s: s[2])
            fine_left = max(left, medium_best[0] - tick_steps[1])
            fine_right = min(right, medium_best[0] + tick_steps[1])
            fine = [sample(t, "fine")[:3] for t in positions(fine_left, fine_right, tick_steps[2])]
        else:
            if (coarse[-1][2] is not None and coarse[-2][2] is not None
                    and coarse[-1][2] > coarse[-2][2]
                    and coarse[-1][2] >= max(s[2] for s in good)):
                raise SearchStopped("range_exhausted", "Sharpness still rising at upper Z bound")
            if not settings.dense_fallback:
                status = "range_exhausted" if good[-1][2] >= max(s[2] for s in good) else "no_bracket"
                raise SearchStopped(status, "No accepted interior coarse peak")
            result.fallback_used = True
            fine = [sample(t, "fallback")[:3] for t in positions(0, last_tick, tick_steps[2])]
        peaks = interior_maxima(fine)
        if not peaks:
            status = "range_exhausted" if fine[-1][2] is not None and fine[-1][2] == max(
                (s[2] for s in fine if s[2] is not None), default=-1) else "no_bracket"
            raise SearchStopped(status, "No two-sided fine peak")
        if len(peaks) > 1:
            raise SearchStopped("ambiguous_peak", "Competing fine peaks")
        chosen = fine[peaks[0]]
        check(moving=True)
        requested = target(chosen[0])
        try:
            actual = float(move_to(requested))
        except StageFault:
            stage_healthy = False
            raise
        record_move(actual, requested)
        if abs(actual - chosen[1]) > increment_um / 2 + 1e-6:
            raise StageFault("Final stage readback differs from sampled peak")
        verified = sample(chosen[0], "verify")
        result.verification_score = verified[2]
        if abs(verified[1] - chosen[1]) > increment_um / 2 + 1e-6:
            raise StageFault("Verification readback differs from sampled peak")
        if verified[2] is None or abs(verified[2] - chosen[2]) > settings.verification_tolerance * max(chosen[2], 1e-12):
            raise SearchStopped("ambiguous_peak", "Verification score differs from sampled peak")
        # Metric calculation and image validation also consume the time budget.
        # A frame received just before the deadline must not certify focus after it.
        check()
        result.status = "success"
        result.accepted_z_um = chosen[1]
        result.verification_passed = True
    except SearchStopped as exc:
        result.status, result.error = exc.status, str(exc)
        result.trace.append({"phase": "stop", "reason": str(exc), "status": exc.status})
    except StageFault as exc:
        stage_healthy = False
        result.status = "hardware_failed"
        result.error = str(exc)
        result.trace.append({"phase": "stop", "reason": str(exc), "status": result.status})
    finally:
        if result.status != "success" and stage_healthy and lower_z_um <= starting_z_um <= upper_z_um:
            try:
                restored = float((restore_to or move_to)(starting_z_um))
                record_move(restored, starting_z_um)
            except Exception as exc:
                result.cleanup_errors.append(str(exc))
        result.final_z_um = current
        result.elapsed_s = time.monotonic() - start_time
    return result
