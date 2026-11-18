import numpy as np
import pytest
from scipy import ndimage

from control.core.sample_incidence_analysis import (
    FocusPeaks,
    assess_flatness,
    focus_peaks,
    incidence_from_planes,
    image_to_stage_matrix,
    preview,
    register_fields,
    separate_illumination,
    stage_field_size,
    stage_profiles,
    texture_scores,
)
from control.core.sample_incidence_calibration import dither_offsets


def overlapping_fields(size=128, center=(0.15, -0.12), radius=1.3):
    rng = np.random.default_rng(3)
    y, x = np.mgrid[: 3 * size, : 3 * size]
    specimen = np.exp(0.0015 * x - 0.001 * y + ndimage.gaussian_filter(rng.normal(size=x.shape), 1) * 0.6)
    positions = dither_offsets(round(size * 0.33))
    shifts = -positions
    yy, xx = np.mgrid[:size, :size]
    xn, yn = 2 * (xx + 0.5) / size - 1, 2 * (yy + 0.5) / size - 1
    illumination = np.exp(-((xn - center[0]) ** 2 + (yn - center[1]) ** 2) / radius**2)
    fields = [
        2000 * ndimage.map_coordinates(specimen, [yy - shift[1] + size, xx - shift[0] + size], order=1) * illumination
        for shift in shifts
    ]
    return fields, positions, shifts


def test_texture_score_is_brightness_normalized_and_dark_has_no_focus():
    rng = np.random.default_rng(5)
    image = 1000 * (1 + ndimage.gaussian_filter(rng.normal(size=(192, 192)), 1) * 0.25)
    np.testing.assert_allclose(texture_scores(image), texture_scores(image * 4 + 100, black_level=100), rtol=1e-4)
    np.testing.assert_array_equal(texture_scores(np.zeros((192, 192))), 0)
    sharp, blurred = texture_scores(image), texture_scores(ndimage.gaussian_filter(image, 2))
    assert np.mean(sharp) > 3 * np.mean(blurred)


def test_focus_curves_reject_unresolved_or_edge_maxima():
    z = np.linspace(-2, 2, 9)
    scores = np.broadcast_to(np.exp(-(((z - 0.2) / 0.8) ** 2))[:, None, None], (9, 3, 3)).copy()
    result = focus_peaks(z, scores)
    assert result.valid.all()
    np.testing.assert_allclose(result.height_um, 0.2, atol=0.08)
    assert not focus_peaks(z, np.ones_like(scores)).valid.any()
    assert not focus_peaks(z, np.broadcast_to(np.arange(9)[:, None, None], (9, 3, 3))).valid.any()


def test_registration_calibrates_image_sign_and_scale_from_sample_texture():
    fields, positions, shifts = overlapping_fields()
    measured, transform, error = register_fields(fields, positions)
    np.testing.assert_allclose(measured, shifts, atol=1)
    np.testing.assert_allclose(transform, -np.eye(2), atol=0.03)
    assert error < 1


def flatness_example(
    gradient=(0.0002, -0.0001),
    camera_fixed=False,
    inconsistent=False,
    camera_effect=None,
    centers_only=False,
    curved_sample=False,
):
    size = 128
    positions = dither_offsets(size * 0.33)
    local = np.array(
        [((c + 0.5) * size / 3 - size / 2, (r + 0.5) * size / 3 - size / 2) for r in range(3) for c in range(3)]
    )
    peaks = []
    for index, position in enumerate(positions):
        xy = local if camera_fixed else local + position
        slopes = np.asarray(gradient) * (1 if not inconsistent or index % 2 else -1)
        z = 10 + xy @ slopes
        if camera_effect is not None:
            z += np.asarray(camera_effect).ravel()
        if curved_sample:
            z += 0.001 * np.sum((local + position) ** 2, axis=1)
        valid = np.ones((3, 3), bool)
        if centers_only:
            valid[:] = False
            valid[1, 1] = True
        peaks.append(FocusPeaks(np.where(valid, z.reshape(3, 3), np.nan), np.ones((3, 3)) * 0.05, valid))
    return assess_flatness(positions, peaks, -np.eye(2), (size, size))


def test_flat_plane_passes_and_geometric_tilt_repeats_across_fields():
    result = flatness_example()
    assert result.accepted
    assert result.overlap_rms_um < 1e-6
    tilted = flatness_example((0.01, -0.004))
    assert not tilted.accepted
    assert tilted.direction_agreement > 0.99
    assert "Consistent geometric tilt" in tilted.diagnosis


def test_inconsistent_sample_defocus_fails_but_camera_fixed_focus_variation_is_ignored():
    inconsistent = flatness_example((0.02, -0.01), inconsistent=True)
    assert not inconsistent.accepted
    assert "holder tilt is unproven" in inconsistent.diagnosis
    optical = flatness_example((0.02, -0.01), camera_fixed=True)
    assert optical.accepted
    assert optical.overlap_rms_um < 1e-6
    assert np.ptp(optical.camera_focus_offsets_um) > 0.25


