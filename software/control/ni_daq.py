"""
National Instruments DAQ interface for synchronized waveform generation and acquisition.

This module provides an interface to NI DAQ hardware for:
- Analog output waveform generation (multiple channels)
- Digital output waveform generation (multiple lines)
- Analog input acquisition (multiple channels)
- Hardware-triggered synchronized operation

The typical workflow is:
1. Configure the clock rate and number of samples
2. Set up analog output waveforms
3. Set up digital output patterns
4. Configure analog input channels
5. Arm the tasks (prepare for start trigger)
6. Send start trigger or wait for external trigger
7. Collect acquired analog input data

All tasks share a common sample clock and start trigger for synchronized operation.
"""

import abc
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Dict, List, Optional, Tuple, Union

import numpy as np

import squid.logging

# Module-level logger
_log = squid.logging.get_logger(__name__)


class TriggerEdge(Enum):
    """Edge type for digital trigger signals."""
    RISING = auto()
    FALLING = auto()


class TriggerSource(Enum):
    """Source of the start trigger."""
    SOFTWARE = auto()  # Software-initiated start
    EXTERNAL = auto()  # External digital trigger input
    INTERNAL = auto()  # Internal trigger from another task


@dataclass
class NIDAQConfig:
    """Configuration for NI DAQ operation."""
    
    # Device identifier (e.g., "Dev1")
    device_name: str = "Dev1"
    # Sample clock configuration
    sample_rate_hz: float = 10000.0  # Samples per second
    samples_per_channel: int = 1000  # Number of samples per waveform cycle
    
    # Analog output configuration
    ao_channels: List[str] = field(default_factory=list)  # e.g., ["ao0", "ao1"]
    ao_min_voltage: float = -10.0
    ao_max_voltage: float = 10.0
    
    # Digital output configuration  
    do_port: str = "port0"  # e.g., "port0"
    do_lines: List[int] = field(default_factory=list)  # e.g., [0, 1, 2, 3]
    
    # Digital input configuration
    di_port: str = "port0"  # e.g., "port0"
    di_lines: List[int] = field(default_factory=list)  # e.g., [0, 1, 2, 3]
    
    # Analog input configuration
    ai_channels: List[str] = field(default_factory=list)  # e.g., ["ai0", "ai1"]
    ai_min_voltage: float = -10.0
    ai_max_voltage: float = 10.0
    ai_terminal_config: str = "RSE"  # RSE, NRSE, Diff, PseudoDiff
    
    # Trigger configuration
    trigger_source: TriggerSource = TriggerSource.SOFTWARE
    external_trigger_terminal: str = "/Dev1/PFI0"  # For external trigger
    trigger_edge: TriggerEdge = TriggerEdge.RISING
    
    # Continuous vs finite operation
    continuous: bool = False  # If True, waveforms repeat continuously


@dataclass
class WaveformData:
    """Container for waveform data."""
    
    # Analog output waveforms: dict mapping channel name to numpy array
    analog_output: Dict[str, np.ndarray] = field(default_factory=dict)
    
    # Digital output patterns: dict mapping line index to numpy array of bool
    digital_output: Dict[int, np.ndarray] = field(default_factory=dict)


@dataclass
class AcquisitionResult:
    """Container for acquisition results."""
    
    # Analog input data: dict mapping channel name to numpy array
    analog_input: Dict[str, np.ndarray] = field(default_factory=dict)
    
    # Digital input data: dict mapping line index to numpy array of bool
    digital_input: Dict[int, np.ndarray] = field(default_factory=dict)

    # Analog output data: dict mapping channel name to numpy array
    analog_output: Dict[str, np.ndarray] = field(default_factory=dict)
    
    # Digital output data: dict mapping line index to numpy array of bool
    digital_output: Dict[int, np.ndarray] = field(default_factory=dict)
    
    # Timestamps for the samples (seconds from start)
    timestamps: Optional[np.ndarray] = None
    
    # Sample rate used for acquisition
    sample_rate_hz: float = 0.0
    
    # Number of samples acquired per channel
    samples_acquired: int = 0


