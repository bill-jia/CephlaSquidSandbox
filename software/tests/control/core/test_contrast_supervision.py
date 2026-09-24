"""Hardware-free decisions and cadence for supervised laser autofocus."""

import numpy as np
import time
from types import SimpleNamespace
import pytest
from unittest.mock import MagicMock
import contextlib

from control.core.contrast_autofocus.supervision import VisitCadence, TrustedHistory, inspect_image, require_new_supervised_run
from control.models.contrast_autofocus import ContrastSupervisionPolicy
from control.models.laser_af_reference import LaserAssessment
from control.models.laser_af_reference import LaserAFReference
from control.models.contrast_autofocus import ContrastAFSettings
from control.core.contrast_autofocus.search import FocusSample, SearchResult, run_search
from control.core.multi_point_worker import MultiPointWorker
from control.core.laser_auto_focus_controller import LaserAutofocusController


def test_default_off_and_entry_periodic_cadence():
    assert ContrastSupervisionPolicy().mode == "off"
    policy = ContrastSupervisionPolicy(mode="monitor", every_n_completed_fovs=1)
    cadence = VisitCadence()
    assert cadence.due("A1", 0, policy)
    assert not cadence.due("A1", 0, policy)
    cadence.complete()
    assert cadence.due("A1", 0, policy)
    assert not cadence.due("A1", 0, policy)
    assert cadence.due("A2", 0, policy)
    assert cadence.due("A1", 1, policy)


def test_removed_scheduled_correction_restores_safely_off():
    policy = ContrastSupervisionPolicy.model_validate({
        "mode": "scheduled", "every_n_completed_fovs": 3,
        "full_verification_every_n_checks": 2,
    })
    assert policy.mode == "off"
    assert "full_verification_every_n_checks" not in policy.model_dump()
    assert ContrastSupervisionPolicy.model_validate({
        "mode": "quality", "full_verification_every_n_checks": 2,
    }).mode == "quality"


def test_incomplete_supervised_run_cannot_resume(monkeypatch):
    monkeypatch.setattr("control.core.contrast_autofocus.supervision.os.path.exists", lambda path: True)
    require_new_supervised_run("existing", ContrastSupervisionPolicy())
    with pytest.raises(RuntimeError, match="Resuming a supervised acquisition"):
        require_new_supervised_run("existing", ContrastSupervisionPolicy(mode="monitor"))


def test_only_independent_trusted_fields_form_baseline():
    policy = ContrastSupervisionPolicy(mode="quality", min_baseline_fields=3,
                                       bad_checks_required=2)
    history = TrustedHistory()
    image = np.indices((32, 32)).sum(axis=0).astype(np.uint8) * 4 + 40
    good = inspect_image(image, 100.0)
    poor = inspect_image(image, 40.0)
    assert history.classify(1, poor, policy)[0] == "unknown"
    now = time.time()
    history.seed(1, good, timestamp=now)
    assert not history.extend_verified_basis(1, good, policy)
    assert history.extend_verified_basis(2, good, policy)
    assert history.extend_verified_basis(3, good, policy)
    assert history.classify(4, poor, policy, timestamp=now + 1)[0] == "suspect"
    assert history.classify(4, poor, policy, timestamp=now + 1)[0] == "poor"
    assert history.support == 3
    assert history.classify(4, good, policy, timestamp=now + 1)[0] == "good"


def test_blank_and_saturated_are_unknown():
    assert inspect_image(np.zeros((32, 32), dtype=np.uint8), 100).reason == "dim"
    assert inspect_image(np.full((32, 32), 255, dtype=np.uint8), 100).reason == "saturated"
    assert inspect_image(np.indices((32, 32)).sum(axis=0).astype(np.uint8), 0).reason == "invalid_sharpness"
    assert inspect_image(np.full((32, 32), np.nan), 100).reason == "invalid_pixels"
    assert inspect_image(np.ones((32, 32), dtype=np.uint8), 100,
                         sensor_full_scale=65535).reason == "scale_mismatch"


