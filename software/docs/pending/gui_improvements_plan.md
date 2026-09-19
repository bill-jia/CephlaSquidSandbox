# GUI improvements — work list (branch `gui-improvements`)

Ordered as we intend to do them. Items are grouped so each group is one commit
and one screenshot; the last group changes acquisition behaviour and is
deliberately last.

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

## D. Stage-motion behaviour (last — changes what the stage does)

12. **Move stage on click** checkbox (default on = today's behaviour).
    Today `cellClicked → go_to` always drives the stage; when off, clicking
    only selects. `Prev Pos` / `Next Pos` always move — they are navigation.
13. **Z home at 100 µm + retract for XY moves.**
    - Home = `OBJECTIVE_RETRACTED_POS_MM = 0.1` (already 100 µm in `_def.py`;
      reuse it, don't add a second constant).
    - Checkbox *"Retract Z to home for XY moves"*, default **on**. Sequence
      per move: Z → home (blocking), XY move, Z → target.
    - Applies in two places:
      a. GUI `go_to` (click, Prev/Next Pos).
      b. Worker `move_to_coordinate` **between regions only**, not between
         FOVs inside a region's tile grid. Today Z is issued non-blocking
         alongside XY; the retract path must serialise Z-up before XY.
    - Carried on `AcquisitionParameters` (like `skip_saving`) so it is
      recorded in `acquisition.yaml`.
    - Watch-outs: two extra Z moves per region visit (tens of ms each on the
      TMC path, plus settle); the AF z-cache (`_z_pos_proposal`) is the
      *target* Z, retract happens before it; `INVERTED_OBJECTIVE` flips
      which way is "away from the sample"; piezo Z is untouched.

## Decisions for D (2026-09-19)

- The retract **does** apply to the first move of a run and to the
  return-to-start move at the end.
- 100 µm (`OBJECTIVE_RETRACTED_POS_MM`) **is** the right clearance for
  inter-region travel on the current holder.

## D13 non-GUI core — done

The acquisition side of item 13 is in; only the checkbox is left.

- `MultiPointController.set_retract_z_between_regions(bool)` is what the
  checkbox calls (default **on**, mirrors `set_skip_saving`). It rides
  `AcquisitionParameters.retract_z_between_regions` into the worker and lands
  in `acquisition.yaml` under `acquisition:`.
- `MultiPointWorker.move_to_coordinate` brackets a move when it *enters* a
  region — `fov == 0`, or a region id different from the previous move, which
  also covers the very first move of a run and the inter-timepoint pre-move.
  Steps across a region's own tile grid are byte-for-byte unchanged.
- The end-of-run return lives on the controller, not the worker:
  `MultiPointController._move_back_to_start_position`.
- `OBJECTIVE_RETRACTED_POS_MM` goes to `stage.move_z_to` as-is, like the
  loading-position retract in `squid/stage/utils.py` — no raw→canonical
  conversion (that is only for the raw-units `Z_HOME_SAFETY_POINT`) and no
  `INVERTED_OBJECTIVE` sign flip. Piezo Z is untouched.
- Tests: `software/tests/control/test_multipoint_z_retract.py`.
- GUI `go_to` (item 13a) is still to do.

## Screenshots without hardware

`python tools/screenshot_multipoint.py <out_dir>` (from `software/`, squid env)
builds the whole GUI with every `SIMULATE_*` flag on and writes
`flexible_multipoint.png` / `wellplate_multipoint.png`. Takes ~60 s; the
`default` profile has no observation-state presets, so the channel list is
empty.
