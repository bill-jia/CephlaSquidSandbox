"""
Source-coded Fourier Ptychography (FPM) darkfield LED selection.

This module is **pure** (no hardware, no Qt, no I/O beyond reading/writing a CSV)
so it can be unit-tested directly. It answers one question:

    "Given the LED array's per-LED NA-space positions and the objective NA,
     which darkfield LEDs tile the darkfield region at >= the required Fourier
     overlap, and how should they be grouped into multiplexed patterns?"

Background — source-coded FPM (Tian et al., Optica 2, 904 (2015)):
  * Brightfield is captured with four DPC half-circle images (handled by the
    existing ``dpc.{l,r,t,b}`` LED-matrix modes — not this module).
  * The darkfield Fourier region is filled by turning on **multiple** darkfield
    LEDs simultaneously (angle-multiplexing), one camera frame per group. BF and
    DF LEDs are never mixed in the same frame (Poisson-noise argument in the
    paper).

In Fourier space each LED contributes a circular "pupil" of radius equal to the
objective NA, centred at that LED's illumination NA ``(na_x, na_y)``. FPM
reconstruction needs neighbouring pupils to overlap by >= ~60%. The dome array
(793 LEDs) samples Fourier space far more densely than that minimum, so we
*thin* the darkfield candidates down to the minimal set whose pupils still tile
the darkfield annulus at the required overlap, then group that set into
multiplexed patterns.

The per-LED NA table is obtained once from the firmware via ``pledposna`` (see
``tools/dump_sci_dome_geometry.py``) and cached as a CSV — it reflects the true
hemispherical dome geometry at the configured working distance, which a flat
``NA = r / sqrt(r^2 + z^2)`` formula would get wrong.
"""

from __future__ import annotations

import csv
import logging
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Default on-disk location of the cached per-LED NA table (written by the dump
# tool). Kept next to the other instrument data formats.
DEFAULT_NA_TABLE_PATH: Path = (
    Path(__file__).resolve().parent.parent
    / "objective_and_sample_formats"
    / "led_arrays"
    / "sci_dome_na_positions.csv"
)

# An NA-space position table: LED index -> (na_x, na_y).
NaTable = Dict[int, Tuple[float, float]]


# ─────────────────────────────────────────────────────────────────────────────
# NA table persistence
# ─────────────────────────────────────────────────────────────────────────────


def save_na_table(rows: Sequence[Tuple[int, float, float]], path: Optional[Path] = None) -> Path:
    """Write an ``(index, na_x, na_y)`` table to CSV, creating parent dirs.

    Returns the path written. ``rows`` is any iterable of 3-tuples.
    """
    path = Path(path) if path is not None else DEFAULT_NA_TABLE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["index", "na_x", "na_y"])
        for idx, nx, ny in rows:
            w.writerow([int(idx), f"{float(nx):.6f}", f"{float(ny):.6f}"])
    return path


def load_na_table(path: Optional[Path] = None) -> NaTable:
    """Load the cached per-LED NA table (``index -> (na_x, na_y)``).

    Raises ``FileNotFoundError`` with actionable guidance when the table has not
    been generated yet — FPM needs the real dome geometry, so there is no silent
    fallback.
    """
    path = Path(path) if path is not None else DEFAULT_NA_TABLE_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"LED NA table not found at {path}. Generate it once on the rig with "
            f"`python -m tools.dump_sci_dome_geometry` (sends the firmware "
            f"`pledposna` command and caches the result)."
        )
    table: NaTable = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            table[int(row["index"])] = (float(row["na_x"]), float(row["na_y"]))
    if not table:
        raise ValueError(f"LED NA table at {path} is empty.")
    return table


# ─────────────────────────────────────────────────────────────────────────────
# Fourier-overlap geometry
# ─────────────────────────────────────────────────────────────────────────────


