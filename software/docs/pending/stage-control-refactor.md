# Stage Control Refactor Plan

## Motivation

Stage coordinate conventions across the codebase are inconsistent with what
makes physical/visual sense. The most visible symptom today is in the napari
mosaic view: tile positions reported by the stage are flipped on both X and Y
relative to the natural viewer frame (where increasing X goes right and
increasing Y goes up/down consistently with the physical sample).

We currently work around this in the mosaic display by hard-negating `x_mm`
and `y_mm` at the viewer boundary. The negations live in:

- File: `software/gui/widgets/napari_views.py`
- Class: `NapariMosaicDisplayWidget`
- Methods:
  - `updateMosaic` — negates tile placement coords before drawing
  - `convert_shape_to_mm` — negates when converting ROI shapes from viewer to stage frame
  - `convert_mm_to_viewer_shapes` — negates when converting ROI shapes from stage to viewer frame
  - `onDoubleClick` — negates click coordinates before emitting move-to-stage signal

All four sites are tagged `TEMPORARY:` in the source and should be removed
once this refactor lands.

## Known issues to address

- Stage X/Y reported by the controller is flipped relative to the natural
  viewer/sample frame. The mosaic widget compensates with hard negations.
- `STAGE_MOVEMENT_SIGN_X/Y/Z/THETA/W` in `software/control/_def.py` already
  exists as a per-axis sign flip mechanism, but the interaction between these
  signs, the controller's native frame, and the viewer frame is not clearly
  defined or documented.
- A previous `MOSAIC_FLIP_Y` config flag (now removed) was an earlier ad-hoc
  attempt at the same workaround. Removing it in favor of a hard negation is
  itself a temporary measure.
- **Stale star-imports in `microcontroller.py` leak pre-yaml defaults to the
  MCU.** `software/control/microcontroller.py` does `from control._def import
  *` at module load. The names bound this way (`MICROSTEPPING_DEFAULT_X`,
  `SCREW_PITCH_X_MM`, `MAX_VELOCITY_X_mm`, etc.) are frozen at _def.py's
  import-time defaults. `config_bridge.apply_machine_config` updates the
  attributes on `control._def` *after* this import, so the star-imported
  local names never see the yaml values. `Microcontroller.configure_actuators`
  used the stale names, which caused the stepper driver's microstepping
  register to be set from the default (e.g. 8) instead of the yaml value
  (e.g. 16) — producing a 2× scaling error between commanded and physical
  motion on X/Y. Mitigated in-place by switching X and Y references in
  `configure_actuators` to `_def.XYZ` attribute access; Z still uses the
  star-imported names pending trustworthy yaml scalings for that axis.
  **The real fix** is to stop threading motor-driver config through
  `_def.py` globals at all: pass the yaml-sourced `StageConfig` /
  `AxisConfig` directly into `Microcontroller.configure_actuators` (or move
  that configuration into `CephlaStage.__init__`, which already receives
  `StageConfig`). While doing that, also fix the mismatched attribute name
  for `home_switch_polarity` — `config_bridge.py:240-243` writes to
  `HOME_SWITCH_POLARITY_{X,Y,Z}` but every reader uses
  `{X,Y,Z}_HOME_SWITCH_POLARITY`, so yaml values for that field are silently
  dropped today.

- **Image-origin conventions per camera aren't modelled.** The Tucsen ARIES
  sensors deliver frames with **bottom-left pixel origin** (array row 0 = bottom
  physical row), whereas most cameras and napari both assume top-left origin.
  Today the driver only applies a horizontal `ReverseX` flip (see
  `software/control/camera_tucsen.py` around L596-608 in `_configure_camera`);
  there is no vertical flip. That means placing a raw Tucsen frame directly
  into the napari mosaic would show tile content upside-down. The pragmatic
  workaround is to set `flip: Vertical` under `devices.main_camera.config` in
  the machine yaml, or to add a `ReverseY` SDK call alongside the existing
  `ReverseX`. The real fix is a first-class `image_origin` camera-config
  concept, normalised to top-left in `_process_raw_frame` for every driver.
  With that in place, tile-position math (which this refactor centralises in
  the Stage abstraction anyway) only has to reason about one convention.
- **Tile grid / FOV math should read per-axis dimensions** even when the
  crop is non-square. `AbstractCamera.get_fov_size_mm()` now returns
  `(width_mm, height_mm)` and every `scan_coordinates.py` grid builder uses
  separate `step_x_mm` / `step_y_mm`. `get_crop_size()` also falls back to the
  camera's current resolution when no `crop:` block is set in yaml, so the
  downstream FOV math never silently breaks with `None`. When the stage
  coordinate centralisation lands, the per-axis FOV numbers should be
  carried through the `Stage` / canonical-frame API rather than reaching into
  the camera from scan-coordinate code.

## Goal

Define a single canonical stage coordinate frame (sample/world frame) such
that:

