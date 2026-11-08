import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from control.core.sample_bead_geometry import correct_points, uncorrect_points, _tracks
from control.core.sample_bead_analysis import register_beads, assess_bead_flatness, bead_illumination
from control.core.sample_incidence_analysis import incidence_from_planes
from control.core.sample_incidence_calibration import dither_offsets


def test_recorded_radial_distortion_connects_field8_through_neighbors():
    data = json.loads((Path(__file__).parent / "data" / "radial_bead_registration.json").read_text())
    shape = tuple(data["shape"])
    images = [np.zeros(shape, np.float32)] * len(data["fields"])
    fields = [
        SimpleNamespace(
            **{k: np.asarray(v) for k, v in f.items()}, valid=np.ones(len(f["points_px"]), bool), projection=images[i]
        )
        for i, f in enumerate(data["fields"])
    ]
    details = {}
    shifts, transform, error = register_beads(fields, data["positions_um"], images, diagnostics=details)
    model = details["distortion"]
    assert model["kind"] == "radial_center"
    assert model["k1"] == pytest.approx(-0.007017, abs=0.0001)
    np.testing.assert_allclose(model["center_offset"], [-19.15 / 1200, 18.43 / 1200], atol=0.003)
    assert details["track_rms_px"] < 0.03
    assert error < 1
    field8 = next(f for f in details["fields"] if f["field"] == 8)
    assert 1 not in field8["neighbors"]
    assert len(field8["neighbors"]) >= 2
    flat = assess_bead_flatness(shifts @ np.linalg.inv(transform).T, fields, shifts, transform, distortion=model)
    assert not flat.accepted  # Registration success must not bypass flatness.
    assert flat.plane_span_um == pytest.approx(2.47, abs=0.04)
    assert flat.residual_rms_um < 0.1


def synthetic_catalogs(z=0):
    rng = np.random.default_rng(713)
    world = rng.uniform(-430, 430, (180, 2))
    brightness = rng.uniform(0.3, 2.5, len(world))
    positions = dither_offsets(140)
    model = dict(k1=-0.007, center_offset=np.array([-0.016, 0.015]), kind="radial_center")
    fields = []
    for position in positions:
        ideal = world - position + 255.5
        raw = uncorrect_points(ideal, (512, 512), model)
        keep = np.all((raw > 12) & (raw < 500), axis=1)
        uv = 2 * (ideal[keep] + 0.5) / 512 - 1
        center = np.array([0.1 + 0.0005 * z, -0.05])
        flux = brightness[keep] * 1000 * np.exp(-np.sum((uv - center) ** 2, axis=1) / 1.5)
        camera = 2 * (raw[keep] + 0.5) / 512 - 1
        heights = 1 + world[keep] @ [0.0001, -0.00015] + camera @ [0.3, 0.15] + 0.2 * camera[:, 0] ** 2
        fields.append(
            SimpleNamespace(
                points_px=raw[keep],
                flux=flux,
                valid=np.ones(keep.sum(), bool),
                projection=np.zeros((512, 512)),
                height_um=heights,
                uncertainty_um=np.full(keep.sum(), 0.03),
            )
        )
    return fields, positions, model


def test_radial_geometry_preserves_sample_plane_and_incidence_angle_with_fixed_calibration():
    fields, positions, truth = synthetic_catalogs()
    details = {}
    images = [f.projection for f in fields]
    shifts, transform, _ = register_beads(fields, positions, images, diagnostics=details)
    model = details["distortion"]
    assert model["k1"] == pytest.approx(truth["k1"], abs=2e-5)
    np.testing.assert_allclose(model["center_offset"], truth["center_offset"], atol=0.001)
    np.testing.assert_allclose(transform, -np.eye(2), atol=0.0001)
    flat = assess_bead_flatness(shifts @ np.linalg.inv(transform).T, fields, shifts, transform, distortion=model)
    assert flat.accepted
    np.testing.assert_allclose(flat.plane[:2], [0.0001, -0.00015], atol=1e-7)
    fits = []
    for z in (-2, 0, 2):
        current, _, _ = synthetic_catalogs(z)
        saved = {}
        sh, measured, _ = register_beads(current, positions, images, diagnostics=saved, distortion=model)
        assert "distortion_validation" not in saved  # No refitting at each Z.
        np.testing.assert_array_equal(saved["distortion"]["center_offset"], model["center_offset"])
        fit = bead_illumination(current, sh, (512, 512), distortion=model)
        np.testing.assert_allclose(fit.center_px, (np.array([0.1 + 0.0005 * z, -0.05]) + 1) * 256 - 0.5, atol=0.005)
        fits.append(fit)
    angle = incidence_from_planes(*fits, 4, transform)
    assert angle.angle_deg == pytest.approx(np.degrees(np.arctan(0.128)), abs=0.005)


def test_radial_forward_inverse_preserves_native_aperture_positions():
    model = dict(k1=-0.007, center_offset=np.array([-0.016, 0.015]))
    points = np.array([[0, 0], [2399, 2399], [1200, 1200], [100, 2300]], float)
    corrected = correct_points(points, (2400, 2400), model)
    np.testing.assert_allclose(uncorrect_points(corrected, (2400, 2400), model), points, atol=1e-7)
    scaled = (points + 0.5) * 512 / 2400 - 0.5
    np.testing.assert_allclose(correct_points(scaled, (512, 512), model), (corrected + 0.5) * 512 / 2400 - 0.5)


def test_conflicting_identity_cycles_and_disconnected_fields_are_rejected():
    points = np.array([[0, 0], [20, 2], [5, 30], [40, 50]], float)
    edges = [
        dict(i=0, j=1, a=np.arange(4), b=np.arange(4)),
        dict(i=1, j=2, a=np.arange(4), b=np.arange(4)),
        dict(i=0, j=2, a=np.arange(4), b=np.array([1, 0, 2, 3])),
    ]
    with pytest.raises(ValueError, match="Conflicting bead identities"):
        _tracks([points] * 3, edges, (512, 512))
    with pytest.raises(ValueError, match="Disconnected bead fields 3"):
        _tracks([points] * 3, edges[:1], (512, 512))


def test_unphysical_frozen_distortion_is_rejected():
    fields, positions, model = synthetic_catalogs()
    model["k1"] = -0.3
    with pytest.raises(ValueError, match="distortion"):
        register_beads(fields, positions, [f.projection for f in fields], distortion=model)


def test_field_specific_geometry_change_cannot_be_absorbed_by_shared_radial_fit():
    fields, positions, _ = synthetic_catalogs()
    points = fields[4].points_px
    fields[4].points_px = 255.5 + (points - 255.5) * [1.04, 0.97]
    with pytest.raises(ValueError):
        register_beads(fields, positions, [f.projection for f in fields], stage_rms_limit_px=2)


def test_geometry_fit_honors_cancellation_between_overlap_pairs():
    from control.core.sample_incidence_calibration import CalibrationCancelled

    fields, positions, _ = synthetic_catalogs()

    def cancel():
        raise CalibrationCancelled()

    with pytest.raises(CalibrationCancelled):
        register_beads(fields, positions, [f.projection for f in fields], check_cancelled=cancel)
