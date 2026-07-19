# Autofocus Speed & Accuracy Improvement Proposals

2026-07-18. Produced by a multi-agent review: 5 subsystem readers, 4 proposal
lenses (speed / accuracy / robustness / literature), 30 raw proposals deduped to
18, each adversarially verified line-by-line against branch
`worktree-zarr-upload-debug`. Impact numbers below are the **verifier-corrected**
estimates, not the original claims. Complements (does not replace)
`docs/pending/laser_autofocus_speedup.md` (focus-map short-circuit plan, still
the single largest laser-AF speed win at ~170 ms/position).

---

## Bugs found during review — fix regardless of which proposals proceed

> **Status (2026-07-18, branch `worktree-af-bugfix-batch`)**: all seven bugs
> below are fixed, and the table-path audit (Proposal 1's validation
> instrument) is wired behind a "Table-path audit" checkbox in the laser-AF
> settings dialog. The fix batch also closed four adjacent defects the
> verification pass surfaced: the contrast-AF cadence counter now resets per
> timepoint (a counter carried across timepoints phase-locks when the
> per-timepoint FOV total is divisible by 3, permanently exempting ⅔ of FOVs
> from AF), the refresh cadence off-by-one is fixed (period was N+1; Legacy
> N=1 alternated refresh/table instead of refreshing every FOV), contrast-AF
> proposals now cache the post-AF focused z instead of freezing at the t=0
> arrival z, and stale laser-AF fallbacks no longer count as refreshes for the
> consistency/end-of-region checks. Untested on the rig.

1. **Contrast-AF cadence counts z-slices, not FOVs.** `af_fov_count` increments
   inside the per-z loop (`multi_point_worker.py:4044`) while the
   every-`NUMBER_OF_FOVS_PER_AF` gate reads it per FOV (`:4198-4201`). Firing
   period is `P/gcd(NZ,P)`: with default P=3, **NZ=3 or 6 runs a full 1.5–3 s
   contrast sweep at every FOV** (~10–20 min wasted per timepoint on an 864-FOV
   NZ=3 run). Can only make AF denser, never sparser — pure waste, not an
   accuracy risk. Fix: increment once per FOV after the z loop.
2. **`deltaZ` unit hazard.** `AutoFocusController.__init__` stores `1.524`
   as mm without the `/1000` that `set_deltaZ` applies
   (`auto_focus_controller.py:43`); only the GUI widget's `set_deltaZ(1.524)`
   call rescues it. A headless caller (`tools/microscope_stress_test.py:201`
   constructs one today) would sweep **±7.6 mm** — a physical crash hazard.
3. **Illumination leak on None frames.** In the sweep loop a `None` frame hits
   `continue` before `turn_off_illumination` (`auto_focus_worker.py:94-98`),
   leaving the BF matrix on through the next move — and after the sweep if the
   last frame is None. Fix: try/finally.
4. **Scan-mode seed data never applied at t=0.** The seed pre-pass measures
   per-FOV z for every FOV, but `move_to_coordinate` applies `_z_pos_proposal`
   only when `time_point > 0` (`multi_point_worker.py:2699`) and the table
   branch's z move is commented out (`:4276-4282`). At t=0 with cadence 10,
   FOVs 1–9 of each well image at the anchor's z, not their measured z. ~2-line
   fix: gate on `(region_id, fov) in self._z_pos_proposal` instead.
5. **Failed region-entry refresh strands 10 FOVs on a stale anchor.** On
   failure the counter is left at 1 (`:4507`), so the next re-attempt is 10
   FOVs away while the fallback anchor may be minutes old. 1-line fix: set
   `_fovs_since_refresh[region_id] = refresh_every_n` on failure so the next
   FOV retries (mirrors the existing mid-region self-retry).
6. **Single-region timelapses never re-anchor at timepoint boundaries.**
   `_last_region_id` never resets per timepoint (only `:4244/4293`), so a
   single-region run's anchor goes multi-timepoint stale. ~3-line fix: reset in
   `run_single_time_point`.
