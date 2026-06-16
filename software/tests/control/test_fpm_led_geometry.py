"""
Unit tests for source-coded FPM darkfield LED selection (pure geometry).

No hardware, no I/O beyond a temp CSV round-trip. These pin down the Fourier
overlap math, the darkfield selection (coverage + thinning + overlap guarantee),
and the deterministic multiplexed grouping.
"""

import math

import pytest

from control import fpm_led_geometry as fpm


class TestOverlapGeometry:
    def test_overlap_bounds(self):
        assert fpm.two_circle_overlap_fraction(0.0, 0.2) == pytest.approx(1.0)
        assert fpm.two_circle_overlap_fraction(0.4, 0.2) == pytest.approx(0.0)  # d == 2R
        assert fpm.two_circle_overlap_fraction(1.0, 0.2) == 0.0  # well separated

    def test_overlap_monotonic_decreasing(self):
        R = 0.25
        prev = 1.1
        for d in [0.0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.49]:
            o = fpm.two_circle_overlap_fraction(d, R)
            assert o <= prev + 1e-9
            prev = o

    @pytest.mark.parametrize("min_overlap", [0.4, 0.6, 0.75])
    def test_pitch_hits_requested_overlap(self, min_overlap):
        R = 0.2
        pitch = fpm.pitch_for_overlap(R, min_overlap)
        # Overlap at the returned pitch should equal the target (within tolerance).
        assert fpm.two_circle_overlap_fraction(pitch, R) == pytest.approx(min_overlap, abs=1e-3)
        # Slightly closer overlaps more; slightly farther overlaps less.
        assert fpm.two_circle_overlap_fraction(pitch * 0.9, R) > min_overlap
        assert fpm.two_circle_overlap_fraction(pitch * 1.1, R) < min_overlap


class TestDarkfieldSelection:
    def setup_method(self):
        self.table = fpm.synthetic_dome_na_table(793, 0.98)

    def test_candidates_in_annulus(self):
        cands = fpm.darkfield_candidates(self.table, 0.2, 0.8)
        nas = [math.hypot(*self.table[i]) for i in cands]
        assert nas, "expected darkfield candidates"
        assert min(nas) >= 0.2 - 1e-9
        assert max(nas) <= 0.8 + 1e-9

    def test_selection_covers_and_thins(self):
        selected, rep = fpm.select_darkfield_leds(
            self.table, inner_na=0.2, outer_na=0.8, pupil_radius_na=0.2, min_overlap=0.6
        )
        assert rep.n_selected > 0
        # Thinning: a dome samples denser than needed, so we pick far fewer than
        # all candidates.
        assert rep.n_selected < rep.n_candidates
        # Adjacent SELECTED pupils (not just dropped ones) overlap >= the target:
        # the representative neighbour spacing is within one pitch.
        assert rep.neighbor_spacing_na <= rep.pitch_na + 1e-3
        assert rep.min_achieved_overlap >= 0.6 - 1e-2

    def test_higher_overlap_target_selects_more(self):
        # The count is geometry-driven: a higher overlap floor needs more LEDs.
        _, rep60 = fpm.select_darkfield_leds(
            self.table, inner_na=0.4, outer_na=0.8, pupil_radius_na=0.4, min_overlap=0.6
        )
        _, rep70 = fpm.select_darkfield_leds(
            self.table, inner_na=0.4, outer_na=0.8, pupil_radius_na=0.4, min_overlap=0.7
        )
        assert rep70.n_selected > rep60.n_selected

    def test_paper_regime_pattern_count(self):
        # Sanity vs Tian et al.: a 0.2-NA objective tiling out to ~0.8 needs on the
        # order of the paper's ~17 multiplexed darkfield patterns (8 LEDs each),
        # NOT a handful — guards against the old under-sampling bug.
        pats, _ = fpm.build_fpm_darkfield_patterns(
            self.table, objective_na=0.2, outer_na=0.8, min_overlap=0.6, leds_per_pattern=8
        )
        assert 12 <= len(pats) <= 22

    def test_higher_objective_na_needs_fewer_patterns(self):
        # Larger objective NA -> larger pupils -> larger pitch -> fewer LEDs.
        _, rep_low = fpm.select_darkfield_leds(
            self.table, inner_na=0.2, outer_na=0.8, pupil_radius_na=0.2, min_overlap=0.6
        )
        _, rep_high = fpm.select_darkfield_leds(
            self.table, inner_na=0.4, outer_na=0.8, pupil_radius_na=0.4, min_overlap=0.6
        )
        assert rep_high.n_selected < rep_low.n_selected

    def test_empty_when_region_has_no_leds(self):
        selected, rep = fpm.select_darkfield_leds(
            self.table, inner_na=1.2, outer_na=1.3, pupil_radius_na=0.2, min_overlap=0.6
        )
        assert selected == []
        assert rep.n_candidates == 0


