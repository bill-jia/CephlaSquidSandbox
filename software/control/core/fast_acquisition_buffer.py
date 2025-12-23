"""
Ring buffer for fast acquisition frame storage.

This module provides a high-performance ring buffer for storing camera frames
in memory during fast acquisition. The buffer allows non-blocking writes from
the camera thread and non-blocking reads from the writer thread.
"""

import threading
from typing import Optional, Tuple, Dict
import numpy as np
import squid.logging


class FastAcquisitionFrameBuffer:
    """
    Ring buffer for storing frames in memory during fast acquisition.
    
    This buffer allows the camera thread to write frames without blocking,
    while the writer thread can read frames asynchronously. When the buffer
    is full, new frames will overwrite the oldest frames (or return False
    if overwrite is disabled).
    
    Thread-safe operations using RLock for concurrent access.
    """
    
    def __init__(self, buffer_size: int, frame_shape: Tuple[int, int], 
                 dtype: np.dtype, overwrite_when_full: bool = True):
        """
        Initialize the ring buffer.
        
        Args:
            buffer_size: Number of frames to buffer (e.g., 100-1000)
            frame_shape: (height, width) of frames
            dtype: NumPy dtype (e.g., np.uint16)
            overwrite_when_full: If True, overwrite oldest frames when full.
                                If False, return False when full.
        """
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self._buffer_size = buffer_size
        self._frame_shape = frame_shape
        self._dtype = dtype
        self._overwrite_when_full = overwrite_when_full
        
        # Pre-allocate buffer
        self._buffer = np.zeros((buffer_size, *frame_shape), dtype=dtype)
        self._frame_ids = np.zeros(buffer_size, dtype=np.int64)
        self._timestamps = np.zeros(buffer_size, dtype=np.float64)
        
        # Buffer state
        self._write_index = 0
        self._read_index = 0
        self._frame_count = 0  # Total frames written
        self._available_frames = 0  # Frames available to read
        self._lock = threading.RLock()
        
        self._log.info(
            f"Initialized frame buffer: size={buffer_size}, shape={frame_shape}, "
            f"dtype={dtype}, memory={self._buffer.nbytes / 1024**2:.1f} MB"
        )
    
    def write_frame(self, frame: np.ndarray, frame_id: int, 
                   timestamp: float) -> bool:
        """
        Write frame to buffer.
        
        Args:
            frame: Frame data as numpy array
            frame_id: Unique frame identifier
            timestamp: Frame timestamp (seconds since epoch)
            
        Returns:
            True if frame was written successfully, False if buffer was full
            and overwrite is disabled.
        """
        with self._lock:
            # Check if buffer is full
            if self._available_frames >= self._buffer_size:
                if not self._overwrite_when_full:
                    self._log.warning(
                        f"Buffer full (available={self._available_frames}), "
                        f"frame {frame_id} dropped"
                    )
                    return False
                else:
                    # Overwrite oldest frame
                    self._read_index = (self._read_index + 1) % self._buffer_size
                    self._available_frames -= 1
                    self._log.debug(
                        f"Buffer full, overwriting frame at index {self._read_index}"
                    )
            
            # Write frame
            self._buffer[self._write_index] = frame
            self._frame_ids[self._write_index] = frame_id
            self._timestamps[self._write_index] = timestamp
            
            # Update indices
            self._write_index = (self._write_index + 1) % self._buffer_size
            self._frame_count += 1
            self._available_frames += 1
            
            return True
    
    def read_frame(self) -> Optional[Tuple[np.ndarray, int, float]]:
        """
        Read oldest frame from buffer.
        
        Returns:
            Tuple of (frame, frame_id, timestamp) if available, None if buffer is empty.
            The frame is a copy to avoid issues with concurrent access.
        """
        with self._lock:
            if self._available_frames == 0:
                return None
            
            # Read frame
            frame = self._buffer[self._read_index].copy()
            frame_id = int(self._frame_ids[self._read_index])
            timestamp = float(self._timestamps[self._read_index])
            
            # Update indices
            self._read_index = (self._read_index + 1) % self._buffer_size
            self._available_frames -= 1
            
            return (frame, frame_id, timestamp)
    
    def get_buffer_status(self) -> Dict[str, int]:
        """
        Get current buffer status.
        
        Returns:
            Dictionary with buffer statistics:
            - available_frames: Number of frames available to read
            - total_frames: Total frames written since initialization
            - buffer_size: Maximum buffer capacity
            - fill_percent: Percentage of buffer filled
        """
        with self._lock:
            fill_percent = int((float(self._available_frames) / float(self._buffer_size)) * 100)
            return {
                "available_frames": self._available_frames,
                "total_frames": self._frame_count,
                "buffer_size": self._buffer_size,
                "fill_percent": fill_percent,
            }
    
    def clear(self):
        """Clear the buffer (reset to empty state)."""
        with self._lock:
            self._write_index = 0
            self._read_index = 0
            self._frame_count = 0
            self._available_frames = 0
            self._log.info("Buffer cleared")
    
    def get_memory_usage_mb(self) -> float:
        """Get memory usage of buffer in MB."""
        return self._buffer.nbytes / 1024**2

