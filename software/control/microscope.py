from pathlib import Path
from typing import Dict, Optional

import numpy as np

import control._def
from control._def import TriggerMode, NIDAQ_CONFIG
from control.core.config import ConfigRepository
from control.core.config_bridge import apply_machine_config
from control.core.contrast_manager import ContrastManager
from control.core.driver_registry import get_driver_class, is_registered
from control.core.live_controller import LiveController
from control.core.objective_store import ObjectiveStore
from control.core.stream_handler import StreamHandler, StreamHandlerFunctions, NoOpStreamHandlerFunctions

from control.core.io_controller import IORegistry, LightSourceSerialAdapter
from control.lighting import LightSourceType, IntensityControlMode, ShutterControlMode, IlluminationController
from control.microcontroller import Microcontroller
from control.models.machine_config import MachineConfig, DeviceEntry
from control.piezo import PiezoStage
from control.serial_peripherals import SciMicroscopyLEDArray
from squid.abc import CameraAcquisitionMode, AbstractCamera, AbstractStage, AbstractFilterWheelController, LightSource
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
from control.nidaq import AbstractNIDAQ, NIDAQ


def _should_simulate(global_simulated: bool, component_override: bool) -> bool:
    """Determine if a component should be simulated.

    Args:
        global_simulated: The global --simulation flag value.
        component_override: Per-component setting from control._def.SIMULATE_*.
            True = simulate this component
            False = use real hardware (default)

    Returns:
        True if the component should be simulated, False otherwise.

    Behavior:
        - Per-component SIMULATE_* is always respected.
        - When --simulation is used, apply_simulation_mode_defaults(True) sets any
          SIMULATE_* not specified in config to True, so unset components are simulated.
    """
    return bool(component_override)


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
        stage: AbstractStage,
        micro: Optional[Microcontroller],
        simulated: bool = False,
        skip_init: bool = False,
        machine_config: Optional[MachineConfig] = None,
    ) -> "MicroscopeAddons":
        """Build MicroscopeAddons from MachineConfig device entries.

        Device construction is driven by the ``devices`` section of
        ``machine_config.yaml``.  The ``_def.py`` globals have already been
        populated by :func:`apply_machine_config` before this is called.
        """
        log = squid.logging.get_logger("MicroscopeAddons")
        mc = machine_config or ConfigRepository().get_machine_config()

        def _dev(name: str) -> Optional[DeviceEntry]:
            d = mc.get_device(name)
            return d if d and d.enabled else None

        def _sim(name: str) -> bool:
            d = mc.get_device(name)
            return d.simulate if d else simulated

        # ── Spinning disk confocal ────────────────────────────────────────
        xlight = None
        xlight_entry = _dev("xlight")
        if xlight_entry:
            if not _sim("xlight"):
                sn = xlight_entry.connection.serial_number if xlight_entry.connection else ""
                sleep_time = xlight_entry.config.get("sleep_time_for_wheel", 0.25)
                xlight = serial_peripherals.XLight(sn, sleep_time)
            else:
                xlight = serial_peripherals.XLight_Simulation()

        dragonfly = None
        dragonfly_entry = _dev("dragonfly")
        if dragonfly_entry:
            if not _sim("dragonfly"):
                sn = dragonfly_entry.connection.serial_number if dragonfly_entry.connection else ""
                dragonfly = serial_peripherals.Dragonfly(SN=sn)
            else:
                dragonfly = serial_peripherals.Dragonfly_Simulation()

        # ── NL5 laser combiner ────────────────────────────────────────────
        nl5 = None
        if _dev("nl5"):
            try:
                import control.NL5 as NL5_mod
                nl5 = NL5_mod.NL5() if not _sim("nl5") else NL5_mod.NL5_Simulation()
            except ImportError:
                log.warning("NL5 module not available")

        # ── CellX ─────────────────────────────────────────────────────────
        cellx = None
        cellx_entry = _dev("cellx")
        if cellx_entry:
            sn = cellx_entry.connection.serial_number if cellx_entry.connection else ""
            cellx = (
                serial_peripherals.CellX(sn)
                if not _sim("cellx")
                else serial_peripherals.CellX_Simulation()
            )

        # ── Emission filter wheel ─────────────────────────────────────────
        emission_filter_wheel = None
        fw_config = squid.config.get_filter_wheel_config()
        if fw_config:
            emission_filter_wheel = squid.filter_wheel_controller.utils.get_filter_wheel_controller(
                fw_config, microcontroller=micro,
                simulated=_should_simulate(simulated, control._def.SIMULATE_FILTER_WHEEL),
                skip_init=skip_init,
            )

        # ── Objective changer ─────────────────────────────────────────────
        objective_changer = None
        oc_entry = _dev("objective_changer")
        if oc_entry:
            try:
                from control.objective_changer_2_pos_controller import (
                    ObjectiveChanger2PosController,
                    ObjectiveChanger2PosController_Simulation,
                )
                sn = oc_entry.connection.serial_number if oc_entry.connection else ""
                objective_changer = (
                    ObjectiveChanger2PosController(sn=sn, stage=stage)
                    if not _sim("objective_changer")
                    else ObjectiveChanger2PosController_Simulation(sn=sn, stage=stage)
                )
            except ImportError:
                log.warning("Objective changer module not available")

        # ── Focus camera (laser AF) ──────────────────────────────────────
        camera_focus = None
        if _dev("focus_camera") and _dev("laser_af") and _dev("laser_af").enabled:
            camera_focus = squid.camera.utils.get_camera(
                squid.config.get_autofocus_camera_config(),
                simulated=_sim("focus_camera"),
            )

        # ── Fluidics ──────────────────────────────────────────────────────
        fluidics = None
        fluidics_entry = _dev("fluidics")
        if fluidics_entry:
            try:
                from control.fluidics import Fluidics
                cfg_path = fluidics_entry.config.get("config_path", "")
                fluidics = Fluidics(config_path=cfg_path, simulation=_sim("fluidics"))
            except ImportError:
                log.warning("Fluidics module not available")

        # ── SciMicroscopy LED array (serial device) ───────────────────────
        sci_microscopy_led_array = None
        led_entry = _dev("led_matrix")
        if led_entry:
            if led_entry.driver == "scimicroscopy_led_array":
                sn = led_entry.connection.serial_number if led_entry.connection else ""
                dist = led_entry.config.get("distance", 50)
                delay = led_entry.config.get("turn_on_delay", 0.03)
                na = led_entry.config.get("default_na", 0.8)
                default_color = tuple(led_entry.config.get("default_color", [1, 1, 1]))
                sci_microscopy_led_array = serial_peripherals.SciMicroscopyLEDArray(
                    SN=sn,
                    array_distance=dist,
                    turn_on_delay=delay,
                    default_color=default_color,
                )
                sci_microscopy_led_array.set_NA(na)

        # ── NI-DAQ ────────────────────────────────────────────────────────
        nidaq = None
        nidaq_entry = _dev("nidaq")
        if nidaq_entry and not _sim("nidaq"):
            nidaq = NIDAQ(config=NIDAQ_CONFIG())

        # ── Hybrid serial+IO light sources ────────────────────────────────
        coolled = None
        coolled_entry = _dev("coolled")
        if coolled_entry:
            try:
                import control.serial_peripherals_coolled as coolled_peripherals
                if not _sim("coolled"):
                    sn = coolled_entry.connection.serial_number if coolled_entry.connection else None
                    port = coolled_entry.connection.port if coolled_entry.connection else None
                    coolled = coolled_peripherals.CoolLEDpE400(SN=sn, port=port)
                else:
                    coolled = coolled_peripherals.CoolLEDpE400_Simulation()
            except ImportError:
                log.warning("coolLED module not available")

        serial_devices: Dict[str, object] = {}
        if coolled is not None:
            serial_devices["coolled"] = LightSourceSerialAdapter(coolled)

        # ── IO endpoint registry ──────────────────────────────────────────
        # Collect IO endpoints from device entries in machine_config.yaml
        io_registry = None
        if micro is not None:
            io_config = mc.collect_io_endpoints()
            io_registry = IORegistry(
                config=io_config,
                microcontroller=micro,
                nidaq=nidaq,
                serial_devices=serial_devices or None,
            )
            io_registry.log_summary()

        # ── Piezo stage ───────────────────────────────────────────────────
        piezo_stage = None
        piezo_entry = _dev("piezo")
        if piezo_entry:
            if not micro:
                raise ValueError("Cannot create PiezoStage without a Microcontroller.")
            piezo_ep = io_registry.get("piezo.output") if io_registry else None
            piezo_stage = PiezoStage(
                microcontroller=micro,
                config={
                    "OBJECTIVE_PIEZO_HOME_UM": piezo_entry.config.get("home_um", 150),
                    "OBJECTIVE_PIEZO_RANGE_UM": piezo_entry.config.get("range_um", 300),
                    "OBJECTIVE_PIEZO_CONTROL_VOLTAGE_RANGE": piezo_entry.config.get("control_voltage_range", 10),
                    "OBJECTIVE_PIEZO_FLIP_DIR": piezo_entry.config.get("flip_direction", False),
                },
                piezo_endpoint=piezo_ep,
            )

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
            nidaq,
            io_registry=io_registry,
            coolled=coolled,
        )

    def __init__(
        self,
        xlight=None,
        dragonfly=None,
        nl5=None,
        cellx=None,
        emission_filter_wheel: Optional[AbstractFilterWheelController] = None,
        objective_changer=None,
        camera_focus: Optional[AbstractCamera] = None,
        fluidics=None,
        piezo_stage: Optional[PiezoStage] = None,
        sci_microscopy_led_array: Optional[SciMicroscopyLEDArray] = None,
        nidaq: Optional[AbstractNIDAQ] = None,
        io_registry: Optional[IORegistry] = None,
        coolled=None,
    ):
        self.xlight = xlight
        self.dragonfly = dragonfly
        self.nl5 = nl5
        self.cellx = cellx
        self.emission_filter_wheel = emission_filter_wheel
        self.objective_changer = objective_changer
        self.camera_focus: Optional[AbstractCamera] = camera_focus
        self.fluidics = fluidics
        self.piezo_stage = piezo_stage
        self.sci_microscopy_led_array = sci_microscopy_led_array
        self.nidaq = nidaq
        self.io_registry: Optional[IORegistry] = io_registry
        self.coolled = coolled

    def prepare_for_use(self, skip_init: bool = False, skip_homing: bool = False):
        """
        Prepare all the addon hardware for immediate use.

        Args:
            skip_init: If True, skip homing operations (e.g., during restart).
            skip_homing: If True, skip mechanical motions (filter wheel home, piezo home).
        """
        skip_motion = skip_init or skip_homing
        if self.emission_filter_wheel:
            fw_config = squid.config.get_filter_wheel_config()
            self.emission_filter_wheel.initialize(fw_config.indices)
            if not skip_motion:
                self.emission_filter_wheel.home()
        if self.piezo_stage and not skip_motion:
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
    def build_from_global_config(
        simulated: bool = False,
        skip_init: bool = False,
        machine_config: Optional[MachineConfig] = None,
    ) -> "LowLevelDrivers":
        mc = machine_config or ConfigRepository().get_machine_config()
        teensy_entry = mc.get_device("teensy")
        mcu_simulated = _should_simulate(
            simulated, control._def.SIMULATE_MICROCONTROLLER
        )
        if teensy_entry and teensy_entry.simulate:
            mcu_simulated = True

        sn = None
        if teensy_entry and teensy_entry.connection:
            sn = teensy_entry.connection.serial_number

        micro_serial_device = (
            control.microcontroller.get_microcontroller_serial_device(
                version=control._def.CONTROLLER_VERSION,
                sn=sn or control._def.CONTROLLER_SN,
            )
            if not mcu_simulated
            else control.microcontroller.get_microcontroller_serial_device(simulated=True)
        )
        micro = control.microcontroller.Microcontroller(
            serial_device=micro_serial_device,
            reset_and_initialize=not skip_init,
        )

        # Configure LED matrix RGB factors for MCU-driven brightfield, if present.
        led_entry = mc.get_device("led_matrix")
        if led_entry:
            r_factor = float(led_entry.config.get("r_factor", 1.0))
            g_factor = float(led_entry.config.get("g_factor", 1.0))
            b_factor = float(led_entry.config.get("b_factor", 1.0))
            micro.set_led_matrix_factors(r_factor, g_factor, b_factor)

        return LowLevelDrivers(microcontroller=micro)

    def __init__(self, microcontroller: Optional[Microcontroller] = None):
        self.microcontroller: Optional[Microcontroller] = microcontroller

    def prepare_for_use(self, skip_init: bool = False):
        """
        Prepare the low-level drivers for immediate use.
        
        Args:
            skip_init: If True, skip homing operations (e.g., during restart).
        """
        # Note: Currently no homing operations here, but accepting skip_init for API consistency
        if self.microcontroller and control._def.HAS_OBJECTIVE_PIEZO:
            # Configure DAC gains for objective piezo
            # If piezo requires 5V range, enable gain on channel 7
            control._def.OUTPUT_GAINS.CHANNEL7_GAIN = control._def.OBJECTIVE_PIEZO_CONTROL_VOLTAGE_RANGE == 5
            # Reference divider: 0 = no divide (2.5V ref), 1 = divide by 2 (1.25V ref)
            div = 1 if control._def.OUTPUT_GAINS.REFDIV else 0
            # Pack gain bits for all 8 channels into a single byte
            gains = sum(getattr(control._def.OUTPUT_GAINS, f"CHANNEL{i}_GAIN") << i for i in range(8))
            self.microcontroller.configure_dac80508_refdiv_and_gain(div, gains)


