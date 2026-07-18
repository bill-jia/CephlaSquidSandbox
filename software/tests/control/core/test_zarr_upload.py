"""Unit tests for the SMB upload pipeline (control.core.zarr_upload).

Hardware-free: exercises the UploadWorker against local temp directories
standing in for the remote share, so it validates the pipelined copy/verify,
per-task result aggregation, manifest integrity under concurrent lanes, the
heartbeat, force-stop, and the reachability probe without any rig.
"""

import os
import time

import pytest

import squid.logging
from control.core.zarr_upload import (
    UploadTarget,
    UploadTask,
    UploadWorker,
    collect_sidecar_files,
    drain_output_queue_nonblocking,
    local_to_remote_path,
    read_manifest,
    remote_root_reachable,
    upload_one_file,
    COPY_CHUNK_BYTES,
)

_log = squid.logging.get_logger("test_zarr_upload")


def _make_local_files(tmp_path, n, size=4096):
    local_base = tmp_path / "exp"
    local_base.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(n):
        p = local_base / f"file_{i}.bin"
        p.write_bytes(os.urandom(size))
        paths.append(str(p))
    return str(local_base), paths


def _task(task_id, files, local_base, remote_root, deletable=True):
    pairs = [(f, local_to_remote_path(f, local_base, remote_root)) for f in files]
    return UploadTask(
        task_id=task_id,
        time_point=0,
        region_id="A1",
        fov=0,
        files=pairs,
        deletable_local_paths=set(files) if deletable else set(),
        stable_read_paths=set(),
    )


def _drain(worker, expected, timeout=30.0):
    results = []
    deadline = time.time() + timeout
    while len(results) < expected and time.time() < deadline:
        results.extend(drain_output_queue_nonblocking(worker.output_queue))
        time.sleep(0.02)
    return results


def _stop(worker):
    try:
        worker.shutdown()
        worker.join(timeout=10.0)
    finally:
        worker.force_stop()


@pytest.mark.parametrize("pipelined", [True, False])
def test_worker_uploads_verifies_and_records(tmp_path, pipelined):
    local_base, files = _make_local_files(tmp_path, 6)
    remote_root = str(tmp_path / "remote")
    os.makedirs(remote_root, exist_ok=True)
    target = UploadTarget(
        enabled=True, remote_root=remote_root, local_base=local_base, delete_after_verify=True
    )
    manifest = str(tmp_path / "manifest.jsonl")
    worker = UploadWorker(
        target=target, manifest_path=manifest, pipelined=pipelined, threads=3
    )
    worker.start()
    try:
        task = _task("t1", files, local_base, remote_root)
        worker.submit(task)
        results = _drain(worker, 1)
        assert len(results) == 1
        r = results[0]
        assert r.success
        assert set(r.uploaded_paths) == set(files)
        assert set(r.deletable_uploaded_paths) == set(files)
        # Remote bytes match source bytes for every file.
        for local, remote in task.files:
            assert os.path.isfile(remote), f"missing remote {remote}"
            with open(local, "rb") as a, open(remote, "rb") as b:
                assert a.read() == b.read()
            assert not os.path.exists(remote + ".part")
        # Manifest has one valid record per file.
        recs = read_manifest(manifest)
        assert len(recs) == len(files)
        assert all(rec["sha256"] for rec in recs)
        assert {rec["local_path"] for rec in recs} == set(files)
        # Worker stamped progress.
        assert worker.heartbeat > 0
    finally:
        _stop(worker)


def test_results_aggregate_per_task(tmp_path):
    local_base, files = _make_local_files(tmp_path, 6)
    remote_root = str(tmp_path / "remote")
    os.makedirs(remote_root, exist_ok=True)
    target = UploadTarget(
        enabled=True, remote_root=remote_root, local_base=local_base, delete_after_verify=False
    )
    worker = UploadWorker(
        target=target, manifest_path=str(tmp_path / "m.jsonl"), pipelined=True, threads=4
    )
    worker.start()
    try:
        # Three tasks of two files each -> three results, each covering its pair.
        tasks = [
            _task(f"t{i}", files[2 * i : 2 * i + 2], local_base, remote_root, deletable=False)
            for i in range(3)
        ]
        for t in tasks:
            worker.submit(t)
        results = _drain(worker, 3)
        assert len(results) == 3
        by_id = {r.task_id: r for r in results}
        assert set(by_id) == {"t0", "t1", "t2"}
        for t in tasks:
            r = by_id[t.task_id]
            assert r.success
            assert set(r.uploaded_paths) == {lp for lp, _ in t.files}
            # delete_after_verify off -> nothing is deletable
            assert r.deletable_uploaded_paths == []
    finally:
        _stop(worker)


