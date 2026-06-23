"""Tests for observation-state FOV/ROI consistency used by multipoint tiling."""

import pytest

from control.models.observation_state import ObservationState, CameraLiveSnapshot
from control.core.observation_state_service import (
    observation_state_fov_mm,
    observation_state_roi_report,
)


class _FakeConfig:
    crop_width = 4168
    crop_height = 4168


class _FakeCamera:
    """Minimal stand-in matching the bits observation_state_fov_mm reads."""

    _config = _FakeConfig()

    def get_pixel_size_unbinned_um(self):
        return 3.76

    def get_fov_size_mm(self):
        # Sensor-frame full FOV (used only as the no-ROI fallback).
        return (4168 * 3.76 / 1000, 4168 * 3.76 / 1000)


_FACTOR = 0.1  # 10x-like sample-frame scaling


def _state(name, roi_w, roi_h, bx=1, by=1):
    return ObservationState(
        name=name,
        camera_live=CameraLiveSnapshot(
            exposure_time_ms=10.0, roi_width=roi_w, roi_height=roi_h, binning_x=bx, binning_y=by
        ),
    )


def test_fov_from_roi():
    fov = observation_state_fov_mm(_state("a", 2200, 2200), _FakeCamera(), _FACTOR)
    assert fov == pytest.approx((0.8272, 0.8272))


def test_fov_clamped_to_configured_crop():
    # ROI larger than the configured crop -> saved frame is the crop, not the ROI.
    fov = observation_state_fov_mm(_state("b", 5000, 5000), _FakeCamera(), _FACTOR)
    expected = 4168 * 3.76 * _FACTOR / 1000
    assert fov == pytest.approx((expected, expected))


def test_fov_binning_cancels():
    # Same physical ROI expressed at binning 2 (half the binned pixels) -> same FOV.
    fov_b1 = observation_state_fov_mm(_state("a", 2200, 2200, bx=1, by=1), _FakeCamera(), _FACTOR)
    fov_b2 = observation_state_fov_mm(_state("a", 1100, 1100, bx=2, by=2), _FakeCamera(), _FACTOR)
    assert fov_b1 == pytest.approx(fov_b2)


def test_report_picks_largest_and_flags_mismatch():
    report = observation_state_roi_report(
        [("small", _state("small", 2200, 2200)), ("big", _state("big", 3000, 3000))],
        _FakeCamera(),
        _FACTOR,
    )
    assert report["largest_name"] == "big"
    assert report["mismatch"] is True
    assert report["mismatch_names"] == ["small"]
    # Tiling FOV is the largest ROI's FOV.
    assert report["tiling_fov_mm"] == pytest.approx((3000 * 3.76 * _FACTOR / 1000,) * 2)


def test_report_no_mismatch_when_all_equal():
    report = observation_state_roi_report(
        [("a", _state("a", 2200, 2200)), ("b", _state("b", 2200, 2200))],
        _FakeCamera(),
        _FACTOR,
    )
    assert report["mismatch"] is False
    assert report["mismatch_names"] == []


def test_report_empty_states():
    report = observation_state_roi_report([], _FakeCamera(), _FACTOR)
    assert report["mismatch"] is False
    assert report["tiling_fov_mm"] is None
