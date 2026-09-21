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
    Set Z-range mode so its Z-min/Z-max rows hide. *(Round 2 replaced this with
    the Z-stack header checkbox; `_sync_z_stack_controls` is gone.)*
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
- **R2-layout (main tree): DONE.** `_make_parameter_block` (a `QFrame` named
  `multipointParameterBlock`) + `_make_checkable_section` + `_make_row_widget` +
  `_collect_widgets` + `_make_section(title, header_extra)` build both panels'
  left column; `_ZTimeGroupMixin` holds the group enable logic
  (`_apply_zstack_enabled` / `_apply_timelapse_enabled` / `_effective_NZ` /
  `_effective_Nt`), resolved through each panel's `_zstack_checkbox` /
  `_timelapse_checkbox` property (Flexible: `checkbox_zstack` /
  `checkbox_timelapse`; Wellplate keeps `checkbox_z` / `checkbox_time`).
  Wellplate gained `checkbox_set_z_range`, `entry_NX`/`entry_NY`,
  `radio_tiling_fraction`/`radio_tiling_grid` (`tiling_method_group`) and lost
  `combobox_z_mode`, `update_tab_styles`, `update_control_visibility`, the
  `*_not_selected_label`s, the hide/show and store/restore Z/Time machinery, and
  the `xy_frame`/`z_frame`/`time_frame`/`*_controls_*frame` colour carriers.
  Cache keys: `set_z_range`, `tiling_method`, `nx`, `ny` (a leftover `z_mode` key
  is ignored). YAML: `z_stacking_config` now drives `combobox_z_stack`; the loader
  carries no explicit z range, so `Set Z-range` is left alone on a drop. Snake
  scan moved to *Scan behaviour* on the Flexible tab. The `Method` selector only
  swaps which tiling rows are on screen — `Nx × Ny` is not generated yet.
  Tests: the Z/Time group cases in `test_flexible_region_state.py`.
- **R2-wire (main tree, after both): DONE.** Every coordinate path on the wellplate
  tab now goes through one of two private helpers — `_tile_wells()` (Select Wells)
  and `_tile_live(x, y)` (Current Position) — which are the single place the
  `radio_tiling_grid.isChecked()` branch lives, so `update_coordinates`,
  `update_well_coordinates` and `update_live_coordinates` cannot drift apart.
  `_tile_wells` always `clear_regions()` first: both methods name a region after the
  well and both generators skip a well that already has one, so without the clear a
  changed parameter (or a changed method) would leave the old grid standing.
  `_on_tiling_method_changed` = `update_scan_control_ui` + `update_coordinates`, wired
  to `radio_tiling_fraction.toggled` (one radio of an exclusive pair is enough), and
  `entry_NX/NY.valueChanged` re-tile as well as write the cache.
  `handle_objective_change` skips the coverage recompute for grids and just re-tiles.
  `toggle_acquisition` pushes `set_NX`/`set_NY` (the grid values, or 1 for the
  fraction method) so `acquisition.yaml` records what was really tiled — they are
  metadata only, the worker does not read them.
  YAML: `AcquisitionYAMLData.nx/ny` (and `delta_x_mm`/`delta_y_mm`) are now read from
  whichever scan section the file carries, not just `flexible_scan` — the wellplate
  writer has always emitted `nx`/`ny` under `wellplate_scan`. A drop with `nx*ny > 1`
  selects `Nx × Ny` and fills the spinboxes; `1x1` says nothing (it is also what a
  fraction-of-well run writes) and leaves the method alone.
  Z-range rows are now identical on both tabs: the Flexible tab gained
  `goto_minZ_button` / `goto_maxZ_button` and `goto_z_min` / `goto_z_max` (a plain
  `move_z_to`; the retract-Z-for-XY-moves setting does not apply to a Z-only move),
  and its `Set` buttons are labelled `Set Z-min` / `Set Z-max`.
  `tools/screenshot_multipoint.py` gained `--select-wells A1,B2`, `--tiling
  fraction|grid|both` with `--nx/--ny`, and `--nav-shot`; it raises each panel's tab
  *before* touching it, because the panels ignore coordinate updates from a background
  tab. `--tiling both` writes `wellplate_fraction.png` + `wellplate_grid.png`.
  Tests: `software/tests/control/test_wellplate_tiling_method.py` (9 cases) plus the
  wellplate-grid case in `test_acquisition_yaml_loader.py`.

## Follow-ups (found during R2-wire, deliberately not fixed here)

- FIXED (item 9, confocal partial failures): `set_xlight_confocal_mode` now raises
  instead of moving the disk when the motor cannot be confirmed running, and both
  `ObservationStateController.apply_confocal_mode` and the panel's
  `_on_disk_position_toggled` no longer record/light confocal on a failed move; the
  panel's motor switch is now refreshed on failure too, and the error is logged. See
  `tests/control/test_confocal_widget_sync.py`, `tests/control/core/test_observation_state_confocal.py`.
- FIXED (stale doc): `software/docs/confocal-panel-sync.md` now documents the
  `SegmentedSwitch`-based Widefield/Confocal and Disk Off/Disk On controls,
  `set_xlight_confocal_mode`'s auto-start, and the failure semantics above.

