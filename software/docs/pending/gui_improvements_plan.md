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

## B. Parameter grid → four labelled groups

```
Tiling per position          Focus
  Nx  Ny  Overlap  Snake       Contrast AF · Laser AF [Off ▸] · Use Focus Map
Z-stack                      Saving
  Nz  dz  From  Set range      Format ▾ (incl. "Don't save — dry run") · size estimate
Time-lapse                   Scan behaviour
  Nt  dt                       Keep illuminators on · Show live preview
```

6. Fold **Skip Saving** into the Save format dropdown as `Don't save (dry run)`.
   Size label already knows how to say "no files written"; postprocessing's
   start-time refusal becomes a greyed option.
7. **Laser AF** becomes a normal-height setting row in the Focus group; the
   big-button column keeps actions only.
8. Grey out `Z-stack from` / `Set Z-range` when Nz = 1.
9. `Simple ▾` combobox gets a header: `Channels · Simple ▾` above the list.
10. `ID` → `Experiment ID` with placeholder text.

## C. Actions column

11. `Snap Images` → `Acquire Current FOV`, half the height of Start, tooltip
    ("checked channels, one plane, at the current stage position, using the
    Save format above"). Fix folder name: `snapped_images<ID>_…` →
    `snap_<ID>_…` (`on_snap_images`, missing separator).

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

## Screenshots without hardware

`python tools/screenshot_multipoint.py <out_dir>` (from `software/`, squid env)
builds the whole GUI with every `SIMULATE_*` flag on and writes
`flexible_multipoint.png` / `wellplate_multipoint.png`. Takes ~60 s; the
`default` profile has no observation-state presets, so the channel list is
empty.
