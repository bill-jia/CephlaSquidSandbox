# Sample flatness and illumination incidence

Open **Sample flatness and laser incidence alignment** from the microscope's
tools menu. The dialog now has two separately started measurements. The main
window is available between measurements and blocked while acquisition and
hardware restoration are in progress.

## Bead workflow

1. Select a fluorescence preset and focus on beads attached to a stable surface.
   Confocal is useful for this flatness step because it rejects out-of-focus
   fluorescence. Choose **Fluorescent beads** in the alignment dialog.
2. Click **1. Measure flatness**. The tool collects nine overlapping XY fields
   with Z-stacks. It stops and restores the starting stage, piezo, camera and
   illumination state when finished. Incidence does not start automatically.
3. Review the surface map, fitted plane, residuals and matched-bead overlap
   error. If you adjust the sample or holder, measure flatness again.
4. Once flatness passes, select a **widefield** fluorescence preset in the main
   window, without changing stage XY, objective, camera geometry or sample.
   Adjust exposure/intensity if necessary. Keep the same bead fluorescence
   channel where possible.
5. Click **2. Measure incidence**. The tool checks the original field and
   remeasures the center bead stack in widefield. Matching bead focus heights
   establish the focus offset between modes. A changed field, insufficient
   matches, or a nonuniform focus change invalidates the handoff.

Bead mode starts with a ±3 µm focus range and 31 planes (nominal 0.2 µm steps).
Increase the range if bead peaks are not bracketed; increase plane count to
retain adequate sampling. The tool uses measured stage Z, with the piezo
position added, and rejects duplicate/descending sampled Z positions.

**Normalized bead sharpness** is the default focus metric. It reduces sensitivity
to multiplicative brightness fluctuations. **Peak bead brightness** is available
when illumination is stable. The photometry radius is in native camera pixels
(default 16); use well-separated beads whose halos fit inside the aperture.
Hot pixels, clipped beads, overlapping candidates, low-signal measurements and
unresolved/edge focus peaks are excluded. Sparse or poorly distributed beads
produce an explicit failure rather than a passing flatness result.

Steady imaging illumination remains on throughout each bead Z-stack and each
incidence sweep. Laser AF, when available, operates between illuminated blocks.
Timed/waveform presets cannot be forced continuously on and are rejected for
bead calibration. Cancellation and capture/analysis errors run the same hardware
restoration as normal completion. Troubleshooting image writes can lengthen the
illuminated interval.

## Adjustable parameters

The defaults and allowed ranges below describe the dialog controls. Selecting
**Fluorescent beads** changes the focus range and plane count to the bead preset;
switching back to texture does not automatically restore their initial values.
Exposure, excitation power, objective and widefield/confocal mode are selected
in the main microscope window between measurements.

### Sample and bead analysis

| Control | Default / allowed values | Meaning and how to choose it |
| --- | --- | --- |
| **Sample** | Textured fluorescent surface; or Fluorescent beads | Select the analysis appropriate to the specimen. Texture mode tracks extended fluorescent features; bead mode tracks individual isolated beads across overlapping fields. Changing this selection invalidates the saved flatness measurement. |
| **Bead focus metric** | Normalized bead sharpness; or Peak bead brightness | Sharpness measures bead concentration after local background subtraction and normalizes by integrated flux, reducing sensitivity to multiplicative brightness changes. Peak brightness finds focus using the brightest background-subtracted pixel and needs stable illumination. This choice affects focus measurement and widefield focus transfer. Incidence always uses integrated bead flux, regardless of this selection. Only used in bead mode. |
| **Bead aperture radius** | 16 native camera pixels; 4–48 | Sets the radius of the local bead analysis patch, not the physical bead radius or a radius in the downsampled preview. The patch is `(2r + 1)` pixels square; signal is integrated within radius `0.72r`, and background is estimated from pixels beyond `0.85r`. Choose a radius that contains the defocused bead signal while leaving clean background and avoiding neighboring beads. Too small can truncate the halo and bias incidence; too large admits more background and rejects more crowded candidates. Only used in bead mode. |
| **Bead signal threshold (× noise)** | 8; 3–20 | Minimum signal relative to estimated noise for bead detection and per-image photometry. Lower values admit dimmer beads but also more noise candidates; higher values require stronger beads. Applied to the filtered detection signal, background-subtracted peak and integrated flux (with its aperture noise estimate). This is a selection threshold, not camera gain or a statistical confidence level. Only used in bead mode. |

Changing the bead focus metric, aperture radius, signal threshold, analysis
resolution or stage-motion RMS limit after flatness requires a new flatness
measurement before incidence.

