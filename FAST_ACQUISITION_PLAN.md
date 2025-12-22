# Fast Acquisition Mode Implementation Plan

## Overview
This document outlines the plan for implementing a fast acquisition mode that handles high-speed imaging with large file sizes, hardware triggers, and synchronized NI DAQ waveform recording.

## Architecture Components

### 1. Hardware Trigger System

#### 1.1 Trigger Source Abstraction
**Location**: `software/control/core/fast_acquisition_trigger.py` (new file)

Create a trigger source abstraction layer that supports:
- **TI Microcontroller triggers**: Via existing `Microcontroller.send_hardware_trigger()`
- **NI DAQ triggers**: Via NI DAQ digital output lines
- **Per-frame triggers**: Each frame individually triggered
- **Start acquisition trigger**: Single trigger to start continuous acquisition

**Key Classes**:
```python
class TriggerSource(Enum):
    TI_MICROCONTROLLER = "ti_microcontroller"
    NI_DAQ = "ni_daq"
    
class FastAcquisitionTriggerController:
    def __init__(self, trigger_source: TriggerSource, 
                 microcontroller: Optional[Microcontroller] = None,
                 ni_daq: Optional[AbstractNIDAQ] = None):
        ...
    
    def send_start_trigger(self) -> bool:
        """Send trigger to start acquisition sequence"""
        
    def send_frame_trigger(self, frame_number: int) -> bool:
        """Send trigger for individual frame"""
        
    def configure_trigger_timing(self, frame_rate_hz: float, 
                                exposure_time_ms: float):
        """Configure trigger timing parameters"""
```

#### 1.2 Integration Points
- **Microcontroller**: Extend existing `send_hardware_trigger()` to support fast acquisition mode
- **NI DAQ**: Add trigger generation capability to `NIDAQ` class
- **Camera**: Extend `FLIRCamera` to support both trigger modes:
  - `HARDWARE_TRIGGER`: Per-frame hardware trigger (existing)
  - `HARDWARE_TRIGGER_FIRST`: Start trigger, then continuous (existing)
  - `FAST_ACQUISITION`: New mode for high-speed with external trigger control

### 2. Frame Buffer Management

#### 2.1 Ring Buffer Implementation
**Location**: `software/control/core/fast_acquisition_buffer.py` (new file)

Implement a high-performance ring buffer for frame storage:

```python
class FastAcquisitionFrameBuffer:
    def __init__(self, buffer_size: int, frame_shape: Tuple[int, int], 
                 dtype: np.dtype):
        """
        Ring buffer for storing frames in memory.
        
        Args:
            buffer_size: Number of frames to buffer (e.g., 100-1000)
            frame_shape: (height, width) of frames
            dtype: NumPy dtype (e.g., np.uint16)
        """
        self._buffer = np.zeros((buffer_size, *frame_shape), dtype=dtype)
        self._write_index = 0
        self._read_index = 0
        self._frame_count = 0
        self._lock = threading.RLock()
        
    def write_frame(self, frame: np.ndarray, frame_id: int, 
                   timestamp: float) -> bool:
        """Write frame to buffer. Returns False if buffer full."""
        
    def read_frame(self) -> Optional[Tuple[np.ndarray, int, float]]:
        """Read oldest frame from buffer. Returns None if empty."""
        
    def get_buffer_status(self) -> Dict[str, int]:
        """Return buffer fill level, available space, etc."""
```

#### 2.2 Frame Writer Thread
**Location**: `software/control/core/fast_acquisition_writer.py` (new file)

Dedicated thread for writing frames to disk without blocking acquisition:

```python
class FastAcquisitionWriter(threading.Thread):
    def __init__(self, frame_buffer: FastAcquisitionFrameBuffer,
                 output_path: str, file_format: str = "tiff"):
        """
        Thread that continuously reads from buffer and writes to disk.
        
        Args:
            frame_buffer: Ring buffer containing frames
            output_path: Base directory for saving frames
            file_format: "tiff", "zarr", or "hdf5" for large datasets
        """
        
    def run(self):
        """Main loop: read from buffer, write to disk"""
        
    def stop(self):
        """Gracefully stop writer thread"""
        
    def get_write_statistics(self) -> Dict[str, float]:
        """Return write speed, queue depth, etc."""
```

#### 2.3 File Format Options
- **TIFF**: For smaller datasets (< 10GB), individual files
- **Zarr**: For large datasets, chunked storage, parallel I/O
- **HDF5**: Alternative for large datasets with metadata support

