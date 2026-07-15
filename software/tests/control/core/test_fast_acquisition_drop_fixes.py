"""Regression tests for the fast-acquisition frame-drop fixes.

These cover, without any camera/DAQ hardware:
- FastAcquisitionFrameBuffer.write_frame_from_ptr: the single-copy ctypes path the
  Tucsen SDK callback uses (copy straight from a source pointer into the ring slab).
- The writer's post-loop drain: frames sitting in the ring when stop is signaled must
  be written, not silently truncated.
- overwrite_when_full=False: a full ring drops the newest frame loudly instead of
  silently overwriting already-captured frames.
"""

import ctypes

import numpy as np

from control.core.fast_acquisition_buffer import FastAcquisitionFrameBuffer
from control.core.fast_acquisition_writer import FastAcquisitionWriter


def _ptr_to(data: bytes):
    """A ctypes void pointer to a freshly allocated copy of ``data`` (kept alive by caller)."""
    buf = ctypes.create_string_buffer(data, len(data))
    return ctypes.cast(buf, ctypes.c_void_p), buf


def test_write_frame_from_ptr_roundtrip():
    buf = FastAcquisitionFrameBuffer(
        buffer_size=4, max_frame_bytes=8, frame_shape=(2, 2), dtype=np.uint16
    )
    payload = np.array([1, 2, 3, 4], dtype=np.uint16).tobytes()  # 8 bytes
    ptr, _keep = _ptr_to(payload)

    assert buf.write_frame_from_ptr(ptr, len(payload), frame_id=7, timestamp=1.5, metadata={"k": "v"})

    frame_bytes, frame_id, timestamp, metadata = buf.read_frame()
    assert frame_bytes == payload
    assert frame_id == 7
    assert timestamp == 1.5
    assert metadata["k"] == "v"
    assert metadata["frame_byte_length"] == len(payload)


def test_write_frame_from_ptr_truncates_to_max():
    # SDK delivers more bytes (e.g. unpacked 16-bit) than the packed ring slot holds.
    buf = FastAcquisitionFrameBuffer(
        buffer_size=2, max_frame_bytes=6, frame_shape=(2, 2), dtype=np.uint16
    )
    payload = bytes(range(10))  # 10 bytes, slot holds 6
    ptr, _keep = _ptr_to(payload)

    assert buf.write_frame_from_ptr(ptr, len(payload), frame_id=0, timestamp=0.0)
    frame_bytes, *_ = buf.read_frame()
    assert frame_bytes == payload[:6]


def test_write_frame_from_ptr_matches_write_frame_bytes():
    """The pointer path stores exactly what the bytes path would."""
    payload = np.arange(8, dtype=np.uint16).tobytes()

    a = FastAcquisitionFrameBuffer(buffer_size=2, max_frame_bytes=16, frame_shape=(2, 4), dtype=np.uint16)
    a.write_frame(payload, frame_id=0, timestamp=0.0)
    bytes_path, *_ = a.read_frame()

    b = FastAcquisitionFrameBuffer(buffer_size=2, max_frame_bytes=16, frame_shape=(2, 4), dtype=np.uint16)
    ptr, _keep = _ptr_to(payload)
    b.write_frame_from_ptr(ptr, len(payload), frame_id=0, timestamp=0.0)
    ptr_path, *_ = b.read_frame()

    assert bytes_path == ptr_path == payload


def test_overwrite_when_full_false_drops_newest():
    buf = FastAcquisitionFrameBuffer(
        buffer_size=2, max_frame_bytes=4, frame_shape=(1, 2), dtype=np.uint16,
        overwrite_when_full=False,
    )
    f0 = np.array([10, 11], dtype=np.uint16).tobytes()
    f1 = np.array([20, 21], dtype=np.uint16).tobytes()
    f2 = np.array([30, 31], dtype=np.uint16).tobytes()
    assert buf.write_frame(f0, 0, 0.0)
    assert buf.write_frame(f1, 1, 0.0)
    # Ring full -> newest dropped, oldest preserved.
    assert buf.write_frame(f2, 2, 0.0) is False
    assert buf.read_frame()[0] == f0
    assert buf.read_frame()[0] == f1
    assert buf.read_frame() is None


def test_ring_full_drops_are_counted():
    """Every ring-full drop is counted (logging is throttled, counting is not)."""
    buf = FastAcquisitionFrameBuffer(
        buffer_size=2, max_frame_bytes=4, frame_shape=(1, 2), dtype=np.uint16,
        overwrite_when_full=False,
    )
    payload = np.array([1, 2], dtype=np.uint16).tobytes()
    assert buf.write_frame(payload, 0, 0.0)
    assert buf.write_frame(payload, 1, 0.0)
    for i in range(2, 9):  # 7 drops via both write paths
        if i % 2:
            assert buf.write_frame(payload, i, 0.0) is False
        else:
            ptr, _keep = _ptr_to(payload)
            assert buf.write_frame_from_ptr(ptr, len(payload), i, 0.0) is False
    assert buf.get_buffer_status()["dropped_frames"] == 7
    buf.clear()
    assert buf.get_buffer_status()["dropped_frames"] == 0


