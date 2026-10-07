"""
Unit tests for the acquisition-cycle data model, resolver, and save layout.

All pure logic — no hardware, no I/O. These pin down the per-position plan
(event order, frame indices), the dense/ragged decision, and the dense T-fold /
ragged per-state save coordinates.
"""

import pytest

from control.models.acquisition_cycle import (
    AcquisitionCycle,
    CycleFPMBrightfield,
    CycleFPMClusteredDarkfield,
    CycleFPMDarkfield,
    CycleGroup,
    CycleStep,
    CycleWait,
    RegionPlan,
    _index_events,
    all_states_in_order,
    chain_frame_counts,
    frame_coord,
    imaged_states_in_order,
    is_dense,
    resolve_chain,
    resolve_cycle,
)


def _names(events):
    return [e.observation_state for e in events]


class TestResolveCycle:
    def test_flat_steps_one_frame_each(self):
        cyc = AcquisitionCycle(name="c", items=[CycleStep(observation_state="GFP"), CycleStep(observation_state="RFP")])
        ev = resolve_cycle(cyc)
        assert _names(ev) == ["GFP", "RFP"]
        assert [e.cycle_event_index for e in ev] == [0, 1]
        assert [e.state_frame_index for e in ev] == [0, 0]

    def test_n_frames_expands(self):
        cyc = AcquisitionCycle(name="c", items=[CycleStep(observation_state="GFP", n_frames=3)])
        ev = resolve_cycle(cyc)
        assert _names(ev) == ["GFP", "GFP", "GFP"]
        assert [e.state_frame_index for e in ev] == [0, 1, 2]

    def test_outer_repeat(self):
        cyc = AcquisitionCycle(
            name="c", repeat=2, items=[CycleStep(observation_state="A"), CycleStep(observation_state="B")]
        )
        ev = resolve_cycle(cyc)
        assert _names(ev) == ["A", "B", "A", "B"]
        # per-state frame index keeps counting across outer repeats
        assert [e.state_frame_index for e in ev] == [0, 0, 1, 1]

    def test_group_repeat_nested_one_level(self):
        cyc = AcquisitionCycle(
            name="c",
            items=[
                CycleGroup(repeat=3, steps=[CycleStep(observation_state="GFP"), CycleStep(observation_state="stim")]),
                CycleStep(observation_state="RFP", n_frames=2),
            ],
        )
        ev = resolve_cycle(cyc)
        assert _names(ev) == ["GFP", "stim", "GFP", "stim", "GFP", "stim", "RFP", "RFP"]

    def test_stimulus_predicate_marks_events(self):
        cyc = AcquisitionCycle(
            name="c", items=[CycleStep(observation_state="GFP"), CycleStep(observation_state="stim")]
        )
        ev = resolve_cycle(cyc, is_stimulus=lambda n: n == "stim")
        kinds = {e.observation_state: e.is_stimulus for e in ev}
        assert kinds == {"GFP": False, "stim": True}


class TestChainAndDensity:
    def test_resolve_chain_concatenates(self):
        cycles = {
            "c1": AcquisitionCycle(name="c1", items=[CycleStep(observation_state="GFP", n_frames=2)]),
            "c2": AcquisitionCycle(name="c2", items=[CycleStep(observation_state="RFP")]),
        }
        ev = resolve_chain(["c1", "c2"], cycles.get)
        assert _names(ev) == ["GFP", "GFP", "RFP"]
        assert [e.cycle_event_index for e in ev] == [0, 1, 2]

    def test_unknown_cycle_skipped(self):
        ev = resolve_chain(["missing"], lambda n: None)
        assert ev == []

    def test_dense_equal_counts(self):
        cyc = AcquisitionCycle(
            name="c", repeat=3, items=[CycleStep(observation_state="GFP"), CycleStep(observation_state="RFP")]
        )
        ev = resolve_cycle(cyc)
        assert chain_frame_counts(ev) == {"GFP": 3, "RFP": 3}
        assert is_dense(ev) is True

    def test_ragged_unequal_counts(self):
        cyc = AcquisitionCycle(
            name="c",
            items=[CycleStep(observation_state="GFP", n_frames=10), CycleStep(observation_state="RFP", n_frames=5)],
        )
        ev = resolve_cycle(cyc)
        assert chain_frame_counts(ev) == {"GFP": 10, "RFP": 5}
        assert is_dense(ev) is False

    def test_density_ignores_stimulus(self):
        # GFP x2, RFP x2 imaged + stim x5 -> still dense (stim excluded)
        cyc = AcquisitionCycle(
            name="c",
            items=[
                CycleStep(observation_state="GFP", n_frames=2),
                CycleStep(observation_state="RFP", n_frames=2),
                CycleStep(observation_state="stim", n_frames=5),
            ],
        )
        ev = resolve_cycle(cyc, is_stimulus=lambda n: n == "stim")
        assert chain_frame_counts(ev) == {"GFP": 2, "RFP": 2}
        assert is_dense(ev) is True
        assert imaged_states_in_order(ev) == ["GFP", "RFP"]
        assert all_states_in_order(ev) == ["GFP", "RFP", "stim"]

    def test_empty_chain_is_dense(self):
        assert is_dense([]) is True


