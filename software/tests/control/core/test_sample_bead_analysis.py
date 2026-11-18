from dataclasses import replace
import json
from types import SimpleNamespace
from pathlib import Path

import numpy as np
import pytest

from control.core.sample_bead_analysis import (
    BeadMeasurements,
    assess_bead_flatness,
    bead_focus,
    bead_illumination,
    bead_reference_ratio,
    bead_translation,
    detect_beads,
    measure_beads,
    register_beads,
)
from control.core.sample_incidence_calibration import AlignmentSettings, SampleIncidenceSession, dither_offsets
from control.core.sample_incidence_analysis import analysis_pixel_scale, preview, incidence_from_planes
from tests.control.core.test_sample_incidence_calibration import hardware


def test_sparse_recording_recovers_bead_shifts_but_preserves_stage_consistency_gate():
    data = json.loads((Path(__file__).parent / "data" / "sparse_bead_registration.json").read_text())
    fields = [SimpleNamespace(points_px=np.asarray(p), valid=np.ones(len(p), bool)) for p in data["points"]]
    details = {}
    # Empty images deliberately cannot provide a texture-registration seed.
    images = [np.zeros((512, 512), np.float32)] * len(fields)
    with pytest.raises(ValueError, match=r"stage travel: RMS .*limit 1\.00"):
        register_beads(fields, data["positions_um"], images, diagnostics=details)
    # The legacy replay measured distorted coordinates. Correcting radial scale
    # changes these shifts by about one pixel, while preserving bead identities.
    np.testing.assert_allclose(details["shifts"], data["expected_shifts_px"], atol=1.5)
    assert details["fields"][0]["matches"] >= 10
    assert details["fields"][2]["matches"] >= 6
    assert details["error_px"] == pytest.approx(1.34, abs=0.02)
    assert details["distortion"]["k1"] == pytest.approx(-0.00704, abs=0.0001)
    assert details["track_rms_px"] < 0.03
    assert details["limit_px"] == 1


@pytest.mark.parametrize("pixel_scale", [0.5, 1, 2, 4.6875])
@pytest.mark.parametrize("limit", [1, 1.5, 2])
def test_adjustable_stage_limit_accepts_recorded_xy_mismatch_independent_of_resolution(pixel_scale, limit):
    from control.core.sample_bead_calibration import _register

    data = json.loads((Path(__file__).parent / "data" / "sparse_bead_registration.json").read_text())
    fields = [
        SimpleNamespace(points_px=(np.asarray(p) + 0.5) * pixel_scale - 0.5, valid=np.ones(len(p), bool))
        for p in data["points"]
    ]
    records = []
    session = SimpleNamespace(
        settings=AlignmentSettings(stage_rms_limit_px=limit), _record=lambda kind, **kw: records.append(kw)
    )
    images = [np.zeros((round(512 * pixel_scale),) * 2, np.float32)] * len(fields)
    if limit == 1:
        with pytest.raises(ValueError, match="stage travel"):
            _register(session, fields, data["positions_um"], images, pixel_scale, "registration")
    else:
        shifts, transform, rms = _register(session, fields, data["positions_um"], images, pixel_scale, "registration")
        assert 1 < rms / pixel_scale < limit
    assert records[0]["limit_px"] == limit * pixel_scale
    assert records[0]["stage_rms_limit_px"] == limit
    assert records[0]["reference_error_px"] == pytest.approx(1.34, abs=0.02)


@pytest.mark.parametrize("pixel_scale", [0.5, 1, 2, 4.6875])
def test_direct_bead_registration_handles_rotation_reflection_and_background(pixel_scale):
    rng = np.random.default_rng(73)
    points = rng.uniform(50, 350, (25, 2))
    positions = dither_offsets(35)
    transform = np.array([[0.2, -0.9], [-1.1, -0.1]]) * pixel_scale
    fields = []
    for i, position in enumerate(positions):
        # Missing beads, unrelated candidates, and shuffled order in each field.
        selected = rng.choice(len(points), 15, replace=False)
        xy = points[selected] * pixel_scale + transform @ position
        xy = np.vstack((xy, rng.uniform(220, 400, (5, 2)) * pixel_scale))
        rng.shuffle(xy)
        fields.append(SimpleNamespace(points_px=xy, valid=np.ones(len(xy), bool)))
    images = [np.ones((round(512 * pixel_scale),) * 2, np.float32)] * 9
    shifts, measured, error = register_beads(fields, positions, images, pixel_scale=pixel_scale)
    np.testing.assert_allclose(measured, transform, atol=1e-10)
    np.testing.assert_allclose(shifts, positions @ transform.T, atol=1e-10)
    assert error < 1e-10