- FIXED: `ScanCoordinates.add_flexible_region` never populates `region_shapes`, so
  `region_contains_coordinate` raises `KeyError` for regions defined on the Flexible
  panel (bites focus-map point generation, `control/core/core.py:~1963`). The wellplate
  grid method dodges it only because `_add_well_grid_region` sets the shape by hand
  afterwards. Every `add_*` region method now funnels through a shared
  `_register_region` that always records a shape ("Square" for grid/template/single-FOV
  regions, whose bounding box is exact), so `_add_well_grid_region`'s hand-set shape was
  removed as redundant. See `test_scan_coordinates.py`'s
  `test_region_contains_coordinate_for_*` tests.
- FIXED: The wellplate tab's non-range Z span was `z + dz * (Nz - 1)` where `z` is in
  mm and `dz` in µm (`entry_deltaZ` has a µm suffix), giving a span ~1000x too large;
  the Flexible tab already divided `dz` by 1000 in the same expression. Both
  `toggle_acquisition` methods now share one `_ZTimeGroupMixin._compute_z_range`
  helper so the two panels cannot diverge again. See
  `test_wellplate_tiling_method.py` / `test_flexible_region_state.py`.
- Channels box placement: **DONE.** The channel list was a full-width box under both
  columns; it is now the top of a right-hand *group* that mirrors the left box, built for
  both tabs by one helper, `_make_acquisition_body(widget, focus_section, saving_section,
  scan_section)`. The panel body is a single QHBoxLayout of two 1:1-stretch columns: the
  parameter block on the left (topped, with a stretch under it, as before) and, on the
  right, a QVBoxLayout of the *Channels* header + `list_configurations` (the only item
  with vertical stretch, so it absorbs the spare height and makes the right group as tall
  as the left box) above a QHBoxLayout of two 1:1 subcolumns — *Focus* then *Scan
  behaviour* then stretch; *Saving* then stretch then `btn_snap_images` (1) and
  `btn_startAcquisition` (2), which puts Start in the panel's bottom-right corner. At half
  the panel width the *Saving* rows no longer fit on one line, so
  `_make_file_saving_format_row` returns a QVBoxLayout with the size estimate
  right-aligned under the dropdown, and `_make_zarr_streaming_row` returns two lines
  (enable + delete-after-verify, then path + Browse); both attribute names
  (`fileSavingFormatRow`, `zarrStreamingRow`, `label_size_estimate`) are unchanged, and
  so is `self.grid_acquisition` (now the body row itself), which `TemplateMultiPointWidget`
  still inserts between its template row and the progress row. The progress bar stays
  full-width at the bottom.
- FIXED (retract-Z-for-XY-moves review, item 3): the retract bracket fired on
  zero-travel moves (single-position time-lapse, repeated "Acquire Current FOV")
  even though the stage never left the target XY; `move_xy_with_z_retract` now
  guards on real XY travel (`RETRACT_MIN_XY_TRAVEL_MM`, 5 um).
- FIXED (retract-Z-for-XY-moves review, item 5): the bracket existed as three
  hand-rolled copies; consolidated into one `move_xy_with_z_retract` helper in
  `squid/stage/utils.py`, used by `MultiPointWorker.move_to_coordinate`,
  `MultiPointController._move_back_to_start_position` and the AF-map return hop,
  the laser-AF seed scan, `AutoFocusController.gen_focus_map`, and
  `FlexibleMultiPointWidget._move_stage_to_position`.
- FIXED (retract-Z-for-XY-moves review, item 4): `MultiPointWithFluidicsWidget`
  gained the same "Retract Z ... for XY moves" checkbox as the other tabs
  (default on), and `microscope_control_server._configure_controller_from_yaml`
  now forwards a YAML's explicit `retract_z_between_regions` to the controller.
- FIXED (retract-Z-for-XY-moves review, item 8): a table click within 1 um of
  the stage's current XYZ only selects the row (fixes the double-click-to-rename
  excursion); a same-XY, different-Z click is a plain Z move via the same
  travel guard.
- FIXED (1): The Nx x Ny grid path baked the stage Z into every FOV (3-tuples), so the
  worker drove Z back to that stale value at every well; `add_flexible_region` now takes
  `center_z=None` for "no Z on the FOVs" and the well-grid path passes it.
- FIXED (6): The Wellplate drop handler guessed the tiling method from `nx*ny > 1`, so a
  pre-branch fraction-of-well `acquisition.yaml` with a stale shared NX/NY flipped the
  radio to Nx x Ny; `wellplate_scan.tiling_method` is now written and read explicitly, and
  the drop handler only switches methods when the tag is present.
- FIXED (7): Disabling the `[x] Z-stack` group force-unchecked `checkbox_set_z_range`,
  whose own toggle overwrote the user's Z-min/Z-max entries and collapsed Nz to 1;
  `_apply_zstack_enabled` now only greys the group and leaves `Set Z-range` untouched.
- FIXED (10): `FlexibleMultiPointWidget.toggle_acquisition` never re-pushed NX/NY, so a
  Flexible run after a Wellplate one recorded a stale `nx: 1, ny: 1`; it now calls the new
  `_push_tiling_grid_to_controller` helper alongside its other per-tab pushes.
- FIXED (11): `ScanCoordinates._tile_selected_wells` still carried the add/remove diffing
  from before the GUI's `_tile_wells` started clearing and rebuilding on every re-tile;
  the dead deselection branch is gone, keeping only the glass-slide fallback and the
  empty-selection clear.
