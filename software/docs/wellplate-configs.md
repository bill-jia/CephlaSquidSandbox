# Wellplate / Sample Holder Configuration

This doc describes the declarative YAML schema that defines wellplate and
sample-holder geometry, and the auto-generation tool that builds the
graphical maps shown in the plate viewer.

## Files

| Path | Purpose |
|---|---|
| `software/objective_and_sample_formats/sample_formats.yaml` | Plate-intrinsic geometry for every supported format. Machine-independent. |
| `software/cache/sample_formats.yaml` | Runtime override (takes precedence over the default file). Written by the calibration dialog when a user adds or recalibrates a format. |
| `software/cache/plate_images/{id}.png` | Auto-generated background maps for the plate viewer. Regenerated when the source YAML's mtime is newer than the cached PNG. Not committed. |
| `software/machine_configs/machine_config.yaml` → `wellplate_calibrations:` | Per-machine stage calibration (where this stage finds A1). Separate concern — plate geometry is machine-independent. |

## Schema

Each entry under `formats:` describes one plate/holder. All distances are
millimeters; the coordinate origin is the **physical top-left corner** of
the sample holder when viewed from above with column 1 on the left and
row A at the top.

```yaml
formats:
  - id: "96 well plate"                # key used everywhere in code
    display_name: "96 Well Plate"      # human label (defaults to id)
    plate_dimensions_mm: [127.76, 85.48]   # outer bounding box [W, H]
    plate_corner_radius_mm: 3.18           # visual only
    a1_chamfer: true                       # SBS A1-corner chamfer (visual cue)
    a1_offset_mm: [11.31, 10.75]           # A1 CENTER from plate top-left corner
    grid:
      rows: 8
      cols: 12
      row_spacing_mm: 9.0                  # center-to-center, rows
      col_spacing_mm: 9.0                  # center-to-center, cols
                                           # (independent — supports non-square pitch)
    well:
      shape: circle                        # "circle" | "rectangle"
      diameter_mm: 6.21                    # required if shape == circle
      # width_mm, height_mm, corner_radius_mm   # required if shape == rectangle
    number_of_skip: 0                      # unusable rows/cols on every edge
```

### Rectangle wells

```yaml
well:
  shape: rectangle
  width_mm: 14.0
  height_mm: 8.0
  corner_radius_mm: 1.5      # optional; 0 → pure rectangle
```

### Notes on the coordinate split

`a1_offset_mm` is plate-intrinsic: it's the same value regardless of which
machine the plate is on, because it's a property of the plate's physical
design.

Where the **stage** finds A1 (which depends on how the plate is clamped,
stage homing, etc.) is a separate concern recorded under
`wellplate_calibrations:` in `machine_config.yaml`. Changing machines
should not require editing this YAML.

## Adding a new format

Two paths, both write to `software/cache/sample_formats.yaml`:

1. **GUI.** Sample Format → `calibrate format…` opens a dialog that
   captures rows/cols, plate size, spacing, and now a **well shape**
   selector (Circle / Rectangle). Calibrate A1 by either 3 edge points
   or a center point; the dialog writes the new entry and regenerates
   its PNG automatically.
2. **Hand-edit** `sample_formats.yaml` (for the shipped defaults) or
   `cache/sample_formats.yaml` (for per-machine additions). The plate
   image is regenerated the next time the viewer loads that format.

## The graphical map generator

`software/control/core/wellplate_image_generator.py`:

- `ensure_plate_image(format_id) -> path`: returns a cached PNG path,
  regenerating if the source YAML is newer than the cache.
- `get_a1_pixel(format_id) -> (px, py)`: pixel coordinate of A1 in the
  generated image (used by the viewer for stage ↔ pixel alignment).
- `regenerate_all()`: force-rebuild every known format's PNG.

All generated PNGs use the fixed scale `PLATE_IMAGE_MM_PER_PX = 0.084665`
mm/px (~11.81 px/mm), matching `NavigationViewer.mm_per_pixel` so no
runtime calibration of the transform is needed.

## Where these values flow in code

- `software/control/_def.py:read_sample_formats_yaml` — loader; flattens
  the YAML into the dict shape the rest of the codebase consumes, and
  derives `a1_x_pixel`/`a1_y_pixel` from `a1_offset_mm`.
- `software/control/core/wellplate_image_generator.py` — renders the PNG.
- `software/control/core/core.py:NavigationViewer._resolve_background_image`
  — asks the generator for the plate map when setting a sample.
- `software/gui/widgets/hardware_panels.py:WellSelectionWidget` and
  `software/gui/widgets/tracking_and_controls.py:Well1536SelectionWidget`
  — draw circles or rectangles in each selectable cell according to
  `well_shape`.
- `software/control/core/geometry_utils.py:is_in_rectangle` — new
  primitive alongside `is_in_circle`, for future scan-path code that
  wants to clip tiles to a rectangular-well boundary.
