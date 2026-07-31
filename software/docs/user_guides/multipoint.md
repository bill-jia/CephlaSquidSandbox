# Multipoint Acquisition — User Guide

This guide explains how to set up and run a multipoint acquisition from the GUI:
choosing where to image (wells or arbitrary positions), what to image (channels,
Z‑stacks, time‑lapse), how to keep it in focus, and where the data goes.

If you are new to the software, read **Orientation** and **Core concepts** first,
then jump to **Wellplate acquisition** (imaging a plate) or **Flexible acquisition**
(imaging a hand‑picked set of positions).

---

## Orientation — where things are in the GUI

The acquisition controls are split between two areas of the main window:

- **Acquisition tabs (top‑right panel).** A row of tabs that includes
  **Wellplate Multipoint**, **Flexible Multipoint**, and — depending on your rig —
  **Template Multipoint**, **Multipoint with Fluidics**, **Tracking**,
  **Simple Recording**, and **Fast Acquisition**. Each tab is a self‑contained
  acquisition panel. This guide covers the two you will use most: *Wellplate
  Multipoint* and *Flexible Multipoint*.

- **Navigation section (bottom‑right).** This is the "wellplate navigator". It holds:
  - the **Sample Format** dropdown (and objective selector) at the top, and
  - the **plate / slide map** below it — a live visual overview of your sample
    where the current field of view, the planned scan grid, and autofocus points are
    drawn.

- **Well‑selection grid (dock).** A clickable grid of wells laid out like the physical
  plate (rows A, B, C…; columns 1, 2, 3…). You select wells here and they become the
  regions the acquisition will scan. This grid is hidden for the "glass slide" format.

- **Stage panel.** Numeric X/Y/Z position readouts and jog buttons, plus a
  **Click to Move** checkbox. Use this (or the plate map) to drive the stage.

- **Image display tabs (main viewing area).** **Live View**, **Multichannel
  Acquisition**, **Mosaic View**, and optionally **Plate View** and **NDViewer** —
  these show images as they are captured. The Live View status bar (bottom of the
  window) shows the cursor position, pixel value, and stage/piezo position, plus two
  measurement tools that become available once the first image is displayed:
  - **Line Profiler.** Toggle on, then click a start and end point to drop a
    draggable, rotatable line; an intensity profile along the line is plotted below
    the image and updates live.
  - **Crosshair.** Toggle on to overlay a draggable crosshair (a full‑span vertical
    and horizontal line). Drag either line to position the center; its image pixel
    coordinates are shown in the status bar. Toggle off to hide it.

> Which tabs and panels appear depends on your machine configuration and attached
> hardware. Laser autofocus, the objective piezo, fluidics, the 1536‑well selector,
> and the Plate View tab only show up when the relevant feature is enabled.

---

## Core concepts

**Observation state = channel.** An *observation state* is the complete light‑path
configuration for one acquisition step — illumination source and intensity, camera
exposure/gain, emission filter, Z‑offset, and so on. It is the equivalent of a
"channel" in other microscopy software (and can drive more than one camera at once).
Observation states are saved as **named presets per user profile**; you pick which
ones to image from a checkbox list. The preset name also becomes part of the saved
file names.

**Region.** A region is one area to be imaged — a selected well, an imported
coordinate, or a manually added position. Each region is covered by a grid of
overlapping fields of view (tiles).

**FOV / tile.** A single field of view (one camera frame). A region is scanned as a
grid of FOVs with a configurable overlap so the tiles can be stitched later. The FOV
size used to space tiles is the *actually saved* frame — it reflects binning, the
software crop, **and the hardware ROI**. If you shrink the camera ROI (e.g. a centered
ROI smaller than the configured crop), the tile spacing shrinks with it so the overlap
percentage still holds; otherwise tiles would be spaced for a larger image than is saved
and leave gaps.

