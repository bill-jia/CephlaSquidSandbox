"""
2D quantitative phase from a defocused brightfield z-stack (waveorder).

Uses the high-level ``waveorder.api.phase`` pipeline — ``phase.Settings`` →
``phase.compute_transfer_function`` (``recon_dim=2``) → ``phase.apply_inverse_
transfer_function`` — to invert the acquired z-defocus stack ``(Z, Y, X)`` into a
single 2D phase image. The transfer function (returned as an ``xr.Dataset`` of the
singular system) depends only on the acquisition geometry, so it is computed once
(pre-acquisition ``warmup``, or the first FOV) and cached in ``ctx.cache`` for
every subsequent FOV of the run.

Outputs per FOV visit:
- ``phase`` — float32 ``(Y, X)`` quantitative phase.
- ``bf_center`` — the raw brightfield slice at the focus plane (``nz // 2``, the
  plane the transfer function treats as zero defocus), input dtype.

Requires ``waveorder`` (and ``torch``) in the environment::

    pip install PyWavelets
    pip install --no-deps --ignore-requires-python -e C:\\Code\\waveorder

Imports of both are deferred to the compute/warmup path so pre-flight validation
in the main process stays light.
"""

from typing import Any, Dict, List

import numpy as np

from control.postprocessing.base import (
    DTYPE_INPUT,
    InputStateSpec,
    OutputSpec,
    PostprocessContext,
    PostprocessRoutine,
)


