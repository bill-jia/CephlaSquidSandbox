# Heterogeneous Time Intervals for Multipoint Acquisition

## Context

The current multipoint worker (`software/control/core/multi_point_worker.py:L701-L753`) runs a
single global time loop: every `dt` it scans every region. This forces every well onto the
same imaging cadence, which doesn't fit experiments where different cell types or treatments
need different temporal sampling.

This change introduces **acquisition groups** — named subsets of the wellplate-selected
regions, each with its own `dt` and `Nt`. The worker becomes an event-driven scheduler that
interleaves group passes, predicts overruns at submission and during run, and emits per-group
output archives. A new "Schedule" tab in `imageDisplayTabs` (`main_window.py:606`) visualises
the timeline before and during acquisition.

**Decisions locked from interview**:
- v1 group customises only `dt` + `Nt` (channels and Z stay global / per-well)
- Each region belongs to **exactly one** group
- One on-disk archive per group at the experiment root
- Shared `t=0 = acquisition_start`; group event timestamps reference this anchor
- "Always groups" model — when the user defines none, an implicit single "Default" group
  carries the global `dt`/`Nt`; legacy single-cadence is the special case
- UI: paint mode on the existing well selector, design language matching the
  per-point-channels popup
- Overrun policy: **catch up immediately** (next group event = `actual_pass_completion + dt`,
  cadence drifts; smallest-dt-first tiebreak when two groups due simultaneously)
- Failure semantics: skip the failing group, continue others, mark failure in metadata
- Notification: persistent banner in the multipoint widget + log; modal popup only at
  preflight hard-fail
- Timing estimates persist to `~/.squid/timing_estimates.yaml` keyed per-microscope
- Region source v1: wellplate only (manual / flexible / current-position keep legacy path)

**Out of scope for v1** (called out so the data model leaves room): per-group channel/Z/AF
overrides, multi-membership, manual/flexible groups, fluidics-aware groups, per-group
abort/pause/resume, modal popups on overruns.

## Critical files

| File | Role |
|---|---|
| `software/control/core/multi_point_worker.py` | Outer loop refactor (`:L701-L753`), region iteration (`:L1639`), save path branch (`:L916-L925`), `CaptureInfo` build (`:L2413`), TimingManager attachment (`:L94, :L630, :L636`), per-FOV save dispatch |
| `software/control/core/multi_point_utils.py` | `AcquisitionParameters` (`:L29-L96`) — add `groups` field |
| `software/control/core/multi_point_controller.py` | `build_params` snapshot (`:L1175-L1229`) — populate `groups`; new `set_groups`; preflight estimation hook; abort flag (`:L1330-L1331`) |
| `software/control/core/job_processing.py` | `CaptureInfo` (`:L79-L100`), `append_frame_acquisition_time_csv` (`:L144-L204`) — add `group_id`, `nominal_time_s` |
| `software/control/core/scan_coordinates.py` | Region storage (`:L77-L83`) — no schema change; just used to validate group region IDs |
| `software/gui/widgets/multipoint.py` | `WellplateMultiPointWidget` — group paint-mode UI, group list panel, status banner |
| `software/control/utils.py` | `TimingManager` (`:L543-L694`) — gain a per-group tag in timer names |
| **NEW** `software/control/core/scheduling.py` | `GroupConfig` dataclass + `MultiGroupScheduler` |
| **NEW** `software/control/core/timing_estimator.py` | Cost model + per-microscope YAML persistence |
| **NEW** `software/gui/widgets/schedule_view.py` | Schedule tab widget (preview + live Gantt) |
| `software/gui/gui_hcs/main_window.py` | Register the Schedule tab in `imageDisplayTabs` near `:766` |

## Reused existing infrastructure

- `_ChannelChipDelegate` painting style and `_CHANNEL_COLOR_PALETTE` pattern from
  `software/gui/widgets/multipoint.py` — mirror for groups with a separate
  `_GROUP_COLOR_PALETTE`
- `WellSelectionWidget` (`hardware_panels.py:L2723`) hosts the well grid; we add a paint
  overlay layer rather than rebuilding the selector