def test_bead_registration_rejects_ambiguous_and_insufficient_patterns():
    first = np.array([[0, 0], [10, 2], [3, 17], [20, 24]], float)
    duplicated = np.vstack((first + [40, 50], first + [100, 70]))
    with pytest.raises(ValueError, match="Ambiguous.*4 and 4"):
        bead_translation(first, duplicated)
    with pytest.raises(ValueError, match="Too few shared beads"):
        bead_translation(first, first[:3])
    rng = np.random.default_rng(8)
    with pytest.raises(ValueError, match="Too few shared beads"):
        bead_translation(rng.uniform(0, 512, (12, 2)), rng.uniform(0, 512, (12, 2)))


def test_bead_registration_failure_records_diagnostics():
    from control.core.sample_bead_calibration import _register

    rng = np.random.default_rng(92)
    reference = rng.uniform(30, 300, (15, 2))
    positions = dither_offsets(25)
    fields = [SimpleNamespace(points_px=reference - p, valid=np.ones(len(reference), bool)) for p in positions]
    fields[3].points_px += [12, 0]
    records = []
    session = SimpleNamespace(settings=AlignmentSettings(), _record=lambda kind, **kw: records.append((kind, kw)))
    with pytest.raises(ValueError, match="stage travel"):
        _register(session, fields, positions, [np.zeros((32, 32))] * 9, 1, "flatness_registration")
    kind, details = records[0]
    assert kind == "flatness_registration" and "stage travel" in details["error"]
    assert details["error_px"] > details["limit_px"]
    assert len(details["fields"]) == 8


def specimen():
    rng = np.random.default_rng(44)
    y, x = np.mgrid[-260:261:36, -260:261:36]
    points = np.column_stack((x.ravel(), y.ravel())) + rng.uniform(-5, 5, (x.size, 2))
    return points, rng.uniform(0.5, 2, len(points))


def bead_image(position=(0, 0), z=0, size=256, camera_effect=True, gain=1, focus_offset=0):
    world, amount = specimen()
    pixels = world - position + size / 2 - 0.5
    image = np.full((size, size), 100.0, np.float32)
    yy, xx = np.mgrid[-12:13, -12:13]
    for world_xy, (x, y), brightness in zip(world, pixels, amount):
        if x < 13 or y < 13 or x >= size - 13 or y >= size - 13:
            continue
        u, v = 2 * (np.array([x, y]) + 0.5) / size - 1
        focus = world_xy @ [0.0002, -0.0001] + focus_offset
        if camera_effect:
            focus += 0.3 * u + 0.15 * v + 0.2 * (u * u + v * v)
        sigma = np.sqrt(1 + (z - focus) ** 2)
        illumination = np.exp(-((u - (0.12 + 0.003 * z)) ** 2 + (v + 0.1) ** 2) / 1.5)
        ix, iy = int(round(x)), int(round(y))
        patch = (
            4000
            * brightness
            * gain
            * illumination
            / sigma**2
            * np.exp(-((xx + ix - x) ** 2 + (yy + iy - y) ** 2) / (2 * sigma**2))
        )
        image[iy - 12 : iy + 13, ix - 12 : ix + 13] += patch
    return image


def test_bead_focus_is_resistant_to_brightness_fluctuation_and_rejects_bad_peaks():
    z = np.linspace(-2, 2, 17)
    images = [bead_image(z=v) for v in z]
    baseline = bead_focus(z, images, radius=7)
    flicker = [100 + (image - 100) * (2 if i == 6 else 0.8) for i, image in enumerate(images)]
    result = bead_focus(z, flicker, radius=7)
    assert result.valid.sum() > 20
    np.testing.assert_allclose(result.height_um[result.valid], baseline.height_um[baseline.valid], atol=0.015)
    with pytest.raises(ValueError, match="focus peaks"):
        bead_focus(z, [images[8]] * len(z), radius=7)
    with pytest.raises(ValueError, match="isolated beads"):
        bead_focus(z, [np.ones((128, 128)) * 100] * len(z), radius=7)
    with pytest.raises(ValueError, match="distinct"):
        bead_focus(np.zeros(len(z)), images, radius=7)


