"""
Shared NIDAQ pulse-capture helpers for waveform-driven observation states.

A *waveform-driven* :class:`~control.models.observation_state.ObservationState`
(one whose active illuminator carries an ``IlluminatorTiming`` comb) has its
illumination gated by a one-shot NIDAQ digital pulse synchronized to the camera
exposure, instead of the LED being held on for the full exposure. This module
centralizes that arm/illuminate/cleanup logic so BOTH the multipoint
acquisition worker and the live/snap preview path drive an *identical* gated
pulse — i.e. live preview faithfully shows the same pulse the cycle captures,
rather than a misleading full-on flash.

These functions hold no worker/controller instance state: they take the
microscope, the observation state, and a few callables, so one implementation
serves both paths.
"""

from __future__ import annotations

import contextlib
from typing import Callable, Optional

import control._def
from control.nidaq import TriggerSource
from control.core.waveform_observation_state import (
    build_pulse_waveform_for_state,
    nidaq_lines_for_state,
)

# Callback invoked once per cleanup when the NIDAQ pulse never fired within the
# timeout: (frame_signal_terminal, timeout_s, state_name, error). The caller
# decides the policy (abort an acquisition, log-once in live, etc.).
WaitFailureHandler = Callable[[str, float, str, Optional[BaseException]], None]


def _noop_get_timer(_name: str):
    return contextlib.nullcontext()


def apply_illumination_for_waveform_capture(microscope, config, log) -> None:
    """Set DC intensities for a waveform-driven capture, leaving timed gates LOW.

    DC intensities for every active illuminator are set as usual, but the
    digital gating line for any illuminator with a ``timing`` block is left LOW
    — the NIDAQ one-shot waveform is the only thing that pulls it HIGH during the
    exposure. Standard (un-timed) illuminators in the same observation state are
    turned on for the full exposure, exactly like the regular path.
    """
    ic = microscope.illumination_controller
    active = config.active_illuminator_states
    if not active:
        return
    try:
        ic.apply_observation_illumination(
            active,
            turn_on=True,
            force_hardware=True,
            gate_timed_illuminators=False,
        )
    except Exception as e:
        log.warning("Could not apply waveform-capture illumination: %s", e)


def resolve_camera_frame_signal_terminal(microscope, nidaq, log) -> str:
    """Resolve the NIDAQ terminal carrying the camera's frame readout edge.

    Reads the ``main_camera.frame_readout`` endpoint from the machine config's
    ``io:`` declarations (e.g. ``port0/line7``), then translates the channel id
    into a form ``cfg_dig_edge_start_trig`` accepts. NI-DAQ won't take
    ``port0/lineN`` as a start-trigger source on X-series devices — for
    triggering you need the corresponding ``PFIN`` alias (same physical pin for
    N=0..7). Read/write paths still address the line as ``port0/lineN``; only the
    trigger source needs translation. Falls back to
    ``control._def.NIDAQ_FRAME_SIGNAL_TERMINAL`` when the endpoint is missing.
    """
    try:
        mc = microscope.config_repo.get_machine_config()
        io_config = mc.collect_io_endpoints()
        ep = io_config.get("main_camera.frame_readout")
        if ep is not None and ep.channel_id:
            device = getattr(nidaq, "device_name", None) or "Dev1"
            cid = ep.channel_id.strip()
            # Translate "port0/lineN" -> "PFIN" for the trigger-source path.
            if cid.startswith("port0/line"):
                try:
                    n = int(cid.rsplit("line", 1)[-1])
                    return f"/{device}/PFI{n}"
                except ValueError:
                    pass
            # Already a PFI/PXI/internal terminal — use as-is.
            return f"/{device}/{cid}"
        log.debug(
            "main_camera.frame_readout not declared in machine config; "
            "using NIDAQ_FRAME_SIGNAL_TERMINAL fallback %s",
            control._def.NIDAQ_FRAME_SIGNAL_TERMINAL,
        )
    except Exception:
        log.exception("Failed to resolve camera frame signal terminal; using fallback")
    return control._def.NIDAQ_FRAME_SIGNAL_TERMINAL