- `TimingManager` (`utils.py:L543-L694`) and existing `SQUID_TIMING_REPORT=1` gating
- `BackpressureController` (`backpressure.py:L77`) — unchanged; one global queue still works
- Save-job classes (`SaveImageJob`, `SaveOMETiffJob`, `SaveZarrJob` in `job_processing.py`) —
  modified only to honour the per-group output root and to pass `group_id` through
- Abort pattern (`abort_requested_fn`, `multi_point_controller.py:L1330-L1331`) — unchanged;
  abort still ends the whole run. Per-group skip-on-error is independent of abort.
- Existing `save_multipoint_widget_config_to_cache` in the wellplate widget — extend to
  serialise group definitions
- `pyqtgraph` is already imported (`_bootstrap.py:L48`) — used for the schedule Gantt

## Design

### Data model

```python
# scheduling.py
@dataclass
class GroupConfig:
    name: str                      # display name, e.g. "GroupA" or user-renamed
    color_index: int               # palette slot, stable across reorderings
    region_ids: list[str]          # well IDs from scan_coordinates.region_centers
    dt: float                      # seconds between timepoints for this group
    Nt: int                        # total timepoints
    # v1 stops here; per-group channels/Z/AF can be added later as Optional fields
```

`AcquisitionParameters.groups: list[GroupConfig]` (`multi_point_utils.py`) replaces
top-level `Nt`/`deltat` as the source of truth for the worker. Existing `Nt`/`deltat` fields
stay during transition but get derived from / overwritten by `groups[i].Nt/dt` — the
wellplate widget's no-groups path populates a single implicit `GroupConfig(name="Default",
region_ids=all_selected_wells, dt=global_dt, Nt=global_Nt)`.

### Scheduler

```python
class MultiGroupScheduler:
    """
    Drift-mode scheduler. After each pass finishes, the next event for that group is
    pushed at (now + dt_g). Two events tied → smallest dt_g first.
    """
    def __init__(self, groups, t0):
        self._heap = []  # (due_time, tiebreak_key, group_idx)
        for i, g in enumerate(groups):
            heapq.heappush(self._heap, (t0, g.dt, i))
        self._t_idx = [0] * len(groups)
        self._failed = [False] * len(groups)
        self._groups = groups

    def next_event(self) -> Optional[tuple[float, int]]:  # (due_time, group_idx)
    def record_completion(self, group_idx, completion_time):  # push next event
    def mark_failed(self, group_idx):  # remove all future events for this group
    def done(self) -> bool
```

Drift-mode `record_completion` pushes `(completion_time + g.dt, g.dt, group_idx)` if
`t_idx + 1 < g.Nt`. "Catch up immediately" falls out for free: the popped event's
`due_time` is in the past, so the wait at the top of the loop is `max(0, due - now) = 0`.

### Worker refactor

Replace the loop body at `multi_point_worker.py:L701-L753` with a scheduler-driven loop:

```python
self._scheduler = MultiGroupScheduler(self.groups, t0=self.timestamp_acquisition_start)
while not self._scheduler.done():
    if self.abort_requested_fn():
        break
    due, group_idx = self._scheduler.next_event()
    self._wait_until(due)  # spin-sleep matching current granularity
    if self.abort_requested_fn():
        break
    group = self.groups[group_idx]
    try:
        with self._timing.get_timer(f"group:{group.name}:pass"):
            self._run_group_pass(group, t_idx=self._scheduler.t_idx(group_idx))
        self._scheduler.record_completion(group_idx, time.time())
    except Exception:
        self._log.exception(f"Group {group.name} failed; skipping remaining timepoints")
        self._scheduler.mark_failed(group_idx)
        # continue main loop
