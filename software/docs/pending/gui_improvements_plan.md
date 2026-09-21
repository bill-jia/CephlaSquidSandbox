# GUI improvements — work list (branch `gui-improvements`)

All of it has shipped; this is the record of what went in, in the order it went
in. Items were grouped so each group was one commit and one screenshot, with the
group that changes what the stage does left deliberately last.

Both `FlexibleMultiPointWidget` and `WellplateMultiPointWidget`
(`software/gui/widgets/multipoint.py`) share almost all of this code; every
layout change is applied to both.

## Done

- Spinning Disk Confocal panel: `Widefield | Confocal` and `Disk Off | Disk On`
  are segmented switches (`SegmentedSwitch`, `gui/widgets/common.py`) that
  show the current hardware state; entering confocal spins the disk up first
  (`set_xlight_confocal_mode`, `control/serial_peripherals.py`).
- A. Positions block (`FlexibleMultiPointWidget`, the only widget with a
  positions table): columns are `x | y | z | AF Ref | Region Name` with the
  wide editable name column last and every index taken from the class
  constants `_COL_X/_COL_Y/_COL_Z/_COL_AF_REF/_COL_NAME`; the header carries
  `Add  Remove  Clear  Import  Export` and the row under the table carries
  `Prev Pos  Next Pos  Set Z from Stage  Recapture AF Ref`. `Prev Pos` wraps
  backwards (and starts at the last row with nothing selected). `Recapture AF
  Ref` is greyed out, with a tooltip saying why, unless Laser AF is on — it
  follows `LaserAutofocusButton.toggled`, and the old error dialog is gone.
  The empty-list hint now reads *"no positions — Start acquires the current
  field"*, which is what `acquisition_in_place` does.
- B + C. Option groups and the actions column (both multipoint widgets, items
  6-11). The unlabelled 8-column parameter grid is gone; options now sit under
  bold section headers built by `_make_section` — *Tiling per position*,
  *Z-stack*, *Time-lapse* on the left, *Focus*, *Saving*, *Scan behaviour* on
  the right (the Wellplate tab keeps its XY/Z/Time boxes on top and gets the
  same groups below, with `Snake scan` in *Scan behaviour* since it has no
  tiling group). Details:
  - **Skip Saving is gone as a checkbox**: the save-format combo's last entry is
    `DRY_RUN_SAVE_FORMAT` ("Don't save (dry run)"). `_push_save_format_to_controller`
    splits the selection into `set_skip_saving` / `set_file_saving_option`
    (the dry-run string is not a `FileSavingOption` and must never reach the
    enum conversion); `_refresh_size_estimate` and the pre-start disk-space
    check read the combo via `_is_dry_run`. `AcquisitionYAMLData.skip_saving`
    is new, and `_apply_save_format_from_yaml` selects the dry-run entry when a
    dropped `acquisition.yaml` has `skip_saving: true`.
  - **Laser AF** is a `Laser AF [Off ▸]` row in the *Focus* group at normal
    height. `LaserAutofocusButton` dropped the "Laser AF: " prefix from its own
    text (the label beside it carries the name) — the fluidics widget got the
    same label row.
  - **Nz = 1** greys `From` and `Set Z-range` (`_sync_z_stack_controls`, also
    re-applied after `setEnabled_all(True)`); dropping to one plane leaves
    Set Z-range mode so its Z-min/Z-max rows hide.
  - **Channels header**: `<b>Channels</b>  Simple ▾  …  Per-Point Channels
    Edit Cycles` above a full-width list; both buttons are single-line now.
  - `ID` → `Experiment ID` with the placeholder *optional name, prefixed to the
    folder* on both tabs.
  - **Actions column** holds only `Acquire Current FOV` (stretch 1, was
    `Snap Images`) and `Start Acquisition` (stretch 2). `on_snap_images` now
    names the dataset `snap_<ID>` (or `snap`), not `snapped images<ID>`.
  - Tests: `software/tests/control/test_save_format_dry_run.py`.