def reference_z_index(nz: int) -> int:
    """Focus-plane index within the stack — the plane the transfer function
    treats as zero defocus (``nz // 2``, matching waveorder's centered,
    ``z_focus_offset=0`` position list)."""
    return max(0, min(nz - 1, nz // 2))


class Phase2DRoutine(PostprocessRoutine):
    name = "phase2d"
    display_name = "Phase 2D (waveorder)"

    # Dialog/validation fill wavelength_nm and na_detection from the state and
    # objective; None means "not provided" and fails with a clear message. The
    # numeric defaults are reasonable brightfield values, tunable per run.
    DEFAULT_PARAMS: Dict[str, Any] = {
        "wavelength_nm": 625,
        "na_detection": 0.4,
        "na_illumination": 0.33,
        "index_of_refraction_media": 1.333,
        "regularization": 0.05,
        "invert_phase_contrast": False,
    }

    def describe_outputs(self, input_states: Dict[str, InputStateSpec], params: Dict[str, Any]) -> List[OutputSpec]:
        if len(input_states) != 1:
            raise ValueError(
                f"phase2d takes exactly one brightfield input state, got {sorted(input_states)}"
            )
        spec = next(iter(input_states.values()))
        if not spec.acquire_z_stack:
            raise ValueError(
                f"phase2d needs the defocus z-stack of {spec.state!r} — enable 'Full z-stack' on that step"
            )
        if spec.frames_per_visit != 1:
            raise ValueError(
                f"phase2d expects one frame of {spec.state!r} per FOV visit, got {spec.frames_per_visit} "
                "(reduce n_frames / repeats)"
            )
        return [
            OutputSpec(name="phase", z_size=1, dtype="float32"),
            OutputSpec(name="bf_center", z_size=1, dtype=DTYPE_INPUT),
        ]

    def _resolve(self, ctx: PostprocessContext, params: Dict[str, Any], state: str, nz: int) -> dict:
        """Validate + resolve the scalar optical parameters (raises on missing)."""
        p = {**self.DEFAULT_PARAMS, **(params or {})}
        if nz < 2:
            raise ValueError(f"phase2d needs NZ > 1 defocus planes, got {nz}")
        if not ctx.dz_um:
            raise ValueError("phase2d: z step size (dz) unknown")
        if not ctx.pixel_size_um:
            raise ValueError("phase2d: pixel size unknown")
        wavelength_nm = p["wavelength_nm"] or ctx.state_meta.get(state, {}).get("wavelength_nm")
        if not wavelength_nm:
            raise ValueError(f"phase2d: illumination wavelength for {state!r} unknown — set it in the routine params")
        na_det = p["na_detection"]
        if not na_det:
            raise ValueError("phase2d: detection NA unknown — set it in the routine params")
        n_media = float(p["index_of_refraction_media"])
        return {
            "wavelength_um": float(wavelength_nm) / 1000.0,  # waveorder wants micrometers
            "yx_pixel_size_um": float(ctx.pixel_size_um),
            # Physical axial sampling in the medium = mechanical z step × RI.
            "z_pixel_size_um": float(ctx.dz_um) * n_media,
            "na_ill": float(p["na_illumination"] or na_det),
            "na_det": float(na_det),
            "n_media": n_media,
            "invert": bool(p["invert_phase_contrast"]),
            "regularization": float(p["regularization"]),
            "ref_z": reference_z_index(nz),
        }

    def _build_settings(self, o: dict):
        from waveorder.api import phase
        print(o)
        return phase.Settings(
            transfer_function=phase.TransferFunctionSettings(
                wavelength_illumination=o["wavelength_um"],
                yx_pixel_size=o["yx_pixel_size_um"],
                z_pixel_size=o["z_pixel_size_um"],
                z_focus_offset=0,
                numerical_aperture_illumination=o["na_ill"],
                numerical_aperture_detection=o["na_det"],
                index_of_refraction_media=o["n_media"],
                invert_phase_contrast=o["invert"],
            ),
            apply_inverse=phase.ApplyInverseSettings(
                regularization_strength=o["regularization"],
            ),
        )

    @staticmethod
    def _release_memory():
        """Drop freed Python objects and return torch's cached GPU blocks to the
        driver, so a subsequent (re)compute or the per-FOV reconstruction does not
        stack on top of the previous operation's peak working set."""
        import gc

        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def _get_transfer_function(self, ctx: PostprocessContext, o: dict, settings, yx_shape, nz: int):
        """Cached ``xr.Dataset`` transfer function for this geometry, computing it
        on first use. Shared by :meth:`warmup` (pre-acquisition) and
        :meth:`process` (per FOV) so the first FOV is a cache hit.

        The singular system is multi-GB at full frame size (e.g. Vh is
        ``(2, Z, Y, X)`` complex64 — ~3 GiB at 4168², plus the batched SVD's
        transient working set), so memory is managed carefully: a stale entry
        (e.g. a pre-acquisition warmup that guessed a different frame shape) is
        freed BEFORE the new one is built, the compute runs under ``no_grad``,
        and the SVD's transient GPU allocation is released afterwards.
        """
        import torch
        import xarray as xr
        from waveorder.api import phase

        cache_key = (
            tuple(yx_shape),
            nz,
            round(o["yx_pixel_size_um"], 5),
            round(o["z_pixel_size_um"], 5),
            round(o["wavelength_um"], 4),
            round(o["na_ill"], 3),
            round(o["na_det"], 3),
            round(o["n_media"], 3),
            o["invert"],
        )
        tf = ctx.cache.get(cache_key)
        if tf is not None:
            ctx.logger.debug("phase2d: transfer-function cache hit")
            return tf
        # Release any previously-cached transfer function (a different geometry,
        # e.g. a warmup that guessed the wrong frame shape) FIRST — holding the
        # old multi-GB singular system while building the new one is what drove
        # the host OOM. Keep only one transfer function resident at a time.
        ctx.cache.clear()
        self._release_memory()
        # compute_transfer_function only reads the CZYX shape, so a zeros DataArray
        # of the run geometry is enough to build the (data-independent) TF.
        czyx = xr.DataArray(
            np.zeros((1, nz) + tuple(yx_shape), dtype=np.float32), dims=("c", "z", "y", "x")
        )
        with torch.no_grad():
            tf = phase.compute_transfer_function(czyx, recon_dim=2, settings=settings, device="auto")
        # Return the SVD's transient GPU working set to the driver so it is not
        # left reserved on top of the per-FOV reconstruction.
        self._release_memory()
        ctx.cache[cache_key] = tf
        ctx.logger.info(
            "phase2d: computed transfer function (waveorder api, recon_dim=2, yx_shape=%s, nz=%d)",
            tuple(yx_shape),
            nz,
        )
        return tf

    def warmup(self, input_states, ctx, params):
        if not ctx.yx_shape:
            ctx.logger.debug("phase2d: no yx_shape for warmup; will compute lazily on first FOV")
            return
        (state,) = list(input_states)[:1] or [None]
        o = self._resolve(ctx, params, state, ctx.nz)
        settings = self._build_settings(o)
        self._get_transfer_function(ctx, o, settings, ctx.yx_shape, ctx.nz)

    def process(
        self, inputs: Dict[str, np.ndarray], ctx: PostprocessContext, params: Dict[str, Any]
    ) -> Dict[str, np.ndarray]:
        import torch
        import xarray as xr
        from waveorder.api import phase

        ((state, stack),) = inputs.items()
        zyx = np.asarray(stack)[0]  # (F=1, Z, Y, X) -> (Z, Y, X)
        nz, y, x = zyx.shape
        o = self._resolve(ctx, params, state, nz)
        settings = self._build_settings(o)
        tf = self._get_transfer_function(ctx, o, settings, (y, x), nz)

        input_da = xr.DataArray(
            zyx[None].astype(np.float32),  # (C=1, Z, Y, X)
            dims=("c", "z", "y", "x"),
            coords={
                "c": [state],
                "z": np.arange(nz, dtype=float),
                "y": np.arange(y, dtype=float),
                "x": np.arange(x, dtype=float),
            },
        )
        with torch.no_grad():
            result = phase.apply_inverse_transfer_function(
                input_da, tf, recon_dim=2, settings=settings, device="auto"
            )
            # recon_dim=2 output is a CZYX DataArray with a singleton Z -> (1, 1, Y, X).
            phase2d = np.asarray(result.values).reshape(y, x).astype(np.float32)
        bf_center = zyx[o["ref_z"]]
        # Free the reconstruction's GPU working set so VRAM doesn't creep across
        # FOVs (each apply re-uploads the singular system + FFT buffers).
        del result, input_da
        self._release_memory()
        return {
            "phase": phase2d,
            "bf_center": bf_center,
        }


ROUTINE = Phase2DRoutine()