7. **Dead `or use_focus_map` clause** in the contrast-AF dispatch
   (`:4207-4209`) — the outer condition already requires the cadence hit, so it
   can never fire; dead since its introduction (upstream cf599f01, Dec 2024).
   Either make it a real branch (focus-map move is one cheap `move_z_to`,
   worth running every FOV on tilted samples) or delete it.

Also noteworthy: `move_to_target`'s correction loop re-measures immediately
after `_move_z` with **no settle** (`laser_auto_focus_controller.py:488-491`;
settle exists only in calibration) — ringing-contaminated measurements are an
open exposure that Proposal 6's scatter gate (or a 20 ms in-loop settle) covers.

---

## Tier 1 — Strong (verified, recommended)

### 1. Backlash-consistent Z landing for table-path FOVs — accuracy, medium

**Defect.** `CephlaStage.move_z_to` skips backlash compensation for
non-blocking calls (`squid/stage/cephla.py:130`), and the multipoint table path
lands Z exactly that way (`multi_point_worker.py:2699-2708, 2746-2749`).
Refresh FOVs are physically closed-loop (re-measured until within the 1.0 µm
window), so the two FOV families rest on **opposite flanks of the Z drive's
reversal error** — a systematic offset bounded by the 5 µm compensation
distance (plausibly 1–3 µm) on every down-approached table FOV. With cadence
n, ~40–50 % of fast-mode NZ=1 FOVs carry an offset comparable to or exceeding
the 1.0 µm AF success window. Invisible to any AF tuning because the table path
takes no measurement. (FROM CENTER z-stacks self-heal via `prepare_z_stack`'s
blocking down-move; impact is NZ=1 — the dominant case — plus FROM BOTTOM/TOP
and piezo stacks.)

**Fix.** Two-phase approach-from-below split across the XY travel that already
blocks: when target_z < current_z, issue a non-blocking move to
(target_z − 5 µm) before the XY move; after XY settle, one short blocking
up-move (~10–40 ms, mostly hidden). Implement phase 2 inside
`_wait_for_move_settled` (`:2725-2742`, the single join point) with a
`get_pos()`/wait-for-idle guard so it never retargets a still-descending stage.
Fix bug 4 (t=0 gap) in the same change — it dwarfs the backlash error.

**Validate first**: re-enable the dormant `_check_table_path_displacement`
audit (`:4291`) for one tilted-plate run to measure the actual reversal error;
if < ~0.5 µm on the target rigs, deprioritize.

### 2. Event-driven MCU completion packets — speed, medium (firmware)

**Defect.** Command completion is only learned from the 10 ms periodic status
packet, and motion-done detection itself polls at 10 ms
(`firmware/controller/src/constants.h:110-111`) — so **every MCU command
carries ~10 ms mean / 20 ms max of pure reporting latency**. Illumination/pin
commands (including the AF laser toggle) are atomic in firmware; their entire
~11 ms measured cost is packet-cadence wait.

**Fix.** (a) Force an immediate status packet after each command callback in
`process_serial_message` (set `us_since_last_pos_update` past the interval);
(b) force a send at every true→false transition of
`mcu_cmd_execution_in_progress` via a central `clear_busy_and_flag_send()`
helper (transition sites: `check_position`, `check_limits`,
`finalize_homing_*`); (c) drop `interval_check_position` 10 ms → 1 ms (cheap
TMC4361A SPI read). Keep the 10 ms heartbeat. **Host needs zero changes** —
`wait_till_operation_is_completed` already blocks on a per-packet condition
variable, and duplicate packets are idempotent.

**Impact (verified).** ~45–65 ms per laser-AF event (20–28 % of the 228 ms
mean); ~35–45 s on an 864-FOV seed scan; and fleet-wide, every illumination
toggle and XY/Z move in ordinary acquisitions gets ~5–10 ms back — tens of
seconds per plate, on top of AF. Cost is operational: reflash every rig Teensy;
bench with `tools/microcontroller_stress_test.py` first.

