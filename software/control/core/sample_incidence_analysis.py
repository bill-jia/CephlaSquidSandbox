"""Texture-based focus maps and illumination estimates from overlapping fields.

No hardware is accessed here. Positions are stage coordinates in micrometers;
image shifts are observed texture displacements in the delivered camera image.
"""

from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree


def preview(image, maximum=512):
    image = np.squeeze(np.asarray(image))
    if image.ndim == 3 and image.shape[-1] in (3, 4):
        image = image[..., :3].mean(axis=-1)
    if image.ndim != 2 or min(image.shape) < 24 or not np.isfinite(image).all():
        raise ValueError("A finite monochrome/RGB camera field of at least 24 pixels is required.")
    scale = min(1.0, maximum / max(image.shape))
    return cv2.resize(
        image.astype(np.float32),
        (round(image.shape[1] * scale), round(image.shape[0] * scale)),
        interpolation=cv2.INTER_AREA,
    )


def robust_fit(design, values, iterations=8):
    """Huber IRLS; returns coefficients and residuals without discarding outliers."""
    design, values = np.asarray(design, float), np.asarray(values, float)
    if len(values) <= design.shape[1] or np.linalg.matrix_rank(design) < design.shape[1]:
        raise ValueError("Insufficient independent measurements for this fit.")
    weights = np.ones(len(values))
    for _ in range(iterations):
        root = np.sqrt(weights)
        coefficients = np.linalg.lstsq(design * root[:, None], values * root, rcond=None)[0]
        residual = values - design @ coefficients
        scale = max(1.4826 * np.median(np.abs(residual - np.median(residual))), 1e-8)
        weights = np.minimum(1, 1.5 * scale / np.maximum(np.abs(residual), 1e-12))
    return coefficients, residual


def texture_scores(image, black_level=0):
    """3×3 local focus scores with broad brightness and sparse marks suppressed.

    A noise-rejecting difference of Gaussians measures resolved texture. Divide
    by a local low-pass intensity before squaring, so coating amount and beam
    gradients do not directly determine the winning focus height. Winsorization
    limits sparse hot pixels/pockmark boundaries. No texture implies no focus.
    """
    image = np.asarray(image)
    if image.ndim == 3:
        image = image[..., :3].mean(axis=-1)
    image = np.squeeze(image).astype(np.float32) - black_level
    if image.ndim != 2 or not np.isfinite(image).all():
        raise ValueError("Invalid focus image.")
    mean_signal = float(np.median(image))
    if mean_signal <= 1:
        return np.zeros((3, 3))
    envelope = cv2.GaussianBlur(image, (0, 0), 12)
    band = cv2.GaussianBlur(image, (0, 0), 0.8) - cv2.GaussianBlur(image, (0, 0), 2.5)
    power = (band / np.maximum(envelope, 0.15 * mean_signal)) ** 2
    y_edges = np.linspace(0, image.shape[0], 4, dtype=int)
    x_edges = np.linspace(0, image.shape[1], 4, dtype=int)
    result = np.empty((3, 3))
    for row in range(3):
        for col in range(3):
            tile = power[y_edges[row] : y_edges[row + 1], x_edges[col] : x_edges[col + 1]]
            result[row, col] = np.mean(np.minimum(tile, np.percentile(tile, 95)))
    return result


@dataclass
class FocusPeaks:
    height_um: np.ndarray
    uncertainty_um: np.ndarray
    valid: np.ndarray


