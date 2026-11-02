"""Pipeline measurements use local fixtures only; no hardware or network share."""

from dataclasses import dataclass
import json
import logging
import queue
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from control.core import pipeline_diagnostics as diagnostics
from control.core.backpressure import BackpressureController
from control.core.job_processing import Job, JobRunner
from control.core.upload_manager import SubmittedFile, UploadManagerEngine, UploadStore, _transfer_process
from control.core.zarr_upload import upload_one_file


@dataclass
class MeasuredJob(Job):
    fail: bool = False

    def run(self):
        with diagnostics.stage("reconstruction"):
            if self.fail:
                raise RuntimeError("intentional diagnostic test failure")
        return "done"


def test_queue_age_nested_stage_and_failed_completion(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(diagnostics.time, "monotonic", lambda: clock[0])
    tracker = diagnostics.WorkerDiagnostics()
    first, second = SimpleNamespace(), SimpleNamespace()
    tracker.dispatched(first)
    clock[0] = 102.0
    tracker.dispatched(second)
    clock[0] = 105.0
    with pytest.raises(RuntimeError), diagnostics.diagnostic_job(tracker, first):
        with diagnostics.stage("reconstruction"):
            clock[0] = 108.0
            snapshot = tracker.snapshot()
            assert snapshot["queued"] == 1
            assert snapshot["oldest_queued_s"] == 6
            assert snapshot["active_stage"] == "reconstruction"
            assert snapshot["active_stage_age_s"] == 3
        assert tracker.snapshot()["active_stage"] == "job_run"
        raise RuntimeError("failed job")
    snapshot = tracker.snapshot()
    assert snapshot["pending"] == snapshot["queued"] == 1
    assert snapshot["active_stage"] is None
    assert snapshot["stages"]["job_run"]["failures"] == 1
    assert snapshot["stages"]["reconstruction"]["total_s"] == 3
    assert snapshot["stages"]["queue_wait"]["total_s"] == 5
    with diagnostics.diagnostic_job(tracker, second):
        pass
    assert tracker.snapshot()["pending"] == 0


def test_dispatch_failure_does_not_leave_phantom_backlog(tmp_path, monkeypatch):
    runner = JobRunner(log_file_path=str(tmp_path / "main.log"))
    monkeypatch.setattr(runner._input_queue, "put_nowait", Mock(side_effect=queue.Full))
    try:
        with pytest.raises(queue.Full):
            runner.dispatch(MeasuredJob(capture_info=None, capture_image=None))
        assert runner._pipeline_diagnostics.snapshot()["pending"] == 0
        assert not runner.has_pending()
    finally:
        runner._input_queue.close()
        runner._output_queue.close()


def test_measurements_survive_windows_spawn_and_job_failure(tmp_path):
    runner = JobRunner(log_file_path=str(tmp_path / "main.log"))
    runner.start()
    try:
        assert runner.wait_ready(timeout_s=30)
        for fail in (False, True):
            runner.dispatch(MeasuredJob(capture_info=None, capture_image=None, fail=fail))
            result = runner.output_queue().get(timeout=30)
            assert (result.exception is not None) == fail
        snapshot = runner._pipeline_diagnostics.snapshot()
        assert snapshot["pending"] == 0
        assert snapshot["stages"]["job_run"]["count"] == 2
        assert snapshot["stages"]["job_run"]["failures"] == 1
        assert snapshot["stages"]["reconstruction"]["failures"] == 1
    finally:
        runner.shutdown(timeout_s=30)
    assert runner.exitcode == 0
    assert runner._pipeline_diagnostics.snapshot()["stages"]["finalize"]["count"] == 2


def test_report_cadence_and_event_snapshot(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(diagnostics.time, "monotonic", lambda: clock[0])
    tracker = diagnostics.WorkerDiagnostics()
    runner = SimpleNamespace(_pipeline_diagnostics=tracker, pid=123)
    reporter = diagnostics.PipelineReporter(logging.getLogger("pipeline-test"), "test-acquisition")
    reporter.report([(MeasuredJob, runner)])
    clock[0] += 10
    tracker.observe("reconstruction", 2)
    reporter.report([(MeasuredJob, runner)])  # suppressed periodic report
    reporter.report([(MeasuredJob, runner)], "throttle")
    records = [json.loads(line) for line in reporter.writer.path.read_text().splitlines()]
    assert len(records) == 2
    assert records[-1]["reason"] == "throttle"
    assert records[-1]["window_s"] == 10
    assert records[-1]["workers"][0]["interval_stages"]["reconstruction"]["total_s"] == 2


def test_detail_write_failure_falls_back_to_log(tmp_path):
    logger = Mock()
    writer = diagnostics.DetailWriter("test", logger, tmp_path)
    writer.path.parent.write_text("blocks directory creation")
    writer.write({"kind": "test", "value": 42})
    logger.warning.assert_called_once()
    assert json.loads(logger.info.call_args.args[-1])["value"] == 42
    writer.write({"kind": "test"})
    logger.warning.assert_called_once()


def test_busy_worker_counter_does_not_block_summary(tmp_path):
    tracker = diagnostics.WorkerDiagnostics()
    held, release = threading.Event(), threading.Event()

    def hold_counter():
        with tracker.values.get_lock():
            held.set()
            release.wait(timeout=10)

    thread = threading.Thread(target=hold_counter)
    thread.start()
    try:
        assert held.wait(timeout=5)
        reporter = diagnostics.PipelineReporter(logging.getLogger("busy-test"), "test")
        reporter.report([(MeasuredJob, SimpleNamespace(_pipeline_diagnostics=tracker, pid=123))])
        record = json.loads(reporter.writer.path.read_text())
        assert "unavailable" in record["workers"][0]
    finally:
        release.set()
        thread.join(timeout=5)


@pytest.mark.parametrize("reason, jobs, mib", [("jobs", 1, 0), ("bytes", 5, 1), ("jobs+bytes", 1, 1)])
def test_throttle_timeout_reports_reason_and_duration(monkeypatch, reason, jobs, mib):
    from control.core import backpressure
    logger = Mock()
    monkeypatch.setattr(backpressure, "log", logger)
    controller = BackpressureController(enabled=True, max_jobs=jobs, max_mb=1, timeout_s=0)
    try:
        controller.job_dispatched(mib * 1048576)
        assert controller.wait_for_capacity() is False
        call = logger.info.call_args.args
        message = call[0] % call[1:]
        assert f"reason={reason}, outcome=timeout, waited=" in message
        assert call[2] >= 0
    finally:
        controller.close()


def test_copy_retries_preserve_verification_and_stage_failure_counts(tmp_path, monkeypatch):
    from control.core import zarr_upload
    source, target = tmp_path / "source", tmp_path / "remote" / "target"
    source.write_bytes(b"verified contents")
    real_copy = zarr_upload._stream_copy_with_hash
    # On the successful retry actually write the requested temporary destination.
    attempts = []
    def flaky_copy(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("transient failure")
        return real_copy(*args, **kwargs)
    monkeypatch.setattr(zarr_upload, "_stream_copy_with_hash", flaky_copy)
    timings = {}
    ok, _, size, error = upload_one_file(str(source), str(target), log=logging.getLogger("copy-test"),
                                        stable_read=True, initial_backoff_s=0, timings=timings)
    assert ok and error is None and size == len(source.read_bytes())
    assert target.read_bytes() == source.read_bytes()
    assert timings["copy_and_source_hash"]["count"] == 2
    assert timings["copy_and_source_hash"]["failures"] == 1
    assert timings["retry_backoff"]["count"] == 1
    assert {"source_recheck", "remote_readback_hash", "remote_rename"} <= timings.keys()


def test_upload_detail_summary_and_blocked_backlog(tmp_path):
    store = UploadStore(tmp_path / "state" / "uploads.sqlite3")
    local, remote = tmp_path / "local", tmp_path / "remote"
    local.mkdir()
    remote.mkdir()
    store.register_dataset(dataset_id="test", name="test", local_root=str(local), remote_root=str(remote))
    for name in ("first", "retry", "ready"):
        (local / name).write_bytes(b"content")
    store.submit_batch("test", "batch", [SubmittedFile(name, 7, deletable=True) for name in ("first", "retry", "ready")])
    engine = UploadManagerEngine(store)
    # Execute the same child entry point synchronously with a local result queue.
    engine._results.close()
    engine._results = queue.Queue()
    try:
        item = store.claim_next()
        _transfer_process(item, engine._results)
        engine._collect_results()
        engine._delete_verified_files()
        with store._db:
            store._db.execute("UPDATE files SET retry_after_epoch=9999999999 WHERE relative_path='retry'")
        backlog = store.diagnostic_backlog()
        assert backlog["pending_files"] == 2 and backlog["pending_bytes"] == 14
        assert backlog["ready_files"] == backlog["retry_wait_files"] == 1
        with store._db:
            store._db.execute("UPDATE datasets SET state='paused'")
        assert store.diagnostic_backlog()["ready_files"] == 0
        engine._report_diagnostics(force=True)
        records = [json.loads(line) for line in engine._diagnostic_writer.path.read_text().splitlines()]
        assert [row["kind"] for row in records] == ["transfer", "delete", "upload_summary"]
        assert records[0]["stages"]["remote_readback_hash"]["count"] == 1
        assert records[0]["outside_worker_s"] is None  # no process launched by scheduler here
        assert records[-1]["totals"]["verified_bytes"] == records[-1]["totals"]["deleted_bytes"] == 7
        assert records[-1]["backlog"]["pending_files"] == 2
        assert (remote / "first").read_bytes() == b"content"
        assert not (local / "first").exists()
    finally:
        store.close()