### 3. Contrast-AF result object: failure detection + stranded-z protection — accuracy, medium

**Defect.** `AutofocusWorker.run_autofocus` has no try/except: any exception
kills the thread mid-sweep, leaves the stage up to 7.6 µm below start (the
rewind never runs), and `multi_point_worker.py:4212` still records
`_last_af_status='ok'` unconditionally. Soft failures are worse because more
frequent: None-frames leave zeros in the curve (all-None → argmax lands ~6 µm
low, status 'ok'), and empty wells always "find" focus on noise. A dead AF
thread also poisons `gen_focus_map`, which feeds every interpolated move.

**Fix.** Return an `AFResult(status, idx, curve, error)`: wrap the sweep
(BaseException-aware — `CameraTimeoutError` derives from BaseException by
design); on failure restore z from an absolute pre-sweep snapshot
(`get_pos().z_mm` + `move_z_to`, not commanded-step arithmetic); add quality
gates on the already-collected curve — peak/median ratio (`no_contrast` for
empty wells), edge detection on the last *measured* index, count of
None-frames — gates computed on the measured prefix only (early-stop and skips
leave zeros; naive peak/median divides by ~0). Plumb the real status to the
worker and `autofocus_log.csv`, replacing the unconditional 'ok'. Ship gates
warn-only first. Also make `_on_autofocus_completed` failure-safe: it calls
`camera.enable_callbacks(True)` before clearing `autofocus_in_progress`; if
that raises, `wait_till_autofocus_has_completed` spins forever.

Zero happy-path cost; brings contrast AF into the ok/stale/failed audit
taxonomy the laser branch already has.

---

## Tier 2 — Viable (worth doing, with the stated caveats)

