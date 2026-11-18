"""Two separately started acquisitions for bead flatness and widefield incidence."""

import numpy as np

from control.core.sample_bead_analysis import (
    assess_bead_flatness,
    bead_focus,
    bead_illumination,
    bead_reference_ratio,
    bead_translation,
    measure_beads,
    register_beads,
)
from control.core.sample_incidence_analysis import analysis_pixel_scale, incidence_from_planes, preview
from control.core.sample_bead_geometry import correct_points, uncorrect_points
from control.core.sample_incidence_calibration import dither_offsets


def _objective(hw):
    return getattr(getattr(hw.scope, "objective_store", None), "current_objective", None)


def _register(session, fields, positions, images, pixel_scale, kind, diagnostics=None, distortion=None, **context):
    details = {} if diagnostics is None else diagnostics
    try:
        return register_beads(
            fields,
            positions,
            images,
            session.settings.analysis_maximum,
            pixel_scale,
            details,
            stage_rms_limit_px=session.settings.stage_rms_limit_px,
            distortion=distortion,
            check_cancelled=getattr(getattr(session, "hardware", None), "check_cancelled", None),
        )
    except ValueError as error:
        details["error"] = str(error)
        raise
    finally:
        session._record(kind, **context, **details)


def _stack(session, field, base_z, piezo):
    s, hw = session.settings, session.hardware
    images, actual_z = [], []
    targets = base_z + np.linspace(-s.focus_half_range_um, s.focus_half_range_um, s.focus_steps)
    # Bound peak memory: one field, rather than the complete nine-field scan.
    try:
        with hw.illuminated(s.settle_s):
            for plane, target in enumerate(targets):
                hw.move_z(target)
                image = session._capture("bead_focus", field=field, plane=plane + 1, target_z_um=target)
                images.append(image)
                actual_z.append(hw.stage.get_pos().z_mm * 1000 + piezo)
                session.progress(
                    f"Bead focus: field {field}, plane {plane + 1}/{len(targets)}", preview(image, s.analysis_maximum)
                )
    finally:
        hw.stage.move_z_to(base_z / 1000)
    hw.check_cancelled()
    result = bead_focus(actual_z, images, s.bead_radius_px, s.bead_focus_metric, s.analysis_maximum, s.bead_min_snr)
    session._record("bead_focus", field=field, measured_z_um=np.asarray(actual_z), result=result)
    return result


def _flatness(session):
    s, hw = session.settings, session.hardware
    first = session._capture("initial")
    if first.ndim != 2:
        raise ValueError("Bead calibration requires a monochrome camera image.")
    fov_um = min(first.shape) * session.pixel_size_um
    pixel_scale = analysis_pixel_scale(first.shape, s.analysis_maximum)
    offsets = dither_offsets(s.dither_fraction * fov_um)
    hw.validate_path(offsets, max(5, 2 * s.focus_half_range_um) + s.focus_half_range_um + s.z_baseline_um / 2)
    pos = hw.stage.get_pos()
    origin = np.array([pos.x_mm, pos.y_mm]) * 1000
    positions = offsets + origin
    scans = []
    for i, xy in enumerate(positions):
        hw.move_xy(*xy)
        base, piezo = hw.refocus(max(5, 2 * s.focus_half_range_um))
        scans.append(_stack(session, i + 1, base, piezo))
    images = [scan.projection for scan in scans]
    geometry = {}
    shifts, transform, error = _register(
        session, scans, positions, images, pixel_scale, "flatness_registration", diagnostics=geometry
    )
    distortion = geometry["distortion"]
    sample_positions = shifts @ np.linalg.inv(transform).T
    flatness = assess_bead_flatness(
        sample_positions, scans, shifts, transform, s.max_plane_span_um, s.max_residual_um, pixel_scale, distortion
    )
    flatness.coating_message += (
        f" Radial correction k1={distortion['k1']:.5f}; "
        f"bead-fit RMS {geometry['track_rms_px'] / pixel_scale:.3f} reference px; "
        f"stage-motion RMS {error / pixel_scale:.3f} reference px."
    )
    session.calibration = dict(
        kind="beads",
        flatness=flatness,
        scans=scans,
        positions=positions,
        sample_positions_um=sample_positions,
        distortion=distortion,
        native_shape=first.shape,
        pixel_size_um=session.pixel_size_um,
        objective=_objective(hw),
        bead_radius_px=s.bead_radius_px,
        bead_focus_metric=s.bead_focus_metric,
        analysis_maximum=s.analysis_maximum,
        bead_min_snr=s.bead_min_snr,
        stage_rms_limit_px=s.stage_rms_limit_px,
    )
    session._record("flatness", result=flatness, sample_positions_um=sample_positions, distortion=distortion)
    session.flatness_ready(flatness)
    session.progress("Flatness complete. Review the result, select widefield, then click Measure incidence.", None)


