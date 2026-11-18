"""Passive acquisition pipeline accounting; no image data or hardware IO."""

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import json
import multiprocessing
import os
from pathlib import Path
import threading
import time

import squid.logging


STAGES = ("job_run", "queue_wait", "reconstruction", "derived_write", "zarr_flush",
          "upload_submit", "preview_prepare", "finalize")
_tracker = ContextVar("pipeline_tracker", default=None)


class WorkerDiagnostics:
    """Spawn-safe shared totals plus a parent-owned FIFO of dispatch metadata.

    Callers serialize dispatch through dispatch_lock, including queue.put, so
    completed sequence numbers retire the right metadata even with producers
    on different threads. Metadata is retired on dispatch and snapshot.
    """

    def __init__(self):
        self.dispatch_lock = multiprocessing.RLock()
        # started sequence, completed sequence, active stage, stage start time;
        # then count/total/max/failures for each named stage.
        self.values = multiprocessing.Array("d", 4 + 4 * len(STAGES))
        self.pending = deque()
        self.sequence = 0

    def _retire(self):
        done = self.values[1]
        while self.pending and self.pending[0][0] <= done:
            self.pending.popleft()

    def dispatched(self, job):
        self._retire()
        self.sequence += 1
        now = time.monotonic()
        image = getattr(getattr(job, "capture_image", None), "image_array", None)
        self.pending.append((self.sequence, now, type(job).__name__, int(getattr(image, "nbytes", 0))))
        job._pipeline_sequence, job._pipeline_dispatched = self.sequence, now

    def rollback(self):
        self.pending.pop()

    def started(self, job):
        with self.values.get_lock():
            self.values[0] = job._pipeline_sequence
        self.observe("queue_wait", time.monotonic() - job._pipeline_dispatched)

    def completed(self, job):
        with self.values.get_lock():
            self.values[1] = job._pipeline_sequence

    def observe(self, name, seconds, failed=False):
        offset = 4 + 4 * STAGES.index(name)
        with self.values.get_lock():
            self.values[offset] += 1
            self.values[offset + 1] += seconds
            self.values[offset + 2] = max(self.values[offset + 2], seconds)
            self.values[offset + 3] += int(failed)

    @contextmanager
    def stage(self, name):
        started = time.monotonic()
        with self.values.get_lock():
            previous = (self.values[2], self.values[3])
            self.values[2:4] = (STAGES.index(name) + 1, started)
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            self.observe(name, time.monotonic() - started, failed)
            with self.values.get_lock():
                self.values[2:4] = previous

    def snapshot(self):
        # A terminated worker can leave a shared lock held. Reporting must not
        # wedge acquisition or its writer-completion notification in that case.
        if not self.dispatch_lock.acquire(timeout=0.05):
            raise TimeoutError("dispatch metadata busy")
        try:
            lock = self.values.get_lock()
            if not lock.acquire(timeout=0.05):
                raise TimeoutError("worker counters busy or worker stopped while holding lock")
            try:
                values = list(self.values)
            finally:
                lock.release()
            while self.pending and self.pending[0][0] <= values[1]:
                self.pending.popleft()
            pending = list(self.pending)
        finally:
            self.dispatch_lock.release()
        now = time.monotonic()
        active = next((p for p in pending if p[0] == values[0] and p[0] > values[1]), None)
        queued = [p for p in pending if p[0] > values[0]]
        stages = {}
        for i, name in enumerate(STAGES):
            count, total, maximum, failures = values[4 + 4*i:8 + 4*i]
            stages[name] = dict(count=int(count), total_s=total, max_s=maximum,
                                mean_s=total / count if count else None, failures=int(failures))
        return dict(pending=len(pending), queued=len(queued), active_job=active[2] if active else None,
                    queued_image_mib=sum(p[3] for p in queued) / 1048576,
                    oldest_pending_s=now-pending[0][1] if pending else None,
                    oldest_queued_s=now-queued[0][1] if queued else None,
                    completed_sequence=int(values[1]), active_stage=STAGES[int(values[2])-1] if values[2] else None,
                    active_stage_age_s=now-values[3] if values[2] else None, stages=stages)


