# Cell focus stack: November 7, 2026, 23:52

Analysis recorded November 8, 2026. This is an offline analysis; no microscope
settings or raw recordings were changed. It follows the
[rotated-bead diagnostic report](sample-flatness-incidence-20261107-analysis.md).

## Result

The cells show a substantially larger variation in the Z of maximum brightness
than in the Z of maximum normalized sharpness. Across nine broad image regions,
brightness peaks span **1.99 µm**, whereas three noise-resistant sharpness
metrics give spans of **0.80–1.14 µm**. A common Z retains at least **90.6%** of
each region's local maximum sharpness score across all three metrics, while
brightness in the lower-left region is only about **41%** of its own maximum.
These percentages describe metric scores, not retained optical resolution.

Thus reasonably consistent within-FOV sharpness can coexist with pronounced
brightness changes across Z. This supports the distinction seen in the beads;
it does not imply perfectly flat focus or prove that all cell heterogeneity
comes from the microscope. The brightness-minus-sharpness pattern has the same
broad direction as the bead pattern, but is larger and more specimen-dependent.

## Acquisition and coordinate checks

Source:
`C:\Microscope_Data\Bill\20261107_diagnose_tilt\cells_focus_test_2026-11-07_23-52-00.751594`

- Image: `ome_tiff/R0.ome.tiff`, 51 × 2400 × 2400 pixels, unsigned 16-bit.
- One FOV, one time point, one confocal channel: `ERKKTR_mKate2_fixed`.
- Objective: 20×, NA 0.75; recorded specimen pixel size 0.361111 µm.
- Exposure: 200 ms; active fluorescence excitation channel 555 nm, intensity
  setting 100%. These differ from the bead acquisitions; absolute intensities
  must not be compared between specimens as if acquisition conditions matched.
- Illumination was configured to stay on between captures. This is a recorded
  acquisition setting, not an independent measurement of laser-power stability.
- The measured stage positions in `acquired_positions.csv` run from
  **1832.4375 to 1841.8125 µm**, in distinct **0.1875 µm** steps. The analysis
  uses these positions, not the different planned ranges in `acquisition.yaml`.
- Plots use Z relative to the measured stack midpoint, **1837.125 µm**.
  First-to-last frame timestamps span about **15.70 seconds**.
- Recorded pixel values peak at 4589; no pixel reaches the 16-bit ceiling.

Image coordinates below are the saved/delivered camera orientation: row 1 at
the top, column 1 at the left. These cell maps are not rotated into stage XY
coordinates and are not corrected using the bead sample-plane model.

## Metrics and safeguards

The initial conventional gradient and Laplacian scores, normalized without
adequate noise treatment, incorrectly favored the dim stack ends. Those
preliminary peak maps are **not** accepted focus results. The final analysis
uses the following procedure:

1. Define cell-containing pixels from a smoothed maximum-over-Z signal image,
   using a threshold of 40 camera units above a dim-end reference and a small
   mask dilation. Keep this mask fixed throughout Z so that the set of sampled
   pixels does not change when a region gets dimmer.
2. Measure mean cell brightness after subtracting the median outside-mask
   background in each tile. An independent initial analysis using whole-tile
   mean signal and the 99th percentile gives similar brightness peak spans
   (1.96 and 1.93 µm).
3. Use cross-products of filtered images at **adjacent Z planes**, rather than
   squared quantities from one noisy frame. Temporally uncorrelated noise has
   zero expected cross-product. Subtract the corresponding outside-mask
   background cross energy to reduce persistent detector-pattern contributions.
4. Compute three amplitude-normalized sharpness scores: gradient energy divided
   by signal covariance; Laplacian energy divided by signal covariance; and
   fine-scale band energy divided by mid-scale band energy. The Gaussian scales
   for the latter are 1.0, 2.5 and 6.0 native pixels. These ratios reduce the
   response to a purely multiplicative brightness change. They are cell-texture
   metrics, not the identical normalized bead-concentration metric.