### Image resolution and sparse beads

**Analysis resolution** defaults to **512 px long edge**. Choose **256, 512,
1024, 2048**, or **Native (no reduction)**. Images smaller than the selected
limit are not enlarged. Reduction uses area averaging across the entire
delivered camera field; it does not crop the field or skip sensor rows/columns.
The setting controls registration, spatial-analysis image coordinates and
displayed images. In texture mode it also controls the images used to fit the
illumination envelope. Spatial matching and angle-quality tolerances scale with
resolution to preserve their original physical extent.

Bead detection, bead focus scores and bead flux measurements always use the
**native delivered camera image**, regardless of this setting. Full-resolution
recordings also retain that image. The tool does not change camera ROI or
hardware binning; "native" means the pixels delivered under the current camera
configuration, which may already be cropped or binned. Texture focus scores
also use the delivered image before reduction.

For sparse beads, try 1024 or Native if reduction makes the small features hard
to inspect. Bead registration itself matches the bead coordinates measured in
the native delivered image; it does not depend on texture correlation or the
brightness of the displayed projection. Higher resolution preserves displayed
spatial detail but costs time and memory; it does not increase the number of
beads or collect more light. For dim
beads, cautiously lower the signal threshold (for example, from 8 to 5) and
review the focus fits and overlap consistency. Saturation, isolation, focus
peak and spatial-coverage checks still apply. If too few beads are shared
between fields, reduce XY offset or choose a field with more distributed beads.
Exposure and illumination power are adjusted in the main window between runs.

Bead registration connects overlapping fields using the relative positions of
beads. Each link needs at least four mutual matches and an unambiguous pattern.
A field can connect through neighboring fields; it need not share four beads
directly with the starting field. Conflicting bead identities around overlap
paths, disconnected fields and inconsistent translations are rejected.

The flatness step automatically estimates a radial image correction from repeated
beads, allowing each field its own XY translation. It compares a translation-only
model, one radial coefficient, and that coefficient with a fitted distortion
center. Distortion is retained only when it improves prediction of fields left
out of the fit; an already accurate translation model remains uncorrected.
Calibration needs distributed repeated beads and enough overlap to validate it.
Final matching uses the original tight correspondence limits after correction.

The correction is `p_corrected = p + k1 * (p - c) * (|p - c| / R)^2`, where
`R` is half the longer image dimension and `c` is the effective distortion center.
It is fitted for the current flatness acquisition, not hard-coded from an earlier
recording. The fitted center does not independently measure the objective axis.
Recorded diagnostics include the coefficient, center, validation errors,
overlap links and residuals. The flatness result reports the correction and RMS.

Corrected bead coordinates are used for matching and specimen positions in the
flatness fit. Recorded stage coordinates still determine motion commands and
the overall stage-to-image scale/orientation, and remain subject to the adjustable
stage-motion check. Camera-field focus terms are fitted in delivered-image
coordinates. Raw images and native-pixel bead photometry are preserved.

The distortion model is held fixed for the widefield handoff and all incidence
Z planes. It is not re-estimated independently at each Z, which could absorb the
motion being measured. The illumination envelope and its center are fitted in
corrected coordinates. The confocal-to-widefield handoff still needs at least six
shared beads and only a small permitted field shift; aperture locations are
mapped back to native pixels for intensity measurements.

**Stage-motion RMS limit (512 px equiv.)** defaults to **1.0**, adjustable from
**0.25 to 10** in bead mode. It limits the root-mean-square Euclidean disagreement
between measured bead shifts and the fitted stage-to-image transform across the
scan. This is an RMS limit, not the maximum error allowed in any one field.
Pixels refer to the delivered camera image reduced to at most 512 pixels on its
long edge. For example, setting 1.5 allows 1.5 pixels at 512 resolution, 3 pixels
at 1024, or approximately 7 native pixels on a 2400-pixel camera image. The
physical tolerance therefore stays the same when analysis resolution changes.
For images originally smaller than 512 pixels, the reference is the native image.

A successful bead match
can still fail this check: matching identifies the same beads, while this check
tests whether their displacements agree with one stage-to-image transform.
The setting applies to flatness and each incidence-plane registration. Raising
it accepts more disagreement in the overall stage-to-image calibration. The
flatness fit uses bead-measured specimen positions regardless of this limit.
It does not relax constellation ambiguity, individual correspondence
scatter, transform conditioning, focus consistency, the mode-transfer field
shift check, or illumination/angle checks. Larger allowed discrepancies can
reduce the accuracy of the spatial calibration and make movement harder to
distinguish from sample or optical effects. It does not make Z settling or
insert rocking during a scan acceptable.