def test_hot_pixels_saturation_and_overlapping_beads_are_not_valid_focus():
    im = np.ones((128, 128), np.uint16) * 100
    im[60, 60] = 65535
    assert len(detect_beads(im, radius=7)) == 0
    image = bead_image().astype(np.uint16)
    points = detect_beads(image, radius=7)
    assert len(points) > 20
    x, y = np.rint(points[0]).astype(int)
    image[y, x] = 65535
    m = measure_beads(image, points, radius=7)
    assert m.saturated[0] and not m.valid[0]


def test_lower_signal_threshold_admits_dim_resolved_beads():
    yy, xx = np.mgrid[:128, :128]
    image = np.full((128, 128), 100.0)
    for x, y in ((32, 32), (96, 32), (32, 96), (96, 96)):
        image += 7 * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * 1.3**2))
    assert len(detect_beads(image, radius=7)) == 0
    points = detect_beads(image, radius=7, min_snr=3)
    assert len(points) == 4
    assert not measure_beads(image, points, radius=7).valid.any()
    assert measure_beads(image, points, radius=7, min_snr=3).valid.all()


@pytest.mark.parametrize("maximum", [256, 512, 1024, 0])
def test_resolution_preserves_native_photometry_registration_and_incidence(maximum):
    positions = dither_offsets(40)
    scale = min(640, maximum or 640) / 640
    pixel_scale = analysis_pixel_scale((640, 640), maximum)
    fits = []
    for z in (-1, 0, 1):
        images = [bead_image(p, z, size=640, camera_effect=False) for p in positions]
        fields = []
        for image in images:
            points = detect_beads(image, radius=7)
            measured = measure_beads(image, points, radius=7, maximum=maximum)
            native = measure_beads(image, points, radius=7, maximum=0)
            np.testing.assert_array_equal(measured.flux, native.flux)
            np.testing.assert_array_equal(measured.valid, native.valid)
            np.testing.assert_allclose(measured.points_px + 0.5, (native.points_px + 0.5) * scale)
            fields.append(measured)
        small = [preview(image, maximum) for image in images]
        shifts, transform, error = register_beads(fields, positions, small, maximum, pixel_scale)
        np.testing.assert_allclose(transform, -np.eye(2) * scale, atol=0.005)
        fits.append(bead_illumination(fields, shifts, small[0].shape, pixel_scale))
    result = incidence_from_planes(*fits, 2, transform, pixel_scale)
    # Synthetic beam center moves 0.003 * half-FOV = 0.96 um per um in Z.
    assert result.angle_deg == pytest.approx(np.degrees(np.arctan(0.96)), abs=0.5)


def test_focus_resolution_does_not_change_native_bead_heights():
    z = np.linspace(-2, 2, 17)
    images = [bead_image(z=v, size=640) for v in z]
    coarse = bead_focus(z, images, radius=7, maximum=256)
    native = bead_focus(z, images, radius=7, maximum=0)
    assert coarse.projection.shape == (256, 256)
    assert native.projection.shape == (640, 640)
    np.testing.assert_array_equal(coarse.valid, native.valid)
    np.testing.assert_allclose(coarse.height_um, native.height_um)


def test_dither_separates_bead_surface_from_camera_focus_and_checks_overlaps():
    positions = dither_offsets(40)
    z = np.linspace(-2, 2, 17)
    scans = [bead_focus(z, [bead_image(p, v) for v in z], radius=7) for p in positions]
    shifts, t, error = register_beads(scans, positions, [s.projection for s in scans])
    np.testing.assert_allclose(t, -np.eye(2), atol=0.01)
    flat = assess_bead_flatness(positions, scans, shifts, t, max_span=0.2, max_residual=0.1)
    assert flat.accepted
    np.testing.assert_allclose(flat.plane[:2], [0.0002, -0.0001], atol=0.0001)
    assert np.ptp(flat.camera_focus_offsets_um) > 0.4
    assert flat.overlap_rms_um < 0.03
    broken = list(scans)
    broken[3] = replace(scans[3], height_um=scans[3].height_um + 1)
    assert not assess_bead_flatness(positions, broken, shifts, t, max_span=0.2, max_residual=0.1).accepted


def illumination_catalogs(center=(0.12, -0.1)):
    world, amount = specimen()
    positions = dither_offsets(40)
    fields = []
    for p in positions:
        pixels = world - p + 127.5
        keep = np.all((pixels > 12) & (pixels < 244), axis=1)
        xy = pixels[keep]
        uv = 2 * (xy + 0.5) / 256 - 1
        flux = amount[keep] * 1000 * np.exp(-np.sum((uv - center) ** 2, axis=1) / 1.5)
        fields.append(
            BeadMeasurements(xy, xy, flux, np.ones(len(xy)), flux, np.ones(len(xy), bool), np.zeros(len(xy), bool))
        )
    return fields, -positions


