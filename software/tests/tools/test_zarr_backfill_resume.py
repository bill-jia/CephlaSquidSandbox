"""Persistent backfill resume and destination-bound local deletion."""

import hashlib
import json
import queue
from unittest.mock import patch

import pytest

import control.core.zarr_upload as upload
from scripts import zarr_backfill_upload as backfill


@pytest.fixture
def archive(tmp_path):
    local = tmp_path / "local" / "c.0.0.0.0.0"
    local.parent.mkdir()
    local.write_bytes(b"image data")
    target = upload.UploadTarget(
        enabled=True, local_base=str(local.parent), remote_root=str(tmp_path / "remote")
    )
    remote = tmp_path / "remote" / local.name
    remote.parent.mkdir()
    remote.write_bytes(local.read_bytes())
    manifest = tmp_path / "manifest.jsonl"
    record = {
        "local_path": str(local), "remote_path": str(remote),
        "remote_root": target.remote_root, "task_id": "previous-run",
        "deletable": True, "verified_utc": "2026-09-28T00:00:00+00:00",
        "bytes": local.stat().st_size,
        "sha256": hashlib.sha256(local.read_bytes()).hexdigest(),
    }
    return local, remote, manifest, target, record


def run_task(archive, monkeypatch, *, pipelined=True, task_id="current-run"):
    local, remote, manifest, target, _ = archive
    worker = upload.UploadWorker(target, str(manifest), resume_manifest=True, max_attempts=1)
    results = queue.Queue()
    monkeypatch.setattr(worker, "_output_queue", results)
    task = upload.UploadTask(
        task_id=task_id, time_point=0, region_id="A1", fov=0,
        files=[(str(local), str(remote))], deletable_local_paths={str(local)},
    )
    if pipelined:
        tasks = queue.Queue()
        tasks.put(task)
        tasks.put(upload._SHUTDOWN_SENTINEL)
        monkeypatch.setattr(worker, "_input_queue", tasks)
        worker._run_pipelined(backfill.log)
    else:
        worker._process_task(task, backfill.log)
    return results.get_nowait()


@pytest.mark.parametrize("pipelined", [True, False])
@pytest.mark.parametrize("legacy", [True, False])
def test_resume_preserves_history_skips_copy_and_allows_deletion(archive, monkeypatch, pipelined, legacy):
    local, remote, manifest, target, record = archive
    if legacy:
        record.pop("remote_root")
        record.pop("task_id")
    upload.append_manifest_record(str(manifest), record)
    history = manifest.read_bytes()
    with patch.object(upload, "upload_one_file", wraps=upload.upload_one_file) as copy:
        result = run_task(archive, monkeypatch, pipelined=pipelined)
        assert result.success
        copy.assert_not_called()
    assert manifest.read_bytes().startswith(history)
    records = upload.read_manifest(str(manifest))
    assert len(records) == 2
    assert records[-1]["remote_root"] == target.remote_root
    assert records[-1]["task_id"] == "current-run"
    guard = upload.VerifiedUploadManifest(str(manifest), target)
    assert backfill.delete_verified_locals(result, backfill.log, guard) == 1
    assert not local.exists()
    assert remote.read_bytes() == b"image data"


@pytest.mark.parametrize("change", ["root", "path", "local", "remote", "missing_remote", "not_deletable"])
def test_resume_reuploads_when_history_cannot_be_trusted(archive, monkeypatch, change):
    local, remote, manifest, _, record = archive
    if change == "root":
        record["remote_root"] += "-other"
    elif change == "path":
        record["remote_path"] += "-other"
    elif change == "local":
        local.write_bytes(b"newer data")  # Same size, different hash.
    elif change == "remote":
        remote.write_bytes(b"wrong data")
    elif change == "missing_remote":
        remote.unlink()
    else:
        record["deletable"] = False
    upload.append_manifest_record(str(manifest), record)
    with patch.object(upload, "upload_one_file", wraps=upload.upload_one_file) as copy:
        result = run_task(archive, monkeypatch)
        assert result.success
        assert copy.call_count == 1
    assert remote.read_bytes() == local.read_bytes()


@pytest.mark.parametrize("legacy", [False, True])
def test_historical_record_alone_can_authorize_cleanup(archive, legacy):
    local, _, manifest, target, record = archive
    if legacy:
        record.pop("task_id")
        record.pop("remote_root")
    upload.append_manifest_record(str(manifest), record)
    result = upload.UploadResult(
        task_id="resumed-run", time_point=0, region_id="A1", fov=0, success=True,
        uploaded_paths=[str(local)], deletable_uploaded_paths=[str(local)],
    )
    guard = upload.VerifiedUploadManifest(str(manifest), target)
    assert backfill.delete_verified_locals(result, backfill.log, guard) == 1


@pytest.mark.parametrize("problem", [
    "root", "path", "local", "remote", "missing_record", "not_deletable", "failed_task", "bad_hash",
])
def test_cleanup_rejects_untrusted_history(archive, problem):
    local, remote, manifest, target, record = archive
    if problem == "root":
        record["remote_root"] += "-other"
    elif problem == "path":
        record["remote_path"] += "-other"
    elif problem == "local":
        local.write_bytes(b"newer data")
    elif problem == "remote":
        remote.write_bytes(b"wrong data")
    elif problem == "not_deletable":
        record["deletable"] = False
    elif problem == "bad_hash":
        record["sha256"] = None
    if problem != "missing_record":
        upload.append_manifest_record(str(manifest), record)
    result = upload.UploadResult(
        task_id="resumed-run", time_point=0, region_id="A1", fov=0,
        success=problem != "failed_task",
        uploaded_paths=[str(local)], deletable_uploaded_paths=[str(local)],
    )
    guard = upload.VerifiedUploadManifest(str(manifest), target)
    assert backfill.delete_verified_locals(result, backfill.log, guard) == 0
    assert local.exists()


def test_missing_manifest_write_retains_uploaded_local_file(archive, monkeypatch):
    local, _, manifest, target, _ = archive
    monkeypatch.setattr(upload, "append_manifest_record", lambda *args: None)
    result = run_task(archive, monkeypatch)
    assert result.success
    assert backfill.delete_verified_locals(
        result, backfill.log, upload.VerifiedUploadManifest(str(manifest), target)
    ) == 0
    assert local.exists()


def test_append_recovers_cancelled_partial_line_and_index_refreshes(archive):
    _, _, manifest, target, record = archive
    manifest.write_bytes(b'{"interrupted":')
    guard = upload.VerifiedUploadManifest(str(manifest), target)
    guard.refresh()
    assert guard.offset == 0
    upload.append_manifest_record(str(manifest), record)
    guard.refresh()
    assert guard.get(record["remote_path"]) == record
    assert manifest.read_bytes().startswith(b'{"interrupted":\n')
    assert upload.read_manifest(str(manifest)) == [record]
    # Leave a valid record incomplete, then finish it during a later poll.
    changed = dict(record, remote_root="different-root")
    with manifest.open("ab") as stream:
        stream.write(json.dumps(changed).encode())
    guard.refresh()
    assert guard.get(record["remote_path"]) == record
    with manifest.open("ab") as stream:
        stream.write(b"\n")
    guard.refresh()
    assert guard.get(record["remote_path"]) is None
