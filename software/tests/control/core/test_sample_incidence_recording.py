import json

import numpy as np
import pytest
import tifffile

from control.core.sample_incidence_calibration import AlignmentSettings, CalibrationCancelled, SampleIncidenceSession
from control.core.sample_incidence_recording import AlignmentRecorder
from squid.config import DirectionSign, get_stage_config
from tests.control.core.test_sample_incidence_calibration import hardware


def read_events(recorder):
    return [json.loads(line) for line in (recorder.path / "events.jsonl").read_text().splitlines()]


def test_lossless_images_and_numeric_analysis_can_be_loaded_without_pickle(tmp_path):
    recorder = AlignmentRecorder(tmp_path)
    config = get_stage_config().model_copy(deep=True)
    config.X_AXIS.CANONICAL_SIGN = DirectionSign(-1)
    recorder.start({"settings": AlignmentSettings(), "stage_config": config})
    image = (np.arange(48 * 96, dtype=np.uint16).reshape(48, 96) * 7).astype(np.uint16)
    recorder.capture(image, phase="flatness", field=3, plane=4, stage_canonical_mm={"x": 2, "y": 3, "z": 1.234})
    recorder.record("focus", scores=np.array([1, np.nan, 3]), peaks={"height_um": np.arange(9).reshape(3, 3)})
    recorder.finish("completed")
    events = read_events(recorder)
    restored = tifffile.imread(recorder.path / events[0]["image"])
    np.testing.assert_array_equal(restored, image)
    assert restored.dtype == np.uint16
    with tifffile.TiffFile(recorder.path / events[0]["image"]) as tiff:
        description = json.loads(tiff.pages[0].description)
        assert description["stage_canonical_mm"]["z"] == 1.234
    metadata = json.loads((recorder.path / events[1]["metadata"]).read_text())
    with np.load(recorder.path / events[1]["arrays"], allow_pickle=False) as arrays:
        np.testing.assert_array_equal(arrays[metadata["scores"]["npz_key"]], [1, np.nan, 3])
        assert arrays[metadata["peaks"]["height_um"]["npz_key"]].shape == (3, 3)
    manifest = json.loads((recorder.path / "session.json").read_text())
    assert manifest["metadata"]["stage_config"]["X_AXIS"]["CANONICAL_SIGN"] == -1
    assert manifest["status"] == "completed"


def test_each_run_uses_a_separate_directory(tmp_path):
    first, second = AlignmentRecorder(tmp_path), AlignmentRecorder(tmp_path)
    first.start({})
    second.start({})
    assert first.path != second.path
    assert first.path.is_dir() and second.path.is_dir()
    with pytest.raises(ValueError, match="Choose"):
        AlignmentRecorder(" ")


@pytest.mark.parametrize("stop", ["cancel", "analysis_error", "disk_error"])
def test_partial_data_survive_and_hardware_is_restored_on_stop(tmp_path, monkeypatch, stop):
    hw, light, _ = hardware(initialized=False)
    recorder = AlignmentRecorder(tmp_path)

    def progress(message, image):
        if stop == "cancel" and message.startswith("Flatness:"):
            hw.cancel.set()

    if stop == "disk_error":

        def disk_full(*args, **kwargs):
            assert not light.on  # Disk I/O never leaves capture illumination on.
            raise OSError("disk full")

        monkeypatch.setattr(tifffile, "imwrite", disk_full)
    session = SampleIncidenceSession(
        hw, AlignmentSettings(repeat=False, settle_s=0), 1, progress=progress, recorder=recorder
    )
    expected = {"cancel": CalibrationCancelled, "analysis_error": ValueError, "disk_error": OSError}[stop]
    with pytest.raises(expected):
        session.run()
    np.testing.assert_allclose(hw.stage.xyz, [10, 11, 2])
    assert not light.on and hw.live.is_live and hw.camera.streaming and hw.camera.callbacks
    manifest = json.loads((recorder.path / "session.json").read_text())
    assert manifest["status"] == ("cancelled" if stop == "cancel" else "failed")
    assert manifest["traceback"]
    assert manifest["final_position"]["stage_canonical_mm"]["z"] == 2
    if stop != "disk_error":
        events = read_events(recorder)
        captures = [event for event in events if event["kind"] == "capture"]
        assert len(captures) >= 2
        for event in captures:
            assert tifffile.imread(recorder.path / event["image"]).shape == (128, 128)


def test_unwritable_destination_fails_before_camera_or_stage_takeover(tmp_path):
    destination = tmp_path / "file_not_folder"
    destination.write_text("Existing data must remain intact.")
    hw, light, _ = hardware(initialized=False)
    session = SampleIncidenceSession(hw, AlignmentSettings(repeat=False), 1, recorder=AlignmentRecorder(destination))
    with pytest.raises(OSError):
        session.run()
    assert hw.camera.frame_id == 0 and not hw.stage.moves
    assert hw.camera.mode == "original" and hw.live.is_live and not light.on
    assert destination.read_text() == "Existing data must remain intact."
