"""Whole-stack writing, flat/legacy recovery, and verified metadata caching."""

import multiprocessing
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

ts = pytest.importorskip("tensorstore")

from control.core.job_processing import FlushAndStageUploadJob, SaveZarrJob
from control.core.zarr_layout import array_chunk_path, timepoint_shards
from control.core.zarr_writer import ZarrAcquisitionConfig, ZarrWriter
from control.core.zarr_upload import UploadTarget, UploadWorker, read_manifest
from scripts import zarr_backfill_upload as backfill
import control.core.zarr_upload as upload_module
import squid.logging


def make_writer(root, *, separator=".", per_z=False, z=3, c=2, y=256):
    writer = ZarrWriter(ZarrAcquisitionConfig(
        output_path=str(root / "fov.ome.zarr" / "0"),
        shape=(2, c, z, y, y), dtype=np.dtype("uint16"), pixel_size_um=0.5,
        chunk_separator=separator, shard_per_z=per_z,
    ))
    writer.initialize()
    return writer


def read_array(path):
    return ts.open({"driver": "zarr3", "kvstore": {"driver": "file", "path": str(path)}}).result().read().result()


@pytest.mark.parametrize("separator", [".", "/"])
@pytest.mark.parametrize("per_z", [False, True])
def test_roundtrip_and_backfill_discover_all_levels(tmp_path, separator, per_z):
    writer = make_writer(tmp_path, separator=separator, per_z=per_z)
    group = Path(writer.config.output_path).parent
    expected = np.zeros(writer.config.shape, dtype=np.uint16)
    try:
        for t in range(2):
            for z in range(3):
                for c in range(2):
                    expected[t, c, z] = 1 + t * 100 + z * 10 + c
                    writer.write_frame(expected[t, c, z], t=t, c=c, z=z)
                    writer.record_frame_time(t, c, z, 1000 + t * 10 + z + c)
        writer.wait_for_pending()
        staged = set(map(Path, writer.drain_unstaged_shard_paths()))
        assert len(staged) == 2 * (3 if per_z else 1) * len(writer._level_shapes)
        assert writer.drain_unstaged_shard_paths() == []
        assert backfill.check_fov_layout(group)[0]
        recovered = set()
        for level, (y, x) in enumerate(writer._level_shapes):
            directory = group / str(level)
            assert backfill.enumerate_timepoints_for_level(directory) == [0, 1]
            for t in range(2):
                recovered.update(backfill.shard_files_for_timepoint(directory, t))
            actual = read_array(directory)
            np.testing.assert_array_equal(actual, expected[..., :y, :x])
        assert recovered == staged
        if separator == ".":
            assert not (group / "0" / "c").exists()
        writer.finalize()
        timestamps = array_chunk_path(group / "frame_times", (0, 0, 0))
        assert timestamps in backfill.gather_metadata_files(group)
        assert read_array(group / "frame_times")[1, 1, 2] == 1013
        # Real recovery discovers data only, never temporary lock/part files.
        for suffix in (".__lock", ".part-123"):
            Path(str(next(iter(staged))) + suffix).write_bytes(b"temporary")
        assert {p for level in backfill.enumerate_levels(group) for _, p in timepoint_shards(level)} == staged
    finally:
        if not writer.is_finalized:
            writer.finalize()


def test_complete_stack_uses_one_write_per_level(tmp_path):
    writer = make_writer(tmp_path)
    calls = []

    class DatasetSpy:
        def __init__(self, dataset):
            self.dataset = dataset

        def __getitem__(self, index):
            target = self.dataset[index]

            class Selection:
                def write(self, data):
                    calls.append(data.shape)
                    return target.write(data)

            return Selection()

    writer._level_datasets = [DatasetSpy(d) for d in writer._level_datasets]
    try:
        for z in range(3):
            for c in range(2):
                writer.write_frame(np.full((256, 256), 1 + z + c, np.uint16), t=0, c=c, z=z)
                if (z, c) != (2, 1):
                    writer.wait_for_pending()  # Early barriers cannot publish partial stacks.
                    assert calls == []
                    assert writer.drain_unstaged_shard_paths() == []
        assert calls == [(2, 3, y, x) for y, x in writer._level_shapes]
        assert len(writer.drain_unstaged_shard_paths()) == len(calls)
    finally:
        writer.finalize()


def test_spill_bounds_buffers_across_fovs_without_publishing_partial_data(tmp_path, monkeypatch):
    monkeypatch.setattr(ZarrWriter, "MAX_BUFFERED_STACK_BYTES", 3 * 32 * 32 * 2)
    writers = [make_writer(tmp_path / str(i), y=32, z=5, c=1) for i in range(2)]
    try:
        for z in range(4):
            for writer in writers:
                writer.write_frame(np.full((32, 32), z + 1, np.uint16), t=0, c=0, z=z)
                assert sum(w._buffered_stack_bytes for w in writers) <= ZarrWriter.MAX_BUFFERED_STACK_BYTES
                writer.wait_for_pending()
                assert writer.drain_unstaged_shard_paths() == []
        # Complete one stack and finalize the other with a missing last plane.
        writers[0].write_frame(np.full((32, 32), 5, np.uint16), t=0, c=0, z=4)
        assert len(writers[0].drain_unstaged_shard_paths()) == 1
        for i, writer in enumerate(writers):
            writer.finalize()
            actual = read_array(writer.config.output_path)
            for z in range(4):
                assert np.all(actual[0, 0, z] == z + 1)
            assert np.all(actual[0, 0, 4] == (5 if i == 0 else 0))
        assert len(writers[1].drain_unstaged_shard_paths()) == 1
    finally:
        for writer in writers:
            if not writer.is_finalized:
                writer.finalize()


