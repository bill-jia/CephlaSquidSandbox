# Online Postprocessing (Acquisition Cycles)

Online postprocessing runs a **routine** on the frames a cycle item produces at
each FOV, saves the routine's outputs, and **discards the raw inputs**. It is an
Advanced-mode acquisition-cycle feature: any step, FPM sweep, or group can be
assigned a routine via the **Postprocess** column of the cycle editor. Compute
runs in a dedicated subprocess (never blocking acquisition), is memory-bounded by
the same backpressure accountant as saving, and the outputs are written as normal
plates, uploaded like any other data, and pushed to the live display.

The first built-in routine is **`phase2d`** — z-defocus 2D quantitative phase
reconstruction (waveorder), outputting the phase image plus the center raw
brightfield slice.

## What a routine sees and returns

A routine consumes **all frames one item produces per FOV visit** and returns an
arbitrary set of output images:

- **Per-step**: the step's frames — the z-stack (`Z = NZ`, or 1 for a
  reference-z-only step) times `n_frames` occurrences.
- **Per-group** (assign the routine on a `CycleGroup`): the frames of *all* member
  steps pooled into one invocation. This is how a multi-input routine — e.g. DPC
  from four half-circle steps — receives its inputs: **put those steps under one
  group and assign the routine to the group.**

Inputs arrive as `inputs[state]` = an `(F, Z, Y, X)` array (F = pooled
occurrences of that state per visit). Outputs are `(Y, X)` or `(z_size, Y, X)`
arrays keyed by name.

## Where the outputs go

Each declared output becomes its own single-channel plate keyed
`{label}_{output}` (label = the routine's `label` param, or the first input
state name). For ZARR_V3 this is `{label}_{output}.ome.zarr` with `T = Nt`,
`C = 1`, `Z = z_size`, and the declared dtype. **One output-set is produced per
FOV visit per scan timepoint**, regardless of how many input frames were pooled.
The raw input frames of a postprocessed item are never written.

`cycles_manifest.yaml` records each group's routine spec, member input states, and
declared outputs; `acquisition_times.csv` gets a `postprocess/{group}` row per raw
input frame (ground-truth timing) plus rows for each written output.

## Supported save formats

