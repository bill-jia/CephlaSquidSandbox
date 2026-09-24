import csv
import threading
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
import tifffile

from control.core.af_validation_collector import AFValidationCollector
from tests.control.core.test_af_validation import controller_fixture, metadata, operation, make_worker


def read_rows(path):
    with path.open(newline="", encoding="utf-8") as src:
        return list(csv.DictReader(src))


def paired_operation(value=5, shape=(16, 24)):
    op = operation()
    op.update(
        before_native=np.full(shape, value, dtype=np.uint8),
        after_native=np.full(shape, value + 1, dtype=np.uint8),
        before_full_sensor=np.full((32, 48), value, dtype=np.uint8),
        after_full_sensor=np.full((32, 48), value + 1, dtype=np.uint8),
        before_metadata={"z_mm": 1.0}, after_metadata={"z_mm": 1.01},
    )
    return op


def test_before_and_after_are_fresh_frames_at_the_actual_positions():
    controller, stage, camera = controller_fixture()
    stage.z_um = 20
    with controller.collect_validation_event() as events:
        assert controller.move_to_target(0)
    event = events[0]
    assert event["before_metadata"]["z_mm"] == pytest.approx(0.02)
    assert event["after_metadata"]["z_mm"] == pytest.approx(0, abs=0.001)
    assert np.argmax(event["before_native"].sum(axis=0)) > np.argmax(event["after_native"].sum(axis=0))
    assert event["before_full_sensor"] is not None and event["after_full_sensor"] is not None
    assert camera.roi == (100, 200, 1536, 256)
    assert camera.streaming and camera.get_callbacks_enabled()


def test_after_snapshot_follows_rollback_not_rejected_verification_position():
    controller, stage, _ = controller_fixture()
    stage.z_um = 20
    controller._verify_spot_alignment_with_laser_on = lambda: (False, 0.1)
    with controller.collect_validation_event() as events:
        assert not controller.move_to_target(0)
    event = events[0]
    assert event["after_metadata"]["z_mm"] == pytest.approx(0.02)
    np.testing.assert_array_equal(event["before_native"], event["after_native"])


def test_imagej_stack_channel_order_and_event_index_in_af_only_mode(tmp_path):
    collector = AFValidationCollector(tmp_path)
    first = collector.record(metadata(phase="seed"), paired_operation(5))
    second = collector.record({**metadata(), "fov": 1}, paired_operation(10))
    missing = paired_operation(15)
    missing["after_native"] = None
    collector.record({**metadata(), "fov": 2}, missing)
    collector.finalize()
    with tifffile.TiffFile(tmp_path / "before_after_native.tif") as stack:
        assert stack.is_imagej
        assert stack.imagej_metadata["channels"] == 2
        assert stack.imagej_metadata["frames"] == 2
        assert stack.series[0].axes == "TCYX"
        data = stack.asarray()
        assert data.shape == (2, 2, 16, 24)
        assert data[:, :, 0, 0].tolist() == [[5, 6], [10, 11]]
    index = read_rows(tmp_path / "before_after_index.csv")
    assert [row["event_id"] for row in index[:2]] == [str(first["event_id"]), str(second["event_id"])]
    assert index[2]["status"] == "missing_before_or_after"
    folder = tmp_path / first["artifacts_path"]
    assert (folder / "before_full_sensor.tiff").exists()
    assert (folder / "after_full_sensor.tiff").exists()


def test_stack_splits_shape_changes_and_size_without_resampling(tmp_path, monkeypatch):
    monkeypatch.setattr("control.core.af_validation_reports.STACK_PART_BYTES", 2 * 16 * 24)
    collector = AFValidationCollector(tmp_path)
    for shape in [(16, 24), (16, 24), (8, 12)]:
        collector.record(metadata(), paired_operation(shape=shape))
    collector.finalize()
    index = read_rows(tmp_path / "before_after_index.csv")
    assert len({row["stack_file"] for row in index}) == 3
    assert all(row["frame_1based"] == "1" for row in index)
    assert all(row["status"] == "included" for row in index)