def test_symmetric_objective_field_curvature_does_not_block_a_flat_sample():
    yy, xx = np.mgrid[-1:2, -1:2]
    optical_bowl = 1.5 * (xx**2 + yy**2)
    result = flatness_example((0, 0), camera_effect=optical_bowl)
    assert result.accepted
    assert result.plane_span_um < 1e-6
    assert result.residual_rms_um < 1e-6
    assert result.overlap_rms_um < 1e-6
    np.testing.assert_allclose(result.camera_focus_offsets_um, optical_bowl, atol=1e-6)


def test_camera_field_curvature_cannot_hide_real_sample_tilt_or_sample_curvature():
    yy, xx = np.mgrid[-1:2, -1:2]
    optical_bowl = 1.5 * (xx**2 + yy**2)
    tilted = flatness_example((0.01, -0.004), camera_effect=optical_bowl)
    assert not tilted.accepted
    assert "Consistent geometric tilt" in tilted.diagnosis
    np.testing.assert_allclose(tilted.plane[:2], [0.01, -0.004], atol=1e-6)
    curved = flatness_example((0, 0), camera_effect=optical_bowl, curved_sample=True)
    assert not curved.accepted
    assert curved.residual_rms_um > 0.25


def test_unresolved_edge_focus_peaks_leave_distributed_center_flatness_usable():
    result = flatness_example(centers_only=True)
    assert result.accepted
    assert len(result.heights_um) == 9
    assert np.isnan(result.overlap_rms_um)
    assert "distributed central regions" in result.diagnosis
    assert np.isnan(result.local_slopes).all()
    curved = flatness_example((0, 0), centers_only=True, curved_sample=True)
    assert not curved.accepted
    assert curved.residual_rms_um > 0.25


def test_weaker_broader_edge_focus_curves_do_not_imply_tilt():
    z = np.linspace(-4, 4, 17)
    yy, xx = np.mgrid[-1:2, -1:2]
    radius_squared = xx**2 + yy**2
    width = 0.6 + 0.5 * radius_squared
    strength = 1e-3 * np.exp(-radius_squared)
    scores = strength[None, :, :] * np.exp(-0.5 * (z[:, None, None] / width[None, :, :]) ** 2)
    peaks = focus_peaks(z, scores)
    assert peaks.valid.all()
    np.testing.assert_allclose(peaks.height_um, 0, atol=1e-6)


def test_same_sample_overlap_cancels_fluorophore_gradient():
    fields, positions, shifts = overlapping_fields()
    result = separate_illumination(fields, shifts)
    # Specimen has a substantial independent low-frequency gradient. Recover
    # illumination center from identical specimen locations, not field averages.
    expected = (np.array([0.15, -0.12]) + 1) * 128 / 2 - 0.5
    np.testing.assert_allclose(result.center_px, expected, atol=1)
    assert result.sample_variation > 0.05
    assert result.overlap_residual < 0.03


def test_known_axial_beam_motion_yields_angle_and_flat_beam_does_not():
    results = []
    for delta in (-0.05, 0, 0.05):
        fields, _, shifts = overlapping_fields(center=(0.15 + delta, -0.12))
        results.append(separate_illumination(fields, shifts))
    result = incidence_from_planes(*results, 10, -np.eye(2))
    # Center changes 6.4 pixels. Empirical XY mapping is 1 pixel per µm.
    assert result.angle_deg == pytest.approx(np.degrees(np.arctan2(6.4, 10)), abs=0.5)
    np.testing.assert_allclose(result.lateral_shift_um, [6.4, 0], atol=0.1)
    fields, _, shifts = overlapping_fields(radius=100)
    flat = separate_illumination(fields, shifts)
    assert flat.center_px is None
    assert incidence_from_planes(flat, flat, flat, 10, -np.eye(2)).angle_deg is None


def test_preview_preserves_full_field_and_dither_is_bounded():
    assert preview(np.ones((1200, 2400))).shape == (256, 512)
    offsets = dither_offsets(0.45 * 100)
    assert np.max(np.linalg.norm(offsets, axis=1)) < 100
    assert len(offsets) == 9


