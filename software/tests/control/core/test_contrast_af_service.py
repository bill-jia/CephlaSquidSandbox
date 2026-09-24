"""Deterministic contract checks for the synchronous legacy search."""

import numpy as np
import pytest

from control.core.contrast_autofocus.search import FocusSample
from control.core.contrast_autofocus.service import run_legacy_scan, run_contrast_search
from control.models.contrast_autofocus import ContrastAFSettings


def _scan(scores, *, cancelled=lambda: False):
    z = 100.0
    moves = []
    samples = iter(scores)

    def move(target):
        nonlocal z
        z = target
        moves.append(target)
        return z

    def capture(target):
        move(target)
        value = next(samples)
        return FocusSample(None if value is None else np.array([[value]]), z)

    result = run_legacy_scan(
        capture_at=capture, move_to=move, restore_to=move,
        score=lambda image: image[0, 0], cancelled=cancelled,
        starting_z_um=100, step_um=2, count=3, stop_threshold=0.5,
        metric="LAPE", settings={"method": "legacy"})
    return result, moves


def test_legacy_uses_existing_sweep_and_best_sample():
    result, moves = _scan([1, 5, 2])
    assert result.status == "success"
    assert result.accepted_z_um == 100
    assert result.frames == 3
    assert moves == [96, 98, 100, 102, 96, 100]
    assert result.verification_passed is False


def test_missing_frame_cannot_report_success_and_restores_start():
    result, moves = _scan([1, None])
    assert result.status == "capture_failed"
    assert result.final_z_um == 100
    assert result.accepted_z_um is None
    assert moves[-1] == 100


def test_cancel_before_motion_has_no_capture():
    result, moves = _scan([1, 2, 3], cancelled=lambda: True)
    assert result.status == "cancelled"
    assert result.frames == 0
    assert moves == [100]


@pytest.mark.parametrize("failure", ["cancel", "deadline"])
def test_final_move_failure_restores_start_without_accepting(monkeypatch, failure):
    from control.core.contrast_autofocus import service
    clock = [0.0]
    cancelled = [False]
    z = [100.0]
    monkeypatch.setattr(service.time, "monotonic", lambda: clock[0])

    def move(target):
        z[0] = target
        if target == 102 and clock[0] == 0 and calls[0] >= 3:
            if failure == "cancel":
                cancelled[0] = True
            else:
                clock[0] = 11
        calls[0] += 1
        return z[0]

    calls = [0]
    result = run_legacy_scan(
        capture_at=lambda target: FocusSample(np.array([[1]]), move(target)),
        move_to=move, restore_to=move, score=lambda image: image[0, 0],
        cancelled=lambda: cancelled[0], starting_z_um=100, step_um=2,
        count=1, stop_threshold=0, metric="LAPE", settings={}, deadline_s=10)
    assert result.status == ("cancelled" if failure == "cancel" else "range_exhausted")
    assert result.accepted_z_um is None
    assert result.final_z_um == z[0] == 100
    assert result.cleanup_errors == []


@pytest.mark.parametrize("limit,expected", [
    ({"max_frames": 1}, "Frame or exposure budget exhausted"),
    ({"max_moves": 2}, "Move budget exhausted"),
    ({"max_exposure_ms": 5}, "Frame or exposure budget exhausted"),
])
def test_legacy_budgets_stop_before_disallowed_action(limit, expected):
    z = [100.0]
    captures = []
    def move(target):
        z[0] = target
        return target
    def capture(target):
        captures.append(target)
        return FocusSample(np.array([[len(captures)]]), move(target))
    result = run_legacy_scan(
        capture_at=capture, move_to=move, restore_to=move,
        score=lambda image: image[0, 0], cancelled=lambda: False,
        starting_z_um=100, step_um=2, count=3, stop_threshold=0,
        metric="LAPE", settings={}, exposure_ms=5, **limit)
    assert result.status == "range_exhausted"
    assert result.error == expected
    assert result.final_z_um == 100
    assert result.accepted_z_um is None
    assert result.frames == len(captures)
    assert result.exposure_ms == result.frames * 5
    assert result.moves == 1 + len(captures) + 1  # initial, samples, rollback


def test_legacy_dispatch_enforces_popup_budgets_and_records_effective_exposure():
    z = [100.0]
    captures = []
    def move(target):
        z[0] = target
        return target
    def capture(target):
        captures.append(target)
        return FocusSample(np.array([[1]]), move(target))
    settings = ContrastAFSettings(method="legacy", max_frames=5, max_moves=20,
                                  max_exposure_ms=15)
    result = run_contrast_search(
        settings=settings, legacy_options={"step_um": 1, "count": 10,
                                          "stop_threshold": 0},
        capture_at=capture, move_to=move, restore_to=move,
        score=lambda image: float(image[0, 0]), cancelled=lambda: False,
        starting_z_um=100, lower_z_um=0, upper_z_um=200,
        increment_um=1, exposure_ms=10)
    assert result.status == "range_exhausted"
    assert result.frames == 1
    assert result.exposure_ms == 10
    assert result.settings["effective_exposure_ms"] == 10
    assert result.settings["restoration_moves_exempt_from_max_moves"] is True
    assert result.final_z_um == 100