def test_brightness_drift_does_not_become_defocus_or_lower_baseline():
    policy = ContrastSupervisionPolicy(mode="quality", min_baseline_fields=2)
    texture = np.indices((32, 32)).sum(axis=0).astype(np.uint8) * 3
    normal = inspect_image(texture + 50, 100)
    bleached = inspect_image(texture + 5, 40)
    history = TrustedHistory()
    history.seed(0, normal)
    history.seed(1, normal)
    before = len(history.samples)
    assert history.classify(2, bleached, policy)[0] == "unknown"
    assert len(history.samples) == before
    assert not history.record_good(2, inspect_image(texture + 50, 90))


def test_gradual_defocus_does_not_drag_trusted_baseline_down():
    policy = ContrastSupervisionPolicy(mode="quality", min_baseline_fields=3,
                                       bad_checks_required=2)
    image = (np.indices((32, 32)).sum(axis=0) * 3 + 50).astype(np.uint8)
    history = TrustedHistory()
    for field in range(3):
        history.seed(field, inspect_image(image, 100))
    for score in (99, 97, 94, 90, 85, 80, 75, 70):
        observation = inspect_image(image, score)
        state, _ = history.classify(4, observation, policy)
        if state == "good":
            history.record_good(4, observation, policy)
    assert history.classify(4, inspect_image(image, 60), policy)[0] in ("suspect", "poor")
    assert history.classify(4, inspect_image(image, 60), policy)[0] == "poor"
    assert history.statistics(time.time(), policy.max_baseline_age_s)["log_center"] > np.log(90)


def test_slow_defocus_cannot_reanchor_from_ordinary_good_checks():
    policy = ContrastSupervisionPolicy(mode="quality", min_baseline_fields=3,
                                       bad_checks_required=2, max_baseline_age_s=3600)
    image = (np.indices((32, 32)).sum(axis=0) * 3 + 50).astype(np.uint8)
    history = TrustedHistory()
    for field in range(3):
        history.seed(field, inspect_image(image, 100))
    decisions = []
    for i in range(1, 1401):
        observation = inspect_image(image, 100 * 0.9995 ** i)
        decision, _ = history.classify(i % 3, observation, policy)
        decisions.append(decision)
        if decision == "good":
            history.record_good(i % 3, observation, policy)
    assert "poor" in decisions
    assert history.statistics(time.time(), policy.max_baseline_age_s)["log_center"] == pytest.approx(np.log(100))
    assert len(history.samples) <= 64


def test_new_fields_cannot_ratchet_verified_reference_downward():
    policy = ContrastSupervisionPolicy(mode="quality", min_baseline_fields=3)
    image = (np.indices((32, 32)).sum(axis=0) * 3 + 50).astype(np.uint8)
    history = TrustedHistory()
    history.seed(0, inspect_image(image, 100))
    assert history.extend_verified_basis(1, inspect_image(image, 99), policy)
    assert not history.extend_verified_basis(2, inspect_image(image, 97), policy)
    assert history.reference_log_score == pytest.approx(np.log(100))
    assert history.support == 2


def test_stable_noisy_checks_preserve_verified_anchor_until_expiry():
    policy = ContrastSupervisionPolicy(mode="quality", min_baseline_fields=3,
                                       max_baseline_age_s=3600)
    image = (np.indices((32, 32)).sum(axis=0) * 3 + 50).astype(np.uint8)
    history = TrustedHistory()
    for field in range(3):
        history.seed(field, inspect_image(image, 100))
    for i in range(300):
        observation = inspect_image(image, 100 + (i % 5 - 2))
        assert history.classify(i % 3, observation, policy)[0] == "good"
        history.record_good(i % 3, observation, policy)
    assert history.statistics(time.time(), policy.max_baseline_age_s)["log_center"] == pytest.approx(np.log(100))
    expired = time.time() + policy.max_baseline_age_s + 1
    assert history.classify(0, inspect_image(image, 50), policy, timestamp=expired) == (
        "unknown", "baseline_expired")
    for field in range(3):
        history.anchors[field] = (*history.anchors[field][:2], 1,
                                  *history.anchors[field][3:])
    assert not history.record_good(0, inspect_image(image, 50), policy)
    assert TrustedHistory(revision=history.revision + 1).support == 0