def two_circle_overlap_fraction(d: float, radius: float) -> float:
    """Overlap of two equal circles (radius ``radius``, centre distance ``d``).

    Returns the lens-shaped intersection area divided by a single circle's area
    (1.0 when coincident, 0.0 when they no longer touch). This is the standard
    FPM "overlap" measure between two adjacent sub-apertures.
    """
    if radius <= 0:
        return 0.0
    if d <= 0:
        return 1.0
    if d >= 2.0 * radius:
        return 0.0
    r = float(radius)
    half = d / (2.0 * r)
    half = max(-1.0, min(1.0, half))
    inter = 2.0 * r * r * math.acos(half) - (d / 2.0) * math.sqrt(max(0.0, 4.0 * r * r - d * d))
    return inter / (math.pi * r * r)


def pitch_for_overlap(radius: float, min_overlap: float) -> float:
    """Largest centre-to-centre distance with overlap >= ``min_overlap``.

    Two pupils spaced by this distance overlap exactly ``min_overlap``; anything
    closer overlaps more. Solved by bisection (overlap is monotonically
    decreasing in distance). ``min_overlap`` is clamped to (0, 1).
    """
    min_overlap = max(1e-6, min(0.999999, float(min_overlap)))
    lo, hi = 0.0, 2.0 * float(radius)
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if two_circle_overlap_fraction(mid, radius) >= min_overlap:
            lo = mid
        else:
            hi = mid
    return lo


# ─────────────────────────────────────────────────────────────────────────────
# Darkfield LED selection
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class SelectionReport:
    """Diagnostics for a darkfield selection (for logging / verification)."""

    n_candidates: int
    n_selected: int
    pitch_na: float                 # target neighbour spacing for the overlap floor
    neighbor_spacing_na: float      # representative (90th-pctile) selected NN spacing
    min_achieved_overlap: float     # pupil overlap implied by neighbor_spacing_na


def _na(p: Tuple[float, float]) -> float:
    return math.hypot(p[0], p[1])


def darkfield_candidates(
    table: NaTable,
    inner_na: float,
    outer_na: float,
) -> List[int]:
    """LED indices whose NA lies in the darkfield annulus ``[inner_na, outer_na]``.

    Returned sorted by NA then index for deterministic downstream selection.
    """
    cands = [
        idx
        for idx, p in table.items()
        if inner_na <= _na(p) <= outer_na
    ]
    cands.sort(key=lambda i: (_na(table[i]), i))
    return cands


def _hex_lattice_points(inner_na: float, outer_na: float, step: float) -> List[Tuple[float, float]]:
    """Hexagonal lattice points covering the annulus ``[inner_na, outer_na]``.

    A hex lattice is the densest covering of the plane and gives uniform
    nearest-neighbour spacing ``step`` — the right model for tiling Fourier space.
    """
    pts: List[Tuple[float, float]] = []
    if step <= 0:
        return pts
    dy = step * math.sqrt(3.0) / 2.0
    row = 0
    y = -outer_na
    while y <= outer_na + 1e-9:
        x = -outer_na + (step / 2.0 if (row % 2) else 0.0)
        while x <= outer_na + 1e-9:
            r = math.hypot(x, y)
            if inner_na - 1e-9 <= r <= outer_na + 1e-9:
                pts.append((x, y))
            x += step
        y += dy
        row += 1
    return pts


def _snap_lattice_to_leds(pts, cands, inner_na, outer_na, step) -> List[int]:
    """Snap each hex-lattice point to its nearest candidate LED (deduped, sorted)."""
    selected: List[int] = []
    seen = set()
    for lx, ly in _hex_lattice_points(inner_na, outer_na, step):
        best_i, best_d2 = None, None
        for i in cands:
            x, y = pts[i]
            d2 = (x - lx) ** 2 + (y - ly) ** 2
            if best_d2 is None or d2 < best_d2:
                best_d2, best_i = d2, i
        if best_i is not None and best_i not in seen:
            seen.add(best_i)
            selected.append(best_i)
    selected.sort(key=lambda i: (_na(pts[i]), i))
    return selected