class TestFrameCoordDense:
    def _plan(self):
        cyc = AcquisitionCycle(
            name="c", repeat=2, items=[CycleStep(observation_state="GFP"), CycleStep(observation_state="RFP")]
        )
        return RegionPlan.from_events(resolve_cycle(cyc))

    def test_dense_folds_frames_into_T(self):
        plan = self._plan()
        assert plan.dense is True
        assert plan.frames_per_position == 4  # GFP x2 + RFP x2
        # imaged events only
        imaged = [e for e in plan.events if not e.is_stimulus]
        coords = [frame_coord(plan, Nt=3, t_scan=0, event=e) for e in imaged]
        # GFP frames 0,1 -> c=0 t=0,1 ; RFP frames 0,1 -> c=1 t=0,1
        assert [(c.array_key, c.c_index, c.t_index) for c in coords] == [
            (None, 0, 0),
            (None, 1, 0),
            (None, 0, 1),
            (None, 1, 1),
        ]
        # T size = Nt * frames_per_state(=2); C size = 2 channels
        assert all(c.t_size == 6 and c.c_size == 2 for c in coords)

    def test_scan_timepoint_blocks_stack_on_T(self):
        plan = self._plan()
        gfp1 = [e for e in plan.events if e.observation_state == "GFP"][1]  # state_frame_index 1
        c0 = frame_coord(plan, Nt=3, t_scan=0, event=gfp1)
        c1 = frame_coord(plan, Nt=3, t_scan=1, event=gfp1)
        assert c0.t_index == 1
        assert c1.t_index == 3  # t_scan=1 block: 1*2 + 1


class TestFrameCoordRagged:
    def _plan(self):
        cyc = AcquisitionCycle(
            name="c",
            items=[CycleStep(observation_state="GFP", n_frames=10), CycleStep(observation_state="RFP", n_frames=5)],
        )
        return RegionPlan.from_events(resolve_cycle(cyc))

    def test_ragged_per_state_arrays(self):
        plan = self._plan()
        assert plan.dense is False
        gfp = [e for e in plan.events if e.observation_state == "GFP"]
        rfp = [e for e in plan.events if e.observation_state == "RFP"]
        cg = frame_coord(plan, Nt=2, t_scan=0, event=gfp[7])
        cr = frame_coord(plan, Nt=2, t_scan=0, event=rfp[3])
        # each state its own single-channel array; t = its own frame index
        assert cg.array_key == "GFP" and cg.c_index == 0 and cg.c_size == 1
        assert cg.t_index == 7 and cg.t_size == 2 * 10
        assert cr.array_key == "RFP" and cr.c_index == 0 and cr.c_size == 1
        assert cr.t_index == 3 and cr.t_size == 2 * 5

    def test_stimulus_event_has_no_coord(self):
        cyc = AcquisitionCycle(name="c", items=[CycleStep(observation_state="stim")])
        plan = RegionPlan.from_events(resolve_cycle(cyc, is_stimulus=lambda n: True))
        with pytest.raises(ValueError):
            frame_coord(plan, Nt=1, t_scan=0, event=plan.events[0])


