"""Verify worker file verbosity through an actual subprocess and queued job."""

from dataclasses import dataclass

import pytest

import squid.logging
from control.core.job_processing import Job, JobRunner


@dataclass
class LoggingJob(Job):
    def run(self):
        log = squid.logging.get_logger("WorkerLoggingTest")
        log.debug("worker-test debug detail")
        log.info("worker-test info summary")
        log.warning("worker-test warning detail")
        log.error("worker-test error detail")
        return "done"


@pytest.mark.parametrize(
    "env_level, expect_debug, expect_info",
    [(None, False, True), (" debug ", True, True), ("WARNING", False, False), ("BASIC_FORMAT", False, True)],
)
def test_worker_log_level(tmp_path, monkeypatch, env_level, expect_debug, expect_info):
    if env_level is None:
        monkeypatch.delenv("SQUID_WORKER_LOG_LEVEL", raising=False)
    else:
        monkeypatch.setenv("SQUID_WORKER_LOG_LEVEL", env_level)

    runner = JobRunner(log_file_path=str(tmp_path / "main.log"))
    runner.start()
    try:
        assert runner.wait_ready(timeout_s=30.0)
        runner.dispatch(LoggingJob(capture_info=None, capture_image=None))
        result = runner.output_queue().get(timeout=30.0)
        assert result.result == "done"
        assert result.exception is None
    finally:
        runner.shutdown(timeout_s=30.0)

    assert runner.exitcode == 0
    contents = (tmp_path / "main_worker.log").read_text(encoding="utf-8")
    assert ("worker-test debug detail" in contents) is expect_debug
    assert ("worker-test info summary" in contents) is expect_info
    assert "worker-test warning detail" in contents
    assert "worker-test error detail" in contents
    if expect_debug:
        assert contents.count("result queued.") == 1
