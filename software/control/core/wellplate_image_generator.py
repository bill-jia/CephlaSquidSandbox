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
# Search order for locating the source YAML (mirrors control._def.load_formats).
_YAML_SEARCH_PATHS = (
    os.path.join("cache", _def.SAMPLE_FORMATS_YAML_PATH),
    os.path.join("objective_and_sample_formats", _def.SAMPLE_FORMATS_YAML_PATH),
)


def _mm_to_px(mm: float) -> int:
    return round(mm / _def.PLATE_IMAGE_MM_PER_PX)


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
    """Draw the outer plate body (rounded rectangle with optional A1 chamfer)."""
    draw.rounded_rectangle(
        [0, 0, width_px - 1, height_px - 1],
        radius=max(0, corner_radius_px),
        outline="black",
        width=4,
        fill="lightgrey",
    )
    if chamfer_px > 0:
        # SBS A1 chamfer: cut the top-left corner with a triangle filled in the
        # background color to show orientation.
        c = min(chamfer_px, width_px // 4, height_px // 4)
        draw.polygon(
            [(0, 0), (c, 0), (0, c)],
            fill="white",
            outline="black",
        )


def _draw_well(draw: ImageDraw.ImageDraw, cx_px: int, cy_px: int, settings: dict):
    shape = settings["well_shape"]
    if shape == "circle":
        r = _mm_to_px(settings["well_diameter_mm"]) / 2.0
        draw.ellipse(
            [cx_px - r, cy_px - r, cx_px + r, cy_px + r],
            outline="black",
            width=3,
            fill="white",
        )
    elif shape == "rectangle":
        w = _mm_to_px(settings["well_width_mm"])
        h = _mm_to_px(settings["well_height_mm"])
        corner_r = _mm_to_px(settings.get("well_corner_radius_mm", 0.0))
        x1 = cx_px - w // 2
        y1 = cy_px - h // 2
        x2 = x1 + w
        y2 = y1 + h
        if corner_r > 0:
            draw.rounded_rectangle(
                [x1, y1, x2, y2],
                radius=corner_r,
                outline="black",
                width=3,
                fill="white",
            )
        else:
            draw.rectangle(
                [x1, y1, x2, y2],
                outline="black",
                width=3,
                fill="white",
            )
    else:
        raise ValueError(f"Unknown well shape: {shape}")


def _generate(format_id: str, settings: dict, out_path: str) -> None:
    plate_w_mm, plate_h_mm = settings["plate_dimensions_mm"]
    width_px = _mm_to_px(plate_w_mm)
    height_px = _mm_to_px(plate_h_mm)
    if width_px <= 0 or height_px <= 0:
        # Degenerate format (e.g. placeholder "0"); write a tiny blank png.
        width_px = max(width_px, 1)
        height_px = max(height_px, 1)

    image = Image.new("RGB", (width_px, height_px), color="white")
    draw = ImageDraw.Draw(image)

    corner_r_px = _mm_to_px(settings.get("plate_corner_radius_mm", 0.0))
    chamfer_px = _mm_to_px(3.0) if settings.get("a1_chamfer") else 0
    _draw_plate_outline(draw, width_px, height_px, corner_r_px, chamfer_px)

    a1_off = settings["a1_offset_mm"]
    row_pitch_mm = settings["row_spacing_mm"]
    col_pitch_mm = settings["col_spacing_mm"]
    rows = settings["rows"]
    cols = settings["cols"]

    for row in range(rows):
        for col in range(cols):
            cx = _mm_to_px(a1_off[0] + col * col_pitch_mm)
            cy = _mm_to_px(a1_off[1] + row * row_pitch_mm)
            _draw_well(draw, cx, cy, settings)

    # Row/column labels. Skip for degenerate 1x1 "plates" (e.g. glass slide).
    if rows > 1 or cols > 1:
        try:
            font_size = max(10, _mm_to_px(min(row_pitch_mm, col_pitch_mm) * 0.35))
            font = ImageFont.load_default().font_variant(size=font_size)
        except Exception:
            font = ImageFont.load_default()

        for col in range(cols):
            label = str(col + 1)
            cx = _mm_to_px(a1_off[0] + col * col_pitch_mm)
            cy = _mm_to_px(a1_off[1] / 2.0)
            bbox = font.getbbox(label)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text((cx - tw / 2, cy - th / 2), label, fill="black", font=font)

        for row in range(rows):
            label = chr(65 + row) if row < 26 else chr(65 + row // 26 - 1) + chr(65 + row % 26)
            cx = _mm_to_px(a1_off[0] / 2.0)
            cy = _mm_to_px(a1_off[1] + row * row_pitch_mm)
            bbox = font.getbbox(label)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text((cx - tw / 2, cy - th / 2), label, fill="black", font=font)

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
