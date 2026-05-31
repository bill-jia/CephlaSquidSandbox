"""
Unit tests for the acquisition-cycle data model, resolver, and save layout.

All pure logic — no hardware, no I/O. These pin down the per-position plan
(event order, frame indices), the dense/ragged decision, and the dense T-fold /
ragged per-state save coordinates.
"""

import pytest

from control.models.acquisition_cycle import (
    AcquisitionCycle,
    CycleGroup,
    CycleStep,
    CycleWait,
    RegionPlan,
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
