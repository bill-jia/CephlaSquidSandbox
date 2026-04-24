# NIDAQ per-observation-state synchronous task

## Context

Today the NIDAQ subsystem runs in two disjoint modes:

1. **Fast acquisition** — a single pre-armed waveform task drives camera
   trigger + any LED/AO timing synchronously from the DAQ sample clock. Armed
   once via `arm()` / `start_trigger()`, runs through its full pattern with
   zero Python-thread involvement. This is the path in
   `control/core/fast_acquisition_controller.py` and the `nidaq_fast.py`
   plumbing.

2. **Live / multipoint** — one-shot control through `NIDAQIOController`. Each
   primitive operation (`set_digital`, `set_analog`, `send_trigger`) creates
   an on-demand DAQmx task, writes a value or fires a pulse, tears down.
   `send_edge_pulse` (the camera trigger path used by the virtualized
   software-trigger mode on the Aries) is a FINITE sample-clocked DO task —
   still hardware-clocked edges for the camera to latch, but with per-fire
   DAQmx arm overhead.

Right now NIDAQ only drives **one** signal in multipoint: the camera trigger.
No LED shutters, no analog intensity outputs, no strobe lines on NIDAQ
(illumination is routed through the MCU / CoolLED / etc. at the moment).
Under that constraint the mode-2 path is workable, though each
`send_edge_pulse` still costs ~100+ ms of DAQmx arm/stop overhead on this
rig. See current baseline in `timings.txt` (Phase C1) — send_trigger runs
~125 ms/capture vs. ~2 ms for the native software-trigger path.

## Why this will get worse

The natural next additions to NIDAQ are:

- LED shutter DO lines gated off the camera exposure window.
- Analog intensity AO lines that ramp during exposure (confocal,
  structured illumination).
- A second camera trigger (side camera, AF camera) co-fired with the main.

As each of those lands, mode-2 grows the per-capture overhead: every extra
line claims (or contends for) the DO port, and each `send_*` primitive
repeats the arm/fire/stop cycle. Layering the `start_live_output` /
`_stop_live_output` juggling back on top — which we just ripped out of
`send_edge_pulse` — is a workable but expensive shortcut.

## Proposed direction

Replace the one-shot `send_trigger` / `set_digital` / `set_analog` path for
multipoint with a **per-observation-state waveform task**:

1. When the worker transitions into a new observation state (channel /
   exposure), **compute the waveform** for the current capture: camera
   trigger pulse at `t=0`, LED shutter on at `t=strobe_delay`, LED shutter
   off at `t=exposure_end`, any AO ramps in between.
2. **Pre-arm** the waveform task before the FOV move completes (hide the
   arm cost in stage-motion dead time).
3. **Fire** via `start_trigger()` at the moment the worker wants to capture.
   Everything — camera edge, LED gate, AO ramp — runs off the DAQ's own
   sample clock. Single Python call, no per-primitive arm overhead.
4. `wait_until_done()` on the DAQ task returns when the hardware says the
   pattern is complete; the frame is already on its way via the SDK
   callback path by then.
5. On observation-state change, rebuild the waveform task (same cadence
   as today's `set_camera_mode` / `set_exposure_time` writes).

This is exactly fast-acquisition's arming model, extended to one-shot
captures instead of continuous streaming. The same
`prepare_for_acquisition` / `arm` / `start_trigger` / `wait_until_done` /
`release_tasks` primitives in `nidaq.py` already support it.

## Current state (after the persistent on-demand DO task refactor)

Everything on the mode-2 path now flows through one `nidaqmx.Task("persistent_do")`
that holds every DO line we've touched. `start_live_output`, `set_digital`,
and `send_edge_pulse` are all just `write()` calls on that task. Per-op
cost is ~200–500 µs. The task is torn down in `_stop_live_output` when
fast acquisition claims the DO port and rebuilt by
`restore_after_acquisition` — same lifecycle the old `_live_do_task` had.

This already covers "several lines on the same port" cleanly: adding an
LED shutter DO line is a single `start_live_output(do_values={led: True})`
call in the illumination controller; the persistent task extends to
include the new line on first use and stays stable from then on. Python
paces the high/low transitions.

## Retire at that point

- `NIDAQIOController.send_trigger` → replaced by the waveform task's
  `start_trigger()`. The `camera_tucsen.send_trigger()` virtualized-SW
  path calls `self._hw_trigger_fn(None)` which today routes to
  `NIDAQIOController.send_trigger`; redirect it to the per-state task's
  fire method.
- `NIDAQ.send_edge_pulse` becomes dead code (its two writes on the
  persistent task are subsumed by the waveform's preloaded edge).
- The persistent DO task itself stays as the transport for pure
  "set-and-hold" operations that don't need DAQ-clock timing (debug
  pokes from the IO panel, static rigging state between captures).
  `_ensure_persistent_do_task_locked` / `_write_persistent_do_state_locked`
  / `_teardown_persistent_do_task_locked` all remain useful.

## Why not now

The payoff is proportional to how much NIDAQ-driven IO needs sub-ms
synchronization to the camera exposure window. The persistent-task
refactor brings trigger overhead under 1 ms, so Python-paced LED gating
that straddles exposures is adequate for most use cases. The per-state
waveform task becomes worth building when a second signal needs *timed*
coordination with the trigger edge (strobe-delay compensation,
intra-exposure AO ramps, dual-camera co-fire with bounded offset).

## Constraints to keep in mind when it's time

- Fast acquisition owns the waveform engine during its runs — the
  per-observation-state task must coexist with fast acq's lifecycle
  (use `prepare_for_acquisition` / `release_tasks` around fast acq start /
  stop the same way we do today).
- The camera trigger line (default `port0/line12`) is shared with fast
  acquisition's preloaded trigger channel; don't re-bind it mid-task.
- Strobe-delay compensation for rolling-shutter cameras (currently
  disabled in `camera_tucsen.set_exposure_time`) needs to come back on
  as part of the waveform compute — per-LED pulse positioned at
  `readout_time_ms` after the trigger edge.
