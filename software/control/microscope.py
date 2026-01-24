"""
Core microscope control module.

This module provides the main Microscope class that coordinates all microscope components:
- Stage positioning (X, Y, Z, Theta)
- Camera acquisition (main and focus cameras)
- Illumination control
- Optional addons (filter wheels, piezo stages, fluidics, etc.)

The Microscope class acts as the central coordinator, providing a unified interface
for high-level operations like image acquisition, stage movement, and illumination control.
It manages the lifecycle of all hardware components and handles their initialization.

Architecture:
- Microscope: Main class that coordinates all components
- MicroscopeAddons: Optional hardware components (filter wheels, piezo, etc.)
- LowLevelDrivers: Direct hardware interfaces (microcontroller, stage drivers)
"""

import serial
from typing import Optional, TypeVar

import control._def
from control._def import TriggerMode, NIDAQ_CONFIG
from control.core.channel_configuration_mananger import ChannelConfigurationManager
from control.core.configuration_mananger import ConfigurationManager
from control.core.contrast_manager import ContrastManager
from control.core.laser_af_settings_manager import LaserAFSettingManager
from control.core.live_controller import LiveController
from control.core.objective_store import ObjectiveStore
from control.core.stream_handler import StreamHandler, StreamHandlerFunctions, NoOpStreamHandlerFunctions

from control.lighting import LightSourceType, IntensityControlMode, ShutterControlMode, IlluminationController
from control.microcontroller import Microcontroller
from control.piezo import PiezoStage
from control.serial_peripherals import SciMicroscopyLEDArray
from squid.abc import CameraAcquisitionMode, AbstractCamera, AbstractStage, AbstractFilterWheelController
from squid.stage.utils import move_z_axis_to_safety_position
from squid.stage.cephla import CephlaStage
from squid.stage.prior import PriorStage
import control.celesta
import control.illumination_andor
import control.microcontroller
import control.serial_peripherals as serial_peripherals
import squid.camera.utils
import squid.config
import squid.filter_wheel_controller.utils
import squid.logging
import squid.stage.cephla
import squid.stage.utils
from control.ni_daq import AbstractNIDAQ, NIDAQ

if control._def.USE_XERYON:
    from control.objective_changer_2_pos_controller import (
        ObjectiveChanger2PosController,
        ObjectiveChanger2PosController_Simulation,
    )
else:
    ObjectiveChanger2PosController = TypeVar("ObjectiveChanger2PosController")

if control._def.RUN_FLUIDICS:
    from control.fluidics import Fluidics
else:
    Fluidics = TypeVar("Fluidics")

if control._def.ENABLE_NL5:
    import control.NL5 as NL5
else:
    NL5 = TypeVar("NL5")


