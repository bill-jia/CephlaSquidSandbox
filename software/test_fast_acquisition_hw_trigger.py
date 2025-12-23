"""
Minimal test script for camera frame collection with NI DAQ hardware triggering.

This script tests the basic flow:
1. Configure camera for hardware triggering
2. Configure NI DAQ to generate trigger pulses on DIO 1
3. Start camera acquisition
4. Arm and fire DAQ waveforms
5. Attempt to read frames from camera

Run this script to debug frame collection issues.
"""

import time
import numpy as np
import PySpin
from pathlib import Path
import sys
from typing import Optional

from configparser import ConfigParser
import glob

# Add the software directory to path
sys.path.insert(0, str(Path(__file__).parent))

from control.ni_daq import NIDAQ, NIDAQConfig, WaveformData, TriggerSource, generate_pulse_train
from control.camera_flir import FLIRCamera
from squid.config import CameraConfig, CameraVariant
from squid.abc import CameraAcquisitionMode, CameraPixelFormat
import squid.camera.utils
from control._def import CACHED_CONFIG_FILE_PATH
from control._def import USE_TERMINAL_CONSOLE
from control.camera_flir import get_enumeration_node_and_current_entry

def print_step(step_num, description):
    """Print a clearly marked step."""
    print(f"\n{'='*60}")
    print(f"STEP {step_num}: {description}")
    print(f"{'='*60}")