- Increasing X moves right in the mosaic / plate view.
- Increasing Y moves in a consistent, documented direction (up or down — pick
  one and stick with it across mosaic, plate view, click-to-move, ROI shapes,
  and saved coordinates).
- Increasing Z moves the objective in a documented direction relative to the
  sample.

All conversions between this canonical frame and the controller's native
frame should happen in one place (likely the `Stage` abstraction), not at
display sites.

## Proposed steps (rough)

1. Audit every site that reads/writes stage coordinates: stage controller,
   multipoint acquisition, mosaic view, plate view, ROI shape conversion,
   click-to-move, saved coordinate CSVs, MCP control surface.
2. Pick the canonical frame and document it (this file → promote to a real
   architecture doc).
3. Centralize the controller↔canonical conversion inside the `Stage` class.
4. Remove all per-site sign flips, including the temporary negations in
   `NapariMosaicDisplayWidget`.
5. Verify with: live navigation (joystick + click-to-move), a multipoint
   acquisition that crosses well boundaries, and an ROI drawn on the mosaic.

## Progress — canonical-frame plumbing landed (2026-04-19)

Pure plumbing only; zero behavior change with default yaml.

- `AxisConfig` (`software/squid/config.py`) now carries two new per-axis
  fields: `CANONICAL_SIGN` (default `+1`) and `CANONICAL_ORIGIN_RAW_MM`
  (default `0.0`). These define the affine mapping
  `canonical = CANONICAL_SIGN * (raw - CANONICAL_ORIGIN_RAW_MM)`.
  Helper methods: `raw_to_canonical`, `canonical_to_raw`,
  `raw_to_canonical_delta`, `canonical_to_raw_delta`.
- `CephlaStage` (`software/squid/stage/cephla.py`) public methods
  (`get_pos`, `move_{x,y,z}_to`, `move_{x,y,z}`) route through these helpers.
  `set_limits` stays raw-frame (it talks directly to the MCU limit switches;
  documented in-line).
- `_build_stage_config_from_device` in `squid/config.py` reads a new yaml
  block:
  ```yaml
  devices:
    stage:
      config:
        canonical:
          x: { sign: -1, origin_raw_mm: 56.0 }
          y: { sign: -1, origin_raw_mm: 56.0 }
  ```
  Absent block → defaults → no-op.

**Revertibility.** Nothing activates automatically. The demo rig behaves
exactly as before until its yaml adds a `canonical:` block. The production
rig (stage rotated 180°, home already at physical top-left) stays at defaults
and also behaves identically to today. To undo: delete the yaml block, or
`git revert` the plumbing commit.

## Still pending (blocked on dedicated test time)

These are the "real" consumer-side changes that must happen to actually
realise the canonical flip on the demo rig. Each assumes `canonical` yaml is
set so `Stage.get_pos()` returns true top-left-origin coordinates.

- Remove the four `TEMPORARY:` negations in
  `software/gui/widgets/napari_views.py` (`NapariMosaicDisplayWidget`
  methods `updateMosaic`, `convert_shape_to_mm`,
  `convert_mm_to_viewer_shapes`, `onDoubleClick`).
- Flip the Y sign in the joystick + click-to-move handlers
  (`software/gui/widgets/tracking_and_controls.py` `moveStage` and
  `viewerClicked`) so pushing up / clicking above still pans up in the new
  Y-grows-down canonical frame.
- Update `software/control/sample_formats.csv` `a1_x_mm` / `a1_y_mm` to
  canonical frame (`56 - raw` for both axes on the demo rig).
- Update `STARTUP_DEFAULT_STAGE_X_MM` / `_Y_MM` in `software/control/_def.py`
  to canonical values.
- Decide whether `SOFTWARE_POS_LIMIT` should be canonical or stay raw;
  today consumers treat it as ambient so the choice ripples.
- Delete `cache/last_coords.txt` once (format changes from raw to canonical).
- Flag migration needs for any user-saved `coordinates.csv` / focus-map
  exports / acquisition yamls — they are in raw frame and will be
  misinterpreted as canonical after the flip.

## Tucsen ReverseX/ReverseY now yaml-configurable (2026-04-19)

The hardcoded `ReverseX=True` in
`software/control/camera_tucsen.py:_configure_camera` became yaml-driven:

```yaml
devices:
  main_camera:
    config:
      reverse_x: true    # default: true (preserves old behavior)
      reverse_y: true    # default: unset — driver leaves the sensor at its
                         # default vertical orientation
```

Plumbed through `CameraConfig.reverse_x` / `reverse_y` in `squid/config.py`
and `_build_camera_config_from_device`. Both GenICam (ARIES) and legacy
TUCAM code paths honor the fields. This gives the production unit a clean
lever to set the correct sensor orientation without touching driver code,
and pairs naturally with the stage canonical-frame plumbing above when it
eventually activates.

## Out of scope (for now)

- Changing the controller firmware's native axis directions.
- Re-defining `STAGE_MOVEMENT_SIGN_*` semantics — those are part of the
  refactor target, not a separate change.
