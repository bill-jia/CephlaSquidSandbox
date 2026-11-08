"""Sparse-bead focus and illumination measurements. No hardware access.

Photometry uses native pixels; geometry uses the same preview coordinates as
the alignment plots. Repeated-bead differences remove intrinsic bead brightness.
Neither stage dithering nor these fits establish an absolute objective axis.
"""

from dataclasses import dataclass

import cv2
import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree
from control.core.sample_bead_geometry import correct_points, fit_bead_geometry

from control.core.sample_incidence_analysis import (
    FlatnessResult,
    IlluminationResult,
    _quadratic,
    image_to_stage_matrix,
    preview,
    robust_fit,
    stage_field_size,
)


@dataclass
class BeadMeasurements:
    points_px: np.ndarray
    native_points_px: np.ndarray
    flux: np.ndarray
    sharpness: np.ndarray
    brightness: np.ndarray
    valid: np.ndarray
    saturated: np.ndarray


@dataclass
class BeadFocus:
    points_px: np.ndarray
    native_points_px: np.ndarray
    height_um: np.ndarray
    uncertainty_um: np.ndarray
    valid: np.ndarray
    scores: np.ndarray
    brightness_scores: np.ndarray
    projection: np.ndarray


def detect_beads(image, radius=16, min_snr=8.0):
    image = np.asarray(image, np.float32)
    if image.ndim != 2 or not np.isfinite(image).all():
        raise ValueError("Bead analysis requires finite monochrome images.")
    band = ndimage.gaussian_filter(image, 1) - ndimage.gaussian_filter(image, 6)
    noise = max(1.4826 * np.median(np.abs(band - np.median(band))), 0.5)
    y, x = np.where((band == ndimage.maximum_filter(band, size=9)) & (band > max(min_snr * noise, min_snr)))
    keep = (x > radius + 2) & (x < image.shape[1] - radius - 3)
    keep &= (y > radius + 2) & (y < image.shape[0] - radius - 3)
    points, strengths = [], []
    for xx, yy in zip(x[keep], y[keep]):
        cut = image[yy - 3 : yy + 4, xx - 3 : xx + 4]
        signal = np.maximum(cut - np.median(cut[[0, -1]]), 0)
        if signal.sum() <= 0 or signal.max() / signal.sum() > 0.6:
            continue  # isolated hot pixel, rather than a resolved bead
        cy, cx = ndimage.center_of_mass(signal)
        points.append((xx + cx - 3, yy + cy - 3))
        strengths.append(band[yy, xx])
    points = np.asarray(points, float).reshape(-1, 2)
    if len(points) > 1:
        # Exclude unresolved doubles and overlapping photometry apertures.
        reject = set()
        for a, b in cKDTree(points).query_pairs(2 * radius + 2):
            if min(strengths[a], strengths[b]) > 0.15 * max(strengths[a], strengths[b]):
                reject.update((a, b))
            else:
                # Weak diffraction-ring maxima must not eliminate their bead.
                reject.add(a if strengths[a] < strengths[b] else b)
        points = points[[i not in reject for i in range(len(points))]]
    if len(points) > 500:
        raise ValueError("Too many beads for isolated-bead analysis; use a sparser field or smaller aperture.")
    return points