def test_expired_baseline_is_unknown_and_revision_reset_discards_old_scores():
    policy = ContrastSupervisionPolicy(mode="quality", min_baseline_fields=2,
                                       max_baseline_age_s=10)
    image = (np.indices((32, 32)).sum(axis=0) * 3 + 50).astype(np.uint8)
    good = inspect_image(image, 100)
    history = TrustedHistory()
    history.seed(0, good, timestamp=1)
    history.seed(1, good, timestamp=1)
    assert history.classify(2, inspect_image(image, 40), policy, timestamp=20) == (
        "unknown", "baseline_expired")
    reset = TrustedHistory(revision=history.revision + 1)
    assert reset.support == 0


def test_laser_assessment_requires_fresh_finite_measurement():
    evidence = LaserAssessment(measured_at=10, spot_valid=True, residual_um=0.5,
                               correlation=0.9, correlation_valid=True)
    args = dict(max_age_s=5, max_residual_um=2, min_correlation=0.5)
    assert evidence.good(now=11, **args)
    assert not evidence.good(now=20, **args)
    assert not evidence.model_copy(update={"residual_um": float("nan")}).good(now=11, **args)


def test_strict_reference_capture_rejects_laser_cleanup_fault():
    controller = LaserAutofocusController.__new__(LaserAutofocusController)
    controller.is_initialized = True
    controller._log = MagicMock()
    controller.laser_af_properties = SimpleNamespace(spot_crop_size=8)
    controller.image = np.indices((16, 16)).sum(axis=0).astype(np.uint8) + 1
    controller.turn_on_AF_laser = MagicMock()
    controller.turn_off_AF_laser = MagicMock(side_effect=TimeoutError("laser off timeout"))
    controller._get_laser_spot_centroid = lambda **kwargs: (8.0, 8.0)
    with pytest.raises(RuntimeError, match="Laser cleanup failed"):
        controller.capture_reference_strict()
    controller.turn_off_AF_laser.assert_called_once()


def test_fresh_nonmoving_laser_assessment():
    controller = LaserAutofocusController.__new__(LaserAutofocusController)
    controller.get_active_reference = lambda: LaserAFReference(x_reference=10)
    controller.turn_on_AF_laser = MagicMock()
    controller.turn_off_AF_laser = MagicMock()
    controller._measure_displacement_with_laser_on = lambda: 0.25
    controller._verify_spot_alignment_with_laser_on = lambda: (True, 0.9)
    assessment = controller.assess_active_reference()
    assert assessment.good(now=time.time(), max_age_s=5, max_residual_um=2,
                           min_correlation=0.5)
    controller.turn_off_AF_laser.assert_called_once()


def test_laser_correlation_rejects_stale_image_when_new_spot_is_missing():
    controller = LaserAutofocusController.__new__(LaserAutofocusController)
    controller._timing = None
    controller._log = MagicMock()
    controller.image = np.ones((16, 16), dtype=np.uint8)
    controller.reference_crop = np.ones((8, 8), dtype=np.float32)
    controller.laser_af_properties = SimpleNamespace(x_reference=8, spot_crop_size=8)
    controller._get_laser_spot_centroid = lambda: None
    valid, correlation = controller._verify_spot_alignment_with_laser_on()
    assert not valid
    assert np.isnan(correlation)


