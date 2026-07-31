"""
2D differential phase contrast (DPC) — quantitative phase from four half-circle
(or half-annulus) brightfield captures.

Implements the weak-object transfer function (WOTF) / Tikhonov solver of
L. Tian and L. Waller, "Quantitative differential phase contrast imaging in an
LED array microscope," Opt. Express 23(9), 11394 (2015) — the same math as the
offline reference engine (``qpm-analysis/example/tian2015/dpc_algorithm.py``
driven by ``dpc_tian2015_run.py``), including that runner's
``VariableNADPCSolver`` extension that decouples the illumination (source) NA
from the objective NA so an under/over-filled condenser (or an annular ring) is
modelled correctly.

Inputs: four observation states, one frame each per FOV visit, illuminated by
the top / bottom / left / right half of the LED array (``top_half`` …, or the
half-annulus modes ``half_ann_t`` …). Put the four steps under **one cycle
group** and assign this routine to the group — a per-step assignment only ever
sees one state. Roles come from the ``state_top`` / ``state_bottom`` /
``state_left`` / ``state_right`` params; when those are blank the state names
are matched against the usual tags (``top``/``bot``/``left``/``right``, ``th``/
``bh``/``lh``/``rh``, ``half_ann_t`` …).

Outputs per FOV visit:
- ``phase`` — float32 ``(Y, X)`` quantitative phase in radians.
- ``absorption`` — float32 ``(Y, X)`` absorption (optional, ``output_absorption``).
- ``brightfield`` — the mean of the four raw halves ≈ the full-disk brightfield
  image, input dtype (optional, ``output_brightfield``).

Everything that depends only on the geometry and the regularization — the
pupils, the four half-plane sources, the WOTFs ``Hu``/``Hp``, and the Tikhonov
normal equations — is **precomputed once** (pre-acquisition ``warmup``, or the
first FOV) and cached in ``ctx.cache``. It is cached in an already-inverted form
(see :meth:`DPC2DRoutine._build_filter_bank`), so each FOV costs only four
forward FFTs, a weighted sum, and one inverse FFT.

Only numpy + scipy are required (both already in the environment); imports stay
inside the compute/warmup path so pre-flight validation in the main process
stays light.
"""

import re
from typing import Any, Dict, Iterable, List, Optional

import numpy as np

from control.postprocessing.base import (
    DTYPE_INPUT,
    InputStateSpec,
    OutputSpec,
    PostprocessContext,
    PostprocessRoutine,
)

# The four illumination halves, in the order the filter bank is built.
ROLES = ("top", "bottom", "left", "right")

# Half-plane boundary angle (degrees) per role, measured from the +fy axis:
# the lit side of the source satisfies fy·cos(θ) ≥ fx·sin(θ) for θ < 180 and the
# complement for θ ≥ 180.
#
# Top/bottom are SWAPPED (180/0) relative to the bare Tian reference, matching
# ``dpc_tian2015_run.ROTATION``: it makes the phase WOTF's vertical (fy) lobes —
# and hence the recovered vertical phase gradient — line up with the image's
# vertical axis. This is a fixed software/WOTF convention, independent of camera
# image orientation.
ROTATION_DEG = {"top": 180.0, "bottom": 0.0, "left": 90.0, "right": 270.0}

# Filename/state-name tags used to infer a role when the explicit state_* params
# are blank. Tags of ≥ 4 characters also match as a substring of the whole name;
# shorter ones must appear as a whole token (the name split on non-alphanumerics)
# so that e.g. "l" doesn't match every state containing the letter.
_ROLE_TAGS = {
    "top": ("top", "tophalf", "half_ann_t", "hat", "th", "t"),
    "bottom": ("bottom", "bottomhalf", "half_ann_b", "hab", "bot", "bh", "b"),
    "left": ("left", "lefthalf", "half_ann_l", "hal", "lh", "l"),
    "right": ("right", "righthalf", "half_ann_r", "har", "rh", "r"),
}

