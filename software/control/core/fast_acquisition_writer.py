"""
Frame writer thread for fast acquisition.

This module provides a dedicated thread for writing frames from the ring buffer
to disk without blocking the camera acquisition thread.
"""

import os
import threading
import time
from typing import Optional, Dict
import numpy as np
import squid.logging

from control.core.fast_acquisition_buffer import FastAcquisitionFrameBuffer


class FastAcquisitionWriter(threading.Thread):
    """
    Thread that continuously reads from buffer and writes frames to disk.
    
    This thread runs in the background and writes frames as fast as possible
    to prevent buffer overflow. It supports multiple file formats including
    TIFF, Zarr, and HDF5.
    """
    
    def __init__(self, frame_buffer: FastAcquisitionFrameBuffer,
                 output_path: str, file_format: str = "tiff",
                 frames_per_file: int = 1000):
        """
        Initialize the frame writer thread.
        
        Args:
            frame_buffer: Ring buffer containing frames
            output_path: Base directory for saving frames
            file_format: "tiff", "zarr", or "hdf5" for large datasets
            frames_per_file: Number of frames per file (for TIFF format)
        """
        super().__init__(daemon=True)
        self._log = squid.logging.get_logger(self.__class__.__name__)
        
        self._frame_buffer = frame_buffer
        self._output_path = output_path
        self._file_format = file_format.lower()
        self._frame_timestamps_ms = []
        self._frames_per_file = frames_per_file
        
        # Thread control
        self._stop_event = threading.Event()
        self._running = False
        
        # Statistics
        self._frames_written = 0
        self._start_time = None
        self._last_write_time = None
        self._write_times = []
        self._stats_lock = threading.Lock()
        
        # Create output directory
        os.makedirs(output_path, exist_ok=True)
        self._frames_dir = os.path.join(output_path, "frames")
        os.makedirs(self._frames_dir, exist_ok=True)

        # For TIFF mode, we now write a single raw bytestream that will be
        # converted to a 3D TIFF stack after acquisition completes.
        self._raw_file = None
        self._raw_file_path: Optional[str] = None
        if self._file_format == "tiff":
            self._raw_file_path = os.path.join(self._frames_dir, "frames.raw")
        
        # File format specific initialization
        if self._file_format == "zarr":
            try:
                import zarr
                self._zarr_group = None
                self._zarr_dataset = None
            except ImportError:
                self._log.warning("zarr not available, falling back to TIFF")
                self._file_format = "tiff"
        elif self._file_format == "hdf5":
            try:
                import h5py
                self._h5_file = None
                self._h5_dataset = None
            except ImportError:
                self._log.warning("h5py not available, falling back to TIFF")
                self._file_format = "tiff"
        
        self._log.info(
            f"Initialized writer: format={self._file_format}, "
            f"output={output_path}"
        )
    
    def run(self):
        """Main loop: read from buffer, write to disk."""
        self._running = True
        self._start_time = time.time()
        self._log.info("Frame writer thread started")

        # Initialize file format specific resources
        if self._file_format == "zarr":
            self._init_zarr()
        elif self._file_format == "hdf5":
            self._init_hdf5()
        elif self._file_format == "tiff":
            # Open raw bytestream file once; frames will be appended sequentially
            try:
                if self._raw_file_path is None:
                    raise RuntimeError("Raw file path not initialized for TIFF writer")
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
                    # Buffer empty, wait a bit
                    time.sleep(0.001)  # 1ms
                    continue
                
                frame, frame_id, timestamp = frame_data
                
                # Write frame
                write_start = time.time()
                success = self._write_frame(frame, frame_id, timestamp)
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
        
        except Exception as e:
            self._log.error(f"Error in writer thread: {e}", exc_info=True)
        finally:
            # Cleanup
            if self._file_format == "zarr":
                self._close_zarr()
            elif self._file_format == "hdf5":
                self._close_hdf5()
            elif self._file_format == "tiff":
                # Close raw file if open
                try:
                    if self._raw_file is not None:
                        self._raw_file.close()
                        self._log.info(f"Closed raw frame file {self._raw_file_path}")
                except Exception as e:
                    self._log.warning(f"Error closing raw frame file: {e}", exc_info=True)
            
            self._running = False
            self._frame_timestamps_ms = np.array(self._frame_timestamps_ms)
            np.save(os.path.join(self._output_path, "frame_timestamps_ms.npy"), self._frame_timestamps_ms)
            self._log.info("Frame writer thread stopped")
    
    def stop(self):
        """Gracefully stop writer thread."""
        self._log.info("Stopping frame writer thread...")
        self._stop_event.set()
        # Wait for thread to finish (with timeout)
        self.join(timeout=5.0)
        if self.is_alive():
            self._log.warning("Writer thread did not stop within timeout")
    
    def _write_frame(self, frame: np.ndarray, frame_id: int, 
                    timestamp: float) -> bool:
        """Write a single frame to disk."""
        try:
            self._frame_timestamps_ms.append(timestamp)
            if self._file_format == "tiff":
                # In TIFF mode we now stream raw bytes; 3D stack is created later
                return self._write_raw_frame(frame, frame_id, timestamp)
            elif self._file_format == "zarr":
                return self._write_zarr(frame, frame_id, timestamp)
            elif self._file_format == "hdf5":
                return self._write_hdf5(frame, frame_id, timestamp)
            else:
                self._log.error(f"Unknown file format: {self._file_format}")
                return False
        except Exception as e:
            self._log.error(f"Error writing frame {frame_id}: {e}")
            return False
    
    def _write_raw_frame(self, frame: np.ndarray, frame_id: int,
                         timestamp: float) -> bool:
        """
        Append a single frame to the raw bytestream file.

        The raw file is later converted into a 3D TIFF stack once acquisition
        has completed, using the known frame shape, dtype, and frame count.
        """
        try:
            if self._raw_file is None:
                raise RuntimeError("Raw file handle is not open for TIFF writer")

            # Ensure contiguous C-order bytes for predictable layout
            frame_bytes = np.ascontiguousarray(frame).tobytes()
            self._raw_file.write(frame_bytes)
            return True
        except Exception as e:
            self._log.error(f"Error writing raw frame {frame_id}: {e}", exc_info=True)
            return False
    
    def _init_zarr(self):
        """Initialize Zarr storage."""
        try:
            import zarr
            
            zarr_path = os.path.join(self._output_path, "frames.zarr")
            # Zarr will be initialized when we know the total number of frames
            # For now, we'll use a growing array approach
            self._zarr_group = zarr.open(zarr_path, mode='w')
            self._zarr_dataset = None  # Will be created on first write
            self._zarr_write_index = 0
            
        except Exception as e:
            self._log.error(f"Error initializing Zarr: {e}")
            raise
    
    def _write_zarr(self, frame: np.ndarray, frame_id: int, 
                   timestamp: float) -> bool:
        """Write frame to Zarr array."""
        try:
            import zarr
            
            if self._zarr_dataset is None:
                # Create dataset on first write
                shape = (0, *frame.shape)
                chunks = (100, *frame.shape)
                self._zarr_dataset = self._zarr_group.create_dataset(
                    'frames',
                    shape=shape,
                    chunks=chunks,
                    dtype=frame.dtype,
                    compressor=zarr.Blosc(cname='lz4', clevel=5)
                )
                # Create metadata arrays
                self._zarr_frame_ids = self._zarr_group.create_dataset(
                    'frame_ids',
                    shape=(0,),
                    chunks=(1000,),
                    dtype=np.int64
                )
                self._zarr_timestamps = self._zarr_group.create_dataset(
                    'timestamps',
                    shape=(0,),
                    chunks=(1000,),
                    dtype=np.float64
                )
            
            # Append frame
            self._zarr_dataset.append(frame[np.newaxis, ...])
            self._zarr_frame_ids.append(np.array([frame_id]))
            self._zarr_timestamps.append(np.array([timestamp]))
            
            return True
        except Exception as e:
            self._log.error(f"Error writing Zarr: {e}")
            return False
    
    def _close_zarr(self):
        """Close Zarr storage."""
        if self._zarr_group is not None:
            self._zarr_group.close()
    
    def _init_hdf5(self):
        """Initialize HDF5 storage."""
        try:
            import h5py
            
            h5_path = os.path.join(self._output_path, "frames.h5")
            self._h5_file = h5py.File(h5_path, 'w')
            self._h5_dataset = None  # Will be created on first write
            self._h5_write_index = 0
            
        except Exception as e:
            self._log.error(f"Error initializing HDF5: {e}")
            raise
    
    def _write_hdf5(self, frame: np.ndarray, frame_id: int, 
                   timestamp: float) -> bool:
        """Write frame to HDF5 file."""
        try:
            import h5py
            
            if self._h5_dataset is None:
                # Create dataset on first write
                shape = (0, *frame.shape)
                maxshape = (None, *frame.shape)
                self._h5_dataset = self._h5_file.create_dataset(
                    'frames',
                    shape=shape,
                    maxshape=maxshape,
                    dtype=frame.dtype,
                    chunks=(100, *frame.shape),
                    compression='gzip',
                    compression_opts=4
                )
                # Create metadata datasets
                self._h5_frame_ids = self._h5_file.create_dataset(
                    'frame_ids',
                    shape=(0,),
                    maxshape=(None,),
                    dtype=np.int64,
                    chunks=(1000,)
                )
                self._h5_timestamps = self._h5_file.create_dataset(
                    'timestamps',
                    shape=(0,),
                    maxshape=(None,),
                    dtype=np.float64,
                    chunks=(1000,)
                )
            
            # Resize and append
            current_size = self._h5_dataset.shape[0]
            self._h5_dataset.resize((current_size + 1, *frame.shape))
            self._h5_dataset[current_size] = frame
            
            self._h5_frame_ids.resize((current_size + 1,))
            self._h5_frame_ids[current_size] = frame_id
            
            self._h5_timestamps.resize((current_size + 1,))
            self._h5_timestamps[current_size] = timestamp
            
            return True
        except Exception as e:
            self._log.error(f"Error writing HDF5: {e}")
            return False
    
    def _close_hdf5(self):
        """Close HDF5 file."""
        if self._h5_file is not None:
            self._h5_file.close()
    
    def get_write_statistics(self) -> Dict[str, float]:
        """
        Get write statistics.
        
        Returns:
            Dictionary with statistics:
            - frames_written: Total frames written
            - write_rate: Frames per second
            - avg_write_time: Average write time per frame (ms)
            - max_write_time: Maximum write time (ms)
        """
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
                avg_write_time = np.mean(self._write_times) * 1000  # ms
                max_write_time = np.max(self._write_times) * 1000  # ms
            else:
                avg_write_time = 0.0
                max_write_time = 0.0
            
            return {
                "frames_written": self._frames_written,
                "write_rate": write_rate,
                "avg_write_time": avg_write_time,
                "max_write_time": max_write_time,
            }