def test_monitor_only_checks_image_without_changing_reference():
    worker = MultiPointWorker.__new__(MultiPointWorker)
    worker._supervision_policy = ContrastSupervisionPolicy(mode="monitor")
    worker.do_reflection_af = True
    worker.objectiveStore = SimpleNamespace(current_objective="20x")
    worker._supervision_objective = "20x"
    worker._supervision_cadence = VisitCadence()
    worker.time_point = 0
    worker._supervision_frames_run = 0
    worker._supervision_frames_by_region = {}
    worker._supervision_exposure_ms_run = 0
    worker._supervision_exposure_ms_by_region = {}
    worker._supervision_time_s_run = 0
    worker._supervision_time_s_by_region = {}
    worker._supervision_laser_exposure_ms_run = 0
    worker._supervision_laser_exposure_ms_by_region = {}
    worker._supervision_histories = {}
    worker._supervision_revision = {}
    worker._supervision_checks_by_region = {}
    worker._contrast_af_state = SimpleNamespace(exposure_time=10)
    worker.laser_auto_focus_controller = SimpleNamespace(
        camera=SimpleNamespace(get_exposure_time=lambda: 1),
        assess_active_reference=lambda: LaserAssessment(
            measured_at=time.time(), spot_valid=True, residual_um=0,
            correlation=0.9, correlation_valid=True))
    worker._supervision_capture = lambda state: inspect_image(
        np.indices((32, 32)).sum(axis=0).astype(np.uint8) * 3 + 50, 100)
    worker._supervision_log = lambda *args, **kwargs: None
    worker._run_laser_af_refresh = lambda *args: (_ for _ in ()).throw(AssertionError("unexpected laser move"))
    worker._supervision_attempt_correction = lambda *args: (_ for _ in ()).throw(AssertionError("unexpected correction"))
    worker._supervise_nominal_plane("A1", 0)
    assert worker._supervision_checks_by_region["A1"] == 1
    assert worker._supervision_histories["A1"].support == 0


@pytest.mark.parametrize("nz,use_piezo", [(1, False), (2, False), (2, True)])
def test_supervision_runs_before_channel_offset_and_counts_completed_fov_once(nz, use_piezo):
    worker = MultiPointWorker.__new__(MultiPointWorker)
    events = []
    stage = SimpleNamespace(z=1.0)
    stage.get_pos = lambda: SimpleNamespace(x_mm=0, y_mm=0, z_mm=stage.z)
    worker.stage = stage
    worker.do_reflection_af = True
    worker.do_autofocus = False
    worker._supervision_policy = ContrastSupervisionPolicy(mode="monitor")
    worker._supervision_cadence = VisitCadence()
    worker._supervision_region_completed_total = {}
    worker._timing = SimpleNamespace(get_timer=lambda name: contextlib.nullcontext())
    worker._collect_af_validation = lambda *args: contextlib.nullcontext()
    worker.Nt = 1
    worker.NZ = nz
    worker.use_piezo = use_piezo
    worker.piezo = SimpleNamespace(position=100.0)
    worker.validation_mode = False
    worker.laser_auto_focus_controller = SimpleNamespace(characterization_mode=False)
    worker._log = MagicMock()
    worker._record_autofocus_event = lambda **kwargs: events.append(("af_log", kwargs["z_actual_mm"]))

    def autofocus(region, fov):
        stage.z = 1.001
        worker._last_af_status = "ok"
        return True

    worker.perform_autofocus = autofocus
    worker._supervise_nominal_plane = lambda region, fov: (
        events.append(("supervision", stage.z)), setattr(stage, "z", 1.002))
    event = SimpleNamespace(is_wait=False, acquire_z_stack=True, is_stimulus=False,
                            observation_state="imaging", multiplexed_leds=None,
                            postprocess=None, postprocess_group=None)
    worker._get_region_plan = lambda region: SimpleNamespace(captured_frames_per_position=1,
                                                               events=[event])
    worker._apply_observation_state = lambda name: SimpleNamespace(is_stimulus_only=False)
    worker.handle_z_offset = lambda config, entering: (
        events.append(("channel_offset_enter" if entering else "channel_offset_exit", stage.z)),
        setattr(stage, "z", stage.z + (0.003 if entering else -0.003)))
    worker.prepare_z_stack = lambda: (events.append(("stack_prepare", stage.z)),
                                      setattr(worker.piezo if use_piezo else stage,
                                              "position" if use_piezo else "z",
                                              98.0 if use_piezo else stage.z - 0.002))
    worker.move_z_for_stack = lambda: setattr(worker.piezo if use_piezo else stage,
                                              "position" if use_piezo else "z",
                                              (worker.piezo.position + 4) if use_piezo else stage.z + 0.004)
    worker.move_z_back_after_stack = lambda: setattr(worker.piezo if use_piezo else stage,
                                                     "position" if use_piezo else "z",
                                                     100.0 if use_piezo else 1.002)
    worker._reference_z_level = lambda: 0
    worker._build_save_layout = lambda plan, event: SimpleNamespace(c_index=0)
    worker.acquire_camera_image = lambda *args, **kwargs: events.append(("imaging", stage.z))
    worker.callbacks = SimpleNamespace(signal_region_progress=lambda update: None,
                                       signal_current_fov=lambda x, y: None)
    worker.scan_region_fov_coords_mm = {"A1": [(0, 0, 1)]}
    worker.total_scans = 1
    worker.update_coordinates_dataframe = lambda *args: None
    worker.abort_requested_fn = lambda: False
    worker.af_fov_count = 0
    worker._timepoint_fov_count = 0
    worker.acquire_at_position("A1", "unused", 0)
    if nz == 1:
        assert events.index(("supervision", 1.001)) < events.index(("channel_offset_enter", 1.002))
        assert ("imaging", 1.005) in events
    else:
        assert events.index(("supervision", 1.001)) < events.index(("stack_prepare", 1.002))
        assert len([event for event in events if event[0] == "imaging"]) == 2
        if use_piezo:
            assert worker.piezo.position == 100
    assert worker._supervision_cadence.completed == 1
    assert worker._supervision_region_completed_total["A1"] == 1
    worker.acquire_camera_image = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("aborted frame"))
    with pytest.raises(RuntimeError, match="aborted frame"):
        worker.acquire_at_position("A1", "unused", 0)
    assert worker._supervision_cadence.completed == 1
    assert worker._supervision_region_completed_total["A1"] == 1


