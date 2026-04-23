# Reducing overhead in `acquire_camera_image_inner`

## Context

A Tucsen-Aries multipoint run (27 FOVs × 3 channels = **81 captures**) currently spends **14.89 s** inside `acquire_camera_image_inner` — mean **184 ms/capture**. Of that, only ~**85 ms/capture** is irreducible camera exposure (mix of 5/50/200 ms per the user's preset). The remaining ~**99 ms/capture** is software overhead, totalling **~8 s of reducible time per timepoint**.

Per-capture overhead, measured from `timings.txt` "CURRENT BEST":

| sub-step | total (81 caps) | mean / cap | where |
|---|---|---|---|
| `illuminate_for_capture` | 1.01 s | 12.5 ms | `multi_point_worker.py:2087` — calls `_apply_current_illumination_state_to_hardware` + `wait_till_operation_is_completed`. Inside that, `apply_shutter_state_to_hardware` is 0.63 s and the MCU round-trip wait is the balance. |
| `illumination_settle` | 0.54 s | 6.7 ms | `multi_point_worker.py:2101` — settle sleep, value driven by `control._def.Acquisition.ILLUMINATION_SETTLE_MS` (machine config). |
| `send_trigger` | 0.20 s | 2.4 ms | `multi_point_worker.py:2156` → `camera_tucsen.py:1727`. |
| `exposure_time_done_sleep_hw or wait_for_image_sw` | 9.88 s | 122 ms | `multi_point_worker.py:2158`. Contains the 85 ms exposure; the **~37 ms above exposure** is frame readout + `_propogate_frame` + callback latency. |
| `turn_off_capture_illumination` | 1.27 s | 15.7 ms | `multi_point_worker.py:2183` → `illumination_controller.turn_off_all(preserve_logical_state=True)`. Iterates all devices. |
| unaccounted | ~2.0 s | ~24 ms | logging (the `_log.info` on 2153 formats the whole `CaptureInfo`), the `get_ready_for_trigger re-check` loop (`:2121`), `backpressure.should_throttle()` (`:2113`), `CaptureInfo` construction (`:2130`). |

Hardware trigger plumbing already exists: `acquisition_camera_hw_trigger_fn` is wired in `microscope.py:697-738` and routes via an IO endpoint (NI-DAQ or MCU). Tucsen's "virtualized SW trigger" path exists in `camera_tucsen.py:1740-1752` but is force-disabled (`_virt_sw_trigger = False`, `:1575` with the previous auto-detection commented out on `:1572-1573`). Full `HARDWARE_TRIGGER` mode's rolling-shutter strobe-delay compensation is also currently disabled in `set_exposure_time` (`:1043-1058`).

`stage.get_pos()` returns cached values (`microcontroller.py:1798-1799`) — not an MCU round-trip. `CaptureInfo` build is cheap. The 24 ms unaccounted is mostly logging + loop safeguards, not metadata gathering.

## Recommended phased approach

Three phases, smallest-and-safest first. Each is independently shippable and measurable via the TimingManager report.

### Phase A — Quick wins (low-risk, ~30–50 ms / capture saved)

All within `multi_point_worker.py::acquire_camera_image` and adjacent:

- **A1. Delete the `get_ready_for_trigger` re-check loop** (`:2121-2129`). The comment at `:2123` says it's already a no-op on the hot path. Replace with a single `self._ready_for_next_trigger.clear()` (the `else` branch already does exactly that). Saves ~1 ms/cap, removes the `self._sleep(0.001)` call.
- **A2. Demote per-capture logging on the hot path**. The `_log.info` at `:2153` formats the full `CaptureInfo` into the message, and `:2091` ("Using legacy illumination for capture") runs when not using observation presets. Convert both to `_log.debug` (or gate behind a verbose-capture flag). Formatting a `@dataclass` with a `Pos` and an `ObservationState` is ~10–15 ms per call in Python's logging when any handler is active. Expect ~10–20 ms/cap back.
- **A3. Batch the "apply shutter state + wait" round-trip**. `_apply_current_illumination_state_to_hardware` (`:526-540`) fires `set_channel_state(name, is_on, force_hardware=True)` **per channel in a Python loop**, then `wait_till_operation_is_completed` runs once outside. Confirm the lighting controller's implementation in `control/lighting.py:1108` coalesces these writes into a single MCU packet; if it doesn't, add a `set_shutter_bank(state_dict)` path that emits one `SET_MULTI_PORT_MASK` (`_def.py:121`, already a firmware command) and one wait. Saves ~5–8 ms/cap on `illuminate_for_capture` and likely a similar amount on `turn_off_capture_illumination`.
- **A4. Tighten `ILLUMINATION_SETTLE_MS`** via the machine config (`software.acquisition.illumination_settle_ms`) after measuring the LED rise time on this rig with a photodiode trace — the 6.7 ms currently applied may be conservative. Pure config change, reversible. Saves up to ~5 ms/cap if reducible.

**Phase A expected gain**: **~25–45 ms/cap → ~2.0–3.6 s per timepoint.** No behavior change, no hardware tests needed beyond "images still look right."

### Phase B — Overlap the next capture's setup with the current frame's delivery (medium risk)

The current flow is fully serial per capture: `illuminate_on → settle → trigger → wait → illuminate_off`. The ~37 ms "above-exposure" window between `send_trigger` and the frame-callback completion is dead time for the main thread on the sample camera side; the MCU and illumination hardware are idle.

Interleave by **deferring `turn_off_capture_illumination` and the next-channel illumination change to the next iteration's `_ready_for_next_trigger.wait` window**. Specifically, at `multi_point_worker.py:2105` add a small asynchronous "pending illumination update" queued before the wait; the worker fires those commands off to the MCU/NI-DAQ during the wait, not after it. The wait itself is currently 0.001 s — with deferred MCU work, the wait-vs-frame race is what fills it.

**Constraints / risks**: must keep the previous channel asserted throughout that capture's exposure; only *flip* at the boundary, and only after the frame callback signals frame-arrived. Measurement overhead (we're trading one set of MCU commands from the tail of capture N to the head of capture N+1). Needs careful state machine around `_last_illumination_config_name`.

