# Toupcam DPC gray-noise & analog-gain debug (2026-06-08)

Debugging of the Heeseok QPM/DPC setup (Toupcam `ITR3CMOS26000KMA` + SCI-dome
LED matrix, SN `12732490`) where multipoint DPC captures produced **gray-noise
frames** and analog gain appeared **stuck at 13.979**. All findings below were
verified on the live rig (camera + dome driven directly, no stage).

## Symptoms

- DPC TIFFs (`BF_single`, `BF_left/right/top/bottom`) were a flat pedestal
  (mean ≈ 777, std ≈ 21) ≈ 9 counts above the black-level pedestal — essentially
  **no integrated light**, not random sensor noise. Pedestal = `black_level(3) ×
  MONO16 factor(256) = 768`.
- Saved observation presets recorded `analog_gain: 13.979400086720377` despite
  the user intending `0`.
- In **live** mode, any exposure above ~0.1 ms "blew out" the camera, forcing
  the user to 0.1 ms.

## Root causes (all confirmed on hardware)

### 1. Dark frames = LED brightness driven at ~1% (the dominant cause)

Illumination intensity is **0–100 percent** everywhere in the runtime
(`IlluminatorState.intensity` is documented `0–100`, `ChannelState`, `set_intensity`,
and `SciMicroscopyLEDArray.set_brightness` all use 0–100). The presets stored
`intensity: 1.0`, which is **1 %**, not "full". `set_brightness(1.0)` →
`int(255 × 1.0/100) = 2/255 ≈ 0.8 %` drive. Combined with a 0.1 ms exposure, the
sensor saw essentially nothing.

Hardware confirmation (BF full green array, gain 0):

| brightness | 5 ms mean-ped |
|-----------:|--------------:|
| 1 %  | 166 |
| 5 %  | 959 |
| 25 % | 4933 |
| 50 % | 9720 |

Signal scales cleanly with both brightness and exposure — the camera/dome were
always healthy; the presets were just driving ~1 % brightness at 0.1 ms.

### 2. `BF_single` used the single-LED `sg` pattern (intentional)

`sg` → `single_led: 0` → one on-axis green LED (serial `l.0`). This is the
**intended** pattern for `BF_single` (a near-collimated on-axis reference), but a
single LED is intrinsically ~20× dimmer than the half-array modes and needs
much more brightness/exposure. (Earlier the array `bf_full` was wrongly tried —
see "Pitfalls".)

### 3. Analog gain "stuck" at 13.979 = stale captured default + dropped enforcement

`13.979 = 20·log10(500/100)` — the Toupcam's **power-on default raw gain 500
(≈5×)**. The preset *save* path snapshots live hardware
(`get_analog_gain()`), so it captured whatever the camera held. Historically
`LiveController.set_microscope_mode` force-wrote the stored gain on every mode
switch; that per-channel enforcement was dropped when gain ownership moved to
`ObservationStateController`, so the camera sat at its raw-500 default and every
snapshot recaptured 13.979. The value is self-perpetuating because
`general.yaml` (the live cache restored at startup) also held 13.979.

There is **no clamping bug** — the logged `min_gain=0.0` confirms 0 is in range;
the camera simply was never told to go to 0 before a snapshot.

### 4. Live "blowout" = the stuck 5× gain, not exposure

At `bf_g` 30 %, software trigger:

| exposure | gain 0 (mean-ped) | gain 13.979 (mean-ped) |
|---------:|------------------:|-----------------------:|
| 1 ms  | 1 247  | 6 180 |
| 5 ms  | 5 916  | 29 438 |
| 10 ms | 11 817 | 53 280 (**37 % saturated**) |

Exactly 4.98×. At the stuck 5× gain, ≥5 ms saturates — which is why live "blew
out" above ~0.1 ms. With gain correctly at 0, live runs ~10 ms+ cleanly.
Continuous (live) mode gives identical signal to software trigger; there is no
continuous-mode-specific effect.

## Hardware-validated settings (gain 0, MONO16, HCG)

The camera powers up in **HCG** conversion gain (it does **not** support
`high_fullwell` — the flag is absent on this model; the capability guard
correctly ignores it). All numbers below are at gain 0, HCG.

| Channel | mode | brightness | exposure | mean-ped | notes |
|---|---|---:|---:|---:|---|
| `BF_single` | `sg` (1 on-axis LED) | 100 % | 40 ms | ~400 | dim but clear structure; single LED → negligible current, 100 % safe |
| `BF_left/right/top/bottom` | half modes | 30 % | 10 ms | 3.7k–7.6k | real DPC structure |

**Dome current limit:** the SCI dome enforces a 10 A ceiling. The full green
array at 100 % draws 15.86 A (rejected); ~50 % is the safe max for the green
full array. **White `bf_full` at 30 % draws 14.18 A and is rejected** — do not
use `bf_full` (white) for full-array BF on this dome; use `bf_g` (green) which
matches the green DPC halves and stays within the limit.

To re-tune for a different sample: drive the dome (`set_matrix_mode` →
`set_intensity` → `turn_on`) and sweep exposure/brightness, picking a value whose
`max` stays below ~50k (headroom) while the region of interest is well above the
~6-count read-noise floor.

## Fixes applied

**Data** (`software/user_profiles/Heeseok/`):

- `observation_presets/BF_*.yaml` and `channel_configs/general.yaml`:
  `analog_gain`/`gain_mode` `13.979 → 0.0`; `intensity 1.0 → 30` (halves) / `100`
  (single-LED); `exposure 0.1 → 10 ms` (halves) / `40 ms` (single-LED);
  `camera_mode LCG → HCG` (matches the camera default and the validated values).
- `BF_single` keeps its intentional `sg` single-LED mode.

