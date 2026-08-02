"""Regression tests for the end-of-run upload drain.

Two live acquisitions ended with the drainer reporting thousands of
"outstanding" uploads and force-terminating the UploadWorker, even though the
manifest showed every shard had already been verified on the remote. The
outstanding count was a phantom: task_ids were tracked in a set that a
BarrierResult could re-populate *after* the matching UploadResult had already
been consumed, so nothing would ever discard them again. Accounting is now
``worker.tasks_submitted - results_received``, which cannot be perturbed by
BarrierResult delivery order.

Also covered: the post-finalize metadata resync (which must see every plate,
not just the dense one, and must not run before the writers finalize) and the
failure log line (which must name the files that failed).

Hardware-free: fakes stand in for the UploadWorker and the runner queues.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from control.core.multi_point_worker import (
    _BackgroundUploadDrainer,
    format_upload_failure,
)
from control.core.zarr_upload import UploadResult, UploadTarget


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeQueue:
    """Minimal stand-in for a multiprocessing output queue."""

    def __init__(self, items=None):
        self._items = list(items or [])

    def put(self, item):
        self._items.append(item)

    def get_nowait(self):
        import queue as _q

        if not self._items:
            raise _q.Empty
        return self._items.pop(0)

    def empty(self):
        return not self._items


class _FakeWorker:
    """UploadWorker stand-in: a submitted counter and an output queue."""

    def __init__(self):
        self.tasks_submitted = 0
        self.output_queue = _FakeQueue()
        self.heartbeat = time.time()
        self.terminated = False
        self.submitted_tasks = []

    def submit(self, task):
        self.submitted_tasks.append(task)
        self.tasks_submitted += 1

    def is_alive(self):
        return not self.terminated

    def force_stop(self, *a, **kw):
        self.terminated = True

    def shutdown(self, *a, **kw):
        self.terminated = True

    def release_queue_resources(self):
        pass

    def close(self):
        pass


def _drainer(tmp_path, worker=None, runners_done=None, target=None, **kw):
    worker = worker if worker is not None else _FakeWorker()
    return _BackgroundUploadDrainer(
        upload_worker=worker,
        upload_target=target,
        tasks_by_tp={},
        results_by_tp={},
        expected_by_tp={},
        deletion_done=set(),
        failed_tasks=[],
        completed_task_ids=set(),
        results_received=0,
        zarr_writer_info=None,
        experiment_path=str(tmp_path),
        runners_done_event=runners_done,
        **kw,
    )


def _result(task_id, tp=0, success=True, failed_paths=None):
    return UploadResult(
        task_id=task_id,
        time_point=tp,
        region_id="A1",
        fov=0,
        success=success,
        uploaded_paths=[],
        failed_paths=failed_paths or [],
        error=None if success else f"{len(failed_paths or [])} file(s) failed",
    )


class _Barrier:
    def __init__(self, task_id, tp=0, submitted=True):
        self.task_id = task_id
        self.time_point = tp
        self.region_id = "A1"
        self.fov = 0
        self.submitted = submitted


# ---------------------------------------------------------------------------
# Phantom-outstanding accounting
# ---------------------------------------------------------------------------


class TestOutstandingAccounting:
    def test_outstanding_is_zero_once_every_result_is_in(self, tmp_path):
        worker = _FakeWorker()
        d = _drainer(tmp_path, worker=worker)
        for i in range(3):
            worker.submit(object())
            worker.output_queue.put(_result(f"task{i}"))
        assert d._outstanding() == 3
        d._drain_available_results()
        assert d._outstanding() == 0

    def test_barrier_arriving_after_its_result_creates_no_phantom(self, tmp_path):
        """THE regression. UploadResults and BarrierResults travel on two
        independent queues, so a fast task's result can be consumed before the
        barrier that registers its task_id. The old set-based accounting then
        re-added an id nothing would ever discard, and the drain terminated on
        a stall with a nonzero phantom count."""
        worker = _FakeWorker()
        d = _drainer(tmp_path, worker=worker)
        worker.submit(object())

        # Result first...
        worker.output_queue.put(_result("task-fast"))
        d._drain_available_results()
        assert d._outstanding() == 0

        # ...barrier second. Must not resurrect the task.
        d._note_barrier(_Barrier("task-fast"))
        assert d._outstanding() == 0
        assert "task-fast" not in d._tasks_by_tp.get(0, set())

    def test_many_late_barriers_do_not_inflate_outstanding(self, tmp_path):
        """Shape of the real failure: every task completed, then a burst of
        late barriers arrived during the finalize window."""
        worker = _FakeWorker()
        d = _drainer(tmp_path, worker=worker)
        ids = [f"task{i}" for i in range(50)]
        for tid in ids:
            worker.submit(object())
            worker.output_queue.put(_result(tid, tp=int(tid[4:]) % 5))
        d._drain_available_results()
        assert d._outstanding() == 0

        for tid in ids:
            d._note_barrier(_Barrier(tid, tp=int(tid[4:]) % 5))
        assert d._outstanding() == 0

    def test_outstanding_never_goes_negative(self, tmp_path):
        """A duplicate/extra result must not drive the count below zero, which
        would make the drain loop's `outstanding == 0` exit unreachable."""
        worker = _FakeWorker()
        d = _drainer(tmp_path, worker=worker)
        worker.submit(object())
        worker.output_queue.put(_result("task0"))
        worker.output_queue.put(_result("task0"))
        d._drain_available_results()
        assert d._outstanding() == 0


