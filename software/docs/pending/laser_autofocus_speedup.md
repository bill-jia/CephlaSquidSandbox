# Laser Autofocus Per-Event Speedup

## Context

During multipoint acquisition on the Tucsen + SQUID rig, `perform_autofocus` runs once per position. Measured cost on a 9-position instrumented run: **mean 228 ms / event, ~2.05 s total** (~19 % of `run_single_time_point` at this scale; projects to ~9 s on a 27-position run).

An earlier plan proposed a `fast` kwarg on `move_to_target` (single-frame verify + consolidate laser toggles). The timer breakdown invalidated it — camera and CV are ~10× cheaper than initially estimated, so the savings would be ~36 ms / event = ~1 s / run. Not worth a dual-path maintenance cost.

## Measured breakdown (per event)

| Sub-step | Measured |
|---|---|
| `af:move_z` | **123 ms mean, 54 ms median, up to 283 ms** |
| `af:spot_centroid_loop` × 2 (measure + verify) | 48 ms total |
| `af:get_new_frame` × 6 | 28 ms total |
| `af:find_spot_location` × 6 | 12 ms total |
| Laser toggles × 4 | 46 ms total |
| overhead | ~10 ms |
| **`af:move_to_target` total** | **228 ms** |

The dominant cost is the **stepper Z move** — `stage.move_z` → MCU `wait_till_operation_is_completed` + 20 ms `SCAN_STABILIZATION_TIME_MS_Z` settle + **backlash compensation on downward moves** (`software/squid/stage/cephla.py:78-149`). The 54 ms median vs 123 ms mean is consistent with upward (no backlash) vs downward (extra 5 µm excursion) moves alternating.

## Approach

**Reuse the existing focus-map infrastructure** — wire it into the laser-AF path, where it currently has no effect.

### What already exists in the codebase

- `AutoFocusController.use_focus_map` / `focus_map_coords` (`software/control/core/auto_focus_controller.py:47-48`).
- `utils.interpolate_plane(*focus_map_coords[:3], (x_mm, y_mm))` — 3-point barycentric plane fit (`software/control/utils.py:212`).
- `AutoFocusController.autofocus()` already short-circuits when `use_focus_map` is True: interpolates target_z, calls `self.stage.move_z_to(target_z)`, done. No image-based focus at all (`auto_focus_controller.py:60-73`).
- `FocusMapWidget` in `software/gui/widgets/napari_views.py` — add/remove points, surface-fit method (spline / rbf / constant), smoothing, grid generation, import/export.
- `software/gui/widgets/multipoint.py:691-1073` exposes "Generate AF Map" and "Use Focus Map" in the UI.

### What's missing

`MultiPointWorker.perform_autofocus` (~line 1465) always calls `laser_auto_focus_controller.move_to_target(0)` when `do_reflection_af` is set. It **never checks `self.autofocusController.use_focus_map`**. So the existing UI toggle has no effect when laser AF is the active method.

## Plan — Phase A (primary)

Add a short-circuit at the top of `MultiPointWorker.perform_autofocus`: if `self.autofocusController.use_focus_map` is True and `focus_map_coords` has ≥ 3 points, interpolate `target_z` and `stage.move_z_to(target_z)` directly. Skip `laser_auto_focus_controller.move_to_target(0)` entirely. Mirror the existing contrast-AF pattern.

**Savings / position**: `af:move_to_target` 228 ms → `stage.move_z_to` ~60 ms. **~170 ms / position.**

On a 27-position run: **~4.6 s saved** (from the ~9 s AF total).

**Reversibility**: controlled by the existing `use_focus_map` UI toggle. No new flag needed — the legacy path is what runs when the toggle is off. The flag is user-visible and persistent, which matches the "fast path behind a flag" preference.

## Plan — Phase B (optional, deferred)

Per-well anchor refresh — re-anchor the focus map at each timepoint (or each well entry) by running one laser AF measurement and applying its displacement as a Z offset to all map-predicted Z values for that anchor's scope. Keeps the map accurate under drift without paying for AF at every position.

Not needed for the initial implementation. Add only if measured drift warrants it (see verification step 4).

## What we do NOT do (and why)

- **AF during XY stage motion.** Breaks silently on tilted plates (common in biology — meniscus, stage tilt, coverslip wedge cause 1-5 µm Z variation across 500 µm XY). Requires stage-motion model + threading. Strictly worse than the focus-map approach for flat samples (same savings, more risk). Cross-correlation verify would catch the failure but then you lose the savings.
- **Original Phase 2** (single-frame verify + consolidate laser toggles behind a `fast` kwarg). Would save ~1 s / run with a maintenance cost; shelved. If per-well anchor AF is adopted later and anchor frequency is high, revisit.

## Files to modify

| File | Change |
|---|---|
| `software/control/core/multi_point_worker.py` | In `perform_autofocus`, before dispatching to laser AF, check `self.autofocusController.use_focus_map` and `len(self.autofocusController.focus_map_coords) >= 3`. If true, compute `target_z = utils.interpolate_plane(*coords[:3], (pos.x_mm, pos.y_mm))` and call `self.stage.move_z_to(target_z)`. Return success. |

Single-file change, ~10 LoC. No API surface changes.

## Verification

1. **Smoke — map disabled (default path)**: run the same 9-position multipoint used for prior benchmarks. Confirm `af:move_to_target` still fires, `run_single_time_point` unchanged vs the Phase-1-timer-only baseline. No regression.
2. **Map enabled, 3-point manual map**: in the UI, seed a 3-point focus map (3 well positions spanning the scan area). Enable "Use Focus Map". Re-run the 9-position multipoint. Expect:
   - `af:move_to_target` does **not** appear in the timing report (laser AF bypassed).
   - A new timer `perform_autofocus` per position is ~60 ms mean (just the Z move) instead of ~228 ms.
   - `run_single_time_point` drops by ~1.5 s on 9 positions (~170 ms × 9).
   - Image focus quality is acceptable across all positions — spot-check in saved Z-stack or single-plane images.
3. **Map enabled, tilted plate**: seed 3 points spanning actual plate corners (or deliberately use a tilted sample); confirm focus is maintained across the scan. A plane fit captures tilt; a visibly blurry image at an interior position indicates the 3-point plane assumption is too simple and we need the existing `FocusMapWidget` spline / rbf path (which already exists, we'd just wire it up).
4. **Time-lapse drift check**: if running multi-timepoint, compare focus quality at T=0 vs last timepoint. If visibly degraded, that's the signal to implement Phase B (per-well anchor refresh).

## Open question

The existing focus-map `interpolate_plane` path uses the first 3 points only. `FocusMapWidget` also supports spline / rbf / constant fits for larger point sets. Open whether to:
- start with the 3-point plane (minimum viable, matches current contrast-AF behavior), or
- wire in the widget's higher-order fit directly from the start.

Default recommendation: start with 3-point plane; the higher-order fit is an easy follow-up once we validate the integration end-to-end.

## Related instrumentation already in place

`LaserAutofocusController` has an optional `_timing` attribute + `_time(name)` helper. `MultiPointWorker.run()` attaches its TimingManager at acquisition start and detaches on finish, so the `af:*` rows appear in the report. These cost nothing when `_timing` is None, so they stay enabled in production. Reuse them when verifying Phase A.