**The four acquisition dimensions.** A multipoint run is the product of:
- **XY** — which regions, and the tile grid within each region;
- **Z** — an optional focal stack (number of planes `Nz`, spacing `dz`);
- **Channel** — the checked observation states; and
- **Time** — an optional time‑lapse (number of timepoints `Nt`, interval `dt`).

---

## Quick start (wellplate)

1. Open the **Wellplate Multipoint** tab.
2. In the navigation section, set **Sample Format** to your plate (e.g. "96 well
   plate"). Calibrate it first if this plate/holder has never been calibrated (see
   [Calibrating a plate format](#calibrating-a-plate-format)).
3. In the panel, set the **XY** mode to **Select Wells**.
4. Click the wells you want in the **well‑selection grid** (click, drag, Ctrl‑click,
   Shift‑click).
5. Set **Scan Size** (or **Coverage**) and **FOV Overlap** for the per‑well tile grid.
6. Check the **observation states** (channels) you want in the list.
7. (Optional) Enable **Z**, **Time**, and an autofocus mode.
8. Set the **Saving Path** and **Experiment ID**.
9. Click **Start Acquisition**.

The rest of this guide explains each step in detail.

---

## Wellplate acquisition

Use the **Wellplate Multipoint** tab when your sample sits in a multi‑well plate (or a
calibrated holder) and you want to image whole wells.

### Step 1 — Choose the sample format

In the navigation section, pick your holder from the **Sample Format** dropdown. Built‑in
formats include:

- glass slide
- 6 / 12 / 24 / 96 / 384 / 1536 well plate
- plus any custom formats you have calibrated.

Choosing a format reconfigures the plate map, the well‑selection grid, and the
coordinate model all at once. The last dropdown entry, *"calibrate format…"*, is not a
real format — it opens the calibration dialog (next step).

#### Calibrating a plate format

Calibration tells the software where well **A1** actually sits in stage coordinates.
Do this once per physical plate/holder geometry (or when a plate seats differently).

1. Select **"calibrate format…"** in the Sample Format dropdown to open the
   **Well Plate Calibration** dialog.
2. Choose **Add New Format** (define a brand‑new plate) or **Calibrate Existing
   Format** (re‑locate A1 or tweak parameters for a format you already have).
   - For a new format, fill in the name, **# Rows**, **# Columns**, **Plate Width/
     Height**, the **A1 X/Y offset (drawing)** values (these only affect how the map is
     drawn, not stage positioning), **Well Spacing**, **Well Shape** (Circle/
     Rectangle), and well size.
3. Choose a **Calibration Method**:
   - **3 Edge Points** — recommended for large wells. You will capture three points
     around the rim of well A1.
   - **Center Point** — recommended for small wells (auto‑selected for 384/1536).
4. Drive the stage to well A1 using the dialog's live camera view (**Click to Move**)
   and/or the **Virtual Joystick** (with an adjustable **Joystick Sensitivity** slider).
5. Capture the position(s):
   - Edge mode: position on each of three rim points and click **Set Point** for each
     (**Clear Point** undoes one).
   - Center mode: center on A1 and click **Set Center** (**Clear Center** undoes it).
6. Click **Calibrate** (enabled once the required points are set). On success the
   dialog saves the format, regenerates the plate map, selects the format in the main
   dropdown, and closes.

For existing custom formats you can also use **Update Parameters** (apply spacing/size/
offset changes without recapturing A1) and **Delete Format** (built‑in formats cannot be
deleted). Closing the dialog without calibrating reverts to your previous format.

> See also [wellplate-configs.md](wellplate-configs.md) for the underlying format
> definition files.

### Step 2 — Set the acquisition mode

The panel has three mode "tabs" near the top — **XY**, **Z**, and **Time** — each with a
checkbox that turns that dimension on. For a wellplate run, leave **XY** checked and pick
a mode from its dropdown:

| XY mode | What it does |
|---|---|
| **Select Wells** | Image the wells you select in the well‑selection grid. This is the standard plate workflow. |
| **Current Position** | Image a single FOV at wherever the stage currently is (no tiling). |
| **Manual** | Draw arbitrary region shapes on the Mosaic View (becomes available once the mosaic layers initialize). |
| **Load Coordinates** | Load a CSV of FOV coordinates instead of computing them from wells. |

The instructions below assume **Select Wells**.

### Step 3 — Select wells

In the **well‑selection grid**, click the wells you want to image:

- **Click** a well to select it.
- **Click‑drag** to rubber‑band a rectangular block of wells.
- **Ctrl‑click** to add/remove individual wells.
- **Shift‑click** to extend a range; click a row/column header to select that whole
  row/column.
- **Double‑click** a well to drive the stage to that well's center.

Selected wells are highlighted and immediately become the scan regions. Edge wells that
the format marks as "skip" (e.g. the outer ring on a 384‑well plate) are not selectable.

The plate map in the navigation section mirrors this: it draws the current camera FOV
(red outline), the planned scan tiles (yellow), and acquired FOVs (blue) as the run
proceeds. **Double‑click anywhere on the map** to drive the stage to that location.
The **Clear Scan Grid** button on the map clears the drawn FOV overlay.

### Step 4 — Set the per‑well tile grid (XY scan controls)

These controls define how each selected well is tiled:

| Control | Meaning |
|---|---|
| **Scan Shape** | Tile pattern within the well: **Square**, **Circle**, or **Rectangle**. |
| **Scan Size** | Extent of the scan region in mm. |
| **Coverage** | The fraction of the well covered, shown as a percentage. In Select Wells mode this is derived from Scan Size (the two are linked). |
| **FOV Overlap** | Percent overlap between adjacent tiles (default 10%). Higher overlap = easier stitching, more tiles, slower. |

Below these is a **Save Coordinates** button that writes the computed FOV coordinates to
a CSV (one file per objective, columns `region`, `x (mm)`, `y (mm)`). When coordinates
are already loaded, the same button reads **Clear Coordinates** instead.

In **Load Coordinates** mode, the scan‑shape controls are replaced by a **Load New
Coords** button and a read‑only field showing the loaded file path. The CSV must contain
`region`, `x (mm)`, and `y (mm)` columns.

### Step 5 — Choose channels (observation states)

A **Simple / Advanced** dropdown next to the channel list controls what the checklist
holds. It defaults to **Simple** every time you open the widget.

- **Simple** (default) — the checklist lists single **observation‑state presets**
  (channels); each checked one is imaged **once** per position, like standard microscope
  software. This is all most acquisitions need.
- **Advanced** — the checklist lists **acquisition cycles** (per‑position sequences of
  states with frame counts, waits, and repeats) and an **Edit Cycles** button appears.
  Use this for voltage‑imaging / optogenetics‑style protocols. See
  [acquisition-cycles.md](../acquisition-cycles.md).

Switching modes clears the current selection (cycle names and channel names are not
interchangeable). The underlying acquisition is identical either way — Simple is just a
cycle of one‑frame steps — and the **size estimate** (Step 9) works in both modes.

For the checklist itself:

- **Drag** rows to reorder them — this sets the channel acquisition order.
- Checking a row floats it to the top of the checked block, so selected channels stay
  grouped at the top.
- Rows shown in *italic* are NIDAQ‑timed presets (pulse‑timed illumination or
  stimulus‑only steps); hover for a tooltip. These require a working NIDAQ on the rig.

By default every checked channel is imaged at every selected well. To image **different
channels at different wells**, use **Per‑Point Channels** (see
[Per‑point / per‑well channels](#per-point--per-well-channels)).

#### Tile spacing vs. channel ROIs

Each observation state carries its own camera ROI, and the saved tile size depends on it.
Two things keep the overlap correct across a mixed‑ROI acquisition:

- **Tiling is recomputed at run time** for the FOV that will actually be imaged, so the
  overlap you set is honored even if you changed the camera ROI/binning *after* laying out
  the regions. (The on‑screen tile preview is refreshed to match.)
- **The largest ROI in the group sets the spacing.** If the checked channels don't all use
  the same ROI, tiles are spaced so the largest‑ROI channel keeps its overlap; channels
  with a smaller ROI then under‑sample (leave gaps between their tiles). Because that may be
  deliberate subsampling *or* a mistake, starting such an acquisition pops a warning listing
  the mismatched channels and their FOVs, and asks you to confirm before continuing.

### Step 6 — (Optional) Z‑stack

Check the **Z** mode box to acquire a focal stack. Set **dz** (step size, µm) and
**Nz** (number of planes), and pick where the stack sits relative to the focal plane
with the **Z‑stack from** dropdown:

- **From Bottom (Z‑min)** — the focal plane is the first (bottom) slice; the stack is
  built upward (`+dz` per plane).
- **From Center** — the focal plane is the middle slice; the stack extends
  `±(Nz‑1)/2 · dz` around it.
- **From Top (Z‑max)** — the focal plane is the last (top) slice; the stack is built
  downward.

**Interaction with autofocus:** whatever focal plane autofocus lands on (see Step 8)
is the reference the dropdown is measured from — AF runs once per position *before* the
stack is positioned, so the slices are always drawn relative to the freshly focused
plane (for laser AF this includes the per‑region reference captured with **Update Ref**).
This works with Contrast AF and Laser AF in all three modes.

- **Set Range** — alternatively define **Z‑min** and **Z‑max** explicitly. Use the
  **Set Z‑min** / **Set Z‑max** buttons to capture the current stage Z, and **Go To** to
  move there. In this mode **Nz** is computed automatically from the range and dz.
  (Laser AF is disabled while Set Range is active.)

If your rig has an objective piezo, the **Piezo Z‑Stack** checkbox uses it to drive the
stack instead of the stage Z motor.

### Step 7 — (Optional) Time‑lapse

Check the **Time** mode box, then set:

- **dt** — interval between timepoints, in seconds.
- **Nt** — number of timepoints.

### Step 8 — Autofocus

Three independent focus aids are available (combine as needed):

- **Contrast AF** — software contrast‑based autofocus run during the acquisition.
- **Laser AF** — the reflection/laser autofocus button (only present on rigs with the
  laser‑focus camera). Its label shows the current mode: *Laser AF: Off*, *Every FOV*,
  or *Fast (N=…)*. Click it to open the laser‑AF settings dialog.
- **Use Focus Map** — fits a focus surface from a set of measured points (configured in
  the **Focus Map** tab) and follows it during the scan. When checked, the surface is
  fitted at Start; if the fit fails the acquisition will not begin.

**Autofocus log.** Whenever autofocus is enabled, every position at which it runs is
recorded to `autofocus_log.csv` at the dataset root, with columns
`position_index, t_index, x, y, z_expected, z_actual, af_status`. `z_expected` is the
target Z before AF; `z_actual` is the Z after correction (or, on `af_status=failed`, the
Z the acquisition fell back to). Use it to audit focus drift and AF reliability over a run.

### Step 9 — Saving and output options

- **Saving Path** — base folder for the output. Use **Browse** to pick it; the last‑used
  path is remembered. (You must set this before starting.)
- **Experiment ID** — free‑text name; becomes part of the dataset folder name.
- **Save format** — how images are written:

  | Format | Description |
  |---|---|
  | **INDIVIDUAL_IMAGES** | One TIFF (or PNG/BMP) per (region, FOV, Z, channel). |
  | **MULTI_PAGE_TIFF** | One multi‑page TIFF per FOV (pages = Z × channel × time). |
  | **OME_TIFF** | OME‑TIFF stacks (TZCYX) with embedded XML metadata; opens in ImageJ/FIJI. |
  | **ZARR_V3** | OME‑NGFF v0.5 zarr per FOV. Best for large timelapses and stitching pipelines. |

- **Size estimate** — to the right of **Save format**, a live `N images · ~size` readout
  updates as you change settings (regions, Nz, Nt, channels/cycles, save format). It
  accounts for the per‑position cycle plan (frames per position, including ragged plans —
  see [acquisition-cycles.md](../acquisition-cycles.md)) and the chosen format: TIFF
  formats are estimated uncompressed, while **ZARR_V3** is shown with a `≈` and reflects
  the selected compression preset plus pyramid overhead (the real size is data‑dependent
  and usually smaller). It reads *Saving disabled* when **Skip Saving** is checked.
- **Stream to network** (ZARR_V3 only) — appears when the format is ZARR_V3. Streams the
  zarr output to a mounted network share as the run proceeds, sha256‑verifies each
  timepoint on the remote, and optionally deletes local copies (**Delete after verify**)
  to reclaim disk space. See [zarr-network-streaming.md](zarr-network-streaming.md).
- **Skip Saving** — acquire without writing files (also bypasses the disk‑space check).
- **Snake scan** — alternate the tile direction each row (boustrophedon) to minimize
  stage travel. When off, every row starts from the same side.
- **Keep illuminators on between captures** — leaves the light source on between frames
  (faster, more light exposure).
- **Show live preview during acquisition** — when unchecked, skips per‑frame display
  updates and only redraws the last frame at the end, reducing per‑capture overhead.
  This is the one control that stays enabled while a run is in progress.

> For ZARR_V3 compression and related tuning, see **Settings → Preferences**. For the
> data layout produced by each format, see
> [multipoint-data-saving.md](multipoint-data-saving.md).

### Step 10 — Run it

- **Snap Images** captures a single FOV at the current position using the checked
  channels (no Z‑stack, no time‑lapse, no autofocus) — handy for a quick test shot.
- **Start Acquisition** begins the full run. The button toggles to **Stop Acquisition**
  while running; click it again to abort. During the run, every other control is
  disabled and the **progress bar**, region counter, and **ETA** appear at the bottom.

On Start the software validates the saving path, checks available disk space (unless
*Skip Saving*) and RAM, and — if *Use Focus Map* is on — fits the focus surface. If any
check fails it shows a dialog and the run does not start.

---

## Flexible acquisition

Use the **Flexible Multipoint** tab when you want to image a hand‑picked set of
positions rather than wells — arbitrary points anywhere on the sample, each imaged as a
small tile grid.

The channel, Z‑stack, time‑lapse, autofocus, and saving controls are **identical** to
the Wellplate tab (Steps 5–10 above). The difference is how you specify *where* to image.

### Building a position list

Drive the stage to a spot of interest (using the stage jog buttons, the plate map, or
Click‑to‑Move), then manage the list with these buttons:

| Button / action | Effect |
|---|---|
| **Add** (or the `;` key, or Ctrl+A) | Add the current stage X/Y/Z as a new position (region `R0`, `R1`, …). Duplicate X/Y positions are rejected. |
| **Remove** | Remove the position currently selected in the **Location List** dropdown. |
| **Next** | Advance the selection to the next position and move the stage there. |
| **Clear** | Remove all positions. |
| **Location List** (dropdown) | Lists every saved position as `<region name> \| x … mm  y … mm  z … µm`; selecting one moves the stage there. |
| **Update Z** | Overwrite the selected position's Z with the current stage Z. |
| **Update Ref** | Re‑capture the laser‑AF reference (focus target) for the selected position. Only shown on rigs with the laser‑focus camera; see [Per‑region laser autofocus references](#per-region-laser-autofocus-references). |

### Naming regions

Auto‑assigned names (`R0`, `R1`, …) can be replaced with anything meaningful — `liver
section`, `tumor_1`, `day3 rep2`. Open **Edit** and type a new value in the **Region
Name** column (or set the `ID` column of an imported CSV).

The name you choose is what appears everywhere downstream:

- the `region` column of `coordinates.csv`;
- the `positions` list in `acquisition.yaml`;
- `region_observation_states.csv` and `region_laser_af_references.csv`;
- the image filename prefix, `<region>_<fov>_<z>.tiff`;
- the zarr folder for the region, `zarr/<region>/fov_<n>.ome.zarr`.

Because a name is a folder name and a dict key, not just a label, it must be a safe,
unique path component. A rename is rejected (with an explanation, leaving the previous
name in place) when it is empty, longer than 48 characters, contains `< > : " / \ | ? *`
or a control character, ends with `.`, is a Windows reserved device name (`CON`, `NUL`,
`COM1`, …), or matches another region's name ignoring case — `sample` and `Sample` would
be the same folder on Windows. Renames are also refused while an acquisition is running,
since the worker took its region names when the run started. Names loaded from a CSV or
YAML that fail these rules are replaced with a fresh `R{n}` and a warning in the log.

An accepted rename carries the region's per‑point channel selection, laser‑AF reference,
and focus‑map points with it, and leaves the scan order unchanged.

### Import / export / edit

- **Import Location List** — load positions from a CSV with columns `x (mm)`,
  `y (mm)`, `z (mm)`, and optional `ID`. This replaces the current list. If the CSV has a
  `laser_af_x_reference` column and/or a `<name>.laser_af.json` sidecar file next to it,
  the per‑region laser‑AF references are restored too (see
  [Per‑region laser autofocus references](#per-region-laser-autofocus-references)).
- **Export Location List** — save the current positions to a CSV (`x (mm)`, `y (mm)`,
  `z (mm)`, `ID`, plus a `laser_af_x_reference` column). When any position has a laser‑AF
  reference, a companion `<name>.laser_af.json` sidecar is written alongside the CSV
  holding the full references (including the cross‑correlation crop image) so the export
  round‑trips exactly. Keep the sidecar next to the CSV to re‑import references.
- **Edit** — open the position list as an editable table (columns x, y, z, **Region
  Name**, and a read‑only **AF Ref**); edit x/y/z to move that position or the name to
  rename it (see [Naming regions](#naming-regions)), click a row to select it. The **AF
  Ref** column shows each region's stored laser‑AF spot position, or `—` when none is
  set.

### Per‑position tile grid

Each position is imaged as a small tile grid. Depending on configuration you set either:

- **Nx** / **Ny** (number of tiles in X and Y) and **FOV Overlap** (%), or
- **dx** / **dy** (step sizes in mm) together with **Nx** / **Ny**.

If you click **Start Acquisition** with an empty list, the current stage position is
added automatically and removed again when the run finishes.

> Dropping an `acquisition.yaml` onto the Flexible tab is not currently supported (you'll
> get a "Not Supported" notice). YAML drag‑and‑drop works on the **Wellplate** tab.

### Per‑region laser autofocus references

Normally laser AF corrects every position back to a single reference (the in‑focus
reflected‑spot position) shared across the whole run. On the **Flexible** tab you can
instead give **each position its own laser‑AF reference** — useful when positions sit on
substrates of different thickness, or otherwise focus to a different reference plane, so a
single global reference would mis‑focus some of them. (A reference is per *region*, not per
tile: every tile in a position uses that position's reference.)

How to use it:

1. Enable **Laser AF** (the Reflection AF button) and make sure it is initialized (use the
   *Focus Camera / Laser AF Setup* tool to find the spot and calibrate).
2. Drive to a position, get it in focus, and click **Add**. The current laser‑AF reference
   is captured and attached to that position — its spot position appears in the **AF Ref**
   column of the **Edit** table.
3. Repeat for each position, focusing each one before adding it.
4. To re‑capture a position's reference later, select it in the **Location List** and click
   **Update Ref** (focus first). **Update Z** changes only the stored Z, not the reference.

During acquisition the worker loads each region's own reference before focusing in that
region, independent of scan order. Positions **without** a captured reference fall back to
the global reference. Acquisition can therefore start when *either* a global reference is
set *or* every position has its own — otherwise the start check reports that a reference is
missing. A `region_laser_af_references.csv` summary is written into the experiment folder
for reproducibility, and references export/import with the location list (see
[Import / export / edit](#import--export--edit)).

This per‑region reference plumbing is shared with the wellplate code path, but only the
Flexible tab captures references today.

---

## Per‑point / per‑well channels

By default every checked channel is imaged at every region. The **Per‑Point Channels**
button lets you override this so different locations get different channels — for example
to skip a slow channel where it isn't needed, reduce photobleaching, or run different
stains per well.

When a custom map is active, the button shows a trailing asterisk (**Per‑Point
Channels \***). Note that changing the global channel checkboxes or the selection of
wells/positions clears the map, so finalize your global channel selection and selection
*first*, then open this dialog.

**Wellplate tab → "Per‑Well Channels" dialog.** Available only in *Select Wells* mode
with a wellplate format. The dialog shows a plate‑shaped grid; each well displays its
active channels as colored dots. Select one or more wells, then click a **channel chip**
in the toolbar to toggle that channel on/off across the selection (chips show **● ON**,
**◐ MIXED**, or **○ OFF**). **All on** / **All off** apply to the selected wells. Drag
chips to reorder channels. Click **OK** to apply or **Cancel** to discard.

**Flexible tab → "Per‑Point Observation States" dialog.** A matrix of registered points
(rows) × channels (columns) with checkboxes. Click‑and‑drag to paint cells on/off, click
a row/column header to toggle that whole row/column, and use **Check All** / **Uncheck
All** for bulk edits. Drag column headers to reorder channels. Click **OK** or **Cancel**.

Channel dot/chip colors are assigned by the dialog from a stable palette (a given channel
keeps its color across openings); they are a UI aid only and are not stored in the
channel configuration.

---

## Monitoring an acquisition

- **Progress row** (bottom of the acquisition panel): region/timepoint counter, progress
  bar, and a live ETA.
- **Multichannel Acquisition** and **Mosaic View** tabs show captured frames as they
  arrive (unless *Show live preview during acquisition* is off).
- **Plate View** tab (when enabled): a real‑time downsampled overview of the whole plate
  for well‑based runs without Z‑stacking. Double‑click any FOV to inspect it in the
  **NDViewer**. See [downsampled-plate-view.md](downsampled-plate-view.md) and
  [ndviewer-tab.md](ndviewer-tab.md).

---

## Tips and troubleshooting

- **"Please choose base saving directory first."** Set the **Saving Path** before
  starting.
- **Acquisition won't start after a disk/RAM warning.** The run is blocked when there
  isn't enough disk space or memory; free space, reduce the run size, or (for disk only)
  enable *Skip Saving* if you don't need the files.
- **"Failed to fit focus surface."** *Use Focus Map* is on but the surface fit failed —
  add/adjust focus points in the **Focus Map** tab, or uncheck *Use Focus Map*.
- **Per‑Point Channels is greyed out / warns you.** It needs at least one checked
  channel and at least one selected well (or position). On the Wellplate tab it only
  works in *Select Wells* mode with a wellplate format.
- **Stitching looks misaligned.** Increase **FOV Overlap**, and recalibrate the plate if
  well centers are off.
- **Wells aren't where they should be.** Recalibrate the sample format (A1 position).
- **Run is slow per frame.** Turn off *Show live preview during acquisition*, enable
  *Keep illuminators on between captures*, and keep *Snake scan* on to minimize travel.

---

## Related documentation

- [Observation state model](observation-state-migration.md) — what an observation state
  contains and how it replaced the older channel model.
- [Wellplate configuration files](wellplate-configs.md) — sample‑format definitions.
- [Multipoint data saving](multipoint-data-saving.md) — output layout and metadata.
- [Zarr network streaming](zarr-network-streaming.md) — streaming ZARR_V3 to a share.
- [Downsampled plate view](downsampled-plate-view.md) and [NDViewer tab](ndviewer-tab.md)
  — live monitoring during acquisition.
- [User profiles](user-profiles.md) — where observation‑state presets are stored.
