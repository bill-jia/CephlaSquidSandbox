"""Synthetic optical targets exercise clipping, alignment and sharpness ranking."""

import numpy as np
import pytest

from control.core.spinning_disk_alignment import (
    camera_patch,
    measure_pinholes,
    profile_edge_steepness,
    profile_fwhm,
)


CENTERS = [(85.3, 90.2), (85.7, 165.5), (165.2, 90.6), (165.6, 165.3)]


def pinholes(centers=CENTERS, sigma=2.0, amplitude=1000, background=100, shape=(256, 256)):
    y, x = np.mgrid[: shape[0], : shape[1]]
    image = np.full(shape, float(background))
    for cy, cx in centers:
        image += amplitude * np.exp(-((x - cx) ** 2 + (y - cy) ** 2) / (2 * sigma**2))
    return image


def test_average_subpixel_alignment_normalization_and_width():
    result = measure_pinholes(pinholes())
    assert len(result.centers) == 4
    assert result.average.shape == (25, 25)
    assert np.unravel_index(np.argmax(result.average), result.average.shape) == (12, 12)
    assert result.average[12, 12] == pytest.approx(1)
    assert result.fwhm_x == pytest.approx(2.355 * 2, abs=0.25)
    assert result.fwhm_y == pytest.approx(2.355 * 2, abs=0.25)
    np.testing.assert_allclose(result.centers[:, ::-1], sorted(CENTERS), atol=0.05)


@pytest.mark.parametrize("center", [(0, 35), (255, 35), (35, 0), (35, 255), (0, 0), (255, 255)])
def test_objects_clipped_by_any_patch_edge_do_not_change_average(center):
    baseline = measure_pinholes(pinholes())
    # Bright, broad partial pinholes must not dominate the normalized average.
    clipped = pinholes([center], sigma=5, amplitude=15000, background=0)
    result = measure_pinholes(pinholes() + clipped)
    assert len(result.centers) == 4
    np.testing.assert_allclose(result.average, baseline.average, atol=1e-8)


@pytest.mark.parametrize("center", [(10, 35), (245, 35), (35, 10), (35, 245)])
def test_peak_inside_patch_but_extraction_window_clipped_is_excluded(center):
    baseline = measure_pinholes(pinholes())
    result = measure_pinholes(pinholes() + pinholes([center], background=0))
    assert len(result.centers) == 4
    np.testing.assert_allclose(result.average, baseline.average, atol=1e-8)


def test_signal_reaching_extraction_border_is_excluded():
    assert measure_pinholes(pinholes([(128, 128)], sigma=9), radius=12) is None
    assert measure_pinholes(pinholes([(128, 128)], sigma=9), radius=35) is not None


def test_blur_decreases_edge_steepness_and_brightness_does_not_change_ranking():
    sharp = measure_pinholes(pinholes(sigma=2))
    blurred = measure_pinholes(pinholes(sigma=3.5))
    brighter = measure_pinholes(pinholes(sigma=2, amplitude=5000, background=700))
    assert blurred.width > sharp.width * 1.5
    assert sharp.sharpness > blurred.sharpness * 1.5
    assert brighter.sharpness == pytest.approx(sharp.sharpness)
    np.testing.assert_allclose(brighter.average, sharp.average, atol=1e-8)


def resolved_pinholes(edge_width=0.6, hole_radius=6, amplitude=1000, background=100):
    """Resolved circular apertures with separately controlled size and edge blur."""
    from scipy.special import expit

    y, x = np.mgrid[:256, :256]
    image = np.full((256, 256), float(background))
    for cy, cx in CENTERS:
        distance = np.hypot(x - cx, y - cy)
        image += amplitude * expit((hole_radius - distance) / edge_width)
    return image


def test_edge_metric_prefers_wider_sharp_aperture_over_narrow_blurred_aperture():
    narrow_blurred = measure_pinholes(resolved_pinholes(edge_width=1.0, hole_radius=3))
    wider_sharp = measure_pinholes(resolved_pinholes(edge_width=0.4, hole_radius=6))
    assert wider_sharp.width > narrow_blurred.width
    assert wider_sharp.sharpness > narrow_blurred.sharpness


def test_profile_edge_slope_is_independent_of_flat_top_width_and_rejects_incomplete_edges():
    from scipy.special import expit

    x = np.arange(-40, 41, dtype=float)
    small = expit((8 - np.abs(x)) / 0.7)
    large = expit((16 - np.abs(x)) / 0.7)
    assert profile_fwhm(large) > 1.9 * profile_fwhm(small)
    assert profile_edge_steepness(small) == pytest.approx(profile_edge_steepness(large), rel=0.001)
    assert np.isnan(profile_edge_steepness(np.ones(25)))
    assert np.isnan(profile_edge_steepness(np.zeros(25)))
    clipped = large.copy()
    clipped[x > 0] = 1
    assert np.isnan(profile_edge_steepness(clipped))


def test_empty_noise_hot_pixels_and_saturation_are_excluded():
    assert measure_pinholes(np.zeros((256, 256))) is None
    noise = np.random.default_rng(4).normal(100, 2, (256, 256))
    assert measure_pinholes(noise) is None
    noise[80, 80] = noise[150, 150] = noise[80, 150] = 30000
    assert measure_pinholes(noise) is None
    saturated = np.clip(pinholes(amplitude=100000), 0, 65535).astype(np.uint16)
    assert measure_pinholes(saturated) is None
    invalid = pinholes()
    invalid[0, 0] = np.nan
    assert measure_pinholes(invalid) is None


def test_close_neighbors_are_excluded_from_average():
    image = pinholes() + pinholes([(40, 40), (40, 55)], background=0)
    result = measure_pinholes(image)
    assert len(result.centers) == 4


def test_native_crop_is_independent_clamps_offsets_and_handles_small_rgb():
    image = np.arange(600 * 800, dtype=np.int32).reshape(600, 800)
    patch = camera_patch(image)
    np.testing.assert_array_equal(patch, image[44:556, 144:656])
    assert not np.shares_memory(patch, image)
    np.testing.assert_array_equal(camera_patch(image, offset=(10000, -10000)), image[:512, -512:])
    rgb = np.ones((64, 80, 3), dtype=np.uint16) * [10, 20, 30]
    np.testing.assert_array_equal(camera_patch(rgb), np.full((64, 80), 20))
