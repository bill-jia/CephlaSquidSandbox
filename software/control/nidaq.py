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
from dataclasses import dataclass, field, fields
from enum import Enum, auto
from typing import Callable, Dict, List, Optional, Tuple, Union, Iterable, Set, Mapping, Any
from nidaqmx.constants import LogicFamily
from nidaqmx.system.physical_channel import PhysicalChannel
from control._def import NI_DAQ_LOGIC_FAMILY
from control.models.io_endpoint_config import (
    IOControllerType,
    IOEndpointConfig,
    IOSignalType,
    IODirection,
)
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
    
    def __init__(
        self,
        *,
        device_name: str,
        sample_rate_hz: float,
        samples_per_channel: int,
        ao_channels: Optional[List[str]] = None,
        ao_min_voltage: float = -10.0,
        ao_max_voltage: float = 10.0,
        do_port: str = "port0",
        do_lines: Optional[List[int]] = None,
        di_port: str = "port0",
        di_lines: Optional[List[int]] = None,
        ai_channels: Optional[List[str]] = None,
        ai_min_voltage: float = -10.0,
        ai_max_voltage: float = 10.0,
        ai_terminal_config: str = "RSE",
        trigger_source: Union[str, "TriggerSource"] = "SOFTWARE",
        external_trigger_terminal: Optional[str] = None,
        trigger_edge: Union[str, "TriggerEdge"] = "RISING",
        continuous: bool = False,
        do_logic_family: str = NI_DAQ_LOGIC_FAMILY,
    ):
        self._log = squid.logging.get_logger(self.__class__.__name__)

        # Required core timing/device settings
        self.device_name = str(device_name)
        self.sample_rate_hz = float(sample_rate_hz)
        self.samples_per_channel = int(samples_per_channel)

        # Channel collections
        # Public attributes reflect all channels/lines available on this device
        # (typically derived from machine configuration).
        self.ao_channels = list(ao_channels) if ao_channels is not None else []
        self.do_lines = list(do_lines) if do_lines is not None else []
        self.di_lines = list(di_lines) if di_lines is not None else []
        self.ai_channels = list(ai_channels) if ai_channels is not None else []

        # Voltage ranges and AI terminal config
        self.ao_min_voltage = float(ao_min_voltage)
        self.ao_max_voltage = float(ao_max_voltage)
        self.ai_min_voltage = float(ai_min_voltage)
        self.ai_max_voltage = float(ai_max_voltage)
        self.ai_terminal_config = str(ai_terminal_config)

        # Digital ports
        self.do_port = str(do_port)
        self.di_port = str(di_port)

        # Trigger configuration
        if isinstance(trigger_source, str):
            self.trigger_source = TriggerSource[trigger_source]
        else:
            self.trigger_source = trigger_source

        if isinstance(trigger_edge, str):
            self.trigger_edge = TriggerEdge[trigger_edge]
        else:
            self.trigger_edge = trigger_edge

        if external_trigger_terminal is None:
            self.external_trigger_terminal = f"/{self.device_name}/PFI0"
        else:
            self.external_trigger_terminal = str(external_trigger_terminal)

        self.continuous = bool(continuous)
        self.do_logic_family = str(do_logic_family)

        self._is_armed = False
        self._is_running = False

        # Logical task configuration (which endpoints participate in the CURRENT
        # waveform-based task). By default this is initialized to \"all available\"
        # channels, but higher layers should narrow/modify these via
        # configure_task_io(...) without overwriting the public collections.
        self._task_ao_channels: List[str] = list(self.ao_channels)
        self._task_do_lines: List[int] = list(self.do_lines)
        self._task_di_lines: List[int] = list(self.di_lines)
        self._task_ai_channels: List[str] = list(self.ai_channels)

        # Live-output state (logical only; hardware-specific tasks are managed in subclasses)
        # These track the latest requested live values per endpoint, independent of tasks.
        self._live_ao_values: Dict[str, float] = {}
        self._live_do_values: Dict[int, bool] = {}

        # Snapshot of live values for endpoints that overlap with a running acquisition.
        # Used by prepare_for_acquisition()/restore_after_acquisition() in subclasses.
        self._live_ao_overrides_for_acquisition: Dict[str, float] = {}
        self._live_do_overrides_for_acquisition: Dict[int, bool] = {}

        # Human-readable descriptions for channels/lines (e.g. from machine config display_name).
        # Keys use the same identifiers as task IO: "ao0", "ai0", "line3", etc.
        self._channel_descriptions: Dict[str, str] = {}

        # Backwards-compat alias: config/_config refer to the instance
        self._config = self

    @property
    def config(self):
        """Backwards-compatible accessor for configuration/state."""
        return self
    
    @property
    def is_armed(self) -> bool:
        """Check if tasks are armed and ready for trigger."""
        return self._is_armed
    
    @property
    def is_running(self) -> bool:
        """Check if tasks are currently running."""
        return self._is_running
    
    @abc.abstractmethod
    def configure(self, config: Mapping[str, Any]) -> None:
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

    # -------------------------------------------------------------------------
    # Task / live state helpers (logical, hardware-agnostic)
    # -------------------------------------------------------------------------

    def configure_task_io(
        self,
        *,
        ao_channels: Optional[Iterable[str]] = None,
        do_lines: Optional[Iterable[int]] = None,
        di_lines: Optional[Iterable[int]] = None,
        ai_channels: Optional[Iterable[str]] = None,
    ) -> None:
        """
        Configure which endpoints participate in waveform-based tasks.

        This updates only the logical task IO tracking used when hardware tasks
        are created, without overwriting the full-channel collections
        (ao_channels/do_lines/di_lines/ai_channels), which represent all
        available endpoints from machine configuration.
        """
        if ao_channels is not None:
            self._task_ao_channels = list(ao_channels)
        if do_lines is not None:
            self._task_do_lines = list(do_lines)
        if di_lines is not None:
            self._task_di_lines = list(di_lines)
        if ai_channels is not None:
            self._task_ai_channels = list(ai_channels)

    def get_task_io(self) -> Dict[str, List[Union[str, int]]]:
        """Return the currently configured task IO endpoints."""
        return {
            "ao_channels": list(self._task_ao_channels),
            "do_lines": list(self._task_do_lines),
            "di_lines": list(self._task_di_lines),
            "ai_channels": list(self._task_ai_channels),
        }

    def set_channel_descriptions(self, descriptions: Dict[str, str]) -> None:
        """Set human-readable descriptions for channels/lines.

        Keys should match the identifiers used in task IO and HDF5 datasets,
        e.g. ``"ao0"``, ``"ai0"``, ``"line3"``.
        """
        self._channel_descriptions.update(descriptions)

    def get_channel_descriptions(self) -> Dict[str, str]:
        """Return the current channel/line description mapping."""
        return dict(self._channel_descriptions)

    def get_live_output_state(self) -> Dict[str, Dict[Union[str, int], Union[float, bool]]]:
        """
        Return the current logical live-output state (latest requested values).

        Subclasses are responsible for ensuring their hardware tasks reflect this state.
        """
        return {
            "ao": dict(self._live_ao_values),
            "do": dict(self._live_do_values),
        }

    def start_live_output(
        self,
        ao_values: Optional[Dict[str, float]] = None,
        do_values: Optional[Dict[int, bool]] = None,
    ) -> None:
        """Output constant values for debugging. Override in hardware implementation."""
        pass

    def stop_live_output(self) -> None:
        """Stop constant live output. Override in hardware implementation."""
        pass

    def release_tasks(self) -> None:
        """Stop and close all tasks so the device is free for new tasks (e.g. live output or re-arm)."""
        pass


