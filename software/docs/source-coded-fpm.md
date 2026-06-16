# Fourier Ptychography (FPM)

Two FPM acquisition routines are available, each as a generative **Acquisition
Cycle** item (Advanced mode), sharing the `mux` LED-matrix mode and the cached
dome geometry:

FPM is built from composable, single-base-state cycle items (so each references
one `observation_state`, the same paradigm as a normal step):

| Cycle item | LEDs | Best for |
|---|---|---|
| `CycleFPMDarkfield` | random angle-**multiplexed** darkfield patterns | fast 2D large-SBP phase (source-coded) |
| `CycleFPMBrightfield` | **single-LED** brightfield, full sweep or a pseudorandom subset of N | brightfield half of a full run |
| `CycleFPMClusteredDarkfield` | angle-**clustered** darkfield cells (co-located LEDs) | darkfield half of a 3D/tomography run |

A **full FPM run** = `CycleFPMBrightfield` + `CycleFPMClusteredDarkfield` in one
cycle (each with its own base state, so darkfield can use a longer exposure). A
**source-coded run** = four `dpc.*` steps + `CycleFPMDarkfield`.

All compute their frame counts from the geometry (objective NA + overlap) — never
hardcoded — and write `fpm_patterns.yaml` (per-frame base state, LED indices, NA
positions, and centroids) to the acquisition root for reconstruction.

---

## Source-coded routine (`CycleFPMDarkfield`)

Source-coded FPM (Tian et al., *Optica* **2**, 904 (2015)) reconstructs a
high-resolution, wide-field quantitative-phase image from a small number of
images by **angle-multiplexing** the LED illumination. Instead of scanning every
LED one at a time (sequential FPM, ~hundreds of images), it uses a hybrid scheme:

1. **Brightfield** is captured as **four DPC half-circle images** (top, bottom,
   left, right) — already available as the `dpc.{l,r,t,b}` LED-matrix modes.
2. **Darkfield** Fourier space is filled by turning on **multiple darkfield LEDs
   at once** (one camera frame per group), separating brightfield and darkfield
   LEDs so the weak darkfield signal isn't swamped by brightfield Poisson noise.

This implementation generates **only the darkfield multiplexed patterns** — add
the four DPC steps to your cycle yourself for a complete source-coded dataset.

## How the darkfield LEDs are chosen

In Fourier space each LED contributes a circular "pupil" of radius equal to the
**objective NA**, centred at that LED's illumination NA `(na_x, na_y)`. FPM
reconstruction needs neighbouring pupils to overlap by ≥ ~60%. The SCI DOME (793
LEDs, hemispherical) samples Fourier space far denser than that minimum, so we
**thin** the darkfield candidates to the minimal set that still tiles the
darkfield annulus at the required overlap, then group that set into multiplexed
patterns.

`control/fpm_led_geometry.py` (pure, unit-tested):

1. **Candidates** — LEDs whose NA falls in the darkfield annulus
   `[inner_na, outer_na]`. `inner_na` defaults to the **objective NA** (the
   brightfield/darkfield boundary); `outer_na` defaults to **0.8** (the
   resolution target).
