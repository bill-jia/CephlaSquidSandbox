# Multipoint Acquisition Speed Optimization

Optimizations applied to shrink per-FOV overhead during multipoint acquisition on the Tucsen + SQUID rig. Measurements are from an 81-FOV acquisition (27 positions × 3 channels) with 110 ms exposure.

## Results summary

| Run | `run_single_time_point` | Notes |
|---|---|---|
| Baseline (pre-optimization) | 55.6 s | |
| After GenICam sleep fix | 39.8 s | −15.8 s (−28 %) |
| After `turn_off_all` fix (projected) | ~34 s | additional ~−5.7 s |

## Instrumentation added

Fine-grained timers were threaded through the observation-state apply path so `multi_point_worker`'s existing `TimingManager` can record sub-step durations:

- `ObservationStateController` gained an optional `_timing` attribute and a `_time(name)` helper that returns `contextlib.nullcontext()` when unset.
- Every hardware call inside `apply_observation_state_preset`, `_apply_camera_live_snapshot`, `apply_full_observation_state`, `apply_illumination_parameters`, and `apply_optical_path` is wrapped in a named timer prefixed with `obs:*`.
- `MultiPointWorker.run()` attaches its timing manager to `obs_controller._timing` at acquisition start and detaches on finish, so the sub-step rows appear in the standard timings report.
- Extra per-site timers (`acquire_camera_image_inner`, `turn_off_prev_channel_illumination`, `turn_off_capture_illumination`) localize the remaining overhead inside `acquire_camera_image`.

These timers are zero-cost when `_timing` is unset, so they stay enabled in production.

## Optimization 1 — Defer GenICam post-write sleep

**File**: `software/control/camera_tucsen.py`, `_set_genicam_parameter`

**Problem**: every GenICam parameter write ended with an unconditional `time.sleep(0.1)` "to avoid sequential parameter setting from causing errors." During multipoint, each channel switch really does change the camera's exposure time, so `cs_set_exposure_time` hit that sleep on every FOV — ~106 ms per call, 8.6 s across 81 FOVs (83 % of the old `apply_observation_state_to_hardware` budget). `send_trigger` also paid the sleep somewhere in its setup path (~104 ms/call).

**Fix**: convert the sleep from post-write to *pre-write gate*. The invariant ("≥100 ms between consecutive GenICam writes") is preserved, but when natural work (stage move, image capture, illumination setup) fills the gap, the sleep collapses to zero.

```python
# In __init__
self._last_genicam_write_ts: float = 0.0

# In _set_genicam_parameter, before the SDK call
elapsed_since_last_write = time.perf_counter() - self._last_genicam_write_ts
if elapsed_since_last_write < 0.1:
    time.sleep(0.1 - elapsed_since_last_write)

# At the end of _set_genicam_parameter (replaces time.sleep(0.1))
self._last_genicam_write_ts = time.perf_counter()
```

**Measured impact**:

| Timer | Before | After |
|---|---|---|
| `cs_set_exposure_time` mean | 106 ms | 3 ms |
| `send_trigger` mean | 104 ms | 2 ms |
| `apply_observation_state_to_hardware` mean | 127 ms | 17 ms |
| `run_single_time_point` | 55.6 s | 39.8 s |

## Optimization 2 — Skip already-off channels in `turn_off_all_hardware_preserving_state`

**File**: `software/control/lighting.py`, `IlluminationController.turn_off_all_hardware_preserving_state`

**Problem**: after every captured frame, `_turn_off_capture_illumination_preserving_logical_state` called `turn_off_all(preserve_logical_state=True)` → `turn_off_all_hardware_preserving_state`, which iterated every device and called `dev.turn_off_all()`, which in turn iterated every channel on the device. On SQUID with 6 IO-routed channels, each `turn_off(ch)` does `shutter_ep.set_digital(False) + wait` plus a DAC write — ~12–14 ms per channel. Total: **~84 ms/FOV** to turn off 6 channels when only one was ever on.

**Fix**: filter by `_hardware_asserted`. The controller already records which channels are actually driving hardware (every `set_channel_state` update keeps this flag in sync), so we only need to command off channels where `_hardware_asserted[ch] == True`.

```python
def turn_off_all_hardware_preserving_state(self) -> None:
    for ch, asserted in list(self._hardware_asserted.items()):
        if not asserted:
            continue
        dev = self._channel_map.get(ch)
        if dev is not None:
            try:
                dev.turn_off(ch)
            except Exception as exc:
                logger.warning(...)
        self._hardware_asserted[ch] = False
```

**Works for every device type** because `_hardware_asserted` is keyed by `_channel_map` keys (which use the unified channel name for `LEDMatrixIlluminationDevice`). None of the concrete subclasses override `turn_off_all`, so replacing it with a filtered per-channel call is equivalent.

**Expected impact**: for typical multipoint acquisition with one channel on at a time, 5 of 6 `turn_off(ch)` calls are skipped → ~70 ms/FOV saved → **~5.7 s on an 81-FOV run**. `turn_off_capture_illumination` row should drop from ~84 ms mean to ~14 ms mean.

**Caveat**: if `_hardware_asserted` drifts out of sync with actual hardware state (e.g., a device is manipulated outside the controller), a channel could be left on. The old call was "belt and suspenders"; the new one trusts bookkeeping that's already maintained for every live-mode and acquisition call.

## Remaining wins (ideas, not yet applied)

After the two optimizations above, the 39.8 s → ~34 s breakdown looks approximately:

| Bucket | Total | Share |
|---|---|---|
| `move_to_coordinate` (27×) | ~15.9 s | ~45 % |
| Camera exposure floor (81 × 110 ms) | 8.9 s | ~25 % |
| `perform_autofocus` (27×) | ~9.4 s | ~26 % |
| Residual per-FOV overhead | ~1–2 s | ~5 % |

Ordered by expected savings:

1. **Overlap stage move → next observation state & autofocus prep.** Stage moves for ~588 ms; `apply_observation_state` (now 17 ms) and other preparatory work can run during the move. Savings up to ~1.5 s.
2. **Parallelize autofocus with stage move of next position, or sparsify it.** ~9.4 s on autofocus; big structural win if it can overlap or run less often.
3. **`wait_for_image_sw` variance (min 2.7 ms / max 127 ms, mean 54 ms for 110 ms exposure).** The wide spread suggests some frames arrive well before we reach the wait (illumination setup filled the time since the previous trigger). Worth poking at once the high-leverage items are done.
4. **Skip no-op illumination writes in `apply_illumination_parameters`.** Iterates all 6 channels × 81 FOVs = 486 `set_channel_intensity` + 486 `set_channel_state` calls even though only the active channel changes. Small savings (~400 ms) but trivial.
5. **Dedupe `apply_shutter_state_to_hardware` (0.79 s total).** Applies every loop even when the logical state already matches hardware. ~0.5 s savings.