def arm_nidaq_pulse_for_capture(
    microscope,
    config,
    *,
    log,
    get_timer: Optional[Callable[[str], object]] = None,
    on_wait_failure: Optional[WaitFailureHandler] = None,
) -> Optional[Callable[[], None]]:
    """Arm an NIDAQ one-shot pulse waveform for a waveform-driven capture.

    Builds the per-frame ``WaveformData`` from the observation state's timed
    illuminators, configures the NIDAQ for an EXTERNAL start trigger latched on
    the camera's exposure-active line, and arms the task. Returns a cleanup
    closure that callers must invoke after the camera frame has been triggered;
    the closure waits for the waveform to finish, releases the tasks, drives the
    gate lines LOW, restores any prior live-output state, and rewrites the NIDAQ
    timing/trigger config back to whatever the previous user (fast acquisition,
    DAQ-only widget, etc.) had set.

    Returns ``None`` (and logs a warning) when the NIDAQ is not configured on
    this rig — the caller should then fall back to a standard (full-exposure)
    capture. Re-raises ``ValueError`` if the waveform cannot be built.

    Args:
        get_timer: ``name -> context-manager`` used to profile the arm/cleanup
            sub-steps (defaults to a no-op).
        on_wait_failure: called once per cleanup when the waveform never fired
            within the timeout, with ``(terminal, timeout_s, state_name,
            error)``. Lets the multipoint worker abort while live preview merely
            logs.
    """
    get_timer = get_timer or _noop_get_timer
    nidaq = getattr(microscope.addons, "nidaq", None)
    if nidaq is None:
        log.warning(
            "Waveform-driven observation state '%s' selected but no NIDAQ is configured; "
            "falling back to standard capture (illumination will stay on for the full exposure)",
            config.name,
        )
        return None

    ic = microscope.illumination_controller
    sample_rate_hz = float(control._def.NIDAQ_PULSE_SAMPLE_RATE_HZ)
    try:
        waveform = build_pulse_waveform_for_state(config, ic, sample_rate_hz=sample_rate_hz)
        do_lines = nidaq_lines_for_state(config, ic)
    except ValueError:
        log.exception("Failed to build NIDAQ pulse waveform for state '%s'", config.name)
        raise

    terminal = resolve_camera_frame_signal_terminal(microscope, nidaq, log)
    # The NIDAQ instance is shared with the fast-acquisition widget and the
    # DAQ-only acquisition controller — both write sample_rate_hz /
    # samples_per_channel / trigger_source / external_trigger_terminal directly.
    # Snapshot those now so we can put them back when our one-shot waveform is done.
    prev_sample_rate_hz = float(getattr(nidaq, "sample_rate_hz", sample_rate_hz))
    prev_samples_per_channel = int(getattr(nidaq, "samples_per_channel", 0))
    prev_trigger_source = getattr(nidaq, "trigger_source", TriggerSource.SOFTWARE)
    prev_external_terminal = getattr(nidaq, "external_trigger_terminal", terminal)

    # Pick our own task length to match the per-frame waveform — never inherit
    # whatever the previous run left behind.
    per_frame_samples = next(iter(waveform.digital_output.values())).size

    with get_timer("nidaq_waveform_arm"):
        try:
            nidaq.sample_rate_hz = sample_rate_hz
            nidaq.samples_per_channel = per_frame_samples
            nidaq.trigger_source = TriggerSource.EXTERNAL
            nidaq.external_trigger_terminal = terminal
            nidaq.configure_task_io(
                ao_channels=[],
                do_lines=do_lines,
                di_lines=[],
                ai_channels=[],
            )
            nidaq.prepare_for_acquisition()
            nidaq.set_waveforms(waveform)
            nidaq.arm()
            nidaq.start_trigger()
        except Exception:
            # Best-effort cleanup if any step in the arm sequence failed.
            try:
                nidaq.release_tasks()
            except Exception:
                pass
            try:
                restore_fn = getattr(nidaq, "restore_after_acquisition", None)
                if callable(restore_fn):
                    restore_fn()
            except Exception:
                pass
            # Put the NIDAQ timing/trigger config back to whatever it was.
            try:
                nidaq.sample_rate_hz = prev_sample_rate_hz
                if prev_samples_per_channel:
                    nidaq.samples_per_channel = prev_samples_per_channel
                nidaq.trigger_source = prev_trigger_source
                nidaq.external_trigger_terminal = prev_external_terminal
            except Exception:
                pass
            raise

    exposure_s = float(config.exposure_time) / 1000.0
    # Just enough to cover exposure + camera readout + DMA dispatch. A long wait
    # here only delays surfacing a wiring problem (e.g. the configured frame
    # signal terminal doesn't match where the camera signal is actually wired)
    # and stretches every frame needlessly.
    timeout_s = max(exposure_s + 0.2, 0.3)

    def _cleanup() -> None:
        with get_timer("nidaq_waveform_done"):
            wait_failed = False
            wait_error: Optional[BaseException] = None
            try:
                nidaq.wait_until_done(timeout_s=timeout_s)
            except Exception as e:
                wait_failed = True
                wait_error = e
            try:
                nidaq.release_tasks()
            except Exception as e:
                log.warning("NIDAQ release_tasks failed for '%s': %s", config.name, e)
            # Drive each timed DO line LOW before the live-output restore: a
            # FINITE DAQmx output task holds its last written sample after
            # release, so a comb whose final pulse runs to the window boundary
            # would otherwise leave the gate HIGH, and the live snapshot can't
            # fix it for never-live lines.
            try:
                nidaq.start_live_output(do_values={line: False for line in do_lines})
            except Exception as e:
                log.warning(
                    "Post-capture DO clear failed for '%s' on lines %s: %s",
                    config.name, do_lines, e,
                )
            try:
                restore_fn = getattr(nidaq, "restore_after_acquisition", None)
                if callable(restore_fn):
                    restore_fn()
            except Exception as e:
                log.warning("NIDAQ restore_after_acquisition failed for '%s': %s", config.name, e)
            # Restore the timing/trigger config snapshot so the next NIDAQ user
            # (fast acquisition widget, DAQ-only acquisition) sees the device in
            # the same shape it was before.
            try:
                nidaq.sample_rate_hz = prev_sample_rate_hz
                if prev_samples_per_channel:
                    nidaq.samples_per_channel = prev_samples_per_channel
                nidaq.trigger_source = prev_trigger_source
                nidaq.external_trigger_terminal = prev_external_terminal
            except Exception as e:
                log.warning("NIDAQ config restore failed for '%s': %s", config.name, e)
            if wait_failed and on_wait_failure is not None:
                on_wait_failure(terminal, timeout_s, config.name, wait_error)

    return _cleanup
