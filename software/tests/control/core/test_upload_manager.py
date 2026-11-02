import os
import time
import hashlib
import json
import sqlite3
import threading

import pytest
import squid.logging

from control.core.upload_manager import (
    DatasetOwnershipError,
    SubmittedFile,
    TransferResult,
    UploadStore,
    UploadManagerEngine,
    handle_request,
)


def make_store(tmp_path):
    return UploadStore(tmp_path / "state" / "uploads.sqlite3")


def register(store, tmp_path, dataset_id="dataset"):
    local = tmp_path / "local"; remote = tmp_path / "remote"
    local.mkdir(exist_ok=True); remote.mkdir(exist_ok=True)
    store.register_dataset(
        dataset_id=dataset_id, name="run", local_root=str(local), remote_root=str(remote),
        delete_after_verify=True,
    )
    return local, remote


def test_duplicate_submission_is_idempotent_and_progress_is_unique(tmp_path):
    store = make_store(tmp_path); local, _ = register(store, tmp_path)
    (local / "a.bin").write_bytes(b"abc")
    file = SubmittedFile("a.bin", 3, deletable=True)
    first = store.submit_batch("dataset", "stable-id", [file])
    second = store.submit_batch("dataset", "stable-id", [file])
    assert first["file_count"] == 1
    assert second == {"accepted": True, "duplicate": True, "task_id": "stable-id", "file_count": 1}
    claimed = store.claim_next()
    store.record_transfer(TransferResult(claimed["id"], True, "a" * 64, 3, None, 3, 0.1))
    status = store.dataset_status("dataset")
    assert status["total_bytes"] == status["verified_bytes"] == 3
    assert status["uploaded_percent"] == 100.0


def test_destination_cannot_be_retargeted_or_concurrently_owned(tmp_path):
    store = make_store(tmp_path); local, remote = register(store, tmp_path)
    with pytest.raises(DatasetOwnershipError):
        store.register_dataset(
            dataset_id="dataset", name="run", local_root=str(local),
            remote_root=str(tmp_path / "elsewhere"),
        )
    with pytest.raises(DatasetOwnershipError):
        store.register_dataset(
            dataset_id="other", name="run", local_root=str(local), remote_root=str(remote),
        )


def test_restart_requeues_transfer_and_reconciles_delete_intent(tmp_path):
    store = make_store(tmp_path); local, _ = register(store, tmp_path)
    path = local / "a.bin"; path.write_bytes(b"abc")
    store.submit_batch("dataset", "one", [SubmittedFile("a.bin", 3, deletable=True)])
    claimed = store.claim_next(); store.close()
    store = make_store(tmp_path)
    assert store.file_row(claimed["id"])["state"] == "pending"
    claimed = store.claim_next()
    store.record_transfer(TransferResult(claimed["id"], True, "a" * 64, 3, None, 3, 0.1))
    assert store.begin_delete(claimed["id"])
    os.remove(path); store.close()
    store = make_store(tmp_path)
    assert store.file_row(claimed["id"])["state"] == "deleted"


def test_whole_task_deletion_waits_for_every_file(tmp_path):
    store = make_store(tmp_path)
    local = tmp_path / "local"; remote = tmp_path / "remote"
    local.mkdir(); remote.mkdir()
    store.register_dataset(
        dataset_id="dataset", name="run", local_root=str(local), remote_root=str(remote),
        deletion_mode="whole_task",
    )
    for name in ("a", "b"): (local / name).write_bytes(b"x")
    store.submit_batch("dataset", "one", [
        SubmittedFile("a", 1, deletable=True), SubmittedFile("b", 1, deletable=True),
    ])
    first = store.claim_next()
    store.record_transfer(TransferResult(first["id"], True, "a" * 64, 1, None, 1, 0.1))
    assert store.eligible_deletions() == []
    second = store.claim_next()
    store.record_transfer(TransferResult(second["id"], True, "b" * 64, 1, None, 1, 0.1))
    assert {row["relative_path"] for row in store.eligible_deletions()} == {"a", "b"}


def test_protocol_rejects_incompatible_client(tmp_path):
    store = make_store(tmp_path)
    response = handle_request(store, {"protocol_version": 99, "request_id": "x", "method": "get_status"})
    assert response["ok"] is False
    assert "incompatible protocol" in response["error"]