def measure_beads(image, points, radius=16, recenter=False, maximum=512, min_snr=8.0):
    image = np.asarray(image)
    h, w = image.shape
    small = preview(image, maximum)
    scale = np.array(small.shape[::-1]) / [w, h]
    points = np.asarray(points, float).reshape(-1, 2).copy()
    yy, xx = np.mgrid[-radius : radius + 1, -radius : radius + 1]
    rr = xx**2 + yy**2
    aperture, annulus = rr < (0.72 * radius) ** 2, rr > (0.85 * radius) ** 2
    flux, sharpness, brightness, valid, saturated = [], [], [], [], []
    limit = np.iinfo(image.dtype).max if np.issubdtype(image.dtype, np.integer) else np.inf
    for index, (x, y) in enumerate(points):
        x, y = int(round(x)), int(round(y))
        cut = image[y - radius : y + radius + 1, x - radius : x + radius + 1]
        if cut.shape != rr.shape:
            flux.append(np.nan)
            sharpness.append(np.nan)
            brightness.append(np.nan)
            valid.append(False)
            saturated.append(False)
            continue
        background = float(np.median(cut[annulus]))
        noise = max(1.4826 * np.median(np.abs(cut[annulus].astype(float) - background)), 1.0)
        signal = cut.astype(float) - background
        if recenter:
            weights = np.maximum(signal - 2 * noise, 0) * aperture
            if weights.sum() > 0:
                points[index] = [x + np.sum(weights * xx) / weights.sum(), y + np.sum(weights * yy) / weights.sum()]
        total = float(signal[aperture].sum())
        peak = float(signal[aperture].max())
        energy = float(np.sum(signal[aperture] ** 2) - noise**2 * aperture.sum())
        good = total > min_snr * noise * np.sqrt(aperture.sum()) and peak > min_snr * noise
        good = bool(good and energy > 0 and not np.any(cut >= limit))
        saturated.append(bool(np.any(cut >= limit)))
        flux.append(total)
        brightness.append(peak)
        sharpness.append(energy / total**2 if good else np.nan)
        valid.append(good)
    return BeadMeasurements(
        (points + 0.5) * scale - 0.5,
        points.copy(),
        np.asarray(flux),
        np.asarray(sharpness),
        np.asarray(brightness),
        np.asarray(valid),
        np.asarray(saturated),
    )


def bead_focus(z_um, images, radius=16, metric="sharpness", maximum=512, min_snr=8.0):
    z = np.asarray(z_um, float)
    if len(z) < 5 or len(images) != len(z) or not np.all(np.diff(z) > 0):
        raise ValueError("Bead focus requires at least five distinct, increasing measured Z positions.")
    if metric not in ("sharpness", "brightness"):
        raise ValueError("Unknown bead focus metric.")
    projection = np.array(images[0], copy=True)
    for image in images[1:]:
        if image.shape != projection.shape:
            raise ValueError("Camera dimensions changed during the bead stack.")
        np.maximum(projection, image, out=projection)
    points = detect_beads(projection, radius, min_snr)
    if len(points) < 4:
        raise ValueError("Fewer than four isolated beads found; choose a field with distributed beads.")
    measured = [measure_beads(image, points, radius, maximum=maximum, min_snr=min_snr) for image in images]
    scores = np.array([getattr(m, metric) for m in measured])
    brightness = np.array([m.brightness for m in measured])
    valid = np.array([m.valid for m in measured])
    heights, uncertainty = np.full(len(points), np.nan), np.full(len(points), np.nan)
    for bead in range(len(points)):
        if any(m.saturated[bead] for m in measured):
            continue
        curve = scores[:, bead]
        usable = valid[:, bead] & np.isfinite(curve)
        if usable.sum() < 5:
            continue
        i = int(np.argmax(np.where(usable, curve, -np.inf)))
        if i < 1 or i >= len(z) - 1 or not usable[i - 1 : i + 2].all():
            continue
        # Invalid saturated/low-signal planes cannot serve as peak flanks.
        left, right = np.where(usable[:i])[0], np.where(usable[i + 1 :])[0] + i + 1
        if (
            not len(left)
            or not len(right)
            or min(curve[i] - curve[left[0]], curve[i] - curve[right[-1]]) < 0.08 * curve[i]
        ):
            continue
        a, b, _ = np.polyfit(z[i - 1 : i + 2] - z[i], curve[i - 1 : i + 2] / curve[i], 2)
        if a >= 0 or abs(-b / (2 * a)) > min(z[i] - z[i - 1], z[i + 1] - z[i]):
            continue
        heights[bead] = z[i] - b / (2 * a)
        uncertainty[bead] = max(np.max(np.diff(z[i - 1 : i + 2])) / 4, 0.02)
    if np.isfinite(heights).sum() < 4:
        raise ValueError("Fewer than four resolved bead focus peaks; extend Z range, reduce Z step or improve signal.")
    return BeadFocus(
        measured[0].points_px,
        points,
        heights,
        uncertainty,
        np.isfinite(heights),
        scores,
        brightness,
        preview(projection, maximum),
    )