- D. Stage-motion behaviour (items 12-13), two checkboxes:
  - **Move stage on click** (Flexible only, default on), at the right end of the
    `Prev Pos / Next Pos / Set Z from Stage / Recapture AF Ref` row. Off, a table
    click only selects the row (`_select_row`); `Prev Pos` / `Next Pos` still move,
    they are navigation. It locks with the rest of the positions block during a run.
  - **Retract Z to 100 µm for XY moves** in *Scan behaviour* on **both** tabs,
    default on (the controller default). The label is built from
    `OBJECTIVE_RETRACTED_POS_MM` so it cannot go stale. `toggled` →
    `set_retract_z_between_regions`, and each tab re-pushes its own value in
    `toggle_acquisition` beside the save-format push, because one controller is
    shared by two checkboxes.
  - `FlexibleMultiPointWidget._move_stage_to_position` is the bracket for the
    GUI's own moves (click, Prev/Next Pos): Z → `OBJECTIVE_RETRACTED_POS_MM`
    (blocking), X, Y, then Z → target. Unchecked it is the old X, Y, Z sequence.
    `on_snap_images` is untouched — a snap does not move XY.
  - `acquisition.retract_z_between_regions` round-trips: optional in
    `AcquisitionYAMLData` (absent → the checkbox is left alone), applied by
    `_apply_retract_z_from_yaml` on both tabs' drop path.
  - Decisions taken: the retract applies to the first move of a run and to the
    return-to-start at the end; 100 µm is the right clearance for inter-region
    travel on the current holder.
  - Acquisition-side core (landed first): `MultiPointController.
    set_retract_z_between_regions` rides `AcquisitionParameters` into the worker;
    `MultiPointWorker.move_to_coordinate` brackets a move only when it *enters* a
    region (`fov == 0` or a new region id), so steps across a region's own tile grid
    are unchanged; the end-of-run return lives in
    `MultiPointController._move_back_to_start_position`. The retract height goes to
    `stage.move_z_to` as-is — no raw→canonical conversion, no `INVERTED_OBJECTIVE`
    flip — and piezo Z is untouched.
  - Tests: `software/tests/control/test_multipoint_z_retract.py`, plus the stage-motion
    cases in `test_flexible_region_state.py` and the YAML field in
    `test_acquisition_yaml_loader.py`.

## Screenshots without hardware

`python tools/screenshot_multipoint.py <out_dir>` (from `software/`, squid env)
builds the whole GUI with every `SIMULATE_*` flag on and writes
`flexible_multipoint.png` / `wellplate_multipoint.png`. Takes ~60 s; the
`default` profile has no observation-state presets, so the channel list is
empty.

---

# Round 2 (2026-09-21): uniform Positions / Tiling / Z-stack / Time-lapse block

Goal: the left column of both multipoint panels is the *same* boxed block,
built by one helper, so a user who knows one panel knows the other. The only
permitted difference is Wellplate's extra tiling method ("fraction of well").
The Channels box stays where it is for now; its placement is decided after
screenshots.

## Target layout (left column, one framed box; right column unchanged)

```
┌ Positions ─────────────────────────────────────────────────────┐
│ Flexible:  the positions table + its header/action rows          │
│ Wellplate: [x] XY   Mode ▾ (Current Position / Select Wells /    │
│            Manual / Load Coordinates)   … Save / Load coordinates │
├ Tiling per position ──────────────────────────────────────────── ┤
│ Wellplate only:  Method  (•) Fraction of well   ( ) Nx × Ny       │
│   fraction:      Shape ▾   Scan size [mm]   Coverage [%]          │
│ Both:            Nx [ ]  Ny [ ]   Overlap [ %]                    │
│                  (dx/dy variant when use_overlap is False)        │
├ [x] Z-stack ──────────────────────────────────────────────────── ┤
│   Nz [ ]  dz [ µm]  From ▾ (Bottom / Center / Top)  [ ] Set Z-range│
│   Z-min … Z-max … rows (visible only with Set Z-range)            │
│   [ ] Piezo Z-stack (HAS_OBJECTIVE_PIEZO)                         │
├ [x] Time-lapse ───────────────────────────────────────────────── ┤
│   Nt [ ]  dt [ s]                                                 │
└──────────────────────────────────────────────────────────────────┘
```