def _transaction_worker(*, fail_confirmation=False, method="legacy"):
    worker = MultiPointWorker.__new__(MultiPointWorker)
    old = LaserAFReference.from_capture(20, np.ones((4, 4), dtype=np.float32))
    new = LaserAFReference.from_capture(21, np.ones((4, 4), dtype=np.float32))

    class Stage:
        z = 1.0

        def get_pos(self):
            return SimpleNamespace(z_mm=self.z, x_mm=0, y_mm=0)

        def move_z_to(self, value):
            self.z = value

        def wait_for_idle(self, timeout):
            pass

    class Laser:
        active = old
        camera = SimpleNamespace(get_exposure_time=lambda: 1)

        def get_active_reference(self):
            return self.active

        def capture_reference_strict(self):
            return new

        def apply_reference(self, ref):
            self.active = ref

        def assess_active_reference(self):
            return LaserAssessment(measured_at=time.time(), spot_valid=True,
                                   residual_um=0.1, correlation=0.9, correlation_valid=True)

    worker.stage = Stage()
    worker.piezo = None
    worker.laser_auto_focus_controller = Laser()
    worker._supervision_policy = ContrastSupervisionPolicy(mode="quality", max_attempts_per_region=2)
    worker._supervision_save_artifacts = False
    worker.time_point = 0
    worker._supervision_cadence = SimpleNamespace(completed=0)
    worker._supervision_frames_by_region = {}
    worker._supervision_frames_run = 0
    worker._supervision_exposure_ms_by_region = {}
    worker._supervision_exposure_ms_run = 0
    worker._supervision_laser_exposure_ms_run = 0
    worker._supervision_laser_exposure_ms_by_region = {}
    worker._supervision_time_s_by_region = {}
    worker._supervision_time_s_run = 0
    worker._supervision_attempts = {}
    worker._supervision_last_attempt_completed = {}
    worker._supervision_region_completed_total = {}
    worker._supervision_revision = {}
    worker._supervision_histories = {}
    worker._region_laser_af_references = {}
    worker._region_anchor_z_current = {"A1": 1.0}
    worker._region_anchor_fov = {"A1": 0}
    worker._fovs_since_refresh = {"A1": 2}
    worker._region_refresh_count_this_entry = 1
    worker._fov_z_map = {("A1", 0): 1.0, ("A1", 1): 1.01, ("A2", 0): 1.5}
    worker._fov_z_delta_map = {("A1", 0): 0, ("A1", 1): 0.01, ("A2", 0): 0}
    worker._z_pos_proposal = {("A1", 1): 1.01, ("A2", 0): 1.5}
    worker._contrast_af_step_um = 1
    worker._wait_for_move_settled = lambda: None
    worker.wait_till_operation_is_completed = lambda: None
    worker.abort_requested_fn = lambda: False
    worker.aborted = False
    worker.request_abort_fn = lambda: setattr(worker, "aborted", True)
    worker.events = []
    worker._supervision_log = lambda *a, **kw: worker.events.append((a[2], kw))
    scan = SearchResult("success", 1000, accepted_z_um=1000, frames=5,
                        trace=[{"z_um": 999, "score": 80}, {"z_um": 1000, "score": 100},
                               {"z_um": 1001, "score": 80}])
    worker._run_owned_contrast_scan = lambda state, **kwargs: scan
    captures = []

    def capture(state, z, artifact_path=None, deadline=None):
        captures.append(z)
        worker.stage.z = z / 1000
        score = 80 if z in (999, 1001) else 100
        if fail_confirmation and len(captures) == 6:
            score = None
        return SimpleNamespace(score=score, brightness=100, noise=5,
                               reason="good" if score is not None else "invalid")

    worker._supervision_capture = capture
    settings = (ContrastAFSettings(max_frames=10) if method == "legacy" else
                ContrastAFSettings(method="frequency_assisted", coarse_step_um=10,
                                   medium_step_um=2, fine_step_um=1,
                                   window_below_um=20, window_above_um=20,
                                   sensor_full_scale=255, max_frames=10))
    state = SimpleNamespace(contrast_af=settings, exposure_time=10)
    return worker, state, old, new