def _selected_nn_distances(selected, pts) -> List[float]:
    """Distance from each selected LED to its nearest *other* selected LED."""
    out: List[float] = []
    for a in selected:
        xa, ya = pts[a]
        nn = None
        for b in selected:
            if b == a:
                continue
            d = math.hypot(xa - pts[b][0], ya - pts[b][1])
            if nn is None or d < nn:
                nn = d
        if nn is not None:
            out.append(nn)
    return out


def _percentile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[k]


def select_darkfield_leds(
    table: NaTable,
    *,
    inner_na: float,
    outer_na: float,
    pupil_radius_na: float,
    min_overlap: float = 0.6,
) -> Tuple[List[int], SelectionReport]:
    """Select the minimal darkfield LED set tiling ``[inner_na, outer_na]``.

    In Fourier space each LED is a pupil of radius ``pupil_radius_na`` (= objective
    NA) centred at the LED's NA position. FPM needs *adjacent* pupils to overlap by
    >= ``min_overlap``, i.e. selected LEDs spaced no farther than the overlap
    *pitch* (the centre distance giving exactly ``min_overlap``). We therefore lay a
    hexagonal lattice of that pitch over the darkfield annulus and snap each lattice
    point to the nearest real LED — the minimal set whose *neighbours* actually meet
    the overlap floor.

    (An earlier dominating-set approach was wrong: it only guaranteed every dropped
    LED was near a *selected* one, letting adjacent selected pupils drift to ~2x the
    pitch — ~26% overlap — and so under-sampled Fourier space with far too few
    frames.)

    Snapping to discrete LEDs perturbs the spacing, so the lattice is tightened by
    an adaptive safety factor until the 90th-percentile neighbour overlap meets the
    target (robust to a few unavoidable annulus-boundary gaps). The count is purely
    geometry-driven: it scales as (NA range / objective NA)^2, so a larger objective
    pupil needs *fewer* darkfield frames, not more.

    Returns ``(selected_indices, report)``. Deterministic.
    """
    pitch = pitch_for_overlap(pupil_radius_na, min_overlap)
    cands = darkfield_candidates(table, inner_na, outer_na)
    if not cands or pitch <= 0:
        return list(cands), SelectionReport(len(cands), len(cands), pitch, 0.0, 1.0)
    pts = {i: table[i] for i in cands}

    # Floor the lattice step so a tiny pitch (small pupil / very high overlap) can't
    # explode the lattice — finer than the LED spacing just reselects every LED.
    min_step = max(1e-6, (2.0 * outer_na) / 200.0)

    best = None
    for safety in (1.0, 0.92, 0.85, 0.78, 0.72, 0.66, 0.6):
        step = max(pitch * safety, min_step)
        selected = _snap_lattice_to_leds(pts, cands, inner_na, outer_na, step)
        spacing = _percentile(_selected_nn_distances(selected, pts), 0.9)
        overlap = two_circle_overlap_fraction(spacing, pupil_radius_na) if spacing > 0 else 1.0
        best = (selected, spacing, overlap)
        if overlap >= min_overlap or len(selected) >= len(cands):
            break

    selected, spacing, overlap = best
    return selected, SelectionReport(
        n_candidates=len(cands),
        n_selected=len(selected),
        pitch_na=pitch,
        neighbor_spacing_na=spacing,
        min_achieved_overlap=overlap,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Grouping into multiplexed patterns
# ─────────────────────────────────────────────────────────────────────────────


def recommended_leds_per_pattern(n_selected: int) -> int:
    """A balanced LEDs-per-pattern for ``n_selected`` darkfield LEDs.

    No value is rigorously "optimal" — it trades acquisition speed (fewer, larger
    patterns) against reconstruction conditioning (smaller patterns constrain the
    inverse problem more). ``round(sqrt(N))`` balances the pattern *count* against
    the per-pattern LED count; we cap at the paper's empirically-robust 8 and floor
    at 2 (so there is always a multiplexing speed-up), never exceeding ``N``.
    """
    if n_selected <= 1:
        return max(1, int(n_selected))
    m = int(round(math.sqrt(n_selected)))
    return max(2, min(8, min(int(n_selected), m)))


def group_leds(indices: Sequence[int], leds_per_pattern: int, seed: int = 0) -> List[List[int]]:
    """Randomly (seeded) partition ``indices`` into groups of ``leds_per_pattern``.

    Random multiplexing per the source-coded FPM scheme; the seed makes it
    reproducible so the cycle definition fully determines the patterns (and the
    saved manifest records which LEDs each frame used). The final group may be
    smaller. Returns a list of LED-index lists.
    """
    leds_per_pattern = max(1, int(leds_per_pattern))
    shuffled = list(indices)
    random.Random(int(seed)).shuffle(shuffled)
    return [shuffled[i : i + leds_per_pattern] for i in range(0, len(shuffled), leds_per_pattern)]


def build_fpm_darkfield_patterns(
    table: NaTable,
    *,
    objective_na: float,
    outer_na: float = 0.8,
    inner_na: Optional[float] = None,
    min_overlap: float = 0.6,
    leds_per_pattern: int = 0,
    seed: int = 0,
) -> Tuple[List[List[int]], SelectionReport]:
    """Full pipeline: select darkfield LEDs and group into multiplexed patterns.

    The darkfield region is the annulus from ``inner_na`` (default: the objective
    NA, i.e. the brightfield/darkfield boundary) out to ``outer_na``. The pupil
    radius is the objective NA. ``leds_per_pattern`` <= 0 means **auto** — a
    balanced value computed from the selected-LED count
    (:func:`recommended_leds_per_pattern`). Returns ``(patterns, report)`` where
    ``patterns`` is a list of LED-index lists, one per multiplexed darkfield
    capture.
    """
    inner = float(objective_na) if inner_na is None else float(inner_na)
    selected, report = select_darkfield_leds(
        table,
        inner_na=inner,
        outer_na=float(outer_na),
        pupil_radius_na=float(objective_na),
        min_overlap=float(min_overlap),
    )
    m = int(leds_per_pattern)
    if m <= 0:
        m = recommended_leds_per_pattern(len(selected))
    patterns = group_leds(selected, m, seed=seed)
    return patterns, report


# ─────────────────────────────────────────────────────────────────────────────
# Full FPM: brightfield single-LED sweep + angle-clustered darkfield sweep
# ─────────────────────────────────────────────────────────────────────────────


def brightfield_leds(table: NaTable, objective_na: float) -> List[int]:
    """Every dome LED inside the objective pupil (illumination NA <= objective NA).

    These are the brightfield LEDs; the sweep captures one frame per LED, single
    LED at a time. Sorted by NA then index (centre-out, deterministic).
    """
    leds = [idx for idx, p in table.items() if _na(p) <= float(objective_na)]
    leds.sort(key=lambda i: (_na(table[i]), i))
    return leds


def pseudorandom_sample(indices: Sequence[int], n: int, seed: int = 0) -> List[int]:
    """A reproducible pseudorandom subset of ``n`` of ``indices``.

    Returns all of ``indices`` (order unchanged) when ``n <= 0`` or ``n`` exceeds
    the count. Otherwise the kept indices preserve the input order (e.g. the
    centre-out NA ordering of :func:`brightfield_leds`), so only *which* LEDs are
    dropped is randomised — the acquisition order stays sensible.
    """
    items = list(indices)
    if n <= 0 or n >= len(items):
        return items
    order = list(range(len(items)))
    random.Random(int(seed)).shuffle(order)
    keep = set(order[: int(n)])
    return [items[i] for i in range(len(items)) if i in keep]


@dataclass
class DarkfieldCell:
    """One angle-clustered darkfield frame: co-located LEDs fired together."""

    indices: List[int]                 # member LED indices (all lit in this frame)
    centroid: Tuple[float, float]      # mean (na_x, na_y) of the members


def _centroid(indices: Sequence[int], pts) -> Tuple[float, float]:
    if not indices:
        return (0.0, 0.0)
    sx = sum(pts[i][0] for i in indices)
    sy = sum(pts[i][1] for i in indices)
    n = float(len(indices))
    return (sx / n, sy / n)


def cluster_darkfield_leds(
    table: NaTable,
    *,
    inner_na: float,
    outer_na: float,
    pupil_radius_na: float,
    min_overlap: float = 0.6,
) -> Tuple[List[DarkfieldCell], SelectionReport]:
    """Bin darkfield LEDs into angular **cells** at the ~``min_overlap`` tiling step.

    Unlike the source-coded scheme (which picks one representative LED per tile and
    multiplexes random groups across the whole annulus), this assigns **every**
    darkfield LED to the nearest tile centre on a hex lattice of the overlap pitch,
    so each cell is a tight cluster of *adjacent* LEDs at nearly the same angle.
    Firing each cell as one frame keeps its members co-located in Fourier space —
    so every frame maps to one tight patch of the cap and the per-shell angular
    structure that 3D/tomographic reconstruction depends on is preserved (random
    multiplexing would scramble it).

    Returns ``(cells, report)`` where ``report.n_selected`` is the cell count.
    """
    pitch = pitch_for_overlap(pupil_radius_na, min_overlap)
    cands = darkfield_candidates(table, inner_na, outer_na)
    if not cands or pitch <= 0:
        if not cands:
            return [], SelectionReport(0, 0, pitch, 0.0, 1.0)
        pts0 = {i: table[i] for i in cands}
        cell = DarkfieldCell(list(cands), _centroid(cands, pts0))
        return [cell], SelectionReport(len(cands), 1, pitch, 0.0, 1.0)

    pts = {i: table[i] for i in cands}
    centers = _hex_lattice_points(inner_na, outer_na, pitch)
    if not centers:
        cell = DarkfieldCell(
            sorted(cands, key=lambda i: (_na(pts[i]), i)), _centroid(cands, pts)
        )
        return [cell], SelectionReport(len(cands), 1, pitch, 0.0, 1.0)

    # Voronoi assignment: each darkfield LED joins its nearest tile centre.
    buckets: Dict[int, List[int]] = {}
    for i in cands:
        x, y = pts[i]
        best_c, best_d2 = None, None
        for ci, (cx, cy) in enumerate(centers):
            d2 = (x - cx) ** 2 + (y - cy) ** 2
            if best_d2 is None or d2 < best_d2:
                best_d2, best_c = d2, ci
        buckets.setdefault(best_c, []).append(i)

    cells: List[DarkfieldCell] = []
    for ci in buckets:
        members = sorted(buckets[ci], key=lambda i: (_na(pts[i]), i))
        cells.append(DarkfieldCell(members, _centroid(members, pts)))
    # Deterministic order: by centroid NA, then azimuth.
    cells.sort(key=lambda c: (math.hypot(*c.centroid), math.atan2(c.centroid[1], c.centroid[0])))

    report = SelectionReport(
        n_candidates=len(cands),
        n_selected=len(cells),
        pitch_na=pitch,
        neighbor_spacing_na=pitch,
        min_achieved_overlap=two_circle_overlap_fraction(pitch, pupil_radius_na),
    )
    return cells, report


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic dome (simulation + tests only — NOT the real geometry)
# ─────────────────────────────────────────────────────────────────────────────


def synthetic_dome_na_table(n_leds: int = 793, max_na: float = 0.98) -> NaTable:
    """Generate a plausible hemispherical-dome NA table for sim/tests.

    Uses a Fibonacci-sphere-style spiral so NA-space is filled roughly uniformly
    out to ``max_na``. This is a stand-in only; the real positions come from the
    firmware ``pledposna`` dump.
    """
    table: NaTable = {}
    golden = math.pi * (3.0 - math.sqrt(5.0))  # golden angle
    for i in range(int(n_leds)):
        # Spread NA quadratically-ish so density is roughly uniform in NA-area.
        frac = (i + 0.5) / float(n_leds)
        na = max_na * math.sqrt(frac)
        theta = i * golden
        table[i] = (na * math.cos(theta), na * math.sin(theta))
    return table
