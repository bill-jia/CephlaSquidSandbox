"""Radial self-calibration and registration through overlapping bead fields.

Geometry is fitted from repeated beads with free field translations, independently
of stage coordinates. The caller subsequently checks those translations against
stage travel. Raw images and native-pixel photometry are never modified.
"""

import numpy as np
from scipy.linalg import orth
from scipy.optimize import least_squares


def correct_points(points, shape, distortion=None):
    points = np.asarray(points, float)
    if distortion is None or distortion["k1"] == 0:
        return points.copy()
    radius = max(shape) / 2
    center = (np.array(shape[::-1]) - 1) / 2 + radius * np.asarray(distortion["center_offset"])
    q = (points - center) / radius
    return points + distortion["k1"] * (points - center) * np.sum(q**2, axis=-1)[..., None]


def uncorrect_points(points, shape, distortion=None):
    """Invert the bounded, monotonic radial correction for aperture placement."""
    target = np.asarray(points, float)
    guess = target.copy()
    for _ in range(40):
        step = target - correct_points(guess, shape, distortion)
        guess += step
        if np.max(np.abs(step), initial=0) < 1e-8:
            return guess
    raise ValueError("Radial correction could not be inverted reliably.")


def _validate_model(model):
    if not np.isfinite(model["k1"]) or abs(model["k1"]) >= 0.03:
        raise ValueError("Radial distortion exceeds the supported calibration range.")
    center = np.asarray(model["center_offset"])
    if center.shape != (2,) or not np.isfinite(center).all() or np.max(abs(center)) >= 0.25:
        raise ValueError("Radial distortion center is outside supported bounds.")


def _edges(catalogs, shape, pixel_scale, model, bootstrap=False, check_cancelled=None):
    from control.core.sample_bead_analysis import bead_translation

    coordinates = [correct_points(p, shape, model) for p in catalogs]
    edges, rejected = [], []
    for i in range(len(catalogs)):
        for j in range(i + 1, len(catalogs)):
            if check_cancelled is not None:
                check_cancelled()
            try:
                shift, a, b, details = bead_translation(
                    coordinates[i], coordinates[j], pixel_scale * (2 if bootstrap else 1)
                )
            except ValueError as error:
                rejected.append(dict(first=i + 1, second=j + 1, error=str(error)))
                continue
            edges.append(dict(i=i, j=j, a=a, b=b, shift=shift, **details))
    return edges, rejected