class TestWaits:
    def test_top_level_wait(self):
        cyc = AcquisitionCycle(
            name="c",
            items=[CycleStep(observation_state="GFP"), CycleWait(duration_ms=500.0), CycleStep(observation_state="RFP")],
        )
        ev = resolve_cycle(cyc)
        assert [(_n(e), e.is_wait, e.wait_ms) for e in ev] == [
            ("GFP", False, 0.0),
            ("", True, 500.0),
            ("RFP", False, 0.0),
        ]
        # cycle_event_index still counts the wait slot
        assert [e.cycle_event_index for e in ev] == [0, 1, 2]

    def test_wait_nested_in_group_repeats(self):
        cyc = AcquisitionCycle(
            name="c",
            items=[
                CycleGroup(
                    repeat=2,
                    steps=[CycleStep(observation_state="GFP"), CycleWait(duration_ms=250.0)],
                )
            ],
        )
        ev = resolve_cycle(cyc)
        kinds = [(_n(e), e.is_wait) for e in ev]
        assert kinds == [("GFP", False), ("", True), ("GFP", False), ("", True)]
        assert [e.wait_ms for e in ev if e.is_wait] == [250.0, 250.0]

    def test_waits_excluded_from_counts_density_channels(self):
        cyc = AcquisitionCycle(
            name="c",
            items=[
                CycleStep(observation_state="GFP", n_frames=2),
                CycleWait(duration_ms=100.0),
                CycleStep(observation_state="RFP", n_frames=2),
            ],
        )
        ev = resolve_cycle(cyc)
        assert chain_frame_counts(ev) == {"GFP": 2, "RFP": 2}  # "" not counted
        assert is_dense(ev) is True
        assert imaged_states_in_order(ev) == ["GFP", "RFP"]
        assert all_states_in_order(ev) == ["GFP", "RFP"]  # wait is not a state
        plan = RegionPlan.from_events(ev)
        assert plan.frames_per_position == 4

    def test_wait_event_has_no_coord(self):
        cyc = AcquisitionCycle(name="c", items=[CycleWait(duration_ms=10.0)])
        plan = RegionPlan.from_events(resolve_cycle(cyc))
        with pytest.raises(ValueError):
            frame_coord(plan, Nt=1, t_scan=0, event=plan.events[0])


def _n(e):
    return e.observation_state


class TestFPMDarkfield:
    """Source-coded FPM darkfield item: provider-driven expansion to multiplexed
    frames, threading of the LED index set, round-trip, and plan layout."""

    @staticmethod
    def _provider(item):
        # Deterministic fake: three (base_state, led_set) frames.
        name = item.observation_state
        return [(name, (10, 11, 12)), (name, (13, 14, 15)), (name, (16, 17))]

    def test_expands_via_provider(self):
        cyc = AcquisitionCycle(
            name="fpm",
            items=[
                CycleStep(observation_state="dpc_l"),
                CycleFPMDarkfield(observation_state="fpm_df", outer_na=0.8, leds_per_pattern=8, seed=0),
            ],
        )
        ev = resolve_cycle(cyc, fpm_provider=self._provider)
        # One DPC step + three multiplexed darkfield frames.
        assert _names(ev) == ["dpc_l", "fpm_df", "fpm_df", "fpm_df"]
        mux = [e for e in ev if e.multiplexed_leds is not None]
        assert [e.multiplexed_leds for e in mux] == [(10, 11, 12), (13, 14, 15), (16, 17)]
        # Multiplexed frames count as frames of the base state (T axis).
        assert [e.state_frame_index for e in mux] == [0, 1, 2]
        assert all(e.observation_state == "fpm_df" for e in mux)
        assert all(not e.is_stimulus for e in mux)

    def test_no_provider_yields_no_frames(self):
        cyc = AcquisitionCycle(
            name="fpm",
            items=[CycleStep(observation_state="dpc_l"), CycleFPMDarkfield(observation_state="fpm_df")],
        )
        ev = resolve_cycle(cyc)  # provider omitted
        assert _names(ev) == ["dpc_l"]

    def test_plan_is_ragged_with_per_pattern_frames(self):
        cyc = AcquisitionCycle(
            name="fpm",
            items=[
                CycleStep(observation_state="dpc_l"),
                CycleFPMDarkfield(observation_state="fpm_df"),
            ],
        )
        ev = resolve_cycle(cyc, fpm_provider=self._provider)
        plan = RegionPlan.from_events(ev)
        assert plan.frame_counts == {"dpc_l": 1, "fpm_df": 3}
        assert plan.dense is False  # ragged: counts differ
        assert plan.channel_order == ["dpc_l", "fpm_df"]

    def test_resolve_chain_threads_provider(self):
        cycles = {
            "c": AcquisitionCycle(name="c", items=[CycleFPMDarkfield(observation_state="fpm_df")]),
        }
        ev = resolve_chain(["c"], cycles.get, fpm_provider=self._provider)
        assert len([e for e in ev if e.multiplexed_leds]) == 3

    def test_model_round_trip_disambiguates_from_step(self):
        cyc = AcquisitionCycle(
            name="fpm",
            items=[
                CycleStep(observation_state="dpc_l", n_frames=2),
                CycleFPMDarkfield(observation_state="fpm_df", outer_na=0.75, inner_na=0.3, seed=5),
            ],
        )
        restored = AcquisitionCycle.model_validate(cyc.model_dump(mode="json"))
        assert isinstance(restored.items[0], CycleStep)
        assert isinstance(restored.items[1], CycleFPMDarkfield)
        assert restored.items[1].outer_na == 0.75
        assert restored.items[1].inner_na == 0.3
        assert restored.items[1].seed == 5