2. **Overlap pitch** — the largest centre-to-centre NA distance whose two pupils
   (radius = objective NA) still overlap by `min_overlap` (default 0.6, the
   paper's requirement; this is the *area* overlap of two equal circles), found
   by inverting the two-circle intersection area.
3. **Selection** — a **hexagonal lattice** of that pitch is laid over the
   darkfield annulus and each lattice point snapped to the nearest real LED. This
   makes *adjacent selected* pupils overlap by ≥ `min_overlap` — the actual FPM
   requirement. (A greedy dominating set is **not** sufficient: it only keeps each
   *dropped* LED near a selected one, letting adjacent *selected* pupils drift to
   ~2× the pitch and under-sample Fourier space.) Snapping to discrete LEDs
   perturbs the spacing, so the lattice is tightened adaptively until the
   90th-percentile neighbour overlap meets the target.
4. **Grouping** — the selected LEDs are partitioned (seeded-random, reproducible)
   into patterns of `leds_per_pattern` LEDs. **`leds_per_pattern = 0` means auto**
   (the default): a balanced value `≈ round(sqrt(N_selected))`, floored at 2 and
   capped at the paper's empirically-robust 8 — so it adapts to the NA selection
   instead of being fixed. The pattern **count is computed, never hardcoded**: the
   selected-LED count scales as ≈ `(NA_range / objective_NA)²`, so a **larger
   objective pupil needs *fewer* darkfield frames**, and a higher overlap target
   needs more. Sanity check: a 0.2-NA objective tiling to ~0.8 yields ~17 patterns,
   matching the paper; a 0.4-NA objective yields ~5 at 60% overlap (~7 at 70%).

> The darkfield region is clipped to the dome's actual NA reach. At WD 71 mm the
> SCI DOME reaches ~0.755 NA, so `outer_na` above that simply has no LEDs to use.

## LED geometry source (`pledposna`)

Selecting individual LEDs by NA position requires a host-side per-LED NA table,
which the firmware owns (it knows the true hemispherical dome geometry at the
configured working distance). Dump it **once** with:

```bash
conda activate squid
python -m tools.dump_sci_dome_geometry          # SN + WD read from machine config
# python -m tools.dump_sci_dome_geometry --sim  # synthetic dome for offline testing
```

This sends the firmware `pledposna` command and caches `(index, na_x, na_y)` to
`objective_and_sample_formats/led_arrays/sci_dome_na_positions.csv`. Re-run it
only if the array, firmware, or working distance changes. Using the firmware's
own NA values (rather than a flat `NA = r/√(r²+z²)` formula) keeps the host's
darkfield selection consistent with the firmware's `bf`/`df`/`an` decisions and
correct for a dome where LEDs have varying z.

## Firmware path: the `mux` mode

A multiplexed pattern is lit in a single serial write via the illuminate `l`
command, which accepts a list of indices: `l.<i0>.<i1>.<i2>…`
(`SciMicroscopyLEDArray.set_multiple_leds`). The unified LED matrix exposes this
as the `"mux"` mode (`control/lighting.py`); the per-capture index list is pushed
via `IlluminationController.set_led_matrix_multiplexed_indices`, reusing the
existing color/brightness latch and re-fire lifecycle. Color and brightness come
from the global array color and the base state's intensity.

## Using it (Acquisition Cycle)

FPM lives in the **acquisition-cycle** system (Advanced mode), not the flat
observation-state list — so it never pollutes the channel dropdowns. In the
cycle editor (**Add FPM Darkfield**):

- pick a **base observation state** — it supplies exposure/gain/color and **must
  have the LED-matrix channel ON** (the mux override only lights an already-on
  matrix). This is checked pre-flight by `validate_acquisition_settings`, which
  fails the run with a clear message if the matrix channel is OFF or no
  SciMicroscopy array is configured — rather than silently capturing dark frames;
- set the FPM params (outer NA, inner NA or "use objective NA", min overlap,
  LEDs per pattern — "Auto" balances it — and seed) in the **FPM params** popup.
  The popup shows the **current objective NA** and a **live preview** ("→ N
  darkfield LEDs, P patterns of ≤m, overlap X") that recomputes from the cached
  geometry as you change any setting.

For a complete source-coded run, add four `CycleStep`s for the `dpc.*` modes
(brightfield) plus one FPM darkfield item. At acquisition time the item expands
to *N* multiplexed darkfield frames (frames of the base state on its `T` axis).
Two records are written to the acquisition root for reconstruction:
`cycles_manifest.yaml` (the full event order, with each frame's LED *indices*) and
**`fpm_patterns.yaml`** (per-pattern LED indices **plus their NA positions**, the
objective NA, and the array distance) — so the dataset is self-contained.

> The darkfield frames are saved as a multi-frame stack of one channel (the base
> state), distinguished by frame index — matching the paper's "N multiplexed
> dark-field images", not N separate channels.

---

## Full routine — brightfield sweep + clustered darkfield

A comprehensive, 3D/tomography-oriented acquisition, built from two composable
cycle items (add both to one cycle):

### `CycleFPMBrightfield` (**Add FPM Brightfield**)
Brightfield LEDs (illumination NA = sinθ ≤ objective NA), captured **one LED per
frame** (`brightfield_leds`). By default the full sweep — comprehensive, so many
frames (e.g. ~177 for a 0.4 objective on the SCI DOME). Set **N LEDs** in the
popup to instead sample a reproducible **pseudorandom subset** of that size
(`pseudorandom_sample`, seeded) for a faster, sparser brightfield set; the kept
LEDs keep their centre-out order. One base state (short exposure).

### `CycleFPMClusteredDarkfield` (**Add FPM Clustered DF**)
The objective-NA→`outer_na` annulus is binned into angular **cells** at the same
~60% Fourier-overlap step used to tile it. Every darkfield LED is Voronoi-assigned
to its nearest tile centre, so each cell is a tight cluster of *adjacent* LEDs at
nearly the same angle; the whole cell fires as one frame (`cluster_darkfield_leds`).
~tens of cells (e.g. ~25 for a 0.4 objective). One base state — use a **longer
exposure** (SNR per cell scales ~√(cell size), more when read-noise-limited, which
darkfield usually is).

**Why clustered, not random-multiplexed:** random multiplexing scrambles which
Fourier shell each photon came from — fine for 2D stitching (only the *union* of
coverage matters) but it destroys the per-angle shell assignment that 3D needs.
Clustering keeps a frame's members co-located in angle, so each frame maps to one
tight patch of the cap and the angular structure tomography depends on is preserved.

Keeping brightfield and darkfield as **separate items** (separate base states)
means each uses a single `observation_state` — the same paradigm as every other
cycle item — and the two exposures are independent. Each base state must have the
LED-matrix channel ON (validated pre-flight). BF frames save under the BF state,
DF cells under the DF state (separate stacks). The clustered-DF popup live-previews
"→ N darkfield LEDs in M clustered cells".

`fpm_patterns.yaml` records, per frame: the base state, member LED indices, their
k-vectors (`led_na`), and the **centroid** — the per-cell angular position 3D
reconstruction keys on.

## Code map

| Concern | Location |
|---|---|
| Darkfield selection / clustering / BF list / overlap math | `control/fpm_led_geometry.py` |
| Geometry dump tool (`pledposna`) | `tools/dump_sci_dome_geometry.py` |
| `mux` mode + multi-LED command | `control/lighting.py`, `control/serial_peripherals.py` |
| Cycle items + resolver | `control/models/acquisition_cycle.py` (`CycleFPMDarkfield`, `CycleFPMBrightfield`, `CycleFPMClusteredDarkfield`) |
| Frame provider wiring + manifest + `fpm_patterns.yaml` | `control/core/multi_point_controller.py` |
| Per-capture LED override | `control/core/multi_point_worker.py` |
| GUI cycle nodes + params dialogs | `gui/widgets/multipoint.py` (`FPMDarkfieldParamsDialog`, `FPMClusteredDarkfieldParamsDialog`) |
| Tests | `tests/control/test_fpm_led_geometry.py`, `tests/control/models/test_acquisition_cycle.py` |