class TestGrouping:
    def test_group_sizes_and_completeness(self):
        idx = list(range(50))
        groups = fpm.group_leds(idx, leds_per_pattern=8, seed=0)
        assert [len(g) for g in groups] == [8, 8, 8, 8, 8, 8, 2]
        flat = [i for g in groups for i in g]
        assert sorted(flat) == idx  # every LED used exactly once

    def test_grouping_is_deterministic(self):
        idx = list(range(40))
        assert fpm.group_leds(idx, 8, seed=3) == fpm.group_leds(idx, 8, seed=3)

    def test_seed_changes_grouping(self):
        idx = list(range(40))
        assert fpm.group_leds(idx, 8, seed=1) != fpm.group_leds(idx, 8, seed=2)


class TestFullFPM:
    def setup_method(self):
        self.table = fpm.synthetic_dome_na_table(793, 0.98)

    def test_brightfield_leds_within_objective(self):
        import math

        bf = fpm.brightfield_leds(self.table, 0.4)
        assert bf, "expected brightfield LEDs"
        assert all(math.hypot(*self.table[i]) <= 0.4 + 1e-9 for i in bf)
        # Brightfield + darkfield candidates partition the cap at the boundary.
        df = fpm.darkfield_candidates(self.table, 0.4, 0.98)
        assert set(bf).isdisjoint(set(df))

    def test_pseudorandom_sample(self):
        bf = fpm.brightfield_leds(self.table, 0.4)
        # n<=0 or n>=len -> all, order preserved.
        assert fpm.pseudorandom_sample(bf, 0, seed=0) == bf
        assert fpm.pseudorandom_sample(bf, len(bf) + 5, seed=0) == bf
        # Subset: exactly n, all members from the pool, order preserved (subseq).
        sub = fpm.pseudorandom_sample(bf, 20, seed=3)
        assert len(sub) == 20
        assert set(sub) <= set(bf)
        assert sub == [i for i in bf if i in set(sub)]  # original (centre-out) order kept
        # Deterministic per seed; different seeds usually differ.
        assert fpm.pseudorandom_sample(bf, 20, seed=3) == sub
        assert fpm.pseudorandom_sample(bf, 20, seed=4) != sub

    def test_clusters_cover_all_darkfield_and_are_local(self):
        cells, rep = fpm.cluster_darkfield_leds(
            self.table, inner_na=0.4, outer_na=0.8, pupil_radius_na=0.4, min_overlap=0.6
        )
        assert len(cells) > 1
        # Every darkfield LED lands in exactly one cell (a partition).
        members = [i for c in cells for i in c.indices]
        assert len(members) == len(set(members)) == rep.n_candidates
        # Cells are angularly tight: each member is within ~one pitch of its centroid.
        import math

        for c in cells:
            cx, cy = c.centroid
            assert all(
                math.hypot(self.table[i][0] - cx, self.table[i][1] - cy) <= rep.pitch_na
                for i in c.indices
            )

    def test_higher_overlap_makes_more_cells(self):
        _, r60 = fpm.cluster_darkfield_leds(
            self.table, inner_na=0.4, outer_na=0.8, pupil_radius_na=0.4, min_overlap=0.6
        )
        _, r70 = fpm.cluster_darkfield_leds(
            self.table, inner_na=0.4, outer_na=0.8, pupil_radius_na=0.4, min_overlap=0.7
        )
        assert r70.n_selected > r60.n_selected


class TestAutoLedsPerPattern:
    def test_recommended_scales_and_clamps(self):
        # Balanced ~sqrt(N), floored at 2, capped at the paper's 8, never > N.
        assert fpm.recommended_leds_per_pattern(1) == 1
        assert fpm.recommended_leds_per_pattern(25) == 5
        assert fpm.recommended_leds_per_pattern(9) == 3
        assert fpm.recommended_leds_per_pattern(1000) == 8  # capped
        assert fpm.recommended_leds_per_pattern(4) == 2  # floored

    def test_build_auto_uses_recommended(self):
        table = fpm.synthetic_dome_na_table(793, 0.98)
        pats_auto, rep = fpm.build_fpm_darkfield_patterns(
            table, objective_na=0.4, outer_na=0.8, min_overlap=0.6, leds_per_pattern=0
        )
        m_auto = max(len(p) for p in pats_auto)
        assert m_auto == fpm.recommended_leds_per_pattern(rep.n_selected)
        # Auto adapts to NA: a 0.4 objective picks fewer LEDs/pattern than the
        # paper's fixed 8 here (since the selected set is small).
        assert 2 <= m_auto <= 8