def _incidence(session):
    s, hw, cal = session.settings, session.hardware, session.calibration
    if cal is None or cal.get("kind") != "beads" or not cal["flatness"].accepted:
        raise ValueError("Measure and pass bead flatness before incidence.")
    config = hw.scope.obs_controller.current_observation_state
    if config is None or getattr(config, "confocal_mode", True):
        raise ValueError("Bead incidence requires widefield fluorescence. Switch preset and try again.")
    if cal["objective"] != _objective(hw) or not np.isclose(cal["pixel_size_um"], session.pixel_size_um):
        raise ValueError("Objective or pixel calibration changed; measure flatness again.")
    if s.bead_radius_px != cal["bead_radius_px"]:
        raise ValueError("Bead aperture changed; measure flatness again.")
    if s.bead_focus_metric != cal["bead_focus_metric"]:
        raise ValueError("Bead focus metric changed; measure flatness again.")
    if s.analysis_maximum != cal["analysis_maximum"] or s.bead_min_snr != cal["bead_min_snr"]:
        raise ValueError("Analysis resolution or bead signal threshold changed; measure flatness again.")
    if s.stage_rms_limit_px != cal["stage_rms_limit_px"]:
        raise ValueError("Stage-motion RMS limit changed; measure flatness again.")
    pixel_scale = analysis_pixel_scale(cal["native_shape"], s.analysis_maximum)
    flatness, positions, scans = cal["flatness"], cal["positions"], cal["scans"]
    distortion = cal["distortion"]
    if (
        flatness.plane_span_um > s.max_plane_span_um
        or max(flatness.residual_rms_um, flatness.overlap_rms_um) > s.max_residual_um
    ):
        raise ValueError("The saved flatness result does not meet the current tolerances.")
    session._record("flatness_reference", calibration=cal)
    pos = hw.stage.get_pos()
    if np.linalg.norm(np.array([pos.x_mm, pos.y_mm]) * 1000 - positions[0]) > 0.5:
        raise ValueError("Stage XY moved since flatness; return to the original position or measure flatness again.")
    initial = session._capture("incidence_initial")
    if initial.shape != cal["native_shape"]:
        raise ValueError("Camera dimensions changed; measure flatness again.")
    hw.validate_path(
        positions - positions[0], max(5, 2 * s.focus_half_range_um) + s.focus_half_range_um + s.z_baseline_um / 2
    )
    base, piezo = hw.refocus(max(5, 2 * s.focus_half_range_um))
    center = _stack(session, 1, base, piezo)
    # The switch can alter global focus. Establish it from the same beads, and
    # reject a changed field or a non-uniform focus change across the center FOV.
    shift, a, b, match = bead_translation(
        correct_points(scans[0].points_px[scans[0].valid], center.projection.shape, distortion),
        correct_points(center.points_px[center.valid], center.projection.shape, distortion),
        pixel_scale,
    )
    session._record("widefield_bead_registration", shift_px=shift, **match)
    if np.linalg.norm(shift) > 3 * pixel_scale:
        raise ValueError("Bead field changed after switching modes; measure flatness again.")
    if len(a) < 6:
        raise ValueError("Too few same beads after the mode switch; measure flatness again.")
    changes = center.height_um[center.valid][b] - scans[0].height_um[scans[0].valid][a]
    offset = float(np.median(changes))
    scatter = float(np.sqrt(np.mean((changes - offset) ** 2)))
    if scatter > s.max_residual_um:
        raise ValueError("Focus changed non-uniformly after the mode switch; flatness must be remeasured in widefield.")
    session._record(
        "widefield_focus_transfer",
        matched_beads=len(a),
        offset_um=offset,
        residual_rms_um=scatter,
        image_shift_px=np.asarray(shift),
    )
    native_scale = np.array(cal["native_shape"][::-1]) / np.array(center.projection.shape[::-1])
    aperture_points = [
        (
            uncorrect_points(
                correct_points(scan.points_px, center.projection.shape, distortion) + shift,
                center.projection.shape,
                distortion,
            )
            + 0.5
        )
        * native_scale
        - 0.5
        for scan in scans
    ]
    transform = flatness.stage_to_image
    for cycle in range(3 if s.repeat else 1):
        hw.check_cancelled()
        fields, images, times, actual_z = [[], [], []], [[], [], []], [[], [], []], [[], [], []]
        drift = 0.0
        # No AF interruptions or per-frame illumination toggles in this sweep.
        with hw.illuminated(s.settle_s):
            for field, xy in enumerate(positions):
                hw.move_xy(*xy)
                target = flatness.plane[2] + cal["sample_positions_um"][field] @ flatness.plane[:2] + offset - piezo
                for plane, dz in enumerate((-s.z_baseline_um / 2, 0, s.z_baseline_um / 2)):
                    hw.move_z(target + dz)
                    image = session._capture(
                        "bead_incidence", cycle=cycle + 1, field=field + 1, plane=plane + 1, target_z_um=target + dz
                    )
                    fields[plane].append(
                        measure_beads(
                            image,
                            aperture_points[field],
                            s.bead_radius_px,
                            recenter=True,
                            maximum=s.analysis_maximum,
                            min_snr=s.bead_min_snr,
                        )
                    )
                    images[plane].append(preview(image, s.analysis_maximum))
                    times[plane].append(session.last_capture_monotonic_s)
                    actual_z[plane].append(hw.stage.get_pos().z_mm * 1000 + piezo)
                    session.progress(
                        f"Bead incidence: sweep {cycle+1}, field {field+1}/9, Z {dz:+.2f} µm", images[plane][-1]
                    )
            hw.move_xy(*positions[0])
            target = flatness.plane[2] + offset - piezo
            for plane, dz in enumerate((-s.z_baseline_um / 2, 0, s.z_baseline_um / 2)):
                hw.move_z(target + dz)
                image = session._capture(
                    "bead_incidence_reference", cycle=cycle + 1, plane=plane + 1, target_z_um=target + dz
                )
                reference = measure_beads(
                    image,
                    aperture_points[0],
                    s.bead_radius_px,
                    recenter=True,
                    maximum=s.analysis_maximum,
                    min_snr=s.bead_min_snr,
                )
                ratio = bead_reference_ratio(fields[plane][0], reference, pixel_scale)
                drift = max(drift, abs(ratio - 1))
                if abs(ratio - 1) > 0.03:
                    raise ValueError("Repeated bead reference drift exceeded 3%; angle withheld.")
                end = session.last_capture_monotonic_s
                duration = max(end - times[plane][0], 1e-6)
                for i, t in enumerate(times[plane]):
                    fields[plane][i].flux /= np.exp(np.log(ratio) * (t - times[plane][0]) / duration)
                session._record("bead_reference_drift", plane=plane + 1, ratio=ratio)
        fits = []
        for plane in range(3):
            shifts, measured_transform, error = _register(
                session,
                fields[plane],
                positions,
                images[plane],
                pixel_scale,
                "bead_incidence_registration",
                distortion=distortion,
                cycle=cycle + 1,
                plane=plane + 1,
            )
            if np.linalg.norm(measured_transform - transform) / np.linalg.norm(transform) > 0.02:
                raise ValueError("Camera geometry changed after flatness; angle withheld.")
            fit = bead_illumination(fields[plane], shifts, center.projection.shape, pixel_scale, distortion)
            fits.append(fit)
            session._record(
                "bead_illumination",
                cycle=cycle + 1,
                plane=plane + 1,
                beads=fields[plane],
                shifts=shifts,
                transform=measured_transform,
                result=fit,
            )
        baseline = actual_z[2][0] - actual_z[0][0]
        result = incidence_from_planes(*fits, baseline, transform, pixel_scale)
        result.reference_drift = drift
        result.message += (
            " Bead intensity fits include illumination and detection shading; angle is apparent relative to stage Z."
        )
        session._record("incidence", cycle=cycle + 1, measured_z_um=np.asarray(actual_z), result=result)
        session.incidence_ready(result)
    session.progress("Incidence complete. Remeasure flatness after any sample/holder adjustment.", None)


def run_bead_session(session):
    with session.hardware:
        session._record("hardware_state", saved_state=session.hardware._saved)
        session.sweep += 1
        if session.phase == "incidence":
            _incidence(session)
        else:
            # Beads always stop after flatness, even for legacy 'both' callers.
            _flatness(session)