def test_empty_task_emits_success(tmp_path):
    remote_root = str(tmp_path / "remote")
    os.makedirs(remote_root, exist_ok=True)
    target = UploadTarget(
        enabled=True, remote_root=remote_root, local_base=str(tmp_path), delete_after_verify=False
    )
    worker = UploadWorker(target=target, manifest_path=str(tmp_path / "m.jsonl"), pipelined=True)
    worker.start()
    try:
        worker.submit(
            UploadTask(task_id="empty", time_point=0, region_id="A1", fov=0, files=[])
        )
        results = _drain(worker, 1)
        assert len(results) == 1 and results[0].success
        assert results[0].uploaded_paths == []
    finally:
        _stop(worker)


def test_missing_source_reports_failure(tmp_path):
    local_base, files = _make_local_files(tmp_path, 1)
    remote_root = str(tmp_path / "remote")
    os.makedirs(remote_root, exist_ok=True)
    bogus = str(tmp_path / "exp" / "does_not_exist.bin")
    target = UploadTarget(
        enabled=True, remote_root=remote_root, local_base=local_base, delete_after_verify=True
    )
    worker = UploadWorker(
        target=target, manifest_path=str(tmp_path / "m.jsonl"), pipelined=True, max_attempts=1
    )
    worker.start()
    try:
        task = UploadTask(
            task_id="bad",
            time_point=0,
            region_id="A1",
            fov=0,
            files=[
                (files[0], local_to_remote_path(files[0], local_base, remote_root)),
                (bogus, local_to_remote_path(bogus, local_base, remote_root)),
            ],
            deletable_local_paths={files[0], bogus},
        )
        worker.submit(task)
        results = _drain(worker, 1)
        assert len(results) == 1
        r = results[0]
        assert not r.success
        assert files[0] in r.uploaded_paths
        # The verified file is deletable; the missing one is not, and is not
        # deleted locally (deferred for backfill).
        assert r.deletable_uploaded_paths == [files[0]]
        assert any(bogus == fp for fp, _ in r.failed_paths)
    finally:
        _stop(worker)


def test_heartbeat_called_per_chunk(tmp_path):
    local_base, files = _make_local_files(tmp_path, 1, size=5 * COPY_CHUNK_BYTES + 7)
    remote = str(tmp_path / "remote.bin")
    calls = []
    ok, sha, n_bytes, err = upload_one_file(
        files[0], remote, log=_log, heartbeat=lambda nbytes: calls.append(nbytes)
    )
    assert ok, err
    # ~6 chunks for the copy + ~6 for the verify read-back.
    assert len(calls) >= 6
    assert sum(calls) >= n_bytes


def test_verify_readback_false_uses_size_check(tmp_path):
    local_base, files = _make_local_files(tmp_path, 1, size=8192)
    remote = str(tmp_path / "remote.bin")
    ok, sha, n_bytes, err = upload_one_file(
        files[0], remote, log=_log, verify_readback=False
    )
    assert ok, err
    assert os.path.getsize(remote) == n_bytes
    with open(files[0], "rb") as a, open(remote, "rb") as b:
        assert a.read() == b.read()


def test_remote_root_reachable(tmp_path):
    assert remote_root_reachable(str(tmp_path), timeout_s=2.0)
    assert not remote_root_reachable(str(tmp_path / "nope"), timeout_s=2.0)
    assert not remote_root_reachable("", timeout_s=2.0)


def test_tasks_submitted_counter_tracks_both_producer_paths(tmp_path):
    """submit() and the raw shared-Value path (used by JobRunner barriers)
    must both count; outstanding accounting is (submitted - results)."""
    local_base, files = _make_local_files(tmp_path, 4)
    remote_root = str(tmp_path / "remote")
    os.makedirs(remote_root, exist_ok=True)
    target = UploadTarget(
        enabled=True, remote_root=remote_root, local_base=local_base, delete_after_verify=False
    )
    worker = UploadWorker(target=target, manifest_path=str(tmp_path / "m.jsonl"))
    worker.start()
    try:
        assert worker.tasks_submitted == 0
        # Producer path 1: local submit().
        worker.submit(_task("t0", files[:2], local_base, remote_root, deletable=False))
        assert worker.tasks_submitted == 1
        # Producer path 2: external producer holding only the shared Value +
        # queue (what FlushAndStageUploadJob does in the JobRunner subprocess).
        UploadWorker.count_submitted(worker.tasks_submitted_value)
        worker.input_queue.put(_task("t1", files[2:], local_base, remote_root, deletable=False))
        assert worker.tasks_submitted == 2
        results = _drain(worker, 2)
        assert len(results) == 2
        assert worker.tasks_submitted - len(results) == 0
    finally:
        _stop(worker)