**Code** (`software/control/core/observation_state_controller.py`):

- Added a cached `_camera_supports_analog_gain()` capability probe
  (`get_gain_range().max_gain > min_gain`) and gated the per-state gain apply on
  it, so analog gain is **applied wherever the option exists** and is a clean
  no-op on gainless cameras (honoring the request "set analog gain if the option
  is available on the camera"). Cached so it adds no per-capture hardware
  round-trip.
- Removed the redundant exposure/gain writes in
  `apply_observation_state_preset` — `apply_full_observation_state` (always
  called) is now the single authority for exposure + (gated) gain; only
  `pixel_format` remains unique to that block.

**Camera** (`software/control/camera_toupcam.py`):

- **Disable SDK auto-exposure at init** (`set_auto_exposure(False)`). This is the
  root cause of the recurring live blowout *and* the gain reverting to 13.979.
  Touptek defaults `AutoExpoEnable=1`; in CONTINUOUS (live) mode the SDK ramps
  ExpoTime 10 ms → 350 ms and ExpoAGain 1× → 500 (=13.979 user units) chasing
  `AutoExpoTarget=120` — "first frames dim, then saturates." It also silently
  drove gain to 500, which the live-state cache then persisted. Software-trigger
  mode does not auto-ramp (why earlier SW-trigger probes looked stable).
  Verified: AE on → ramps to 350 ms/5×; `put_AutoExpoEnable(0)` → holds 10 ms/1×.
- **`default_analog_gain`** config (set to `0` in the toupcam machine configs),
  applied in `_configure_camera`, so the sensor baselines at unity gain instead
  of its ~5× power-on default — defense for the bootstrap-from-hardware path that
  otherwise reseeds 13.979 when `general.yaml` is missing.
- **Strobe recalc robustness**: `_update_internal_settings` skips the strobe
  calc when the stream is stopped (the SDK can't read `MAX_PRECISE_FRAMERATE`
  then → `E_UNEXPECTED` "Catastrophic failure" on any camera-state change after
  CONTINUOUS live) and marks `_strobe_dirty`; `start_streaming` refreshes it when
  the stream restarts, so settings changed while stopped recalc correctly.
- `TOUPCAM_OPTION_HIGH_FULLWELL` is applied at init only when the model supports
  it (`has_high_fullwell` capability) — this model does not, and it is correctly
  skipped with a warning.

**Multipoint → live binning/ROI reset** (`software/control/core/multi_point_controller.py`):

- Symptom: after a multipoint run, live returns at **2×2 binning** (the config
  default) with the **ROI cropped to a weird location**, even if live was 1×1
  before the run.
- Cause: an asymmetry in the acquisition's camera setup/teardown.
  `_seed_camera_for_first_observation_state` applies the **first observation
  preset's** `camera_live` snapshot at acquisition start, which sets
  binning/ROI/pixel_format/camera_mode to the preset's values. The teardown
  (`_restore_state_after_acquisition`) only called
  `apply_full_observation_state(prior_config)`, which restores exposure / gain /
  illumination / optical-path but **not** geometry — so the camera was left in
  the preset's resolution and ROI. (The "weird location" is the preset's saved
  ROI applied over the wrong resolution.)
- Fix: capture a `CameraLiveSnapshot` of the live camera
  (`obs_controller._collect_camera_live_snapshot()`) at acquisition start and
  re-apply it via `_apply_camera_live_snapshot(..., apply_trigger_settings=False)`
  at the end — the symmetric counterpart of the seed step. Trigger mode is
  restored separately, so trigger settings are skipped in the geometry restore.
  This is camera-agnostic but was observed on Toupcam.

**Y-flip (frames mirrored vs real-space optics)**:

- The Toupcam sensor reads out **Y-flipped** relative to the real-space optical
  image. Corrected in **software** via the generic `flip` config
  (`devices.main_camera.config.flip: Vertical`), applied in
  `AbstractCamera._process_raw_frame` (`utils.rotate_and_flip_image`) for the
  normal path — live view and software-trigger multipoint.
- The **fast-acquisition** path bypasses `_process_raw_frame` (it works on raw
  bytes for performance), so `toupcam_raw_bytes_to_np` calls a new
  `_apply_config_flip` helper to apply the same flip — keeping every path's
  orientation identical (the same idea as Tucsen's `_build_byte_decoding_fn`
  software Y-flip for its raw DataCallBack path).
- Why `flip` and not `reverse_x`/`reverse_y`: those are **SDK/sensor mirror**
  flags wired **only to the Tucsen driver**. Toupcam pulls **RAW** on every path,
  and the Touptek SDK's `put_VFlip`/`put_HFlip` act on the ISP/processed pipeline,
  not raw pulls — so they'd be no-ops here. Software `flip` is the reliable
  mechanism for this camera. Only `Vertical` is applied (X is already correct).

## Pitfalls / notes for future work

- **Intensity is 0–100 %, never a 0–1 fraction.** Presets authored with `1.0`
  meaning "full" are silently 1 %. Save presets via the GUI (stores 0–100) or use
  percent values.
- The preset's `camera_mode`/ROI/binning are **not** re-applied per channel in
  multipoint (`apply_camera_live_snapshot=False`), so the camera runs in whatever
  conversion-gain mode it booted in (HCG here). If deterministic LCG/HCG per
  channel is needed, pin `camera_mode` explicitly.
- Multipoint currently forces **software** trigger
  (`multi_point_controller`), so `hardware_triggering_enabled: true` has no
  effect on capture today. The Toupcam HW-trigger strobe-repush path has a latent
  guard (`set_acquisition_mode` early-return) that should be revisited before
  wiring HW trigger into multipoint.