DEFAULT_PARAMS: Dict[str, Any] = {
    # Role assignment. Explicit is the intended path (the params dialog fills
    # these from the group's member steps); blank falls back to tag matching.
    "state_top": None,
    "state_bottom": None,
    "state_left": None,
    "state_right": None,
    # Optics. Dialog/validation fill wavelength_nm from the states and
    # na_detection from the objective; None means "not provided" and fails with
    # a clear message.
    "wavelength_nm": 530,
    "na_detection": 0.4,
    "na_illumination": 0.4,
    "na_illumination_inner": 0.0,
    # Tikhonov weights, reference defaults (dpc_tian2015_run --reg-u / --reg-p).
    "regularization_absorption": 1e-1,
    "regularization_phase": 1e-2,
    # Extra outputs (each costs another plate on disk and in the upload).
    "output_absorption": False,
    "output_brightfield": True,
    # False halves the cached filter bank and the per-FOV FFT cost by working in
    # complex64 instead of the reference's complex128. Kept off by default so
    # results are bit-comparable with the offline reference run.
    "single_precision": False,
}


def _tokens(name: str) -> set:
    return set(t for t in re.split(r"[^a-z0-9]+", name.lower()) if t)


def _role_of(name: str) -> Optional[str]:
    """The role whose tag matches this state name, ``None`` if none or several."""
    lowered = name.lower()
    tokens = _tokens(name)
    hits = [
        role
        for role, tags in _ROLE_TAGS.items()
        if any(t in tokens for t in tags) or any(len(t) >= 4 and t in lowered for t in tags)
    ]
    return hits[0] if len(hits) == 1 else None