def _build_illumination_controller(
    micro: Optional[Microcontroller],
    io_registry: Optional[IORegistry],
    illum_entry: Optional[DeviceEntry],
    coolled_entry: Optional[DeviceEntry],
    coolled_instance: Optional[LightSource],
    simulated: bool,
) -> IlluminationController:
    """Construct the appropriate IlluminationController from device entries."""
    driver = ""
    if coolled_entry and coolled_entry.enabled:
        driver = "coolled_pe400"
    elif illum_entry and illum_entry.enabled:
        driver = illum_entry.driver

    if driver == "coolled_pe400" and coolled_instance is not None:
        return IlluminationController(
            micro,
            IntensityControlMode.Software,
            ShutterControlMode.TTL,
            LightSourceType.CoolLED,
            coolled_instance,
            io_registry=io_registry,
        )

    if driver == "ldi" and not simulated:
        ldi = serial_peripherals.LDI()
        return IlluminationController(
            micro, ldi.intensity_mode, ldi.shutter_mode,
            LightSourceType.LDI, ldi, io_registry=io_registry,
        )

    if driver == "celesta" and not simulated:
        celesta = control.celesta.CELESTA()
        return IlluminationController(
            micro,
            IntensityControlMode.Software,
            ShutterControlMode.TTL,
            LightSourceType.CELESTA,
            celesta,
            io_registry=io_registry,
        )

    if driver == "andor_laser" and not simulated:
        andor_laser = control.illumination_andor.AndorLaser(
            control._def.ANDOR_LASER_VID, control._def.ANDOR_LASER_PID
        )
        return IlluminationController(
            micro,
            IntensityControlMode.Software,
            ShutterControlMode.TTL,
            LightSourceType.AndorLaser,
            andor_laser,
            io_registry=io_registry,
        )

    # Default: Cephla built-in (MCU DAC/TTL)
    return IlluminationController(micro, io_registry=io_registry)


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
    def build_from_global_config(
        simulated: bool = False, skip_init: bool = False, skip_homing: bool = False
    ) -> "Microscope":
        """Build Microscope from ``machine_config.yaml`` via :class:`MachineConfig`.

        Loads the unified machine configuration, applies it to ``_def.py``
        globals for backward compatibility, then constructs all devices from
        the device entries.
        """
        config_repo = ConfigRepository()
        mc = config_repo.get_machine_config()

        # Backward-compat bridge: populate _def.py globals from MachineConfig
        apply_machine_config(mc)

        # ── Low-level drivers (Teensy MCU) ────────────────────────────────
        low_level_devices = LowLevelDrivers.build_from_global_config(
            simulated, skip_init=skip_init, machine_config=mc,
        )

        # ── Stage ─────────────────────────────────────────────────────────
        stage_config = squid.config.get_stage_config()
        stage_entry = mc.get_device("stage")
        use_prior = stage_entry and stage_entry.driver == "prior" if stage_entry else False

        if use_prior:
            sn = stage_entry.connection.serial_number if stage_entry.connection else ""
            stage = PriorStage(sn=sn, stage_config=stage_config)
        else:
            if low_level_devices.microcontroller is None:
                raise ValueError("For a cephla stage microscope, you must provide a microcontroller.")
            stage = CephlaStage(low_level_devices.microcontroller, stage_config)

        # ── Addons (IO, peripherals, focus camera, etc.) ──────────────────
        addons = MicroscopeAddons.build_from_global_config(
            stage, low_level_devices.microcontroller,
            simulated=simulated, skip_init=skip_init, machine_config=mc,
        )

        # ── Camera trigger routing ────────────────────────────────────────
        cam_trigger_log = squid.logging.get_logger("camera hw functions")
        io_reg = addons.io_registry
        trigger_ep = io_reg.get("main_camera.trigger") if io_reg else None

        def acquisition_camera_hw_trigger_fn(illumination_time: Optional[float]) -> bool:
            if addons.nl5 and control._def.NL5_USE_DOUT:
                addons.nl5.start_acquisition()
            elif trigger_ep is not None:
                illumination_time_us = int(1000.0 * illumination_time) if illumination_time else 0
                cam_trigger_log.debug(
                    f"Sending hw trigger via IO endpoint with illumination_time="
                    f"{illumination_time_us if illumination_time else None} [us]"
                )
                trigger_ep.send_trigger(
                    control_illumination=illumination_time is not None,
                    illumination_on_time_us=illumination_time_us,
                )
            else:
                illumination_time_us = 1000.0 * illumination_time if illumination_time else 0
                cam_trigger_log.debug(
                    f"Sending hw trigger (legacy) with illumination_time="
                    f"{illumination_time_us if illumination_time else None} [us]"
                )
                low_level_devices.microcontroller.send_hardware_trigger(
                    illumination_time is not None, illumination_time_us
                )
            return True

        def acquisition_camera_hw_strobe_delay_fn(strobe_delay_ms: float) -> bool:
            strobe_delay_us = int(1000 * strobe_delay_ms)
            cam_trigger_log.debug(f"Setting strobe delay to {strobe_delay_us} [us]")
            if trigger_ep is not None:
                trigger_ep.set_strobe_delay(strobe_delay_us)
                trigger_ep.wait()
            else:
                low_level_devices.microcontroller.set_strobe_delay_us(strobe_delay_us)
                low_level_devices.microcontroller.wait_till_operation_is_completed()
            return True

        camera_simulated = _should_simulate(simulated, control._def.SIMULATE_CAMERA)
        camera = squid.camera.utils.get_camera(
            config=squid.config.get_camera_config(),
            simulated=camera_simulated,
            hw_trigger_fn=acquisition_camera_hw_trigger_fn,
            hw_set_strobe_delay_ms_fn=acquisition_camera_hw_strobe_delay_fn,
        )

        # ── Illumination controller ───────────────────────────────────────
        io_reg = addons.io_registry
        illum_entry = mc.get_device("illumination")
        coolled_entry = mc.get_device("coolled")
        illumination_controller = _build_illumination_controller(
            low_level_devices.microcontroller, io_reg, illum_entry, coolled_entry,
            addons.coolled, simulated,
        )

        return Microscope(
            stage=stage,
            camera=camera,
            illumination_controller=illumination_controller,
            addons=addons,
            low_level_drivers=low_level_devices,
            simulated=simulated,
            skip_init=skip_init,
            skip_homing=skip_homing,
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
        skip_init: bool = False,
        skip_homing: bool = False,
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

        # Centralized config management
        self.config_repo: ConfigRepository = ConfigRepository()

        # Note: Migration from acquisition_configurations to user_profiles is handled
        # by run_auto_migration() in main_hcs.py before Microscope is created

        # Load default profile (ensures configs exist)
        profiles = self.config_repo.get_available_profiles()
        if profiles:
            self.config_repo.load_profile(profiles[0])
        else:
            # Create a default profile if none exist - load_profile() will call
            # ensure_default_configs() to generate configs from illumination_channel_config.yaml
            self._log.info("No profiles found, creating 'default' profile")
            self.config_repo.create_profile("default")
            self.config_repo.load_profile("default")

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

        # Sync confocal mode from hardware (must be after LiveController creation)
        if control._def.ENABLE_SPINNING_DISK_CONFOCAL:
            self._sync_confocal_mode_from_hardware()

        if not skip_prepare_for_use:
            self._prepare_for_use(skip_init=skip_init, skip_homing=skip_homing)

    def _prepare_for_use(self, skip_init: bool = False, skip_homing: bool = False):
        """
        Initialize all hardware components for use.
        
        This method:
        - Configures DAC gains for piezo control
        - Initializes filter wheels and other addons
        - Sets camera pixel formats and acquisition modes
        When skip_homing is True, device init runs but no mechanical motions (e.g. homing) are performed.
        """
        self.low_level_drivers.prepare_for_use(skip_init=skip_init)
        self.addons.prepare_for_use(skip_init=skip_init, skip_homing=skip_homing)

        # Configure serial watchdog for illumination safety (requires firmware v1.1+)
        if self.low_level_drivers.microcontroller:
            mcu = self.low_level_drivers.microcontroller
            if mcu.firmware_version >= (1, 1):
                timeout_s = control._def.WATCHDOG_TIMEOUT_S
                mcu.set_watchdog_timeout(timeout_s)
                mcu.wait_till_operation_is_completed()
                mcu.start_heartbeat(interval_s=timeout_s / 2)
                self._log.info(f"Illumination watchdog enabled: timeout={timeout_s}s, heartbeat={timeout_s / 2}s")
            else:
                self._log.warning(
                    f"Illumination watchdog not available: firmware v{mcu.firmware_version[0]}.{mcu.firmware_version[1]} "
                    "requires v1.1+"
                )

        # Configure main camera
        # Set pixel format (MONO8, MONO16, etc.) from configuration

        self.camera.set_pixel_format(
            squid.config.CameraPixelFormat.from_string(control._def.CAMERA_CONFIG.PIXEL_FORMAT_DEFAULT)
        )

        _trigger_map = {
            TriggerMode.SOFTWARE: CameraAcquisitionMode.SOFTWARE_TRIGGER,
            TriggerMode.HARDWARE: CameraAcquisitionMode.HARDWARE_TRIGGER,
            TriggerMode.CONTINUOUS: CameraAcquisitionMode.CONTINUOUS,
        }
        acq_mode = _trigger_map.get(control._def.DEFAULT_TRIGGER_MODE)
        if acq_mode is None:
            raise ValueError(f"Invalid trigger mode: {control._def.DEFAULT_TRIGGER_MODE}")
        self.camera.set_acquisition_mode(acq_mode)


        # Configure focus camera if available
        if self.addons.camera_focus:
            # Focus camera typically uses 8-bit format for faster processing
            self.addons.camera_focus.set_pixel_format(squid.config.CameraPixelFormat.from_string("MONO8"))
            self.addons.camera_focus.set_acquisition_mode(CameraAcquisitionMode.SOFTWARE_TRIGGER)

    def _sync_confocal_mode_from_hardware(self) -> bool:
        """Sync confocal mode state from spinning disk hardware.

        Queries the actual hardware state (XLight disk position or Dragonfly modality)
        and updates the live controller accordingly.
        This ensures correct channel settings are used in both GUI and headless modes.

        Returns:
            True if sync was successful, False if hardware query failed.
        """
        confocal_mode = False
        sync_successful = True

        if self.addons.dragonfly is not None:
            try:
                modality = self.addons.dragonfly.get_modality()
                confocal_mode = modality == "CONFOCAL" if modality else False
            except Exception as e:
                self._log.warning(f"Could not query Dragonfly modality: {e}")
                sync_successful = False
        elif self.addons.xlight is not None:
            try:
                # XLight returns 0 for widefield, 1 for confocal
                disk_position = self.addons.xlight.get_disk_position()
                confocal_mode = bool(disk_position)
            except Exception as e:
                self._log.warning(f"Could not query XLight disk position: {e}")
                sync_successful = False

        if sync_successful:
            self.live_controller.sync_confocal_mode_from_hardware(confocal_mode)
        else:
            self._log.warning(
                "Confocal mode could not be synchronized from hardware; " "keeping existing live controller state."
            )
        return sync_successful

    def set_confocal_mode(self, confocal: bool) -> None:
        """Set confocal/widefield mode and move the spinning disk.

        This is the preferred method for headless scripts to switch imaging modes.
        It updates both the hardware and the live controller.

        Args:
            confocal: True for confocal mode, False for widefield mode.

        Raises:
            RuntimeError: If spinning disk confocal is not enabled or hardware unavailable.
        """
        if not control._def.ENABLE_SPINNING_DISK_CONFOCAL:
            raise RuntimeError("Spinning disk confocal is not enabled in configuration")

        if self.addons.dragonfly is not None:
            modality = "CONFOCAL" if confocal else "BF"
            self.addons.dragonfly.set_modality(modality)
        elif self.addons.xlight is not None:
            # XLight: 1 for confocal, 0 for widefield
            self.addons.xlight.set_disk_position(1 if confocal else 0)
        else:
            raise RuntimeError("No spinning disk hardware available")

        self.live_controller.toggle_confocal_widefield(confocal)

    def is_confocal_mode(self) -> bool:
        """Check if currently in confocal mode.

        Returns:
            True if in confocal mode, False if in widefield mode.
        """
        return self.live_controller.is_confocal_mode()

    def update_camera_functions(self, functions: StreamHandlerFunctions) -> None:
        """Update the stream handler callback functions for the main camera.

        Args:
            functions: New callback functions for frame handling.
        """
        self.stream_handler.set_functions(functions)

    def update_camera_focus_functions(self, functions: StreamHandlerFunctions) -> None:
        """Update the stream handler callback functions for the focus camera.

        Args:
            functions: New callback functions for frame handling.

        Raises:
            ValueError: If no focus camera is configured.
        """
        if not self.addons.camera_focus:
            raise ValueError("No focus camera, cannot change its stream handler functions.")

        self.stream_handler_focus.set_functions(functions)

    def initialize_core_components(self) -> None:
        """Initialize and home core hardware components like piezo stage."""
        if self.addons.piezo_stage:
            self.addons.piezo_stage.home()

    def setup_hardware(self) -> None:
        """Set up camera frame callbacks and start streaming for focus camera if present."""
        self.camera.add_frame_callback(self.stream_handler.on_new_frame)
        self.camera.enable_callbacks(True)

        if self.addons.camera_focus:
            self.addons.camera_focus.add_frame_callback(self.stream_handler_focus.on_new_frame)
            self.addons.camera_focus.enable_callbacks(True)
            self.addons.camera_focus.start_streaming()

    def acquire_image(self) -> np.ndarray:
        """Acquire a single image from the camera.

        Turns on illumination, triggers the camera, reads the frame, and turns off
        illumination. The trigger mode (software vs hardware) is determined by the
        live controller configuration.

        Returns:
            The acquired image as a numpy array.

        Raises:
            RuntimeError: If the camera fails to return a frame.
        """
        using_software_trigger = self.live_controller.trigger_mode == control._def.TriggerMode.SOFTWARE

        # turn on illumination and send trigger
        if using_software_trigger:
            self.live_controller.turn_on_illumination()
            self._wait_for_microcontroller()
            self.camera.send_trigger()
        elif self.live_controller.trigger_mode == control._def.TriggerMode.HARDWARE:
            trigger_ep = self.addons.io_registry.get("main_camera.trigger") if self.addons.io_registry else None
            illumination_time_us = int(self.camera.get_exposure_time() * 1000)
            if trigger_ep is not None:
                trigger_ep.send_trigger(
                    control_illumination=True,
                    illumination_on_time_us=illumination_time_us,
                )
            else:
                self.low_level_drivers.microcontroller.send_hardware_trigger(
                    control_illumination=True, illumination_on_time_us=illumination_time_us,
                )

        try:
            # read a frame from camera
            image = self.camera.read_frame()
            if image is None:
                self._log.error("camera.read_frame() returned None")
                raise RuntimeError("Failed to acquire image: camera.read_frame() returned None")
            return image
        finally:
            # always turn off illumination when using software trigger
            if using_software_trigger:
                self.live_controller.turn_off_illumination()

    def home_xyz(self) -> None:
        """Home the X, Y, and Z axes based on configuration settings.

        Homes Z first if enabled, then performs a coordinated X/Y homing sequence
        that avoids the plate clamp actuation post by moving Y first, homing X,
        moving X clear, then homing Y.
        """
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

    def move_x(self, distance: float, blocking: bool = True) -> None:
        """Move the stage by a relative distance along the X axis.

        Args:
            distance: Distance to move in mm (positive or negative).
            blocking: If True, wait for movement to complete before returning.
        """
        self.stage.move_x(distance, blocking=blocking)

    def move_y(self, distance: float, blocking: bool = True) -> None:
        """Move the stage by a relative distance along the Y axis.

        Args:
            distance: Distance to move in mm (positive or negative).
            blocking: If True, wait for movement to complete before returning.
        """
        self.stage.move_y(distance, blocking=blocking)

    def move_x_to(self, position: float, blocking: bool = True) -> None:
        """Move the stage to an absolute X position.

        Args:
            position: Target position in mm.
            blocking: If True, wait for movement to complete before returning.
        """
        self.stage.move_x_to(position, blocking=blocking)

    def move_y_to(self, position: float, blocking: bool = True) -> None:
        """Move the stage to an absolute Y position.

        Args:
            position: Target position in mm.
            blocking: If True, wait for movement to complete before returning.
        """
        self.stage.move_y_to(position, blocking=blocking)

    def get_x(self) -> float:
        """Get the current X position of the stage.

        Returns:
            Current X position in mm.
        """
        return self.stage.get_pos().x_mm

    def get_y(self) -> float:
        """Get the current Y position of the stage.

        Returns:
            Current Y position in mm.
        """
        return self.stage.get_pos().y_mm

    def get_z(self) -> float:
        """Get the current Z position of the stage.

        Returns:
            Current Z position in mm.
        """
        return self.stage.get_pos().z_mm

    def move_z_to(self, z_mm: float, blocking: bool = True) -> None:
        """Move the stage to an absolute Z position.

        Args:
            z_mm: Target position in mm.
            blocking: If True, wait for movement to complete before returning.
        """
        self.stage.move_z_to(z_mm, blocking=blocking)

    def start_live(self) -> None:
        """Start live view streaming from the camera."""
        self.camera.start_streaming()
        self.live_controller.start_live()

    def stop_live(self) -> None:
        """Stop live view streaming from the camera."""
        self.live_controller.stop_live()
        self.camera.stop_streaming()

    def _wait_for_microcontroller(self, timeout: float = 5.0, error_message: Optional[str] = None) -> None:
        """Wait for the microcontroller to complete the current operation.

        Args:
            timeout: Maximum time to wait in seconds.
            error_message: Custom error message for timeout errors.

        Raises:
            TimeoutError: If operation does not complete within timeout.
        """
        try:
            self.low_level_drivers.microcontroller.wait_till_operation_is_completed(timeout)
        except TimeoutError as e:
            self._log.error(error_message or "Microcontroller operation timed out!")
            raise e

    def close(self) -> None:
        """Close the microscope and release all hardware resources.

        Attempts to cleanly shut down all hardware components. Errors during
        shutdown are logged but do not prevent other components from being closed.
        """
        try:
            self.stop_live()
        except Exception as e:
            self._log.warning(f"Error stopping live view during close: {e}")

        if self.low_level_drivers.microcontroller:
            try:
                self.low_level_drivers.microcontroller.close()
            except Exception as e:
                self._log.warning(f"Error closing microcontroller: {e}")

        if self.addons.emission_filter_wheel:
            try:
                self.addons.emission_filter_wheel.close()
            except Exception as e:
                self._log.warning(f"Error closing emission filter wheel: {e}")

        if self.addons.camera_focus:
            try:
                self.addons.camera_focus.close()
            except Exception as e:
                self._log.warning(f"Error closing focus camera: {e}")

        try:
            self.camera.close()
        except Exception as e:
            self._log.warning(f"Error closing camera: {e}")

    def move_to_position(self, x: float, y: float, z: float) -> None:
        """Move the stage to an absolute XYZ position.

        Args:
            x: Target X position in mm.
            y: Target Y position in mm.
            z: Target Z position in mm.
        """
        self.move_x_to(x)
        self.move_y_to(y)
        self.move_z_to(z)

    def set_objective(self, objective: str) -> None:
        """Set the current objective lens.

        Args:
            objective: Name of the objective to set as current.
        """
        self.objective_store.set_current_objective(objective)

    def set_illumination_intensity(self, channel: str, intensity: float, objective: Optional[str] = None) -> None:
        """Set the illumination intensity for a channel.

        Args:
            channel: Name of the channel.
            intensity: Illumination intensity value.
            objective: Objective name. If None, uses current objective.
        """
        if objective is None:
            objective = self.objective_store.current_objective
        channel_config = self.live_controller.get_channel_by_name(objective, channel)
        if channel_config:
            channel_config.illumination_intensity = intensity
            self.live_controller.set_microscope_mode(channel_config)

    def set_exposure_time(self, channel: str, exposure_time: float, objective: Optional[str] = None) -> None:
        """Set the exposure time for a channel.

        Args:
            channel: Name of the channel.
            exposure_time: Exposure time in milliseconds.
            objective: Objective name. If None, uses current objective.
        """
        if objective is None:
            objective = self.objective_store.current_objective
        channel_config = self.live_controller.get_channel_by_name(objective, channel)
        if channel_config:
            channel_config.exposure_time = exposure_time
            self.live_controller.set_microscope_mode(channel_config)