### 3. Camera Frame Acquisition

#### 3.1 Enhanced FLIR Camera Read Thread
**Location**: `software/control/camera_flir.py` (modify existing)

Modify `_wait_for_frame()` to support fast acquisition mode:

```python
def _wait_for_frame(self):
    """Enhanced frame reading with fast acquisition support"""
    while self._read_thread_keep_running.is_set():
        # Check acquisition mode
        if self._acquisition_mode == CameraAcquisitionMode.FAST_ACQUISITION:
            # Use non-blocking GetNextImage with minimal timeout
            raw_image = self._camera.GetNextImage(1)  # 1ms timeout
        else:
            # Existing blocking behavior
            raw_image = self._camera.GetNextImage(int(np.round(self._exposure_time_ms*1.1)))
        
        # Process frame...
        
        # For fast acquisition, write directly to buffer instead of callbacks
        if self._acquisition_mode == CameraAcquisitionMode.FAST_ACQUISITION:
            if self._fast_acquisition_buffer:
                self._fast_acquisition_buffer.write_frame(
                    processed_frame, frame_id, timestamp
                )
        else:
            # Existing callback propagation
            self._propogate_frame(camera_frame)
```

#### 3.2 Spinnaker API Optimization
- Use `GetNextImage()` with minimal timeout for non-blocking operation
- Configure camera buffer size: `TLStream.StreamBufferCount` (increase for fast acquisition)
- Set `TLStream.StreamBufferHandlingMode` to `NewestFirst` or `OldestFirst` based on requirements
- Disable unnecessary image processing during acquisition

#### 3.3 Camera Digital Output for Frame Synchronization
The camera is already configured to output a frame synchronization signal:
- **Line2** is configured as output with `LineSource = "ExposureActive"` (see `camera_flir.py` line 688)
- This signal goes HIGH when the camera is actively exposing
- Connect this camera output to NI DAQ digital input (default: DIO 0)
- The DAQ will sample this signal at high rate to detect frame boundaries
- Rising edge typically indicates frame start, falling edge indicates frame end

**Note**: If a different signal is needed (e.g., frame trigger pulse instead of exposure active), 
the camera configuration can be modified to use a different LineSource option.

### 4. NI DAQ Integration

#### 4.1 Camera Digital Output Configuration
**Location**: `software/control/camera_flir.py` (modify existing)

Configure camera to output frame synchronization signal on a digital line:
- **Exposure Active signal**: Camera's Line2 already configured as output with `LineSource = "ExposureActive"` (see line 688 in camera_flir.py)
- This signal goes HIGH during camera exposure and can be connected to NI DAQ digital input
- Alternative: Configure a different line for frame trigger output if needed

**Camera Configuration**:
```python
# In FLIRCamera.open() or set_fast_acquisition_mode():
# Line2 is already configured as output with ExposureActive source
# This provides hardware signal indicating when camera is exposing
# Connect this to NI DAQ DIO 0 (or configurable digital input line)
```

#### 4.2 Synchronized Waveform Recording
**Location**: `software/control/core/fast_acquisition_daq.py` (new file)

Coordinate DAQ acquisition with camera frames using digital input signal:

```python
class FastAcquisitionDAQController:
    def __init__(self, ni_daq: AbstractNIDAQ, camera: AbstractCamera):
        """
        Controller for synchronizing NI DAQ with camera acquisition.
        
        Uses digital input from camera (default: DIO 0) to detect frame boundaries.
        The camera outputs either:
        - ExposureActive signal: HIGH during exposure
        - FrameTrigger signal: Pulse on each frame start
        
        Records:
        - Analog input waveforms (e.g., sensor readings, voltages)
        - Digital input patterns (e.g., trigger signals, status bits)
        - Digital input from camera (frame synchronization signal)
        - Analog output waveforms (e.g., control signals)
        """
        self._camera_dio_line = 0  # Default: DIO 0
        self._frame_edge_detection = "rising"  # or "falling", "both"
        self._frame_sample_indices = []  # List of DAQ sample indices where frames occur
        
    def configure_acquisition(self, sample_rate_hz: float,
                            num_frames: int,
                            camera_dio_line: int = 0,
                            frame_edge: str = "rising"):
        """
        Configure DAQ for synchronized acquisition.
        
        Args:
            sample_rate_hz: DAQ sample rate
            num_frames: Expected number of frames
            camera_dio_line: Digital input line connected to camera (default: 0)
            frame_edge: Edge to detect frames ("rising", "falling", or "both")
        """
        
    def start_synchronized_acquisition(self):
        """Start DAQ recording synchronized with camera"""
        
    def detect_frame_edges(self, digital_input_data: np.ndarray) -> List[int]:
        """
        Detect frame edges in digital input signal.
        
        Args:
            digital_input_data: 1D array of digital input samples (0 or 1)
            
        Returns:
            List of sample indices where frame edges detected
        """
        # Detect rising/falling edges based on configuration
        # Return sample indices where frames occur
        
    def get_waveform_data(self, frame_number: int) -> Dict[str, np.ndarray]:
        """
        Get waveform data corresponding to a specific frame.
        
        Uses frame_sample_indices to extract the correct DAQ samples
        for each frame.
        """
        if frame_number >= len(self._frame_sample_indices):
            return {}
        
        frame_sample_idx = self._frame_sample_indices[frame_number]
        # Extract waveform data around this sample index
        # Return analog inputs, digital inputs, etc.
        
    def save_waveform_data(self, output_path: str):
        """
        Save all waveform data to file (HDF5 or NumPy format).
        
        Includes:
        - Analog input waveforms
        - Digital input waveforms
        - Frame synchronization signal
        - Frame sample index mapping (frame_number -> DAQ_sample_index)
        """
```