def test_engine_transfers_verifies_and_deletes_with_global_lane(tmp_path):
    store = make_store(tmp_path); local, remote = register(store, tmp_path)
    source = local / "a.bin"; source.write_bytes(b"manager-owned transfer")
    store.submit_batch(
        "dataset", "one", [SubmittedFile("a.bin", source.stat().st_size, deletable=True)]
    )
    store.seal_dataset("dataset")
    log_path = tmp_path / "upload_manager.log"
    handler = squid.logging.add_file_handler(str(log_path), replace_existing=True)
    engine = UploadManagerEngine(store, max_lanes=1)
    deadline = time.monotonic() + 15
    while store.dataset_status("dataset")["state"] != "complete" and time.monotonic() < deadline:
        engine.tick()
        time.sleep(0.05)
    engine.stop()
    if handler is not None:
        squid.logging.remove_handler(handler)
    assert (remote / "a.bin").read_bytes() == b"manager-owned transfer"
    assert not source.exists()
    assert store.dataset_status("dataset")["deleted_bytes"] == len(b"manager-owned transfer")
    log_text = log_path.read_text(encoding="utf-8")
    assert "Verified file_id=" in log_text
    assert "Deleted verified local file" in log_text


def test_legacy_manifest_is_revalidated_without_counting_network_write(tmp_path):
    local = tmp_path / "local"; remote = tmp_path / "remote"
    local.mkdir(); remote.mkdir()
    content = b"already uploaded"
    (local / "a.bin").write_bytes(content); target = remote / "a.bin"; target.write_bytes(content)
    checksum = hashlib.sha256(content).hexdigest()
    (local / "upload_manifest.jsonl").write_text(
        json.dumps({
            "local_path": str(local / "a.bin"), "remote_path": str(target),
            "remote_root": str(remote), "sha256": checksum, "bytes": len(content),
            "verified_utc": "2026-01-01T00:00:00+00:00", "deletable": True,
        }) + "\n",
        encoding="utf-8",
    )
    before = target.stat().st_mtime_ns
    store = make_store(tmp_path)
    store.register_dataset(
        dataset_id="dataset", name="run", local_root=str(local), remote_root=str(remote),
        delete_after_verify=False,
    )
    store.submit_batch("dataset", "one", [SubmittedFile("a.bin", len(content))])
    store.seal_dataset("dataset")
    engine = UploadManagerEngine(store, max_lanes=1)
    deadline = time.monotonic() + 15
    while store.dataset_status("dataset")["state"] != "complete" and time.monotonic() < deadline:
        engine.tick(); time.sleep(0.05)
    engine.stop()
    row = store.file_row(1)
    assert row["state"] == "verified"
    assert row["transferred_bytes"] == 0
    assert target.stat().st_mtime_ns == before


def test_seal_recovers_unsubmitted_zarr_shards_and_keeps_metadata(tmp_path):
    store = make_store(tmp_path); local, remote = register(store, tmp_path)
    array = local / "plate.ome.zarr" / "A" / "1" / "0" / "0"
    array.mkdir(parents=True)
    shard = array / "c.0.0.0.0.0"; shard.write_bytes(b"image shard")
    metadata = array / "zarr.json"; metadata.write_text("{}", encoding="utf-8")
    status = store.seal_dataset("dataset")
    assert status["file_count"] == 1
    queued = store.claim_next()
    assert queued["deletable"] == 1
    # Put the claimed row back through restart recovery, then exercise the engine.
    store.recover_interrupted_work()
    engine = UploadManagerEngine(store, max_lanes=1)
    deadline = time.monotonic() + 15
    while store.dataset_status("dataset")["state"] != "complete" and time.monotonic() < deadline:
        engine.tick(); time.sleep(0.05)
    engine.stop()
    assert not shard.exists()
    assert metadata.exists()
    assert (remote / shard.relative_to(local)).read_bytes() == b"image shard"