### 4. Cadence-counter fix + real focus-map branch — small
Bugs 1 + 7 above. Removes ~2/3 of contrast sweeps on NZ∈{3,6} runs
(~10–20 min/timepoint) and gives focus-map users per-FOV plane-following.
Laser-AF runs (this lab's primary path) are unaffected — it keys on
`_fovs_since_refresh`, not `af_fov_count`. Release-note the NZ=3 change.

### 5. Contrast sweep overhaul — medium, split in two
Land unconditionally: parabolic sub-step vertex on log(focus) at the current
1.524 µm grid (quantization ±0.76 µm → ~0.1–0.3 µm; must require all 3 points
measured, > 0, strictly concave in log space), single backlash-compensated
direct final move (replaces the 2-move retrace; preserves approach-from-below),
compute overlapped with the next non-blocking up-move, illumination held on
across the sweep **only when the channel is the BF matrix** (manual AF runs on
the live channel, which may be FL — per-capture off is load-bearing there), and
bugs 2 + 3. Separately flagged: coarser grid (N=6 × ~2–3 µm) — the only part
that can regress accuracy (argmax fallback on a 3 µm grid is ±1.5 µm, and
high-NA peaks with 1–2 µm FWHM can be straddled). Verified saving: ~0.5–1.0 s
of the measured 1.3–1.9 s event with the coarse grid; ~0.3–0.4 s without it.
Brenner focus measure as opt-in enum value; lowest-value item, don't let it
gate the rest.

### 6. Laser-AF measurement validity gates — small, accuracy tail
Today `_get_laser_spot_centroid` accepts a 1-of-3 "median", warns only at 20 px
(≈8 µm) scatter against a **1.0 µm** success window, has no saturation
handling, and fixed 0.2 ms exposure. Add: detection quorum (≥2 of 3), tight
scatter gate (~3 px) with a conditional settle-and-retry (accept only the fresh
burst — zero cost on clean bursts, and it covers the settle-free re-measure
exposure in `move_to_target`), one-shot exposure retry at 4×/¼× for dim/
saturated spots (guard the camera setter — Toupcam-hang lesson), and spot
width/amplitude columns in `autofocus_log.csv`. Scope gates to
`restrict_to_reference=True` calls only (calibration/initialize_auto stay
permissive). Demote the width-band gate to log-only until field data shows
separation. Reliability-tail win, not throughput.

### 7. Extend the SDK watchdog to the Daheng focus camera — medium, insurance
The 2026-07-02 hang class on the *other*, still-unprotected camera:
`DefaultCamera` control calls are raw ctypes with no timeout, executed ~27
times per AF event (~10⁵ per plate run). Wrap in the existing
`BoundedSdkCaller` (15 s timeout per Toupcam precedent — ROI setters
legitimately exceed 2 s), catch `CameraTimeoutError` at `_run_laser_af_refresh`
(+ `_check_last_fov_displacement`, characterization, GUI entry points — with
(a) alone, the generic handler would reopen the **wrong** camera and abort on
Tucsen rigs), one-shot reopen mirroring `_recover_wedged_camera`, then degrade
to table/stale for the rest of the run. No recorded gxipy wedge yet (severity
proven, probability unknown; docs note chronic focus-camera read timeouts).
Sibling task the review flagged: the Tucsen **main** camera has the same gap
with per-frame exposure.

### 8. pixel_to_um calibration hygiene + drift watchdog — medium, reframed
The historical 5× miscalibration is already root-cause-fixed (one-directional
sweep + R²/span gates) and gross error already trips the divergence guard on
event 1. Remaining real value: the calibration is 5 points over 6 µm applied to
±100 µm (**16× extrapolation**) and `calibration_timestamp` is never aged.
Stage 1: 9-point sweep over a per-objective 20–30 µm span, persist residual RMS
+ quadratic-term advisory, staleness warning at acquisition start; passively
collect the implied-scale ratio `move_to_target` already computes per iteration
(median over a deque; exclude NaN/rollback/piezo iterations, |move| ≥ 2 µm)
and surface >15 % drift through the existing Slack stats. Stage 2 (only if
field data shows drift): auto-recalibrate (~1–1.5 s) at the `new_region_entry`
boundary. Drop the in-memory scale adaptation and the contrast-AF cross-audit
(duplicates existing consistency checks). Insurance + observability, not
throughput.

### 9. Interface-aware dual-peak selection — medium, accuracy
`DUAL_RIGHT` blindly takes the rightmost peak inside a ±475 px window
(`utils.py:373-374`); `find_peaks` properties are computed and discarded, and
`self.spot_spacing_pixels=None` is an unused breadcrumb — the concept was
contemplated, never built. Record the pair spacing (z-invariant: set by
interface separation) + amplitude ratio at reference time (only when exactly 2
peaks; full-width search can poison it), select at measure time by
spacing-match (tolerance scaled by `pixel_to_um`, amplitude as tiebreaker
only), fall back to DUAL_RIGHT single-peak. Fixes persistent in-window
artifact-pair latching that the 2026-07 hardening's window+median can't (median
defeats one transient outlier, not a persistent second reflection; a
similar-looking wrong-interface spot can pass the 0.9 correlation). Known
limitation: single-visible-peak wrong-interface selection survives — consider
|x − x_reference| disambiguation for that case. Quantify first via the
`laser_af_debug` PNG corpus. Cleanup: delete dead `LASER_AF_SPOT_DETECTION_MODE`
(`_def.py:776`) and the breadcrumb.

---

## Tier 3 — Marginal as proposed; salvage the named pieces only

- **Adaptive refresh cadence** — headline collapses: region entry resets the
  counter AND forces a refresh, so on 96-well × 9-FOV plates interval growth
  saves ~0 s (the 96 × 228 ms of forced entry refreshes is the real cost — and
  that's the focus-map plan's target). Only pays on large mosaics
  (FOVs ≫ interval, ~10–13 s/timepoint). Salvage: none now; revisit for mosaics.
- **Gaussian sub-pixel spot refinement** — arithmetically dead for speed: the
  1.0 µm window is 8–12σ of post-median noise; iterations are driven by real z
  error. Salvage: plumb the hardcoded `intensity_threshold=0.1`
  (`utils.py:301`) into `LaserAFConfig` (trivial); revisit the estimator only
  as a prerequisite for tightening the success window on piezo rigs, after
  measuring actual scatter with a 100-frame static capture.
- **Verify-pass overhaul** — headline bugs don't exist (y is already
  full-image at `utils.py:471`; Pearson correlation is normalization-invariant;
  int() truncation is symmetric). Salvage: reuse the just-measured frame in
  `_verify_spot_alignment_with_laser_on` instead of the discarded-centroid
  re-capture (~24 ms/event, ~10 %, exact-equivalence, ~20 LoC).