class MicroscopeAddons:
    """
    Optional hardware components that may be present on the microscope.
    
    These include:
    - XLight/Cicero: Spinning disk confocal system
    - Dragonfly: Alternative confocal system
    - NL5: Laser combiner
    - CellX: Cell culture system
    - Emission filter wheel: For multi-color fluorescence
    - Objective changer: For switching between objectives
    - Focus camera: For autofocus or displacement measurement
    - Fluidics: For automated sample handling
    - Piezo stage: For fine Z positioning
    - SciMicroscopy LED array: For brightfield illumination
    - NIDAQ: For hardware triggering
    """
    @staticmethod
    def build_from_global_config(
        stage: AbstractStage, micro: Optional[Microcontroller], simulated: bool = False
    ) -> "MicroscopeAddons":
        """
        Build MicroscopeAddons from global configuration.
        
        Args:
            stage: Stage instance (needed for objective changer)
            micro: Microcontroller instance (needed for piezo control)
            simulated: If True, create simulated hardware
            
        Returns:
            MicroscopeAddons instance with all configured addons
        """

        # XLight/Cicero: Spinning disk confocal system for high-speed imaging
        xlight = None
        if control._def.ENABLE_SPINNING_DISK_CONFOCAL and not control._def.USE_DRAGONFLY:
            # TODO: For user compatibility, when ENABLE_SPINNING_DISK_CONFOCAL is True, we use XLight/Cicero on default.
            # This needs to be changed when we figure out better machine configuration structure.
            xlight = (
                serial_peripherals.XLight(control._def.XLIGHT_SERIAL_NUMBER, control._def.XLIGHT_SLEEP_TIME_FOR_WHEEL)
                if not simulated
                else serial_peripherals.XLight_Simulation()
            )

        # Dragonfly: Alternative spinning disk confocal system
        dragonfly = None
        if control._def.ENABLE_SPINNING_DISK_CONFOCAL and control._def.USE_DRAGONFLY:
            dragonfly = (
                serial_peripherals.Dragonfly(SN=control._def.DRAGONFLY_SERIAL_NUMBER)
                if not simulated
                else serial_peripherals.Dragonfly_Simulation()
            )

        # NL5: Laser combiner for multiple laser lines
        nl5 = None
        if control._def.ENABLE_NL5:
            nl5 = NL5.NL5() if not simulated else NL5.NL5_Simulation()

        # CellX: Automated cell culture system
        cellx = None
        if control._def.ENABLE_CELLX:
            cellx = (
                serial_peripherals.CellX(control._def.CELLX_SN)
                if not simulated
                else serial_peripherals.CellX_Simulation()
            )

        # Emission filter wheel: Selects emission filters for multi-color fluorescence
        emission_filter_wheel = None
        fw_config = squid.config.get_filter_wheel_config()
        if fw_config:
            emission_filter_wheel = squid.filter_wheel_controller.utils.get_filter_wheel_controller(
                fw_config, microcontroller=micro, simulated=simulated
            )

        # Objective changer: Automatically switches between objectives (e.g., 10x and 20x)
        objective_changer = None
        if control._def.USE_XERYON:
            objective_changer = (
                ObjectiveChanger2PosController(sn=control._def.XERYON_SERIAL_NUMBER, stage=stage)
                if not simulated
                else ObjectiveChanger2PosController_Simulation(sn=control._def.XERYON_SERIAL_NUMBER, stage=stage)
            )

        # Focus camera: Separate camera for autofocus or laser-based displacement measurement
        camera_focus = None
        if control._def.SUPPORT_LASER_AUTOFOCUS:
            camera_focus = squid.camera.utils.get_camera(
                squid.config.get_autofocus_camera_config(), simulated=simulated
            )

        # Fluidics: Automated sample handling and reagent delivery
        fluidics = None
        if control._def.RUN_FLUIDICS:
            fluidics = Fluidics(config_path=control._def.FLUIDICS_CONFIG_PATH, simulation=simulated)

        # Piezo stage: Fine Z positioning (typically mounted on objective)
        # Provides sub-micron precision for focus control
        piezo_stage = None
        if control._def.HAS_OBJECTIVE_PIEZO:
            if not micro:
                raise ValueError("Cannot create PiezoStage without a Microcontroller.")
            piezo_stage = PiezoStage(
                microcontroller=micro,
                config={
                    "OBJECTIVE_PIEZO_HOME_UM": control._def.OBJECTIVE_PIEZO_HOME_UM,
                    "OBJECTIVE_PIEZO_RANGE_UM": control._def.OBJECTIVE_PIEZO_RANGE_UM,
                    "OBJECTIVE_PIEZO_CONTROL_VOLTAGE_RANGE": control._def.OBJECTIVE_PIEZO_CONTROL_VOLTAGE_RANGE,
                    "OBJECTIVE_PIEZO_FLIP_DIR": control._def.OBJECTIVE_PIEZO_FLIP_DIR,
                },
            )

        # SciMicroscopy LED array: RGB LED array for brightfield illumination
        sci_microscopy_led_array = None
        if control._def.SUPPORT_SCIMICROSCOPY_LED_ARRAY:
            # to do: add error handling
            sci_microscopy_led_array = serial_peripherals.SciMicroscopyLEDArray(
                control._def.SCIMICROSCOPY_LED_ARRAY_SN,
                control._def.SCIMICROSCOPY_LED_ARRAY_DISTANCE,
                control._def.SCIMICROSCOPY_LED_ARRAY_TURN_ON_DELAY,
            )
            sci_microscopy_led_array.set_NA(control._def.SCIMICROSCOPY_LED_ARRAY_DEFAULT_NA)

        # NIDAQ: For hardware triggering
        nidaq = None
        if control._def.ENABLE_NIDAQ and ((not simulated) or control._def.NI_DAQ_BYPASS_SIMULATION):
            nidaq = NIDAQ(config=NIDAQ_CONFIG())

        return MicroscopeAddons(
            xlight,
            dragonfly,
            nl5,
            cellx,
            emission_filter_wheel,
            objective_changer,
            camera_focus,
            fluidics,
            piezo_stage,
            sci_microscopy_led_array,
            nidaq
        )

    def __init__(
        self,
        xlight: Optional[serial_peripherals.XLight] = None,
        dragonfly: Optional[serial_peripherals.Dragonfly] = None,
        nl5: Optional[NL5] = None,
        cellx: Optional[serial_peripherals.CellX] = None,
        emission_filter_wheel: Optional[AbstractFilterWheelController] = None,
        objective_changer: Optional[ObjectiveChanger2PosController] = None,
        camera_focus: Optional[AbstractCamera] = None,
        fluidics: Optional[Fluidics] = None,
        piezo_stage: Optional[PiezoStage] = None,
        sci_microscopy_led_array: Optional[SciMicroscopyLEDArray] = None,
        nidaq: Optional[AbstractNIDAQ] = None,
    ):
        self.xlight: Optional[serial_peripherals.XLight] = xlight
        self.dragonfly: Optional[serial_peripherals.Dragonfly] = dragonfly
        self.nl5: Optional[NL5] = nl5
        self.cellx: Optional[serial_peripherals.CellX] = cellx
        self.emission_filter_wheel = emission_filter_wheel
        self.objective_changer = objective_changer
        self.camera_focus: Optional[AbstractCamera] = camera_focus
        self.fluidics = fluidics
        self.piezo_stage = piezo_stage
        self.sci_microscopy_led_array = sci_microscopy_led_array
        self.nidaq = nidaq

    def prepare_for_use(self):
        """
        Prepare all the addon hardware for immediate use.
        """
        if self.emission_filter_wheel:
            fw_config = squid.config.get_filter_wheel_config()
            self.emission_filter_wheel.initialize(fw_config.indices)
            self.emission_filter_wheel.home()
        if self.piezo_stage:
            self.piezo_stage.home()


