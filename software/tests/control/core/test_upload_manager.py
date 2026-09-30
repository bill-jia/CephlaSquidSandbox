import os
import time
import hashlib
import json

import pytest

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
    engine = UploadManagerEngine(store, max_lanes=1)
    deadline = time.monotonic() + 15
    while store.dataset_status("dataset")["state"] != "complete" and time.monotonic() < deadline:
        engine.tick()
        time.sleep(0.05)
    engine.stop()
    assert (remote / "a.bin").read_bytes() == b"manager-owned transfer"
    assert not source.exists()
    assert store.dataset_status("dataset")["deleted_bytes"] == len(b"manager-owned transfer")


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