def matched_indices(first, second, shift, tolerance=1.5):
    if not len(first) or not len(second):
        return np.array([], int), np.array([], int)
    distance, index = cKDTree(second).query(first + shift)
    reverse = cKDTree(first + shift).query(second)[1]
    good = (distance < tolerance) & (reverse[index] == np.arange(len(first)))
    return np.where(good)[0], index[good]


def bead_translation(first, second, pixel_scale=1.0):
    """Find a unique translation from bead constellations, independent of intensity.

    Pair-displacement voting proposes translations without a texture-registration
    seed. Four shifted voting grids avoid a single bin boundary splitting a peak.
    Mutual nearest neighbors validate each proposal; competing constellations
    must be rejected rather than resolved using the commanded stage displacement.
    """
    first, second = np.asarray(first, float), np.asarray(second, float)
    if min(len(first), len(second)) < 4:
        raise ValueError("Too few shared beads: at least four are required; use a smaller XY offset.")
    if (
        not np.isfinite(first).all()
        or not np.isfinite(second).all()
        or not np.isfinite(pixel_scale)
        or pixel_scale <= 0
    ):
        raise ValueError("Invalid bead coordinates or analysis pixel scale.")
    tolerance = 1.5 * pixel_scale
    differences = (second[:, None] - first[None]).reshape(-1, 2)
    proposals = []
    for origin in ((0, 0), (0.5, 0), (0, 0.5), (0.5, 0.5)):
        bins = np.floor(differences / (2 * tolerance) + origin).astype(np.int64)
        bins -= bins.min(axis=0)
        keys = bins[:, 0] * (bins[:, 1].max() + 1) + bins[:, 1]
        _, inverse, counts = np.unique(keys, return_inverse=True, return_counts=True)
        sums = np.column_stack([np.bincount(inverse, weights=differences[:, axis]) for axis in (0, 1)])
        # Bound work for dense fields while retaining all strongly supported peaks.
        for index in np.argsort(-counts, kind="stable")[:64]:
            if counts[index] < 2:
                break
            proposals.append((int(counts[index]), sums[index] / counts[index]))
    proposals.sort(key=lambda item: -item[0])
    hypotheses = []
    hypothesis_centers = np.empty((4 * len(proposals), 2))
    first_tree, second_tree = cKDTree(first), cKDTree(second)
    attempted = set()
    for _, shift in proposals:
        key = tuple(np.round(shift / tolerance, 10))
        if key in attempted:
            continue
        attempted.add(key)
        for _ in range(4):
            distance, index = second_tree.query(first + shift)
            candidates = np.where(distance < tolerance)[0]
            if len(candidates) < 4:
                break
            reverse = first_tree.query(second[index[candidates]] - shift)[1]
            a = candidates[reverse == candidates]
            b = index[a]
            if len(a) < 4:
                break
            rms = float(np.sqrt(np.mean(np.sum((second[b] - first[a] - shift) ** 2, axis=1))))
            candidate = (len(a), rms, shift.copy(), a, b)
            delta = hypothesis_centers[: len(hypotheses)] - shift
            nearby = np.flatnonzero(np.einsum("ij,ij->i", delta, delta) < tolerance**2)
            same = int(nearby[0]) if len(nearby) else None
            if same is None:
                hypothesis_centers[len(hypotheses)] = shift
                hypotheses.append(candidate)
            elif (len(a), -rms) > (hypotheses[same][0], -hypotheses[same][1]):
                hypotheses[same] = candidate
                hypothesis_centers[same] = shift
            # Keep the best consensus seen: refining its median can lose a bead
            # near the matching boundary and must not overwrite a stronger fit.
            refined = np.median(second[b] - first[a], axis=0)
            if np.linalg.norm(refined - shift) < 1e-9:
                break
            shift = refined
    hypotheses.sort(key=lambda h: (-h[0], h[1]))
    if not hypotheses:
        raise ValueError("Too few shared beads: no constellation of four matches; use a smaller XY offset.")
    count, rms, shift, a, b = hypotheses[0]
    runner_up = hypotheses[1][0] if len(hypotheses) > 1 else 0
    if runner_up >= max(4, 0.8 * count):
        raise ValueError(f"Ambiguous bead registration: competing translations match {count} and {runner_up} beads.")
    if rms > pixel_scale:
        raise ValueError(
            f"Bead correspondence is inconsistent: {count} matches, RMS {rms:.2f} analysis px "
            f"exceeds {pixel_scale:.2f} px."
        )
    return shift, a, b, dict(matches=count, match_rms_px=rms, competing_matches=runner_up)