5. Exclude low-signal planes whose signal covariance is below 5% of that tile's
   maximum. Check a stricter 15% cutoff and an alternate dim-end image-template
   subtraction. The adjacent-frame score is assigned to the midpoint of those
   two Z positions. Smooth curves lightly along Z (Gaussian sigma 0.8 samples)
   and interpolate a bracketed maximum with a local three-point quadratic.
6. Analyze 6×6 tiles and pool their energies into a 3×3 grid, weighted by fixed
   cell-mask fractions. The broad 3×3 regions give more stable summaries of the
   heterogeneous cells. These are region-level focus estimates, not a fit to
   one thin planar layer or a measurement of every individual cell.

Adjacent-Z cross products also mix structures separated by 0.1875 µm in depth;
normalization does not make them immune to changing cellular morphology. The
noise and background checks reduce obvious artifacts but do not turn these
metrics into an independent optical-resolution measurement.

## Measured focus and brightness variation

| Metric | Lowest peak Z relative to midpoint (µm) | Highest peak Z (µm) | Span across the nine regions (µm) |
| --- | ---: | ---: | ---: |
| Mean cell brightness | +0.074 | +2.060 | 1.986 |
| Fine/mid-scale normalized sharpness | −0.252 | +0.545 | 0.797 |
| Normalized gradient sharpness | −0.299 | +0.841 | 1.140 |
| Normalized Laplacian sharpness | −0.184 | +0.730 | 0.914 |

The broad brightness gradient is smooth: a plane fitted to the nine brightness
peak positions has 0.076 µm RMS residual. Fine/mid-scale sharpness has a smaller
gradient and more local variation (0.138 µm plane-fit residual). These planes
describe trends in the metric peak maps, not independently identified sample
or camera tilts. Finite cell depth and different structures in each region
remain part of the measurement.

Fine/mid-scale sharpness peak Z, relative to the stack midpoint:

| Camera row | Left (µm) | Middle (µm) | Right (µm) |
| --- | ---: | ---: | ---: |
| Top | +0.294 | −0.247 | −0.252 |
| Middle | +0.310 | +0.154 | +0.099 |
| Bottom | +0.458 | +0.318 | +0.545 |

Brightness-peak Z minus that sharpness-peak Z in the same regions:

| Camera row | Left (µm) | Middle (µm) | Right (µm) |
| --- | ---: | ---: | ---: |
| Top | +0.689 | +0.752 | +0.326 |
| Middle | +1.125 | +0.730 | +0.235 |
| Bottom | +1.602 | +1.093 | +0.213 |

Maximum brightness therefore occurs at a higher Z than maximum sharpness in
all nine broad regions, with the separation largest toward the lower left.

### How well does one common Z work?

At an adjacent-plane midpoint of **+0.281 µm** relative to the stack midpoint,
the worst region retains 93.8% of its peak fine/mid sharpness score, 92.3% of
its peak gradient score and 90.6% of its peak Laplacian score. Mean retention
across the nine regions is 98.7%, 97.8% and 97.6%, respectively. This is a
descriptive compromise for this recording, not a commanded hardware position
or proof that every individual cell is optimally focused.

Interpolated brightness at that same Z, as a fraction of each region's own
brightness maximum:

| Camera row | Left | Middle | Right |
| --- | ---: | ---: | ---: |
| Top | 88% | 98% | 99% |
| Middle | 67% | 91% | 100% |
| Bottom | 41% | 72% | 94% |

The lower-left region can therefore be near its sharpness optimum while far
from its brightness optimum. These are within-region fractions; they do not
compare absolute brightness between different populations of cells.

### Sensitivity and unresolved detail

Increasing the signal cutoff from 5% to 15% leaves the accepted 3×3 peak
positions unchanged but makes the bottom-left sharpness peak unbracketed
because its low-Z flank is excluded. Alternate image-template subtraction
retains the broad result: gradient/Laplacian spans are about 1.16/0.92 µm,
and the accepted fine/mid peaks span about 0.83 µm. The bottom-middle fine/mid
peak becomes ambiguous under that alternate treatment. The gradient and
Laplacian curves in that region already favor higher Z than the fine/mid ratio.

At 6×6 resolution, individual regions can show multiple or unstable peaks,
particularly a lower-middle tile. A numerical interior maximum alone is not
proof of a biologically meaningful focus plane. The coarse-grid conclusions
are more reliable than an assertion of a precise focus height at every cell.

