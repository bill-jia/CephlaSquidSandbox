# Laser Autofocus

How the reflection-based laser autofocus works, what can make it fail, and how
the routine defends against those failures. Code:
`control/core/laser_auto_focus_controller.py` (controller),
`control/utils.py::find_spot_location` (spot detection),
`control/models/laser_af_config.py` (per-objective config),
`control/models/laser_af_reference.py` (per-region focus targets).

## Principle

A near-IR laser reflects off the sample interfaces onto a dedicated focus
camera at an oblique angle, producing one or two spots whose **x position moves
linearly with stage z**. The per-objective calibration stores that linearity as
`pixel_to_um` (µm of z per pixel of spot motion). A *reference* — the spot's x
position (plus a small crop of the spot image) captured at the in-focus plane —
defines displacement zero. Focusing is then: measure the spot, convert
`(x − x_reference) · pixel_to_um` to a displacement, and move z to cancel it.

## The correction loop (`move_to_target`)

1. Measure displacement. Fail out if it can't be measured or exceeds
   `laser_af_range` (default ±100 µm).
2. Move z by the residual, re-measure, repeat until within
   `displacement_success_window_um` (up to `MOVE_TO_TARGET_MAX_ITERATIONS`).
   The loop is **closed**: with an accurate `pixel_to_um` it converges in one
   iteration; with a moderately wrong scale (< 2×) it converges geometrically.
3. **Divergence guard**: a correction that doesn't shrink the residual means
   the configured `pixel_to_um` doesn't match the spot's true response. The
   loop aborts, rolls z back to the starting position, and logs the *implied*
   scale (`configured · commanded/measured-response`) so the operator knows
   what to recalibrate to.
4. Cross-correlation verify: the spot image around `x_reference` is compared
   with the reference crop (`correlation_threshold`). Failure rolls z back.
   A NaN correlation (blank crop) counts as failure.

The AF laser is held on across the whole loop (one on/off pair instead of one
per measurement — each MCU toggle costs ~10 ms).

## Spot measurement (`_get_laser_spot_centroid`)

- **Fresh frames enforced.** `get_new_frame` compares camera frame ids so a
  buffered frame from before the trigger — or before a preceding z move — is
  never accepted. (`DefaultCamera.read_camera_frame` has a stale-frame fast
  path whose window includes a full-sensor strobe estimate of ~22 ms; without
  the id check, "averaging" reads one frame repeatedly and post-move
  measurements can see pre-move data.)
- **Median of `laser_af_averaging_n` frames**, robust to one artifact
  detection. A warning is logged if per-frame detections scatter by more than
  `x_window` pixels.
- **Search windowed around the reference** (when one is set): only spots
  within 1.5× `laser_af_range` (plus `spot_spacing` for the companion spot)
  of `x_reference` are considered. The `DUAL_LEFT`/`DUAL_RIGHT` modes take the
  leftmost/rightmost detected peak, and the x profile is normalized before
  peak-finding — so without the window, any reflection or noise elsewhere in
  the frame wins whenever the true spot is dim or displaced. Callers that
  establish a new reference (`capture_reference`, `initialize_auto`,
  calibration) search full-width.
- **Absolute intensity floor** (`min_spot_intensity`): frames whose maximum is
  below it are reported as spot-absent (laser off / blocked) instead of
  running peak detection on normalized noise, which always "finds" something.
- On total detection failure the last frame is saved to
  `<log dir>/laser_af_debug/` (newest 20 kept) so field failures are
  diagnosable after the fact.

## Calibration (`_calibrate_pixel_to_um`)

Sweeps `calibration.positions` (5) z offsets spanning the effective sweep
distance for the current objective and least-squares fits x(z):

- The sweep descends once to the lowest offset (that move is
  backlash-compensated by the stage) and then only steps **upward**, so every
  sample is approached from the same direction. The old two-point scheme
  (−d/2 then +d) reversed direction mid-sweep, letting backlash/settling
  compress the measured pixel span and inflate the scale several-fold.
- Acceptance gates: ≥3 detected positions, total spot motion ≥
  `calibration.min_total_px` (5 px), fit R² ≥ `calibration.min_r2` (0.90).
  A bad sweep **fails loudly** instead of writing a garbage scale that later
  wrecks every `move_to_target`.