def focus_peaks(z_um, scores):
    """Resolve interior focus maxima; endpoints/flat/ambiguous curves are invalid."""
    z, scores = np.asarray(z_um, float), np.asarray(scores, float)
    if len(z) < 5 or not np.all(np.diff(z) > 0) or scores.shape != (len(z), 3, 3):
        raise ValueError("Focus scanning requires increasing Z and at least five 3×3 score planes.")
    height, uncertainty = np.full((3, 3), np.nan), np.full((3, 3), np.nan)
    for row in range(3):
        for col in range(3):
            curve = scores[:, row, col]
            peak = int(np.argmax(curve))
            if not np.isfinite(curve).all() or peak in (0, len(z) - 1) or curve[peak] <= 1e-7:
                continue
            # Both ends must fall away; brightness alone cannot establish focus.
            prominence = (curve[peak] - max(curve[0], curve[-1])) / curve[peak]
            if prominence < 0.08:
                continue
            x = z[peak - 1 : peak + 2] - z[peak]
            a, b, _ = np.polyfit(x, curve[peak - 1 : peak + 2] / curve[peak], 2)
            if a >= 0:
                continue
            offset = -b / (2 * a)
            if abs(offset) > max(abs(x[0]), abs(x[-1])):
                continue
            height[row, col] = z[peak] + offset
            # Resolution floor, not a statistical confidence interval. Agreement
            # across repeated/overlapping fields supplies the independent check.
            uncertainty[row, col] = max(np.min(np.diff(z)) / 4, 0.02)
    return FocusPeaks(height, uncertainty, np.isfinite(height))


def register_fields(images, positions_um):
    """Measure texture motion, then calibrate stage-to-image direction/scale.

    High-pass phase correlation tolerates broad illumination differences. This
    empirical transform handles image rotation, reflection and stage axis signs.
    """
    fields = [preview(image) for image in images]
    if any(image.shape != fields[0].shape for image in fields):
        raise ValueError("Camera field dimensions changed during the sweep.")
    reference = fields[0] - cv2.GaussianBlur(fields[0], (0, 0), 8)
    window = cv2.createHanningWindow((reference.shape[1], reference.shape[0]), cv2.CV_32F)
    shifts = [np.zeros(2)]
    for image in fields[1:]:
        high = image - cv2.GaussianBlur(image, (0, 0), 8)
        shift, response = cv2.phaseCorrelate(reference.copy(), high, window.copy())
        if response < 0.12 or not np.isfinite(shift).all():
            raise ValueError("Overlapping texture could not be registered reliably; use a smaller XY dither.")
        shifts.append(np.asarray(shift))
    shifts = np.asarray(shifts)
    positions = np.asarray(positions_um)
    offsets = positions - positions[0]
    transform = np.linalg.lstsq(offsets, shifts, rcond=None)[0].T
    error = float(np.sqrt(np.mean(np.sum((offsets @ transform.T - shifts) ** 2, axis=1))))
    if np.linalg.matrix_rank(transform) < 2 or np.linalg.cond(transform) > 4 or error > 2:
        raise ValueError("Texture motion is inconsistent with XY travel; alignment measurements are inconclusive.")
    return shifts, transform, error


def image_to_stage_matrix(transform):
    """Map image displacement to specimen coordinates in the Stage API frame.

    ``transform`` measures MOTION of a fixed specimen feature as the stage
    moves. A feature to the right of the field center is reached by moving
    against that motion, hence the minus sign. Stage positions already include
    motor/encoder and canonical signs; delivered frames already include camera
    reversal/rotation/flip. Do not apply any of those settings a second time.
    """
    return -np.linalg.inv(np.asarray(transform, float))


def stage_field_size(field_shape, transform):
    """Extent of the delivered rectangular camera field along stage X and Y."""
    h, w = field_shape
    corners = np.array([(0, 0), (w, 0), (w, h), (0, h)])
    return np.ptp(corners @ image_to_stage_matrix(transform).T, axis=0)


def stage_profiles(image, transform):
    """Center profiles along +stage X/right and +stage Y/down, in percent FOV.

    Sample the native image along stage axes, including rotated/reflected
    cameras, rather than labelling native image rows/columns as stage X/Y.
    """
    image = np.asarray(image)
    h, w = image.shape
    center = (np.array([w, h]) - 1) / 2
    result = []
    for axis in range(2):
        direction = -np.asarray(transform)[:, axis]
        nonzero = np.abs(direction) > 1e-12
        half_span = np.min(center[nonzero] / np.abs(direction[nonzero]))
        distance = np.linspace(-half_span, half_span, max(h, w))
        pixels = center[:, None] + direction[:, None] * distance
        values = ndimage.map_coordinates(image, [pixels[1], pixels[0]], order=1, mode="nearest")
        extent = stage_field_size(image.shape, transform)[axis]
        result.append((100 * distance / extent, values))
    return result


