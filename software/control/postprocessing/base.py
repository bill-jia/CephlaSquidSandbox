"""
Online postprocessing — routine contract for per-step/per-group compute during
acquisition.

A :class:`PostprocessRoutine` consumes **all frames one cycle item (step, FPM
sweep, or group) produces per FOV visit** and returns derived images that flow
to the normal writers; the raw input frames are *not* saved. Routines run in a
dedicated ``JobRunner`` subprocess (never the acquisition thread), so heavy
imports (``torch``, ``waveorder``) must happen lazily inside :meth:`process` —
the main process only imports the module to validate it pre-flight.

Custom user scripts implement the same contract: a ``.py`` file defining a
module-level ``ROUTINE = MyRoutine()`` instance (see ``registry.load_routine``).
"""

import abc
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Optional

import numpy as np
from pydantic import BaseModel, Field

# OutputSpec.dtype sentinel: the output inherits the dtype of the (first) input
# frame at write time (e.g. passing a raw camera slice through unchanged).
DTYPE_INPUT = "input"


class OutputSpec(BaseModel):
    """One derived image a routine emits per FOV visit.

    Declared up-front (via :meth:`PostprocessRoutine.describe_outputs`) so the
    save layer can key each output to its own single-channel plate before the
    first frame is captured. ``name`` is prefixed with the group label by the
    controller to form the on-disk array key (``{label}_{name}.ome.zarr``).
    """

    name: str
    z_size: int = Field(1, ge=1, description="Z extent of the output array (1 = a 2D image)")
    dtype: str = Field(
        "float32", description=f"numpy dtype name, or {DTYPE_INPUT!r} to inherit the input frame dtype"
    )
    channel_color: str = "#FFFFFF"
    wavelength_nm: Optional[int] = None

    model_config = {"extra": "forbid"}


@dataclass(frozen=True)
class InputStateSpec:
    """Static description of one input state feeding a routine invocation."""

    state: str
    acquire_z_stack: bool
    frames_per_visit: int  # occurrences of this state in the group per FOV visit (the F axis)


@dataclass
class PostprocessContext:
    """Runtime context handed to :meth:`PostprocessRoutine.process`.

    ``cache`` persists across FOVs for the lifetime of the postprocess
    subprocess (per routine instance) — use it for anything expensive that is
    constant across FOVs, e.g. a transfer function / singular system.
    """

    cache: Dict[Any, Any]
    logger: Any
    pixel_size_um: Optional[float]
    dz_um: Optional[float]
    nz: int
    nt: int
    z_positions_um: Optional[List[float]] = None
    # Per input state: {"wavelength_nm": ..., "exposure_ms": ...}
    state_meta: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Expected camera frame shape (Y, X) for this run — known before the first
    # capture, so ``warmup`` can precompute frame-shape-dependent state (e.g. a
    # transfer function) into ``cache`` with the same key ``process`` will use.
    yx_shape: Optional[tuple] = None


class PostprocessRoutine(abc.ABC):
    """Contract for an online postprocessing routine.

    ``inputs`` in :meth:`process` maps each input state name to an
    ``(F, Z, Y, X)`` float-or-integer array — F pooled occurrences per FOV
    visit (n_frames × repeats), Z the acquired planes (1 for a reference-z-only
    step). Returned arrays must be ``(Y, X)`` or ``(z_size, Y, X)`` matching
    the declared :class:`OutputSpec`.
    """

    name: ClassVar[str] = ""
    display_name: ClassVar[str] = ""

    @abc.abstractmethod
    def describe_outputs(self, input_states: Dict[str, InputStateSpec], params: Dict[str, Any]) -> List[OutputSpec]:
        """Declare the outputs for the given inputs, or raise ``ValueError``
        with a user-actionable message when the inputs don't fit the routine."""
        raise NotImplementedError

    @abc.abstractmethod
    def process(
        self, inputs: Dict[str, np.ndarray], ctx: PostprocessContext, params: Dict[str, Any]
    ) -> Dict[str, np.ndarray]:
        """Compute the derived images for one FOV visit, keyed by output name."""
        raise NotImplementedError

    def warmup(self, input_states: Dict[str, InputStateSpec], ctx: PostprocessContext, params: Dict[str, Any]) -> None:
        """Precompute FOV-shared state into ``ctx.cache`` before acquisition.

        Called once per run (before any hardware fires) so anything constant
        across FOVs — e.g. a transfer function that only depends on the geometry
        in ``ctx`` (``yx_shape``, ``dz_um``, ``nz``, pixel size) plus ``params`` —
        is ready and the first FOV's :meth:`process` is a cache hit rather than a
        multi-second stall. Default: no-op. Best-effort; a failure here is logged
        and the routine falls back to lazy computation on the first FOV.
        """
        return None