def register_beads(
    fields,
    positions,
    images,
    maximum=512,
    pixel_scale=1.0,
    diagnostics=None,
    stage_rms_limit_px=1.0,
    distortion=None,
    check_cancelled=None,
):
    """Register bead catalogs, then independently check motion against stage XY.

    Images are retained for field-dimension validation, not texture correlation.
    Catalog coordinates already use the selected analysis resolution.
    """
    if not np.isfinite(stage_rms_limit_px) or not 0.25 <= stage_rms_limit_px <= 10:
        raise ValueError("Stage-motion RMS limit must be 0.25–10 reference-analysis pixels.")
    details = {} if diagnostics is None else diagnostics
    details.update(pixel_scale=pixel_scale, positions_um=np.asarray(positions), fields=[])
    if len(fields) != len(images) or any(im.shape != images[0].shape for im in images):
        raise ValueError("Camera field dimensions changed during the bead sweep.")
    catalogs = [field.points_px[field.valid] for field in fields]
    details["reference_beads"] = len(catalogs[0])
    shifts, _ = fit_bead_geometry(catalogs, images[0].shape, pixel_scale, details, distortion, check_cancelled)
    offsets = np.asarray(positions) - positions[0]
    transform = np.linalg.lstsq(offsets, shifts, rcond=None)[0].T
    residuals = shifts - offsets @ transform.T
    error = float(np.sqrt(np.mean(np.sum(residuals**2, axis=1))))
    limit = stage_rms_limit_px * pixel_scale
    details.update(
        shifts=shifts,
        transform=transform,
        residuals_px=residuals,
        error_px=error,
        limit_px=limit,
        reference_error_px=error / pixel_scale,
        stage_rms_limit_px=stage_rms_limit_px,
    )
    if np.linalg.matrix_rank(transform) < 2 or np.linalg.cond(transform) > 4 or error > limit:
        raise ValueError(
            f"Bead motion is inconsistent with stage travel: RMS {error:.2f} analysis px "
            f"(limit {limit:.2f} px), transform condition {np.linalg.cond(transform):.2f}. "
            "Matched-bead shifts were found; check XY repeatability and image geometry."
        )
    return shifts, transform, error


