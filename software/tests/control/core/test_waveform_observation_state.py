"""Unit tests for the per-FOV NIDAQ pulse waveform builder."""

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


def _state_with(
    *illuminators: IlluminatorState,
    exposure_ms: float = 50.0,
    is_stimulus_only: bool = False,
    stimulus_duration_ms: float | None = None,
) -> ObservationState:
    return ObservationState(
        name="test",
        camera_settings=CameraSettings(exposure_time_ms=exposure_ms, gain_mode=1.0),
        illuminator_states=list(illuminators),
        is_stimulus_only=is_stimulus_only,
        stimulus_duration_ms=stimulus_duration_ms,
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
            timing=IlluminatorTiming(start_offset_ms=24.5, pulse_width_ms=1.0),
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
            timing=IlluminatorTiming(start_offset_ms=2.0, pulse_width_ms=1.0),
        ),
        IlluminatorState(
            illumination_channel="LaserB",
            intensity=30.0,
            on=True,
            timing=IlluminatorTiming(start_offset_ms=8.0, pulse_width_ms=1.0),
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
            timing=IlluminatorTiming(start_offset_ms=49.0, pulse_width_ms=5.0),
        ),
        exposure_ms=50.0,
    )
    ic = _ic_with_lines({"Laser561": 2})

    with pytest.raises(ValueError, match="outside the .* step window"):
        build_pulse_waveform_for_state(state, ic, sample_rate_hz=100_000.0)


def test_no_nidaq_line_raises():
    state = _state_with(
        IlluminatorState(
            illumination_channel="LED matrix",
            intensity=50.0,
            on=True,
            timing=IlluminatorTiming(start_offset_ms=1.0, pulse_width_ms=1.0),
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


def test_two_timed_illuminators_on_same_line_or_together():
    """OR'd lines: two illuminators sharing a DO line collapse to a bitwise OR."""
    state = _state_with(
        IlluminatorState(
            illumination_channel="A",
            intensity=50.0,
            on=True,
            timing=IlluminatorTiming(start_offset_ms=1.0, pulse_width_ms=2.0),
        ),
        IlluminatorState(
            illumination_channel="B",
            intensity=50.0,
            on=True,
            timing=IlluminatorTiming(start_offset_ms=5.0, pulse_width_ms=2.0),
        ),
        exposure_ms=20.0,
    )
    ic = _ic_with_lines({"A": 3, "B": 3})

    wf = build_pulse_waveform_for_state(state, ic, sample_rate_hz=10_000.0)
    # 10 kHz: 20 ms window = 200 samples; A's pulse at samples 10..30, B's at 50..70.
    assert set(wf.digital_output.keys()) == {3}
    pattern = wf.digital_output[3]
    assert pattern.size == 200
    high = np.where(pattern)[0]
    # Both pulses present (disjoint), summing to 40 samples HIGH total.
    assert high.size == 40
    # Confirm both windows actually appear.
    assert pattern[10:30].all()
    assert pattern[50:70].all()


def test_comb_generates_evenly_spaced_pulses():
    state = _state_with(
        IlluminatorState(
            illumination_channel="A",
            intensity=50.0,
            on=True,
            timing=IlluminatorTiming(
                start_offset_ms=0.0,
                pulse_width_ms=5.0,
                period_ms=50.0,
                num_pulses=10,
            ),
        ),
        is_stimulus_only=True,
        stimulus_duration_ms=500.0,
    )
    ic = _ic_with_lines({"A": 1})

    wf = build_pulse_waveform_for_state(state, ic, sample_rate_hz=10_000.0)
    assert list(wf.digital_output.keys()) == [1]
    pattern = wf.digital_output[1]
    # 500 ms × 10 kHz = 5000 samples
    assert pattern.size == 5000
    # Each pulse: 5 ms × 10 kHz = 50 samples; periods at 50 ms × 10 kHz = 500 samples
    high = np.where(pattern)[0]
    assert high.size == 10 * 50
    # Check the first rising edge is at sample 0; last rising edge is at sample 4500.
    rising = high[np.where(np.diff(high, prepend=high[0] - 2) != 1)]
    assert list(rising) == [0, 500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500]


def test_stimulus_state_uses_stimulus_duration_for_window():
    state = _state_with(
        IlluminatorState(
            illumination_channel="A",
            intensity=50.0,
            on=True,
            timing=IlluminatorTiming(
                start_offset_ms=0.0,
                pulse_width_ms=5.0,
                period_ms=10.0,
                num_pulses=3,
            ),
        ),
        # Camera exposure is 50 ms but stimulus window is 200 ms — the builder
        # must use the stimulus duration for samples_per_channel.
        exposure_ms=50.0,
        is_stimulus_only=True,
        stimulus_duration_ms=200.0,
    )
    ic = _ic_with_lines({"A": 7})

    wf = build_pulse_waveform_for_state(state, ic, sample_rate_hz=10_000.0)
    pattern = wf.digital_output[7]
    assert pattern.size == 2000  # 200 ms × 10 kHz, not 500


def test_period_at_or_below_width_rejected_at_model():
    with pytest.raises(ValueError, match="period_ms must exceed pulse_width_ms"):
        IlluminatorTiming(pulse_width_ms=5.0, period_ms=5.0, num_pulses=2)


def test_nidaq_lines_for_state_returns_unique_line_indices():
    state = _state_with(
        IlluminatorState(
            illumination_channel="A",
            intensity=50.0,
            on=True,
            timing=IlluminatorTiming(start_offset_ms=1.0, pulse_width_ms=1.0),
        ),
        IlluminatorState(
            illumination_channel="B",
            intensity=50.0,
            on=False,  # Inactive — must not appear
            timing=IlluminatorTiming(start_offset_ms=1.0, pulse_width_ms=1.0),
        ),
        IlluminatorState(
            illumination_channel="C",
            intensity=50.0,
            on=True,
            timing=IlluminatorTiming(start_offset_ms=2.0, pulse_width_ms=1.0),
        ),
    )
    ic = _ic_with_lines({"A": 1, "B": 2, "C": 4})
    assert nidaq_lines_for_state(state, ic) == [1, 4]