class NIDAQ(AbstractNIDAQ):
    """
    National Instruments DAQ interface using nidaqmx library.
    
    This class manages synchronized analog output, digital output, and analog input
    tasks that share a common sample clock and start trigger.
    """
    
    def __init__(self, **config: Any):
        super().__init__(**config)
        
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
        self._live_ao_task = None
        self._live_do_task = None
        
        self._waveforms: Optional[WaveformData] = None
        self._acquired_data: Optional[np.ndarray] = None
        self._acquired_di_data: Optional[np.ndarray] = None
        
        self._lock = threading.Lock()

        # Track whether we have live overrides that were active when an acquisition
        # was prepared. Used so we can restore live outputs after the task completes.
        self._has_live_overrides_for_acquisition: bool = False

        # Configure digital port logic family ONCE at initialization
        # This must be done before any tasks are created
        self._configure_digital_port_logic_family()
        self._log.info(f"Initialized NI DAQ for device {self._config.device_name}")
    
    def _configure_digital_port_logic_family(self):
        """
        Configure the digital port logic family based on camera type.
        
        This should be called ONCE during initialization before any tasks are created.
        - FLIR cameras require 3.3V TTL logic (THREE_POINT_THREE_V)
        - Photometrics and most other cameras use 5V TTL logic (FIVE_V, default)
        
        The logic family setting affects the voltage levels used for digital I/O:
        - 3.3V: Logic high = 3.3V, compatible with FLIR Blackfly cameras
        - 5V: Logic high = 5V, compatible with most industrial cameras
        """
        device = self._config.device_name
        
        # Configure DO port logic family
        if self._config.do_port:
            do_port_name = f"{device}/{self._config.do_port}"
            # try:
            phys_channel = PhysicalChannel(do_port_name)
            if self._config.do_logic_family == "THREE_POINT_THREE_V":
                phys_channel.dig_port_logic_family = LogicFamily.THREE_POINT_THREE_V
                self._log.info(f"Set {do_port_name} logic family to 3.3V (FLIR compatible)")
            else:
                # Default to 5V - standard for most cameras including Photometrics
                phys_channel.dig_port_logic_family = LogicFamily.FIVE_V
                self._log.info(f"Set {do_port_name} logic family to 5V (default)")
            # except Exception as e:
                # self._log.warning(f"Could not set DO port logic family for {do_port_name}: {e}")
        
        # Configure DI port logic family (if different from DO port)
        if self._config.di_port and self._config.di_port != self._config.do_port:
            di_port_name = f"{device}/{self._config.di_port}"
            try:
                phys_channel = PhysicalChannel(di_port_name)
                if self._config.do_logic_family == "THREE_POINT_THREE_V":
                    phys_channel.dig_port_logic_family = LogicFamily.THREE_POINT_THREE_V
                else:
                    phys_channel.dig_port_logic_family = LogicFamily.FIVE_V
            except Exception as e:
                self._log.warning(f"Could not set DI port logic family for {di_port_name}: {e}")

    def configure(self, **config: Any) -> None:
        """Update the configuration."""
        with self._lock:
            if self._is_running:
                raise RuntimeError("Cannot configure while tasks are running")
            # Update known attributes; reject unknown keys
            for key, value in config.items():
                if not hasattr(self, key):
                    raise ValueError(f"Unknown NI-DAQ config key: {key}")
                setattr(self, key, value)

            # Normalize trigger enums if they were updated
            if "trigger_source" in config and isinstance(self.trigger_source, str):
                self.trigger_source = TriggerSource[self.trigger_source]
            if "trigger_edge" in config and isinstance(self.trigger_edge, str):
                self.trigger_edge = TriggerEdge[self.trigger_edge]

            self._stop_live_output()
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
            self._stop_live_output()
            self._cleanup_tasks()
            self._setup_tasks()
            self._is_armed = True
            self._log.info("Tasks armed and ready for trigger")
    
    def start_trigger(self) -> None:
        """Send a software start trigger."""
        with self._lock:
            if not self._is_armed:
                raise RuntimeError("Tasks must be armed before triggering")
            if self._config.trigger_source == TriggerSource.INTERNAL:
                master_task = (
                    self._ao_task if self._ao_task is not None
                    else self._do_task if self._do_task is not None
                    else self._di_task if self._di_task is not None
                    else self._ai_task
                )
                if master_task is not None:
                    self._log.info(f"Using internal start trigger from {master_task.name}")
                else:
                    self._log.info("Using internal start trigger (no output/input tasks configured)")
            else:
                self._log.info(f"Starting trigger with source {self._config.trigger_source}")
            
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
        
        if self._acquired_data is not None and len(self._task_ai_channels) > 0:
            # Convert acquired data to dict format
            if len(self._task_ai_channels) == 1:
                # Single channel returns 1D array
                result.analog_input[self._task_ai_channels[0]] = np.array(self._acquired_data)
            else:
                # Multiple channels returns 2D array [channels x samples]
                for i, channel in enumerate(self._task_ai_channels):
                    result.analog_input[channel] = np.array(self._acquired_data[i])
        
        if self._acquired_di_data is not None and len(self._task_di_lines) > 0:
            # Convert digital input data to dict format
            if len(self._task_di_lines) == 1:
                # Single line returns 1D array
                result.digital_input[self._task_di_lines[0]] = np.array(self._acquired_di_data, dtype=bool)
            else:
                # Multiple lines returns 2D array [lines x samples]
                for i, line in enumerate(self._task_di_lines):
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
        self._stop_live_output()
    
    def _stop_live_output(self) -> None:
        """Stop and close live output tasks (constant DC output for debugging)."""
        if self._live_ao_task is not None:
            try:
                self._live_ao_task.stop()
            except Exception:
                pass
            try:
                self._live_ao_task.close()
            except Exception:
                pass
            self._live_ao_task = None
        if self._live_do_task is not None:
            try:
                self._live_do_task.stop()
            except Exception:
                pass
            try:
                self._live_do_task.close()
            except Exception:
                pass
            self._live_do_task = None
    
    def start_live_output(
        self,
        ao_values: Optional[Dict[str, float]] = None,
        do_values: Optional[Dict[int, bool]] = None,
    ) -> None:
        """
        Output constant values to AO/DO channels for debugging. Does not modify
        waveform/pattern data. Use stop_live_output() or arm() to clear.
        """
        if ao_values is None:
            ao_values = {}
        if do_values is None:
            do_values = {}
        with self._lock:
            # Update logical live state (do not touch waveform/task definitions)
            self._live_ao_values.update(ao_values)
            self._live_do_values.update(do_values)

            self._stop_live_output()
            if not ao_values and not do_values:
                return
            device = self._config.device_name
            nidaqmx = self._nidaqmx
            constants = self._constants
            # Many devices require samps_per_chan >= 2; use 2 samples and repeat the value
            LIVE_SAMPS = 2
            try:
                if ao_values:
                    self._live_ao_task = nidaqmx.Task("live_ao")
                    for ch in ao_values:
                        phys = f"{device}/{ch}"
                        self._live_ao_task.ao_channels.add_ao_voltage_chan(
                            phys,
                            min_val=self._config.ao_min_voltage,
                            max_val=self._config.ao_max_voltage,
                        )
                    self._live_ao_task.timing.cfg_samp_clk_timing(
                        rate=1000.0,
                        sample_mode=constants.AcquisitionType.FINITE,
                        samps_per_chan=LIVE_SAMPS,
                    )
                    vals = [ao_values[ch] for ch in ao_values]
                    if len(vals) == 1:
                        self._live_ao_task.write(
                            np.full(LIVE_SAMPS, vals[0], dtype=np.float64),
                            auto_start=False,
                        )
                    else:
                        self._live_ao_task.write(
                            [np.full(LIVE_SAMPS, v, dtype=np.float64) for v in vals],
                            auto_start=False,
                        )
                    self._live_ao_task.start()
                if do_values:
                    self._live_do_task = nidaqmx.Task("live_do")
                    for line in do_values:
                        phys = f"{device}/{self._config.do_port}/line{line}"
                        self._live_do_task.do_channels.add_do_chan(phys)
                    self._live_do_task.timing.cfg_samp_clk_timing(
                        rate=1000.0,
                        sample_mode=constants.AcquisitionType.FINITE,
                        samps_per_chan=LIVE_SAMPS,
                    )
                    vals = [do_values[line] for line in do_values]
                    if len(vals) == 1:
                        self._live_do_task.write(
                            np.full(LIVE_SAMPS, bool(vals[0]), dtype=np.bool_),
                            auto_start=False,
                        )
                    else:
                        # NIDAQmx expects boolean samples for DO tasks.
                        # Using uint8 can fail when the number of digital lines
                        # in the task changes (e.g. 1 -> 2 lines).
                        per_line = np.array([
                            np.full(LIVE_SAMPS, bool(v), dtype=np.bool_)
                            for v in vals
                        ], dtype=np.bool_)
                        self._live_do_task.write(per_line, auto_start=False)
                    self._live_do_task.start()
            except Exception as e:
                self._log.error(f"Failed to start live output: {e}", exc_info=True)
                self._stop_live_output()
    
    def stop_live_output(self) -> None:
        """Stop constant live output (same as _stop_live_output but acquires lock)."""
        with self._lock:
            self._stop_live_output()

    def prepare_for_acquisition(self) -> None:
        """
        Snapshot live-output state for any endpoints that participate in the
        current task IO set so it can be restored after acquisition.
        """
        with self._lock:
            if self._has_live_overrides_for_acquisition:
                return

            # Determine which endpoints are both live-controlled and in the task IO set
            task_ao = set(self._task_ao_channels)
            task_do = set(self._task_do_lines)

            self._live_ao_overrides_for_acquisition = {
                ch: val for ch, val in self._live_ao_values.items() if ch in task_ao
            }
            self._live_do_overrides_for_acquisition = {
                line: val for line, val in self._live_do_values.items() if line in task_do
            }

            if self._live_ao_overrides_for_acquisition or self._live_do_overrides_for_acquisition:
                self._has_live_overrides_for_acquisition = True
                self._log.info(
                    "Prepared live-output overrides for acquisition on AO channels "
                    f"{sorted(self._live_ao_overrides_for_acquisition.keys())} and DO lines "
                    f"{sorted(self._live_do_overrides_for_acquisition.keys())}"
                )

    def restore_after_acquisition(self) -> None:
        """
        Restore live-output state for endpoints that were live-controlled when the
        acquisition was prepared. This uses start_live_output to reapply values
        without modifying task definitions.
        """
        with self._lock:
            if not self._has_live_overrides_for_acquisition:
                return

            ao_values = dict(self._live_ao_overrides_for_acquisition)
            do_values = dict(self._live_do_overrides_for_acquisition)

            # Clear snapshot flags before attempting to restart live output
            self._live_ao_overrides_for_acquisition.clear()
            self._live_do_overrides_for_acquisition.clear()
            self._has_live_overrides_for_acquisition = False

            if ao_values or do_values:
                # Release live-output lock while calling public API to avoid deadlock
                # (start_live_output will reacquire the lock).
                pass

        if ao_values or do_values:
            try:
                self.start_live_output(ao_values=ao_values, do_values=do_values)
                self._log.info("Restored live-output state after acquisition")
            except Exception as e:
                self._log.warning(f"Failed to restore live-output state after acquisition: {e}", exc_info=True)

    def release_tasks(self) -> None:
        """Stop and close all tasks so the device is free for new tasks (e.g. live output or re-arm)."""
        with self._lock:
            self._stop_internal()
            self._cleanup_tasks()
            self._log.info("Tasks released; device free for new tasks")
    
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
        
        # Determine the master task clock and start trigger sources
        # AO will be master, others will use AO's sample clock and (optionally) AO's start trigger
        ao_clock_terminal = f"/{device}/ao/SampleClock"
        ao_start_terminal = f"/{device}/ao/StartTrigger"
        
        # Set up Analog Output task (master clock source) for the current task IO set
        if len(self._task_ao_channels) > 0 and self._waveforms is not None:
            self._ao_task = nidaqmx.Task("ao_task")
            
            for channel in self._task_ao_channels:
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
            for channel in self._task_ao_channels:
                if channel in self._waveforms.analog_output:
                    ao_data.append(self._waveforms.analog_output[channel])
                else:
                    # Default to zeros if channel not in waveforms
                    ao_data.append(np.zeros(num_samples))
            
            if len(self._task_ao_channels) == 1:
                self._ao_task.write(ao_data[0], auto_start=False)
            else:
                self._ao_task.write(ao_data, auto_start=False)
        
        # Set up Digital Output task for the current task IO set
        if len(self._task_do_lines) > 0 and self._waveforms is not None:
            self._do_task = nidaqmx.Task("do_task")
            
            # Add all DO lines for the task
            for line in self._task_do_lines:
                physical_line = f"{device}/{self._config.do_port}/line{line}"
                self._log.info(f"Adding DO channel: {physical_line}")
                do_chan = self._do_task.do_channels.add_do_chan(physical_line)
            
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

            # Configure start trigger behavior
            # - EXTERNAL: start from external terminal (when there is no AO master)
            # - INTERNAL: optionally start from AO start trigger when AO is master
            if self._config.trigger_source == TriggerSource.EXTERNAL and self._ao_task is None:
                edge = (
                    constants.Edge.RISING
                    if self._config.trigger_edge == TriggerEdge.RISING
                    else constants.Edge.FALLING
                )
                self._do_task.triggers.start_trigger.cfg_dig_edge_start_trig(
                    trigger_source=self._config.external_trigger_terminal,
                    trigger_edge=edge,
                )
            elif self._config.trigger_source == TriggerSource.INTERNAL and self._ao_task is not None:
                # Use AO start trigger as internal start trigger for DO
                self._do_task.triggers.start_trigger.cfg_dig_edge_start_trig(
                    trigger_source=ao_start_terminal,
                    trigger_edge=constants.Edge.RISING,
                )
            
            # Build DO data array.
            # nidaqmx accepts boolean only when there is a single channel; for multiple
            # lines we must pass a 2D array of shape (num_channels, num_samples) with
            # integer dtype (e.g. uint8), not bool.
            do_data = []
            for line in self._task_do_lines:
                if line in self._waveforms.digital_output:
                    do_data.append(self._waveforms.digital_output[line].astype(bool))
                else:
                    do_data.append(np.zeros(num_samples, dtype=bool))
            
            if len(self._task_do_lines) == 1:
                self._log.info(f"Writing single line DO data: {do_data[0].shape}")
                self._do_task.write(do_data[0], auto_start=False)
            else:
                # Stack into (num_channels, num_samples) and use uint8 so multi-line write accepts it
                # do_array = np.array(do_data, dtype=np.uint8)
                do_array = np.array(do_data, dtype=np.bool)
                self._do_task.write(do_array, auto_start=False)
        
        # Set up Digital Input task for the current task IO set
        if len(self._task_di_lines) > 0:
            self._di_task = nidaqmx.Task("di_task")
            
            # Add all DI lines for the task
            for line in self._task_di_lines:
                physical_line = f"{device}/{self._config.di_port}/line{line}"
                di_chan = self._di_task.di_channels.add_di_chan(physical_line)
                # if line == 0:
                #     phys_channel = di_chan.physical_channel
                #     phys_channel.dig_port_logic_family = LogicFamily.THREE_POINT_THREE_V
                # Hard coded for FlIR Blackfly TTL lines
            
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

            # Configure start trigger behavior
            # - EXTERNAL: DI-only or DI+AI only case, use external terminal
            # - INTERNAL: when AO (preferred) or DO exists, use its start trigger
            if self._config.trigger_source == TriggerSource.EXTERNAL and self._ao_task is None and self._do_task is None:
                edge = (
                    constants.Edge.RISING
                    if self._config.trigger_edge == TriggerEdge.RISING
                    else constants.Edge.FALLING
                )
                self._di_task.triggers.start_trigger.cfg_dig_edge_start_trig(
                    trigger_source=self._config.external_trigger_terminal,
                    trigger_edge=edge,
                )
            elif self._config.trigger_source == TriggerSource.INTERNAL:
                if self._ao_task is not None:
                    # Use AO start trigger as internal start trigger for DI
                    self._di_task.triggers.start_trigger.cfg_dig_edge_start_trig(
                        trigger_source=ao_start_terminal,
                        trigger_edge=constants.Edge.RISING,
                    )
                elif self._do_task is not None:
                    # Fall back to DO start trigger if AO is not present
                    do_start_terminal = f"/{device}/do/StartTrigger"
                    self._di_task.triggers.start_trigger.cfg_dig_edge_start_trig(
                        trigger_source=do_start_terminal,
                        trigger_edge=constants.Edge.RISING,
                    )
        
        # Set up Analog Input task for the current task IO set
        if len(self._task_ai_channels) > 0:
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

            for channel in self._task_ai_channels:
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

            # Configure start trigger behavior
            # - EXTERNAL: AI-only or AI+DI only case, use external terminal
            # - INTERNAL: when AO (preferred), DO, or DI exists, use its start trigger
            if self._config.trigger_source == TriggerSource.EXTERNAL and self._ao_task is None and self._do_task is None and self._di_task is None:
                edge = (
                    constants.Edge.RISING
                    if self._config.trigger_edge == TriggerEdge.RISING
                    else constants.Edge.FALLING
                )
                self._ai_task.triggers.start_trigger.cfg_dig_edge_start_trig(
                    trigger_source=self._config.external_trigger_terminal,
                    trigger_edge=edge,
                )
            elif self._config.trigger_source == TriggerSource.INTERNAL:
                if self._ao_task is not None:
                    # Use AO start trigger as internal start trigger for AI
                    self._ai_task.triggers.start_trigger.cfg_dig_edge_start_trig(
                        trigger_source=ao_start_terminal,
                        trigger_edge=constants.Edge.RISING,
                    )
                elif self._do_task is not None:
                    # Fall back to DO start trigger if AO is not present
                    do_start_terminal = f"/{device}/do/StartTrigger"
                    self._ai_task.triggers.start_trigger.cfg_dig_edge_start_trig(
                        trigger_source=do_start_terminal,
                        trigger_edge=constants.Edge.RISING,
                    )
                elif self._di_task is not None:
                    # Fall back to DI start trigger if AO/DO are not present
                    di_start_terminal = f"/{device}/di/StartTrigger"
                    self._ai_task.triggers.start_trigger.cfg_dig_edge_start_trig(
                        trigger_source=di_start_terminal,
                        trigger_edge=constants.Edge.RISING,
                    )


