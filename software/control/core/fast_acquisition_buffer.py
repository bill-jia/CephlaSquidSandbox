"""
Ring buffer for fast acquisition frame storage.

Stores raw per-frame bytestreams in a pre-allocated uint8 slab, with parallel
metadata (ids, timestamps, per-frame dicts).
"""

import ctypes
import threading
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import squid.logging


class FastAcquisitionFrameBuffer:
    """
    Ring buffer for raw frame bytes during fast acquisition.

    Thread-safe operations using RLock for concurrent access.
    """

    def __init__(
        self,
        buffer_size: int,
        max_frame_bytes: int,
        frame_shape: Tuple[int, int],
        dtype: np.dtype,
        overwrite_when_full: bool = True,
    ):
        """
        Args:
            buffer_size: Number of frames to buffer (e.g., 100-1000)
            max_frame_bytes: Maximum bytes per frame (camera-specific upper bound)
            frame_shape: (height, width) — logical frame shape for decoded data
            dtype: NumPy dtype used when interpreting raw bytes as flat array (e.g. uint16)
            overwrite_when_full: If True, overwrite oldest frames when full.
        """
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self._buffer_size = buffer_size
        self._max_frame_bytes = int(max_frame_bytes)
        self._frame_shape = frame_shape
        self._dtype = dtype
        self._overwrite_when_full = overwrite_when_full

        self._buffer = np.zeros((buffer_size, self._max_frame_bytes), dtype=np.uint8)
        self._byte_lengths = np.zeros(buffer_size, dtype=np.int32)
        self._frame_ids = np.zeros(buffer_size, dtype=np.int64)
        self._timestamps = np.zeros(buffer_size, dtype=np.float64)
        self._metadata: List[Dict[str, Any]] = [{} for _ in range(buffer_size)]

        self._write_index = 0
        self._read_index = 0
        self._frame_count = 0
        self._available_frames = 0
        self._lock = threading.RLock()

        self._log.info(
            f"Initialized raw frame buffer: size={buffer_size}, max_bytes={self._max_frame_bytes}, "
            f"shape={frame_shape}, dtype={dtype}, memory={self._buffer.nbytes / 1024**2:.1f} MB"
        )

    def write_frame(
        self,
        frame_bytes: bytes,
        frame_id: int,
        timestamp: float,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Write one frame (raw bytes + optional per-frame metadata dict)."""
        n = len(frame_bytes)
        if n > self._max_frame_bytes:
            frame_bytes = frame_bytes[:self._max_frame_bytes]

        with self._lock:
            if self._available_frames >= self._buffer_size:
                if not self._overwrite_when_full:
                    self._log.warning(
                        f"Buffer full (available={self._available_frames}), frame {frame_id} dropped"
                    )
                    return False
                self._read_index = (self._read_index + 1) % self._buffer_size
                self._available_frames -= 1
                self._log.debug(
                    f"Buffer full, overwriting frame at read index {self._read_index}"
                )

            slot = self._write_index
            self._buffer[slot, :n] = np.frombuffer(frame_bytes, dtype=np.uint8)
            if n < self._max_frame_bytes:
                self._buffer[slot, n:] = 0
            self._byte_lengths[slot] = n
            self._frame_ids[slot] = frame_id
            self._timestamps[slot] = timestamp
            # Metadata is a flat dict of scalars (timestamp, frame_index, height,
            # width, ...), so a shallow copy is sufficient and far cheaper than a
            # deepcopy on this per-frame hot path.
            self._metadata[slot] = dict(metadata) if metadata else {}

            self._write_index = (self._write_index + 1) % self._buffer_size
            self._frame_count += 1
            self._available_frames += 1

            return True

    def write_frame_from_ptr(
        self,
        src_ptr: Any,
        n_bytes: int,
        frame_id: int,
        timestamp: float,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Write one frame by copying ``n_bytes`` directly from a ctypes source pointer.

        This is the single-copy fast path used by the camera SDK callback: it memmoves
        straight from the SDK DMA buffer into the ring slab, avoiding the intermediate
        Python ``bytes`` object that ``write_frame`` requires. Bytes beyond
        ``max_frame_bytes`` are truncated (identical to ``write_frame``).
        """
        n = min(int(n_bytes), self._max_frame_bytes)

        with self._lock:
            if self._available_frames >= self._buffer_size:
                if not self._overwrite_when_full:
                    self._log.warning(
                        f"Buffer full (available={self._available_frames}), frame {frame_id} dropped"
                    )
                    return False
                self._read_index = (self._read_index + 1) % self._buffer_size
                self._available_frames -= 1

            slot = self._write_index
            dst = self._buffer[slot]  # contiguous (max_frame_bytes,) uint8 row
            ctypes.memmove(dst.ctypes.data, src_ptr, n)
            if n < self._max_frame_bytes:
                dst[n:] = 0
            self._byte_lengths[slot] = n
            self._frame_ids[slot] = frame_id
            self._timestamps[slot] = timestamp
            self._metadata[slot] = dict(metadata) if metadata else {}

            self._write_index = (self._write_index + 1) % self._buffer_size
            self._frame_count += 1
            self._available_frames += 1

            return True

    def read_frame(self) -> Optional[Tuple[bytes, int, float, Dict[str, Any]]]:
        """Read oldest frame: (bytes, frame_id, timestamp, metadata)."""
        with self._lock:
            if self._available_frames == 0:
                return None

            slot = self._read_index
            n = int(self._byte_lengths[slot])
            frame_bytes = bytes(self._buffer[slot, :n])
            frame_id = int(self._frame_ids[slot])
            timestamp = float(self._timestamps[slot])
            metadata = dict(self._metadata[slot])
            metadata["frame_byte_length"] = n

            self._read_index = (self._read_index + 1) % self._buffer_size
            self._available_frames -= 1

            return (frame_bytes, frame_id, timestamp, metadata)

    def get_buffer_status(self) -> Dict[str, int]:
        with self._lock:
            fill_percent = int(
                (float(self._available_frames) / float(self._buffer_size)) * 100
            )
            return {
                "available_frames": self._available_frames,
                "total_frames": self._frame_count,
                "buffer_size": self._buffer_size,
                "fill_percent": fill_percent,
            }

    def clear(self) -> None:
        with self._lock:
            self._write_index = 0
            self._read_index = 0
            self._frame_count = 0
            self._available_frames = 0
            self._log.info("Buffer cleared")

    def get_memory_usage_mb(self) -> float:
        return self._buffer.nbytes / 1024**2

    @property
    def max_frame_bytes(self) -> int:
        return self._max_frame_bytes