#### 4.3 Frame-to-Waveform Synchronization
**Hardware-based synchronization using digital input**:

1. **Camera Digital Output**: 
   - Camera outputs frame synchronization signal (e.g., ExposureActive on Line2)
   - This signal is connected to NI DAQ digital input (default: DIO 0)

2. **Frame Detection**:
   - DAQ continuously samples the digital input at high rate (e.g., 10 kHz)
   - Detect rising/falling edges in the digital signal
   - Each edge corresponds to a frame boundary
   - Store DAQ sample index for each detected frame edge

3. **Waveform Extraction**:
   - For each camera frame, use the corresponding DAQ sample index
   - Extract analog/digital waveforms around that sample index
   - Handle timing: extract samples before/after frame edge as needed

4. **Advantages**:
   - Hardware synchronization (no software timing issues)
   - No clock drift between camera and DAQ
   - Sub-sample accuracy (can interpolate if needed)
   - Works regardless of frame rate variations

**Implementation Details**:
```python
# In detect_frame_edges():
def detect_frame_edges(self, digital_input_data: np.ndarray) -> List[int]:
    """Detect edges in camera digital input signal"""
    if self._frame_edge_detection == "rising":
        # Find rising edges (0 -> 1 transitions)
        edges = np.where(np.diff(digital_input_data) > 0)[0]
    elif self._frame_edge_detection == "falling":
        # Find falling edges (1 -> 0 transitions)
        edges = np.where(np.diff(digital_input_data) < 0)[0]
    else:  # "both"
        # Find both rising and falling edges
        edges = np.where(np.abs(np.diff(digital_input_data)) > 0)[0]
    
    return edges.tolist()

# Store frame sample indices during acquisition
self._frame_sample_indices = self.detect_frame_edges(digital_input_channel)
```

### 5. User Interface

#### 5.1 Fast Acquisition Tab Widget
**Location**: `software/control/widgets.py` (add new class)

Create a new tab in the GUI for fast acquisition control:

```python
class FastAcquisitionWidget(QWidget):
    """
    Widget for controlling fast acquisition mode.
    
    Features:
    - Trigger source selection (TI Microcontroller / NI DAQ)
    - Frame rate and exposure time settings
    - Buffer size configuration
    - File format selection (TIFF / Zarr / HDF5)
    - Output directory selection
    - Start/Stop acquisition controls
    - Real-time statistics (FPS, buffer fill, write speed)
    - DAQ channel configuration
    """
    
    signal_acquisition_started = Signal()
    signal_acquisition_finished = Signal()
    
    def __init__(self, microscope: Microscope, 
                 ni_daq_widget: Optional[NIDAQWidget] = None):
        ...
        
    def init_ui(self):
        """Initialize UI components"""
        # Trigger source selection
        self.trigger_source_combo = QComboBox()
        self.trigger_source_combo.addItems(["TI Microcontroller", "NI DAQ"])
        
        # Acquisition parameters
        self.frame_rate_spinbox = QDoubleSpinBox()
        self.exposure_time_spinbox = QDoubleSpinBox()
        self.num_frames_spinbox = QSpinBox()
        
        # Buffer settings
        self.buffer_size_spinbox = QSpinBox()
        self.file_format_combo = QComboBox()
        self.file_format_combo.addItems(["TIFF", "Zarr", "HDF5"])
        
        # Output directory
        self.output_dir_button = QPushButton("Select Output Directory")
        
        # DAQ configuration
        self.enable_daq_checkbox = QCheckBox("Record DAQ waveforms")
        self.daq_channels_widget = ...  # Channel selection UI
        
        # Camera digital input configuration
        self.camera_dio_line_spinbox = QSpinBox()
        self.camera_dio_line_spinbox.setRange(0, 31)
        self.camera_dio_line_spinbox.setValue(0)  # Default: DIO 0
        self.camera_dio_line_spinbox.setToolTip("NI DAQ digital input line connected to camera frame signal")
        
        self.frame_edge_combo = QComboBox()
        self.frame_edge_combo.addItems(["Rising Edge", "Falling Edge", "Both Edges"])
        self.frame_edge_combo.setToolTip("Edge type to detect frames in camera digital signal")
        
        # Control buttons
        self.start_button = QPushButton("Start Acquisition")
        self.stop_button = QPushButton("Stop Acquisition")
        
        # Statistics display
        self.stats_label = QLabel()
        self.buffer_progress_bar = QProgressBar()
        
    def start_acquisition(self):
        """Start fast acquisition"""
        
    def stop_acquisition(self):
        """Stop fast acquisition"""
        
    def update_statistics(self):
        """Update real-time statistics display"""
```

#### 5.2 Integration with Main GUI
**Location**: `software/control/gui_hcs.py` (modify existing)

Add fast acquisition tab to the record tab widget:

```python
# In load_widgets():
if ENABLE_FAST_ACQUISITION:
    self.fastAcquisitionWidget = widgets.FastAcquisitionWidget(
        self.microscope, 
        ni_daq_widget=self.niDAQWidget if ENABLE_NI_DAQ else None
    )

# In setupRecordTabWidget():
if ENABLE_FAST_ACQUISITION:
    self.recordTabWidget.addTab(
        self.fastAcquisitionWidget, 
        "Fast Acquisition"
    )
```

### 6. Data Management

#### 6.1 Metadata Storage
Store acquisition metadata alongside images:
- Frame timestamps (from camera)
- Trigger timestamps
- DAQ sample indices (from digital input edge detection)
- Frame number → DAQ sample index mapping
- Camera settings (exposure, gain, ROI)
- Trigger source and timing parameters
- Digital input signal configuration (DIO line, edge type)

**Format**: JSON or HDF5 attributes

**Frame-to-DAQ Mapping**:
```python
# Store as NumPy array or in HDF5 dataset
frame_to_daq_mapping = {
    "frame_numbers": np.array([0, 1, 2, ...]),
    "daq_sample_indices": np.array([100, 5000, 9900, ...]),  # Sample indices where frames detected
    "daq_sample_rate_hz": 10000.0,
    "camera_dio_line": 0,
    "frame_edge_type": "rising"
}
```

#### 6.2 File Organization
```
output_directory/
├── metadata.json
├── frames/
│   ├── frame_000000.tiff
│   ├── frame_000001.tiff
│   └── ...
├── waveforms/
│   ├── analog_input.h5
│   ├── digital_input.h5
│   ├── camera_frame_signal.h5  # Digital input from camera (DIO 0)
│   └── frame_sync_map.npy  # Frame number -> DAQ sample index mapping
└── acquisition_log.txt
```

### 7. Performance Optimizations

#### 7.1 Memory Management
- Pre-allocate frame buffer to avoid allocation overhead
- Use memory-mapped files for large datasets (Zarr/HDF5)
- Monitor memory usage and warn if approaching limits

#### 7.2 I/O Optimization
- Use multiple writer threads for parallel file I/O (if using Zarr)
- Batch frame writes when possible
- Use SSD storage for output directory
- Disable antivirus scanning on output directory

#### 7.3 Threading Architecture
```
Main Thread (GUI)
    │
    ├── Camera Read Thread (pulls frames from Spinnaker)
    │       └── Writes to Ring Buffer
    │
    ├── Frame Writer Thread (reads from buffer, writes to disk)
    │
    ├── DAQ Acquisition Thread (records waveforms + digital input)
    │       └── Detects frame edges in digital input signal
    │       └── Builds frame_number -> DAQ_sample_index mapping
    │
    └── Statistics Update Thread (updates UI periodically)
```

### 8. Error Handling and Recovery