```

`_run_group_pass` is the existing `run_single_time_point` body, refactored to take a region
subset (`group.region_ids`) and a per-group `t_idx`. The region iteration at
`multi_point_worker.py:L1639` (`scan_region_fov_coords_mm.items()`) becomes a filtered
iteration over `(rid, fovs) for rid, fovs in ... if rid in group.region_ids`.

`CaptureInfo` (`job_processing.py:L79-L100`) gains `group_id: str` and `nominal_time_s:
float` (the scheduler's predicted due time, distinct from the actual `capture_time`).
`frame_acquisition_times.csv` gains `group_id` and `nominal_time_s` columns.

Inter-timepoint sleep (`multi_point_worker.py:L748-L752`) is replaced by the
`_wait_until(due)` helper (same spin-sleep granularity, same abort polling).

### Save layout

```
{base_path}/{experiment_ID}/
  groups.yaml                          # group definitions (name, regions, dt, Nt)
  GroupA.zarr/                         # ZARR_V3: one archive per group
  GroupB.zarr/
  GroupA/                              # individual-image fallback
    A1/0/{time_point:05d}/...
    A2/0/{time_point:05d}/...
  GroupB/
    B5/0/{time_point:05d}/...
  acquisition_times.csv                # global, all groups, with group_id column
```

`SaveZarrJob` is modified to root at `{exp}/{group_name}.zarr` instead of `{exp}/zarr` or
`{exp}/plate.ome.zarr`. The existing per-well subtree inside the zarr stays the same. For
`INDIVIDUAL_IMAGES` / `MULTI_PAGE_TIFF` / `OME_TIFF`, the per-timepoint folder created at
`multi_point_worker.py:L916-L925` is rooted at `{exp}/{group_name}/...`.

`groups.yaml` is written once at `_create_new_experiment` (controller) by serialising the
`groups` list. Used by downstream analysis to recover the timing model.

### UI: WellplateMultiPointWidget

Below the existing `xy_controls_frame` add a new `groups_frame` containing:

1. **Group list** — a horizontal strip of `_GroupChipButton`s (modelled on the
   `_ChannelChipButton` shipped with per-point-channels). Each chip shows the group's
   palette color, name, dt, Nt, and member count. `+ New Group` chip at the end.
   Click a chip to select it as the active "paint" group.
2. **Paint mode toggle** — a checkable button next to the group list. When ON, clicks on
   the well selector add wells to the active group (mutually exclusive across groups);
   when OFF, the selector behaves as today.
3. **Group editor row** for the active group — `dt` spinbox, `Nt` spinbox, rename field,
   delete button. The existing top-of-widget `dt`/`Nt` entries stay (they drive the
   implicit Default group).
4. **Status banner** — a thin colored strip below the group list. Green "OK", yellow
   "Group X at risk of overrun", red "Group X failed at T+12:34". Driven by signals
   from worker.

The plate matrix (`well_selection_widget`) gets a paint-overlay delegate that tints each
well with its group's palette color (alpha ~50%) on top of the existing selection
highlight. Wells without a group appear unmodified — they belong to the implicit Default
group.

A region painted into Group A is **removed** from any other group (membership is
exclusive). The wellplate widget's existing well-selection state stays the source of
truth for "is this well selected at all"; group membership is a partition of the selected
set.

`save_multipoint_widget_config_to_cache` is extended to serialise `groups`. Schema
version bump.

### Timing estimator

```python
# timing_estimator.py
class TimingEstimator:
    def __init__(self, microscope_id: str, path: Path = ~/.squid/timing_estimates.yaml):
        self._table = self._load_or_default(microscope_id, path)
    def estimate_capture_cost(self, channel_name, exposure_ms) -> float:
        # bucket exposure to nearest 10ms; lookup mean from table; default if missing
    def estimate_group_pass(self, group, channels, n_z, has_af) -> float:
        # n_fov × (move_cost + AF_cost + n_z × Σ_channel(capture + z_step))
    def update_from_timing_report(self, report):
        # parse per-channel means, EWMA-blend into stored table
    def save(self):
        ...
```

Bootstrap defaults seeded from current measurements (the 184 ms/cap, 211 ms AF, 240 ms
move from `timings_full_af.txt`). Updated on every successful run.

`MultiPointController._on_acquisition_completed` (`:L1231`) calls
`timing_estimator.update_from_timing_report(self._worker._timing.get_report())` on
success.

### Preflight + live notifications

At the top of `MultiPointController.toggle_acquisition` (when starting):

```python
estimates = {g.name: timing_estimator.estimate_group_pass(g, ...) for g in groups}
total_run_length = max(g.Nt * g.dt for g in groups)
total_cost = sum(g.Nt * estimates[g.name] for g in groups)
if total_cost > total_run_length:
    show_modal("Estimated total cost exceeds total run length; consider reducing groups")
    return  # hard-fail