@pytest.mark.parametrize("method", ["legacy", "frequency_assisted"])
def test_reference_transaction_commits_region_map_without_touching_other_region(method):
    worker, state, old, new = _transaction_worker(method=method)
    assert worker._supervision_attempt_correction("A1", 0, state, None)
    assert worker.laser_auto_focus_controller.active == new
    assert worker._region_laser_af_references["A1"] == new
    assert ("A1", 1) not in worker._fov_z_map
    assert worker._fov_z_map[("A2", 0)] == 1.5
    assert worker._supervision_revision["A1"] == 1
    assert worker.events[-1][0] == "commit"
    worker._base_laser_af_reference = old
    worker.laser_auto_focus_controller.apply_reference(old)
    worker._apply_region_laser_af_reference("A1")
    assert worker.laser_auto_focus_controller.active == new


def _real_frequency_result(state):
    settings = state.contrast_af.model_copy(update={"max_frames": 100})

    def capture(z):
        image = np.zeros((4, 4), dtype=np.float64)
        image[0, 0] = max(100 - (z - 1000) ** 2, 0)
        image[0, 1] = 1040 - z
        return FocusSample(image, z)

    result = run_search(capture_at=capture, move_to=lambda z: z, settings=settings,
                        sharpness=lambda image: image[0, 0], energy=lambda image: image[0, 1],
                        cancelled=lambda: False, starting_z_um=1000, lower_z_um=980,
                        upper_z_um=1020, increment_um=1, exposure_ms=1)
    return result


def test_real_frequency_search_trace_commits_reference():
    worker, state, old, new = _transaction_worker(method="frequency_assisted")
    result = _real_frequency_result(state)
    assert result.status == "success"
    assert result.verification_passed
    assert not any("z_um" in row for row in result.trace)
    worker._run_owned_contrast_scan = lambda state, **kwargs: result
    assert worker._supervision_attempt_correction("A1", 0, state, None)
    assert worker._region_laser_af_references["A1"] == new


def test_real_frequency_search_trace_rejects_ambiguous_local_confirmation():
    worker, state, old, _ = _transaction_worker(method="frequency_assisted")
    result = _real_frequency_result(state)
    assert result.status == "success"
    worker._run_owned_contrast_scan = lambda state, **kwargs: result
    worker._supervision_capture = lambda state, z, **kwargs: SimpleNamespace(
        score=100, brightness=100, noise=5, reason="good")
    with pytest.raises(RuntimeError, match="ambiguous or not repeatable"):
        worker._supervision_attempt_correction("A1", 0, state, None)
    assert worker.laser_auto_focus_controller.active == old


