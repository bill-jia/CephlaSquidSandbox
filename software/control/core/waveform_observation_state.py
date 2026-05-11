"""
Per-step NI-DAQ waveform builder for waveform-driven observation states.

Used by ``MultiPointWorker`` when a selected ``ObservationState`` carries one
or more illuminators with an :class:`IlluminatorTiming` comb, or when the
state is a stimulus-only step (``is_stimulus_only=True``).

For capture-window timed pulses the waveform is armed with
``TriggerSource.EXTERNAL`` so its sample 0 lines up with the rising edge of
the camera's exposure-active output. For stimulus-only steps the same
waveform shape is armed with ``TriggerSource.SOFTWARE`` and fires the moment
``start_trigger()`` is called.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from control.nidaq import WaveformData, generate_pulse_train

if TYPE_CHECKING:
    from control.lighting import IlluminationController
    from control.models.observation_state import ObservationState


def build_pulse_waveform_for_state(
    state: "ObservationState",
    illumination_controller: "IlluminationController",
    sample_rate_hz: float,
) -> WaveformData:
    """Build a one-shot ``WaveformData`` for a waveform-driven observation state.

    Each active illuminator with a ``timing`` comb contributes a pulse train
    on its NIDAQ digital-output line; combs sharing the same NIDAQ DO line
    are bitwise-OR'd into a single pattern on that line. The total task
    window comes from ``state.step_window_ms`` — i.e. the camera exposure
    for capture states or ``stimulus_duration_ms`` for stimulus-only states.

    Args:
        state: The observation state being executed.
        illumination_controller: Used to resolve per-channel NIDAQ DO lines.
        sample_rate_hz: NIDAQ sample rate; controls pulse-edge resolution.

    Returns:
        A ``WaveformData`` whose ``digital_output`` keys are NIDAQ line
        indices and values are ``num_samples``-long boolean arrays.

    Raises:
        ValueError: when the state has no timed illuminators, when a comb's
            last edge falls outside the step window, or when a timed
            illuminator has no resolvable NIDAQ DO line.
    """
    window_ms = float(state.step_window_ms)
    if window_ms <= 0:
        raise ValueError(
            f"ObservationState '{state.name}' has non-positive step window ({window_ms} ms)"
        )

    num_samples = max(1, int(round(window_ms * sample_rate_hz / 1000.0)))
    digital_output: dict[int, np.ndarray] = {}

    timed = [ist for ist in state.illuminator_states if ist.on and ist.timing is not None]
    if not timed:
        raise ValueError(
            f"ObservationState '{state.name}' has no active illuminators with timing; "
            "build_pulse_waveform_for_state should not be called on standard states"
        )

    for ist in timed:
        timing = ist.timing
        if timing.end_ms > window_ms + 1e-6:
            raise ValueError(
                f"Illuminator '{ist.illumination_channel}': comb ends at "
                f"{timing.end_ms} ms, outside the {window_ms} ms step window"
            )

        line = illumination_controller.get_nidaq_do_line_for_channel(ist.illumination_channel)
        if line is None:
            raise ValueError(
                f"Illuminator '{ist.illumination_channel}' has no NIDAQ digital-output gating line "
                "and cannot be driven by a pulse waveform (LED matrix and serial-only channels "
                "are not supported)"
            )

        pulse_width_samples = max(1, int(round(timing.pulse_width_ms * sample_rate_hz / 1000.0)))
        n_samples_offset = max(0, int(round(timing.start_offset_ms * sample_rate_hz / 1000.0)))
        if timing.num_pulses > 1:
            period_samples = int(round(timing.period_ms * sample_rate_hz / 1000.0))
            # Pydantic validator already enforces period_ms > pulse_width_ms; clamp here
            # to defend against rounding aliasing at low sample rates.
            period_samples = max(period_samples, pulse_width_samples + 1)
        else:
            # A single pulse: period_samples is unused by generate_pulse_train when
            # max_num_pulses=1, but must be a sane positive value.
            period_samples = pulse_width_samples + 1

        pattern = generate_pulse_train(
            pulse_width_samples=pulse_width_samples,
            period_samples=period_samples,
            num_samples=num_samples,
            n_samples_offset=n_samples_offset,
            inverted=False,
            max_num_pulses=timing.num_pulses,
        )
        # Coerce to bool: WaveformData.digital_output is keyed on bool arrays.
        pattern = pattern.astype(bool, copy=False)

        if line in digital_output:
            # OR'd shared line: two illuminators mapped to the same NIDAQ DO line
            # contribute their pulse patterns together (allowed by design).
            digital_output[line] = np.logical_or(digital_output[line], pattern)
        else:
            digital_output[line] = pattern

    return WaveformData(digital_output=digital_output)


def nidaq_lines_for_state(
    state: "ObservationState",
    illumination_controller: "IlluminationController",
) -> list[int]:
    """Return the NIDAQ DO line indices needed to drive timed illuminators.

    Used by the worker to call ``configure_task_io(do_lines=...)`` before
    arming the per-FOV waveform task.
    """
    lines: list[int] = []
    for ist in state.illuminator_states:
        if not ist.on or ist.timing is None:
            continue
        line = illumination_controller.get_nidaq_do_line_for_channel(ist.illumination_channel)
        if line is None:
            raise ValueError(
                f"Illuminator '{ist.illumination_channel}' has no NIDAQ digital-output gating line"
            )
        if line not in lines:
            lines.append(line)
    return lines