def test_hw_trigger_frame_collection():
    """Test camera frame collection with NI DAQ hardware triggering."""
    
    print("\n" + "="*60)
    print("FAST ACQUISITION HARDWARE TRIGGER TEST")
    print("="*60)

    # Load configuration file
    legacy_config = False
    cf_editor_parser = ConfigParser()
    config_files = glob.glob("." + "/" + "configuration*.ini")
    if config_files:
        cf_editor_parser.read(CACHED_CONFIG_FILE_PATH)
    else:
        print("configuration*.ini file not found, defaulting to legacy configuration")
        legacy_config = True
    
    # ========================================================================
    # STEP 1: Initialize Camera
    # ========================================================================
    print_step(1, "Initialize Camera")
    
    try:
        def acquisition_camera_hw_trigger_fn(illumination_time: Optional[float]) -> bool:
            """
            Hardware trigger function called by camera to start acquisition.
            
            This function:
            - Sends hardware trigger signal to camera
            - Optionally controls illumination timing for synchronized exposure
            
            Args:
                illumination_time: Duration to keep illumination on (ms), or None for no illumination
                
            Returns:
                True if trigger was sent successfully
            """
            # NOTE(imo): If this succeeds, it means we sent the request,
            # but we didn't necessarily get confirmation of success.
            return True

        def acquisition_camera_hw_strobe_delay_fn(strobe_delay_ms: float) -> bool:
            """
            Set the strobe delay for hardware-triggered acquisition.
            
            Strobe delay is the time between trigger signal and illumination turn-on.
            This allows fine-tuning of illumination timing relative to camera exposure.
            
            Args:
                strobe_delay_ms: Delay in milliseconds
            """
            return True

    
        # Create camera instance
        camera = squid.camera.utils.get_camera(
            config=squid.config.get_camera_config(),
            simulated=False,
            hw_trigger_fn=acquisition_camera_hw_trigger_fn,
            hw_set_strobe_delay_ms_fn=acquisition_camera_hw_strobe_delay_fn,
        )
        print(camera)
        print(dir(camera))
        
        # Open camera
        print("Opening camera...")
        camera.open()
        print(f"Camera opened: {camera._config.serial_number}")
        
        # Check if camera is initialized
        if not camera._camera.IsInitialized():
            print("Initializing camera...")
            camera._camera.Init()
        print("✓ Camera initialized")
        
    except Exception as e:
        print(f"✗ Failed to initialize camera: {e}")
        return False
    
    # ========================================================================
    # STEP 2: Configure Camera for Hardware Triggering
    # ========================================================================
    print_step(2, "Configure Camera for Hardware Triggering")
    
    try:
        # Set acquisition mode to HARDWARE_TRIGGER
        print("Setting acquisition mode to HARDWARE_TRIGGER...")
        camera.set_acquisition_mode(CameraAcquisitionMode.HARDWARE_TRIGGER)
        # camera.set_acquisition_mode(CameraAcquisitionMode.CONTINUOUS)
        print(f"✓ Acquisition mode set: {camera.get_acquisition_mode().value}")
        camera.set_pixel_format(CameraPixelFormat.MONO16)
        # camera.set_region_of_interest(360,0,1200,100)
        # Set exposure time
        exposure_time_ms = 9.85
        sample_rate_hz = 10000.0
        frame_rate_hz = 100
        num_frames = 5000  # Test with N frames
        print(f"Setting exposure time to {exposure_time_ms} ms...")
        camera.set_exposure_time(exposure_time_ms)
        print(f"✓ Exposure time set: {camera.get_exposure_time()} ms")
        
        # Verify camera settings
        nodemap = camera._camera.GetNodeMap()
        
        # Check trigger mode
        name, entry = get_enumeration_node_and_current_entry(nodemap.GetNode("TriggerMode"))
        print(f"  TriggerMode: {entry}")
        
        # Check trigger source
        name, entry = get_enumeration_node_and_current_entry(nodemap.GetNode("TriggerSource"))
        print(f"  TriggerSource: {entry}")
        
        # Check trigger selector
        name, entry = get_enumeration_node_and_current_entry(nodemap.GetNode("TriggerSelector"))
        print(f"  TriggerSelector: {entry}")
        
        # Check acquisition mode
        name, entry = get_enumeration_node_and_current_entry(nodemap.GetNode("AcquisitionMode"))
        print(f"  AcquisitionMode: {entry}")
        
    except Exception as e:
        print(f"✗ Failed to configure camera: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # ========================================================================
    # STEP 3: Initialize NI DAQ
    # ========================================================================
    print_step(3, "Initialize NI DAQ")
    
    try:
        # DAQ parameters
        device_name = "Dev1"  # Change if needed
        duration_s = num_frames / frame_rate_hz
        samples_per_channel = int(sample_rate_hz * duration_s)
        trigger_dio_line = 1  # DIO 1 for camera trigger
        
        print(f"Device: {device_name}")
        print(f"Sample rate: {sample_rate_hz} Hz")
        print(f"Frame rate: {frame_rate_hz} Hz")
        print(f"Number of frames: {num_frames}")
        print(f"Duration: {duration_s} s")
        print(f"Samples per channel: {samples_per_channel}")
        print(f"Trigger DIO line: {trigger_dio_line}")
        
        # Create DAQ config
        daq_config = NIDAQConfig(
            device_name=device_name,
            sample_rate_hz=sample_rate_hz,
            samples_per_channel=samples_per_channel,
            do_port="port0",
            do_lines=[trigger_dio_line],
            trigger_source=TriggerSource.SOFTWARE
        )
        
        # Create DAQ instance
        print("Creating NI DAQ instance...")
        ni_daq = NIDAQ(daq_config)
        print("✓ NI DAQ created")
        
    except Exception as e:
        print(f"✗ Failed to initialize NI DAQ: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # ========================================================================
    # STEP 4: Generate Trigger Waveform
    # ========================================================================
    print_step(4, "Generate Trigger Waveform")
    
    try:
        # Generate pulse train for triggers
        frame_period_samples = int(sample_rate_hz / frame_rate_hz)
        pulse_width_samples = 4  # 4 samples wide
        n_samples_offset = 5  # Start after 5 samples
        
        print(f"Frame period: {frame_period_samples} samples")
        print(f"Pulse width: {pulse_width_samples} samples")
        print(f"Offset: {n_samples_offset} samples")
        
        trigger_pattern = generate_pulse_train(
            pulse_width_samples=pulse_width_samples,
            period_samples=frame_period_samples,
            num_samples=samples_per_channel,
            n_samples_offset=n_samples_offset,
            inverted=False
        )
        
        print(f"Trigger pattern shape: {trigger_pattern.shape}")
        print(f"Trigger pattern dtype: {trigger_pattern.dtype}")
        print(f"Number of pulses: {np.sum(trigger_pattern)}")
        print(f"First 20 samples: {trigger_pattern[:20]}")
        
        # Create waveform data
        waveforms = WaveformData()
        waveforms.digital_output[trigger_dio_line] = trigger_pattern
        
        print("✓ Trigger waveform generated")
        
    except Exception as e:
        print(f"✗ Failed to generate trigger waveform: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # ========================================================================
    # STEP 5: Configure and Arm NI DAQ
    # ========================================================================
    print_step(5, "Configure and Arm NI DAQ")
    
    try:
        print("Configuring NI DAQ...")
        ni_daq.configure(daq_config)
        print("✓ NI DAQ configured")
        
        print("Setting waveforms...")
        ni_daq.set_waveforms(waveforms)
        print("✓ Waveforms set")
        
        print("Arming NI DAQ...")
        ni_daq.arm()
        print("✓ NI DAQ armed (ready for trigger)")
        
    except Exception as e:
        print(f"✗ Failed to configure/arm NI DAQ: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # ========================================================================
    # STEP 6: Start Camera Acquisition
    # ========================================================================
    print_step(6, "Start Camera Acquisition")
    
    try:
        # Stop any existing streaming
        if camera.get_is_streaming():
            print("Stopping existing camera streaming...")
            camera.stop_streaming()
        # End previous acquisition
        if camera._camera.IsStreaming():
            print("Ending previous camera acquisition...")
            camera.stop_streaming()

        # Start camera acquisition (BeginAcquisition)
        print("Starting camera acquisition (BeginAcquisition)...")
        if not camera._camera.IsStreaming():
            camera._camera.BeginAcquisition()
            print("✓ Camera acquisition started")
        else:
            print("⚠ Camera already acquiring")
        
        # Check camera state
        print(f"Camera IsStreaming: {camera._camera.IsStreaming()}")
        
    except Exception as e:
        print(f"✗ Failed to start camera acquisition: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # ========================================================================
    # STEP 7: Fire DAQ Trigger and Collect Frames
    # ========================================================================
    print_step(7, "Fire DAQ Trigger and Collect Frames")
    
    frames_collected = []
    frame_times = []
    
    try:
        print("Firing DAQ start trigger...")
        ni_daq.start_trigger()
        print("✓ DAQ trigger fired")
        start_time = time.time()
        # Wait a moment for trigger to propagate
        time.sleep(0.001)
        
        # Try to collect frames
        timeout_duration_ms = int(np.ceil(1/frame_rate_hz*1000*1.1))
        print(f"\nAttempting to collect {num_frames} frames...")
        print(f"Using GetNextImage with {timeout_duration_ms} ms timeout...")
        print(f"Expected number of frames: {int(np.ceil(num_frames*1.1))}")
        
        for i in range(int(np.ceil(num_frames*1.1))):  # Try up to 2x expected frames
            try:
                # Get next image with short timeout
                raw_image = camera._camera.GetNextImage(timeout_duration_ms)
                
                if raw_image is None:
                    print(f"  Frame {i+1}: No image available (timeout)")
                    time.sleep(0.001)
                    continue
                
                # Check if image is complete
                if raw_image.IsIncomplete():
                    status = raw_image.GetImageStatus()
                    print(f"  Frame {i+1}: Incomplete (status={status})")
                    raw_image.Release()
                    continue
                
                # Convert to numpy
                numpy_image = raw_image.GetNDArray()
                frame_time = time.time() - start_time
                
                print(f"  Frame {i+1}: Collected! Shape={numpy_image.shape}, dtype={numpy_image.dtype}, "
                      f"min={numpy_image.min()}, max={numpy_image.max()}, mean={numpy_image.mean():.1f} at {frame_time*1000} ms")
                
                frames_collected.append(numpy_image.copy())
                frame_times.append(frame_time)
                
                raw_image.Release()
                
                # Stop if we have enough frames
                # if len(frames_collected) >= num_frames:
                #     break
                    
            except PySpin.SpinnakerException as e:
                error_str = str(e)
                if "timeout" in error_str.lower() or "not available" in error_str.lower():
                    # Timeout is expected, don't print every time
                    if i % 10 == 0:
                        print(f"  Frame {i+1}: Timeout (expected)")
                else:
                    print(f"  Frame {i+1}: Error - {e}")
                time.sleep(0.01)
        
        print(f"\n✓ Collected {len(frames_collected)} frames")
        
    except Exception as e:
        print(f"✗ Failed during frame collection: {e}")
        import traceback
        traceback.print_exc()
    
    # ========================================================================
    # STEP 8: Wait for DAQ Completion and Cleanup
    # ========================================================================
    print_step(8, "Wait for DAQ Completion and Cleanup")
    
    try:
        print("Waiting for DAQ to complete...")
        ni_daq.wait_until_done(timeout_s=5.0)
        print("✓ DAQ completed")
        
        print("Stopping DAQ...")
        ni_daq.stop()
        print("✓ DAQ stopped")
        
        print("Stopping camera acquisition...")
        if camera._camera.IsStreaming():
            camera._camera.EndAcquisition()
        print("✓ Camera acquisition stopped")
        
        print("Closing DAQ...")
        ni_daq.close()
        print("✓ DAQ closed")
        
    except Exception as e:
        print(f"✗ Error during cleanup: {e}")
        import traceback
        traceback.print_exc()
    
    # ========================================================================
    # STEP 9: Summary
    # ========================================================================
    print_step(9, "Test Summary")
    
    print(f"Frames collected: {len(frames_collected)} / {num_frames} expected")
    
    if len(frames_collected) > 0:
        if len(frame_times) > 1:
            intervals = np.diff(frame_times)
            print
            print(f"Average interval: {np.mean(intervals):.5f} s")
            print(f"Expected interval: {1.0/frame_rate_hz:.5f} s")
        print("✓ TEST PASSED: Frames were collected!")
        return True
    else:
        print("✗ TEST FAILED: No frames were collected!")
        print("\nTroubleshooting suggestions:")
        print("  1. Check that camera trigger line (Line3) is connected to NI DAQ DIO 1")
        print("  2. Verify camera is in HARDWARE_TRIGGER mode")
        print("  3. Check that DAQ waveforms are being generated correctly")
        print("  4. Verify camera exposure time is reasonable")
        print("  5. Check camera trigger activation (should be RisingEdge)")
        return False

if __name__ == "__main__":
    success = test_hw_trigger_frame_collection()
    sys.exit(0 if success else 1)