class LowLevelDrivers:
    """
    Low-level hardware drivers for direct hardware control.
    
    This class manages the microcontroller interface, which provides:
    - Stage motor control (stepper drivers)
    - DAC output for illumination and piezo control
    - TTL I/O for shutters and triggers
    - Hardware trigger generation for synchronized acquisition
    """
    @staticmethod
    def build_from_global_config(simulated: bool = False) -> "LowLevelDrivers":
        """
        Build LowLevelDrivers from global configuration.
        
        Args:
            simulated: If True, create simulated microcontroller
            
        Returns:
            LowLevelDrivers instance
        """
        # Find and connect to the microcontroller (Teensy board)
        micro_serial_device = (
            control.microcontroller.get_microcontroller_serial_device(
                version=control._def.CONTROLLER_VERSION, sn=control._def.CONTROLLER_SN
            )
            if not simulated
            else control.microcontroller.get_microcontroller_serial_device(simulated=True)
        )
        micro = control.microcontroller.Microcontroller(serial_device=micro_serial_device)

        return LowLevelDrivers(microcontroller=micro)

    def __init__(self, microcontroller: Optional[Microcontroller] = None):
        self.microcontroller: Optional[Microcontroller] = microcontroller

    def prepare_for_use(self):
        """
        Configure hardware for use. Sets up DAC gains for piezo control.
        
        The DAC80508 has 8 channels. Channel 7 is typically used for objective piezo.
        Gain settings determine the output voltage range (2.5V or 5V).
        """
        if self.microcontroller and control._def.HAS_OBJECTIVE_PIEZO:
            # Configure DAC gains for objective piezo
            # If piezo requires 5V range, enable gain on channel 7
            control._def.OUTPUT_GAINS.CHANNEL7_GAIN = control._def.OBJECTIVE_PIEZO_CONTROL_VOLTAGE_RANGE == 5
            # Reference divider: 0 = no divide (2.5V ref), 1 = divide by 2 (1.25V ref)
            div = 1 if control._def.OUTPUT_GAINS.REFDIV else 0
            # Pack gain bits for all 8 channels into a single byte
            gains = sum(getattr(control._def.OUTPUT_GAINS, f"CHANNEL{i}_GAIN") << i for i in range(8))
            self.microcontroller.configure_dac80508_refdiv_and_gain(div, gains)