def assess_bead_flatness(
    positions, scans, shifts, transform, max_span=0.5, max_residual=0.25, pixel_scale=1.0, distortion=None
):
    h, w = scans[0].projection.shape
    world, camera, heights, ids, bead_indices = [], [], [], [], []
    for i, scan in enumerate(scans):
        keep = scan.valid & (scan.uncertainty_um <= max_residual)
        points = correct_points(scan.points_px[keep], (h, w), distortion)
        world.extend(positions[i] + (points + 0.5 - [w / 2, h / 2]) @ image_to_stage_matrix(transform).T)
        camera.extend(2 * (scan.points_px[keep] + 0.5) / [w, h] - 1)
        heights.extend(scan.height_um[keep])
        ids.extend([i] * len(points))
        bead_indices.extend(np.where(keep)[0])
    world, camera, heights, ids = np.asarray(world), np.asarray(camera), np.asarray(heights), np.asarray(ids)
    if len(heights) < 24 or np.any(np.ptp(camera, axis=0) < 1):
        raise ValueError("Insufficient bead focus coverage; use more beads across both field axes or finer Z steps.")
    field_size = stage_field_size((h, w), transform)
    design = np.column_stack((world / field_size, np.ones(len(world)), _quadratic(camera[:, 0], camera[:, 1])))
    if np.linalg.cond(design) > 1e5:
        raise ValueError("Sample plane and camera-field focus cannot be separated with these bead positions.")
    co, residual = robust_fit(design, heights)
    plane = np.r_[co[:2] / field_size, co[2]]
    corrected = heights - design[:, 3:] @ co[3:]
    slopes = np.full((len(scans), 2), np.nan)
    for i in range(len(scans)):
        keep = ids == i
        try:
            local, _ = robust_fit(np.column_stack((world[keep], np.ones(keep.sum()))), corrected[keep])
            slopes[i] = local[:2]
        except ValueError:
            pass
    pairs = []
    for i in range(len(scans)):
        for j in range(i + 1, len(scans)):
            ii, jj = np.where(ids == i)[0], np.where(ids == j)[0]
            a, b = matched_indices(
                correct_points(scans[i].points_px[np.asarray(bead_indices)[ii]], (h, w), distortion),
                correct_points(scans[j].points_px[np.asarray(bead_indices)[jj]], (h, w), distortion),
                shifts[j] - shifts[i],
                tolerance=1.5 * pixel_scale,
            )
            pairs.extend(residual[ii[a]] - residual[jj[b]])
    if len(pairs) < 8:
        raise ValueError("Too few repeated bead focus measurements to validate sample flatness.")
    rms, overlap = float(np.sqrt(np.mean(residual**2))), float(np.sqrt(np.mean(np.square(pairs))))
    corners = correct_points(np.array([(0, 0), (w, 0), (w, h), (0, h)]) - 0.5, (h, w), distortion)
    corners += 0.5 - np.array([w / 2, h / 2])
    footprint = (np.asarray(positions)[:, None] + (corners @ image_to_stage_matrix(transform).T)[None]).reshape(-1, 2)
    span = float(np.ptp(footprint @ plane[:2]))
    accepted = span <= max_span and rms <= max_residual and overlap <= max_residual
    diagnosis = (
        "Bead surface within flatness tolerances." if accepted else "Bead surface exceeds flatness/overlap tolerances."
    )
    diagnosis += (
        " Slopes are relative to stage travel; they do not independently identify objective, holder or camera tilt."
    )
    yy, xx = np.mgrid[:3, :3]
    u, v = 2 * (xx + 0.5) / 3 - 1, 2 * (yy + 0.5) / 3 - 1
    optics = (_quadratic(u, v) @ co[3:]).reshape(3, 3)
    local_pixels = np.column_stack((u.ravel() * w / 2, v.ravel() * h / 2)) + [w / 2 - 0.5, h / 2 - 0.5]
    local_pixels = correct_points(local_pixels, (h, w), distortion) + 0.5 - [w / 2, h / 2]
    local = local_pixels @ image_to_stage_matrix(transform).T
    deltas = (local @ plane[:2]).reshape(3, 3) + optics
    return FlatnessResult(
        world,
        corrected,
        residual,
        plane,
        slopes,
        float("nan"),
        span,
        rms,
        overlap,
        bool(accepted),
        diagnosis,
        coating_message=f"{len(heights)} bead focus observations; {len(pairs)} matched overlap comparisons. Grid labels are fitted, not individual bead measurements.",
        camera_focus_offsets_um=optics,
        stage_to_image=transform,
        field_shape=(h, w),
        camera_focus_deltas_um=deltas,
    )


def bead_reference_ratio(first, last, pixel_scale=1.0):
    a, b = matched_indices(first.points_px[first.valid], last.points_px[last.valid], np.zeros(2), 1.5 * pixel_scale)
    if len(a) < 4:
        raise ValueError("Too few matching beads in the repeated center reference.")
    ratios = last.flux[last.valid][b] / first.flux[first.valid][a]
    ratio = float(np.median(ratios))
    if np.median(np.abs(ratios / ratio - 1)) > 0.05:
        raise ValueError("Repeated bead intensities changed inconsistently; angle withheld.")
    return ratio