class SimulatedNIDAQ(AbstractNIDAQ):
    """
    Simulated NI DAQ for testing without hardware.
    
    This class simulates the behavior of a real NI DAQ, generating
    synthetic input data based on the output waveforms.
    """
    
    def __init__(self, **config: Any):
        super().__init__(**config)
        
        self._waveforms: Optional[WaveformData] = None
        self._acquired_data: Dict[str, np.ndarray] = {}
        self._acquired_di_data: Optional[np.ndarray] = None
        
        self._lock = threading.Lock()
        self._completion_event = threading.Event()
        self.configure(config)
    
    def configure(self, **config: Any) -> None:
        """Update the configuration."""
        with self._lock:
            if self._is_running:
                raise RuntimeError("Cannot configure while tasks are running")
            for key, value in config.items():
                if not hasattr(self, key):
                    raise ValueError(f"Unknown NI-DAQ config key: {key}")
                setattr(self, key, value)

            if "trigger_source" in config and isinstance(self.trigger_source, str):
                self.trigger_source = TriggerSource[self.trigger_source]
            if "trigger_edge" in config and isinstance(self.trigger_edge, str):
                self.trigger_edge = TriggerEdge[self.trigger_edge]
    
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

    def start_live_output(
        self,
        ao_values: Optional[Dict[str, float]] = None,
        do_values: Optional[Dict[int, bool]] = None,
    ) -> None:
        """
        Record logical live-output state for simulation.

        No hardware tasks are created; this simply updates the live state
        dictionaries so higher layers see consistent behavior.
        """
        if ao_values is None:
            ao_values = {}
        if do_values is None:
            do_values = {}
        with self._lock:
            self._live_ao_values.update(ao_values)
            self._live_do_values.update(do_values)

    def stop_live_output(self) -> None:
        """Clear simulation live-output tasks (logical state is preserved)."""
        # For simulation we don't need to do anything beyond existing state,
        # but we keep the method for API symmetry.
        self._log.info("[SIM] stop_live_output called (no hardware tasks to stop)")

    def prepare_for_acquisition(self) -> None:
        """
        Mirror NIDAQ behavior: snapshot live-output state for task endpoints.
        """
        with self._lock:
            task_ao = set(self._task_ao_channels)
            task_do = set(self._task_do_lines)
            self._live_ao_overrides_for_acquisition = {
                ch: val for ch, val in self._live_ao_values.items() if ch in task_ao
            }
            self._live_do_overrides_for_acquisition = {
                line: val for line, val in self._live_do_values.items() if line in task_do
            }

    def restore_after_acquisition(self) -> None:
        """
        Mirror NIDAQ behavior: restore recorded live-output state for task endpoints.
        """
        with self._lock:
            if not self._live_ao_overrides_for_acquisition and not self._live_do_overrides_for_acquisition:
                return
            self._live_ao_values.update(self._live_ao_overrides_for_acquisition)
            self._live_do_values.update(self._live_do_overrides_for_acquisition)
            self._live_ao_overrides_for_acquisition.clear()
            self._live_do_overrides_for_acquisition.clear()
    
    def get_acquired_data(self) -> AcquisitionResult:
        """Get the acquired analog and digital input data."""
        result = AcquisitionResult(
            analog_input=self._acquired_data.copy(),
            sample_rate_hz=self._config.sample_rate_hz,
            samples_acquired=self._config.samples_per_channel,
        )
        
        # Add digital input data if available
        if self._acquired_di_data is not None and len(self._config.di_lines) > 0:
            if len(self._config.di_lines) == 1:
                result.digital_input[self._config.di_lines[0]] = np.array(self._acquired_di_data, dtype=bool)
            else:
                for i, line in enumerate(self._config.di_lines):
                    result.digital_input[line] = np.array(self._acquired_di_data[i], dtype=bool)

        # For simulation, propagate configured output waveforms as "acquired" output
        if self._waveforms is not None:
            result.analog_output = self._waveforms.analog_output.copy()
            result.digital_output = self._waveforms.digital_output.copy()
            result.analog_output_channels = list(self._waveforms.analog_output.keys())
            result.digital_output_lines = list(self._waveforms.digital_output.keys())
        
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