class TestFPMBrightfieldAndClustered:
    """Full routine as two composable single-base-state items: BF sweep + clustered DF."""

    @staticmethod
    def _provider(item):
        # BF item -> single-LED frames; clustered-DF item -> co-located cell frames.
        if isinstance(item, CycleFPMBrightfield):
            return [(item.observation_state, (0,)), (item.observation_state, (1,))]
        return [(item.observation_state, (10, 11, 12)), (item.observation_state, (13, 14))]

    def test_compose_bf_then_clustered_df(self):
        cyc = AcquisitionCycle(
            name="full",
            items=[
                CycleFPMBrightfield(observation_state="BF"),
                CycleFPMClusteredDarkfield(observation_state="DF"),
            ],
        )
        ev = resolve_cycle(cyc, fpm_provider=self._provider)
        assert _names(ev) == ["BF", "BF", "DF", "DF"]
        assert [e.multiplexed_leds for e in ev] == [(0,), (1,), (10, 11, 12), (13, 14)]
        assert [e.state_frame_index for e in ev] == [0, 1, 0, 1]
        plan = RegionPlan.from_events(ev)
        assert plan.frame_counts == {"BF": 2, "DF": 2}
        assert plan.channel_order == ["BF", "DF"]

    def test_round_trip_disambiguates_all_fpm_types(self):
        cyc = AcquisitionCycle(
            name="full",
            items=[
                CycleFPMBrightfield(observation_state="BF", n_leds=50, seed=7),
                CycleFPMClusteredDarkfield(observation_state="DF", outer_na=0.7, inner_na=0.35, min_overlap=0.65),
                CycleFPMDarkfield(observation_state="rand", outer_na=0.8, seed=2),
                CycleStep(observation_state="dpc_l"),
            ],
        )
        restored = AcquisitionCycle.model_validate(cyc.model_dump(mode="json"))
        assert isinstance(restored.items[0], CycleFPMBrightfield)
        assert isinstance(restored.items[1], CycleFPMClusteredDarkfield)
        assert isinstance(restored.items[2], CycleFPMDarkfield)
        assert isinstance(restored.items[3], CycleStep)
        assert restored.items[0].observation_state == "BF"
        assert restored.items[0].n_leds == 50 and restored.items[0].seed == 7
        assert restored.items[1].outer_na == 0.7 and restored.items[1].min_overlap == 0.65


class TestZModeLayout:
    """Per-step / per-FPM acquire_z_stack: reference-z-only captures become their
    own single-z (state, z-mode) array; full-z runs are unchanged."""

    @staticmethod
    def _plan(steps):
        return RegionPlan.from_events(resolve_cycle(AcquisitionCycle(name="t", items=steps)))

    def test_all_full_z_unchanged(self):
        p = self._plan([CycleStep(observation_state="GFP"), CycleStep(observation_state="RFP")])
        assert p.dense is True
        assert p.frame_counts == {"GFP": 1, "RFP": 1}
        assert p.array_keys == ["GFP", "RFP"]  # no suffix => backward compatible

    def test_single_reference_only_is_dense_single_z(self):
        # Uniform z-mode (all ref) => still dense; keyed with the _refz suffix.
        p = self._plan([CycleStep(observation_state="GFP", acquire_z_stack=False)])
        assert p.dense is True
        assert p.frame_counts == {"GFP_refz": 1}
        fc = frame_coord(p, Nt=1, t_scan=0, event=p.events[0])
        assert fc.array_key is None  # single dense channel

    def test_mixed_z_mode_is_ragged(self):
        p = self._plan([
            CycleStep(observation_state="GFP"),
            CycleStep(observation_state="RFP", acquire_z_stack=False),
        ])
        assert p.dense is False  # mixed Z extent must be ragged
        assert p.array_keys == ["GFP", "RFP_refz"]
        assert p.channel_order == ["GFP", "RFP"]  # C-axis labels stay state names
        coords = {e.observation_state: frame_coord(p, 1, 0, e) for e in p.events}
        assert coords["GFP"].array_key == "GFP"
        assert coords["RFP"].array_key == "RFP_refz"

    def test_same_state_both_ways_two_arrays(self):
        p = self._plan([
            CycleStep(observation_state="DAPI", n_frames=2),
            CycleStep(observation_state="DAPI", n_frames=2, acquire_z_stack=False),
        ])
        assert p.dense is False
        assert p.frame_counts == {"DAPI": 2, "DAPI_refz": 2}
        full = [frame_coord(p, 1, 0, e) for e in p.events if e.acquire_z_stack]
        ref = [frame_coord(p, 1, 0, e) for e in p.events if not e.acquire_z_stack]
        assert [c.array_key for c in full] == ["DAPI", "DAPI"]
        assert [c.t_index for c in full] == [0, 1]
        assert [c.array_key for c in ref] == ["DAPI_refz", "DAPI_refz"]
        assert [c.t_index for c in ref] == [0, 1]  # independent T per array

    def test_fpm_item_flag_propagates_to_events(self):
        events = resolve_cycle(
            AcquisitionCycle(name="t", items=[
                CycleFPMBrightfield(observation_state="BF", n_leds=3, acquire_z_stack=False),
            ]),
            fpm_provider=lambda item: [("BF", [0]), ("BF", [1]), ("BF", [2])],
        )
        assert len(events) == 3
        assert all(e.acquire_z_stack is False for e in events)  # locked across the sweep

    def test_roundtrip_preserves_flag(self):
        cyc = AcquisitionCycle(name="c", items=[
            CycleStep(observation_state="GFP", acquire_z_stack=False),
            CycleFPMDarkfield(observation_state="rand", acquire_z_stack=False),
        ])
        restored = AcquisitionCycle.model_validate(cyc.model_dump(mode="json"))
        assert restored.items[0].acquire_z_stack is False
        assert restored.items[1].acquire_z_stack is False