def resolve_roles(state_names: Iterable[str], params: Dict[str, Any]) -> Dict[str, str]:
    """Map ``role -> observation state name`` for the four halves.

    Explicit ``state_<role>`` params win; any role left blank is inferred from
    the state names by tag. Raises ``ValueError`` with a user-actionable message
    when the four roles can't be filled unambiguously.
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    names = list(state_names)
    if len(names) != 4:
        raise ValueError(
            f"dpc2d needs exactly 4 input states (top/bottom/left/right half illumination), got "
            f"{len(names)}: {sorted(names)}. Put the four half-circle steps under one cycle group "
            "and assign the routine to the group."
        )

    roles: Dict[str, str] = {}
    for role in ROLES:
        chosen = p.get(f"state_{role}") or None
        if chosen is None:
            continue
        if chosen not in names:
            raise ValueError(
                f"dpc2d: param state_{role}={chosen!r} is not one of this group's input states {sorted(names)}"
            )
        if chosen in roles.values():
            taken = next(r for r, s in roles.items() if s == chosen)
            raise ValueError(f"dpc2d: input state {chosen!r} is assigned to both the {taken} and {role} half")
        roles[role] = chosen

    unassigned = [n for n in names if n not in roles.values()]
    for name in unassigned:
        role = _role_of(name)
        if role is not None and role not in roles:
            roles[role] = name

    missing = [r for r in ROLES if r not in roles]
    if missing:
        raise ValueError(
            f"dpc2d: could not tell which input state is the {'/'.join(missing)} half "
            f"(states: {sorted(names)}). Set state_top / state_bottom / state_left / state_right "
            "in the routine params, or name the states with top/bottom/left/right tags."
        )
    return roles


def _freq_axes(yx_shape, pixel_size_um: float):
    """Centred spatial-frequency axes (cycles/µm) in FFT layout (DC at index 0),
    matching the reference ``_genGrid`` + ``ifftshift``."""
    ny, nx = int(yx_shape[0]), int(yx_shape[1])
    fx = np.fft.ifftshift((np.arange(nx) - nx // 2) / (nx * pixel_size_um))
    fy = np.fft.ifftshift((np.arange(ny) - ny // 2) / (ny * pixel_size_um))
    return fx, fy


def _pupil(fx, fy, wavelength_um: float, na: float, na_in: float = 0.0):
    """Binary pupil mask: |f| ≤ NA/λ, with a central hole below ``na_in``
    (``na_in`` > 0 gives the annular source of a half-annulus capture)."""
    r2 = fx[np.newaxis, :] ** 2 + fy[:, np.newaxis] ** 2
    pupil = (r2 <= (na / wavelength_um) ** 2).astype(np.float64)
    if na_in:
        pupil[r2 < (na_in / wavelength_um) ** 2] = 0.0
    return pupil


def _half_plane_source(fx, fy, rotation_deg: float, source_pupil):
    """One half-plane illumination mask clipped to the source pupil.

    Verbatim reference ``sourceGen``: for θ < 180 the lit side is
    ``fy·cos θ ≥ fx·sin θ``; for θ ≥ 180 the complement is built as
    ``-pupil`` on the opposite side plus the full pupil. The 1e-15 epsilon
    breaks numerical ties exactly on the boundary line.
    """
    src = np.zeros(source_pupil.shape, dtype=np.float64)
    lhs = fy[:, np.newaxis] * np.cos(np.deg2rad(rotation_deg)) + 1e-15
    rhs = fx[np.newaxis, :] * np.sin(np.deg2rad(rotation_deg))
    if rotation_deg < 180:
        src[lhs >= rhs] = 1.0
        src *= source_pupil
    else:
        src[lhs < rhs] = -1.0
        src *= source_pupil
        src += source_pupil
    return src


def _normalize(image, dtype):
    """Self-normalize one raw half image — no blank/empty-field reference needed.

    Reference ``DPCSolver.normalization``: divide by a local mean (uniform filter
    of half the image height) to flatten slowly-varying background, divide by the
    global mean, then subtract 1 so the result is the fractional intensity
    deviation (I − ⟨I⟩)/⟨I⟩ with DC at 0 — directly comparable to the WOTF model.
    """
    from scipy.ndimage import uniform_filter

    img = np.asarray(image, dtype=dtype)
    size = max(1, img.shape[0] // 2)
    img = img / uniform_filter(img, size=size)
    img = img / img.mean()
    return img - dtype(1.0)


def validate_optics(params: Dict[str, Any]) -> dict:
    """Check + resolve the optics/regularization params that need no acquisition
    context, so a bad setting fails pre-flight instead of at the first FOV."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    if not p["na_detection"]:
        raise ValueError("dpc2d: detection NA unknown — set na_detection in the routine params")
    na_det = float(p["na_detection"])
    na_ill = float(p["na_illumination"] or na_det)
    na_in = float(p["na_illumination_inner"] or 0.0)
    if not 0.0 <= na_in < na_ill:
        raise ValueError(
            f"dpc2d: na_illumination_inner ({na_in}) must satisfy 0 ≤ inner < na_illumination ({na_ill}); "
            "the annular ring would be empty or inverted"
        )
    if na_in >= na_det:
        raise ValueError(
            f"dpc2d: the illumination ring's inner NA ({na_in}) is at or beyond the objective NA ({na_det}), so "
            "the ring falls outside the pupil — the capture is pure darkfield and brightfield DPC is not "
            "defined for it"
        )
    reg_u = float(p["regularization_absorption"])
    reg_p = float(p["regularization_phase"])
    if reg_u <= 0 or reg_p <= 0:
        raise ValueError(
            f"dpc2d: both regularization weights must be > 0 (got absorption={reg_u}, phase={reg_p}); "
            "they are what keeps the per-frequency 2×2 inversion well-posed"
        )
    return {"na_det": na_det, "na_ill": na_ill, "na_in": na_in, "reg_u": reg_u, "reg_p": reg_p}