def create_ni_daq(config: Mapping[str, Any], simulation: bool = False) -> AbstractNIDAQ:
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
        return SimulatedNIDAQ(**config)
    else:
        _log.info(f"Creating NI DAQ for device {config.get('device_name', 'Dev1')}")
        return NIDAQ(**config)


def build_nidaq_config_from_io(
    device_name: str,
    base_config: Optional[Dict[str, object]],
    io_config: IOEndpointConfig,
) -> Dict[str, Any]:
    """
    Build a configuration dict from a base config dict and IO endpoints.

    This helper:
    - Uses ``device_name`` and ``base_config`` (from MachineConfig.nidaq.config)
      for global settings like sample_rate and logic_family.
    - Scans IO endpoints for controller==NIDAQ to auto-populate:
        * ao_channels:  channel_id like \"ao0\", \"ao1\" (analog outputs)
        * ai_channels:  channel_id like \"ai0\" (analog inputs)
        * do_port/do_lines:  channel_id like \"port0/line5\" (digital outputs)
        * di_port/di_lines:  channel_id like \"port0/line6\" (digital inputs)

    The resulting dict can be passed directly to ``create_ni_daq``.
    """
    base_config = base_config or {}

    sample_rate_hz = float(base_config.get("sample_rate", 10000.0))
    samples_per_channel = int(base_config.get("samples_per_channel", 1000))
    ai_terminal_config = str(base_config.get("ai_terminal_config", "RSE"))
    trigger_source = str(base_config.get("trigger_source", "SOFTWARE"))
    external_trigger_terminal = str(
        base_config.get("external_trigger_terminal", f"/{device_name}/PFI0")
    )
    trigger_edge = str(base_config.get("trigger_edge", "RISING"))
    continuous = bool(base_config.get("continuous", False))
    do_logic_family = str(base_config.get("logic_family", NI_DAQ_LOGIC_FAMILY))

    ao_channels: Set[str] = set()
    ai_channels: Set[str] = set()
    do_ports: Dict[str, Set[int]] = {}
    di_ports: Dict[str, Set[int]] = {}

    for ep in io_config.get_controller_endpoints(IOControllerType.NIDAQ):
        cid = ep.channel_id

        # Analog outputs: "ao0", "ao1", ...
        if ep.signal_type == IOSignalType.ANALOG and ep.direction == IODirection.OUTPUT:
            if cid.startswith("ao"):
                ao_channels.add(cid)
            continue

        # Analog inputs: "ai0", "ai1", ...
        if ep.signal_type == IOSignalType.ANALOG and ep.direction == IODirection.INPUT:
            if cid.startswith("ai"):
                ai_channels.add(cid)
            continue

        # Digital lines: "portP/lineL"
        if ep.signal_type == IOSignalType.DIGITAL:
            if "port" in cid and "/line" in cid:
                try:
                    port_part, line_part = cid.split("/", 1)
                    port_name = port_part  # e.g. "port0"
                    line_idx = int(line_part.replace("line", ""))
                except Exception:
                    _log.warning(f"Could not parse NIDAQ channel_id '{cid}' for endpoint '{ep.name}'")
                    continue

                if ep.direction == IODirection.OUTPUT:
                    do_ports.setdefault(port_name, set()).add(line_idx)
                elif ep.direction == IODirection.INPUT:
                    di_ports.setdefault(port_name, set()).add(line_idx)

    def _select_port_and_lines(ports: Dict[str, Set[int]]) -> Tuple[Optional[str], List[int]]:
        if not ports:
            return None, []
        if len(ports) > 1:
            _log.warning(
                f"Multiple NIDAQ ports referenced ({list(ports.keys())}); "
                f"using '{sorted(ports.keys())[0]}' for legacy single-port config."
            )
        port = sorted(ports.keys())[0]
        lines = sorted(ports[port])
        return port, lines

    do_port, do_lines = _select_port_and_lines(do_ports)
    di_port, di_lines = _select_port_and_lines(di_ports)

    return {
        "device_name": device_name,
        "sample_rate_hz": sample_rate_hz,
        "samples_per_channel": samples_per_channel,
        "ao_channels": sorted(ao_channels),
        "ao_min_voltage": float(base_config.get("ao_min_voltage", -10.0)),
        "ao_max_voltage": float(base_config.get("ao_max_voltage", 10.0)),
        "do_port": do_port or "port0",
        "do_lines": do_lines,
        "di_port": di_port or "port0",
        "di_lines": di_lines,
        "ai_channels": sorted(ai_channels),
        "ai_min_voltage": float(base_config.get("ai_min_voltage", -10.0)),
        "ai_max_voltage": float(base_config.get("ai_max_voltage", 10.0)),
        "ai_terminal_config": ai_terminal_config,
        "trigger_source": trigger_source,
        "external_trigger_terminal": external_trigger_terminal,
        "trigger_edge": trigger_edge,
        "continuous": continuous,
        "do_logic_family": do_logic_family,
    }


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