def test_same_bead_flux_recovers_illumination_despite_unequal_bead_brightness():
    fields, shifts = illumination_catalogs()
    fit = bead_illumination(fields, shifts, (256, 256))
    np.testing.assert_allclose(fit.center_px, (np.array([0.12, -0.1]) + 1) * 128 - 0.5, atol=0.02)
    assert fit.center_uncertainty_px < 0.02
    assert fit.overlap_residual < 0.001
    changed = list(fields)
    changed[3] = replace(fields[3], flux=fields[3].flux * 2)
    with pytest.raises(ValueError, match="inconsistent"):
        bead_illumination(changed, shifts, (256, 256))
    assert bead_reference_ratio(fields[0], replace(fields[0], flux=fields[0].flux * 1.02)) == pytest.approx(1.02)
    with pytest.raises(ValueError, match="coverage"):
        bead_illumination([replace(f, valid=np.arange(len(f.valid)) < 1) for f in fields], shifts, (256, 256))


@pytest.mark.parametrize("size, maximum", [(256, 512), (640, 256), (640, 0)])
def test_manual_bead_steps_restore_hardware_switch_modes_and_gate_stale_results(tmp_path, size, maximum):
    from control.core.sample_incidence_recording import AlignmentRecorder

    hw, light, _ = hardware(initialized=False)
    state = SimpleNamespace(confocal_mode=True, is_waveform_driven=False)
    hw.scope.obs_controller.current_observation_state = state

    def frame():
        pos = hw.stage.get_pos()
        image = bead_image(
            np.array([pos.x_mm - 10, pos.y_mm - 11]) * 1000,
            (pos.z_mm - 2) * 1000,
            size=size,
            camera_effect=False,
            focus_offset=0 if state.confocal_mode else 0.3,
        )
        assert light.on
        return SimpleNamespace(frame_id=hw.camera.frame_id, frame=image)

    hw.camera.read_camera_frame = frame
    flats, angles = [], []
    settings = AlignmentSettings(
        sample_kind="beads",
        bead_radius_px=7,
        focus_steps=17,
        dither_fraction=40 / size,
        analysis_maximum=maximum,
        settle_s=0,
        repeat=False,
        max_plane_span_um=0.3,
    )
    first = SampleIncidenceSession(
        hw,
        settings,
        1,
        flatness_ready=flats.append,
        incidence_ready=angles.append,
        phase="flatness",
        recorder=AlignmentRecorder(tmp_path),
    )
    first.run()
    assert len(flats) == 1 and flats[0].accepted and not angles
    assert first.calibration["scans"][0].projection.shape == (min(size, maximum or size),) * 2
    assert hw.live.is_live and not light.on
    np.testing.assert_allclose(hw.stage.xyz, [10, 11, 2])
    second = SampleIncidenceSession(
        hw,
        settings,
        1,
        incidence_ready=angles.append,
        phase="incidence",
        calibration=first.calibration,
        recorder=AlignmentRecorder(tmp_path),
    )
    with pytest.raises(ValueError, match="widefield"):
        second.run()
    state.confocal_mode = False
    for changed in (
        replace(settings, analysis_maximum=1024),
        replace(settings, bead_min_snr=5),
        replace(settings, stage_rms_limit_px=1.5),
    ):
        second.settings = changed
        second.recorder = None
        with pytest.raises(ValueError, match="measure flatness again"):
            second.run()
    second.settings = settings
    second.recorder = AlignmentRecorder(tmp_path)
    second.run()
    assert len(angles) == 1
    assert angles[0].angle_deg is not None
    events = [json.loads(line) for line in (second.recorder.path / "events.jsonl").read_text().splitlines()]
    assert any(event["kind"] == "flatness_reference" for event in events)
    assert any(event["kind"] == "widefield_focus_transfer" for event in events)
    assert json.loads((second.recorder.path / "session.json").read_text())["status"] == "completed"
    assert hw.live.is_live and not light.on
    np.testing.assert_allclose(hw.stage.xyz, [10, 11, 2])
    hw.stage.xyz[0] += 0.01
    second.recorder = None
    with pytest.raises(ValueError, match="Stage XY moved"):
        second.run()
    assert hw.live.is_live and not light.on
