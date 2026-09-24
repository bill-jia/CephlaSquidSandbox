"""Synchronous contrast search shared by manual and acquisition owners.

The owner supplies motion and capture.  This module never changes camera
callbacks, observation state, or acquisition bookkeeping.
"""

import time
import math
from typing import Callable

import numpy as np

from control.core.contrast_autofocus.search import FocusSample, SearchResult, run_search
from control.models.contrast_autofocus import ContrastAFSettings


def run_legacy_scan(
    *, capture_at: Callable[[float], FocusSample], move_to: Callable[[float], float],
    restore_to: Callable[[float], float], score: Callable[[np.ndarray], float],
    cancelled: Callable[[], bool], starting_z_um: float, step_um: float,
    count: int, stop_threshold: float, metric: str, settings: dict,
    trigger_route: str = "", deadline_s: float | None = None,
    max_frames: int | None = None, max_moves: int | None = None,
    max_exposure_ms: float | None = None, exposure_ms: float = 0,
) -> SearchResult:
    """Preserve the historical low-to-high sweep and best-sample selection.

    The first sample is one step above ``start - step*round(count/2)``.
    Failure, including a missing frame, restores the starting plane when
    motion remains healthy.  A successful scan makes no Phase 1 threshold or
    verification claim.
    """
    if count < 1 or step_um <= 0:
        raise ValueError("Legacy autofocus requires a positive count and step")
    if not math.isfinite(exposure_ms) or exposure_ms < 0:
        raise ValueError("Legacy autofocus requires a finite nonnegative exposure")
    began = time.monotonic()
    result = SearchResult("no_bracket", starting_z_um, settings=dict(settings),
                          metric=metric, trigger_route=trigger_route)
    result.settings.update({"effective_exposure_ms": exposure_ms,
                            "restoration_moves_exempt_from_max_moves": True})
    best_score = 0.0
    best_z = None
    current = starting_z_um
    motion_healthy = True
    try:
        def check(*, moving=False, exposing=False):
            if cancelled():
                raise InterruptedError("Autofocus cancelled")
            if deadline_s is not None and time.monotonic() - began >= deadline_s:
                raise TimeoutError("Autofocus time budget exhausted")
            if moving and max_moves is not None and result.moves >= max_moves:
                raise TimeoutError("Move budget exhausted")
            if exposing and (max_frames is not None and result.frames >= max_frames or
                             max_exposure_ms is not None and result.exposure_ms + exposure_ms > max_exposure_ms):
                raise TimeoutError("Frame or exposure budget exhausted")

        check(moving=True)
        current = move_to(starting_z_um - step_um * round(count / 2))
        result.moves += 1
        for _ in range(count):
            check(moving=True, exposing=True)
            target = current + step_um
            sample = capture_at(target)
            result.moves += 1
            result.frames += 1
            result.exposure_ms += exposure_ms
            current = sample.actual_z_um if sample is not None else target
            check()
            if sample is None or sample.image is None:
                raise RuntimeError("Autofocus received no camera frame")
            value = float(score(sample.image))
            if not np.isfinite(value):
                raise RuntimeError("Autofocus score is not finite")
            result.trace.append({"z_um": current, "score": value})
            if best_z is None or value > best_score:
                best_z, best_score = current, value
            if value < best_score * stop_threshold:
                break
        if best_z is None:
            raise RuntimeError("Autofocus collected no samples")
        check(moving=True)
        # Match the legacy directional approach: return to the sweep's lower
        # edge before approaching the selected plane from below.
        current = move_to(starting_z_um - step_um * round(count / 2))
        result.moves += 1
        check(moving=True)
        current = move_to(best_z)
        result.moves += 1
        check()
        result.status = "success"
        result.accepted_z_um = current
        result.best_measured_z_um = best_z
        result.best_score = best_score
    except InterruptedError as exc:
        result.status, result.error = "cancelled", str(exc)
    except TimeoutError as exc:
        result.status, result.error = "range_exhausted", str(exc)
    except Exception as exc:
        result.status, result.error = "capture_failed", str(exc)
        # Move adapters may raise on an unsafe stage.  A blind rollback then
        # could make the fault worse; the owner must stop the acquisition.
        from control.core.contrast_autofocus.search import StageFault
        if isinstance(exc, StageFault):
            motion_healthy = False
            result.status = "hardware_failed"
    finally:
        if result.status != "success" and motion_healthy:
            try:
                current = restore_to(starting_z_um)
                result.moves += 1
            except Exception as exc:
                result.cleanup_errors.append(str(exc))
                result.status = "hardware_failed"
        result.final_z_um = current
        result.elapsed_s = time.monotonic() - began
    return result


def run_contrast_search(*, settings: ContrastAFSettings, legacy_options: dict,
                        capture_at: Callable[[float], FocusSample],
                        move_to: Callable[[float], float],
                        restore_to: Callable[[float], float],
                        score: Callable[[np.ndarray], float],
                        cancelled: Callable[[], bool], starting_z_um: float,
                        lower_z_um: float, upper_z_um: float, increment_um: float,
                        energy: Callable[[np.ndarray], float] | None = None,
                        exposure_ms: float = 0, metric: str = "",
                        trigger_route: str = "") -> SearchResult:
    """Dispatch either search against one owner's capture and axis adapter."""
    if settings.method == "legacy":
        return run_legacy_scan(
            capture_at=capture_at, move_to=move_to, restore_to=restore_to,
            score=score, cancelled=cancelled, starting_z_um=starting_z_um,
            step_um=legacy_options["step_um"], count=legacy_options["count"],
            stop_threshold=legacy_options["stop_threshold"], metric=metric,
            settings={**settings.model_dump(mode="json"), **legacy_options},
            trigger_route=trigger_route, deadline_s=settings.max_time_s,
            max_frames=settings.max_frames, max_moves=settings.max_moves,
            max_exposure_ms=settings.max_exposure_ms, exposure_ms=exposure_ms)
    if energy is None:
        raise ValueError("Frequency-assisted search requires an energy metric")
    return run_search(
        capture_at=capture_at, move_to=move_to, restore_to=restore_to,
        settings=settings, sharpness=score, energy=energy, cancelled=cancelled,
        starting_z_um=starting_z_um, lower_z_um=lower_z_um,
        upper_z_um=upper_z_um, increment_um=increment_um,
        exposure_ms=exposure_ms, metric=metric, trigger_route=trigger_route)