@contextmanager
def diagnostic_job(tracker, job):
    tracker.started(job)
    token = _tracker.set(tracker)
    try:
        with tracker.stage("job_run"):
            yield
    finally:
        _tracker.reset(token)
        tracker.completed(job)


@contextmanager
def stage(name):
    tracker = _tracker.get()
    if tracker is None:
        yield
    else:
        with tracker.stage(name):
            yield


class DetailWriter:
    def __init__(self, prefix, logger, directory=None):
        main = squid.logging.get_current_log_file_path()
        directory = directory or (Path(main).parent if main else squid.logging.get_default_log_directory())
        self.path = Path(directory) / "pipeline_diagnostics" / f"{prefix}-{os.getpid()}-{time.monotonic_ns()}.jsonl"
        self.logger = logger
        self.lock = threading.Lock()
        self.failed = False
        logger.info("[PIPELINE] Detailed records: %s", self.path)

    def write(self, record):
        payload = json.dumps(dict(format=1, unix_s=time.time(), **record), default=str)
        with self.lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(payload + "\n")
                self.failed = False
            except OSError as exc:
                if not self.failed:
                    self.logger.warning("[PIPELINE] Cannot write %s: %s; preserving details in log", self.path, exc)
                self.failed = True
                self.logger.info("[PIPELINE] details %s", payload)


@contextmanager
def elapsed(metrics, name):
    """Optional local timings, including failed attempts; never log image data."""
    started = time.monotonic()
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        if metrics is not None:
            value = time.monotonic() - started
            row = metrics.setdefault(name, dict(count=0, total_s=0.0, max_s=0.0, failures=0))
            row["count"] += 1
            row["total_s"] += value
            row["max_s"] = max(row["max_s"], value)
            row["failures"] += int(failed)


class PipelineReporter:
    def __init__(self, logger, acquisition):
        self.writer = DetailWriter("acquisition", logger)
        self.logger, self.acquisition = logger, acquisition
        self.last = 0.0
        self.previous = {}

    def report(self, runners, reason="periodic"):
        now = time.monotonic()
        if reason == "periodic" and now - self.last < 60:
            return
        rows = []
        for job_class, runner in runners:
            tracker = getattr(runner, "_pipeline_diagnostics", None)
            if tracker is None:
                continue
            try:
                row = tracker.snapshot()
            except TimeoutError as exc:
                rows.append(dict(worker=job_class.__name__, pid=runner.pid, unavailable=str(exc)))
                self.logger.warning("[PIPELINE] %s: diagnostic snapshot unavailable: %s", job_class.__name__, exc)
                continue
            row.update(worker=job_class.__name__, pid=runner.pid)
            previous = self.previous.get(id(runner), {})
            row["interval_stages"] = {
                name: dict(count=s["count"]-previous.get(name, {}).get("count", 0),
                           total_s=s["total_s"]-previous.get(name, {}).get("total_s", 0))
                for name, s in row["stages"].items()
            }
            self.previous[id(runner)] = row["stages"]
            rows.append(row)
            slowest = max(row["interval_stages"], key=lambda n: row["interval_stages"][n]["total_s"]
                          if n not in ("job_run", "queue_wait") else -1)
            if not row["interval_stages"][slowest]["count"]:
                slowest = "job_run"
            self.logger.info(
                "[PIPELINE] %s %s: queued=%d, active=%s/%s (%.1fs), oldest queued=%.1fs; "
                "largest stage this interval=%s %.2fs; queue wait mean/max=%.2f/%.2fs",
                reason, row["worker"], row["queued"], row["active_job"] or "idle",
                row["active_stage"] or "idle", row["active_stage_age_s"] or 0, row["oldest_queued_s"] or 0,
                slowest, row["interval_stages"][slowest]["total_s"],
                row["stages"]["queue_wait"]["mean_s"] or 0, row["stages"]["queue_wait"]["max_s"],
            )
        self.writer.write(dict(kind="acquisition", acquisition=self.acquisition, reason=reason,
                               window_s=now-self.last if self.last else None, workers=rows))
        self.last = now