Postprocessing supports **ZARR_V3** and **INDIVIDUAL_IMAGES** only. OME-TIFF and
multi-page TIFF are rejected pre-flight (their global-dims / shared-append writers
don't compose with a second writer process). `skip saving` is also rejected.

## The routine contract

Routines implement `control.postprocessing.base.PostprocessRoutine`:

```python
class PostprocessRoutine(abc.ABC):
    name: ClassVar[str]
    def describe_outputs(self, input_states: Dict[str, InputStateSpec], params: dict) -> List[OutputSpec]: ...
    def process(self, inputs: Dict[str, np.ndarray], ctx: PostprocessContext, params: dict) -> Dict[str, np.ndarray]: ...
```

- **`describe_outputs`** is called at plan-build / validation time — declare each
  output's `name`, `z_size`, `dtype` (or `"input"` to inherit the input dtype),
  `channel_color`, `wavelength_nm`. Raise `ValueError` with a clear message if the
  inputs don't fit (wrong state count, no z-stack, etc.).
- **`process`** runs per FOV visit in the subprocess. `ctx` carries
  `pixel_size_um`, `dz_um`, `nz`, `nt`, `z_positions_um`, `yx_shape` (the camera
  frame size), per-state `state_meta` (wavelength etc.), a `logger`, and a
  **`cache` dict that persists across FOVs** for the process lifetime — use it for
  anything constant across FOVs (e.g. a transfer function). The cache is keyed by
  routine identity (routine + script + params), so two groups/regions using the
  same routine+params share one cache (and one transfer function).
- **`warmup(input_states, ctx, params)`** (optional; default no-op) is called
  **once before any hardware fires** to precompute FOV-shared state into
  `ctx.cache`. All geometry (`yx_shape`, `dz_um`, `nz`, pixel size) is known by
  then, so e.g. the transfer function is factorized up front and the first FOV's
  `process` is a cache hit instead of a multi-second stall (which would otherwise
  delay the first output and backpressure the run). A warmup failure is non-fatal
  — the routine falls back to lazy computation on the first FOV. The acquisition
  shows a brief "pre-computing routine(s)" pause before the first trigger.
- **Lazy-import heavy dependencies** (`torch`, `waveorder`, …) *inside* `process`,
  not at module top level: the main process imports the routine module for
  validation and must stay light.

### Custom scripts

Select **"Custom script…"** in the Postprocess column and pick a `.py` file that
defines a module-level `ROUTINE` instance:

```python
import numpy as np
from control.postprocessing.base import OutputSpec, PostprocessRoutine

class MyRoutine(PostprocessRoutine):
    name = "my_routine"
    def describe_outputs(self, input_states, params):
        return [OutputSpec(name="proj", z_size=1, dtype="float32")]
    def process(self, inputs, ctx, params):
        (stack,) = inputs.values()          # (F, Z, Y, X)
        return {"proj": stack.reshape(-1, *stack.shape[2:]).max(axis=0)}

ROUTINE = MyRoutine()
```

The script is imported in the subprocess (and once in the main process for
validation). Params entered in the editor are passed through as the `params` dict.

## Built-in: `phase2d`

Reconstructs 2D quantitative phase from a brightfield defocus z-stack via the
high-level `waveorder.api.phase` pipeline (`phase.Settings` →
`phase.compute_transfer_function` → `phase.apply_inverse_transfer_function`, with
`recon_dim=2`). Requires one input state with **Full z-stack ON** and `NZ > 1`,
one frame per visit. Runs on the GPU when available (`device="auto"`).

Outputs:
- `phase` — float32 `(Y, X)` quantitative phase.
- `bf_center` — the raw brightfield slice at the focus plane (`nz // 2`, the plane
  the transfer function treats as zero defocus).

Params (pixel size / z-step / NZ come from the acquisition, never set here):
`wavelength_nm` (**nm in the UI; converted to µm for waveorder**; defaults to the
state's illumination wavelength), `na_detection` (defaults to the objective NA),
`na_illumination`, `index_of_refraction_media`, `regularization`,
`invert_phase_contrast` (False). Selecting the routine in the editor pre-populates
these defaults and opens the params dialog for review/edit — that dialog is the
mechanism to set them (no need to edit the routine file). Internally the axial
sampling handed to waveorder is `z_pixel_size = dz × index_of_refraction_media`.
The transfer function (`xr.Dataset` singular system) is computed once (warmup /
first FOV) and cached (keyed on geometry) for all subsequent FOVs.

### Environment

`phase2d` uses the high-level waveorder API, which pulls in `xarray`:

```bash
conda activate squid
pip install PyWavelets xarray
pip install --no-deps --ignore-requires-python -e C:\Code\waveorder
```

Pre-flight validation imports `waveorder.api.phase` and gives this exact message
if it fails.

## Live display

As each output is computed it is pushed to the live image display, labelled with
the output plate key. The shared live viewer holds a single integer dtype and one
image size across all channels, so previews are normalized to `uint16` at their
native resolution; an output whose size differs from the current camera frame is
saved but not displayed (a debug line notes the skip).

## Limitations (v1)

- ZARR_V3 / INDIVIDUAL_IMAGES only.
- Postprocessed states are excluded from the downsampled well mosaics / plate view
  (a warning is logged when downsampled views are enabled).
- Derived outputs are not fed into the downsampled/plate-view pipeline.

## Code map

| Concern | Location |
|---|---|
| Routine contract + context | `control/postprocessing/base.py` |
| Registry (builtin / custom script) | `control/postprocessing/registry.py` |
| `phase2d` routine | `control/postprocessing/routines/phase2d.py` |
| Spec model + plan accounting | `control/models/acquisition_cycle.py` (`PostprocessSpec`, `PostprocessGroupPlan`) |
| Output declaration, manifest, validation | `control/core/multi_point_controller.py` |
| `PostprocessJob` (accumulate/compute/write/barrier) | `control/core/job_processing.py` |
| Worker routing, runner, display, upload tally | `control/core/multi_point_worker.py` |
| Editor Postprocess column + params dialogs | `gui/widgets/multipoint.py` (`Phase2DParamsDialog`, `ScriptParamsDialog`) |
| Tests | `tests/control/postprocessing/`, `tests/control/models/test_acquisition_cycle.py`, `tests/control/core/test_cycle_zarr_layout.py` |