class AbstractNIDAQ(abc.ABC):
    """Abstract base class for NI DAQ interface."""
    
    def __init__(self, config: NIDAQConfig):
        self._log = squid.logging.get_logger(self.__class__.__name__)
        self._config = config
        self._is_armed = False
        self._is_running = False
        
    @property
    def config(self) -> NIDAQConfig:
        """Get the current configuration."""
        return self._config
    
    @property
    def is_armed(self) -> bool:
        """Check if tasks are armed and ready for trigger."""
        return self._is_armed
    
    @property
    def is_running(self) -> bool:
        """Check if tasks are currently running."""
        return self._is_running
    
    @abc.abstractmethod
    def configure(self, config: NIDAQConfig) -> None:
        """Update the configuration."""
        pass
    
    @abc.abstractmethod
    def set_waveforms(self, waveforms: WaveformData) -> None:
        """
        Set the output waveforms.
        
        Args:
            waveforms: WaveformData containing analog and digital output patterns
        """
        pass
    
    @abc.abstractmethod
    def arm(self) -> None:
        """
        Arm all tasks, preparing them to start on trigger.
        
        After arming, the tasks will start when:
        - start_trigger() is called (for SOFTWARE trigger)
        - An external trigger is received (for EXTERNAL trigger)
        """
        pass
    
    @abc.abstractmethod
    def start_trigger(self) -> None:
        """
        Send a software start trigger.
        
        Only valid when trigger_source is SOFTWARE.
        """
        pass
    
    @abc.abstractmethod
    def wait_until_done(self, timeout_s: float = 10.0) -> bool:
        """
        Wait until the tasks complete.
        
        Args:
            timeout_s: Maximum time to wait in seconds
            
        Returns:
            True if completed successfully, False if timed out
        """
        pass
    
    @abc.abstractmethod
    def stop(self) -> None:
        """Stop all running tasks."""
        pass
    
    @abc.abstractmethod
    def get_acquired_data(self) -> AcquisitionResult:
        """
        Get the acquired analog input data.
        
        Returns:
            AcquisitionResult containing the acquired data
        """
        pass
    
    @abc.abstractmethod
    def close(self) -> None:
        """Release all resources and close connection to hardware."""
        pass
    
    @abc.abstractmethod
    def get_available_devices(self) -> List[str]:
        """Get list of available NI DAQ devices."""
        pass
    
    @abc.abstractmethod
    def get_device_info(self, device_name: str) -> Dict:
        """Get information about a specific device."""
        pass