# ---------------------------------------------------------------------------
# Failure reporting
# ---------------------------------------------------------------------------


class TestFailureReporting:
    def test_failed_paths_are_named(self):
        """`UploadResult.error` is only an aggregate; the per-file reasons live
        in a subprocess whose log output reaches no file. Without this the
        cause of an upload failure is unrecoverable after the run."""
        r = _result(
            "t1",
            success=False,
            failed_paths=[
                (r"C:\exp\plate.ome.zarr\A\1\0\frame_times\c\0\0\0", "OSError: [WinError 5]"),
                (r"C:\exp\plate.ome.zarr\A\1\0\zarr.json", "sha256 mismatch after copy"),
            ],
        )
        msg = format_upload_failure(r)
        assert "2 file(s) failed" in msg
        assert "WinError 5" in msg
        assert "sha256 mismatch" in msg
        assert "zarr.json" in msg

    def test_long_failure_list_is_capped(self):
        r = _result(
            "t1",
            success=False,
            failed_paths=[(f"/exp/f{i}.bin", "boom") for i in range(20)],
        )
        msg = format_upload_failure(r, limit=3)
        assert "(+17 more)" in msg
        assert msg.count("boom") == 3

    def test_aggregate_only_result_still_renders(self):
        r = _result("t1", success=False, failed_paths=[])
        r.error = "5 file(s) failed"
        assert format_upload_failure(r) == "5 file(s) failed"


# ---------------------------------------------------------------------------
# Post-finalize metadata resync
# ---------------------------------------------------------------------------


def _fake_experiment(root, plates):
    """Build an experiment tree with one FOV group per plate name."""
    for plate in plates:
        fov = os.path.join(root, f"{plate}.ome.zarr", "A", "1", "0")
        os.makedirs(os.path.join(fov, "0"), exist_ok=True)
        os.makedirs(os.path.join(fov, "frame_times", "c", "0", "0"), exist_ok=True)
        for p in (
            os.path.join(root, f"{plate}.ome.zarr", "zarr.json"),      # plate root
            os.path.join(root, f"{plate}.ome.zarr", "A", "1", "zarr.json"),  # well
            os.path.join(fov, "zarr.json"),                            # FOV group
            os.path.join(fov, "0", "zarr.json"),                       # level
            os.path.join(fov, "frame_times", "zarr.json"),
        ):
            with open(p, "w", encoding="utf-8") as f:
                f.write("{}")
        with open(os.path.join(fov, "frame_times", "c", "0", "0", "0"), "wb") as f:
            f.write(b"\x00" * 16)


