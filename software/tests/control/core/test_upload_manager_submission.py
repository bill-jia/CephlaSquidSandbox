from control.core.job_processing import FlushAndStageUploadJob, SaveZarrJob
from control.core.upload_manager_client import UploadManagerClient
from control.core.zarr_upload import UploadTarget


class _Writer:
    def __init__(self, shard, metadata):
        self.shard = str(shard)
        self.metadata = str(metadata)

    def wait_for_pending(self):
        pass

    def drain_unstaged_shard_paths(self):
        return [self.shard]

    def metadata_paths(self):
        return [self.metadata]


def test_manager_submission_does_not_require_legacy_worker_queue(tmp_path, monkeypatch):
    shard = tmp_path / "0" / "c.0.0.0.0.0"
    shard.parent.mkdir(); shard.write_bytes(b"pixels")
    metadata = tmp_path / "zarr.json"; metadata.write_text("{}", encoding="utf-8")
    output_path = str(tmp_path / "fov.ome.zarr")
    SaveZarrJob._zarr_writers[output_path] = _Writer(shard, metadata)
    captured = {}

    def submit(_self, dataset_id, submission_id, files, *, task_id=None):
        captured.update(dataset_id=dataset_id, files=files, task_id=task_id)
        return {"accepted": True, "file_count": len(files)}

    monkeypatch.setattr(UploadManagerClient, "submit_batch", submit)
    monkeypatch.setattr(FlushAndStageUploadJob, "_upload_input_queue", None)
    try:
        result = FlushAndStageUploadJob(
            time_point=0,
            region_id="A1",
            fov=0,
            output_path=output_path,
            upload_target=UploadTarget(
                enabled=True,
                local_base=str(tmp_path),
                remote_root=str(tmp_path / "remote"),
                delete_after_verify=True,
                manager_owned=True,
                manager_dataset_id="dataset",
            ),
        ).run()
    finally:
        SaveZarrJob._zarr_writers.pop(output_path, None)

    assert result.manager_owned is True
    assert captured["dataset_id"] == "dataset"
    by_path = {item["relative_path"]: item for item in captured["files"]}
    assert by_path["0/c.0.0.0.0.0"]["deletable"] is True
    assert by_path["zarr.json"]["deletable"] is False