class NIDAQ(AbstractNIDAQ):
    """
    National Instruments DAQ interface using nidaqmx library.
    
    This class manages synchronized analog output, digital output, and analog input
    tasks that share a common sample clock and start trigger.
    """
    
    def __init__(self, config: NIDAQConfig):
        super().__init__(config)
        
        try:
            import nidaqmx
            import nidaqmx.constants as constants
            import nidaqmx.system as system
            self._nidaqmx = nidaqmx
            self._constants = constants
            self._system = system
        except ImportError:
            raise ImportError(
                "nidaqmx library is required for NI DAQ support. "
                "Install with: pip install nidaqmx"
            )
        
        self._ao_task = None
        self._do_task = None
        self._di_task = None
        self._ai_task = None
        
        self._waveforms: Optional[WaveformData] = None
        self._acquired_data: Optional[np.ndarray] = None
        self._acquired_di_data: Optional[np.ndarray] = None
        
        self._lock = threading.Lock()
    
    def configure(self, config: NIDAQConfig) -> None:
        """Update the configuration."""
        with self._lock:
            if self._is_running:
                raise RuntimeError("Cannot configure while tasks are running")
            self._config = config
            self._cleanup_tasks()
    
    def set_waveforms(self, waveforms: WaveformData) -> None:
        """Set the output waveforms."""
        with self._lock:
            if self._is_running:
                raise RuntimeError("Cannot set waveforms while tasks are running")
            
            # Validate waveform lengths match samples_per_channel
            expected_samples = self._config.samples_per_channel
            
            for channel, data in waveforms.analog_output.items():
                if len(data) != expected_samples:
                    raise ValueError(
                        f"Analog output channel {channel} has {len(data)} samples, "
                        f"expected {expected_samples}"
                    )
            
            for line, data in waveforms.digital_output.items():
                if len(data) != expected_samples:
                    raise ValueError(
                        f"Digital output line {line} has {len(data)} samples, "
                        f"expected {expected_samples}"
                    )
            
            self._waveforms = waveforms
    
    def arm(self) -> None:
        """Arm all tasks, preparing them to start on trigger."""
        with self._lock:
            if self._is_armed:
                self._log.warning("Tasks already armed, stopping first")
                self._stop_internal()
            
            self._cleanup_tasks()
            self._setup_tasks()
            self._is_armed = True
            self._log.info("Tasks armed and ready for trigger")
    
    def start_trigger(self) -> None:
        """Send a software start trigger."""
        with self._lock:
            if not self._is_armed:
                raise RuntimeError("Tasks must be armed before triggering")
            
            if self._config.trigger_source != TriggerSource.SOFTWARE:
                self._log.warning(
                    f"Trigger source is {self._config.trigger_source}, "
                    "software trigger may not work as expected"
                )
            
            # Start tasks in order: AI/DI first (if slaves), then DO, then AO (master)
            if self._ai_task is not None:
                self._ai_task.start()
            if self._di_task is not None:
                self._di_task.start()
            if self._do_task is not None:
                self._do_task.start()
            if self._ao_task is not None:
                self._ao_task.start()
            
            self._is_running = True
            self._log.info("Tasks started")
    
    def wait_until_done(self, timeout_s: float = 10.0) -> bool:
        """Wait until the tasks complete."""
        with self._lock:
            if not self._is_running:
                return True
            self._log.info(f"Waiting for tasks to complete (timeout={timeout_s}s)...")
            try:
                # Wait for master task to complete
                if self._ao_task is not None:
                    self._ao_task.wait_until_done(timeout=timeout_s)
                elif self._do_task is not None:
                    self._do_task.wait_until_done(timeout=timeout_s)
                elif self._di_task is not None:
                    self._di_task.wait_until_done(timeout=timeout_s)
                
                # Read AI data if configured
                if self._ai_task is not None:
                    self._acquired_data = self._ai_task.read(
                        number_of_samples_per_channel=self._config.samples_per_channel,
                        timeout=timeout_s
                    )
                
                # Read DI data if configured
                if self._di_task is not None:
                    di_data = self._di_task.read(
                        number_of_samples_per_channel=self._config.samples_per_channel,
                        timeout=timeout_s
                    )
                    # Convert to numpy array
                    if isinstance(di_data, (list, tuple)):
                        self._acquired_di_data = np.array(di_data)
                    else:
                        self._acquired_di_data = np.array(di_data)
                
                self._is_running = False
                self._is_armed = False
                self._log.info("Tasks completed")
                return True
                
            except Exception as e:
                self._log.error(f"Wait failed: {e}")
                return False
    
    def stop(self) -> None:
        """Stop all running tasks."""
        with self._lock:
            self._stop_internal()
    
    def _stop_internal(self) -> None:
        """Internal stop without acquiring lock."""
        if self._ao_task is not None:
            try:
                self._ao_task.stop()
            except Exception:
                pass
        if self._do_task is not None:
            try:
                self._do_task.stop()
            except Exception:
                pass
        if self._ai_task is not None:
            try:
                self._ai_task.stop()
            except Exception:
                pass
        if self._di_task is not None:
            try:
                self._di_task.stop()
            except Exception:
                pass
        
        self._is_running = False
        self._is_armed = False
        self._log.info("Tasks stopped")
    
    def get_acquired_data(self) -> AcquisitionResult:
        """Get the acquired analog and digital input data."""
        result = AcquisitionResult(
            sample_rate_hz=self._config.sample_rate_hz,
            samples_acquired=self._config.samples_per_channel
        )
        
        if self._acquired_data is not None and len(self._config.ai_channels) > 0:
            # Convert acquired data to dict format
            if len(self._config.ai_channels) == 1:
                # Single channel returns 1D array
                result.analog_input[self._config.ai_channels[0]] = np.array(self._acquired_data)
            else:
                # Multiple channels returns 2D array [channels x samples]
                for i, channel in enumerate(self._config.ai_channels):
                    result.analog_input[channel] = np.array(self._acquired_data[i])
        
        if self._acquired_di_data is not None and len(self._config.di_lines) > 0:
            # Convert digital input data to dict format
            if len(self._config.di_lines) == 1:
                # Single line returns 1D array
                result.digital_input[self._config.di_lines[0]] = np.array(self._acquired_di_data, dtype=bool)
            else:
                # Multiple lines returns 2D array [lines x samples]
                for i, line in enumerate(self._config.di_lines):
                    result.digital_input[line] = np.array(self._acquired_di_data[i], dtype=bool)

        if self._waveforms is not None:
            result.analog_output = self._waveforms.analog_output.copy()
            result.digital_output = self._waveforms.digital_output.copy()
            result.analog_output_channels = list(self._waveforms.analog_output.keys())
            result.digital_output_lines = list(self._waveforms.digital_output.keys())
        
        # Generate timestamps if we have any data
        if len(result.analog_input) > 0 or len(result.digital_input) > 0:
            result.timestamps = np.arange(self._config.samples_per_channel) / self._config.sample_rate_hz
        
        return result
    
    def close(self) -> None:
        """Release all resources and close connection to hardware."""
        with self._lock:
            self._stop_internal()
            self._cleanup_tasks()
            self._log.info("NI DAQ closed")
    
    def get_available_devices(self) -> List[str]:
        """Get list of available NI DAQ devices."""
        try:
            system = self._system.System.local()
            return [device.name for device in system.devices]
        except Exception as e:
            self._log.error(f"Failed to get device list: {e}")
            return []
    
    def get_device_info(self, device_name: str) -> Dict:
        """Get information about a specific device."""
        try:
            system = self._system.System.local()
            for device in system.devices:
                if device.name == device_name:
                    return {
                        "name": device.name,
                        "product_type": device.product_type,
                        "serial_number": device.dev_serial_num,
                        "ao_channels": [ch.name for ch in device.ao_physical_chans],
                        "ai_channels": [ch.name for ch in device.ai_physical_chans],
                        "do_lines": [line.name for line in device.do_lines],
                        "di_lines": [line.name for line in device.di_lines],
                        "terminals": list(device.terminals),
                    }
            return {}
        except Exception as e:
            self._log.error(f"Failed to get device info: {e}")
            return {}
    
    def _cleanup_tasks(self) -> None:
        """Clean up all tasks."""
        if self._ao_task is not None:
            try:
                self._ao_task.close()
            except Exception:
                pass
            self._ao_task = None
        
        if self._do_task is not None:
            try:
                self._do_task.close()
            except Exception:
                pass
            self._do_task = None
        
        if self._ai_task is not None:
            try:
                self._ai_task.close()
            except Exception:
                pass
            self._ai_task = None
        
        if self._di_task is not None:
            try:
                self._di_task.close()
            except Exception:
                pass
            self._di_task = None
    
    def _setup_tasks(self) -> None:
        """Set up all configured tasks."""
        nidaqmx = self._nidaqmx
        constants = self._constants
        
        device = self._config.device_name
        sample_rate = self._config.sample_rate_hz
        num_samples = self._config.samples_per_channel
        
        # Determine sample mode
        if self._config.continuous:
            sample_mode = constants.AcquisitionType.CONTINUOUS
        else:
            sample_mode = constants.AcquisitionType.FINITE
        
        # Determine the master task clock source
        # AO will be master, others will use AO's sample clock
        ao_clock_terminal = f"/{device}/ao/SampleClock"
        
        # Set up Analog Output task (master clock source)
        if len(self._config.ao_channels) > 0 and self._waveforms is not None:
            self._ao_task = nidaqmx.Task("ao_task")
            
            for channel in self._config.ao_channels:
                physical_channel = f"{device}/{channel}"
                self._ao_task.ao_channels.add_ao_voltage_chan(
                    physical_channel,
                    min_val=self._config.ao_min_voltage,
                    max_val=self._config.ao_max_voltage
                )
            
            # Configure timing - AO is the master
            self._ao_task.timing.cfg_samp_clk_timing(
                rate=sample_rate,
                sample_mode=sample_mode,
                samps_per_chan=num_samples
            )
            
            # Configure trigger if external
            if self._config.trigger_source == TriggerSource.EXTERNAL:
                edge = (constants.Edge.RISING 
                       if self._config.trigger_edge == TriggerEdge.RISING 
                       else constants.Edge.FALLING)
                self._ao_task.triggers.start_trigger.cfg_dig_edge_start_trig(
                    trigger_source=self._config.external_trigger_terminal,
                    trigger_edge=edge
                )
            
            # Write waveform data
            ao_data = []
            for channel in self._config.ao_channels:
                if channel in self._waveforms.analog_output:
                    ao_data.append(self._waveforms.analog_output[channel])
                else:
                    # Default to zeros if channel not in waveforms
                    ao_data.append(np.zeros(num_samples))
            
            if len(self._config.ao_channels) == 1:
                self._ao_task.write(ao_data[0], auto_start=False)
            else:
                self._ao_task.write(ao_data, auto_start=False)
        
        # Set up Digital Output task
        if len(self._config.do_lines) > 0 and self._waveforms is not None:
            self._do_task = nidaqmx.Task("do_task")
            
            # Add all DO lines
            for line in self._config.do_lines:
                physical_line = f"{device}/{self._config.do_port}/line{line}"
                self._do_task.do_channels.add_do_chan(physical_line)
            
            # Configure timing - use AO clock if available, otherwise internal
            if self._ao_task is not None:
                self._do_task.timing.cfg_samp_clk_timing(
                    rate=sample_rate,
                    source=ao_clock_terminal,
                    sample_mode=sample_mode,
                    samps_per_chan=num_samples
                )
            else:
                self._do_task.timing.cfg_samp_clk_timing(
                    rate=sample_rate,
                    sample_mode=sample_mode,
                    samps_per_chan=num_samples
                )
                
                # Configure trigger if external and no AO task
                if self._config.trigger_source == TriggerSource.EXTERNAL:
                    edge = (constants.Edge.RISING 
                           if self._config.trigger_edge == TriggerEdge.RISING 
                           else constants.Edge.FALLING)
                    self._do_task.triggers.start_trigger.cfg_dig_edge_start_trig(
                        trigger_source=self._config.external_trigger_terminal,
                        trigger_edge=edge
                    )
            
            # Build DO data array
            do_data = []
            for line in self._config.do_lines:
                if line in self._waveforms.digital_output:
                    do_data.append(self._waveforms.digital_output[line].astype(bool))
                else:
                    do_data.append(np.zeros(num_samples, dtype=bool))
            
            if len(self._config.do_lines) == 1:
                self._do_task.write(do_data[0], auto_start=False)
            else:
                self._do_task.write(do_data, auto_start=False)
        
        # Set up Digital Input task
        if len(self._config.di_lines) > 0:
            self._di_task = nidaqmx.Task("di_task")
            
            # Add all DI lines
            for line in self._config.di_lines:
                physical_line = f"{device}/{self._config.di_port}/line{line}"
                self._di_task.di_channels.add_di_chan(physical_line)
            
            # Configure timing - use AO clock if available, otherwise DO clock
            if self._ao_task is not None:
                self._di_task.timing.cfg_samp_clk_timing(
                    rate=sample_rate,
                    source=ao_clock_terminal,
                    sample_mode=sample_mode,
                    samps_per_chan=num_samples
                )
            elif self._do_task is not None:
                do_clock_terminal = f"/{device}/do/SampleClock"
                self._di_task.timing.cfg_samp_clk_timing(
                    rate=sample_rate,
                    source=do_clock_terminal,
                    sample_mode=sample_mode,
                    samps_per_chan=num_samples
                )
            else:
                # DI only mode
                self._di_task.timing.cfg_samp_clk_timing(
                    rate=sample_rate,
                    sample_mode=sample_mode,
                    samps_per_chan=num_samples
                )
                
                # Configure trigger if external
                if self._config.trigger_source == TriggerSource.EXTERNAL:
                    edge = (constants.Edge.RISING 
                           if self._config.trigger_edge == TriggerEdge.RISING 
                           else constants.Edge.FALLING)
                    self._di_task.triggers.start_trigger.cfg_dig_edge_start_trig(
                        trigger_source=self._config.external_trigger_terminal,
                        trigger_edge=edge
                    )
        
        # Set up Analog Input task
        if len(self._config.ai_channels) > 0:
            self._ai_task = nidaqmx.Task("ai_task")
            
            # Get terminal configuration
            terminal_config_map = {
                "RSE": constants.TerminalConfiguration.RSE,
                "NRSE": constants.TerminalConfiguration.NRSE,
                "Diff": constants.TerminalConfiguration.DIFF,
                "PseudoDiff": constants.TerminalConfiguration.PSEUDO_DIFF,
            }
            terminal_config = terminal_config_map.get(
                self._config.ai_terminal_config,
                constants.TerminalConfiguration.RSE
            )
            
            for channel in self._config.ai_channels:
                physical_channel = f"{device}/{channel}"
                self._ai_task.ai_channels.add_ai_voltage_chan(
                    physical_channel,
                    min_val=self._config.ai_min_voltage,
                    max_val=self._config.ai_max_voltage,
                    terminal_config=terminal_config
                )
            
            # Configure timing - use AO clock if available, otherwise DO or DI clock
            if self._ao_task is not None:
                self._ai_task.timing.cfg_samp_clk_timing(
                    rate=sample_rate,
                    source=ao_clock_terminal,
                    sample_mode=sample_mode,
                    samps_per_chan=num_samples
                )
            elif self._do_task is not None:
                do_clock_terminal = f"/{device}/do/SampleClock"
                self._ai_task.timing.cfg_samp_clk_timing(
                    rate=sample_rate,
                    source=do_clock_terminal,
                    sample_mode=sample_mode,
                    samps_per_chan=num_samples
                )
            elif self._di_task is not None:
                di_clock_terminal = f"/{device}/di/SampleClock"
                self._ai_task.timing.cfg_samp_clk_timing(
                    rate=sample_rate,
                    source=di_clock_terminal,
                    sample_mode=sample_mode,
                    samps_per_chan=num_samples
                )
            else:
                # AI only mode
                self._ai_task.timing.cfg_samp_clk_timing(
                    rate=sample_rate,
                    sample_mode=sample_mode,
                    samps_per_chan=num_samples
                )
                
                # Configure trigger if external
                if self._config.trigger_source == TriggerSource.EXTERNAL:
                    edge = (constants.Edge.RISING 
                           if self._config.trigger_edge == TriggerEdge.RISING 
                           else constants.Edge.FALLING)
                    self._ai_task.triggers.start_trigger.cfg_dig_edge_start_trig(
                        trigger_source=self._config.external_trigger_terminal,
                        trigger_edge=edge
                    )