def image_info(channel="BF", z_index=0):
    return SimpleNamespace(
        time_point=0, region_id="R0", fov=0, z_index=z_index, position=SimpleNamespace(z_mm=1.0),
        z_piezo_um=None, filename_channel_label=channel, observation_state=SimpleNamespace(name=channel),
        capture_time=1.0, file_id="R0_000_000", save_directory="experiment/0", array_key=None,
        save_t_index=0, save_c_index=0, cycle_event_index=0, state_frame_index=0,
        frame_suffix=None, postprocess_group=None,
    )


def test_data_tenengrad_links_correct_event_and_preserves_channel_z(tmp_path):
    collector = AFValidationCollector(tmp_path, save_all=True)
    seed = collector.record(metadata(phase="seed"), paired_operation())
    collector.link_visit([seed])
    correction = collector.record(metadata(), paired_operation())
    measure_op = operation()
    measure_op["kind"] = "measurement"
    measurement = collector.record(metadata(), measure_op)
    collector.link_visit([correction, measurement])
    sharp = np.zeros((32, 32), dtype=np.uint16)
    sharp[:, 16:] = 2000
    blurred = cv2.GaussianBlur(sharp, (9, 9), 2)
    collector.record_image(sharp, image_info())
    collector.record_image(blurred, image_info("GFP", 1))
    collector.finalize()
    rows = read_rows(tmp_path / "tenengrad.csv")
    assert all(row["event_id"] == str(correction["event_id"]) for row in rows)
    assert rows[0]["visit_event_ids"] == f"{correction['event_id']};{measurement['event_id']}"
    assert [row["channel"] for row in rows] == ["BF", "GFP"]
    assert [row["z_index"] for row in rows] == ["0", "1"]
    assert float(rows[0]["tenengrad"]) > float(rows[1]["tenengrad"]) > 0
    assert (tmp_path / "tenengrad.png").stat().st_size > 1000
    assert (tmp_path / "tenengrad.svg").exists()


def test_worker_finalize_creates_reports_for_recorded_events_on_partial_run(tmp_path):
    worker, _, _ = make_worker(tmp_path)
    worker._af_validator.record(metadata(), paired_operation())
    worker._finalize_af_validation()
    assert (tmp_path / "before_after_native.tif").exists()
    worker._log.exception.assert_not_called()


def test_worker_records_data_scores_only_when_imaging_enabled(tmp_path):
    worker, _, _ = make_worker(tmp_path)
    worker._record_validation_image(np.ones((8, 8), dtype=np.uint8), image_info())
    assert not (tmp_path / "tenengrad.csv").exists()
    worker.validation_with_imaging = True
    worker._af_validator.link_visit([worker._af_validator.record(metadata(), paired_operation())])
    worker._record_validation_image(np.ones((8, 8), dtype=np.uint8), image_info())
    assert float(read_rows(tmp_path / "tenengrad.csv")[0]["tenengrad"]) == 0


def test_data_camera_callback_scores_the_captured_frame_and_its_capture_info(tmp_path):
    worker, _, _ = make_worker(tmp_path)
    worker.validation_with_imaging = True
    worker._af_validator.link_visit([worker._af_validator.record(metadata(), paired_operation())])
    worker._af_owner_lock = threading.Lock()
    worker._af_frame_ids = set()
    worker._ready_for_next_trigger = threading.Event()
    worker._outstanding_lock = threading.Lock()
    worker._image_callback_idle = threading.Event()
    worker._outstanding_frames = 0
    worker._capture_ts = {}
    worker._current_capture_info = image_info("GFP", 2)
    worker.image_count = worker._timepoint_image_count = 0
    worker._first_job_dispatched = True
    worker._job_runners = []
    frame = SimpleNamespace(frame_id=4, frame=np.eye(16, dtype=np.uint16))
    worker._image_callback(frame)
    row = read_rows(tmp_path / "tenengrad.csv")[0]
    assert row["channel"] == "GFP" and row["z_index"] == "2"
    assert row["event_id"]
    assert float(row["tenengrad"]) > 0
    assert worker._image_callback_idle.is_set()
    worker.callbacks.signal_new_image.assert_called_once()
    worker.request_abort_fn.assert_not_called()