@dataclass
class FlatnessResult:
    coordinates_um: np.ndarray
    heights_um: np.ndarray
    residuals_um: np.ndarray
    plane: np.ndarray  # dz/dx, dz/dy, intercept (all in micrometers)
    local_slopes: np.ndarray
    direction_agreement: float
    plane_span_um: float
    residual_rms_um: float
    overlap_rms_um: float
    accepted: bool
    diagnosis: str
    sample_variation: Optional[float] = None
    coating_message: str = ""
    camera_focus_offsets_um: Optional[np.ndarray] = None
    stage_to_image: Optional[np.ndarray] = None
    field_shape: Optional[tuple] = None
    camera_focus_deltas_um: Optional[np.ndarray] = None


def assess_flatness(positions_um, peaks, transform, field_shape, max_span_um=0.5, max_residual_um=0.25):
    """Separate sample-fixed height from repeatable camera-field focus effects.

    Jointly fit z = sample_plane(world XY) + camera_focus_offset(region).
    Dithering breaks the ambiguity: real sample tilt changes focus at the same
    camera position as the stage moves, whereas objective field curvature is
    tied to the camera region. Remove the latter before flatness and overlap
    checks. Weak edge curves are excluded, not assigned a spurious height.
    Nine valid central regions can establish a plane without usable edge peaks;
    unavailable local slopes/overlap checks are reported explicitly in that case.
    """
    resolutions = [np.median(peak.uncertainty_um[peak.valid]) for peak in peaks if np.any(peak.valid)]
    if not resolutions or max(resolutions) > max_residual_um:
        raise ValueError(
            "Focus Z sampling is too coarse for the residual tolerance; reduce the step or increase planes."
        )
    h, w = field_shape
    camera_offsets = np.array(
        [((c + 0.5) * w / 3 - w / 2, (r + 0.5) * h / 3 - h / 2) for r in range(3) for c in range(3)]
    )
    local_offsets = camera_offsets @ image_to_stage_matrix(transform).T
    points, heights, field_ids, tile_ids = [], [], [], []
    for field_id, (position, peak) in enumerate(zip(positions_um, peaks)):
        valid = peak.valid.ravel()
        if not np.any(valid):
            raise ValueError(
                f"Field {field_id + 1}: no resolved texture focus peak, including the center; increase Z range or signal."
            )
        xy = np.asarray(position) + local_offsets[valid]
        z = peak.height_um.ravel()[valid]
        points.extend(xy)
        heights.extend(z)
        field_ids.extend([field_id] * len(z))
        tile_ids.extend(np.flatnonzero(valid))
    points, heights, field_ids = np.asarray(points), np.asarray(heights), np.asarray(field_ids)
    tile_ids = np.asarray(tile_ids)
    observed_tiles = np.unique(tile_ids)
    # Set the center's optical offset to zero; if center texture is absent,
    # use the nearest observable region. Only this unimportant Z origin changes.
    anchor = min(observed_tiles, key=lambda tile: np.linalg.norm(camera_offsets[tile]))
    nuisance_tiles = [tile for tile in observed_tiles if tile != anchor]
    design = np.column_stack((points, np.ones(len(points)), *(tile_ids == tile for tile in nuisance_tiles)))
    coefficients, residual = robust_fit(design, heights)
    plane = coefficients[:3]
    camera_offsets_z = np.full(9, np.nan)
    camera_offsets_z[anchor] = 0
    camera_offsets_z[nuisance_tiles] = coefficients[3:]
    corrected_heights = heights - camera_offsets_z[tile_ids]
    local_slopes = np.full((len(positions_um), 2), np.nan)
    for field_id in range(len(positions_um)):
        selected = field_ids == field_id
        local_design = np.column_stack((points[selected], np.ones(np.count_nonzero(selected))))
        if np.count_nonzero(selected) > 3 and np.linalg.matrix_rank(local_design) == 3:
            local_plane, _ = robust_fit(local_design, corrected_heights[selected])
            local_slopes[field_id] = local_plane[:2]
    field_size_um = stage_field_size(field_shape, transform)
    slope_resolution = max_residual_um / max(field_size_um)
    usable_slopes = local_slopes[np.isfinite(local_slopes).all(axis=1)]
    strength = np.linalg.norm(usable_slopes, axis=1)
    resolved = strength > slope_resolution
    global_strength = np.linalg.norm(plane[:2])
    if not len(usable_slopes):
        agreement = float("nan")
    elif global_strength > slope_resolution and np.any(resolved):
        agreement = np.mean((usable_slopes[resolved] @ plane[:2]) / (strength[resolved] * global_strength))
    elif not np.any(resolved):
        agreement = 1.0
    else:
        agreement = 0.0
    # Paired overlap residuals compare the same neighborhood at different camera
    # locations, after accounting for the small separation on the fitted plane.
    pairs = cKDTree(points).query_pairs(0.10 * min(field_size_um))
    overlap = [residual[i] - residual[j] for i, j in pairs if field_ids[i] != field_ids[j]]
    overlap_rms = float(np.sqrt(np.mean(np.square(overlap)))) if len(overlap) >= 4 else float("nan")
    residual_rms = float(np.sqrt(np.mean(residual**2)))
    footprint = (np.asarray(positions_um)[:, None, :] + local_offsets[None, :, :]).reshape(-1, 2)
    plane_span = float(np.ptp(footprint @ plane[:2]))
    local_disagreement = (
        float(np.sqrt(np.mean(np.sum(((usable_slopes - plane[:2]) * field_size_um) ** 2, axis=1))))
        if len(usable_slopes)
        else 0.0
    )
    consistent = (not np.isfinite(agreement) or agreement >= 0.8) and local_disagreement <= 2 * max_residual_um
    overlap_ok = not np.isfinite(overlap_rms) or overlap_rms <= max_residual_um
    accepted = plane_span <= max_span_um and residual_rms <= max_residual_um and overlap_ok
    accepted = bool(accepted and consistent)
    if accepted:
        diagnosis = "Within flatness/tilt tolerances after removing camera-fixed focus effects."
        if not np.isfinite(overlap_rms):
            diagnosis += (
                " Edge peaks unresolved; flatness uses distributed central regions, without an overlap focus check."
            )
    elif consistent and residual_rms <= max_residual_um and overlap_ok:
        diagnosis = "Consistent geometric tilt across fields; adjust sample holder relative to the objective."
    else:
        diagnosis = "Inconsistent sample-fixed defocus: coating/topography or changing optical effects; holder tilt is unproven."
    # Directly observed focus relative to each field's center, BEFORE removing
    # optics. These values can be compared with manual corner-versus-center
    # focusing; their sign need not match the corrected specimen plane alone.
    deltas = [[] for _ in range(9)]
    for peak in peaks:
        if peak.valid[1, 1]:
            for tile in np.flatnonzero(peak.valid):
                deltas[tile].append(peak.height_um.ravel()[tile] - peak.height_um[1, 1])
    camera_deltas = np.array([np.median(values) if values else np.nan for values in deltas]).reshape(3, 3)
    return FlatnessResult(
        points,
        corrected_heights,
        residual,
        plane,
        local_slopes,
        float(agreement),
        plane_span,
        residual_rms,
        overlap_rms,
        accepted,
        diagnosis,
        camera_focus_offsets_um=camera_offsets_z.reshape(3, 3),
        stage_to_image=np.array(transform, copy=True),
        field_shape=tuple(field_shape),
        camera_focus_deltas_um=camera_deltas,
    )


