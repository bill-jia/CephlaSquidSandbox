"""Acquisition boundaries and derived results produce explicit preview events."""

from unittest.mock import MagicMock

import numpy as np

from control.core.acquisition_preview import PreviewStarted, PreviewCaptured, PreviewProcessed
from control.core.job_processing import JobImage, PostprocessJob, PostprocessResult
from control.models.acquisition_cycle import AcquisitionCycle, CycleStep, PostprocessSpec, resolve_cycle
from tests.control.test_multipoint_observation_snaking import _worker


def test_worker_announces_whole_position_including_reference_only_and_processing():
    events = resolve_cycle(
        AcquisitionCycle(
            name="preview",
            items=[
                CycleStep(observation_state="stack", postprocess=PostprocessSpec(routine="mean")),
                CycleStep(observation_state="reference", acquire_z_stack=False),
            ],
        )
    )
    worker = _worker(events, nz=4, mode="FROM CENTER", piezo=False, snake=True)
    worker.experiment_path = "run"
    worker.acquire_camera_image = MagicMock()
    worker.acquire_at_position("R0", ".", 1)
    started, captured = [call.args[0] for call in worker.callbacks.signal_acquisition_preview.call_args_list]
    assert isinstance(started, PreviewStarted)
    assert started.key == ("run", 0, "R0", 1)
    assert started.expected_frames == 5
    assert set(started.groups) == set(worker._global_plan.postprocess_groups)
    assert captured == PreviewCaptured(started.key)


def test_processed_previews_and_failures_are_sent_even_without_a_legacy_display_frame():
    from control.core.multi_point_worker import MultiPointWorker

    worker = MultiPointWorker.__new__(MultiPointWorker)
    worker.experiment_path = "run"
    worker.callbacks = MagicMock()
    result = PostprocessResult(
        "process",
        2,
        "A1",
        3,
        1,
        display_images={"derived": np.ones((4, 4))},
        display_images_last={"derived": np.full((4, 4), 2)},
    )
    worker._emit_postprocess_display(result)
    event = worker.callbacks.signal_acquisition_preview.call_args.args[0]
    assert isinstance(event, PreviewProcessed)
    assert event.key == ("run", 2, "A1", 3)
    assert event.middle["derived"][0, 0] == 1
    assert event.last["derived"][0, 0] == 2
    result.error = "failed"
    result.display_images = {}
    worker._emit_postprocess_display(result)
    assert worker.callbacks.signal_acquisition_preview.call_args.args[0].error == "failed"


def test_processing_write_failure_returns_keyed_failure(monkeypatch):
    from types import SimpleNamespace

    info = SimpleNamespace(
        time_point=0,
        region_id="A1",
        fov=0,
        observation_state=SimpleNamespace(name="raw"),
        state_frame_index=0,
        z_index=0,
        capture_time=0,
    )
    job = PostprocessJob(
        info,
        JobImage(np.zeros((4, 4))),
        group_key="g",
        label="derived",
        expected_frames=1,
        output_specs=[{"name": "out"}],
    )
    monkeypatch.setattr(job, "_compute", lambda _: {"out": np.zeros((4, 4))})
    monkeypatch.setattr(job, "_coerce_output", lambda arr, *args: arr)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(job, "_write_output", fail)
    result = job.run()
    assert result.group_key == "g" and result.region_id == "A1"
    assert result.error == "derived_out: disk full"
