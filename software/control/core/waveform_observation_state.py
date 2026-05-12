"""
Per-frame NI-DAQ waveform builder for waveform-driven observation states.

Used by ``MultiPointWorker.acquire_camera_image`` when a selected
``ObservationState`` carries one or more illuminators with a
:class:`~control.models.observation_state.IlluminatorTiming` block. The
builder produces a one-shot :class:`~control.nidaq.WaveformData` whose
digital-output lines pulse the LED gating lines at the requested offset and
duration relative to the start of the camera exposure.

The waveform is intended to be armed with ``TriggerSource.EXTERNAL`` against
the camera's exposure-active output, so its sample 0 lines up with the rising
edge of the camera frame.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from control.nidaq import WaveformData

if TYPE_CHECKING:
    from control.lighting import IlluminationController
    from control.models.observation_state import ObservationState


def build_pulse_waveform_for_state(
    state: "ObservationState",
    illumination_controller: "IlluminationController",
    sample_rate_hz: float,
) -> WaveformData:
    """Build a one-shot ``WaveformData`` for a waveform-driven observation state.

    Each active illuminator with a ``timing`` block contributes a HIGH window
    on its NIDAQ digital-output line from ``offset_ms`` to
    ``offset_ms + duration_ms`` after camera exposure begins. Active
    illuminators *without* timing are not represented here — those are held
    continuously high via the standard DC path before the trigger fires.

    Args:
        state: The observation state being captured. Must have an
            ``exposure_time`` (drawn from camera_settings/camera_live).
        illumination_controller: Used to resolve per-channel NIDAQ DO lines.
        sample_rate_hz: NIDAQ sample rate; controls pulse-edge resolution.

    Returns:
        A ``WaveformData`` whose ``digital_output`` keys are NIDAQ line
        indices and values are ``num_samples``-long boolean arrays.

    Raises:
        ValueError: When the state has no timed illuminators, when an
            illuminator's pulse window falls outside the exposure, when a
            timed illuminator has no resolvable NIDAQ DO line (e.g. LED
            matrix or serial-only channel), or when two timed illuminators
            collide on the same NIDAQ DO line.
    """
    exposure_ms = float(state.exposure_time)
    if exposure_ms <= 0:
        raise ValueError(f"ObservationState '{state.name}' has non-positive exposure_time")

    num_samples = max(1, int(round(exposure_ms * sample_rate_hz / 1000.0)))
    digital_output: dict[int, np.ndarray] = {}

    timed = [ist for ist in state.illuminator_states if ist.on and ist.timing is not None]
    if not timed:
        raise ValueError(
            f"ObservationState '{state.name}' has no active illuminators with timing; "
            "build_pulse_waveform_for_state should not be called on standard states"
        )

    for ist in timed:
        timing = ist.timing
        if timing.offset_ms < 0:
            raise ValueError(
                f"Illuminator '{ist.illumination_channel}': pulse offset_ms ({timing.offset_ms}) is negative"
            )
        if timing.offset_ms + timing.duration_ms > exposure_ms + 1e-6:
            raise ValueError(
                f"Illuminator '{ist.illumination_channel}': pulse window "
                f"[{timing.offset_ms}, {timing.offset_ms + timing.duration_ms}] ms "
                f"falls outside camera exposure of {exposure_ms} ms"
            )

        line = illumination_controller.get_nidaq_do_line_for_channel(ist.illumination_channel)
        if line is None:
            raise ValueError(
                f"Illuminator '{ist.illumination_channel}' has no NIDAQ digital-output gating line "
                "and cannot be driven by a pulse waveform (LED matrix and serial-only channels "
                "are not supported)"
            )

        start = max(0, int(round(timing.offset_ms * sample_rate_hz / 1000.0)))
        end = min(num_samples, start + max(1, int(round(timing.duration_ms * sample_rate_hz / 1000.0))))

        if line in digital_output:
            raise ValueError(
                f"Multiple timed illuminators share NIDAQ DO line {line}; "
                "combine their pulse intervals into a single illuminator instead"
            )
        pattern = np.zeros(num_samples, dtype=bool)
        pattern[start:end] = True
        digital_output[line] = pattern

    return WaveformData(digital_output=digital_output)


def nidaq_lines_for_state(
    state: "ObservationState",
    illumination_controller: "IlluminationController",
) -> list[int]:
    """Return the NIDAQ DO line indices needed to drive timed illuminators.

    Used by the worker to call ``configure_task_io(do_lines=...)`` before
    arming the per-frame waveform task.
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
