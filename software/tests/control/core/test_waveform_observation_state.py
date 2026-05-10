"""Unit tests for the per-frame NIDAQ pulse waveform builder."""

from unittest.mock import MagicMock

import numpy as np
import pytest

from control.core.waveform_observation_state import (
    build_pulse_waveform_for_state,
    nidaq_lines_for_state,
)
from control.models.observation_state import (
    CameraSettings,
    IlluminatorState,
    IlluminatorTiming,
    ObservationState,
)


def _state_with(*illuminators: IlluminatorState, exposure_ms: float = 50.0) -> ObservationState:
    return ObservationState(
        name="test",
        camera_settings=CameraSettings(exposure_time_ms=exposure_ms, gain_mode=1.0),
        illuminator_states=list(illuminators),
    )


def _ic_with_lines(line_for_channel: dict[str, int | None]) -> MagicMock:
    """Build an IlluminationController stub that returns the configured DO line per channel name."""
    ic = MagicMock()
    ic.get_nidaq_do_line_for_channel.side_effect = lambda name: line_for_channel.get(name)
    return ic


def test_single_pulse_in_middle_of_exposure():
    state = _state_with(
        IlluminatorState(
            illumination_channel="Laser561",
            intensity=50.0,
            on=True,
            timing=IlluminatorTiming(offset_ms=24.5, duration_ms=1.0),
        ),
        exposure_ms=50.0,
    )
    ic = _ic_with_lines({"Laser561": 2})

    wf = build_pulse_waveform_for_state(state, ic, sample_rate_hz=100_000.0)
    assert list(wf.digital_output.keys()) == [2]
    pattern = wf.digital_output[2]
    assert pattern.dtype == bool
    assert pattern.size == 5000  # 50 ms × 100 kHz
    high = np.where(pattern)[0]
    # 24.5 ms × 100 kHz = sample 2450; 1.0 ms × 100 kHz = 100 samples
    assert high[0] == 2450
    assert high[-1] == 2549
    assert high.size == 100


def test_multiple_timed_illuminators_share_one_waveform():
    state = _state_with(
        IlluminatorState(
            illumination_channel="LaserA",
            intensity=50.0,
            on=True,
            timing=IlluminatorTiming(offset_ms=2.0, duration_ms=1.0),
        ),
        IlluminatorState(
            illumination_channel="LaserB",
            intensity=30.0,
            on=True,
            timing=IlluminatorTiming(offset_ms=8.0, duration_ms=1.0),
        ),
        # Untimed illuminator: must NOT appear in the waveform.
        IlluminatorState(illumination_channel="LaserC", intensity=10.0, on=True),
        exposure_ms=20.0,
    )
    ic = _ic_with_lines({"LaserA": 0, "LaserB": 5, "LaserC": 9})

    wf = build_pulse_waveform_for_state(state, ic, sample_rate_hz=10_000.0)
    assert set(wf.digital_output.keys()) == {0, 5}
    assert 9 not in wf.digital_output  # untimed channel skipped


def test_pulse_outside_exposure_raises():
    state = _state_with(
        IlluminatorState(
            illumination_channel="Laser561",
            intensity=50.0,
            on=True,
            timing=IlluminatorTiming(offset_ms=49.0, duration_ms=5.0),
        ),
        exposure_ms=50.0,
    )
    ic = _ic_with_lines({"Laser561": 2})

    with pytest.raises(ValueError, match="falls outside camera exposure"):
        build_pulse_waveform_for_state(state, ic, sample_rate_hz=100_000.0)


def test_no_nidaq_line_raises():
    state = _state_with(
        IlluminatorState(
            illumination_channel="LED matrix",
            intensity=50.0,
            on=True,
            timing=IlluminatorTiming(offset_ms=1.0, duration_ms=1.0),
        ),
    )
    ic = _ic_with_lines({"LED matrix": None})

    with pytest.raises(ValueError, match="no NIDAQ digital-output gating line"):
        build_pulse_waveform_for_state(state, ic, sample_rate_hz=100_000.0)


def test_no_timed_illuminators_raises():
    state = _state_with(
        IlluminatorState(illumination_channel="LaserA", intensity=50.0, on=True),
    )
    ic = _ic_with_lines({"LaserA": 0})

    with pytest.raises(ValueError, match="no active illuminators with timing"):
        build_pulse_waveform_for_state(state, ic, sample_rate_hz=100_000.0)


def test_two_timed_illuminators_on_same_line_collide():
    state = _state_with(
        IlluminatorState(
            illumination_channel="A",
            intensity=50.0,
            on=True,
            timing=IlluminatorTiming(offset_ms=1.0, duration_ms=1.0),
        ),
        IlluminatorState(
            illumination_channel="B",
            intensity=50.0,
            on=True,
            timing=IlluminatorTiming(offset_ms=5.0, duration_ms=1.0),
        ),
    )
    ic = _ic_with_lines({"A": 3, "B": 3})

    with pytest.raises(ValueError, match="share NIDAQ DO line"):
        build_pulse_waveform_for_state(state, ic, sample_rate_hz=100_000.0)


def test_nidaq_lines_for_state_returns_unique_line_indices():
    state = _state_with(
        IlluminatorState(
            illumination_channel="A",
            intensity=50.0,
            on=True,
            timing=IlluminatorTiming(offset_ms=1.0, duration_ms=1.0),
        ),
        IlluminatorState(
            illumination_channel="B",
            intensity=50.0,
            on=False,  # Inactive — must not appear
            timing=IlluminatorTiming(offset_ms=1.0, duration_ms=1.0),
        ),
        IlluminatorState(
            illumination_channel="C",
            intensity=50.0,
            on=True,
            timing=IlluminatorTiming(offset_ms=2.0, duration_ms=1.0),
        ),
    )
    ic = _ic_with_lines({"A": 1, "B": 2, "C": 4})
    assert nidaq_lines_for_state(state, ic) == [1, 4]
