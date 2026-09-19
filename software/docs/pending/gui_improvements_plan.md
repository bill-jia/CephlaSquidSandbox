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