def test_restart_repairs_completed_database_from_first_manager_release(tmp_path):
    store = make_store(tmp_path); local, _remote = register(store, tmp_path)
    array = local / "plate.ome.zarr" / "A" / "1" / "0" / "0"
    array.mkdir(parents=True)
    (array / "c.0.0.0.0.0").write_bytes(b"missed shard")
    # Model the first-release database: sealed/complete with no shard rows.
    with store._db:
        store._db.execute(
            "UPDATE datasets SET sealed=1,state='complete',seal_status='complete' WHERE id='dataset'"
        )
    store.close()

    repaired = make_store(tmp_path)
    assert repaired.recovered_shards == 1
    status = repaired.dataset_status("dataset")
    assert status["state"] == "draining"
    assert status["file_count"] == 1
    assert repaired.claim_next()["deletable"] == 1


@pytest.mark.parametrize("max_lanes", [1, 2])
def test_persistent_lane_reuses_process_across_batches(tmp_path, max_lanes):
    store = make_store(tmp_path)
    local, remote = register(store, tmp_path)
    engine = UploadManagerEngine(store, max_lanes=max_lanes)
    workers = None
    try:
        for batch in range(3):
            names = [f"batch-{batch}-{i}.bin" for i in range(4)]
            for name in names:
                (local / name).write_bytes(name.encode())
            store.submit_batch("dataset", str(batch), [SubmittedFile(n, len(n), deletable=True) for n in names])
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                engine.tick()
                assert len(engine._workers) == max_lanes
                current = set(engine._workers)
                if workers is None:
                    workers = current
                assert current == workers and all(p.is_alive() for p in current)
                assert len(engine._active) <= max_lanes
                if all(not (local / n).exists() for n in names):
                    break
                time.sleep(0.02)
            for name in names:
                assert (remote / name).read_bytes() == name.encode()
                assert not (local / name).exists()
            assert not engine._active
        store.seal_dataset("dataset")
        assert store.dataset_status("dataset")["state"] == "complete"
    finally:
        engine.stop()
        store.close()
    assert all(not p.is_alive() for p in workers)


def test_dead_persistent_lane_is_replaced_and_claim_is_recoverable(tmp_path):
    store = make_store(tmp_path)
    local, remote = register(store, tmp_path)
    engine = UploadManagerEngine(store, max_lanes=1)
    try:
        # Start a lane, then kill it before its first transfer can complete.
        (local / "first.bin").write_bytes(b"first")
        store.submit_batch("dataset", "first", [SubmittedFile("first.bin", 5, deletable=True)])
        engine.tick()
        victim = next(iter(engine._workers))
        victim.terminate()
        victim.join(5)
        engine.tick()
        assert victim not in engine._workers
        assert (local / "first.bin").exists()
        assert store.file_row(1)["state"] in ("pending", "failed")
        assert len(engine._workers) == 1
        # Permit the normal retry immediately, then verify the replacement lane.
        with store._db:
            store._db.execute("UPDATE files SET state='pending',retry_after_epoch=0 WHERE id=1")
        deadline = time.monotonic() + 15
        while (local / "first.bin").exists() and time.monotonic() < deadline:
            engine.tick()
            time.sleep(0.02)
        assert (remote / "first.bin").read_bytes() == b"first"
        assert not (local / "first.bin").exists()
    finally:
        engine.stop()
        store.close()


def test_stop_does_not_claim_more_files_and_requeues_interrupted_transfer(tmp_path):
    store = make_store(tmp_path)
    local, _ = register(store, tmp_path)
    for name in ("first.bin", "second.bin"):
        (local / name).write_bytes(b"content")
    store.submit_batch("dataset", "batch", [SubmittedFile(n, 7, deletable=True) for n in ("first.bin", "second.bin")])
    engine = UploadManagerEngine(store, max_lanes=1)
    try:
        engine.tick()
        workers = list(engine._workers)
        engine.stop(timeout=0)
        assert all(not p.is_alive() for p in workers)
        assert store.file_row(1)["state"] == "pending"
        assert store.file_row(2)["state"] == "pending"
        assert store.file_row(2)["attempts"] == 0
        assert (local / "first.bin").exists()
        assert (local / "second.bin").exists()
        engine.tick()
        assert not engine._workers
    finally:
        store.close()