def test_ring_sized_to_available_ram():
    """The ring covers the whole capture when RAM allows; otherwise it is capped by
    (available - headroom), with the old 4 GiB budget as the floor."""
    from control.core.fast_acquisition_controller import (
        FAST_ACQ_RING_BUFFER_MIN_BYTES,
        FAST_ACQ_RING_RAM_HEADROOM_BYTES,
        ring_frames_for_capture,
    )

    frame_bytes = 2_160_000  # 2400x600 12-bit packed (the Aries "speed" mode frame)

    # 21.6 GB capture, 37 GiB free: whole capture fits in the ring -> zero drops.
    assert ring_frames_for_capture(500, 10_000, frame_bytes, 37 * 1024**3) == 10_000

    # RAM-starved host: budget floors at the old 4 GiB cap.
    starved = ring_frames_for_capture(
        500, 10_000, frame_bytes, FAST_ACQ_RING_RAM_HEADROOM_BYTES + 1024**3
    )
    assert starved == FAST_ACQ_RING_BUFFER_MIN_BYTES // frame_bytes

    # Small captures never allocate more than needed; requested size stays the floor.
    assert ring_frames_for_capture(500, 100, frame_bytes, 37 * 1024**3) == 500


def test_writer_drains_ring_on_stop(tmp_path):
    """Frames buffered before stop must be written by the post-loop drain, not lost."""
    h, w = 2, 2
    max_bytes = h * w * 2
    buf = FastAcquisitionFrameBuffer(
        buffer_size=16, max_frame_bytes=max_bytes, frame_shape=(h, w), dtype=np.uint16
    )
    n = 8
    for i in range(n):
        payload = np.full(h * w, i, dtype=np.uint16).tobytes()
        buf.write_frame(payload, frame_id=i, timestamp=float(i),
                        metadata={"height": h, "width": w})

    writer = FastAcquisitionWriter(
        frame_buffer=buf,
        output_path=str(tmp_path),
        file_format="raw",  # skip TIFF/Zarr conversion
        frame_shape=(h, w),
        dtype=np.uint16,
    )
    # Signal stop before the run loop starts: the main loop is skipped entirely, so
    # only the post-loop drain can account for the pre-buffered frames.
    writer._stop_event.set()
    writer.start()
    writer.join(timeout=10.0)
    assert not writer.is_alive()
    assert writer.get_write_statistics()["frames_written"] == n


def _run_tiff_writer(tmp_path, n, h, w):
    buf = FastAcquisitionFrameBuffer(
        buffer_size=max(n, 4), max_frame_bytes=h * w * 2, frame_shape=(h, w), dtype=np.uint16
    )
    for i in range(n):
        payload = np.full(h * w, i, dtype=np.uint16).tobytes()
        buf.write_frame(payload, frame_id=i, timestamp=float(i), metadata={"height": h, "width": w})
    writer = FastAcquisitionWriter(
        frame_buffer=buf, output_path=str(tmp_path), file_format="tiff",
        frame_shape=(h, w), dtype=np.uint16,
    )
    writer._stop_event.set()
    writer.start()
    writer.join(timeout=30.0)
    assert not writer.is_alive()
    return tmp_path / "frames"


def test_tiff_writer_single_file_when_under_limit(tmp_path):
    frames_dir = _run_tiff_writer(tmp_path, n=5, h=4, w=4)
    assert (frames_dir / "frames_stack.tiff").exists()
    assert not list(frames_dir.glob("frames_stack_*.tiff"))


def test_tiff_writer_splits_over_4gb_limit(tmp_path, monkeypatch):
    import imageio as iio
    from control.core import fast_acquisition_writer as faw

    h, w, n = 4, 4, 10
    # Force splitting at 3 frames per file (32 bytes/frame * 3).
    monkeypatch.setattr(faw, "FAST_ACQ_MAX_TIFF_BYTES", 3 * h * w * 2)
    frames_dir = _run_tiff_writer(tmp_path, n=n, h=h, w=w)

    parts = sorted(frames_dir.glob("frames_stack_*.tiff"))
    assert len(parts) == 4  # ceil(10 / 3)
    assert not (frames_dir / "frames_stack.tiff").exists()  # numbered scheme when split

    total = sum(len(iio.mimread(str(p), format="tiff")) for p in parts)
    assert total == n
    # raw file removed only after all chunks wrote
    assert not (frames_dir / "frames.raw").exists()