In replay of the November 7 recordings with radial correction, the 21:33 run
has approximately 1.34 reference pixels of stage-fit RMS and a 3.98 µm sample
plane span. The 22:18 run has approximately 0.95 reference pixels of stage-fit
RMS and a 2.47 µm span. Both register at a stage-motion limit of 2, but neither
passes the default 0.5 µm flatness limit. Raising the stage-motion limit does not
make the sample pass that separate flatness check.

Troubleshooting records retain per-field match counts, competing-match counts,
shifts, correspondence residuals and stage-fit residuals, including on failure.
The selected limit and the residual in reference pixels are also recorded.
The error reports the measured RMS and its limit at the active resolution. Do not relax the sample
flatness limits to address a registration failure; they test different things.

### Acquisition and flatness limits

| Control | Default / allowed range | Meaning and how to choose it |
| --- | --- | --- |
| **XY offset (FOV)** | 0.33; 0.05–0.45 | Distance from the starting field to neighboring field centers, along each stage axis, as a fraction of the **shorter** camera field dimension. Nine fields form a 3×3 grid at offsets `−d`, `0`, `+d`. At 0.33, the outer field centers span 0.66 times that FOV dimension. Larger offsets cover more specimen but reduce shared features/beads; smaller offsets increase overlap but provide less spatial information for separating sample and camera-field effects. |
| **Focus ± range (µm)** | Initially ±2; bead preset ±3; 0.1–20 | Half-width of each focus stack around its local focus estimate. ±3 means a 6 µm total stack. Increase it if the focus peak falls near or outside a stack end. Also used for the bead widefield focus-transfer stack. Increasing range without increasing plane count makes Z sampling coarser. |
| **Focus planes** | Initially 9; bead preset 31; odd integers 5–61 | Number of images in each focus stack, including both endpoints. Nominal Z spacing is `2 × focus range / (planes − 1)`: ±2 with 9 planes gives 0.5 µm; ±3 with 31 gives 0.2 µm. More planes improve sampling but take longer and increase illumination exposure and saved data. Actual positions depend on stage resolution; duplicate sampled positions are rejected in bead mode. |
| **Z baseline (µm)** | 4; 0.2–40 | Full separation between the lower and upper incidence planes. Images are taken at `−baseline/2`, `0`, `+baseline/2` around the locally focused, plane-compensated position. Thus 4 means −2, 0 and +2 µm. Larger separation can make a small lateral beam shift easier to resolve, but increases bead defocus and can make flux measurements or the linear-motion assumption unreliable. This is independent of the focus-stack range. |
| **Max plane span (µm)** | 0.5; 0.02–10 | Largest allowed peak-to-valley height change of the **fitted sample plane across the calibration footprint**, including the XY offsets. It limits the overall sample slope after fitting camera-field focus effects. It is an acceptance threshold, not a Z movement, stack range or limit on individual bead heights. A larger scanned footprint gives a larger span for the same slope. Increase it only if that much sample height change is acceptable for the experiment. |
| **Max residual RMS (µm)** | 0.25; 0.02–10 | Largest allowed root-mean-square mismatch between measured focus heights and the fitted sample-plane plus camera-field model, and between repeated observations in overlapping fields. It also controls focus-resolution checks and the allowed scatter in bead focus transfer between modes. Smaller values require more consistent focus measurements and a better-fitting surface; larger values tolerate more variation or measurement noise. It is not a Z step or an outlier-clipping setting. |
| **Camera black level** | 0 camera intensity units (ADU); 0–65535 | Constant camera offset subtracted in textured-surface analysis. Estimate it from a dark image at the relevant camera settings; do not use the dimmest fluorescent region as the offset. Bead analysis estimates background locally around each bead and does not use this setting. |

**The three Z-related quantities serve different purposes:** focus range finds
the bead/texture focus, Z baseline measures beam-center motion with Z, and max
plane span decides whether the fitted sample slope is acceptable. For example,
±3 µm focus range, 4 µm baseline and 0.5 µm max plane span mean a 6 µm focus
stack, incidence images spanning 4 µm, and at most 0.5 µm fitted sample height
change across the calibration footprint. They need not have the same value.

Plane span and residual RMS also test different things. A smooth tilted surface
with 0.8 µm plane span and 0.05 µm residual RMS fails the default span limit. A
surface with 0.2 µm plane span but 0.4 µm residual RMS fails the default residual
limit. Both limits, together with the coverage and consistency checks, must
pass before incidence can proceed. Relaxing a threshold does not improve the
measurement or establish that the surface is normal to the objective axis.

