"""Final QPM previews must cross the process queue before worker shutdown."""

import time
from dataclasses import dataclass
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from control.core.acquisition_preview import AcquisitionPreview, PreviewStarted, PreviewCaptured, PreviewFrame
from control.core.job_processing import Job, JobImage, JobRunner, PostprocessJob, PostprocessResult
from control.core.multi_point_worker import MultiPointWorker


class DelayedPreviewResult:
    def __reduce__(self):
        # Serialization runs in Queue's feeder AFTER the job's pending counter
        # reaches zero. Reproduce this window without relying on machine speed.
        time.sleep(0.5)
        return PostprocessResult, (
            "pp0", 0, "A1", 0, 2, [],
            {"BF_phase": np.full((1200, 1200), 0.25, np.float32),
             "BF_bf_center": np.full((1200, 1200), 1000, np.uint16)},
        )


@dataclass
class PreviewJob(Job):
    def run(self):
        return DelayedPreviewResult()


def _worker(runner):
    preview = AcquisitionPreview()
    preview.mode = "position"
    key = ("run", 0, "A1", 0)
    preview.accept(PreviewStarted(key, 2, ("pp0",)))
    preview.accept(PreviewFrame(key, "fluorescence", np.ones((4, 4)), 0, 1))
    preview.accept(PreviewFrame(key, "BF", np.ones((4, 4)), 0, 1))
    preview.accept(PreviewCaptured(key))
    assert preview.snapshot() is None
    complete = Event()
    worker = MultiPointWorker.__new__(MultiPointWorker)
    worker.experiment_path = "run"
    worker._job_runners = [(PostprocessJob, runner)]
    worker._log = Mock()
    worker._report_pipeline_health = Mock()
    worker._spawn_background_upload_drainer = Mock()
    worker._drain_upload_results = Mock()
    worker._backpressure = Mock()
    worker._zarr_writer_info = None
    worker._upload_target = None
    worker._slack_notifier = None
    worker._acquisition_error_count = 0
    worker.callbacks = SimpleNamespace(
        signal_acquisition_preview=preview.accept, signal_data_writing_complete=complete.set
    )
    return worker, preview, complete


def test_finish_jobs_waits_for_delayed_qpm_preview_after_computation(tmp_path):
    runner = JobRunner(log_file_path=str(tmp_path / "preview-worker.log"))
    runner.start()
    try:
        assert runner.wait_ready(timeout_s=15)
        runner.dispatch(PreviewJob(capture_info=None, capture_image=JobImage(None)))
        deadline = time.monotonic() + 10
        while runner.has_pending() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not runner.has_pending()

        worker, preview, complete = _worker(runner)
        worker._finish_jobs(timeout_s=10)
        snapshot = preview.snapshot()
        assert snapshot is not None and snapshot.complete
        assert set(snapshot.images) == {"fluorescence", "BF", "BF_phase", "BF_bf_center"}
        assert snapshot.images["BF_phase"].shape == (1200, 1200)
        assert snapshot.images["BF_phase"][0, 0] == 0.25
        assert snapshot.images["BF_bf_center"][0, 0] == 1000
        assert not worker._postprocess_result_barriers
        assert complete.wait(15)
    finally:
        # _finish_jobs normally owns shutdown in a background thread.
        if runner.is_alive():
            runner.terminate()
            runner.join(timeout=5)


def test_preview_barrier_timeout_remains_bounded():
    from queue import Queue

    queue = Queue()
    runner = SimpleNamespace(
        has_pending=lambda: False, dispatch=lambda job: True, output_queue=lambda: queue,
        has_upload_pipeline=lambda: False, shutdown=Mock(),
    )
    worker, preview, complete = _worker(runner)
    worker._finish_jobs(timeout_s=0.05)
    assert complete.wait(5)
    assert preview.snapshot() is None
    assert worker._postprocess_result_barriers
    worker._log.warning.assert_called_with("Postprocess preview delivery did not finish within the job drain timeout.")