risks = [g.name for g in groups if estimates[g.name] > 0.8 * g.dt]
if risks:
    self.signal_overrun_risk.emit(risks)  # banner -> yellow
```

During acquisition the worker emits per-group elapsed times after each pass; controller
re-runs the risk check with actuals and emits banner updates.

### Schedule tab

New widget `software/gui/widgets/schedule_view.py`:

- `pyqtgraph.PlotWidget` rendering one horizontal lane per group (color = group color).
  X-axis = wall-clock time from acquisition start. Predicted ticks (open) vs actual
  (filled) vs in-progress (highlighted).
- Bottom panel: text ETA per group, current pass info, "next event in X seconds".
- Two states: **preview** (groups changed in the wellplate widget → recompute predicted
  schedule using `timing_estimator`) and **live** (subscribed to worker signals; updates
  every pass).
- Registered in `main_window.py:L766` area:
  `self.imageDisplayTabs.addTab(self.scheduleViewWidget, "Schedule")`.
- Constructed alongside the napari widgets; takes `multipointController` and the
  `timing_estimator` so it can render preview before any acquisition.

### TimingManager bucketing

Timer names already namespace by string. Add a `with self._timing.get_timer(f"group:{name}:...")`
prefix at the group-pass level so the report can be split per-group. Periodic flush
(every N passes or M minutes) so a multi-day run doesn't lose its timing data on crash.

## Implementation phases (each independently shippable)

1. **Backend data model + scheduler.** `GroupConfig`, `MultiGroupScheduler`,
   `AcquisitionParameters.groups`. Worker refactor with implicit-Default group; legacy
   path runs as a one-group acquisition. No UI changes yet — add a temporary CLI/test
   harness to verify the scheduler.
2. **Save layout.** Per-group archive paths, `groups.yaml`, `group_id`+`nominal_time_s`
   on `CaptureInfo` and CSV. Verify a single-group acquisition still yields valid output
   (matches today's structure modulo the group prefix).
3. **Wellplate UI: paint mode + group editor + status banner.** `_GroupChipButton`,
   palette, paint overlay on well selector, group list panel, banner. Cache schema bump.
4. **Timing estimator + preflight.** New file, persistence, EWMA update, modal on
   hard-fail, banner colour on collision risk.
5. **Schedule tab.** Pyqtgraph Gantt + ETA panel, preview + live signals, registered in
   `main_window`.
6. **Skip-group-on-error + per-group TimingManager bucketing.** Try/except in scheduler
   loop, mark-failed semantics, periodic timing flush.

## Verification

- **Scheduler unit checks**: build a `MultiGroupScheduler` with synthetic
  `(dt=10, Nt=5)` and `(dt=30, Nt=2)` groups; pop events with mocked
  `record_completion` times; assert correct order, drift behaviour, and
  smallest-dt-first tiebreak.
- **Single-group regression** (`conda activate squid`): run a no-groups
  acquisition (implicit Default group); confirm output structure is
  `experiment/Default.zarr/...` and `experiment/Default/<well>/<fov>/...`,
  same image content as before.
- **Two-group acquisition**: 4 wells in GroupA (`dt=30s, Nt=4`) + 4 wells in
  GroupB (`dt=90s, Nt=2`). Verify per-group archives written, CSV has correct
  `group_id` and `nominal_time_s` columns, and total wall time ~120 s.
- **Overrun (drift) check**: deliberately set GroupA `dt=5s` with a per-pass
  cost ~10s. Verify catch-up-immediately behaviour: passes back-to-back,
  banner turns yellow during run, log warns. `nominal_time_s` vs actual
  `unix_time_s` shows the drift in CSV.
- **Smallest-dt tiebreak**: two groups both due at the same instant, smaller
  dt runs first.
- **Skip-group-on-error**: deliberately throw inside one group's pass (e.g.
  raise from a channel filter); confirm other group continues, metadata
  marks the failed group.
- **Preflight modal**: define groups whose total estimated cost exceeds total
  run length; confirm modal at start, acquisition does not begin.
- **Schedule tab**: open the Schedule tab, change groups in the wellplate
  widget, verify the Gantt updates in preview mode. Start acquisition,
  verify live ticks and current-pass highlight.
- **Timing-estimator persistence**: after a successful run, inspect
  `~/.squid/timing_estimates.yaml`; confirm channel-level entries updated.
  Run again, confirm preflight uses the refined estimates.

## Open / future work (not in v1)

- Per-group channel / Z / AF overrides (the next natural extension)
- Manual / flexible / current-position groups
- Fluidics-aware grouping (rounds tied to per-group `t_idx` instead of global)
- Per-group abort / pause / resume
- Multi-membership ("control well in every batch")
- Cross-microscope timing-estimate sharing
- AF-reference re-anchor on group re-entry (becomes important if the
  laser-AF speedup plan's focus-map proposal doesn't land first)

## Refinement focus for remote planning

Areas where a deeper pass would add the most value, in priority order:

1. **Scheduler state machine.** Concrete pseudocode for the worker's main
   loop, including abort interleaving (must be safe to abort mid-pass,
   between events, and during the wait), what happens to in-flight save
   jobs when a group fails, and how `_finish_jobs` / outstanding-callback
   draining interacts with per-group failure marking. Identify any race
   conditions between the camera callback's frame dispatch and the
   scheduler popping the next event.

2. **Save layout migration details.** Exact mapping for each of the four
   `FileSavingOption` variants (INDIVIDUAL_IMAGES, MULTI_PAGE_TIFF,
   OME_TIFF, ZARR_V3) — what changes inside `SaveZarrJob`, what the
   `plate.ome.zarr` HCS-mode root becomes per group, how the existing
   `frame_acquisition_times.csv` schema gets bumped (with version field),
   and the `groups.yaml` schema (proposal: top-level `version`, list of
   groups with name/dt/Nt/region_ids/color_index, plus
   `acquisition_start_unix_s` for cross-archive alignment).

3. **Wellplate widget UI structure.** Concrete widget tree for the new
   group panel: where it sits relative to existing
   `xy_controls_frame` / Z-controls / Time-controls, how the existing
   global `dt`/`Nt` entries become the implicit Default group's editor
   (or whether they should be hidden once any explicit group is created),
   how cache schema versioning handles configs saved before this lands,
   paint-mode entry/exit UX, and how the well-selector overlay layer
   composes with `_WellShapeDelegate`.

4. **Timing estimator data model + EWMA.** YAML schema for
   `~/.squid/timing_estimates.yaml`, exposure bucketing rule, EWMA decay
   constant, missing-key fallback chain (channel-specific →
   channel-default → code default), and how `MultiPointController`
   resolves `microscope_id` (likely from machine config).

5. **Schedule tab signal wiring.** Concrete pyqtgraph rendering: lane
   layout, predicted-tick / actual-tick / in-progress visualization,
   ETA computation source. The signals from `MultiPointWorker` →
   `MultiPointController` → schedule view: per-pass-started, per-pass-
   completed, per-pass-failed, current-fov-changed; preview-mode
   refresh trigger from the wellplate widget. Performance: how often
   to redraw when a 96-well 12-group run is mid-flight.

6. **Per-group `TimingManager` bucketing + periodic flush.** Whether
   to keep a single `TimingManager` instance with prefixed timer names
   or one instance per group, and how the report at acquisition end
   composes the per-group sub-reports. Periodic flush mechanism
   (timer-driven? per-N-passes?) and where the partial reports get
   written so a multi-day crash recovers data.

7. **Edge cases worth nailing.** Single-region group with `Nt=1`
   (one-shot inside a multi-group run); group with empty
   `region_ids` (validation error vs silent skip); group with
   `dt=0` ("acquire as fast as possible"); duplicate group names;
   user editing a group definition mid-acquisition (out of scope?
   call out explicitly).