def _quadratic(x, y):
    return np.column_stack((x.ravel(), y.ravel(), (x * x).ravel(), (x * y).ravel(), (y * y).ravel()))


@dataclass
class IlluminationResult:
    envelope: np.ndarray
    coefficients: np.ndarray
    center_px: Optional[np.ndarray]
    asymmetry: float
    imbalance_xy: np.ndarray
    sample_variation: float
    overlap_residual: float
    center_uncertainty_px: float = float("inf")


def separate_illumination(images, shifts, black_level=0):
    """Fit the camera-fixed log envelope using SAME-sample overlap differences.

    I_k(p) = S(p - shift_k) * L(p). In overlaps, log I_i(p) -
    log I_j(p + shift_j - shift_i) cancels arbitrary specimen brightness S.
    Fit the five nonconstant quadratic coefficients robustly. Per-frame gains
    are deliberately NOT free: arbitrary bleaching/gain changes make the linear
    beam gradient unidentifiable. Large residuals invalidate incidence results.
    """
    fields = [np.asarray(image, float) - black_level for image in images]
    h, w = fields[0].shape
    if len(fields) < 5 or any(image.shape != (h, w) for image in fields):
        raise ValueError("At least five matching overlapping fields are required.")
    if any(not np.isfinite(image).all() or np.median(image) <= 1 for image in fields):
        raise ValueError("Insufficient fluorescence above camera black level.")
    fields = [np.maximum(image, 1e-6) for image in fields]
    smoothed = [ndimage.gaussian_filter(image, 1.2) for image in fields]
    yy, xx = np.mgrid[8 : h - 8 : 6, 8 : w - 8 : 6].astype(float)
    design, values, pair_ids = [], [], []
    for i in range(len(fields)):
        for j in range(i + 1, len(fields)):
            dx, dy = np.asarray(shifts[j]) - shifts[i]
            xj, yj = xx + dx, yy + dy
            valid = (xj >= 8) & (xj < w - 8) & (yj >= 8) & (yj < h - 8)
            if np.count_nonzero(valid) < 50:
                continue
            xi, yi, xj, yj = xx[valid], yy[valid], xj[valid], yj[valid]
            # Independent fields, rather than thousands of correlated texture
            # pixels, set the uncertainty. Bound fitting cost for live updates.
            stride = max(1, len(xi) // 400)
            xi, yi, xj, yj = xi[::stride], yi[::stride], xj[::stride], yj[::stride]
            a = _quadratic(2 * (xi + 0.5) / w - 1, 2 * (yi + 0.5) / h - 1)
            b = _quadratic(2 * (xj + 0.5) / w - 1, 2 * (yj + 0.5) / h - 1)
            # Average a few pixels before comparison to tolerate subpixel
            # registration error without fitting graininess as illumination.
            first = ndimage.map_coordinates(smoothed[i], [yi, xi], order=1)
            second = ndimage.map_coordinates(smoothed[j], [yj, xj], order=1)
            design.append(a - b)
            values.append(np.log(first) - np.log(second))
            pair_ids.append(np.tile([i, j], (len(xi), 1)))
    if not design:
        raise ValueError("No usable specimen overlaps.")
    design, values, pair_ids = np.concatenate(design), np.concatenate(values), np.concatenate(pair_ids)
    coefficients, residual = robust_fit(design, values)
    overlap_residual = float(1.4826 * np.median(np.abs(residual - np.median(residual))))
    if overlap_residual > 0.12:
        raise ValueError(
            "Overlap intensity changed too much: bleaching, coating motion or registration error; angle withheld."
        )
    y, x = np.mgrid[:h, :w].astype(float)
    x, y = 2 * (x + 0.5) / w - 1, 2 * (y + 0.5) / h - 1
    log_envelope = (_quadratic(x, y) @ coefficients).reshape(h, w)
    if np.max(np.abs(log_envelope)) > 10:
        raise ValueError("Illumination fit is unstable.")
    envelope = np.exp(log_envelope)
    envelope /= np.mean(envelope)
    bx, by, qxx, qxy, qyy = coefficients
    nonradial = bx * x + by * y + (qxx - qyy) / 2 * (x * x - y * y) + qxy * x * y
    asymmetry = float(100 * np.sqrt(np.mean(nonradial**2)))
    hessian = np.array([[2 * qxx, qxy], [qxy, 2 * qyy]])
    center = None
    center_uncertainty = float("inf")
    if np.max(np.linalg.eigvalsh(hessian)) < -0.01 and np.linalg.cond(hessian) < 100:
        normalized = -np.linalg.solve(hessian, coefficients[:2])
        if np.linalg.norm(normalized) < 4:
            center = (normalized + 1) * np.array([w, h]) / 2 - 0.5
            centers = []
            # Leave out each FOV with all of its overlap pairs. A center that
            # depends strongly on one specimen field cannot establish an angle.
            for field_id in range(len(fields)):
                keep = np.all(pair_ids != field_id, axis=1)
                try:
                    p, _ = robust_fit(design[keep], values[keep], iterations=3)
                    hh = np.array([[2 * p[2], p[3]], [p[3], 2 * p[4]]])
                    if np.max(np.linalg.eigvalsh(hh)) >= -0.005:
                        break
                    centers.append((-np.linalg.solve(hh, p[:2]) + 1) * np.array([w, h]) / 2 - 0.5)
                except (ValueError, np.linalg.LinAlgError):
                    break
            if len(centers) == len(fields):
                centers = np.asarray(centers)
                variance = (len(fields) - 1) / len(fields) * np.sum((centers - np.mean(centers, axis=0)) ** 2)
                center_uncertainty = float(np.sqrt(variance))
    # Broad specimen variation after removing the camera-fixed envelope. This
    # diagnostic does not turn brightness differences into a height estimate.
    sample = [ndimage.gaussian_filter(image / envelope, max(2, min(h, w) / 32)) for image in fields]
    sample_values = np.concatenate([image[::8, ::8].ravel() for image in sample])
    sample_variation = float(np.std(sample_values) / np.mean(sample_values))
    return IlluminationResult(
        envelope,
        coefficients,
        center,
        asymmetry,
        100 * np.tanh(coefficients[:2]),
        sample_variation,
        overlap_residual,
        center_uncertainty,
    )


@dataclass
class IncidenceResult:
    illumination: IlluminationResult
    angle_deg: Optional[float]
    lateral_shift_um: Optional[np.ndarray]
    message: str
    angle_uncertainty_deg: Optional[float] = None
    reference_drift: float = 0.0


def incidence_from_planes(lower, middle, upper, z_baseline_um, transform):
    """Apparent geometric ray angle from a known sample-stage Z translation.

    Unbounded/flat envelopes cannot locate a beam center and do not yield an
    angle. Three planes test linearity instead of assigning an angle to a
    changing/clipped intensity pattern. The small accepted sample-plane tilt
    is separately reported; this is relative to the commanded optical Z axis.
    """
    if z_baseline_um <= 0:
        raise ValueError("A positive Z baseline is required.")
    if any(result.center_px is None for result in (lower, middle, upper)):
        return IncidenceResult(middle, None, None, "Beam maximum is not bounded reliably; incidence angle unavailable.")
    uncertainty = float(np.hypot(lower.center_uncertainty_px, upper.center_uncertainty_px))
    if not np.isfinite(uncertainty) or uncertainty > 1:
        return IncidenceResult(
            middle, None, None, "Beam center varies between specimen fields; angle uncertainty is too large."
        )
    midpoint_error = middle.center_px - (lower.center_px + upper.center_px) / 2
    shift = upper.center_px - lower.center_px
    if np.linalg.norm(midpoint_error) > max(0.75, 0.2 * np.linalg.norm(shift)):
        return IncidenceResult(
            middle, None, None, "Beam motion is nonlinear across Z; increase signal or reduce Z baseline."
        )
    lateral_um = image_to_stage_matrix(transform) @ shift
    angle = float(np.degrees(np.arctan2(np.linalg.norm(lateral_um), z_baseline_um)))
    uncertainty_um = uncertainty * np.linalg.norm(np.linalg.inv(transform), ord=2)
    angle_uncertainty = float(np.degrees(np.arctan2(uncertainty_um, z_baseline_um)))
    return IncidenceResult(
        middle,
        angle,
        lateral_um,
        "Apparent beam angle relative to optical Z; sample tilt reported separately.",
        angle_uncertainty,
    )