@pytest.mark.parametrize("trace", [
    [{"phase": "coarse", "actual_z_um": 1000, "score": 100},
     {"phase": "move", "actual_z_um": 999}, {"phase": "candidate", "actual_z_um": 1001, "score": 80}],
    [{"phase": "coarse", "actual_z_um": 1000, "score": 100},
     {"phase": "fine", "actual_z_um": 1001, "score": 80},
     {"phase": "fine", "actual_z_um": 1002, "score": 70}],
])
def test_frequency_trace_without_measured_interior_bracket_rejects(trace):
    worker, state, old, _ = _transaction_worker(method="frequency_assisted")
    worker._run_owned_contrast_scan = lambda state, **kwargs: SearchResult(
        "success", 1000, accepted_z_um=1000, trace=trace)
    with pytest.raises(RuntimeError, match="interior bracket"):
        worker._supervision_attempt_correction("A1", 0, state, None)
    assert worker.laser_auto_focus_controller.active == old


def test_reference_transaction_rolls_back_after_candidate_failure():
    worker, state, old, new = _transaction_worker(fail_confirmation=True)
    try:
        worker._supervision_attempt_correction("A1", 0, state, None)
    except RuntimeError:
        pass
    else:
        assert False, "default failure policy must stop"
    assert worker.aborted
    assert worker.laser_auto_focus_controller.active == old
    assert worker.stage.z == 1.0
    assert worker._fov_z_map[("A1", 1)] == 1.01
    assert worker._supervision_revision["A1"] == 0
    assert worker.events[-1][0] == "rollback"


def test_explicit_continue_requires_verified_optical_rollback():
    worker, state, old, _ = _transaction_worker(fail_confirmation=True)
    worker._supervision_policy = worker._supervision_policy.model_copy(
        update={"failure_action": "continue_restored"})
    assert not worker._supervision_attempt_correction("A1", 0, state, None)
    assert not worker.aborted
    assert worker._af_failed_for_fov
    assert worker._last_af_status == "supervision_rolled_back"
    assert worker.laser_auto_focus_controller.active == old
    assert worker.stage.z == 1.0


def test_map_commit_failure_restores_all_bookkeeping():
    worker, state, old, _ = _transaction_worker()

    class FailingMap(dict):
        def __deepcopy__(self, memo):
            return dict(self)

        def __setitem__(self, key, value):
            if key == ("A1", 0):
                raise RuntimeError("map write failed")
            super().__setitem__(key, value)

    worker._fov_z_map = FailingMap(worker._fov_z_map)
    with pytest.raises(RuntimeError, match="map write failed"):
        worker._supervision_attempt_correction("A1", 0, state, None)
    assert worker.aborted
    assert worker.laser_auto_focus_controller.active == old
    assert worker._fov_z_map[("A1", 1)] == 1.01
    assert worker._fov_z_map[("A2", 0)] == 1.5
    assert worker._region_anchor_z_current["A1"] == 1.0
    assert worker.events[-1][0] == "rollback"


def test_stage_correction_keeps_stacking_piezo_fixed():
    worker, state, _, _ = _transaction_worker()
    piezo = SimpleNamespace(position=100.0)
    piezo.move_to = lambda value: setattr(piezo, "position", value)
    worker.piezo = piezo
    assert worker._supervision_attempt_correction("A1", 0, state, None)
    assert piezo.position == 100.0