def _tracks(catalogs, edges, shape):
    starts = np.r_[0, np.cumsum([len(p) for p in catalogs])]
    parent = np.arange(starts[-1])
    neighbors = [set() for _ in catalogs]

    def root(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for edge in edges:
        i, j = edge["i"], edge["j"]
        neighbors[i].add(j)
        neighbors[j].add(i)
        for a, b in zip(edge["a"] + starts[i], edge["b"] + starts[j]):
            parent[root(a)] = root(b)
    reached, pending = set(), [0]
    while pending:
        i = pending.pop()
        if i not in reached:
            reached.add(i)
            pending.extend(neighbors[i] - reached)
    if len(reached) != len(catalogs):
        missing = ", ".join(str(i + 1) for i in range(len(catalogs)) if i not in reached)
        raise ValueError(
            f"Disconnected bead fields {missing}: no unambiguous overlap path with four shared beads per link. "
            "Reduce XY offset or use more distributed beads."
        )
    labels = np.array([root(i) for i in range(len(parent))])
    field = np.repeat(np.arange(len(catalogs)), np.diff(starts))
    _, bead, counts = np.unique(labels, return_inverse=True, return_counts=True)
    for identity in np.unique(bead):
        seen = field[bead == identity]
        if len(np.unique(seen)) != len(seen):
            raise ValueError("Conflicting bead identities around overlap paths; registration is ambiguous.")
    keep = counts[bead] > 1
    _, bead = np.unique(bead[keep], return_inverse=True)
    radius = max(shape) / 2
    q = (np.concatenate(catalogs)[keep] + 0.5 - np.array(shape[::-1]) / 2) / radius
    return q, field[keep], bead, neighbors


def _nuisance(field, bead):
    """Eliminate bead positions analytically; solve only the small field system."""
    fs, fi = np.unique(field, return_inverse=True)
    bs, bi = np.unique(bead, return_inverse=True)
    counts = np.bincount(bi)

    def means(values):
        totals = np.zeros((len(bs),) + values.shape[1:])
        np.add.at(totals, bi, values)
        return totals / counts.reshape((-1,) + (1,) * (values.ndim - 1))

    def centered(values):
        return values - means(values)[bi]

    design = np.zeros((len(field), len(fs) - 1))
    nonreference = fi > 0
    design[np.where(nonreference)[0], fi[nonreference] - 1] = 1
    reduced = centered(design)
    if design.shape[1] and np.linalg.matrix_rank(reduced) < design.shape[1]:
        raise ValueError("Bead overlap paths do not independently constrain field positions.")
    basis = orth(reduced)

    def project(values):
        values = centered(values)
        return values - basis @ (basis.T @ values)

    def solve(values):
        translations = np.linalg.lstsq(reduced, centered(values), rcond=None)[0]
        world = means(values - design @ translations)
        return dict(zip(bs, world)), dict(zip(fs, np.vstack((np.zeros(2), translations))))

    return project, solve


def _fit(q, field, bead, kind="identity", fixed=None):
    project, solve = _nuisance(field, bead)
    pq = project(q)

    def evaluate(center):
        relative = q - center
        feature = relative * np.sum(relative**2, axis=1)[:, None]
        pf = project(feature)
        power = np.sum(pf**2)
        if power < 1e-10:
            raise ValueError("Bead positions do not constrain radial distortion.")
        k = -np.sum(pq * pf) / power
        return k, feature, pq + k * pf

    model = dict(k1=0.0, center_offset=np.zeros(2), kind="identity")
    if fixed is not None:
        model = fixed
    elif kind != "identity":
        center = np.zeros(2)
        if kind == "radial_center":
            solution = least_squares(
                lambda c: evaluate(c)[2].ravel(),
                center,
                bounds=(-0.25, 0.25),
                max_nfev=60,
                ftol=1e-10,
                xtol=1e-10,
                gtol=1e-10,
            )
            if not solution.success:
                raise ValueError("Radial distortion center did not converge.")
            center = solution.x
        k, _, _ = evaluate(center)
        model = dict(k1=float(k), center_offset=center, kind=kind)
    _validate_model(model)
    relative = q - model["center_offset"]
    corrected = q + model["k1"] * relative * np.sum(relative**2, axis=1)[:, None]
    world, shifts = solve(corrected)
    return model, project(corrected), world, shifts


def _validation(q, field, bead, kind, check_cancelled=None):
    residuals = []
    for held in np.unique(field):
        if check_cancelled is not None:
            check_cancelled()
        keep = field != held
        model, _, world, _ = _fit(q[keep], field[keep], bead[keep], kind)
        ids = np.where(~keep & np.isin(bead, list(world)))[0]
        if len(ids) < 4:
            raise ValueError("Too few shared beads to validate a held-out field.")
        relative = q[ids] - model["center_offset"]
        corrected = q[ids] + model["k1"] * relative * np.sum(relative**2, axis=1)[:, None]
        residual = corrected - np.array([world[i] for i in bead[ids]])
        residuals.extend(residual - residual.mean(axis=0))
    return float(np.sqrt(np.mean(np.sum(np.asarray(residuals) ** 2, axis=1))))


def fit_bead_geometry(catalogs, shape, pixel_scale, diagnostics, distortion=None, check_cancelled=None):
    """Return corrected-coordinate field shifts and a validated distortion model."""
    if distortion is not None:
        _validate_model(distortion)
    radius = max(shape) / 2
    edges, rejected = _edges(catalogs, shape, pixel_scale, distortion, check_cancelled=check_cancelled)
    bootstrapped = False
    diagnostics["rejected_links"] = rejected
    try:
        q, field, bead, _ = _tracks(catalogs, edges, shape)
    except ValueError:
        if distortion is not None:
            raise
        edges, rejected = _edges(catalogs, shape, pixel_scale, None, bootstrap=True, check_cancelled=check_cancelled)
        bootstrapped = True
        diagnostics["rejected_links"] = rejected
        q, field, bead, _ = _tracks(catalogs, edges, shape)
    diagnostics.update(repeated_beads=int(len(np.unique(bead))), observations=len(q))
    if distortion is None:
        model, baseline_residual, _, _ = _fit(q, field, bead)
        baseline_rms = float(np.sqrt(np.mean(np.sum(baseline_residual**2, axis=1))) * radius)
        validation = dict(identity_fit_rms_px=baseline_rms)
        # Do not infer a distortion center from noise when translation already
        # describes the data accurately. Identity remains a valid calibration.
        if baseline_rms > 0.2 * pixel_scale and len(np.unique(bead)) >= 8 and np.all(np.ptp(q, axis=0) >= 1):
            try:
                best_cv = _validation(q, field, bead, "identity", check_cancelled) * radius
                validation["identity_cv_rms_px"] = best_cv
                for kind, improvement in (("radial", 0.8), ("radial_center", 0.9)):
                    try:
                        candidate, _, _, _ = _fit(q, field, bead, kind)
                        cv = _validation(q, field, bead, kind, check_cancelled) * radius
                    except ValueError as error:
                        validation[kind + "_error"] = str(error)
                        continue
                    validation[kind + "_cv_rms_px"] = cv
                    if cv < improvement * best_cv:
                        model, best_cv = candidate, cv
            except ValueError as error:
                validation["validation_error"] = str(error)
        elif baseline_rms > 0.2 * pixel_scale:
            validation["validation_error"] = "Insufficient distributed repeated beads for radial calibration."
        diagnostics["distortion_validation"] = validation
    else:
        model = distortion
        _validate_model(model)
    diagnostics["distortion"] = model
    # Bootstrap links are proposals only. Rebuild all correspondences using the
    # corrected geometry and the ORIGINAL correspondence/ambiguity thresholds.
    if bootstrapped or (distortion is None and model["k1"] != 0):
        edges, rejected = _edges(catalogs, shape, pixel_scale, model, check_cancelled=check_cancelled)
    diagnostics["rejected_links"] = rejected
    q, field, bead, neighbors = _tracks(catalogs, edges, shape)
    _, residual, _, translations = _fit(q, field, bead, fixed=model)
    shifts = np.array([translations[i] for i in range(len(catalogs))]) * radius
    diagnostics.update(
        repeated_beads=int(len(np.unique(bead))),
        observations=len(q),
        track_rms_px=float(np.sqrt(np.mean(np.sum(residual**2, axis=1))) * radius),
    )
    corrected = [correct_points(p, shape, model) for p in catalogs]
    links = []
    for edge in edges:
        i, j = edge["i"], edge["j"]
        errors = corrected[j][edge["b"]] - corrected[i][edge["a"]] - (shifts[j] - shifts[i])
        error = float(np.sqrt(np.mean(np.sum(errors**2, axis=1))))
        links.append(
            dict(
                first=i + 1,
                second=j + 1,
                matches=edge["matches"],
                match_rms_px=edge["match_rms_px"],
                cycle_rms_px=error,
                competing_matches=edge["competing_matches"],
            )
        )
    diagnostics["links"] = links
    diagnostics["fields"] = [
        dict(
            field=i + 1,
            valid_beads=len(catalogs[i]),
            matches=int(np.sum(field == i)),
            neighbors=[j + 1 for j in sorted(neighbors[i])],
            shift_px=shifts[i],
        )
        for i in range(1, len(catalogs))
    ]
    if any(link["cycle_rms_px"] > pixel_scale for link in links):
        raise ValueError("Bead overlap paths disagree after radial correction; correspondence RMS exceeds its limit.")
    return shifts, model