def bead_illumination(fields, shifts, shape, pixel_scale=1.0, distortion=None):
    h, w = shape
    designs, values, pair_ids = [], [], []
    used = []
    for i in range(len(fields)):
        for j in range(i + 1, len(fields)):
            p = correct_points(fields[i].points_px[fields[i].valid], shape, distortion)
            q = correct_points(fields[j].points_px[fields[j].valid], shape, distortion)
            a, b = matched_indices(p, q, shifts[j] - shifts[i], 1.5 * pixel_scale)
            if not len(a):
                continue
            pa, pb = 2 * (p[a] + 0.5) / [w, h] - 1, 2 * (q[b] + 0.5) / [w, h] - 1
            designs.extend(_quadratic(pa[:, 0], pa[:, 1]) - _quadratic(pb[:, 0], pb[:, 1]))
            values.extend(np.log(fields[i].flux[fields[i].valid][a] / fields[j].flux[fields[j].valid][b]))
            pair_ids.extend([(i, j)] * len(a))
            used.extend(pa)
            used.extend(pb)
    design, values, pair_ids = np.asarray(designs), np.asarray(values), np.asarray(pair_ids)
    if len(values) < 20 or len(np.unique(pair_ids)) < 5 or np.any(np.ptp(used, axis=0) < 1):
        raise ValueError("Insufficient shared-bead coverage for illumination/incidence; reduce dither or add beads.")
    if np.linalg.cond(design) > 1e4:
        raise ValueError("Bead positions do not constrain the illumination envelope.")
    co, residual = robust_fit(design, values)
    scatter = float(np.sqrt(np.mean(np.minimum(residual**2, 0.5**2))))
    if scatter > 0.12:
        raise ValueError("Matched-bead brightness is inconsistent across fields; angle withheld.")
    y, x = np.mgrid[:h, :w]
    x, y = 2 * (x + 0.5) / w - 1, 2 * (y + 0.5) / h - 1
    logenv = (_quadratic(x, y) @ co).reshape(h, w)
    if np.max(abs(logenv)) > 10:
        raise ValueError("Bead illumination fit is unstable.")
    envelope = np.exp(logenv)
    envelope /= envelope.mean()

    def center(c):
        hh = np.array([[2 * c[2], c[3]], [c[3], 2 * c[4]]])
        if np.max(np.linalg.eigvalsh(hh)) >= -0.01 or np.linalg.cond(hh) > 100:
            return None
        position = -np.linalg.solve(hh, c[:2])
        return (position + 1) * [w / 2, h / 2] - 0.5 if np.linalg.norm(position) < 4 else None

    peak = center(co)
    uncertainty = float("inf")
    centers = []
    if peak is not None:
        for field in range(len(fields)):
            keep = np.all(pair_ids != field, axis=1)
            try:
                c, _ = robust_fit(design[keep], values[keep])
                point = center(c)
            except ValueError:
                break
            if point is None:
                break
            centers.append(point)
        if len(centers) == len(fields):
            centers = np.asarray(centers)
            uncertainty = float(
                np.sqrt((len(fields) - 1) / len(fields) * np.sum((centers - centers.mean(axis=0)) ** 2))
            )
    nonradial = co[0] * x + co[1] * y + (co[2] - co[4]) / 2 * (x * x - y * y) + co[3] * x * y
    # Bead brightness variation is intrinsic variation plus unmodelled optics.
    signal = []
    for field in fields:
        p = 2 * (correct_points(field.points_px[field.valid], shape, distortion) + 0.5) / [w, h] - 1
        signal.extend(field.flux[field.valid] / np.exp(_quadratic(p[:, 0], p[:, 1]) @ co))
    variation = float(np.std(signal) / np.mean(signal))
    return IlluminationResult(
        envelope,
        co,
        peak,
        float(100 * np.sqrt(np.mean(nonradial**2))),
        100 * np.tanh(co[:2]),
        variation,
        scatter,
        uncertainty,
    )
