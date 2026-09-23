import numpy as np
import pytest

from control.core.contrast_autofocus.metrics import energy_factor
from control.core.contrast_autofocus.search import CaptureFailure, FocusSample, StageFault, run_search
from control.models.contrast_autofocus import ContrastAFSettings


def settings(**changes):
    values = dict(method="frequency_assisted", coarse_step_um=10, medium_step_um=2,
                  fine_step_um=1, window_below_um=20, window_above_um=20,
                  sensor_full_scale=255, max_frames=100, max_moves=130)
    values.update(changes)
    return ContrastAFSettings(**values)


def fixture(score=lambda z: 100 - (z - 20) ** 2, energy=lambda z: 40 - z,
            start=20, config=None, cancel=lambda: False, bad_verification=False,
            fail_at=None, stage_fault_at=None, on_measure=None):
    position = [start]
    commanded = []
    visits = {}
    measurements = [0]

    def move(z):
        assert 0 <= z <= 40
        commanded.append(z)
        position[0] = z
        return z

    def capture(z):
        if z == stage_fault_at:
            raise StageFault("stage stalled")
        move(z)
        if z == fail_at:
            raise CaptureFailure("frame missing", actual_z_um=z, frame_attempted=True)
        visits[z] = visits.get(z, 0) + 1
        image = np.zeros((4, 4), dtype=np.float64)
        image[0, 0] = max(score(z), 0) * (0.1 if bad_verification and z == 20 and visits[z] >= 4 else 1)
        image[0, 1] = max(energy(z), 0)
        return FocusSample(image, z)

    def sharpness(image):
        measurements[0] += 1
        if on_measure is not None:
            on_measure(measurements[0])
        return image[0, 0]

    result = run_search(capture_at=capture, move_to=move, settings=config or settings(),
                        sharpness=sharpness, energy=lambda image: image[0, 1],
                        cancelled=cancel, starting_z_um=start, lower_z_um=0,
                        upper_z_um=40, increment_um=1, exposure_ms=1)
    return result, commanded, position[0]


def test_success_has_two_sided_peak_final_move_and_verification():
    result, commanded, final = fixture()
    assert result.status == "success"
    assert result.accepted_z_um == final == 20
    assert result.verification_passed
    assert result.trace[-1]["phase"] == "verify"
    assert any(item["phase"] == "medium" for item in result.trace)
    assert any(item["phase"] == "fine" for item in result.trace)
    assert all(0 <= z <= 40 for z in commanded)


@pytest.mark.parametrize("start", [0, 40])
def test_starts_on_either_side_of_focus(start):
    result, _, final = fixture(start=start)
    assert result.status == "success"
    assert final == 20


def test_monotonic_curve_is_range_exhausted_and_restored():
    result, _, final = fixture(score=lambda z: z, config=settings(dense_fallback=False))
    assert result.status == "range_exhausted"
    assert final == 20
    with_fallback, _, _ = fixture(score=lambda z: z)
    assert with_fallback.status == "range_exhausted"
    assert not with_fallback.fallback_used


def test_verification_budget_is_global():
    result, _, final = fixture(config=settings(max_frames=19))
    assert result.status == "range_exhausted"
    assert result.frames <= 19
    assert final == 20


def test_exposure_budget_is_global():
    result, _, _ = fixture(config=settings(max_exposure_ms=19))
    assert result.status == "range_exhausted"
    assert result.exposure_ms <= 19


def test_camera_failure_rolls_back_but_stage_fault_does_not():
    failure, commanded, final = fixture(fail_at=10)
    assert failure.status == "capture_failed"
    assert failure.frames == 2
    assert final == 20
    assert commanded[-1] == 20
    fault, commanded, final = fixture(stage_fault_at=10)
    assert fault.status == "hardware_failed"
    assert final == 0
    assert commanded == [0]


def test_tie_requires_fallback_or_fails():
    result, _, _ = fixture(energy=lambda z: 1, config=settings(dense_fallback=False))
    assert result.status == "no_bracket"
    assert any(t.get("decision") == "tie" for t in result.trace)


def test_false_prefocus_peak_is_rejected_by_E():
    score = lambda z: max(80 - 10 * abs(z - 10), 100 - 5 * abs(z - 30))
    energy = lambda z: z + 1 if z <= 20 else 100 - z
    result, _, final = fixture(score=score, energy=energy)
    assert result.status == "success"
    assert result.accepted_z_um == final == 30
    assert [t["decision"] for t in result.trace if t["phase"] == "candidate"] == ["rejected", "accepted"]


def test_two_accepted_coarse_peaks_are_ambiguous():
    score = lambda z: max(80 - 10 * abs(z - 10), 100 - 5 * abs(z - 30))
    result, _, final = fixture(score=score, energy=lambda z: 100 - z)
    assert result.status == "ambiguous_peak"
    assert result.accepted_z_um is None
    assert final == 20


def test_verification_disagreement_prevents_success():
    result, _, final = fixture(bad_verification=True)
    assert result.status == "ambiguous_peak"
    assert result.accepted_z_um is None
    assert not result.verification_passed
    assert final == 20


def test_slow_verification_cannot_pass_expired_deadline(monkeypatch):
    import control.core.contrast_autofocus.search as search_module

    clock = [0.0]
    monkeypatch.setattr(search_module.time, "monotonic", lambda: clock[0])
    result, commanded, final = fixture(
        start=30, config=settings(max_time_s=10),
        on_measure=lambda n: clock.__setitem__(0, 100.0) if n == 19 else None)
    assert result.status == "range_exhausted"
    assert result.accepted_z_um is None
    assert not result.verification_passed
    assert result.verification_score is not None
    assert result.elapsed_s == 100
    assert final == commanded[-1] == 30


def test_flat_frames_are_not_texture():
    result, _, _ = fixture(score=lambda z: 0, energy=lambda z: 0)
    assert result.status == "no_texture"


def test_cancel_before_motion():
    result, commanded, _ = fixture(cancel=lambda: True)
    assert result.status == "cancelled"
    assert commanded == [20]  # legal rollback to the starting plane


def test_E_matches_independent_fft_and_rejects_bad_input():
    image = np.array([[0, 2], [3, 4]], dtype=np.uint8)
    a = np.abs(np.fft.fft2(image / 255))
    l = np.log1p(a)
    expected = sum(a[((l - l.min()) / np.ptp(l)) >= 0.5])
    assert energy_factor(image, threshold=0.5, sensor_full_scale=255) == pytest.approx(expected)
    assert energy_factor(image * 2, threshold=0, sensor_full_scale=255) == pytest.approx(
        2 * energy_factor(image, threshold=0, sensor_full_scale=255))
    assert energy_factor(image, threshold=1, sensor_full_scale=255) > 0
    assert energy_factor(np.zeros((4, 4)), sensor_full_scale=255) == 0
    assert energy_factor(np.ones((4, 4)), sensor_full_scale=255) > 0  # DC; quality gate rejects it
    with pytest.raises(ValueError):
        energy_factor(image, sensor_full_scale=0)
    with pytest.raises(ValueError):
        energy_factor(np.array([[np.nan]]), sensor_full_scale=255)
