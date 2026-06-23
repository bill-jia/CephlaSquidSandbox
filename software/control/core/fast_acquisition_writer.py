"""
Frame writer thread for fast acquisition.

Streams raw frame bytes to frames.raw + frame_metadata.jsonl during capture, then runs
byte_decoding_fn (when set) to produce TIFF / Zarr / HDF5 after the raw stream is closed.

Underfilled packed frames: when ``expected_decode_bytes`` is present in per-frame metadata,
raw bytes are truncated or zero-padded to that length before ``byte_decoding_fn`` is called.
Decoders should assume full-sized buffers (padding is not done inside camera decode helpers).
"""

import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import squid.logging

from control.core.fast_acquisition_buffer import FastAcquisitionFrameBuffer

# Keys stored for I/O layout only; excluded when passing per-line dict to byte_decoding_fn
_JSONL_LAYOUT_KEYS = frozenset(
    {"frame_id", "timestamp", "byte_offset", "byte_length", "frame_byte_length"}
)

# Classic TIFF uses 32-bit byte offsets, so a single file cannot exceed 4 GiB. Keep each
# written stack safely under that; larger captures are split across multiple files.
FAST_ACQ_MAX_TIFF_BYTES = int(3.8 * 1024**3)


class FastAcquisitionWriter(threading.Thread):
    """
    Thread that reads from the buffer, writes a raw bytestream, then finalizes
    encoded outputs (TIFF / Zarr / HDF5) when capture completes.
    """

    def __init__(
        self,
        frame_buffer: FastAcquisitionFrameBuffer,
        output_path: str,
        file_format: str = "tiff",
        frames_per_file: int = 1000,
        byte_decoding_fn: Optional[Callable[[bytes, dict], np.ndarray]] = None,
        frame_shape: Optional[Tuple[int, int]] = None,
        dtype: Optional[np.dtype] = None
    ):
        super().__init__(daemon=True)
        self._log = squid.logging.get_logger(self.__class__.__name__)

        self._frame_buffer = frame_buffer
        self._output_path = output_path
        self._file_format = file_format.lower()
        self._frame_timestamps_ms: List[float] = []
        self._frames_per_file = frames_per_file
        self._byte_decoding_fn = byte_decoding_fn
        self._frame_shape = frame_shape
        self._dtype = dtype if dtype is not None else np.uint16
        self._bytes_per_frame: Optional[int] = None
        self._frames_written = 0
        self._start_time: Optional[float] = None
        self._last_write_time: Optional[float] = None
        self._write_times: List[float] = []
        self._stats_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._running = False
        self._conversion_in_progress = False
        os.makedirs(output_path, exist_ok=True)
        self._frames_dir = os.path.join(output_path, "frames")
        os.makedirs(self._frames_dir, exist_ok=True)

        self._raw_file = None
        self._raw_file_path = os.path.join(self._frames_dir, "frames.raw")

        self._jsonl_path = os.path.join(self._frames_dir, "frame_metadata.jsonl")
        self._jsonl_file = None
        self._raw_byte_offset = 0

        # Flush raw + jsonl together every N frames instead of on every frame. The
        # per-frame flush was a fsync-class syscall at up to the full camera rate and
        # slowed the drain; periodic flushing keeps the raw stream and its jsonl index
        # consistent on disk while bounding worst-case loss on a hard crash to N frames.
        self._flush_every_n_frames = 64
        self._frames_since_flush = 0

        if self._file_format == "zarr":
            try:
                import zarr  # noqa: F401
            except ImportError:
                self._log.warning("zarr not available, falling back to TIFF")
                self._file_format = "tiff"
        elif self._file_format == "hdf5":
            try:
                import h5py  # noqa: F401
            except ImportError:
                self._log.warning("h5py not available, falling back to TIFF")
                self._file_format = "tiff"

        self._log.info(
            f"Initialized writer: format={self._file_format}, output={output_path}"
        )

    def run(self) -> None:
        self._running = True
        self._start_time = time.time()
        self._log.info("Frame writer thread started")

        try:
            self._jsonl_file = open(self._jsonl_path, "w", encoding="utf-8")
        except Exception as e:
            self._log.error(f"Failed to open frame metadata jsonl: {e}", exc_info=True)
            self._stop_event.set()

        try:
            self._raw_file = open(self._raw_file_path, "wb")
            self._log.info(f"Opened raw frame file at {self._raw_file_path}")
        except Exception as e:
            self._log.error(f"Failed to open raw frame file: {e}", exc_info=True)
            self._stop_event.set()

        try:
            while not self._stop_event.is_set():
                # Read frame from buffer
                frame_data = self._frame_buffer.read_frame()

                if frame_data is None:
                    time.sleep(0.001)
                    continue

                self._consume_frame(frame_data)

            # Drain any frames still in the ring after stop was signaled. The producer
            # (SDK callback) can deliver a backlog faster than we write it to disk;
            # exiting the moment _stop_event fires would silently truncate the capture.
            drained = 0
            while True:
                frame_data = self._frame_buffer.read_frame()
                if frame_data is None:
                    break
                self._consume_frame(frame_data)
                drained += 1
            if drained:
                self._log.info(f"Drained {drained} buffered frames after stop")

        except Exception as e:
            self._log.error(f"Error in writer thread: {e}", exc_info=True)
        finally:
            # Cleanup
            try:
                if self._raw_file is not None:
                    self._raw_file.close()
                    self._log.info(f"Closed raw frame file {self._raw_file_path}")
            except Exception as e:
                self._log.warning(f"Error closing raw frame file: {e}", exc_info=True)

            try:
                if self._jsonl_file is not None:
                    self._jsonl_file.close()
            except Exception as e:
                self._log.warning(f"Error closing jsonl: {e}", exc_info=True)

            try:
                self._finalize_after_raw_closed()
            except Exception as e:
                self._log.error(f"Post-process after raw capture failed: {e}", exc_info=True)

            self._running = False
            self._frame_timestamps_ms = np.array(self._frame_timestamps_ms)
            np.save(os.path.join(self._output_path, "frame_timestamps_ms.npy"), self._frame_timestamps_ms)
            self._log.info("Frame writer thread stopped")

    def stop(self, wait: bool = True) -> None:
        """
        Signal the writer thread to stop. When ``wait`` is True (default), block until
        the thread exits (raw files closed and optional TIFF/Zarr/HDF5 conversion done).
        When ``wait`` is False, return immediately while conversion may still run in the background.
        """
        self._log.info("Stopping frame writer thread...")
        self._stop_event.set()
        if wait:
            self.join()

    @property
    def is_converting_frames(self) -> bool:
        """True while post-capture decode/write (TIFF/Zarr/HDF5) is running on the writer thread."""
        return self._conversion_in_progress

    def _finalize_after_raw_closed(self) -> None:
        """Decode raw bytestream into TIFF / Zarr / HDF5 when applicable."""
        if self._file_format == "raw":
            return
        self._conversion_in_progress = True
        self._log.info(
            f"Starting frame conversion to output format ({self._file_format}); "
            "this may take a while for large captures"
        )
        try:
            if self._file_format in ("tiff", "tif"):
                self._convert_raw_to_tiff_stack()
            elif self._file_format == "zarr":
                self._build_zarr_from_raw()
            elif self._file_format == "hdf5":
                self._build_hdf5_from_raw()
        finally:
            self._conversion_in_progress = False

    @staticmethod
    def _pad_raw_to_expected(raw: bytes, expected: int) -> bytes:
        """Truncate or zero-pad so len == expected (underfilled frames are padded)."""
        if len(raw) >= expected:
            return raw[:expected]
        return raw + b"\x00" * (expected - len(raw))

    def _decode_frame(self, frame_bytes: bytes, meta: dict) -> np.ndarray:
        if self._byte_decoding_fn is not None:
            if "height" not in meta or "width" not in meta:
                raise ValueError(
                    "byte_decoding_fn requires per-frame metadata 'height' and 'width' "
                    "(recorded with the frame; not inferred from byte length)"
                )
            padded = frame_bytes
            exp = meta.get("expected_decode_bytes")
            if exp is not None:
                padded = self._pad_raw_to_expected(frame_bytes, int(exp))
            return self._byte_decoding_fn(padded, meta)
        if self._frame_shape is None:
            raise ValueError("frame_shape is required when byte_decoding_fn is None")
        h, w = self._frame_shape
        return np.frombuffer(frame_bytes, dtype=self._dtype).reshape(h, w)

    @staticmethod
    def _meta_for_decode(rec: dict) -> dict:
        return {k: v for k, v in rec.items() if k not in _JSONL_LAYOUT_KEYS}

    def _read_jsonl_records(self) -> List[dict]:
        records: List[dict] = []
        if not os.path.isfile(self._jsonl_path):
            return records
        with open(self._jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                records.append(json.loads(line))
        return records

    def _convert_raw_to_tiff_stack(self) -> None:
        import imageio as iio

        raw_path = self._raw_file_path
        if not os.path.exists(raw_path):
            self._log.warning(f"Raw frame file not found at {raw_path}, skipping TIFF conversion")
            return

        records = self._read_jsonl_records()
        if not records:
            self._log.warning("No frame_metadata.jsonl; cannot recover per-frame byte lengths for TIFF")
            return

        # Split into multiple files when the decoded stack would exceed the 4 GiB TIFF
        # limit. Decode one file's worth of frames at a time so peak RAM stays bounded.
        first = records[0]
        fh = int(first.get("height") or (self._frame_shape[0] if self._frame_shape else 0))
        fw = int(first.get("width") or (self._frame_shape[1] if self._frame_shape else 0))
        bytes_per_frame = max(1, fh * fw * np.dtype(self._dtype).itemsize)
        frames_per_file = max(1, FAST_ACQ_MAX_TIFF_BYTES // bytes_per_frame)
        n_files = (len(records) + frames_per_file - 1) // frames_per_file
        multi = n_files > 1
        if multi:
            self._log.info(
                f"Decoded TIFF stack (~{len(records) * bytes_per_frame / 1024**3:.1f} GiB) exceeds the "
                f"4 GiB TIFF limit; splitting {len(records)} frames into {n_files} files "
                f"of up to {frames_per_file} frames each"
            )

        written: List[str] = []
        with open(raw_path, "rb") as f:
            for file_idx in range(n_files):
                start = file_idx * frames_per_file
                chunk_records = records[start:start + frames_per_file]
                planes: List[np.ndarray] = []
                for i, rec in enumerate(chunk_records, start=start):
                    off = int(rec["byte_offset"])
                    ln = rec["frame_byte_length"]
                    f.seek(off)
                    chunk = f.read(ln)
                    if len(chunk) != ln:
                        self._log.warning(
                            f"Short read at frame {i}: got {len(chunk)} bytes, expected {ln}"
                        )
                        break
                    planes.append(self._decode_frame(chunk, rec))
                if not planes:
                    continue
                h, w = planes[0].shape
                name = f"frames_stack_{file_idx:03d}.tiff" if multi else "frames_stack.tiff"
                stack_path = os.path.join(self._frames_dir, name)
                try:
                    # imageio wants a sequence of 2D images for a multipage TIFF (a 3D
                    # ndarray is rejected); the list also avoids an np.stack copy.
                    iio.mimwrite(stack_path, planes, format="tiff")
                    written.append(stack_path)
                    self._log.info(
                        f"Wrote 3D TIFF stack ({len(planes)} frames, {h}x{w}) to {stack_path}"
                        + (f" [{file_idx + 1}/{n_files}]" if multi else "")
                    )
                except Exception as e:
                    self._log.error(f"Failed to write TIFF stack {stack_path}: {e}", exc_info=True)
                    return  # leave raw in place for recovery if any chunk fails

        if not written:
            self._log.warning("No frames decoded for TIFF")
            return
        try:
            os.remove(raw_path)
            self._log.info(f"Deleted raw file {raw_path} after TIFF conversion ({len(written)} file(s))")
        except OSError as e:
            self._log.warning(f"Could not delete raw file: {e}", exc_info=True)

    def _build_zarr_from_raw(self) -> None:

        records = self._read_jsonl_records()
        if not records:
            self._log.warning("No jsonl records for Zarr build")
            return
        raw_path = self._raw_file_path
        zarr_path = os.path.join(self._output_path, "frames.zarr")
        group = zarr.open(zarr_path, mode="w")
        ds_frames = None
        ds_ids = None
        ds_ts = None

        with open(raw_path, "rb") as raw_f:
            for rec in records:
                off = int(rec["byte_offset"])
                ln = rec["frame_byte_length"]
                frame_id = int(rec["frame_id"])
                ts = float(rec["timestamp"])
                raw_f.seek(off)
                chunk = raw_f.read(ln)
                meta = FastAcquisitionWriter._meta_for_decode(rec)
                frame = self._decode_frame(chunk, meta)
                if ds_frames is None:
                    shape = (0, *frame.shape)
                    chunks = (100, *frame.shape)
                    ds_frames = group.create_dataset(
                        "frames",
                        shape=shape,
                        chunks=chunks,
                        dtype=frame.dtype,
                        compressor=zarr.Blosc(cname="lz4", clevel=5),
                    )
                    ds_ids = group.create_dataset(
                        "frame_ids", shape=(0,), chunks=(1000,), dtype=np.int64
                    )
                    ds_ts = group.create_dataset(
                        "timestamps", shape=(0,), chunks=(1000,), dtype=np.float64
                    )
                ds_frames.append(frame[np.newaxis, ...])
                ds_ids.append(np.array([frame_id]))
                ds_ts.append(np.array([ts]))
        group.close()
        self._log.info(f"Wrote Zarr dataset to {zarr_path}")
        try:
            os.remove(raw_path)
        except OSError as e:
            self._log.warning(f"Could not delete raw after Zarr: {e}", exc_info=True)

    def _build_hdf5_from_raw(self) -> None:
        import h5py

        records = self._read_jsonl_records()
        if not records:
            self._log.warning("No jsonl records for HDF5 build")
            return
        raw_path = self._raw_file_path
        h5_path = os.path.join(self._output_path, "frames.h5")
        h5 = h5py.File(h5_path, "w")
        ds_frames = None
        ds_ids = None
        ds_ts = None

        try:
            with open(raw_path, "rb") as raw_f:
                for rec in records:
                    off = int(rec["byte_offset"])
                    ln = rec["frame_byte_length"]
                    frame_id = int(rec["frame_id"])
                    ts = float(rec["timestamp"])
                    raw_f.seek(off)
                    chunk = raw_f.read(ln)
                    meta = FastAcquisitionWriter._meta_for_decode(rec)
                    frame = self._decode_frame(chunk, meta)
                    if ds_frames is None:
                        shape = (0, *frame.shape)
                        maxshape = (None, *frame.shape)
                        ds_frames = h5.create_dataset(
                            "frames",
                            shape=shape,
                            maxshape=maxshape,
                            dtype=frame.dtype,
                            chunks=(100, *frame.shape),
                            compression="gzip",
                            compression_opts=4,
                        )
                        ds_ids = h5.create_dataset(
                            "frame_ids",
                            shape=(0,),
                            maxshape=(None,),
                            dtype=np.int64,
                            chunks=(1000,),
                        )
                        ds_ts = h5.create_dataset(
                            "timestamps",
                            shape=(0,),
                            maxshape=(None,),
                            dtype=np.float64,
                            chunks=(1000,),
                        )
                    cur = ds_frames.shape[0]
                    ds_frames.resize((cur + 1, *frame.shape))
                    ds_frames[cur] = frame
                    ds_ids.resize((cur + 1,))
                    ds_ids[cur] = frame_id
                    ds_ts.resize((cur + 1,))
                    ds_ts[cur] = ts
        finally:
            h5.close()
        self._log.info(f"Wrote HDF5 to {h5_path}")
        try:
            os.remove(raw_path)
        except OSError as e:
            self._log.warning(f"Could not delete raw after HDF5: {e}", exc_info=True)

    @staticmethod
    def _json_safe(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: FastAcquisitionWriter._json_safe(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [FastAcquisitionWriter._json_safe(x) for x in obj]
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return obj

    def _append_jsonl(
        self,
        frame_bytes: bytes,
        frame_id: int,
        timestamp: float,
        metadata: dict,
    ) -> None:
        if self._jsonl_file is None:
            return
        flen = len(frame_bytes)
        # Drop keys that are authoritative record fields so the metadata copy can't
        # clobber them. In particular the SDK's per-frame "timestamp" is 0 on this
        # firmware; the real timestamp is the computed one passed in here.
        meta_out = {
            k: v for k, v in metadata.items() if k not in ("frame_byte_length", "timestamp")
        }
        record = {
            "frame_id": frame_id,
            "timestamp": timestamp,
            "byte_length": flen,
            "frame_byte_length": flen,
            "byte_offset": self._raw_byte_offset,
        }
        self._raw_byte_offset += len(frame_bytes)
        record.update(self._json_safe(meta_out))
        self._jsonl_file.write(json.dumps(record) + "\n")

    def _consume_frame(self, frame_data: Tuple[bytes, int, float, dict]) -> None:
        """Write one frame read from the ring buffer and update statistics."""
        frame_bytes, frame_id, timestamp, metadata = frame_data
        write_start = time.time()
        success = self._write_frame(frame_bytes, frame_id, timestamp, metadata)
        write_time = time.time() - write_start
        if success:
            with self._stats_lock:
                self._frames_written += 1
                self._last_write_time = time.time()
                self._write_times.append(write_time)
                # Keep only last 100 write times for statistics
                if len(self._write_times) > 100:
                    self._write_times.pop(0)
        else:
            self._log.error(f"Failed to write frame {frame_id}")

    def _write_frame(
        self, frame_bytes: bytes, frame_id: int, timestamp: float, metadata: dict
    ) -> bool:
        try:
            self._frame_timestamps_ms.append(timestamp)
            if self._bytes_per_frame is None:
                self._bytes_per_frame = len(frame_bytes)
            self._append_jsonl(frame_bytes, frame_id, timestamp, metadata)
            n = self._raw_file.write(frame_bytes)
            # Flush raw before jsonl so the index never references unflushed raw bytes.
            self._frames_since_flush += 1
            if self._frames_since_flush >= self._flush_every_n_frames:
                self._raw_file.flush()
                if self._jsonl_file is not None:
                    self._jsonl_file.flush()
                self._frames_since_flush = 0
            return n == len(frame_bytes)
        except Exception as e:
            self._log.error(f"Error writing frame {frame_id}: {e}")
            return False

    def get_write_statistics(self) -> Dict[str, float]:
        with self._stats_lock:
            if self._frames_written == 0:
                return {
                    "frames_written": 0,
                    "write_rate": 0.0,
                    "avg_write_time": 0.0,
                    "max_write_time": 0.0,
                }

            elapsed = time.time() - self._start_time if self._start_time else 1.0
            write_rate = self._frames_written / elapsed

            if self._write_times:
                avg_write_time = np.mean(self._write_times) * 1000
                max_write_time = np.max(self._write_times) * 1000
            else:
                avg_write_time = 0.0
                max_write_time = 0.0

            return {
                "frames_written": self._frames_written,
                "write_rate": write_rate,
                "avg_write_time": avg_write_time,
                "max_write_time": max_write_time,
            }