def test_batched_status_matches_individual_status_and_uses_bounded_queries(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    try:
        for i in range(3):
            root = tmp_path / str(i)
            root.mkdir()
            register(store, root, dataset_id=str(i))
        store.submit_batch("0", "batch", [SubmittedFile("a", 100, deletable=True), SubmittedFile("meta", 20)])
        item = store.claim_next()
        store.record_transfer(TransferResult(item["id"], True, "a" * 64, 100, None, 100, 0.1))
        store.submit_batch("1", "batch", [SubmittedFile("b", 30, deletable=True)])
        store.seal_dataset("1")
        with store._db:
            store._db.execute("UPDATE datasets SET state='paused' WHERE id='1'")
        expected = {str(i): store.dataset_status(str(i)) for i in range(3)}
        queries = []
        connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            reader = connect(*args, **kwargs)
            reader.set_trace_callback(queries.append)
            return reader

        monkeypatch.setattr(sqlite3, "connect", traced_connect)
        snapshot = store.status()
        actual = {row["id"]: row for row in snapshot["datasets"]}
        # Free-space probes vary as other processes write; counters must match.
        for row in (*actual.values(), *expected.values()):
            row.pop("source_free_bytes")
        assert actual == expected
        assert snapshot["backlog_bytes"] == 50
        assert len([q for q in queries if q.startswith("SELECT")]) == 3
        rate_plan = store._db.execute(
            "EXPLAIN QUERY PLAN SELECT dataset_id,SUM(bytes) FROM events "
            "WHERE kind='verified' AND created_utc>=? GROUP BY dataset_id", ("2026-01-01",)
        ).fetchall()
        assert any("SEARCH" in row[3] and "events_recent_rate" in row[3] for row in rate_plan)
        fairness_plan = store._db.execute(
            "EXPLAIN QUERY PLAN SELECT MAX(id) FROM events WHERE dataset_id=? AND kind='transfer_started'", ("0",)
        ).fetchall()
        assert any("SEARCH" in row[3] and "events_dataset_kind_id" in row[3] for row in fairness_plan)
    finally:
        store.close()


def test_background_scheduler_progresses_while_status_snapshot_is_blocked(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    local, remote = register(store, tmp_path)
    names = [f"{i}.bin" for i in range(6)]
    for name in names:
        (local / name).write_bytes(b"content")
    store.submit_batch("dataset", "batch", [SubmittedFile(n, 7, deletable=True) for n in names])
    blocked = threading.Event()
    release = threading.Event()
    errors, snapshots = [], []
    connect = sqlite3.connect

    def slow_reader(*args, **kwargs):
        reader = connect(*args, **kwargs)
        if "mode=ro" in str(args[0]):
            def trace(query):
                if query.startswith("SELECT dataset_id,"):
                    # The first SELECT already established a WAL read snapshot.
                    blocked.set()
                    release.wait(20)
            reader.set_trace_callback(trace)
        return reader

    monkeypatch.setattr(sqlite3, "connect", slow_reader)

    def refresh():
        try:
            snapshots.append(store.status())
        except Exception as exc:
            errors.append(exc)

    refresh_thread = threading.Thread(target=refresh)
    engine = UploadManagerEngine(store, max_lanes=2)
    try:
        refresh_thread.start()
        assert blocked.wait(5)
        engine.start()
        deadline = time.monotonic() + 15
        # No manual tick calls or Qt event processing: background scheduling
        # must keep refilling both lanes despite the stalled status reader.
        while any((local / n).exists() for n in names) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not release.is_set()
        assert all(not (local / n).exists() for n in names)
        assert all((remote / n).read_bytes() == b"content" for n in names)
    finally:
        release.set()
        refresh_thread.join(5)
        engine.stop()
    try:
        assert not refresh_thread.is_alive()
        assert not engine._scheduler_thread.is_alive()
        assert not errors
        assert snapshots[0]["datasets"][0]["verified_bytes"] == 0  # consistent old snapshot
        assert store.status()["datasets"][0]["verified_bytes"] == 42
    finally:
        store.close()


def test_recent_view_caches_finished_rows_and_history_retains_old_acquisitions(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    try:
        for name in ("old-complete", "old-active", "recent-complete"):
            root = tmp_path / name
            root.mkdir()
            register(store, root, dataset_id=name)
        with store._db:
            store._db.execute("UPDATE datasets SET created_utc='2000-01-01',sealed_utc='2000-01-01' WHERE id LIKE 'old-%'")
            store._db.execute("UPDATE datasets SET state='complete',sealed=1 WHERE id IN ('old-complete','recent-complete')")
        store.sync_history()
        records = [json.loads(line) for line in store.history_path.read_text().splitlines()]
        assert {r["dataset"]["id"] for r in records} == {"old-complete", "old-active", "recent-complete"}
        snapshot = store.status()
        assert {r["id"] for r in snapshot["datasets"]} == {"old-active", "recent-complete"}
        assert snapshot["active_count"] == 1
        queries = []
        connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            reader = connect(*args, **kwargs)
            reader.set_trace_callback(queries.append)
            return reader

        monkeypatch.setattr(sqlite3, "connect", traced_connect)
        store.status()
        aggregates = [q for q in queries if "FROM files" in q]
        assert len(aggregates) == 1
        assert "old-active" in aggregates[0]
        assert "recent-complete" not in aggregates[0]
        assert "old-complete" not in aggregates[0]
        before = store.history_path.read_bytes()
        store.sync_history()
        assert store.history_path.read_bytes() == before
    finally:
        store.close()
    reopened = make_store(tmp_path)
    try:
        assert reopened.history_path.read_bytes() == before
    finally:
        reopened.close()


def test_cancel_inflight_upload_preserves_files_and_survives_restart(tmp_path):
    store = make_store(tmp_path)
    local, remote = register(store, tmp_path)
    for name in ("first.bin", "second.bin"):
        (local / name).write_bytes(b"keep locally")
    (remote / "existing.bin").write_bytes(b"keep remotely")
    store.submit_batch("dataset", "batch", [SubmittedFile(n, 12, deletable=True) for n in ("first.bin", "second.bin")])
    engine = UploadManagerEngine(store, max_lanes=1)
    try:
        engine.tick()
        worker = next(iter(engine._workers))
        result = engine.cancel_dataset_async("dataset")
        engine.tick()
        assert result.result(timeout=1)["cancelled"]
        assert not worker.is_alive()
        assert store.dataset_info("dataset")["state"] == "cancelled"
        assert store.status()["active_count"] == 0
        assert not store.eligible_deletions()
        assert store.claim_next() is None
        assert all((local / n).read_bytes() == b"keep locally" for n in ("first.bin", "second.bin"))
        assert (remote / "existing.bin").read_bytes() == b"keep remotely"
        # Late producer submissions are acknowledged as cancelled, avoiding an
        # acquisition error or an endlessly replayed producer outbox.
        assert store.submit_batch("dataset", "late", [SubmittedFile("later.bin", 50)])["cancelled"]
        store.seal_dataset("dataset")
        store.pause("dataset")
        store.resume("dataset")
        store.retry_failed("dataset")
        assert store.dataset_info("dataset")["state"] == "cancelled"
    finally:
        engine.stop()
        store.close()
    reopened = make_store(tmp_path)
    try:
        assert reopened.dataset_info("dataset")["state"] == "cancelled"
        assert reopened.claim_next() is None
        assert reopened.recovered_shards == 0
        history = [json.loads(l) for l in reopened.history_path.read_text().splitlines()]
        assert history[-1]["dataset"]["state"] == "cancelled"
        assert history[-1]["totals"]["deleted_bytes"] == 0
    finally:
        reopened.close()


def test_history_export_failure_does_not_advance_marker(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    register(store, tmp_path)
    try:
        import builtins
        original = builtins.open

        def failing_open(path, *args, **kwargs):
            if str(path) == str(store.history_path):
                raise OSError("history disk unavailable")
            return original(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(builtins, "open", failing_open)
            with pytest.raises(OSError, match="history disk unavailable"):
                store.sync_history()
        assert store._db.execute("SELECT COUNT(*) FROM history_snapshots").fetchone()[0] == 0
        store.sync_history()
        assert json.loads(store.history_path.read_text())["dataset"]["id"] == "dataset"
    finally:
        store.close()