def test_requested_baseline_verification_commits_before_normal_fov_imaging():
    worker, state, old, new = _transaction_worker()
    worker._supervision_policy = worker._supervision_policy.model_copy(
        update={"verify_baseline_once": True})
    worker.do_reflection_af = True
    worker.do_autofocus = False
    worker.objectiveStore = SimpleNamespace(current_objective="20x")
    worker._supervision_objective = "20x"
    worker._supervision_cadence = VisitCadence()
    worker._supervision_region_completed_total = {}
    worker._supervision_checks_by_region = {}
    worker._contrast_af_state = state
    worker.time_point = 0
    worker.Nt = 1
    worker.NZ = 1
    worker.use_piezo = False
    worker.validation_mode = False
    worker.af_fov_count = 0
    worker._timepoint_fov_count = 0
    worker._log = MagicMock()
    worker._timing = SimpleNamespace(get_timer=lambda name: contextlib.nullcontext())
    worker._collect_af_validation = lambda *args: contextlib.nullcontext()
    worker._record_autofocus_event = lambda **kwargs: None
    worker.perform_autofocus = lambda region, fov: setattr(worker, "_last_af_status", "ok") or True
    worker._supervision_capture = lambda state, z_um=None, **kwargs: SimpleNamespace(
        score=80 if z_um in (999, 1001) else 100, brightness=100, noise=5, reason="good")
    event = SimpleNamespace(is_wait=False, acquire_z_stack=True, is_stimulus=False,
                            observation_state="imaging", multiplexed_leds=None,
                            postprocess=None, postprocess_group=None)
    worker._get_region_plan = lambda region: SimpleNamespace(events=[event], captured_frames_per_position=1)
    worker._apply_observation_state = lambda name: SimpleNamespace(is_stimulus_only=False)
    worker._reference_z_level = lambda: 0
    worker._build_save_layout = lambda plan, item: SimpleNamespace(c_index=0)
    worker.handle_z_offset = lambda config, entering: None
    captured = []
    worker.acquire_camera_image = lambda *args, **kwargs: captured.append(
        (worker.stage.get_pos().z_mm, worker.laser_auto_focus_controller.active))
    worker.callbacks = SimpleNamespace(signal_region_progress=lambda update: None,
                                       signal_current_fov=lambda x, y: None)
    worker.scan_region_fov_coords_mm = {"A1": [(0, 0, 1)]}
    worker.total_scans = 1
    worker.update_coordinates_dataframe = lambda *args: None
    worker.acquire_at_position("A1", "unused", 0)
    assert captured == [(1.0, new)]
    assert worker._supervision_revision["A1"] == 1
    assert worker._supervision_cadence.completed == 1
    assert worker._region_laser_af_references["A1"] == new


def test_rollback_motion_failure_forces_stop_even_with_continue_policy():
    worker, state, old, _ = _transaction_worker(fail_confirmation=True)
    worker._supervision_policy = worker._supervision_policy.model_copy(
        update={"failure_action": "continue_restored"})
    worker.stage.move_z_to = lambda value: (_ for _ in ()).throw(RuntimeError("stage jam"))
    with pytest.raises(RuntimeError, match="stage jam"):
        worker._supervision_attempt_correction("A1", 0, state, None)
    assert worker.aborted
    assert worker.laser_auto_focus_controller.active == old
    assert "stage:" in worker.events[-1][1]["errors"][0]


@pytest.mark.parametrize("boundary", ["search", "monitor", "reference_capture", "apply",
                                       "laser_confirmation", "cancel_before_commit"])
def test_transaction_failure_boundaries_preserve_reference_and_maps(boundary):
    worker, state, old, new = _transaction_worker()
    if boundary == "search":
        worker._run_owned_contrast_scan = lambda state, **kwargs: SearchResult("ambiguous_peak", 1000)
    elif boundary == "monitor":
        worker._supervision_capture = lambda state, z, **kwargs: (_ for _ in ()).throw(OSError("camera failure"))
    elif boundary == "reference_capture":
        worker.laser_auto_focus_controller.capture_reference_strict = lambda: (_ for _ in ()).throw(
            RuntimeError("laser off failure"))
    elif boundary == "apply":
        apply = worker.laser_auto_focus_controller.apply_reference
        worker.laser_auto_focus_controller.apply_reference = lambda ref: (
            (_ for _ in ()).throw(RuntimeError("apply failed")) if ref == new else apply(ref))
    elif boundary == "laser_confirmation":
        worker.laser_auto_focus_controller.assess_active_reference = lambda: LaserAssessment(
            measured_at=time.time(), spot_valid=True, residual_um=10,
            correlation=0.9, correlation_valid=True)
    elif boundary == "cancel_before_commit":
        calls = iter([False, True])
        worker.abort_requested_fn = lambda: next(calls)
    with pytest.raises(RuntimeError):
        worker._supervision_attempt_correction("A1", 0, state, None)
    assert worker.aborted
    assert worker.laser_auto_focus_controller.active == old
    assert worker.stage.z == 1.0
    assert worker._fov_z_map[("A1", 1)] == 1.01
    assert worker.events[-1][0] == "rollback"