class SimulatedNIDAQ(AbstractNIDAQ):
    """
    Simulated NI DAQ for testing without hardware.
    
    This class simulates the behavior of a real NI DAQ, generating
    synthetic input data based on the output waveforms.
    """
    
    def __init__(self, config: NIDAQConfig):
        super().__init__(config)
        
        self._waveforms: Optional[WaveformData] = None
        self._acquired_data: Dict[str, np.ndarray] = {}
        self._acquired_di_data: Optional[np.ndarray] = None
        
        self._lock = threading.Lock()
        self._completion_event = threading.Event()
    
    def configure(self, config: NIDAQConfig) -> None:
        """Update the configuration."""
        with self._lock:
            if self._is_running:
                raise RuntimeError("Cannot configure while tasks are running")
            self._config = config
    
    def set_waveforms(self, waveforms: WaveformData) -> None:
        """Set the output waveforms."""
        with self._lock:
            if self._is_running:
                raise RuntimeError("Cannot set waveforms while tasks are running")
            self._waveforms = waveforms
    
    def arm(self) -> None:
        """Arm all tasks, preparing them to start on trigger."""
        with self._lock:
            self._completion_event.clear()
            self._acquired_data = {}
            self._is_armed = True
            self._log.info("[SIM] Tasks armed and ready for trigger")
    
    def start_trigger(self) -> None:
        """Send a software start trigger."""
        with self._lock:
            if not self._is_armed:
                raise RuntimeError("Tasks must be armed before triggering")
            
            self._is_running = True
            self._log.info("[SIM] Tasks started")
            
            # Simulate acquisition in a separate thread
            thread = threading.Thread(target=self._simulate_acquisition)
            thread.daemon = True
            thread.start()
    
    def _simulate_acquisition(self) -> None:
        """Simulate data acquisition."""
        # Calculate expected duration
        duration_s = self._config.samples_per_channel / self._config.sample_rate_hz
        
        # Simulate the acquisition time
        time.sleep(duration_s)
        
        # Generate simulated AI data
        with self._lock:
            for channel in self._config.ai_channels:
                # Generate noisy sinusoidal data
                t = np.arange(self._config.samples_per_channel) / self._config.sample_rate_hz
                # Base signal: sum of a few sinusoids plus noise
                signal = (
                    1.0 * np.sin(2 * np.pi * 100 * t) +
                    0.5 * np.sin(2 * np.pi * 200 * t) +
                    0.1 * np.random.randn(len(t))
                )
                self._acquired_data[channel] = signal
            
            self._is_running = False
            self._is_armed = False
            self._completion_event.set()
            self._log.info("[SIM] Acquisition complete")
    
    def wait_until_done(self, timeout_s: float = 10.0) -> bool:
        """Wait until the tasks complete."""
        if not self._is_running and not self._is_armed:
            return True
        
        result = self._completion_event.wait(timeout=timeout_s)
        return result
    
    def stop(self) -> None:
        """Stop all running tasks."""
        with self._lock:
            self._is_running = False
            self._is_armed = False
            self._completion_event.set()
            self._log.info("[SIM] Tasks stopped")
    
    def get_acquired_data(self) -> AcquisitionResult:
        """Get the acquired analog and digital input data."""
        result = AcquisitionResult(
            analog_input=self._acquired_data.copy(),
            sample_rate_hz=self._config.sample_rate_hz,
            samples_acquired=self._config.samples_per_channel
        )
        
        # Add digital input data if available
        if self._acquired_di_data is not None and len(self._config.di_lines) > 0:
            if len(self._config.di_lines) == 1:
                result.digital_input[self._config.di_lines[0]] = np.array(self._acquired_di_data, dtype=bool)
            else:
                for i, line in enumerate(self._config.di_lines):
                    result.digital_input[line] = np.array(self._acquired_di_data[i], dtype=bool)
        
        if len(self._acquired_data) > 0 or len(result.digital_input) > 0:
            result.timestamps = np.arange(self._config.samples_per_channel) / self._config.sample_rate_hz
        
        return result
    
    def close(self) -> None:
        """Release all resources."""
        self.stop()
        self._log.info("[SIM] NI DAQ closed")
    
    def get_available_devices(self) -> List[str]:
        """Get list of available NI DAQ devices (simulated)."""
        return ["SimDev1", "SimDev2"]
    
    def get_device_info(self, device_name: str) -> Dict:
        """Get information about a specific device (simulated)."""
        if device_name in self.get_available_devices():
            return {
                "name": device_name,
                "product_type": "Simulated NI DAQ",
                "serial_number": "SIM12345",
                "ao_channels": [f"{device_name}/ao{i}" for i in range(4)],
                "ai_channels": [f"{device_name}/ai{i}" for i in range(8)],
                "do_lines": [f"{device_name}/port0/line{i}" for i in range(8)],
                "di_lines": [f"{device_name}/port0/line{i}" for i in range(8)],
                "terminals": [f"/{device_name}/PFI{i}" for i in range(8)],
            }
        return {}