### Incidence comparison and repeats

| Control | Default / allowed range | Meaning and how to choose it |
| --- | --- | --- |
| **Angle target (°)** | 1; 0.05–30 | Scale used to compare apparent incidence angles when choosing the displayed **Best** result. Smaller values give angle more weight relative to centering. The reported angle uncertainty must also be no greater than this value for a result to qualify as Best. The desired angle is zero; this value does not command a nonzero angle, move hardware or change the flatness limits. |
| **Center target (% FOV)** | 5; 0.5–50 | Scale used to compare beam-center displacement from the field center when choosing Best. Each X/Y displacement is expressed as a percentage of its corresponding FOV dimension; their Euclidean magnitude is used. Smaller values give centering more weight relative to angle. This is not an XY position command or a separate hard cutoff on each axis. |
| **Acquire three incidence sweeps** | Off | Off acquires one incidence sweep; on acquires three, then stops. The best eligible result is retained for comparison during that measurement. This does not repeat flatness or automatically advance from flatness to incidence. |

The comparison score is

```text
center_error = sqrt(center_x_percent² + center_y_percent²)
score = sqrt((angle / angle_target)² + (center_error / center_target)²)
```

Lower scores are better. These targets are comparison scales, **not a combined
pass/fail test**: a result can be displayed as Best even when its angle or center
error exceeds the corresponding target. Independent analysis checks can still
make an angle unavailable. For example, the repeated-reference drift limit is
a fixed analysis check and is not controlled by either target.

### Recording and measurement buttons

| Control | Default | Meaning |
| --- | --- | --- |
| **Save troubleshooting data** | Off | Saves full-resolution images and analysis records for the measurement. Useful for diagnosing rejected focus fits, brightness drift or unexpected results. Recording can use substantial disk space, slow acquisition and lengthen the illuminated interval. |
| **Output folder / Browse…** | No folder selected | Selects the parent folder for recordings when saving is enabled. Each measurement creates its own run folder; flatness and incidence have separate recordings. |

**1. Measure flatness** acquires and checks the sample and returns control to the
user. **2. Measure incidence** becomes available after an accepted flatness
measurement; use it after selecting widefield mode. **Stop and return** requests
cancellation and restoration of the saved hardware state. **Reset best** clears
the stored Best result and its plots; it does not change the optics or erase
the flatness calibration.

## What the results mean

Flatness jointly fits a sample plane and a quadratic camera-field focus term
using individual bead heights across the stage dither. It checks repeated
observations of the same beads for consistency. The raw-field 3×3 labels are
**fitted** focus differences; the focus scatter plot contains bead measurements.
This separation assumes stable stage XY height travel. It does not independently
measure camera normality or the objective axis.

Incidence uses integrated, locally background-subtracted bead flux at three Z
heights. Ratios of the **same beads** in overlapping fields cancel differences
in intrinsic bead brightness. A quadratic log-intensity envelope is fitted at
each height. Repeated center fields must agree within 3%; small smooth drift is
corrected, but arbitrary independent frame gains are not fitted because they
would make the illumination gradient ambiguous.

An angle is reported only if bead coverage constrains the envelope, its maximum
is bounded, its center is sufficiently stable when individual fields are
omitted, and center motion is consistent across the three Z heights. A sparse
field or a nearly uniform beam can give useful flatness but **no incidence
angle**. The reported angle is apparent relative to stage Z, and the intensity
envelope can include detection shading as well as excitation illumination.
Confocal incidence is disabled in the manual workflow because the disk defines
an intermediate image plane and changes the interpretation of sample Z scans.

The textured-surface mode also stops after **Measure flatness**. Its separately
started widefield incidence step performs a fresh texture-flatness check before
the incidence sweep; it does not transfer a confocal texture calibration.

## Records and validation

Enable **Save troubleshooting data** before either measurement to record it in
its own run folder. Records include full-resolution images, requested and
measured positions, bead coordinates and focus curves, registration, flatness,
widefield focus transfer, matched-bead illumination fits and center-reference
drift. Original recordings are not modified.

Automated tests cover synthetic bead focus with brightness flicker, saturation
and hot-pixel rejection, sample/camera-field separation, unequal intrinsic bead
brightness, drift/coverage failures, explicit step transitions, mode changes,
stale stage positions, continuous illumination and restoration on cancellation.
The bead focus and mode-transfer analysis was also replayed on the November 7
upright confocal and widefield stacks. Live microscope validation is still
required for a full dither/incidence measurement.