#### 8.1 Buffer Overflow Handling
- Monitor buffer fill level
- Warn user if buffer > 80% full
- Option to drop oldest frames or stop acquisition

#### 8.2 Disk Space Monitoring
- Check available disk space before starting
- Estimate required space based on frame count and size
- Warn if disk space insufficient

#### 8.3 Frame Loss Detection
- Track frame IDs from camera
- Detect missing frames (gaps in frame ID sequence)
- Log frame loss events

### 9. Testing Strategy

#### 9.1 Unit Tests
- Test ring buffer under various load conditions
- Test frame writer with different file formats
- Test trigger controller with both sources

#### 9.2 Integration Tests
- Test full acquisition pipeline with simulated camera
- Test DAQ synchronization accuracy
- Test buffer overflow scenarios

#### 9.3 Performance Tests
- Measure maximum sustainable frame rate
- Measure write throughput
- Test with different buffer sizes

## Implementation Phases

### Phase 1: Core Infrastructure (Week 1-2)
1. Implement ring buffer (`FastAcquisitionFrameBuffer`)
2. Implement frame writer thread (`FastAcquisitionWriter`)
3. Add fast acquisition mode to camera abstraction
4. Basic file I/O (TIFF format)

### Phase 2: Trigger System (Week 2-3)
1. Implement trigger source abstraction
2. Integrate with TI microcontroller
3. Integrate with NI DAQ
4. Add trigger timing configuration

### Phase 3: DAQ Integration (Week 3-4)
1. Implement DAQ controller with digital input support
2. Add digital input edge detection for frame synchronization
3. Implement frame sample index mapping
4. Implement waveform data storage with frame alignment

### Phase 4: User Interface (Week 4-5)
1. Create fast acquisition widget
2. Add to main GUI
3. Implement real-time statistics
4. Add configuration persistence

### Phase 5: Optimization & Testing (Week 5-6)
1. Performance optimization
2. Error handling improvements
3. Comprehensive testing
4. Documentation

## Configuration Options

Add to configuration file:
```ini
[FAST_ACQUISITION]
ENABLED = True
DEFAULT_TRIGGER_SOURCE = ti_microcontroller  # or ni_daq
DEFAULT_BUFFER_SIZE = 500
DEFAULT_FILE_FORMAT = tiff  # or zarr, hdf5
DEFAULT_OUTPUT_DIRECTORY = ./fast_acquisition_data
MAX_BUFFER_SIZE = 2000
WARN_BUFFER_FILL_PERCENT = 80
ENABLE_DAQ_RECORDING = True
DAQ_SAMPLE_RATE_HZ = 10000
DAQ_SAMPLES_PER_FRAME = 100
CAMERA_DIO_LINE = 0  # NI DAQ digital input line for camera frame signal
FRAME_EDGE_DETECTION = rising  # rising, falling, or both
```

## Dependencies

New dependencies:
- `zarr` (for Zarr file format support)
- `h5py` (for HDF5 file format support)
- `nidaqmx` (already used for NI DAQ)

## Notes

1. **Spinnaker API Considerations**:
   - Use `GetNextImage()` with timeout for non-blocking operation
   - Increase camera buffer count to prevent frame drops
   - Consider using `GetNextImage(0)` for immediate return (may drop frames if not ready)

2. **Memory Considerations**:
   - For 2048x2048 uint16 frames: ~8 MB per frame
   - Buffer of 500 frames: ~4 GB RAM
   - Consider allowing user to configure buffer size based on available RAM

3. **File Size Considerations**:
   - 1000 frames at 2048x2048 uint16: ~8 GB (TIFF)
   - Zarr format can reduce size with compression
   - Consider chunking for very large datasets

4. **Trigger Timing**:
   - Ensure trigger signals have sufficient setup/hold times
   - Account for camera processing latency
   - Use hardware timestamps when available

5. **Camera Digital Output**:
   - Camera Line2 is already configured as output with ExposureActive source
   - Connect this to NI DAQ digital input (default: DIO 0)
   - The ExposureActive signal goes HIGH during exposure
   - Rising edge detection is typically used to detect frame start
   - Ensure proper signal levels (camera output voltage matches DAQ input requirements)

6. **Frame Synchronization**:
   - Digital input sampling rate should be high enough to capture frame edges accurately
   - For 100 Hz frame rate, 10 kHz DAQ sample rate provides 100 samples per frame
   - Higher sample rates provide better temporal resolution for waveform alignment
   - Edge detection is done post-acquisition by analyzing the digital input waveform