class DPC2DRoutine(PostprocessRoutine):
    name = "dpc2d"
    display_name = "DPC 2D (Tian 2015)"

    def __init__(self):
        # One instance is shared by every FOV of a run (see
        # PostprocessJob.ensure_routine), so this keeps the z-stack notice to a
        # single line instead of four per FOV.
        self._warned_zstack = False

    def describe_outputs(self, input_states: Dict[str, InputStateSpec], params: Dict[str, Any]) -> List[OutputSpec]:
        p = {**DEFAULT_PARAMS, **(params or {})}
        resolve_roles(input_states.keys(), p)  # raises with an actionable message
        validate_optics(p)
        for spec in input_states.values():
            if spec.frames_per_visit != 1:
                raise ValueError(
                    f"dpc2d expects one frame of {spec.state!r} per FOV visit, got "
                    f"{spec.frames_per_visit} (reduce n_frames / repeats)"
                )
        wavelength_nm = p["wavelength_nm"]
        wl = int(wavelength_nm) if wavelength_nm else None
        outputs = [OutputSpec(name="phase", z_size=1, dtype="float32", wavelength_nm=wl)]
        if p["output_absorption"]:
            outputs.append(OutputSpec(name="absorption", z_size=1, dtype="float32", wavelength_nm=wl))
        if p["output_brightfield"]:
            outputs.append(OutputSpec(name="brightfield", z_size=1, dtype=DTYPE_INPUT, wavelength_nm=wl))
        return outputs

    def _resolve(self, ctx: PostprocessContext, params: Dict[str, Any], roles: Dict[str, str]) -> dict:
        """Validate + resolve the scalar optical parameters (raises on missing)."""
        p = {**DEFAULT_PARAMS, **(params or {})}
        if not ctx.pixel_size_um:
            raise ValueError("dpc2d: pixel size unknown")
        wavelength_nm = p["wavelength_nm"]
        if not wavelength_nm:
            for state in roles.values():
                wavelength_nm = ctx.state_meta.get(state, {}).get("wavelength_nm")
                if wavelength_nm:
                    break
        if not wavelength_nm:
            raise ValueError("dpc2d: illumination wavelength unknown — set wavelength_nm in the routine params")
        return {
            **validate_optics(p),
            "wavelength_um": float(wavelength_nm) / 1000.0,
            "pixel_size_um": float(ctx.pixel_size_um),
            "absorption": bool(p["output_absorption"]),
            "single": bool(p["single_precision"]),
        }

    @staticmethod
    def _release_memory():
        """Drop freed Python objects so a rebuilt filter bank does not stack on
        top of the previous one's working set (the WOTFs are ~278 MB each at full
        frame in complex128)."""
        import gc

        gc.collect()

    def _build_filter_bank(self, o: dict, yx_shape) -> dict:
        """Precompute the per-direction, already-inverted frequency filters.

        The reference solves, independently at every frequency, the Tikhonov
        normal equations of the 2-component weak-object model::

            AHA = [[Σ Hu*Hu + reg_u,  Σ Hu*Hp        ],
                   [Σ Hp*Hu,          Σ Hp*Hp + reg_p]]
            AHy = [Σ Hu*·Î_d,  Σ Hp*·Î_d]
            û   = (AHA[3]·AHy[0] − AHA[1]·AHy[1]) / det
            p̂   = (AHA[0]·AHy[1] − AHA[2]·AHy[0]) / det

        Since ``AHy`` is itself a sum over the four directions, substituting it
        collapses the whole inversion into one weighted sum of the four measured
        spectra::

            p̂ = Σ_d Gp_d · Î_d,   Gp_d = (AHA[0]·Hp_d* − AHA[2]·Hu_d*) / det
            û = Σ_d Gu_d · Î_d,   Gu_d = (AHA[3]·Hu_d* − AHA[1]·Hp_d*) / det

        which is algebraically identical but lets the FOV-invariant part be
        cached as just four (or eight) arrays instead of the WOTFs plus the
        Gramian. Two exact symmetries keep the intermediates real where possible:
        ``AHA[0]``/``AHA[3]`` are real (they are Σ|H|² + reg) and
        ``AHA[2] = conj(AHA[1])``, hence ``det = AHA[0]·AHA[3] − |AHA[1]|²`` is
        real and — with both regularization weights > 0 — strictly positive by
        Cauchy–Schwarz, so the division never blows up.
        """
        from scipy.fft import fft2, ifft2

        cdtype = np.complex64 if o["single"] else np.complex128
        fx, fy = _freq_axes(yx_shape, o["pixel_size_um"])
        # Objective pupil (detection) and source pupil (illumination NA, with the
        # optional annular hole) are separate — this is the runner's
        # VariableNADPCSolver extension; with na_ill == na_det and na_in == 0 it
        # reduces exactly to the reference.
        pupil = _pupil(fx, fy, o["wavelength_um"], o["na_det"])
        source_pupil = _pupil(fx, fy, o["wavelength_um"], o["na_ill"], na_in=o["na_in"])
        pupil_spectrum_conj = np.conj(fft2(pupil))

        hu: List[Any] = []
        hp: List[Any] = []
        for role in ROLES:
            source = _half_plane_source(fx, fy, ROTATION_DEG[role], source_pupil)
            # Weak-object transfer functions, Eqs. (5–6): the even part of the
            # source-weighted pupil autocorrelation maps to absorption, the odd
            # part to phase; I0 is the DC intensity through the lit aperture.
            fsp_cfp = fft2(source * pupil) * pupil_spectrum_conj
            i0 = (source * pupil * pupil).sum()
            if i0 == 0:
                # validate_optics already rejects a ring outside the pupil, so
                # reaching here means the lit region falls between grid samples
                # (a very small frame, or a very thin ring at this pixel size).
                raise ValueError(
                    f"dpc2d: the {role} half of the illumination covers no sampled frequency at this frame "
                    f"size and pixel size (NA_ill=[{o['na_in']}, {o['na_ill']}], NA_det={o['na_det']})"
                )
            hu.append((2.0 * ifft2(fsp_cfp.real) / i0).astype(cdtype, copy=False))
            hp.append((2.0j * ifft2(1j * fsp_cfp.imag) / i0).astype(cdtype, copy=False))
        del fsp_cfp, pupil_spectrum_conj, pupil, source_pupil, source

        aha0 = sum((h.conj() * h).real for h in hu) + o["reg_u"]  # real, ≥ reg_u
        aha3 = sum((h.conj() * h).real for h in hp) + o["reg_p"]  # real, ≥ reg_p
        aha1 = sum(u.conj() * p for u, p in zip(hu, hp))
        det = aha0 * aha3 - (aha1.conj() * aha1).real

        gp: List[Any] = []
        gu: List[Any] = [] if o["absorption"] else None
        for i in range(len(ROLES)):
            # AHA[2] = conj(AHA[1]); see the docstring.
            gp.append(((aha0 * hp[i].conj() - aha1.conj() * hu[i].conj()) / det).astype(cdtype, copy=False))
            if gu is not None:
                gu.append(((aha3 * hu[i].conj() - aha1 * hp[i].conj()) / det).astype(cdtype, copy=False))
            hu[i] = None  # free the WOTFs as we consume them
            hp[i] = None
        del hu, hp, aha0, aha1, aha3, det
        self._release_memory()
        return {"Gp": gp, "Gu": gu}

    def _get_filter_bank(self, ctx: PostprocessContext, o: dict, yx_shape) -> dict:
        """Cached inverted-filter bank for this geometry, computing it on first
        use. Shared by :meth:`warmup` (pre-acquisition) and :meth:`process` (per
        FOV) so the first FOV is a cache hit.

        Only one bank is kept resident: a stale entry (e.g. a warmup that guessed
        a different frame shape) is dropped *before* the new one is built, since
        at full frame each array is ~278 MB in complex128.
        """
        cache_key = (
            "dpc2d",
            tuple(int(v) for v in yx_shape),
            round(o["pixel_size_um"], 6),
            round(o["wavelength_um"], 6),
            round(o["na_det"], 4),
            round(o["na_ill"], 4),
            round(o["na_in"], 4),
            o["reg_u"],
            o["reg_p"],
            o["absorption"],
            o["single"],
        )
        bank = ctx.cache.get(cache_key)
        if bank is not None:
            ctx.logger.debug("dpc2d: filter-bank cache hit")
            return bank
        ctx.cache.clear()
        self._release_memory()
        bank = self._build_filter_bank(o, yx_shape)
        ctx.cache[cache_key] = bank
        ctx.logger.info(
            "dpc2d: computed WOTF filter bank (yx_shape=%s, λ=%.3f µm, NA_det=%.3f, NA_ill=%.3f, "
            "NA_inner=%.3f, reg_u=%.3g, reg_p=%.3g, %s)",
            tuple(int(v) for v in yx_shape),
            o["wavelength_um"],
            o["na_det"],
            o["na_ill"],
            o["na_in"],
            o["reg_u"],
            o["reg_p"],
            "complex64" if o["single"] else "complex128",
        )
        return bank

    def warmup(self, input_states, ctx, params):
        if not ctx.yx_shape:
            ctx.logger.debug("dpc2d: no yx_shape for warmup; will compute lazily on first FOV")
            return
        roles = resolve_roles(input_states.keys(), params)
        o = self._resolve(ctx, params, roles)
        self._get_filter_bank(ctx, o, ctx.yx_shape)

    def _select_planes(self, inputs: Dict[str, np.ndarray], roles: Dict[str, str], ctx) -> Dict[str, np.ndarray]:
        """One ``(Y, X)`` raw plane per role, at the focus plane of each input."""
        planes: Dict[str, np.ndarray] = {}
        stacked: List[str] = []
        for role, state in roles.items():
            arr = np.asarray(inputs[state])  # (F, Z, Y, X)
            nz = arr.shape[1]
            if nz > 1:
                stacked.append(state)
            planes[role] = arr[0, nz // 2]
        if stacked and not self._warned_zstack:
            # dpc2d reconstructs a single plane; a step left on "Full z-stack"
            # contributes only its focus plane, so say so rather than silently
            # dropping the rest.
            self._warned_zstack = True
            ctx.logger.warning(
                "dpc2d: input state(s) %s were acquired as z-stacks; using only the focus plane of each. "
                "Turn off 'Full z-stack' on the DPC steps to stop acquiring the rest.",
                ", ".join(repr(s) for s in stacked),
            )
        shapes = {p.shape for p in planes.values()}
        if len(shapes) != 1:
            raise ValueError(f"dpc2d: the four half images have different shapes: {sorted(shapes)}")
        return planes

    def process(
        self, inputs: Dict[str, np.ndarray], ctx: PostprocessContext, params: Dict[str, Any]
    ) -> Dict[str, np.ndarray]:
        from scipy.fft import fft2, ifft2

        p = {**DEFAULT_PARAMS, **(params or {})}
        roles = resolve_roles(inputs.keys(), p)
        planes = self._select_planes(inputs, roles, ctx)
        o = self._resolve(ctx, p, roles)
        bank = self._get_filter_bank(ctx, o, next(iter(planes.values())).shape)

        rdtype = np.float32 if o["single"] else np.float64
        acc_p = None
        acc_u = None
        for i, role in enumerate(ROLES):
            spectrum = fft2(_normalize(planes[role], rdtype))
            term = bank["Gp"][i] * spectrum
            acc_p = term if acc_p is None else acc_p + term
            if bank["Gu"] is not None:
                term_u = bank["Gu"][i] * spectrum
                acc_u = term_u if acc_u is None else acc_u + term_u

        outputs: Dict[str, np.ndarray] = {"phase": np.asarray(ifft2(acc_p).real, dtype=np.float32)}
        if acc_u is not None:
            outputs["absorption"] = np.asarray(ifft2(acc_u).real, dtype=np.float32)
        if p["output_brightfield"]:
            # The four halves tile the source, so their mean is the full-disk
            # (or full-annulus) brightfield image at the same exposure.
            raw_dtype = planes["top"].dtype
            mean = sum(np.asarray(planes[r], dtype=np.float64) for r in ROLES) / len(ROLES)
            if np.issubdtype(raw_dtype, np.integer):
                mean = np.rint(mean)
            outputs["brightfield"] = mean.astype(raw_dtype, copy=False)

        del acc_p, acc_u
        self._release_memory()
        return outputs


ROUTINE = DPC2DRoutine()