def test_heartbeat_not_stamped_by_failing_uploads(tmp_path):
    """A failure grind must look idle to the watchdog: only byte movement and
    verified completions stamp the heartbeat."""
    remote_root = str(tmp_path / "remote")
    os.makedirs(remote_root, exist_ok=True)
    target = UploadTarget(
        enabled=True, remote_root=remote_root, local_base=str(tmp_path), delete_after_verify=False
    )
    worker = UploadWorker(
        target=target, manifest_path=str(tmp_path / "m.jsonl"), max_attempts=1
    )
    worker.start()
    try:
        deadline = time.time() + 10.0
        while worker.heartbeat == 0.0 and time.time() < deadline:
            time.sleep(0.02)
        hb0 = worker.heartbeat
        assert hb0 > 0.0  # startup stamp
        bogus = str(tmp_path / "missing.bin")
        task = UploadTask(
            task_id="fail",
            time_point=0,
            region_id="A1",
            fov=0,
            files=[(bogus, os.path.join(remote_root, "missing.bin"))],
        )
        worker.submit(task)
        results = _drain(worker, 1)
        assert len(results) == 1 and not results[0].success
        assert worker.heartbeat == hb0, "failed upload must not refresh the heartbeat"
    finally:
        _stop(worker)


def test_same_destination_from_two_tasks_is_safe(tmp_path):
    """Shared metadata recurs in every barrier task; two lanes writing the
    same remote path must not corrupt it (unique .part names + per-path
    serialization)."""
    local_base, files = _make_local_files(tmp_path, 1, size=2 * COPY_CHUNK_BYTES)
    remote_root = str(tmp_path / "remote")
    os.makedirs(remote_root, exist_ok=True)
    target = UploadTarget(
        enabled=True, remote_root=remote_root, local_base=local_base, delete_after_verify=False
    )
    worker = UploadWorker(
        target=target, manifest_path=str(tmp_path / "m.jsonl"), pipelined=True, threads=4
    )
    worker.start()
    try:
        for i in range(4):
            worker.submit(_task(f"t{i}", files, local_base, remote_root, deletable=False))
        results = _drain(worker, 4)
        assert len(results) == 4
        assert all(r.success for r in results)
        remote = local_to_remote_path(files[0], local_base, remote_root)
        with open(files[0], "rb") as a, open(remote, "rb") as b:
            assert a.read() == b.read()
        # No temp files left behind anywhere on the remote.
        leftovers = [
            n for n in os.listdir(os.path.dirname(remote)) if ".part" in n
        ]
        assert leftovers == []
    finally:
        _stop(worker)


def test_collect_sidecar_files_prunes_zarr_and_pipeline_outputs(tmp_path):
    """Sidecar sweep must take everything EXCEPT zarr plate subtrees (covered
    by live streaming + metadata resync) and the pipeline's own root files."""
    exp = tmp_path / "exp"
    # HCS plate + non-HCS tree: both pruned wholesale.
    (exp / "plate.ome.zarr" / "A" / "1").mkdir(parents=True)
    (exp / "plate.ome.zarr" / "zarr.json").write_text("{}")
    (exp / "zarr" / "R0" / "fov_0.ome.zarr" / "0").mkdir(parents=True)
    (exp / "zarr" / "R0" / "fov_0.ome.zarr" / "zarr.json").write_text("{}")
    # Sidecars: root files + a per-timepoint folder + a stray non-plate file
    # inside the zarr/ container dir.
    (exp / "acquisition.yaml").write_text("a: 1")
    (exp / "000").mkdir()
    (exp / "000" / "well_A1.png").write_bytes(b"x")
    (exp / "zarr" / "R0" / "notes.txt").write_text("hi")
    # Pipeline outputs at the root: skipped there, kept elsewhere.
    (exp / "upload_manifest.jsonl").write_text("")
    (exp / "000" / "upload_manifest.jsonl").write_text("not-root-so-kept")

    got = collect_sidecar_files(str(exp), frozenset({"upload_manifest.jsonl"}))
    rel = {os.path.relpath(p, str(exp)).replace(os.sep, "/") for p in got}
    assert rel == {
        "acquisition.yaml",
        "000/well_A1.png",
        "000/upload_manifest.jsonl",
        "zarr/R0/notes.txt",
    }


def test_force_stop_terminates_worker(tmp_path):
    import psutil

    target = UploadTarget(
        enabled=True,
        remote_root=str(tmp_path / "r"),
        local_base=str(tmp_path),
        delete_after_verify=False,
    )
    worker = UploadWorker(target=target, manifest_path=str(tmp_path / "m.jsonl"))
    worker.start()
    pid = worker.pid
    assert worker.is_alive()
    worker.force_stop()
    # force_stop() terminate()s then close()s the handle, so query the OS by
    # pid rather than the (now-closed) Process object.
    deadline = time.time() + 5.0
    while time.time() < deadline and psutil.pid_exists(pid):
        time.sleep(0.05)
    assert not psutil.pid_exists(pid)
    # Safe / idempotent to call again (close() on an already-closed handle).
    worker.force_stop()