**Phase B expected gain**: up to ~20 ms/cap (mostly turning the `turn_off_capture_illumination` 15.7 ms into near-zero on the critical path). ~1.6 s per timepoint. Not worth the complexity until Phase A lands.

### Phase C — Re-enable the hardware-gated trigger path (larger gain, per-rig validation)

This recovers the **~37 ms of frame-delivery overhead** on the SW-trigger path. Two variants, listed from least to most invasive:

- **C1 (preferred first step). Re-enable Tucsen virtualized SW trigger when `_hw_trigger_fn` exists.** In `camera_tucsen.py:1575`, replace `virtualize_sw_trigger = False` with the originally-commented-out conditional from `:1572-1573`:
  ```
  virtualize_sw_trigger = (
      acquisition_mode == CameraAcquisitionMode.SOFTWARE_TRIGGER and self._hw_trigger_fn is not None
  )
  ```
  The camera then runs in `TUCCM_TRIGGER_STANDARD` mode (gated, single-shot semantics) and `send_trigger` fires `_hw_trigger_fn(None)` — a fast NI-DAQ or MCU pulse on the trigger line (`microscope.py:697-719`). LED control stays in the worker (`_virt_sw_trigger` path keeps the illumination controller's steady-on behavior — `camera_tucsen.py:1102` guards `_uses_hw_trigger_timing` from firing for virtualized SW). That's why the rolling-shutter strobe-delay compensation stays disabled for this variant: safe for the virtualized path by design.

  Expected gain: **~25–30 ms/cap** (drops the GenICam TriggerSoftwarePulse round-trip and makes readout → callback deterministic through the gated mode). Also removes the concern about `TUCCM_SEQUENCE`-mode frame pipelining that I (incorrectly) flagged earlier.

  Risk: **medium.** Requires verifying the `main_camera.trigger` IO endpoint (`io_registry.get(...)` at `microscope.py:695`) is present and functional on this rig (the NIDAQ_test.ini config suggests yes). Once verified, one-line flip.

- **C2 (bigger bet). Full `DEFAULT_TRIGGER_MODE = HARDWARE`.** Changes `_def.py:477`, makes the MCU pulse both the LED *and* the camera trigger synchronously (`microcontroller.send_hardware_trigger(control_illumination=True, illumination_on_time_us=...)`, `microcontroller.py:1111`). Worker-side `illuminate_for_capture` / `turn_off_capture_illumination` become no-ops for this channel switch because the pulse itself is the illumination. Requires **re-enabling the strobe-delay + rolling-shutter readout compensation in `set_exposure_time`** (`camera_tucsen.py:1043-1058`, currently commented out per its own TODO) to avoid top-row/bottom-row brightness gradients on the rolling-shutter sensor. Per-channel calibration of `_strobe_delay_ms` and `_rolling_shutter_readout_ms` needed.

  Expected gain: up to **~60 ms/cap** (eliminates illuminate on/off, settle, frame-delivery overhead — only the physical exposure plus a small post-pulse sleep remain). ~5 s per timepoint on this run.

  Risk: **higher.** Rolling-shutter compensation wasn't trivial the first time around — that's why it's currently disabled. Needs a photodiode trace per channel and/or visual brightness-uniformity checks on dim samples. Worth doing **only after C1 lands and proves stable**, and only if the Phase A + C1 result isn't enough.

## Critical files

| file | purpose | phases |
|---|---|---|
| `software/control/core/multi_point_worker.py` | the overhead hot path (`acquire_camera_image`, `_apply_current_illumination_state_to_hardware`, `_turn_off_capture_illumination_preserving_logical_state`) | A1, A2, A3, B |
| `software/control/lighting.py` | multi-channel MCU command coalescing | A3 |
| `software/control/microcontroller.py` | `SET_MULTI_PORT_MASK` firmware command already exists | A3 |
| `software/control/_def.py` | `DEFAULT_TRIGGER_MODE` (line 477), `ILLUMINATION_SETTLE_MS` default (line 50) | A4, C2 |
| machine config YAML | `software.acquisition.illumination_settle_ms` | A4 |
| `software/control/camera_tucsen.py` | `virtualize_sw_trigger` flag at `:1575`, strobe-delay/rolling-shutter block at `:1043-1058` | C1, C2 |
| `software/control/microscope.py` | `acquisition_camera_hw_trigger_fn` + IO endpoint wiring (`:697-738`) — **no change needed, just verify endpoint present** | C1, C2 |

### Reused existing infrastructure

- `control._def.CMD_SET.SET_MULTI_PORT_MASK` (`_def.py:121`) — firmware-level batch-mask command for grouping illumination state changes.
- `Microcontroller.send_hardware_trigger` (`microcontroller.py:1111`) — already handles `control_illumination=True` with MCU-driven LED pulse width.
- `acquisition_camera_hw_trigger_fn` (`microscope.py:697-719`) — already wired to both NI-DAQ and MCU back-ends.
- `camera_tucsen.py::_uses_hw_trigger_timing` (`:1091-1105`) and `:_calculate_strobe_delay` paths — exist but partially commented; Phase C2 uncomments them.
- `keep_illuminators_on_between_captures` logic (`multi_point_worker.py:2075-2082, 2182`) — already implements "don't toggle illumination if the channel is unchanged" for the single-channel-per-FOV case. Phase B extends this to overlap transitions across different channels.

## Verification

For each phase, compare against a baseline captured from the same acquisition config (same regions, same channels, same exposures).

1. **Baseline capture** (do this first, before any change): enable TimingManager and run a fixed 27-FOV × 3-channel multipoint. Save the timing report. This is the "CURRENT BEST" reference.

2. **After Phase A**: re-run the same multipoint, diff the TimingManager report. Expect `illuminate_for_capture`, `turn_off_capture_illumination`, and the "unaccounted" delta in `acquire_camera_image_inner` to drop. Visually inspect a sample of saved TIFFs/zarrs across all three channels — brightness/contrast should match the baseline.

3. **After Phase B**: same timer diff, plus verify `_last_illumination_config_name` still reflects the correct channel after each capture (inspect log across a channel-switch boundary). Confirm no cross-channel illumination leakage by comparing fluorescence-channel mean intensity to the Phase A baseline (any leakage shows up as elevated dark counts).

4. **After Phase C1**: confirm the camera actually enters gated single-shot mode (read back `TriggerMode` at `camera_tucsen.py:1628` — should log `"Standard"` per the mapping on `:1623`). Confirm `trigger_ep` resolves to a non-None IO endpoint (add a log on startup). Re-run multipoint and compare images — should be pixel-equivalent to Phase A in terms of exposure; timing should drop ~2 s per timepoint on this run.

5. **After Phase C2**: run the existing `software/test_fast_acquisition_hw_trigger.py` as a hardware smoke test first. Then repeat the multipoint. Use a photodiode trace (or a flat-field sample) per channel to verify brightness uniformity top-to-bottom (rolling-shutter artefacts manifest as vertical gradients). Add an "after" check on the CSV the table-path audit writes — focus displacement distribution should be unchanged (the AF path itself is unaffected, but we're verifying the illumination swap didn't introduce any timing bugs).

Stop at whichever phase meets the wall-clock goal. A + C1 alone is likely enough to push the 27-FOV/3-ch run under ~20 s/timepoint.