- **Focus-camera ROI-aware strobe estimate** (`camera.py:227` TODO) — ~0 ms on
  the AF happy path (frame-id loop already exits on arrival). Salvage as
  correctness hygiene: compute rows from the actual ROI and recompute on ROI
  change; real beneficiaries are the frame-id-less readers (`get_image`,
  control server, hardware panel snapshots) that can today serve a pre-laser-on
  frame.
- **Per-FOV drift prediction (Theil-Sen)** — premise wrong: anchors are
  re-measured per region entry per timepoint and proposals re-seated, so table
  FOVs lag seconds, not a timepoint. Salvage: bugs 5 + 6 above, and an offline
  `autofocus_log.csv` analyzer (validate "delivered accuracy" claims via the
  `table_path_audit` instrument, not log rows that carry no measurement).
- **Retry ladder / k-NN fallback** — widened-search rung is provably killed by
  the `laser_af_range` guard; k-NN degenerates to current behavior in its own
  motivating case; dominant field failure (focus-camera read timeouts) untouched.
  Salvage: bug 5 (the 1-line counter fix) and, with failure-reason plumbing,
  the dim-sample exposure retry (already folded into Proposal 6).
- **Activate `_pending_move_settle` scaffolding** — the in-code comment records
  async moves were already benchmarked "a wash" (MCU serial contention); honest
  Phase-1 payoff is ~1 s/timepoint (fire-and-forget AF-laser-on during XY
  flight). The larger untapped win it points at: keep table-only FOVs (9 of 10)
  un-joined until the pre-trigger gate. Two stage reads (`worker:3735`, `:4548`)
  would need to move behind the join. Revisit only on NIDAQ-illumination rigs.
- **Continuous-motion contrast sweep (moonshot)** — buildable (free-run variant
  only; mid-move MCU triggers break the single-in-flight-command protocol), but
  the camera is the constraint (~15–30 fps ⇒ 20–45 µm/s, not 100–150), giving
  1.5–2.5× on the *secondary* AF path for large effort + per-event SDK
  reconfigures on a camera with a hang history. Useful piece regardless:
  hardware frame timestamps (Toupcam `FrameInfoV2.timestamp`, Tucsen
  `dblTimeLast`) are already delivered and discarded in favor of `time.time()`.

## Rejected

- **Kill the seed pre-pass / inline t=0 seeding** — the mechanism already
  shipped as the selectable **Lazy** seed mode (same savings, one click), and
  deleting scan mode contradicts explicit user feedback (explicit seeding is
  the deliberate default; scan also gives uniform timepoint durations and
  thermally coherent delta maps). Salvaged into bug 4: apply scan-seeded
  per-FOV z at t=0.

## Suggested sequencing

1. Bug batch (1–7): all small, several are 1–3 lines.
2. Proposal 1 audit run (`_check_table_path_displacement`) → implement if the
   measured reversal error is ≥ ~0.5 µm; fold in bug 4.
3. Proposal 3 + Proposal 6 (accuracy/auditability, zero happy-path cost).
4. Proposal 2 (firmware) next bench window; validate with the MCU stress test.
5. Proposal 5 first tranche alongside any contrast-AF work; Tier-2 remainder
   (7, 8, 9) as scheduling allows.
