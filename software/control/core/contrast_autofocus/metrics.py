"""The phase-1 E discriminator; the existing sharpness metrics live in control.utils."""

import numpy as np


def grayscale(image):
    """Use the RGB-to-gray convention of the existing focus-measure dispatch."""
    x = np.asarray(image)
    if x.ndim == 3 and x.shape[2] == 3:
        x = x[..., 0] * 0.299 + x[..., 1] * 0.587 + x[..., 2] * 0.114
    if x.ndim != 2 or not x.size or not np.all(np.isfinite(x)):
        raise ValueError("Autofocus requires a finite nonempty grayscale or BGR frame")
    return x.astype(np.float64, copy=False)


def usable_image(image):
    x = grayscale(image)
    return x.size >= 9 and min(x.shape) >= 3 and float(np.ptp(x)) > 0


def energy_factor(image, *, threshold=0.5, sensor_full_scale):
    """Sum FFT amplitudes whose frame-normalized log amplitude exceeds T.

    T is an amplitude threshold, not a spatial-frequency cutoff. A positive
    constant image has DC energy; callers separately reject untextured frames.
    """
    if not np.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("E threshold must lie in [0, 1]")
    if not np.isfinite(sensor_full_scale) or sensor_full_scale <= 0:
        raise ValueError("sensor_full_scale must be finite and positive")
    x = grayscale(image) / sensor_full_scale
    amplitude = np.abs(np.fft.fft2(x))
    log_amplitude = np.log1p(amplitude)
    span = float(np.ptp(log_amplitude))
    if not np.isfinite(span) or span == 0:
        return 0.0
    mask = (log_amplitude - log_amplitude.min()) / span >= threshold
    result = float(np.sum(amplitude[mask], dtype=np.float64))
    if not np.isfinite(result):
        raise ValueError("E overflowed")
    return result