@pytest.mark.parametrize("rotation", [0, 90, -90, 180])
@pytest.mark.parametrize("flip", [None, "Horizontal", "Vertical", "Both"])
@pytest.mark.parametrize("signs", [(1, 1, 1), (-1, -1, 1), (-1, 1, -1), (1, -1, -1)])
def test_stage_polarity_survives_motor_canonical_and_camera_transforms(rotation, flip, signs):
    from control.utils import FlipVariant, rotate_and_flip_image
    from squid.config import DirectionSign, get_stage_config

    config = get_stage_config().model_copy(deep=True)
    # Include nonzero canonical origins, opposite motor signs, and Z inversion.
    for axis, sign, motor in zip((config.X_AXIS, config.Y_AXIS, config.Z_AXIS), signs, (-1, 1, -1)):
        axis.CANONICAL_SIGN = DirectionSign(sign)
        axis.CANONICAL_ORIGIN_RAW_MM = 30.0
        axis.MOVEMENT_SIGN = DirectionSign(motor)
        axis.SCREW_PITCH = 0.1
        axis.MICROSTEPS_PER_STEP = 256

    def stage_reading(axis, raw_um):
        ticks = axis.convert_real_units_to_ustep(raw_um / 1000)
        return 1000 * axis.raw_to_canonical(axis.convert_to_real_units(ticks))

    fields, raw_positions, _ = overlapping_fields()
    positions = np.array(
        [[stage_reading(config.X_AXIS, x + 10000), stage_reading(config.Y_AXIS, y + 10000)] for x, y in raw_positions]
    )
    positions -= positions[0]

    def delivered(image):
        # Sensor reverse_x, then the shared software rotation/flip processing.
        return rotate_and_flip_image(image[:, ::-1], rotation, None if flip is None else FlipVariant(flip))

    images = [delivered(field) for field in fields]
    _, transform, _ = register_fields(images, positions)
    yy, xx = np.mgrid[:128, :128]
    native_stage_x = signs[0] * (xx + 0.5 - 64)
    native_stage_y = signs[1] * (yy + 0.5 - 64)
    # Probe actual corresponding pixel locations after every camera transform.
    expected_x, expected_y = delivered(native_stage_x), delivered(native_stage_y)
    yy, xx = np.mgrid[:128, :128]
    recovered = np.stack((xx + 0.5 - 64, yy + 0.5 - 64), axis=-1) @ image_to_stage_matrix(transform).T
    np.testing.assert_allclose(recovered[..., 0], expected_x, atol=1.5)
    np.testing.assert_allclose(recovered[..., 1], expected_y, atol=1.5)

    local = np.array([((c + 0.5) * 128 / 3 - 64, (r + 0.5) * 128 / 3 - 64) for r in range(3) for c in range(3)])
    # Known focus-motor slopes: increasing stage X needs lower Z; Y higher Z.
    gradient = np.array([-0.008, 0.003])
    peaks = []
    for position in positions:
        heights = 2000 + (position + local * signs[:2]) @ gradient
        # Convert a physical raw height through motor ticks and the configured
        # canonical Z frame, just as CephlaStage.get_pos does.
        raw_heights = [1000 * config.Z_AXIS.canonical_to_raw(z / 1000) for z in heights]
        read_heights = np.array([stage_reading(config.Z_AXIS, z) for z in raw_heights]).reshape(3, 3)
        peaks.append(FocusPeaks(delivered(read_heights), np.full((3, 3), 0.05), np.ones((3, 3), bool)))
    result = assess_flatness(positions, peaks, transform, images[0].shape)
    np.testing.assert_allclose(result.plane[:2], gradient, atol=4e-5)
    np.testing.assert_allclose(result.local_slopes, np.tile(gradient, (9, 1)), atol=4e-5)
    assert result.direction_agreement > 0.99


def test_rotated_rectangular_field_profiles_follow_stage_axes():
    # Native pixels point down stage Y and left stage X: a 90-degree camera.
    transform = np.array([[0, 2], [-2, 0]])
    h, w = 60, 120
    yy, xx = np.mgrid[:h, :w]
    coordinates = np.stack((xx + 0.5 - w / 2, yy + 0.5 - h / 2), axis=-1) @ image_to_stage_matrix(transform).T
    image = 100 + 2 * coordinates[..., 0] + 3 * coordinates[..., 1]
    np.testing.assert_allclose(stage_field_size(image.shape, transform), [30, 60])
    for axis, (distances, values) in enumerate(stage_profiles(image, transform)):
        physical_distance = distances / 100 * [30, 60][axis]
        np.testing.assert_allclose(values, 100 + [2, 3][axis] * physical_distance, atol=1e-9)
        assert values[-1] > values[0]


def test_measured_corner_focus_can_be_lower_despite_positive_corrected_sample_slopes():
    yy, xx = np.mgrid[-1:2, -1:2]
    optical_effect = -1.5 * xx + 0.5 * yy
    result = flatness_example((0.01, 0.004), camera_effect=optical_effect)
    assert np.all(result.plane[:2] > 0)
    # Camera top right is +X, -Y in this example. Its optical focus offset is
    # large enough to reverse the total corner-versus-center focus difference.
    assert result.camera_focus_deltas_um[0, 2] < 0
    assert result.camera_focus_deltas_um[1, 1] == 0
    assert result.camera_focus_deltas_um[0, 2] == pytest.approx(128 / 3 * (0.01 - 0.004) - 2)