def generate_staircase_ramp(
    amplitude: float,
    ramp_duration_s: float,
    delay_start_s: float,
    delay_ramp_s: float,
    n_staircase_steps: int,
    sample_rate_hz: float,
    num_samples: int,
) -> np.ndarray:
    """Generate a step and ramp."""
    n_staircase_steps = int(n_staircase_steps)
    delay1 = np.zeros(int(np.ceil(delay_start_s * sample_rate_hz)))
    delay2 = np.zeros(int(np.ceil(delay_ramp_s * sample_rate_hz)))

    ramp_duration_samples = int(np.ceil(ramp_duration_s * sample_rate_hz))
    ramp_up = np.linspace(0, 1, ramp_duration_samples)
    ramp_down = np.linspace(1, 0, ramp_duration_samples)
    
    staircase_increment_samples = int(np.ceil(ramp_duration_s * sample_rate_hz / n_staircase_steps))
    staircase_ramp_up = np.zeros(ramp_duration_samples)
    for i in range(n_staircase_steps):
        staircase_ramp_up[i * staircase_increment_samples:(i + 1) * staircase_increment_samples] = (i+1)/n_staircase_steps
    staircase_ramp_down = np.flip(staircase_ramp_up)

    waveform = np.concatenate([delay1, staircase_ramp_up, staircase_ramp_down, delay2, ramp_up, ramp_down])
    if num_samples - len(waveform) < 0:
        waveform = waveform[:num_samples]
    else:
        trailing_zeros = np.zeros(num_samples - len(waveform))
        waveform = np.concatenate([waveform, trailing_zeros])
    waveform = waveform * amplitude
    return waveform

def generate_pulse_train(
    pulse_width_samples: int,
    period_samples: int,
    num_samples: int,
    n_samples_offset: int = 0,
    inverted: bool = False,
    max_num_pulses: int = None,
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
    num_pulses = 0
    for start in range(n_samples_offset, num_samples, period_samples):
        end = min(start + pulse_width_samples, num_samples)
        pattern[start:end] = True
        num_pulses += 1
        if max_num_pulses is not None and num_pulses >= max_num_pulses:
            break

    if inverted:
        pattern = ~pattern
    
    return pattern
