# Acquisition Cycles

Cycles let a multipoint acquisition run a **per-position temporal protocol** —
take *X* frames of one ObservationState, *Y* of another, interleave stimulus
pulses, and repeat — all at one stage position before moving on. This is the
shape voltage-imaging / optogenetics protocols need (interleaved imaging +
stimulus, repeated in place).

Where an [ObservationState](observation-state-migration.md) is the microscopy
equivalent of a "channel" (one light-path config, one frame), a **Cycle**
composes several of them into an ordered, repeatable sequence.

## Model

`control/models/acquisition_cycle.py`

- **`CycleStep`** — capture `n_frames` of one observation state (or, for a
  stimulus-only state, fire `n_frames` NIDAQ pulses with no camera frame).
- **`CycleWait`** — pause `duration_ms` milliseconds (no frame, no stimulus).
  Allowed at **any nesting level** — a top-level item or inside a group — so its
  repeat count comes from the enclosing group/cycle. The worker sleeps
  abort-aware (`_interruptible_sleep`), so a long wait still responds to Stop.
- **`CycleGroup`** — an ordered list of steps/waits repeated `repeat` times
  (exactly one level of nesting; the type structure caps depth).
- **`CycleFPMDarkfield`** — a source-coded Fourier-Ptychography darkfield
  generator. Expands at plan-build time into *N* **multiplexed darkfield**
  captures (one camera frame per LED group). It references a base
  `observation_state` (for exposure/gain/color and the LED-matrix channel) plus
  generation params (`outer_na`, `inner_na`, `min_overlap`, `leds_per_pattern`,
  `seed`). The LED index set per frame is computed from the live objective NA +
  the cached dome geometry — see [source-coded-fpm.md](source-coded-fpm.md).
- **`CycleFPMBrightfield`** — a single-LED brightfield sweep (one frame per dome
  LED with NA ≤ objective NA). One base `observation_state`.
- **`CycleFPMClusteredDarkfield`** — an angle-**clustered** darkfield sweep
  (co-located LED cells, one frame each), for 3D/tomography. One base
  `observation_state` (use a longer exposure). Compose with `CycleFPMBrightfield`
  for a full run. See [source-coded-fpm.md](source-coded-fpm.md).
- **`AcquisitionCycle`** — a named, saved sequence: an outer `repeat` over an
  ordered list of items (steps, waits, groups, and/or FPM darkfield items).
  References states **by name** so it tracks preset edits.

Cycles are saved per profile under `cycles/{name}.yaml`, alongside
`observation_presets/`, via the config repo
(`list/save/load/delete_acquisition_cycle`).

### Resolution

The controller resolves the selected cycles into a flat, ordered
`List[ResolvedEvent]` per region (`resolve_chain`), wrapped in a **`RegionPlan`**:

- `events` — the flat acquisition order (imaged + stimulus), with per-state frame
  indices and chain positions preserved (the interleave).
- `dense` / `frame_counts` / `channel_order` — derived once, drive the save
  layout and the channel (C) axis.

Multiple selected cycles run **back-to-back** at each position (chaining). A
plain (no-cycle) channel selection resolves to a 1-frame-per-state plan — the
legacy behaviour — so the worker has a single iteration path.

`CycleFPMDarkfield` items are expanded by an injected `fpm_provider`
(`MultiPointController._fpm_pattern_provider`) so the resolver stays pure — it
closes over the live objective NA + cached LED NA table and returns one LED-index
list per multiplexed frame. Each resulting `ResolvedEvent` carries
`multiplexed_leds`; the worker lights that set via the `"mux"` LED-matrix mode
before capture, and the set is recorded per frame in `cycles_manifest.yaml` for
reconstruction. The *N* darkfield frames are frames of the base state (so they
land on its `T` axis); the count is computed, never hardcoded.

## Density and save layout

Density is decided **statically per region** at plan-build time, over the
*flattened concatenation of all selected cycles for that region*:

> **Dense** ⟺ every *imaged* state has the same total frame count
> (stimulus-only steps are zero-frame events, excluded). Otherwise **ragged**.

| Layout | ZARR_V3 | OME-TIFF | INDIVIDUAL_IMAGES |
|---|---|---|---|
| **Dense** | one multichannel plate; frames fold into `T` (`T = Nt × frames/state`) | one `TZCYX` stack, `T` expanded | per-frame files |
| **Ragged** | one **single-channel plate per state** (`{state}.ome.zarr`), each with its own `T` | one stack per state | per-frame files |

Each captured frame is **self-describing** (`CaptureInfo.array_key`,
`save_t_index`, `save_c_index`, `save_t_size`, `save_c_size`, per-array channel
metadata), computed by the worker from the `RegionPlan` via `frame_coord` /
`SaveLayout`. The save layer (`job_processing.py`) routes on these fields, so
per-region cycles and ragged counts don't require a global uniform `(T, C, Z)`
assumption. Dense + flat reproduce today's `(t=time_point, c=channel)`
coordinates exactly.

### Metadata backbone

Regardless of array layout, two records are the ground truth and let any run be
reconstructed:

- **`acquisition_times.csv`** (or per-timepoint `frame_acquisition_times.csv`) —
  per-frame timestamps plus `cycle_event_index` / `state_frame_index`.
- **`cycles_manifest.yaml`** — the selected cycle definitions and each region's
  resolved flat order (dense flag, per-state counts, channel order, events).
  Written only for cycle runs.

Per-frame INDIVIDUAL_IMAGES filenames are disambiguated with a frame suffix
(`{file_id}_{channel}_f000`) **only** when a state repeats, so simple-case
filenames are unchanged.

## GUI

`gui/widgets/multipoint.py`

- A **Simple / Advanced** dropdown (`combobox_channel_mode`) gates the cycle UI.
  Cycles are an **Advanced-mode** feature; the widgets default to **Simple**, where
  the checklist lists single observation-state presets (one frame each) and the
  flat path (`set_selected_configurations` / `set_region_observation_state_map`) is
  used. See [multipoint.md](user_guides/multipoint.md) Step 5.
- In **Advanced** mode the checklist (`_ObservationStateListWidget`) lists **cycles**
  (`_populate_cycle_list`); checked + ordered cycles run in sequence at each position,
  and the **Edit Cycles** button appears.
- **Edit Cycles** button → `CycleEditorDialog`: a two-level tree builder
  (outer repeat, add Step / add Group / add Step→Group, reorder, save/load via
  the repo). Steps pick from `list_observation_presets()`.
- **Per-Point Channels** assigns different selected cycles (advanced) or observation
  states (simple) per region. The widget pushes the selection via a single mode-aware
  `_push_channel_selection_to_controller`, wired to `set_region_cycle_map` (advanced) or
  `set_region_observation_state_map` (simple), always clearing the opposite map.

## Code map

| File | Role |
|---|---|
| `control/models/acquisition_cycle.py` | model, resolver, `RegionPlan`, `frame_coord`, `SaveLayout` |
| `control/core/config/repository.py` | `*_acquisition_cycle` persistence (`cycles/`) |
| `control/core/multi_point_controller.py` | `set_selected_cycles` / `set_region_cycle_map`, plan resolution, manifest |
| `control/core/multi_point_worker.py` | iterate `RegionPlan.events`, build `SaveLayout` per frame |
| `control/core/job_processing.py` | dense/ragged routing from self-describing `CaptureInfo` |
| `control/core/utils_ome_tiff_writer.py` | OME-TIFF dense T-fold / ragged per-state files |
| `gui/widgets/multipoint.py` | cycle checklist + `CycleEditorDialog` |

## Tests

- `tests/control/models/test_acquisition_cycle.py` — resolver, density, frame coords.
- `tests/control/core/test_cycle_zarr_layout.py` — dense vs ragged through the real ZarrWriter.
- `tests/control/test_observation_state_and_metadata.py::test_config_repository_acquisition_cycle_io` — repo round-trip.