def create_ni_daq(config: NIDAQConfig, simulation: bool = False) -> AbstractNIDAQ:
    """
    Factory function to create an NI DAQ instance.
    
    Args:
        config: Configuration for the NI DAQ
        simulation: If True, create a simulated device
        
    Returns:
        An AbstractNIDAQ instance (either real or simulated)
    """
    if simulation:
        _log.info("Creating simulated NI DAQ")
        return SimulatedNIDAQ(config)
    else:
        _log.info(f"Creating NI DAQ for device {config.device_name}")
        return NIDAQ(config)


# ============================================================================
# Waveform Generation Utilities
# ============================================================================

def generate_sine_wave(
    frequency_hz: float,
    amplitude: float,
    sample_rate_hz: float,
    num_samples: int,
    offset: float = 0.0,
    phase_rad: float = 0.0
) -> np.ndarray:
    """Generate a sine wave."""
    t = np.arange(num_samples) / sample_rate_hz
    return amplitude * np.sin(2 * np.pi * frequency_hz * t + phase_rad) + offset


def generate_square_wave(
    frequency_hz: float,
    amplitude: float,
    sample_rate_hz: float,
    num_samples: int,
    offset: float = 0.0,
    duty_cycle: float = 0.5
) -> np.ndarray:
    """Generate a square wave."""
    from scipy import signal as scipy_signal
    t = np.arange(num_samples) / sample_rate_hz
    return amplitude * scipy_signal.square(2 * np.pi * frequency_hz * t, duty=duty_cycle) + offset


def generate_ramp_wave(
    frequency_hz: float,
    amplitude: float,
    sample_rate_hz: float,
    num_samples: int,
    offset: float = 0.0
) -> np.ndarray:
    """Generate a sawtooth/ramp wave."""
    from scipy import signal as scipy_signal
    t = np.arange(num_samples) / sample_rate_hz
    return amplitude * scipy_signal.sawtooth(2 * np.pi * frequency_hz * t) + offset


def generate_pulse_train(
    pulse_width_samples: int,
    period_samples: int,
    num_samples: int,
    n_samples_offset: int = 0,
    inverted: bool = False
) -> np.ndarray:
    """
    Generate a digital pulse train.
    
    Args:
        pulse_width_samples: Width of each pulse in samples
        period_samples: Period between pulses in samples
        num_samples: Total number of samples
        inverted: If True, pulse is low instead of high
        
    Returns:
        Boolean array representing the pulse train
    """
    pattern = np.zeros(num_samples, dtype=bool)
    
    for start in range(n_samples_offset, num_samples, period_samples):
        end = min(start + pulse_width_samples, num_samples)
        pattern[start:end] = True
    
    if inverted:
        pattern = ~pattern
    
    return pattern