## Comparison with beads and interpretation

The bead combined, uncorrected sharpness-focus spans at the sampled 3×3 grid
positions were 0.915, 0.270, 0.710 and 0.866 µm across the four dish runs. The
cell sharpness spans of 0.80–1.14 µm are broadly comparable to three of those
runs, although they do not reproduce the particularly flat 23:18 bead result.
The comparison is approximate: cells contain extended structures at different
depths, the cell metrics differ from the bead metric, and cell results average
regions whereas bead grids evaluate fitted functions at grid centers.

The spatial pattern of brightness-minus-sharpness is similar in delivered
camera coordinates: it increases toward the lower left. The nine-region cell
difference map and the mean of the four bead paired-difference maps have a
descriptive correlation of about **0.93** after their constant offsets are
implicitly removed by correlation. With only nine spatially related values,
this is not an independent significance test.

The magnitude is not identical. The fitted linear component of the cell
difference map spans about **1.26 µm** over the central two-thirds grid, versus
**0.56 µm** for the bead difference map. Cell thickness, fluorophore distribution,
different metrics and optical effects can all influence this difference.

The observations make brightness alone an unreliable proxy for defocus and
make a simple single tilted focus surface an incomplete description of the
heterogeneity. A repeatable mismatch in field-dependent excitation/detection
weighting is a plausible contributor, including confocal disk/relay conjugacy
or aberrations. Confocal operation requires illumination and detection foci to
coincide; both paths affect the recorded signal. See
[Leica's explanation of confocal illumination and detection](https://www.leica-microsystems.com/science-lab/life-science/pinhole-effect-in-confocal-microscopes/).

This is a hypothesis, not localization of a defective component. A single
confocal cell stack cannot separate specimen depth/tilt from optical field
effects. Nor does it measure excitation-beam incidence, AF-beam incidence or
camera sensor tilt. There is no same-FOV widefield comparison here. The
monotonic Z scan also confounds Z with time, so bleaching or time-dependent
illumination cannot be completely excluded. Amplitude normalization and
different peak positions across regions make a single global gain change an
insufficient explanation on its own.

## Most informative next checks

1. Acquire confocal and widefield stacks of the same FOV without changing the
   sample or camera geometry. Compare normalized sharpness and brightness
   separately; retain unsaturated signal and adequate low-Z/high-Z coverage.
   The same comparison on a thin bead layer is cleaner than on thick cells.
2. Repeat the confocal stack in reverse Z order, to distinguish a reproducible
   Z-dependent pattern from bleaching, settling or other time-order effects.
3. If the aim is excitation incidence, use the dedicated widefield incidence
   measurement. Do not infer an angle from these brightness-peak gradients or
   adjust the camera solely to flatten the brightness map.

## Artifacts and reproduction

- [Final brightness and sharpness curves](../../artifacts/tilt_20261107/cells_focus/final_focus_curves.png)
- [Final peak and difference maps](../../artifacts/tilt_20261107/cells_focus/final_focus_maps.png)
- [Cell stack overview at a common intensity scale](../../artifacts/tilt_20261107/cells_focus/stack_overview.png)
- [Final metrics and sensitivity variants](../../artifacts/tilt_20261107/cells_focus/final_metrics.json)
- [Common-focus comparison](../../artifacts/tilt_20261107/cells_focus/common_focus.json)
- [Bead comparison](../../artifacts/tilt_20261107/cells_focus/bead_comparison.json)
- [Noise-resistant analysis script](../../artifacts/tilt_20261107/analyze_cells_focus_robust.py)
- [Summary and plotting script](../../artifacts/tilt_20261107/summarize_cells_focus.py)

Run from the repository root in the microscope Python environment:

```powershell
python artifacts/tilt_20261107/analyze_cells_focus_robust.py
python artifacts/tilt_20261107/summarize_cells_focus.py
```

The earlier `analyze_cells_focus.py` records acquisition statistics, an overview
and preliminary metrics. Its uncorrected sharpness peaks at dim stack ends
are retained as troubleshooting evidence but are superseded by this report.
