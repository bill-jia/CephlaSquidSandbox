"""Auto-generate background map PNGs for the wellplate viewer.

The viewer (NavigationViewer) paints a PNG of the sample holder underneath
the stage-position overlay. Historically those PNGs were hand-rendered and
committed under software/images/. This module regenerates them on demand
from the declarative YAML schema in sample_formats.yaml, supporting both
circular and rectangular wells plus arbitrary plate dimensions, A1 offsets,
and row/column pitches.

Output PNGs are cached under software/cache/plate_images/ so they are not
committed. They are regenerated whenever the source YAML is newer than the
cached PNG (mtime check).
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

import control._def as _def

CACHE_DIR = os.path.join("cache", "plate_images")
# Render at N× the final resolution, then downsample with a Lanczos filter so
# well edges and labels aren't pixelated at the ~11.8 px/mm display scale.
# Pillow's ImageDraw does not anti-alias ellipses/rectangles by itself, so
# super-sampling is the cheapest way to get clean curves.
_SUPERSAMPLE = 3
# Search order for locating the source YAML (mirrors control._def.load_formats).
_YAML_SEARCH_PATHS = (
    os.path.join("cache", _def.SAMPLE_FORMATS_YAML_PATH),
    os.path.join("objective_and_sample_formats", _def.SAMPLE_FORMATS_YAML_PATH),
)

# Ordered list of TTF fonts to try for labels. Pillow's default bitmap font
# can't be scaled cleanly, so we prefer any vector font we can find.
_FONT_CANDIDATES = (
    "arial.ttf", "Arial.ttf", "Helvetica.ttf", "DejaVuSans.ttf",
    "LiberationSans-Regular.ttf",
)


def _load_font(size_px: int) -> ImageFont.ImageFont:
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size=size_px)
        except (OSError, IOError):
            continue
    try:
        return ImageFont.load_default().font_variant(size=size_px)
    except Exception:
        return ImageFont.load_default()


def _mm_to_px(mm: float) -> int:
    return round(mm / _def.PLATE_IMAGE_MM_PER_PX)


def _mm_to_px_hires(mm: float) -> int:
    return round(mm * _SUPERSAMPLE / _def.PLATE_IMAGE_MM_PER_PX)


def _sanitize_filename(format_id: str) -> str:
    return format_id.replace(" ", "_").replace("/", "_")


def _resolve_yaml_path() -> Optional[str]:
    for path in _YAML_SEARCH_PATHS:
        if os.path.exists(path):
            return path
    return None


def _is_cache_fresh(cached_png: str, yaml_path: Optional[str]) -> bool:
    if not os.path.exists(cached_png):
        return False
    if yaml_path is None:
        return True
    try:
        return os.path.getmtime(cached_png) >= os.path.getmtime(yaml_path)
    except OSError:
        return False


def _draw_plate_outline(draw: ImageDraw.ImageDraw, width_px: int, height_px: int,
                         corner_radius_px: int, chamfer_px: int):
    """Draw the outer plate body (rounded rectangle with optional A1 chamfer).

    All inputs are in the super-sampled pixel space — line widths are scaled
    by _SUPERSAMPLE so they shrink to reasonable thickness after downsampling.
    """
    line_w = max(1, 2 * _SUPERSAMPLE)
    draw.rounded_rectangle(
        [0, 0, width_px - 1, height_px - 1],
        radius=max(0, corner_radius_px),
        outline="black",
        width=line_w,
        fill="lightgrey",
    )
    if chamfer_px > 0:
        c = min(chamfer_px, width_px // 4, height_px // 4)
        draw.polygon(
            [(0, 0), (c, 0), (0, c)],
            fill="white",
            outline="black",
        )


def _draw_well(draw: ImageDraw.ImageDraw, cx_px: int, cy_px: int, settings: dict):
    """Render one well in the super-sampled pixel space."""
    line_w = max(1, 2 * _SUPERSAMPLE)
    shape = settings["well_shape"]
    if shape == "circle":
        r = _mm_to_px_hires(settings["well_diameter_mm"]) / 2.0
        draw.ellipse(
            [cx_px - r, cy_px - r, cx_px + r, cy_px + r],
            outline="black",
            width=line_w,
            fill="white",
        )
    elif shape == "rectangle":
        w = _mm_to_px_hires(settings["well_width_mm"])
        h = _mm_to_px_hires(settings["well_height_mm"])
        corner_r = _mm_to_px_hires(settings.get("well_corner_radius_mm", 0.0))
        x1 = cx_px - w // 2
        y1 = cy_px - h // 2
        x2 = x1 + w
        y2 = y1 + h
        if corner_r > 0:
            draw.rounded_rectangle(
                [x1, y1, x2, y2],
                radius=corner_r,
                outline="black",
                width=line_w,
                fill="white",
            )
        else:
            draw.rectangle(
                [x1, y1, x2, y2],
                outline="black",
                width=line_w,
                fill="white",
            )
    else:
        raise ValueError(f"Unknown well shape: {shape}")


def _generate(format_id: str, settings: dict, out_path: str) -> None:
    plate_w_mm, plate_h_mm = settings["plate_dimensions_mm"]
    final_w = max(1, _mm_to_px(plate_w_mm))
    final_h = max(1, _mm_to_px(plate_h_mm))
    hires_w = final_w * _SUPERSAMPLE
    hires_h = final_h * _SUPERSAMPLE

    image = Image.new("RGB", (hires_w, hires_h), color="white")
    draw = ImageDraw.Draw(image)

    corner_r_px = _mm_to_px_hires(settings.get("plate_corner_radius_mm", 0.0))
    chamfer_px = _mm_to_px_hires(3.0) if settings.get("a1_chamfer") else 0
    _draw_plate_outline(draw, hires_w, hires_h, corner_r_px, chamfer_px)

    a1_off = settings["a1_offset_mm"]
    row_pitch_mm = settings["row_spacing_mm"]
    col_pitch_mm = settings["col_spacing_mm"]
    rows = settings["rows"]
    cols = settings["cols"]

    for row in range(rows):
        for col in range(cols):
            cx = _mm_to_px_hires(a1_off[0] + col * col_pitch_mm)
            cy = _mm_to_px_hires(a1_off[1] + row * row_pitch_mm)
            _draw_well(draw, cx, cy, settings)

    # Row/column labels. Skip for degenerate 1x1 "plates" (e.g. glass slide).
    if rows > 1 or cols > 1:
        # Well half-extents determine how much margin we have between plate
        # edge and the first well. Labels must fit within that margin,
        # otherwise they overlap the wells (bad for 6-well where each well
        # is ~35 mm across).
        if settings["well_shape"] == "circle":
            well_hw_mm = settings["well_diameter_mm"] / 2.0
            well_hh_mm = well_hw_mm
        else:
            well_hw_mm = settings["well_width_mm"] / 2.0
            well_hh_mm = settings["well_height_mm"] / 2.0
        top_margin_mm = max(0.0, a1_off[1] - well_hh_mm)
        left_margin_mm = max(0.0, a1_off[0] - well_hw_mm)

        # Cap label height to 70% of the smaller margin (dense plates have
        # tiny margins); also cap by pitch so labels don't bleed between cols.
        max_h_mm = min(top_margin_mm, left_margin_mm) * 0.7
        max_pitch_mm = min(row_pitch_mm, col_pitch_mm) * 0.6
        label_mm = max(0.8, min(max_h_mm if max_h_mm > 0 else max_pitch_mm, max_pitch_mm))
        font_size = max(10, _mm_to_px_hires(label_mm))
        font = _load_font(font_size)

        col_label_cy_px = _mm_to_px_hires(top_margin_mm / 2.0)
        row_label_cx_px = _mm_to_px_hires(left_margin_mm / 2.0)

        for col in range(cols):
            label = str(col + 1)
            cx = _mm_to_px_hires(a1_off[0] + col * col_pitch_mm)
            bbox = font.getbbox(label)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text((cx - tw / 2, col_label_cy_px - th / 2 - bbox[1]),
                      label, fill="black", font=font)

        for row in range(rows):
            label = chr(65 + row) if row < 26 else chr(65 + row // 26 - 1) + chr(65 + row % 26)
            cy = _mm_to_px_hires(a1_off[1] + row * row_pitch_mm)
            bbox = font.getbbox(label)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text((row_label_cx_px - tw / 2, cy - th / 2 - bbox[1]),
                      label, fill="black", font=font)

    # Downsample with Lanczos to get anti-aliased well edges and crisp text.
    image = image.resize((final_w, final_h), Image.LANCZOS)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    image.save(out_path)


def ensure_plate_image(format_id: str) -> str:
    """Return the path to the plate-map PNG for `format_id`, regenerating if stale.

    Raises KeyError if `format_id` is not a known format.
    """
    if format_id not in _def.WELLPLATE_FORMAT_SETTINGS:
        raise KeyError(f"Unknown wellplate format: {format_id}")

    settings = _def.WELLPLATE_FORMAT_SETTINGS[format_id]
    out_path = os.path.join(CACHE_DIR, f"{_sanitize_filename(format_id)}.png")
    yaml_path = _resolve_yaml_path()

    if _is_cache_fresh(out_path, yaml_path):
        return out_path

    _generate(format_id, settings, out_path)
    return out_path


def get_a1_pixel(format_id: str) -> Tuple[int, int]:
    """Pixel coordinate of the A1 well center in the generated plate image."""
    settings = _def.WELLPLATE_FORMAT_SETTINGS[format_id]
    a1_off = settings["a1_offset_mm"]
    return _mm_to_px(a1_off[0]), _mm_to_px(a1_off[1])


def get_mm_per_pixel() -> float:
    """Millimeters per pixel of the generated plate image."""
    return _def.PLATE_IMAGE_MM_PER_PX


def regenerate_all() -> None:
    """Force-regenerate every known format's PNG. Useful after editing the YAML."""
    for format_id, settings in _def.WELLPLATE_FORMAT_SETTINGS.items():
        out_path = os.path.join(CACHE_DIR, f"{_sanitize_filename(format_id)}.png")
        _generate(format_id, settings, out_path)