class TestFlatSelectionRawEvents:
    """The flat (no-cycle) path in MultiPointController._resolve_plan /
    MultiPointWorker hand-builds raw ('state', (name, az)) events. _index_events
    requires the (name, acquire_z_stack) tuple payload — passing a bare name
    (the prior regression) raised 'too many values to unpack'.
    """

    def test_flat_state_tuples_resolve(self):
        names = ["Teensy_BF_Full", "D900_mKO_Toupcam_refz", "D900_mVenus_Toupcam_refz"]
        plan = RegionPlan.from_events(_index_events([("state", (n, True)) for n in names]))
        assert plan.frames_per_position == len(names)
        assert plan.array_keys == names  # az=True => full-z arrays, no _refz suffix

    def test_bare_name_payload_is_rejected(self):
        # Guards the regression: a flat selection MUST wrap names as (name, az).
        with pytest.raises(ValueError):
            _index_events([("state", "GFP")])


class TestPostprocessGroupLabels:
    """Two cycles selected together must not fight over one derived output plate.

    Regression: two cycles each carrying an unlabelled DPC group over the same
    input states both derived the label "DPC_circ_bot", so both claimed the
    "DPC_circ_bot_phase" plate and _attach_postprocess_outputs raised — from a
    plain GUI selection change.
    """

    @staticmethod
    def _dpc_cycle(name):
        from control.models.acquisition_cycle import PostprocessSpec

        return AcquisitionCycle(
            name=name,
            items=[
                CycleGroup(
                    steps=[CycleStep(observation_state=s) for s in ("bot", "left", "right", "top")],
                    postprocess=PostprocessSpec(routine="dpc2d"),
                ),
                CycleStep(observation_state="GFP"),
            ],
        )

    def _plan(self, names):
        cycles = {n: self._dpc_cycle(n) for n in names}
        return RegionPlan.from_events(resolve_chain(names, cycles.get))

    def test_single_cycle_uses_first_input_state(self):
        plan = self._plan(["a"])
        assert [g.label for g in plan.postprocess_groups.values()] == ["bot"]

    def test_two_identical_cycles_get_distinct_labels(self):
        plan = self._plan(["a", "b"])
        labels = [g.label for g in plan.postprocess_groups.values()]
        assert labels == ["bot", "bot_pp1"]
        assert len(set(labels)) == len(labels)

    def test_explicit_label_is_never_rewritten(self):
        from control.models.acquisition_cycle import PostprocessSpec

        cycles = {}
        for n in ("a", "b"):
            cyc = self._dpc_cycle(n)
            cyc.items[0].postprocess = PostprocessSpec(routine="dpc2d", label="mine")
            cycles[n] = cyc
        plan = RegionPlan.from_events(resolve_chain(["a", "b"], cycles.get))
        # Left alone so the controller can report the collision actionably.
        assert [g.label for g in plan.postprocess_groups.values()] == ["mine", "mine"]