def test_finalize_stages_partial_stack_and_verified_result_can_be_deleted(tmp_path, monkeypatch):
    writer = make_writer(tmp_path / "local", c=1, y=32)
    writer.write_frame(np.full((32, 32), 7, np.uint16), t=0, c=0, z=1)
    writer.wait_for_pending()
    assert writer.drain_unstaged_shard_paths() == []
    staged = queue.Queue()
    monkeypatch.setattr(SaveZarrJob, "_zarr_writers", {writer.config.output_path: writer})
    monkeypatch.setattr(FlushAndStageUploadJob, "_upload_input_queue", staged)
    submitted = multiprocessing.Value("i", 0)
    monkeypatch.setattr(FlushAndStageUploadJob, "_upload_tasks_submitted", submitted)
    target = UploadTarget(enabled=True, local_base=str(tmp_path / "local"), remote_root=str(tmp_path / "remote"))
    assert SaveZarrJob.finalize_all_writers(upload_target=target)
    assert submitted.value == 1
    task = staged.get_nowait()
    assert len(task.deletable_local_paths) == 1
    worker = UploadWorker(target, str(tmp_path / "manifest.jsonl"))
    results = queue.Queue()
    monkeypatch.setattr(worker, "_output_queue", results)
    worker._process_task(task, squid.logging.get_logger("test"))
    result = results.get_nowait()
    assert result.success
    for local in result.deletable_uploaded_paths:
        Path(local).unlink()
    remote = Path(target.remote_root) / "fov.ome.zarr" / "0"
    assert np.all(read_array(remote)[0, 0, 1] == 7)


def test_metadata_dedup_is_verified_and_detects_content_changes(tmp_path):
    local = tmp_path / "zarr.json"
    remote = tmp_path / "remote" / "zarr.json"
    local.write_text('{"version": 1}')
    worker = UploadWorker(UploadTarget(), str(tmp_path / "manifest.jsonl"), max_attempts=1)
    kwargs = dict(stable_read=True, deletable=False, heartbeat=lambda _: None, log=squid.logging.get_logger("test"))
    real_upload = upload_module.upload_one_file
    with patch.object(upload_module, "upload_one_file", wraps=real_upload) as copy:
        assert worker._upload_file(str(local), str(remote), **kwargs)[0]
        assert worker._upload_file(str(local), str(remote), **kwargs)[4]  # cached
        assert copy.call_count == 1
        local.write_text('{"version": 2}')  # Same length: content, not size, determines change.
        assert worker._upload_file(str(local), str(remote), **kwargs)[0]
        assert copy.call_count == 2
        assert remote.read_text() == local.read_text()
    local.write_text('{"version": 3}')
    with patch.object(upload_module, "upload_one_file", return_value=(False, None, None, "offline")) as copy:
        assert not worker._upload_file(str(local), str(remote), **kwargs)[0]
        assert not worker._upload_file(str(local), str(remote), **kwargs)[0]
        assert copy.call_count == 2  # A failed attempt never seeds the cache.
    assert worker._upload_file(str(local), str(remote), **kwargs)[0]
    assert remote.read_text() == local.read_text()


def test_concurrent_metadata_tasks_upload_and_record_once(tmp_path):
    local = tmp_path / "zarr.json"
    local.write_text('{}')
    remote = tmp_path / "remote" / "zarr.json"
    manifest = tmp_path / "manifest.jsonl"
    worker = UploadWorker(UploadTarget(), str(manifest))
    path_lock, manifest_lock = threading.Lock(), threading.Lock()
    def upload(_):
        return worker._do_upload(str(local), str(remote), True, (0, "A1", 0, "metadata-task"), False,
                                 manifest_lock, lambda _: None, squid.logging.get_logger("test"), path_lock)
    with patch.object(upload_module, "upload_one_file", wraps=upload_module.upload_one_file) as copy:
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert all(ok for ok, _ in pool.map(upload, range(12)))
        assert copy.call_count == 1
    assert len(read_manifest(str(manifest))) == 1


def test_stack_write_failure_does_not_stage_or_discard_buffer(tmp_path):
    writer = make_writer(tmp_path, c=1, z=2, y=32)
    class BrokenDataset:
        def __getitem__(self, _):
            return self

        def write(self, _):
            raise OSError("disk full")

    writer._level_datasets[0] = BrokenDataset()
    try:
        writer.write_frame(np.ones((32, 32), np.uint16), t=0, c=0, z=0)
        with pytest.raises(OSError, match="disk full"):
            writer.write_frame(np.ones((32, 32), np.uint16), t=0, c=0, z=1)
        assert writer.drain_unstaged_shard_paths() == []
        assert writer._buffered_stack_bytes == 2 * 32 * 32 * 2
    finally:
        writer.abort()
