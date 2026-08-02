"""JobRunner shutdown must RUN every already-dispatched job before exiting.

Regression tests for the silent-drop bug: the subprocess loop used to exit on
the shutdown *event* (set before the sentinel was enqueued), abandoning every
job still queued — frames vanished and upload barriers never staged, while
finalize stamped ``acquisition_complete=true`` over the truncated data. The
loop is now sentinel-driven: everything dispatched before ``shutdown()`` runs
first; the parent's join-timeout + terminate() stays the only hard stop.
"""

import time
from dataclasses import dataclass

import numpy as np

import squid.abc
from control.core.job_processing import Job, JobRunner, JobImage, CaptureInfo
from control.models.observation_state import ObservationState, CameraSettings, IlluminatorState


def _make_obs_state(name="BF LED matrix full"):
    return ObservationState(
        name=name,
        camera_settings=CameraSettings(exposure_time_ms=10.0, gain_mode=1.0),
        illuminator_states=[IlluminatorState(illumination_channel=name, intensity=50.0, on=True)],
    )


def _capture_info() -> CaptureInfo:
    return CaptureInfo(
        position=squid.abc.Pos(x_mm=0.0, y_mm=0.0, z_mm=0.0, theta_rad=None),
        z_index=0,
        capture_time=time.time(),
        observation_state=_make_obs_state(),
        save_directory="/tmp/test",
        file_id="test_0_0",
        region_id="A1",
        fov=0,
        configuration_idx=0,
    )


@dataclass
class SlowJob(Job):
    duration_s: float = 0.1
    result_value: str = "done"

    def run(self):
        time.sleep(self.duration_s)
        return self.result_value


def _slow_job(duration_s: float, result_value: str) -> SlowJob:
    return SlowJob(
        capture_info=_capture_info(),
        capture_image=JobImage(image_array=np.zeros((10, 10), dtype=np.uint16)),
        duration_s=duration_s,
        result_value=result_value,
    )


def _drain_results(queue_obj, expected, timeout_s=10.0):
    import queue as _queue

    results = []
    deadline = time.time() + timeout_s
    while len(results) < expected and time.time() < deadline:
        try:
            results.append(queue_obj.get_nowait())
        except _queue.Empty:
            time.sleep(0.02)
    return results


def test_queued_jobs_run_to_completion_during_shutdown():
    """Every job dispatched before shutdown() must produce a result, even
    when shutdown is requested while the backlog is still queued."""
    runner = JobRunner()
    runner.daemon = True
    runner.start()
    assert runner.wait_ready(timeout_s=10.0)

    n_jobs = 6
    for i in range(n_jobs):
        runner.dispatch(_slow_job(0.15, f"job-{i}"))

    out_queue = runner.output_queue()
    # Generous budget: the whole point is that the backlog (~0.9s) finishes.
    # close_output_queue=False keeps the parent-side queue readable afterwards
    # (in production the upload drainer owns reading it).
    runner.shutdown(timeout_s=30.0, close_output_queue=False)
    assert not runner.is_alive()

    results = _drain_results(out_queue, n_jobs)
    got = sorted(r.result for r in results)
    assert got == [f"job-{i}" for i in range(n_jobs)], (
        f"jobs dropped during shutdown: expected {n_jobs} results, got {got}"
    )
    out_queue.close()
    out_queue.cancel_join_thread()


def test_shutdown_keep_output_queue_open_leaves_handle_usable():
    """close_output_queue=False must leave output_queue() readable (the
    background upload drainer polls it after the runner exits)."""
    runner = JobRunner()
    runner.daemon = True
    runner.start()
    assert runner.wait_ready(timeout_s=10.0)

    runner.dispatch(_slow_job(0.05, "only"))
    out_queue = runner.output_queue()
    runner.shutdown(timeout_s=15.0, close_output_queue=False)

    assert runner.output_queue() is not None
    results = _drain_results(out_queue, 1)
    assert len(results) == 1 and results[0].result == "only"
    out_queue.close()
    out_queue.cancel_join_thread()