- A sweep with essentially zero spot motion (< `calibration.simulation_px`,
  0.5 px) is treated as a simulated camera and falls back to the canned scale
  (0.4).

### Sweep distance scales with magnification

The AF laser goes out through the objective, reflects off the sample interface
and comes back through it, so the spot's lateral travel per µm of defocus
scales with the objective: a 10x objective moves the spot roughly half as far
per µm as a 20x one. A sweep tuned at 20x is therefore too short at 10x — a
6 µm sweep that gave a comfortable fit at 20x produced only ~3.3 px at 10x and
calibration failed the 5 px floor.

The sweep is machine policy, not a per-objective measurement, and lives in the
machine config under `devices.laser_af.config.calibration` (typed as
`LaserAFDeviceSettings` / `LaserAFCalibrationSettings` in
`control/models/machine_config.py`):

```yaml
devices:
  laser_af:
    config:
      spot_detection_mode: dual_left    # default for objectives with no saved config
      calibration:
        distance_um: 6.0                # sweep at reference_magnification
        reference_magnification: 20     # the objective distance_um was tuned for
        max_distance_um: 30.0           # clamp on the scaled sweep
        positions: 5                    # z samples across the sweep
        min_total_px: 5.0               # acceptance floor on total spot travel
        min_r2: 0.90                    # acceptance floor on fit linearity
        simulation_px: 0.5              # below this, assume a static (simulated) image
```

The sweep actually used is

```
effective_um = min(distance_um * reference_magnification / magnification, max_distance_um)
```

so 20x keeps today's 6 µm, 10x gets 12 µm, 40x gets 3 µm, and 4x and below are
held at the 30 µm clamp (a sweep much larger than that leaves the spot's linear
region and risks driving z far off focus). If the magnification can't be
determined — no objective store, objective missing from `objectives.csv`, a
zero/absent magnification — the unscaled `distance_um` is used. The computed
sweep is logged at the start of every calibration, and the laser AF settings
panel shows it read-only next to the other spot settings.

Every key is optional: a machine config with no `calibration:` block (or no
`laser_af` device at all) gets the model defaults above, which are the values
that were previously hardcoded in `laser_auto_focus_controller.py`.

**A wrong `pixel_to_um` is nearly invisible in live use**: "Set Reference"
then "Move To Target" at the same spot measures ≈0 displacement, commands ≈0
motion, and verifies trivially. It only bites when a real correction is
commanded — i.e. during multipoint acquisitions — where an overshoot throws
the spot outside `laser_af_range` and every AF event fails. If the divergence
guard fires with an implied scale far from the configured one, recalibrate
(and consider raising `devices.laser_af.config.calibration.distance_um` so the
fit has enough pixels).

## Multipoint integration

`MultiPointWorker.perform_autofocus` applies the region's focus reference
(`apply_reference`) and calls `move_to_target(0)` per the refresh cadence;
failures fall back to the per-region z table (see
`docs/user_guides/multipoint.md`). References are ROI-relative
(`LaserAFReference`); the per-objective YAML cache stores the absolute-sensor
x and adjusts on load.

## Failure modes this design was hardened against (2026-07 incident)

One acquisition session showed every multipoint AF event failing while live
"Move To Target" worked. Contributing defects, all fixed:

1. `pixel_to_um` calibrated ~5× too high from an 8.6 px two-point sweep →
   first real correction overshot ~5×, spot rejected as out of range, AF
   failed at every FOV. → multi-point one-directional fit + gates + runtime
   divergence guard with rollback.
2. Stale/duplicate frames defeating averaging and post-move measurement →
   frame-id-enforced fresh frames.
3. `DUAL_RIGHT` latching onto artifacts near the frame edge (random y,
   1200–1500 px) once the true spot was displaced → reference-windowed search
   + intensity floor + median.
4. Config detection parameters silently ignored (passed under wrong key
   names: `peak_*` instead of `min_peak_*`) → keys fixed; per-objective
   `min_peak_*` values now actually apply.
5. `turn_on/off_AF_laser` infinite recursion when no IO endpoint was
   configured → routed to the MCU methods.
6. Focus-camera callbacks permanently disabled after the first measurement →
   saved/restored around the centroid loop.
7. NaN cross-correlation passing the verify (nan < threshold is False) →
   explicit finite check.