class Microscope:
    """
    Main microscope control class.
    
    This class coordinates all microscope components and provides high-level
    operations for:
    - Image acquisition (single images and live streaming)
    - Stage positioning and movement
    - Illumination control
    - Autofocus
    - Multi-point acquisition
    
    The Microscope class manages:
    - Stage: X, Y, Z, Theta positioning
    - Camera: Main acquisition camera
    - IlluminationController: LED/laser control
    - Addons: Optional components (filter wheels, piezo, etc.)
    - Configuration managers: Channel settings, objectives, autofocus parameters
    """
    @staticmethod
    def build_from_global_config(simulated: bool = False):
        """
        Build a complete Microscope instance from global configuration.
        
        This factory method:
        1. Creates low-level drivers (microcontroller)
        2. Creates stage (Prior or Cephla)
        3. Creates optional addons
        4. Creates camera with hardware trigger support
        5. Creates illumination controller
        6. Wires everything together
        
        Args:
            simulated: If True, use simulated hardware
            
        Returns:
            Fully configured Microscope instance
        """
        # Create low-level hardware drivers (microcontroller for stage control, DAC, etc.)
        low_level_devices = LowLevelDrivers.build_from_global_config(simulated)

        # Create stage: Prior (external controller) or Cephla (integrated with microcontroller)
        stage_config = squid.config.get_stage_config()
        if control._def.USE_PRIOR_STAGE:
            # Prior stage uses its own controller via serial communication
            stage = PriorStage(sn=control._def.PRIOR_STAGE_SN, stage_config=stage_config)
        else:
            # Cephla stage is controlled directly by the microcontroller
            if low_level_devices.microcontroller is None:
                raise ValueError("For a cephla stage microscope, you must provide a microcontroller.")
            stage = CephlaStage(low_level_devices.microcontroller, stage_config)

        # Create optional addon components
        addons = MicroscopeAddons.build_from_global_config(
            stage, low_level_devices.microcontroller, simulated=simulated
        )

        # Set up hardware trigger functions for synchronized acquisition
        # Hardware triggering allows precise timing between illumination and camera exposure
        cam_trigger_log = squid.logging.get_logger("camera hw functions")

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
            if addons.nl5 and control._def.NL5_USE_DOUT:
                # Use NL5 laser combiner's digital output for triggering
                addons.nl5.start_acquisition()
            else:
                # Use microcontroller to send hardware trigger
                # Convert illumination time from ms to microseconds
                illumination_time_us = 1000.0 * illumination_time if illumination_time else 0
                cam_trigger_log.debug(
                    f"Sending hw trigger with illumination_time={illumination_time_us if illumination_time else None} [us]"
                )
                # Send trigger: control_illumination=True means turn on LED during exposure
                low_level_devices.microcontroller.send_hardware_trigger(
                    True if illumination_time else False, illumination_time_us
                )
            return True

        def acquisition_camera_hw_strobe_delay_fn(strobe_delay_ms: float) -> bool:
            """
            Set the strobe delay for hardware-triggered acquisition.
            
            Strobe delay is the time between trigger signal and illumination turn-on.
            This allows fine-tuning of illumination timing relative to camera exposure.
            
            Args:
                strobe_delay_ms: Delay in milliseconds
            """
            strobe_delay_us = int(1000 * strobe_delay_ms)
            cam_trigger_log.debug(f"Setting microcontroller strobe delay to {strobe_delay_us} [us]")
            low_level_devices.microcontroller.set_strobe_delay_us(strobe_delay_us)
            low_level_devices.microcontroller.wait_till_operation_is_completed()

            return True

        # Create camera with hardware trigger support
        # The camera will call hw_trigger_fn when it needs to start acquisition
        if control._def.CAMERA_BYPASS_SIMULATION:
            simulated = False
        
        camera = squid.camera.utils.get_camera(
            config=squid.config.get_camera_config(),
            simulated=simulated,
            hw_trigger_fn=acquisition_camera_hw_trigger_fn,
            hw_set_strobe_delay_ms_fn=acquisition_camera_hw_strobe_delay_fn,
        )

        # Create illumination controller based on configured light source
        if control._def.USE_LDI_SERIAL_CONTROL and not simulated:
            # Lumencor Light Engine (LDI) - controlled via serial communication
            ldi = serial_peripherals.LDI()
            illumination_controller = IlluminationController(
                low_level_devices.microcontroller, ldi.intensity_mode, ldi.shutter_mode, LightSourceType.LDI, ldi
            )
        elif control._def.USE_CELESTA_ETHERNET_CONTROL and not simulated:
            # Lumencor CELESTA - controlled via Ethernet
            celesta = control.celesta.CELESTA()
            illumination_controller = IlluminationController(
                low_level_devices.microcontroller,
                IntensityControlMode.Software,
                ShutterControlMode.TTL,
                LightSourceType.CELESTA,
                celesta,
            )
        elif control._def.USE_ANDOR_LASER_CONTROL and not simulated:
            # Andor laser system - controlled via USB
            andor_laser = control.illumination_andor.AndorLaser(
                control._def.ANDOR_LASER_VID, control._def.ANDOR_LASER_PID
            )
            illumination_controller = IlluminationController(
                low_level_devices.microcontroller,
                IntensityControlMode.Software,
                ShutterControlMode.TTL,
                LightSourceType.AndorLaser,
                andor_laser,
            )
        else:
            # Default: Built-in LEDs/lasers controlled via DAC on microcontroller
            illumination_controller = IlluminationController(low_level_devices.microcontroller)

        return Microscope(
            stage=stage,
            camera=camera,
            illumination_controller=illumination_controller,
            addons=addons,
            low_level_drivers=low_level_devices,
            simulated=simulated,
        )

    def __init__(
        self,
        stage: AbstractStage,
        camera: AbstractCamera,
        illumination_controller: IlluminationController,
        addons: MicroscopeAddons,
        low_level_drivers: LowLevelDrivers,
        stream_handler_callbacks: Optional[StreamHandlerFunctions] = NoOpStreamHandlerFunctions,
        simulated: bool = False,
        skip_prepare_for_use: bool = False,
    ):
        """
        Initialize the Microscope with all components.
        
        Args:
            stage: Stage for X, Y, Z, Theta positioning
            camera: Main acquisition camera
            illumination_controller: Controller for LEDs/lasers
            addons: Optional hardware components
            low_level_drivers: Direct hardware interfaces
            stream_handler_callbacks: Callbacks for processing camera frames
            simulated: Whether using simulated hardware
            skip_prepare_for_use: Skip hardware initialization (for testing)
        """
        super().__init__()
        self._log = squid.logging.get_logger(self.__class__.__name__)

        # Core hardware components
        self.stage: AbstractStage = stage
        self.camera: AbstractCamera = camera
        self.illumination_controller: IlluminationController = illumination_controller

        # Optional components and drivers
        self.addons = addons
        self.low_level_drivers = low_level_drivers

        self._simulated = simulated

        # Configuration and state management
        # ObjectiveStore: Tracks current objective and its properties (NA, magnification, etc.)
        self.objective_store: ObjectiveStore = ObjectiveStore()
        # ChannelConfigurationManager: Manages imaging channel settings (exposure, intensity, filters)
        self.channel_configuration_manager: ChannelConfigurationManager = ChannelConfigurationManager()
        # LaserAFSettingManager: Settings for laser-based autofocus
        self.laser_af_settings_manager: Optional[LaserAFSettingManager] = None
        if control._def.SUPPORT_LASER_AUTOFOCUS:
            self.laser_af_settings_manager = LaserAFSettingManager()

        # ConfigurationManager: Coordinates all configuration settings
        self.configuration_manager: ConfigurationManager = ConfigurationManager(
            self.channel_configuration_manager, self.laser_af_settings_manager
        )
        # ContrastManager: Manages image contrast/brightness settings
        self.contrast_manager: ContrastManager = ContrastManager()
        # StreamHandler: Processes camera frames and routes them to callbacks
        self.stream_handler: StreamHandler = StreamHandler(handler_functions=stream_handler_callbacks)

        # Focus camera setup (if available)
        # Used for laser autofocus or displacement measurement
        self.stream_handler_focus: Optional[StreamHandler] = None
        self.live_controller_focus: Optional[LiveController] = None
        if self.addons.camera_focus:
            self.stream_handler_focus = StreamHandler(handler_functions=NoOpStreamHandlerFunctions)
            self.live_controller_focus = LiveController(
                microscope=self,
                camera=self.addons.camera_focus,
                control_illumination=False,  # Focus camera doesn't control illumination
                for_displacement_measurement=True,  # Used for laser spot detection
            )

        # Live controller for main camera
        # Handles live image streaming, illumination control, and trigger modes
        self.live_controller: LiveController = LiveController(microscope=self, camera=self.camera)

        # Initialize hardware (set pixel formats, acquisition modes, etc.)
        if not skip_prepare_for_use:
            self._prepare_for_use()

    def _prepare_for_use(self):
        """
        Initialize all hardware components for use.
        
        This method:
        - Configures DAC gains for piezo control
        - Initializes filter wheels and other addons
        - Sets camera pixel formats and acquisition modes
        """
        # Configure low-level drivers (DAC settings, etc.)
        self.low_level_drivers.prepare_for_use()
        # Initialize addon components (filter wheels, piezo, etc.)
        self.addons.prepare_for_use()

        # Configure main camera
        # Set pixel format (MONO8, MONO16, etc.) from configuration

        self.camera.set_pixel_format(
            squid.config.CameraPixelFormat.from_string(control._def.CAMERA_CONFIG.PIXEL_FORMAT_DEFAULT)
        )

        # Start with software trigger mode (can be changed to hardware trigger later)
        if control._def.DEFAULT_TRIGGER_MODE == TriggerMode.SOFTWARE:
            self.camera.set_acquisition_mode(CameraAcquisitionMode.SOFTWARE_TRIGGER)
        elif control._def.DEFAULT_TRIGGER_MODE == TriggerMode.HARDWARE:
            self.camera.set_acquisition_mode(CameraAcquisitionMode.HARDWARE_TRIGGER)
        elif control._def.DEFAULT_TRIGGER_MODE == TriggerMode.CONTINUOUS:
            self.camera.set_acquisition_mode(CameraAcquisitionMode.CONTINUOUS)
        else:
            raise ValueError(f"Invalid trigger mode: {control._def.DEFAULT_TRIGGER_MODE}")


        # Configure focus camera if available
        if self.addons.camera_focus:
            # Focus camera typically uses 8-bit format for faster processing
            self.addons.camera_focus.set_pixel_format(squid.config.CameraPixelFormat.from_string("MONO8"))
            self.addons.camera_focus.set_acquisition_mode(CameraAcquisitionMode.SOFTWARE_TRIGGER)

    def update_camera_functions(self, functions: StreamHandlerFunctions):
        self.stream_handler.set_functions(functions)

    def update_camera_focus_functions(self, functions: StreamHandlerFunctions):
        if not self.addons.camera_focus:
            raise ValueError("No focus camera, cannot change its stream handler functions.")

        self.stream_handler_focus.set_functions(functions)

    def initialize_core_components(self):
        if self.addons.piezo_stage:
            self.addons.piezo_stage.home()

    def setup_hardware(self):
        self.camera.add_frame_callback(self.stream_handler.on_new_frame)
        self.camera.enable_callbacks(True)

        if self.addons.camera_focus:
            self.addons.camera_focus.add_frame_callback(self.stream_handler_focus.on_new_frame)
            self.addons.camera_focus.enable_callbacks(True)
            self.addons.camera_focus.start_streaming()

    def acquire_image(self):
        """
        Acquire a single image from the camera.
        
        This method handles both software and hardware trigger modes:
        - Software trigger: Manually turn on illumination, trigger camera, read frame, turn off illumination
        - Hardware trigger: Send synchronized trigger signal that controls both camera and illumination
        
        Returns:
            Image array from camera, or None if acquisition failed
        """
        # Turn on illumination and send trigger
        if self.live_controller.trigger_mode == control._def.TriggerMode.SOFTWARE:
            # Software trigger: Manual control sequence
            self.live_controller.turn_on_illumination()
            self.waitForMicrocontroller()  # Wait for illumination to stabilize
            self.camera.send_trigger()  # Trigger camera exposure
        elif self.live_controller.trigger_mode == control._def.TriggerMode.HARDWARE:
            # Hardware trigger: Synchronized via microcontroller
            # Microcontroller sends trigger to camera and controls illumination timing
            self.low_level_drivers.microcontroller.send_hardware_trigger(
                control_illumination=True, illumination_on_time_us=self.camera.get_exposure_time() * 1000
            )

        # Read a frame from camera
        image = self.camera.read_frame()
        if image is None:
            print("self.camera.read_frame() returned None")

        # Turn off the illumination if using software trigger
        # (Hardware trigger automatically handles illumination timing)
        if self.live_controller.trigger_mode == control._def.TriggerMode.SOFTWARE:
            self.live_controller.turn_off_illumination()

        return image

    def home_xyz(self):
        """
        Home the X, Y, and Z axes of the stage.
        
        Homing moves the stage to its reference position (typically limit switches).
        This is important for accurate positioning, especially after power cycles.
        
        The homing sequence includes safety movements to avoid collisions with
        the plate clamp mechanism.
        """
        # Home Z axis first (if enabled)
        if control._def.HOMING_ENABLED_Z:
            self.stage.home(x=False, y=False, z=True, theta=False)
            
        # Home X and Y axes with safety movements
        if control._def.HOMING_ENABLED_X and control._def.HOMING_ENABLED_Y:
            # The plate clamp actuation post can get in the way of homing if we start with
            # the stage in "just the wrong" position.  Blindly moving the Y out 20, then home x
            # and move x over 20 , guarantees we'll clear the post for homing.  If we are <20mm
            # from the end travel of either axis, we'll just stop at the extent without consequence.
            #
            # The one odd corner case is if the system gets shut down in the loading position.
            # in that case, we drive off of the loading position and the clamp closes quickly.
            # This doesn't seem to cause problems, and there isn't a clean way to avoid the corner
            # case.
            self._log.info("Moving y+20, then x->home->+50 to make sure system is clear for homing.")
            # Move Y away from loading position to clear clamp
            self.stage.move_y(20)
            # Home X axis
            self.stage.home(x=True, y=False, z=False, theta=False)
            # Move X away from home position
            self.stage.move_x(50)

            # Now home Y axis (clamp should be clear)
            self._log.info("Homing the Y axis...")
            self.stage.home(x=False, y=True, z=False, theta=False)

    def move_x(self, distance, blocking=True):
        self.stage.move_x(distance, blocking=blocking)

    def move_y(self, distance, blocking=True):
        self.stage.move_y(distance, blocking=blocking)

    def move_x_to(self, position, blocking=True):
        self.stage.move_x_to(position, blocking=blocking)

    def move_y_to(self, position, blocking=True):
        self.stage.move_y_to(position, blocking=blocking)

    def get_x(self):
        return self.stage.get_pos().x_mm

    def get_y(self):
        return self.stage.get_pos().y_mm

    def get_z(self):
        return self.stage.get_pos().z_mm

    def move_z_to(self, z_mm, blocking=True):
        self.stage.move_z_to(z_mm)

    def start_live(self):
        self.camera.start_streaming()
        self.live_controller.start_live()

    def stop_live(self):
        self.live_controller.stop_live()
        self.camera.stop_streaming()

    def waitForMicrocontroller(self, timeout=5.0, error_message=None):
        try:
            self.low_level_drivers.microcontroller.wait_till_operation_is_completed(timeout)
        except TimeoutError as e:
            self._log.error(error_message or "Microcontroller operation timed out!")
            raise e

    def close(self):
        self.stop_live()
        self.low_level_drivers.microcontroller.close()
        if self.addons.emission_filter_wheel:
            self.addons.emission_filter_wheel.close()
        if self.addons.camera_focus:
            self.addons.camera_focus.close()
        self.camera.close()

    def move_to_position(self, x, y, z):
        self.move_x_to(x)
        self.move_y_to(y)
        self.move_z_to(z)

    def set_objective(self, objective):
        self.objective_store.set_current_objective(objective)

    def set_illumination_intensity(self, channel, intensity, objective=None):
        """
        Set illumination intensity for a specific channel and objective.
        
        Intensity settings are stored per objective because different objectives
        may require different illumination levels (e.g., 10x vs 20x).
        
        Args:
            channel: Wavelength channel (e.g., "405", "488", "561")
            intensity: Intensity percentage (0-100%)
            objective: Objective name (uses current objective if None)
        """
        if objective is None:
            objective = self.objective_store.current_objective
        # Get or create channel configuration for this objective/channel combination
        channel_config = self.channel_configuration_manager.get_channel_configuration_by_name(objective, channel)
        channel_config.illumination_intensity = intensity
        # Apply the configuration to live controller (updates illumination)
        self.live_controller.set_microscope_mode(channel_config)

    def set_exposure_time(self, channel, exposure_time, objective=None):
        """
        Set camera exposure time for a specific channel and objective.
        
        Exposure times are stored per objective because different objectives
        may require different exposure times (e.g., due to different NA).
        
        Args:
            channel: Wavelength channel
            exposure_time: Exposure time in milliseconds
            objective: Objective name (uses current objective if None)
        """
        if objective is None:
            objective = self.objective_store.current_objective
        channel_config = self.channel_configuration_manager.get_channel_configuration_by_name(objective, channel)
        channel_config.exposure_time = exposure_time
        # Apply the configuration to live controller (updates camera settings)
        self.live_controller.set_microscope_mode(channel_config)