The box is a `QFrame` with `StyledPanel | Plain` and a 1-px `palette(mid)`
border; section titles inside stay the bold-label style of `_make_section`,
except that Z-stack and Time-lapse titles are *checkboxes* that enable the
group.

## Decisions

- **Enable checkboxes (both panels).** `[x] Z-stack` / `[x] Time-lapse`
  header checkboxes grey the group's controls when off (no hiding, no
  placeholder labels, no store/restore of values) and push `set_NZ(1)` /
  `set_Nt(1)` to the controller; when on, push the spinbox values. At start,
  `toggle_acquisition` uses the effective values. Flexible's default is
  unchecked (Nz/Nt start at 1); Wellplate keeps its cached state. Flexible's
  `_sync_z_stack_controls` (grey From/Set Z-range at Nz==1) is subsumed: the
  group is either on or off.
- **One `From ▾` per panel** = the AF reference plane (`combobox_z_stack` →
  `set_z_stacking_config`). Wellplate's `combobox_z_mode`
  (`From Bottom | Set Range`) is deleted; its "Set Range" becomes the
  `Set Z-range` checkbox exactly as in Flexible. Fix the YAML apply path that
  mapped `z_stacking_config` into `combobox_z_mode`: `z_stacking_config` now
  drives `combobox_z_stack`, and `Set Z-range` is derived from whether the
  yaml carries an explicit z range (check what the loader provides).
- **Wellplate XY block**: the `XY` checkbox + mode combobox stay (unchecked =
  current position, as today); the Save/Load coordinates controls sit on the
  same row(s). The coloured `xy_frame / z_frame / time_frame`,
  `update_tab_styles`, `*_not_selected_label`, `hide_/show_z_controls`,
  `store_/restore_z_parameters`, `store_/restore_time_parameters` are
  removed. `save_multipoint_widget_config_to_cache` / the restore path change
  keys: `z_mode` → `set_z_range: bool`; add `tiling_method`, `nx`, `ny`.
- **Wellplate tiling methods**: `Fraction of well` (today's Shape / Scan size /
  Coverage) and `Nx × Ny` (Flexible's grid centred on each well centre, or on
  the stage position in Current Position mode). The method row and the rows
  it governs are shown only in Select Wells / Current Position modes (Manual
  and Load Coordinates define their own FOVs, as today). `Overlap` applies to
  both methods.
- **Snake scan** moves from Flexible's Tiling group to Scan behaviour, matching
  Wellplate.
- `dt`/`Nt` live in the Time-lapse group (with the checkbox), so the separate
  `time_controls_frame` is gone.

## Work split

- **R2-core (worktree, no GUI):** `ScanCoordinates.set_well_coordinates_grid(nx, ny, overlap_percent)`
  and `set_live_scan_coordinates_grid(x_mm, y_mm, nx, ny, overlap_percent)`
  in `control/core/scan_coordinates.py`, mirroring `set_well_coordinates` /
  `set_live_scan_coordinates` but tiling with `add_flexible_region` semantics
  (same overlay/navigation-viewer behaviour, same region naming as the
  fraction method so per-region maps keep working). Unit tests with the
  existing ScanCoordinates test fixtures.
- **R2-layout (main tree):** the shared block builder + both panels rebuilt
  on it, Z/Time enable checkboxes, Wellplate `combobox_z_mode` → Set Z-range,
  dead style/store machinery removed, cache keys, YAML fix, snake → Scan
  behaviour. Leaves a `Method` selector in the Wellplate tiling group wired
  to nothing but the fraction rows' visibility.
- **R2-wire (main tree, after both):** wire `Nx × Ny` to the R2-core API in
  `update_coordinates`, `update_well_coordinates`, `update_live_coordinates`,
  the cache and the YAML path; screenshots of both panels with wells selected.
