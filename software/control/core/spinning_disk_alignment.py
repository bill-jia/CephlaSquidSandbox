"""Native-pixel pinhole measurements, independent of camera hardware and Qt."""

from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree


@dataclass
class PinholeMeasurement:
    centers: np.ndarray  # x, y in the displayed patch
    average: np.ndarray  # background-subtracted, peak-normalized
    horizontal: np.ndarray
    vertical: np.ndarray
    fwhm_x: float
    fwhm_y: float
    edge_steepness_x: float
    edge_steepness_y: float

    @property
    def sharpness(self):
        """Mean normalized 20–80% slope of the four axial edges; higher is better."""
        return (self.edge_steepness_x + self.edge_steepness_y) / 2

    @property
    def width(self):
        """Mean axial FWHM in camera pixels, retained as a size diagnostic."""
        return (self.fwhm_x + self.fwhm_y) / 2


def camera_patch(image, size=512, offset=(0, 0)):
    """Copy a native-resolution crop; offsets are relative to the frame center."""
    image = np.asarray(image)
    color = image.ndim == 3 and image.shape[-1] in (3, 4)
    if not color:
        image = np.squeeze(image)
    if (not color and image.ndim != 2) or not image.size:
        raise ValueError("Expected a monochrome or RGB camera frame")
    h, w = image.shape[:2]
    crop_h, crop_w = min(size, h), min(size, w)
    x = int(np.clip((w - crop_w) // 2 + offset[0], 0, w - crop_w))
    y = int(np.clip((h - crop_h) // 2 + offset[1], 0, h - crop_h))
    patch = image[y : y + crop_h, x : x + crop_w]
    return patch[..., :3].mean(axis=-1) if color else patch.copy()


def profile_fwhm(profile):
    """Interpolate the half-height crossings surrounding the central peak."""
    profile = np.asarray(profile)
    peak = len(profile) // 2
    half = profile[peak] / 2
    if half <= 0:
        return float("nan")
    left = np.flatnonzero(profile[:peak] <= half)
    right = np.flatnonzero(profile[peak + 1 :] <= half) + peak + 1
    if not left.size or not right.size:
        return float("nan")
    l, r = left[-1], right[0]
    x_left = l + (half - profile[l]) / (profile[l + 1] - profile[l])
    x_right = r - 1 + (half - profile[r - 1]) / (profile[r] - profile[r - 1])
    return float(x_right - x_left)


def profile_edge_steepness(profile):
    """Mean rising/falling 20–80% slope in normalized intensity per pixel.

    Apply a fixed 0.7-pixel Gaussian smoothing to suppress pixel noise, then
    normalize to the central peak. On each side, linearly interpolate the
    first outward 80% and 20% crossings and use 0.6 / transition_width.
    This measures the edge transition independently of the pinhole diameter.
    Missing crossings invalidate the measurement rather than rewarding clipping.
    """
    profile = np.asarray(profile, dtype=float)
    if profile.ndim != 1 or profile.size < 5 or not np.isfinite(profile).all():
        return float("nan")
    smooth = ndimage.gaussian_filter1d(profile, 0.7)
    middle = len(smooth) // 2
    if smooth[middle] <= 0:
        return float("nan")
    smooth /= smooth[middle]
    slopes = []
    for edge in (smooth[middle::-1], smooth[middle:]):
        crossings = []
        for level in (0.8, 0.2):
            below = np.flatnonzero(edge <= level)
            if not below.size:
                return float("nan")
            index = below[0]
            crossing = index - 1 + (edge[index - 1] - level) / (edge[index - 1] - edge[index])
            crossings.append(crossing)
        transition_width = crossings[1] - crossings[0]
        if transition_width <= 0:
            return float("nan")
        slopes.append(0.6 / transition_width)
    return float(np.mean(slopes))


def _peak_position(image):
    """Quadratic subpixel refinement of a lightly smoothed intensity peak."""
    smooth = ndimage.gaussian_filter(image, 0.7)
    y, x = np.unravel_index(np.argmax(smooth), smooth.shape)
    position = [float(y), float(x)]
    for axis, index in enumerate((y, x)):
        if 0 < index < image.shape[axis] - 1:
            values = smooth[y - 1 : y + 2, x] if axis == 0 else smooth[y, x - 1 : x + 2]
            denominator = values[0] - 2 * values[1] + values[2]
            if denominator < 0:
                position[axis] += float(np.clip(0.5 * (values[0] - values[2]) / denominator, -0.5, 0.5))
    return np.asarray(position)


def measure_pinholes(patch, radius=12, threshold_sigma=6.0) -> Optional[PinholeMeasurement]:
    """Average isolated bright pinholes with complete, untruncated support.

    Entire windows (including interpolation margin) must fit inside the patch.
    Objects touching the patch edge, hot pixels, overlapping windows, saturated
    pinholes, and windows with appreciable signal on their border are excluded.
    Each accepted pinhole is background-subtracted, normalized and aligned to
    its subpixel peak before averaging, so brightness does not determine best.
    """
    source = np.asarray(patch)
    if source.ndim != 2 or radius < 3 or min(source.shape) < 2 * radius + 5:
        return None
    image = source.astype(np.float64)
    if not np.isfinite(image).all():
        return None
    smooth = ndimage.gaussian_filter(image, 1.0)
    residual = image - smooth
    noise = 1.4826 * np.median(np.abs(residual - np.median(residual)))
    signal = smooth - ndimage.gaussian_filter(image, max(radius, 3))
    dynamic_range = float(signal.max() - np.median(signal))
    if dynamic_range <= np.finfo(float).eps:
        return None
    threshold = max(threshold_sigma * noise, 0.03 * dynamic_range)
    labels, _ = ndimage.label(signal > threshold)
    candidates = []
    for label_id, region in enumerate(ndimage.find_objects(labels), 1):
        if region is None:
            continue
        ys, xs = region
        # Reject the whole object, even if its brightest pixel is inside the ROI.
        if ys.start == 0 or xs.start == 0 or ys.stop == image.shape[0] or xs.stop == image.shape[1]:
            continue
        mask = labels[region] == label_id
        if np.count_nonzero(mask) < 5:
            continue
        y, x = np.unravel_index(np.argmax(np.where(mask, signal[region], -np.inf)), mask.shape)
        candidates.append((y + ys.start, x + xs.start))
    if not candidates:
        return None
    candidates = np.asarray(candidates)
    distances = cKDTree(candidates).query(candidates, k=2)[0][:, 1]
    grid_y, grid_x = np.mgrid[-radius : radius + 1, -radius : radius + 1]
    stamps, centers = [], []
    saturation = np.iinfo(source.dtype).max if np.issubdtype(source.dtype, np.integer) else np.inf
    for (y, x), distance in zip(candidates, distances):
        margin = radius + 2
        if distance <= 2 * radius + 2:
            continue
        if y < margin or x < margin or y + margin >= image.shape[0] or x + margin >= image.shape[1]:
            continue
        window = image[y - margin : y + margin + 1, x - margin : x + margin + 1]
        if window.max() >= saturation:
            continue
        center = _peak_position(window) + (y - margin, x - margin)
        # Check refined coordinates too; interpolation must never use padding.
        if np.any(center - radius < 1) or np.any(center + radius >= np.asarray(image.shape) - 1):
            continue
        stamp = ndimage.map_coordinates(image, [center[0] + grid_y, center[1] + grid_x], order=1)
        border = np.concatenate((stamp[0], stamp[-1], stamp[1:-1, 0], stamp[1:-1, -1]))
        background = np.median(border)
        peak = stamp[radius, radius] - background
        if peak <= max(threshold, 0):
            continue
        if np.count_nonzero(window - background > 0.2 * (window.max() - background)) < 5:
            continue  # A hot pixel is not a spatially resolved pinhole.
        # A broad/clipped pinhole or another object reaches the window edge.
        # Smooth first so a single noisy border pixel cannot reject a good hole.
        edge_image = ndimage.gaussian_filter(stamp, 0.7)
        edge = np.concatenate((edge_image[0], edge_image[-1], edge_image[:, 0], edge_image[:, -1]))
        if np.max(edge - background) > max(0.10 * peak, 3 * noise):
            continue
        stamps.append((stamp - background) / peak)
        centers.append(center[::-1])
    if not stamps:
        return None
    average = np.mean(stamps, axis=0)
    horizontal, vertical = average[radius, :].copy(), average[:, radius].copy()
    fwhm_x, fwhm_y = profile_fwhm(horizontal), profile_fwhm(vertical)
    steepness_x, steepness_y = profile_edge_steepness(horizontal), profile_edge_steepness(vertical)
    if not np.isfinite([fwhm_x, fwhm_y, steepness_x, steepness_y]).all():
        return None
    return PinholeMeasurement(
        np.asarray(centers), average, horizontal, vertical, fwhm_x, fwhm_y, steepness_x, steepness_y
    )
