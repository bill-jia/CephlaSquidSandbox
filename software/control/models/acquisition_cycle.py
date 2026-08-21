"""
Acquisition Cycle — an ordered, repeatable sequence of ObservationState steps
run *at a single position* before the stage moves on.

Where an :class:`~control.models.observation_state.ObservationState` is the
microscopy equivalent of a "channel" (one light-path configuration, one frame),
a Cycle composes several of them into a per-position temporal protocol:

  * **Step** — capture ``n_frames`` of one observation state (or, for a
    stimulus-only state, fire ``n_frames`` NIDAQ pulses with no camera frame).
  * **Group** — an ordered list of steps repeated ``repeat`` times. Exactly one
    level of grouping is allowed; the type structure itself caps nesting depth.
  * **Cycle** — an ordered list of items (steps and/or groups) repeated
    ``repeat`` times.

Multiple selected cycles run back-to-back at each position (chaining). A cycle
references observation states **by name** so it tracks preset edits.

Cycles are saved under the active profile (``cycles/*.yaml``), objective-free,
just like observation presets.

The resolution helpers in this module are pure (no hardware, no I/O) so they can
be unit-tested directly: they expand a cycle (or a chain of cycles) into a flat,
ordered list of :class:`ResolvedEvent` — the exact per-position acquisition plan
the worker iterates and the save layer keys on.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from pydantic import BaseModel, Field

from control.postprocessing.base import InputStateSpec, OutputSpec

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Serializable cycle definition
# ─────────────────────────────────────────────────────────────────────────────


class PostprocessSpec(BaseModel):
    """Online postprocessing assignment for a step / FPM sweep / group.

    The referenced routine consumes **all frames the item produces per FOV
    visit** and its declared outputs are saved instead of the raw frames (the
    inputs are never written to disk). ``routine`` is a built-in name from
    ``control.postprocessing.registry`` or the literal ``"script"`` with
    ``script_path`` pointing at a user ``.py`` defining a module-level
    ``ROUTINE`` instance.
    """

    routine: str = Field(..., description='Built-in routine name, or "script" for a custom script')
    script_path: Optional[str] = Field(None, description="Path to the custom routine .py (routine == 'script')")
    params: Dict[str, Any] = Field(default_factory=dict, description="Routine parameters (routine-defined keys)")
    label: Optional[str] = Field(
        None,
        description=(
            "Output-array-name prefix ({label}_{output}); defaults to the first input state name. "
            "Set it to disambiguate two groups using the same routine."
        ),
    )

    model_config = {"extra": "forbid"}


class CycleStep(BaseModel):
    """Capture ``n_frames`` of one observation state at the current position.

    For an imaged state this yields ``n_frames`` camera frames. For a
    stimulus-only state (``ObservationState.is_stimulus_only``) it yields
    ``n_frames`` NIDAQ stimulus pulses and no camera frame.
    """

    observation_state: str = Field(..., description="Observation-state preset name (by reference)")
    n_frames: int = Field(1, ge=1, description="Number of frames / pulses for this step")
    acquire_z_stack: bool = Field(
        True,
        description=(
            "If True (default), capture this step at every z-plane of the acquisition's "
            "z-stack. If False, capture only at the reference (focus/AF) plane — one z — "
            "which makes this step's frame count ragged vs full-z steps, so it is saved to "
            "its own single-z array (see array_key_for)."
        ),
    )
    postprocess: Optional[PostprocessSpec] = Field(
        None, description="Online postprocessing routine consuming this step's frames (raw frames not saved)"
    )

    model_config = {"extra": "forbid"}


class CycleWait(BaseModel):
    """Pause for ``duration_ms`` milliseconds at the current position.

    Produces no camera frame and no stimulus — purely a timed delay between
    events. Allowed at any nesting level (top-level item or inside a group), so
    its repeat count comes from the enclosing group / cycle repeat.
    """

    duration_ms: float = Field(..., ge=0, description="Wait duration in milliseconds")

    model_config = {"extra": "forbid"}


class CycleGroup(BaseModel):
    """An ordered list of steps/waits repeated ``repeat`` times (one nesting level).

    A group-level ``postprocess`` pools the frames of *all* member steps (across
    all repeats) into one routine invocation per FOV visit — this is how a
    multi-input routine (e.g. DPC from four half-circle steps) receives its
    inputs. Member steps must not carry their own ``postprocess``.
    """

    repeat: int = Field(1, ge=1, description="How many times to repeat this group")
    steps: List[Union[CycleStep, CycleWait]] = Field(default_factory=list)
    postprocess: Optional[PostprocessSpec] = Field(
        None, description="Online postprocessing pooling all member steps' frames (raw frames not saved)"
    )

    model_config = {"extra": "forbid"}


class CycleFPMDarkfield(BaseModel):
    """A source-coded Fourier-Ptychography darkfield acquisition.

    Expands at plan-build time into N **multiplexed darkfield** captures: the
    darkfield annulus ``[inner_na, outer_na]`` is tiled by the minimal set of
    darkfield LEDs whose pupils (radius = objective NA) overlap by >=
    ``min_overlap``, and that set is grouped into patterns of
    ``leds_per_pattern`` LEDs (random, seeded). Each pattern is one camera frame
    of the referenced base ``observation_state`` (its exposure/gain/color and the
    LED-matrix channel), with the LED set lit via the 'mux' matrix mode.

    The brightfield half of source-coded FPM (four DPC half-circles) is captured
    by adding ordinary ``CycleStep``s for the ``dpc.*`` modes — this item only
    generates the darkfield patterns. The expansion needs the live objective NA +
    the cached LED NA table, so it is supplied at resolve time by an injected
    provider (the model itself stays pure/serializable).
    """

    observation_state: str = Field(..., description="Base observation-state preset (optical config)")
    outer_na: float = Field(0.8, gt=0, description="Outer NA of the darkfield region")
    inner_na: Optional[float] = Field(
        None, description="Inner NA of the darkfield region (None => current objective NA)"
    )
    min_overlap: float = Field(0.6, gt=0, lt=1, description="Minimum Fourier pupil overlap")
    leds_per_pattern: int = Field(
        0, ge=0, description="LEDs lit per multiplexed darkfield frame (0 = auto/balanced from geometry)"
    )
    seed: int = Field(0, description="Seed for the (reproducible) random LED grouping")
    acquire_z_stack: bool = Field(
        True, description="Acquire every z-plane (True) or only the reference/focus plane (False); locked across the sweep."
    )
    postprocess: Optional[PostprocessSpec] = Field(
        None, description="Online postprocessing routine consuming this sweep's frames (raw frames not saved)"
    )

    model_config = {"extra": "forbid"}


class CycleFPMBrightfield(BaseModel):
    """A brightfield single-LED sweep for Fourier Ptychography.

    Expands at plan-build time into one frame per brightfield LED (illumination
    NA = sin(polar angle) <= objective NA), **single LED at a time**. With
    ``n_leds > 0`` only a reproducible **pseudorandom subset** of that size is
    sampled (for a faster, sparser brightfield set); ``n_leds = 0`` sweeps every
    brightfield LED. One base ``observation_state`` provides exposure/gain/color
    and the (on) LED-matrix channel. Pair with a darkfield item for a full run.
    """

    observation_state: str = Field(..., description="Base observation-state preset (optical config)")
    n_leds: int = Field(0, ge=0, description="Pseudorandom subset size (0 = every brightfield LED)")
    seed: int = Field(0, description="Seed for the pseudorandom subset")
    acquire_z_stack: bool = Field(
        True, description="Acquire every z-plane (True) or only the reference/focus plane (False); locked across the sweep."
    )
    postprocess: Optional[PostprocessSpec] = Field(
        None, description="Online postprocessing routine consuming this sweep's frames (raw frames not saved)"
    )

    model_config = {"extra": "forbid"}


class CycleFPMClusteredDarkfield(BaseModel):
    """An angle-clustered darkfield sweep for (3D/tomography) Fourier Ptychography.

    The objective-NA→``outer_na`` annulus is binned into angular **cells** at the
    ~``min_overlap`` Fourier-tiling step; each cell (a tight cluster of *adjacent*
    LEDs at nearly the same angle) is fired as one frame. Unlike the source-coded
    :class:`CycleFPMDarkfield` (random multiplexing), clustering keeps a frame's
    members co-located in angle, so each frame maps to one tight patch of the cap
    and the per-shell angular structure 3D reconstruction needs is preserved.
    Use a longer exposure than brightfield (SNR scales ~sqrt(cell size)).
    """

    observation_state: str = Field(..., description="Base observation-state preset (use a longer exposure)")
    outer_na: float = Field(0.8, gt=0, description="Outer NA of the darkfield region (clipped to the dome's reach)")
    inner_na: Optional[float] = Field(
        None, description="Inner NA / BF-DF boundary (None => current objective NA)"
    )
    min_overlap: float = Field(0.6, gt=0, lt=1, description="Fourier overlap setting the darkfield cell size")
    acquire_z_stack: bool = Field(
        True, description="Acquire every z-plane (True) or only the reference/focus plane (False); locked across the sweep."
    )
    postprocess: Optional[PostprocessSpec] = Field(
        None, description="Online postprocessing routine consuming this sweep's frames (raw frames not saved)"
    )

    model_config = {"extra": "forbid"}


class AcquisitionCycle(BaseModel):
    """A named, repeatable per-position acquisition sequence."""

    name: str
    version: int = 1
    repeat: int = Field(1, ge=1, description="How many times to repeat the whole cycle")
    items: List[
        Union[
            CycleGroup,
            CycleFPMBrightfield,
            CycleFPMClusteredDarkfield,
            CycleFPMDarkfield,
            CycleStep,
            CycleWait,
        ]
    ] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


# ─────────────────────────────────────────────────────────────────────────────
# Resolved (flattened) acquisition plan
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedEvent:
    """One concrete acquisition event in a flattened per-position plan.

    Attributes:
        observation_state: Preset name to apply for this event ("" for a wait).
        is_stimulus: True for a stimulus-only event (NIDAQ pulse, no frame).
        state_frame_index: Running per-state occurrence index across the whole
            chain (k-th frame of this state). For an imaged state this is the
            ``T`` coordinate within the position; for a stimulus state it is the
            k-th pulse. Starts at 0.
        cycle_event_index: Position of this event in the flat chain (0-based);
            preserves interleave / acquisition order across all selected cycles.
        is_wait: True for a timed-delay event (no frame, no stimulus).
        wait_ms: Delay duration in milliseconds (only meaningful when is_wait).
        multiplexed_leds: For a source-coded FPM darkfield frame, the explicit
            LED indices lit for this capture (None for ordinary events). The base
            observation state still provides exposure/gain/color; the worker lights
            this LED set via the 'mux' matrix mode before capturing.
        acquire_z_stack: True (default) to capture this event at every z-plane of
            the acquisition's z-stack; False to capture it only at the reference
            (focus/AF) plane. Carried from the originating CycleStep / FPM item.
        postprocess: The postprocessing spec of the originating item (or its
            enclosing group), None for a plain saved frame. A postprocessed
            frame is routed to the postprocess runner instead of the writers —
            it has no raw save coordinate.
        postprocess_group: Stable per-assignment instance id ("pp0", "pp1", …
            in first-appearance order). All occurrences of one item/group across
            its repeats share the id — they pool into one routine invocation per
            FOV visit.
    """

    observation_state: str
    is_stimulus: bool
    state_frame_index: int
    cycle_event_index: int
    is_wait: bool = False
    wait_ms: float = 0.0
    multiplexed_leds: Optional[Tuple[int, ...]] = None
    acquire_z_stack: bool = True
    postprocess: Optional[PostprocessSpec] = None
    postprocess_group: Optional[str] = None


# Suffix appended to a ragged array key for a reference-z-only ("single plane")
# capture, so the same observation state used both ways yields two distinct
# arrays: ``{state}.ome.zarr`` (Z=NZ, full stack) and ``{state}_refz.ome.zarr``
# (Z=1, reference plane only). Full-z keys are the bare state name, so all-full-z
# runs (the default) are byte-for-byte unchanged.
REFZ_ARRAY_SUFFIX = "_refz"


def array_key_for(observation_state: str, acquire_z_stack: bool) -> str:
    """Ragged array key for an imaged event of ``observation_state``.

    The key identifies the per-(state, z-mode) zarr plate: the bare state name
    for a full z-stack, or ``{state}{REFZ_ARRAY_SUFFIX}`` for a reference-plane-
    only capture. This is also the grouping key for frame counts and the
    dense/ragged decision, so a state captured both ways is correctly ragged.
    """
    return observation_state if acquire_z_stack else f"{observation_state}{REFZ_ARRAY_SUFFIX}"


# A predicate telling whether a named observation state is stimulus-only.
StimulusPredicate = Callable[[str], bool]

# Supplies the per-frame LED sets for an FPM item (CycleFPMDarkfield,
# CycleFPMBrightfield, or CycleFPMClusteredDarkfield). Injected at resolve time
# (closes over objective NA + cached LED NA table); the cycle model itself stays
# pure. Returns a list of ``(observation_state_name, led_indices)`` frames — one
# camera frame each.
FpmPatternProvider = Callable[[object], List[Tuple[str, Sequence[int]]]]


# A raw event is a tagged tuple (``pp`` = None or (PostprocessSpec, group_id)):
#   ("state", (preset_name, acquire_z_stack, pp))   — ordinary imaged/stimulus step
#   ("wait", duration_ms)                            — timed delay
#   ("mux", (preset_name, leds_tuple, acquire_z_stack, pp)) — one FPM frame
_RawEvent = Tuple[str, object]


class _PostprocessGroupAllocator:
    """Assigns stable ``pp{n}`` ids to postprocessed items/groups, numbered in
    first-appearance order. Keyed on item identity so every occurrence of one
    item across cycle/group repeats shares the id — all its frames pool into
    one routine invocation per FOV visit."""

    def __init__(self):
        self._by_item: Dict[int, str] = {}

    def key_for(self, item) -> str:
        key = self._by_item.get(id(item))
        if key is None:
            key = f"pp{len(self._by_item)}"
            self._by_item[id(item)] = key
        return key


def _own_pp(item, pp_alloc: Optional[_PostprocessGroupAllocator]):
    spec = getattr(item, "postprocess", None)
    if spec is None or pp_alloc is None:
        return None
    return (spec, pp_alloc.key_for(item))


def _expand_item(
    item,
    out: List[_RawEvent],
    fpm_provider: Optional[FpmPatternProvider] = None,
    pp_alloc: Optional[_PostprocessGroupAllocator] = None,
    inherited_pp=None,
) -> None:
    """Expand one cycle item (group / step / wait / fpm) into raw tagged events.

    ``inherited_pp`` carries an enclosing group's (spec, group_id) — a group-
    level postprocess pools all member steps' frames, overriding any (invalid,
    validation-rejected) member-level spec.
    """
    if isinstance(item, CycleGroup):
        pp = _own_pp(item, pp_alloc) or inherited_pp
        for _ in range(item.repeat):
            for sub in item.steps:
                _expand_item(sub, out, fpm_provider, pp_alloc, pp)
    elif isinstance(item, CycleWait):
        out.append(("wait", float(item.duration_ms)))
    elif isinstance(item, (CycleFPMDarkfield, CycleFPMBrightfield, CycleFPMClusteredDarkfield)):
        if fpm_provider is None:
            logger.warning(
                "FPM item %s encountered without an FPM pattern provider; no frames generated",
                type(item).__name__,
            )
            return
        az = bool(getattr(item, "acquire_z_stack", True))
        pp = inherited_pp or _own_pp(item, pp_alloc)
        for name, leds in fpm_provider(item) or []:
            out.append(("mux", (name, tuple(int(i) for i in leds), az, pp)))
    else:  # CycleStep
        az = bool(getattr(item, "acquire_z_stack", True))
        pp = inherited_pp or _own_pp(item, pp_alloc)
        out.extend([("state", (item.observation_state, az, pp))] * item.n_frames)


def _raw_events(
    cycle: AcquisitionCycle,
    fpm_provider: Optional[FpmPatternProvider] = None,
    pp_alloc: Optional[_PostprocessGroupAllocator] = None,
) -> List[_RawEvent]:
    """Expand a single cycle's outer repeat / groups / steps / waits into an
    ordered list of tagged raw events (one entry per event)."""
    out: List[_RawEvent] = []
    for _ in range(cycle.repeat):
        for item in cycle.items:
            _expand_item(item, out, fpm_provider, pp_alloc)
    return out


def _index_events(
    raw: List[_RawEvent],
    is_stimulus: Optional[StimulusPredicate] = None,
) -> List[ResolvedEvent]:
    """Assign per-state frame indices and chain positions to a raw event list.

    When ``is_stimulus`` is None every imaged event is treated as imaged (kind
    unknown). A given observation state is uniformly imaged or stimulus-only, so
    the per-state counter is consistent regardless. Wait events carry their
    duration and do not participate in per-state counting.
    """
    per_state: Dict[str, int] = {}
    events: List[ResolvedEvent] = []
    for position, (kind, payload) in enumerate(raw):
        if kind == "wait":
            events.append(
                ResolvedEvent(
                    observation_state="",
                    is_stimulus=False,
                    state_frame_index=0,
                    cycle_event_index=position,
                    is_wait=True,
                    wait_ms=float(payload),
                )
            )
            continue
        if kind == "mux":
            # Source-coded FPM darkfield frame: base preset name + LED index set.
            name, leds, az, pp = payload
            spec, gid = pp if pp is not None else (None, None)
            # Count per (state, z-mode) so each array's T runs 0..count-1, even
            # when the same state is captured both full-z and reference-only.
            # Postprocessed events count under their own namespaced key so a
            # state used both saved and postprocessed keeps contiguous saved T
            # indices (the pp ordinal orders the routine's input F axis).
            ckey = array_key_for(name, bool(az))
            if gid is not None:
                ckey = f"{ckey}#{gid}"
            k = per_state.get(ckey, 0)
            per_state[ckey] = k + 1
            events.append(
                ResolvedEvent(
                    observation_state=name,
                    is_stimulus=False,
                    state_frame_index=k,
                    cycle_event_index=position,
                    multiplexed_leds=tuple(leds),
                    acquire_z_stack=bool(az),
                    postprocess=spec,
                    postprocess_group=gid,
                )
            )
            continue
        # Flat (no-cycle) callers may hand-build 2-tuple ("state", (name, az))
        # events with no postprocessing; accept both shapes explicitly (a bare
        # string payload is a bug — reject it rather than unpacking its chars).
        if not isinstance(payload, (tuple, list)) or not (2 <= len(payload) <= 3):
            raise ValueError(f"invalid 'state' event payload: {payload!r}")
        name, az = payload[0], payload[1]
        pp = payload[2] if len(payload) == 3 else None
        stim = bool(is_stimulus(name)) if is_stimulus is not None else False
        # A stimulus-only event produces no camera frame — nothing to postprocess.
        spec, gid = (None, None) if (pp is None or stim) else pp
        ckey = array_key_for(name, bool(az))
        if gid is not None:
            ckey = f"{ckey}#{gid}"
        k = per_state.get(ckey, 0)
        per_state[ckey] = k + 1
        events.append(
            ResolvedEvent(
                observation_state=name,
                is_stimulus=stim,
                state_frame_index=k,
                cycle_event_index=position,
                acquire_z_stack=bool(az),
                postprocess=spec,
                postprocess_group=gid,
            )
        )
    return events


def resolve_cycle(
    cycle: AcquisitionCycle,
    is_stimulus: Optional[StimulusPredicate] = None,
    fpm_provider: Optional[FpmPatternProvider] = None,
) -> List[ResolvedEvent]:
    """Flatten a single cycle into its ordered event list."""
    return _index_events(_raw_events(cycle, fpm_provider, _PostprocessGroupAllocator()), is_stimulus)


def resolve_chain(
    cycle_names: List[str],
    load_cycle: Callable[[str], Optional[AcquisitionCycle]],
    is_stimulus: Optional[StimulusPredicate] = None,
    fpm_provider: Optional[FpmPatternProvider] = None,
) -> List[ResolvedEvent]:
    """Flatten a chain of selected cycles (run back-to-back) into one event list.

    ``load_cycle`` resolves a cycle name to its definition (e.g.
    ``repo.load_acquisition_cycle``). Unknown / empty cycles are skipped with a
    warning. Per-state frame indices and chain positions are numbered across the
    *whole* concatenated chain, not per cycle. ``fpm_provider`` supplies the
    multiplexed LED groups for any ``CycleFPMDarkfield`` items.
    """
    raw: List[_RawEvent] = []
    pp_alloc = _PostprocessGroupAllocator()  # one allocator ⇒ group ids unique across the chain
    for name in cycle_names:
        cycle = load_cycle(name)
        if cycle is None:
            logger.warning("Acquisition cycle %r not found, skipping", name)
            continue
        raw.extend(_raw_events(cycle, fpm_provider, pp_alloc))
    return _index_events(raw, is_stimulus)


def chain_frame_counts(events: List[ResolvedEvent]) -> Dict[str, int]:
    """Per-(state, z-mode) count of *imaged* frames across a resolved chain.

    Keyed by :func:`array_key_for` so a state captured both full-z and
    reference-only contributes two independent counts (its two arrays). For
    all-full-z runs the keys are bare state names — unchanged. Stimulus-only and
    wait events are excluded — they produce no saved frame and so do not
    participate in the dense/ragged layout decision.
    """
    counts: Dict[str, int] = {}
    for ev in events:
        if ev.is_stimulus or ev.is_wait or ev.postprocess is not None:
            continue
        key = array_key_for(ev.observation_state, ev.acquire_z_stack)
        counts[key] = counts.get(key, 0) + 1
    return counts


def is_dense(events: List[ResolvedEvent]) -> bool:
    """True if the chain folds into one regular ``T × C × Z`` array.

    Requires both (a) every imaged array-group has the same frame count, AND
    (b) a single z-mode across all imaged events — a stack that mixes full-z and
    reference-only captures has a non-uniform Z extent and so must be ragged
    (one array per group). An empty chain is trivially dense. Postprocessed
    events save no raw frame, so — like stimulus events — they don't participate
    (their derived outputs are always separate side arrays).
    """
    counts = chain_frame_counts(events)
    if len(set(counts.values())) > 1:
        return False
    z_modes = {
        ev.acquire_z_stack for ev in events if not (ev.is_stimulus or ev.is_wait or ev.postprocess is not None)
    }
    return len(z_modes) <= 1


def imaged_states_in_order(events: List[ResolvedEvent]) -> List[str]:
    """Distinct *saved* imaged observation-state names, in first-appearance order.

    This is the channel (``C``) axis ordering for the dense layout, so
    postprocessed events (no saved raw frame) are excluded.
    """
    seen: Dict[str, None] = {}
    for ev in events:
        if ev.is_stimulus or ev.is_wait or ev.postprocess is not None:
            continue
        if ev.observation_state not in seen:
            seen[ev.observation_state] = None
    return list(seen.keys())


def all_states_in_order(events: List[ResolvedEvent]) -> List[str]:
    """Distinct observation-state names (imaged *and* stimulus), first-appearance
    order. Used for the metadata record of which states a run touched."""
    seen: Dict[str, None] = {}
    for ev in events:
        if ev.is_wait:
            continue
        if ev.observation_state not in seen:
            seen[ev.observation_state] = None
    return list(seen.keys())


# ─────────────────────────────────────────────────────────────────────────────
# Per-region acquisition plan + save layout (pure, unit-testable)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class PostprocessGroupPlan:
    """One postprocess assignment (step / sweep / group), resolved for a region.

    ``outputs`` is declared by the routine's ``describe_outputs`` and filled in
    by the controller (``_attach_postprocess_outputs``) so this module stays
    free of routine imports. ``label`` (spec override or the first input state
    name) prefixes output names to form the on-disk array keys.
    """

    group_key: str
    spec: PostprocessSpec
    input_states: Dict[str, InputStateSpec]  # first-appearance order
    label: str
    outputs: List[OutputSpec] = field(default_factory=list)

    @property
    def output_keys(self) -> List[str]:
        """On-disk array keys of the declared outputs (``{label}_{name}``)."""
        return [f"{self.label}_{o.name}" for o in self.outputs]

    @property
    def events_per_visit(self) -> int:
        """Imaged input events per FOV visit (before the per-event z extent)."""
        return sum(s.frames_per_visit for s in self.input_states.values())


def _build_postprocess_groups(events: List[ResolvedEvent]) -> Dict[str, PostprocessGroupPlan]:
    per_group_states: Dict[str, Dict[str, List[bool]]] = {}
    specs: Dict[str, PostprocessSpec] = {}
    for ev in events:
        gid = ev.postprocess_group
        if gid is None or ev.is_stimulus or ev.is_wait:
            continue
        specs.setdefault(gid, ev.postprocess)
        per_group_states.setdefault(gid, {}).setdefault(ev.observation_state, []).append(ev.acquire_z_stack)
    groups: Dict[str, PostprocessGroupPlan] = {}
    used_labels: Dict[str, str] = {}  # label -> gid that claimed it
    for gid, states in per_group_states.items():
        spec = specs[gid]
        input_states = {
            name: InputStateSpec(state=name, acquire_z_stack=z_modes[0], frames_per_visit=len(z_modes))
            for name, z_modes in states.items()
        }
        # An unlabelled group falls back to its first input state. Two cycles in one
        # chain running the same routine over the same states (e.g. the same DPC group
        # copied into two cycles) would then derive the same label and fight over one
        # output plate, so suffix the group id to keep the derived plates distinct.
        # An *explicit* label is left alone: a genuine collision there is the user's to
        # resolve, and _attach_postprocess_outputs reports it.
        label = spec.label
        if not label:
            label = next(iter(input_states))
            if label in used_labels:
                label = f"{label}_{gid}"
        used_labels.setdefault(label, gid)
        groups[gid] = PostprocessGroupPlan(
            group_key=gid,
            spec=spec,
            input_states=input_states,
            label=label,
        )
    return groups


@dataclass
class RegionPlan:
    """Everything the worker and save layer need for one region.

    Built once by the controller from the region's selected cycles (or a bare
    observation-state selection, which is just a chain of 1-frame events).
    """

    events: List[ResolvedEvent]          # flat per-position acquisition order (incl. stimulus)
    dense: bool                          # all imaged states share the same frame count
    frame_counts: Dict[str, int]         # imaged state -> frames per position
    channel_order: List[str]             # distinct imaged states, in C-axis order
    # Postprocess assignments keyed by group id ("pp0", …). Empty for plain runs.
    postprocess_groups: Dict[str, PostprocessGroupPlan] = field(default_factory=dict)

    @staticmethod
    def from_events(events: List[ResolvedEvent]) -> "RegionPlan":
        return RegionPlan(
            events=list(events),
            dense=is_dense(events),
            frame_counts=chain_frame_counts(events),
            channel_order=imaged_states_in_order(events),
            postprocess_groups=_build_postprocess_groups(events),
        )

    @property
    def frames_per_position(self) -> int:
        """Total *saved* imaged frames at one position per scan timepoint."""
        return sum(self.frame_counts.values())

    @property
    def captured_frames_per_position(self) -> int:
        """Total camera frames captured at one position per scan timepoint —
        saved *and* postprocessed (progress/ETA accounting)."""
        return sum(1 for ev in self.events if not (ev.is_stimulus or ev.is_wait))

    @property
    def array_keys(self) -> List[str]:
        """Distinct *raw* ragged plate keys (``array_key_for`` values), in order.

        One per (state, z-mode) array. Unlike ``channel_order`` (bare state names
        for the dense C axis / omero labels), these carry the ``_refz`` suffix for
        reference-only captures, so they match the actual on-disk plate names the
        upload barrier must flush. For an all-full-z run this equals
        ``channel_order``. Postprocessed events save no raw plate — their derived
        plates are listed by :attr:`derived_output_keys` instead.
        """
        seen: Dict[str, None] = {}
        for ev in self.events:
            if ev.is_stimulus or ev.is_wait or ev.postprocess is not None:
                continue
            seen.setdefault(array_key_for(ev.observation_state, ev.acquire_z_stack), None)
        return list(seen.keys())

    @property
    def derived_output_keys(self) -> List[str]:
        """On-disk array keys of all postprocess outputs, in group order."""
        return [key for group in self.postprocess_groups.values() for key in group.output_keys]


@dataclass(frozen=True)
class FrameCoord:
    """Where one imaged event's frame lands in the saved arrays.

    ``array_key`` is ``None`` for the dense layout (one ``TZCYX`` array per FOV)
    and the observation-state name for the ragged layout (one array per
    ``(FOV, state)``). ``t_index`` / ``c_index`` are the coordinates within that
    array; ``t_size`` / ``c_size`` are its full extents (so a frame fully
    describes the array it belongs to, without a global uniform assumption).
    """

    array_key: Optional[str]
    t_index: int
    c_index: int
    t_size: int
    c_size: int


@dataclass(frozen=True)
class SaveLayout:
    """Fully self-describing save target for one imaged frame.

    Carries the array identity, coordinates, extents, and per-array channel
    metadata so the save layer never has to consult a global uniform
    ``(T, C, Z)`` assumption — which per-region cycles and ragged counts break.
    """

    array_key: Optional[str]                 # None=dense single array; (state[,_refz]) ragged plate
    t_index: int
    c_index: int
    t_size: int
    c_size: int
    cycle_event_index: int                   # position in the flat chain (acquisition order)
    state_frame_index: int                   # k-th frame of this state at this position
    channel_names: List[str]                 # omero channel labels for this array
    channel_colors: List[str]
    channel_wavelengths: List[Optional[int]]
    # Z extent of this frame's array: the full stack (NZ) for a normal step, or 1
    # for a reference-z-only capture. None => the writer falls back to the global
    # z_size (legacy / non-cycle path). Set by the worker (NZ is a worker concept).
    # The per-frame z *index* is carried separately (CaptureInfo.z_index): the
    # worker writes a reference-z-only frame at z=0 of its Z=1 array.
    z_size: Optional[int] = None
    # Disambiguating basename suffix for per-frame file formats (INDIVIDUAL_IMAGES),
    # set only when this state captures >1 frame per position so simple-case
    # filenames are unchanged. None = no suffix.
    frame_suffix: Optional[str] = None


def frame_coord(plan: RegionPlan, Nt: int, t_scan: int, event: ResolvedEvent) -> FrameCoord:
    """Compute the save coordinate for one imaged ``event`` at scan timepoint ``t_scan``.

    Dense: imaged frames fold into ``T`` (``T = Nt × frames_per_state``), ``C`` =
    channel index. Ragged: each state is its own array (``C`` size 1) with
    ``T = Nt × that_state's_count``. ``t_scan`` blocks stack along ``T`` so a
    scan-level timelapse runs on top of the per-position cycle.

    Raises ``ValueError`` for a stimulus or wait event (no frame to place).
    """
    if event.is_stimulus or event.is_wait:
        raise ValueError("stimulus/wait events have no frame coordinate")
    if event.postprocess is not None:
        raise ValueError("postprocessed events save no raw frame and have no frame coordinate")
    name = event.observation_state
    ckey = array_key_for(name, event.acquire_z_stack)
    count = plan.frame_counts[ckey]
    k = event.state_frame_index
    if plan.dense:
        # Dense ⟹ a single z-mode, so each state maps to exactly one array-group;
        # all imaged states share `count`. T interleaves scan blocks of size `count`.
        return FrameCoord(
            array_key=None,
            t_index=t_scan * count + k,
            c_index=plan.channel_order.index(name),
            t_size=Nt * count,
            c_size=len(plan.channel_order),
        )
    # Ragged: one array per (state, z-mode); the key carries the _refz suffix for
    # reference-only captures so a state used both ways yields two arrays.
    return FrameCoord(
        array_key=ckey,
        t_index=t_scan * count + k,
        c_index=0,
        t_size=Nt * count,
        c_size=1,
    )