class TestMetadataResync:
    PLATES = ["plate", "DPC_circ_bot_phase", "BF_LED_matrix_full", "BF_LED_matrix_full_refz"]

    def test_resync_covers_every_plate_not_just_the_dense_one(self, tmp_path):
        """It used to resolve paths through ZarrWriterInfo with array_key=None,
        so ragged/derived plates never got their finalized zarr.json or their
        frame_times chunk re-pushed."""
        _fake_experiment(str(tmp_path), self.PLATES)
        worker = _FakeWorker()
        target = UploadTarget(
            enabled=True,
            remote_root=str(tmp_path / "remote"),
            local_base=str(tmp_path),
            delete_after_verify=False,
        )
        d = _drainer(tmp_path, worker=worker, target=target)
        d._enqueue_post_finalize_metadata_resync()

        sent = {local for task in worker.submitted_tasks for local, _remote in task.files}
        for plate in self.PLATES:
            base = os.path.join(str(tmp_path), f"{plate}.ome.zarr")
            assert os.path.join(base, "zarr.json") in sent, f"{plate} plate root missing"
            fov = os.path.join(base, "A", "1", "0")
            assert os.path.join(fov, "zarr.json") in sent, f"{plate} FOV group missing"
            assert os.path.join(fov, "0", "zarr.json") in sent, f"{plate} level missing"
            chunk = os.path.join(fov, "frame_times", "c", "0", "0", "0")
            assert chunk in sent, f"{plate} frame_times chunk missing"

    def test_frame_times_chunk_has_a_resync_uploader(self, tmp_path):
        """frame_times/c/0/0/0 was removed from the per-barrier metadata set to
        avoid the Windows rename-vs-open collision, which makes this pass its
        ONLY uploader. If it stops covering the chunk, remote timestamps are
        silently lost rather than merely stale."""
        _fake_experiment(str(tmp_path), ["plate"])
        worker = _FakeWorker()
        target = UploadTarget(
            enabled=True,
            remote_root=str(tmp_path / "remote"),
            local_base=str(tmp_path),
            delete_after_verify=False,
        )
        d = _drainer(tmp_path, worker=worker, target=target)
        d._enqueue_post_finalize_metadata_resync()

        sent = {local for task in worker.submitted_tasks for local, _remote in task.files}
        chunk = os.path.join(
            str(tmp_path), "plate.ome.zarr", "A", "1", "0", "frame_times", "c", "0", "0", "0"
        )
        assert chunk in sent

    def test_resync_files_are_never_marked_deletable(self, tmp_path):
        _fake_experiment(str(tmp_path), ["plate"])
        worker = _FakeWorker()
        target = UploadTarget(
            enabled=True,
            remote_root=str(tmp_path / "remote"),
            local_base=str(tmp_path),
            delete_after_verify=True,
        )
        d = _drainer(tmp_path, worker=worker, target=target)
        d._enqueue_post_finalize_metadata_resync()
        for task in worker.submitted_tasks:
            assert task.deletable_local_paths == set()


class TestResyncWaitsForFinalize:
    def test_wait_returns_only_once_runners_are_done(self, tmp_path):
        """The drain is handed off while finalize_all_writers() is still
        running in the subprocesses. Resyncing then would ship a pre-finalize
        frame_times and re-open the rename collision the per-barrier removal
        was meant to close."""
        worker = _FakeWorker()
        done = threading.Event()
        d = _drainer(tmp_path, worker=worker, runners_done=done)

        finished = threading.Event()

        def _wait():
            d._wait_for_runners_to_exit("test")
            finished.set()

        t = threading.Thread(target=_wait, daemon=True)
        t.start()
        assert not finished.wait(timeout=0.5), "returned before runners_done was set"

        done.set()
        assert finished.wait(timeout=15.0), "did not return after runners_done was set"
        t.join(timeout=5.0)

    def test_wait_is_a_noop_when_already_done(self, tmp_path):
        done = threading.Event()
        done.set()
        d = _drainer(tmp_path, runners_done=done)
        t0 = time.time()
        d._wait_for_runners_to_exit("test")
        assert time.time() - t0 < 2.0

    def test_stop_request_breaks_the_wait(self, tmp_path):
        """App close must not be blocked for the full finalize budget."""
        d = _drainer(tmp_path, runners_done=threading.Event())
        finished = threading.Event()

        def _wait():
            d._wait_for_runners_to_exit("test")
            finished.set()

        threading.Thread(target=_wait, daemon=True).start()
        time.sleep(0.2)
        d._stop_requested.set()
        assert finished.wait(timeout=15.0), "wait ignored the stop request"