class TestBuildAndPersist:
    def test_inner_na_defaults_to_objective(self):
        table = fpm.synthetic_dome_na_table(400, 0.95)
        pats, rep = fpm.build_fpm_darkfield_patterns(
            table, objective_na=0.3, outer_na=0.8, inner_na=None, leds_per_pattern=8, seed=0
        )
        # No selected LED below the objective NA (the brightfield/darkfield edge).
        nas = [math.hypot(*table[i]) for g in pats for i in g]
        assert nas and min(nas) >= 0.3 - 1e-9

    def test_table_csv_round_trip(self, tmp_path):
        rows = [(0, 0.0, 0.0), (1, 0.1, -0.2), (2, 0.33, 0.44)]
        path = fpm.save_na_table(rows, tmp_path / "na.csv")
        loaded = fpm.load_na_table(path)
        assert loaded[1] == pytest.approx((0.1, -0.2))
        assert loaded[2] == pytest.approx((0.33, 0.44))

    def test_missing_table_raises_actionable(self, tmp_path):
        with pytest.raises(FileNotFoundError) as exc:
            fpm.load_na_table(tmp_path / "does_not_exist.csv")
        assert "pledposna" in str(exc.value)


class TestSaveFpmPatternPositions:
    """The acquisition writes per-pattern LED indices + NA positions to the run
    folder so reconstruction is self-contained."""

    def test_writes_fpm_patterns_yaml(self, tmp_path, monkeypatch):
        import os
        import types

        import yaml

        from control import fpm_led_geometry as fpm
        from control.core import multi_point_controller as mpc
        from control.models.acquisition_cycle import (
            AcquisitionCycle,
            CycleFPMDarkfield,
            RegionPlan,
            resolve_cycle,
        )

        table = {0: (0.5, 0.0), 1: (0.0, 0.5), 2: (0.5, 0.5), 3: (-0.5, 0.0)}
        monkeypatch.setattr(fpm, "load_na_table", lambda *a, **k: table)

        cyc = AcquisitionCycle(name="c", items=[CycleFPMDarkfield(observation_state="df")])
        plan = RegionPlan.from_events(
            resolve_cycle(cyc, fpm_provider=lambda item: [("df", (0, 1)), ("df", (2, 3))])
        )
        params = types.SimpleNamespace(global_region_plan=plan, resolved_region_plans={})

        class FakeRepo:
            def get_machine_config(self):
                raise RuntimeError("no config in test")

        import logging

        # Pass a real logger so the success-log line is exercised too.
        mpc._save_fpm_pattern_positions(str(tmp_path), params, FakeRepo(), 0.3, logger=logging.getLogger("fpm-test"))
        out = yaml.safe_load(open(os.path.join(str(tmp_path), "fpm_patterns.yaml")))
        assert out["n_patterns"] == 2
        assert out["objective_na"] == 0.3
        assert out["patterns"][0]["observation_state"] == "df"
        assert out["patterns"][0]["led_indices"] == [0, 1]
        assert out["patterns"][0]["led_na"] == [[0.5, 0.0], [0.0, 0.5]]
        assert out["patterns"][0]["centroid_na"] == [0.25, 0.25]
        assert out["patterns"][1]["led_indices"] == [2, 3]

    def test_no_file_when_no_fpm_events(self, tmp_path):
        import os
        import types

        from control.core import multi_point_controller as mpc
        from control.models.acquisition_cycle import AcquisitionCycle, CycleStep, RegionPlan, resolve_cycle

        plan = RegionPlan.from_events(resolve_cycle(AcquisitionCycle(name="c", items=[CycleStep(observation_state="GFP")])))
        params = types.SimpleNamespace(global_region_plan=plan, resolved_region_plans={})
        mpc._save_fpm_pattern_positions(str(tmp_path), params, None, 0.3, logger=None)
        assert not os.path.exists(os.path.join(str(tmp_path), "fpm_patterns.yaml"))


class TestSimArrayDump:
    """The simulation SCI array must be constructible (no serial) and able to
    produce a synthetic NA table + multiplexed command, so the FPM pipeline can
    be exercised offline."""

    def test_sim_array_constructs_and_dumps(self):
        from control.serial_peripherals import SciMicroscopyLEDArray_Simulation

        sim = SciMicroscopyLEDArray_Simulation(SN="SIM", array_distance=65)
        rows = sim.dump_led_na_positions()
        assert len(rows) == 793
        assert all(len(r) == 3 for r in rows)

    def test_multi_led_command_builder(self):
        from control.serial_peripherals import SciMicroscopyLEDArray_Simulation

        sim = SciMicroscopyLEDArray_Simulation(SN="SIM")
        assert sim.set_multiple_leds([5, 9, 42]) == "l.5.9.42"

    def test_parse_pledposna_line_and_json(self):
        from control.serial_peripherals import _parse_pledposna

        line = "0, 0.0, 0.0, 65\n1, 0.10, -0.20, 65\n2 0.33 0.44 65\n"
        assert _parse_pledposna(line) == [(0, 0.0, 0.0), (1, 0.10, -0.20), (2, 0.33, 0.44)]
        js = '{"led_position_list_na": {"0": [0.0, 0.0, 65], "1": [0.1, 0.2, 65]}}'
        assert _parse_pledposna(js) == [(0, 0.0, 0.0), (1, 0.1, 0.2)]
