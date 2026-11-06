from concurrent.futures import Future
from contextlib import nullcontext
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from control.core.multi_point_worker import MultiPointWorker


@pytest.mark.parametrize("focus_mode", ["contrast", "legacy", "scan"])
@pytest.mark.parametrize("failure", [False, True])
def test_compute_and_upload_overlap_optics_but_focus_is_last(focus_mode, failure):
    worker = MultiPointWorker.__new__(MultiPointWorker)
    events = []
    optics_ready, compute_started, compute_release, upload_waiting = Event(), Event(), Event(), Event()
    aborted = [False]
    worker.abort_requested_fn = lambda: aborted[0]
    worker.request_abort_fn = lambda: aborted.__setitem__(0, True)
    worker._backpressure = Mock()
    worker._log = Mock()
    worker.validation_mode = False
    worker._job_runners = [(object, SimpleNamespace(wait_ready=lambda **kw: events.append("runners ready") or True))]

    class Registration(Future):
        def result(self, timeout=None):
            upload_waiting.set()
            return super().result(timeout)

    worker._upload_registration = Registration()

    def compute():
        compute_started.set()
        assert compute_release.wait(5)
        events.append("compute ready")

    def begin_compute(**kwargs):
        assert kwargs == {"wait": False}
        return worker._background_preparation(compute, "Test warmup")

    worker._prewarm_postprocess_routines = Mock(side_effect=begin_compute)

    def optics():
        assert compute_started.wait(5)
        assert not worker._upload_registration.done()
        events.append("optics ready")
        optics_ready.set()

    worker._prewarm_observation_states = Mock(side_effect=optics)
    worker._prepare_focus = (lambda: events.append("focus")) if focus_mode == "contrast" else None
    worker.liveController = SimpleNamespace(
        get_observation_state_by_name=lambda _: object(),
        obs_controller=SimpleNamespace(turn_off_illumination=lambda: events.append("illumination off")),
    )
    worker._select_config = lambda _: events.append("AF channel")
    worker.do_reflection_af = focus_mode != "contrast"
    worker.laser_auto_focus_controller = SimpleNamespace(_timing=None)
    worker._timing = SimpleNamespace(get_timer=lambda _: nullcontext())
    worker._laser_af_refresh_every_n_fovs = 1 if focus_mode == "legacy" else 10
    worker._laser_af_seed_mode = "scan"
    worker._initialize_legacy_region_entries = lambda: events.append("focus")
    worker._seed_fov_z_map = lambda: events.append("focus")
    errors = []

    def run():
        try:
            worker._prepare_acquisition_once()
            worker._initialize_acquisition_focus_once()
            events.append("capture")
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=run, daemon=True)
    thread.start()
    try:
        assert optics_ready.wait(5)
        assert "focus" not in events
        compute_release.set()
        assert upload_waiting.wait(5)
        assert "compute ready" in events and "focus" not in events
        if failure:
            worker._upload_registration.set_exception(RuntimeError("upload unavailable"))
        else:
            events.append("upload ready")
            worker._upload_registration.set_result(None)
        thread.join(5)
        assert not thread.is_alive()
        if failure:
            assert aborted[0]
            assert len(errors) == 1 and str(errors[0]) == "upload unavailable"
            assert "focus" not in events and "capture" not in events
        else:
            assert not errors
            assert events[-2:] == ["focus", "capture"]
            worker._prepare_acquisition_once()
            worker._initialize_acquisition_focus_once()
            worker._prewarm_postprocess_routines.assert_called_once()
            worker._prewarm_observation_states.assert_called_once()
            worker._backpressure.reset.assert_called_once()
            assert events.count("focus") == 1
    finally:
        compute_release.set()
        if not worker._upload_registration.done():
            worker._upload_registration.set_result(None)
        thread.join(5)


def test_early_warmup_is_not_redispatched_or_backpressure_reset_before_focus():
    worker = MultiPointWorker.__new__(MultiPointWorker)
    worker.abort_requested_fn = lambda: False
    worker.request_abort_fn = Mock()
    worker.validation_mode = False
    worker._backpressure = Mock()
    worker._job_runners = []
    worker._prewarm_postprocess_routines = Mock(return_value=None)
    worker._prewarm_observation_states = Mock()
    worker._start_postprocess_warmup_once()  # after camera ROI seeding
    worker._prepare_acquisition_once()  # after remaining independent setup
    worker._prewarm_postprocess_routines.assert_called_once_with(wait=False)
    worker._backpressure.reset.assert_called_once()


def test_abort_during_upload_wait_does_not_start_focus():
    worker = MultiPointWorker.__new__(MultiPointWorker)
    aborted = Event()
    waiting = Event()
    worker.abort_requested_fn = aborted.is_set
    worker.request_abort_fn = aborted.set
    worker.validation_mode = False
    worker._log = Mock()
    worker._backpressure = Mock()
    worker._job_runners = []
    worker._prewarm_postprocess_routines = Mock(return_value=None)
    worker._prewarm_observation_states = Mock()

    class Registration(Future):
        def result(self, timeout=None):
            waiting.set()
            return super().result(timeout)

    worker._upload_registration = Registration()
    worker._prepare_focus = Mock()
    thread = Thread(target=worker._prepare_acquisition_once, daemon=True)
    thread.start()
    try:
        assert waiting.wait(5)
        aborted.set()
        thread.join(5)
        assert not thread.is_alive()
        worker._initialize_acquisition_focus_once()
        worker._prepare_focus.assert_not_called()
        assert not getattr(worker, "_startup_prepared", False)
    finally:
        aborted.set()
        worker._upload_registration.set_result(None)
        thread.join(5)
