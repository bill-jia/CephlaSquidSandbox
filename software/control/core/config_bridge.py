"""
Backward compatibility bridge: MachineConfig → _def.py globals.

During the transition from _def.py to machine_config.yaml, many parts of the
codebase still read device-presence flags and configuration values from
``control._def``.  This module provides ``apply_machine_config()`` which
populates those globals from a ``MachineConfig`` instance so that existing
code continues to work unchanged.

Once all consumers are migrated to read from ``MachineConfig`` directly,
this module can be deleted.
"""

import logging
from typing import Optional

from control.models.machine_config import MachineConfig

logger = logging.getLogger(__name__)

# Maps machine_config camera driver names to the legacy CAMERA_TYPE strings
# that _def.py and squid/config.py expect.
_DRIVER_TO_CAMERA_TYPE = {
    "toupcam": "Toupcam",
    "daheng": "Default",
    "flir": "FLIR",
    "hamamatsu": "Hamamatsu",
    "tucsen": "Tucsen",
    "photometrics": "Photometrics",
    "andor_camera": "Andor",
    "retiga": "Retiga",
    "ids": "iDS",
    "tis": "TIS",
}


def apply_machine_config(mc: MachineConfig) -> None:
    """Populate ``control._def`` globals from a MachineConfig.

    This is called early in ``Microscope.build_from_global_config`` so that
    legacy code that reads ``control._def.USE_*`` flags gets correct values.
    """
    import control._def

    def _dev_enabled(name: str) -> bool:
        d = mc.get_device(name)
        return d is not None and d.enabled

    def _dev_simulate(name: str) -> bool:
        d = mc.get_device(name)
        return d is not None and d.simulate

    def _dev_config(name: str, key: str, default=None):
        d = mc.get_device(name)
        if d is None:
            return default
        return d.config.get(key, default)

    def _dev_connection(name: str, key: str, default=None):
        d = mc.get_device(name)
        if d is None or d.connection is None:
            return default
        return getattr(d.connection, key, default)

    # ── Device-presence flags ────────────────────────────────────────────────
    # Laser AF UI/hardware presence is determined from ``microscope.addons.camera_focus``
    # after Microscope is built; do not mirror ``laser_af`` device here (see _def.SUPPORT_LASER_AUTOFOCUS note).

    # Piezo (Z motor config)
    piezo_enabled = _dev_enabled("piezo")
    if piezo_enabled:
        from control._def import ZMotorConfig
        control._def.HAS_OBJECTIVE_PIEZO = True
        control._def.Z_MOTOR_CONFIG = ZMotorConfig.STEPPER_PIEZO
        control._def.OBJECTIVE_PIEZO_HOME_UM = _dev_config("piezo", "home_um", 150)
        control._def.OBJECTIVE_PIEZO_RANGE_UM = _dev_config("piezo", "range_um", 300)
        control._def.OBJECTIVE_PIEZO_CONTROL_VOLTAGE_RANGE = _dev_config(
            "piezo", "control_voltage_range", 10
        )
        control._def.OBJECTIVE_PIEZO_FLIP_DIR = _dev_config(
            "piezo", "flip_direction", False
        )

    # Spinning disk confocal
    xlight_enabled = _dev_enabled("xlight")
    dragonfly_enabled = _dev_enabled("dragonfly")
    control._def.ENABLE_SPINNING_DISK_CONFOCAL = xlight_enabled or dragonfly_enabled
    control._def.USE_DRAGONFLY = dragonfly_enabled

    if xlight_enabled:
        control._def.XLIGHT_SERIAL_NUMBER = _dev_connection("xlight", "serial_number", "")
        xlight_cfg = mc.get_device("xlight").config if mc.get_device("xlight") else {}
        control._def.XLIGHT_SLEEP_TIME_FOR_WHEEL = xlight_cfg.get(
            "sleep_time_for_wheel", 0.25
        )

    if dragonfly_enabled:
        control._def.DRAGONFLY_SERIAL_NUMBER = _dev_connection(
            "dragonfly", "serial_number", ""
        )

    # Other optional devices
    control._def.ENABLE_NL5 = _dev_enabled("nl5")
    control._def.ENABLE_CELLX = _dev_enabled("cellx")
    control._def.SUPPORT_SCIMICROSCOPY_LED_ARRAY = _dev_enabled("led_matrix")
    control._def.USE_XERYON = _dev_enabled("objective_changer")
    control._def.RUN_FLUIDICS = _dev_enabled("fluidics")
    control._def.USE_PRIOR_STAGE = any(
        d.driver == "prior" for d in mc.get_enabled_devices().values()
        if d.role is None or d.role == "stage"  # avoid matching a random device named 'prior'
    )

    # ── Simulation flags ─────────────────────────────────────────────────────

    control._def.SIMULATE_CAMERA = _dev_simulate("main_camera")
    control._def.SIMULATE_MICROCONTROLLER = _dev_simulate("teensy")
    control._def.SIMULATE_SPINNING_DISK = (
        _dev_simulate("xlight") if xlight_enabled
        else _dev_simulate("dragonfly") if dragonfly_enabled
        else False
    )
    control._def.SIMULATE_FILTER_WHEEL = _dev_simulate("emission_filter_wheel")
    control._def.SIMULATE_OBJECTIVE_CHANGER = _dev_simulate("objective_changer")
    control._def.SIMULATE_LASER_AF_CAMERA = _dev_simulate("focus_camera")
    control._def.SIMULATE_NIDAQ = _dev_simulate("nidaq")

    # ── Camera type ──────────────────────────────────────────────────────────

    main_cam = mc.get_device("main_camera")
    if main_cam and main_cam.enabled:
        legacy_type = _DRIVER_TO_CAMERA_TYPE.get(main_cam.driver)
        if legacy_type:
            control._def.CAMERA_TYPE = legacy_type

    # NI-DAQ digital logic family: allow MachineConfig to override camera-based default.
    nidaq_dev = mc.get_device("nidaq")
    if nidaq_dev and nidaq_dev.enabled:
        logic_family = nidaq_dev.config.get("logic_family")
        if logic_family:
            control._def.NI_DAQ_LOGIC_FAMILY = str(logic_family)

    # ── Microcontroller ──────────────────────────────────────────────────────

    teensy = mc.get_device("teensy")
    if teensy and teensy.enabled:
        sn = _dev_connection("teensy", "serial_number")
        if sn:
            control._def.CONTROLLER_SN = sn
        control._def.CONTROLLER_VERSION = "Teensy"

        # Output gains
        gains_cfg = teensy.config.get("output_gains", {})
        if gains_cfg:
            if "refdiv" in gains_cfg:
                control._def.OUTPUT_GAINS.REFDIV = gains_cfg["refdiv"]
            channels = gains_cfg.get("channels", [])
            for i, val in enumerate(channels):
                setattr(control._def.OUTPUT_GAINS, f"CHANNEL{i}_GAIN", bool(val))

        # Illumination intensity factor
        factor = teensy.config.get("illumination_intensity_factor")
        if factor is not None:
            control._def.ILLUMINATION_INTENSITY_FACTOR = factor

    # ── Software settings ────────────────────────────────────────────────────

    sw = mc.software
    control._def.IS_HCS = sw.is_hcs
    control._def.WELLPLATE_FORMAT = str(sw.wellplate_format)
    control._def.WELLPLATE_FORMAT = control._def.WELLPLATE_FORMAT + " well plate" if control._def.WELLPLATE_FORMAT.isdigit() else control._def.WELLPLATE_FORMAT
    if control._def.WELLPLATE_FORMAT not in control._def.WELLPLATE_FORMAT_SETTINGS:
        control._def.WELLPLATE_FORMAT = "96 well plate"
    if sw.default_saving_path:
        control._def.DEFAULT_SAVING_PATH = sw.default_saving_path
    control._def.USE_NAPARI_FOR_LIVE_VIEW = sw.display.use_napari_for_live_view
    control._def.USE_NAPARI_FOR_MOSAIC_DISPLAY = sw.display.use_napari_for_mosaic

    # Acquisition
    acq = sw.acquisition
    control._def.Acquisition.IMAGE_FORMAT = acq.image_format
    control._def.Acquisition.IMAGE_DISPLAY_SCALING_FACTOR = acq.scaling_factor
    control._def.Acquisition.DX = acq.dx
    control._def.Acquisition.DY = acq.dy
    control._def.Acquisition.DZ = acq.dz
    control._def.Acquisition.NUMBER_OF_FOVS_PER_AF = acq.fovs_per_af
    control._def.Acquisition.ILLUMINATION_SETTLE_MS = float(acq.illumination_settle_ms)
    control._def.ENABLE_FLEXIBLE_MULTIPOINT = acq.flexible_multipoint
    control._def.ENABLE_WELLPLATE_MULTIPOINT = acq.wellplate_multipoint
    control._def.ENABLE_RECORDING = acq.recording

    # Autofocus
    af = sw.autofocus
    control._def.MULTIPOINT_AUTOFOCUS_CHANNEL = af.channel
    control._def.MULTIPOINT_AUTOFOCUS_ENABLE_BY_DEFAULT = af.enable_by_default
    control._def.MULTIPOINT_BF_SAVING_OPTION = af.bf_saving_option
    control._def.AF.STOP_THRESHOLD = af.stop_threshold
    control._def.AF.CROP_WIDTH = af.crop_width
    control._def.AF.CROP_HEIGHT = af.crop_height

    # Optics
    control._def.TUBE_LENS_MM = sw.optics.tube_lens_mm
    control._def.INVERTED_OBJECTIVE = sw.optics.inverted_objective

    # Tracking
    control._def.ENABLE_TRACKING = sw.tracking.enabled
    control._def.DEFAULT_TRACKER = sw.tracking.default_tracker

    # ── Stage settings ────────────────────────────────────────────────────────

    stage_dev = mc.get_device("stage")
    if stage_dev and stage_dev.enabled:
        s = stage_dev.config

        def _axis_val(axis: str, key: str, default=None):
            return s.get(axis, {}).get(key, default)

        for axis, uc in [("x", "X"), ("y", "Y"), ("z", "Z")]:
            a = s.get(axis, {})
            enc = s.get("encoders", {}).get(axis, {})
            pid = s.get("pid", {}).get(axis, {})
            lim = s.get("software_limits", {}).get(axis, {})

            setattr(control._def, f"STAGE_MOVEMENT_SIGN_{uc}", a.get("movement_sign", 1))
            setattr(control._def, f"FULLSTEPS_PER_REV_{uc}", a.get("fullsteps_per_rev", 200))
            setattr(control._def, f"SCREW_PITCH_{uc}_MM", a.get("screw_pitch_mm", 1))
            setattr(control._def, f"MICROSTEPPING_DEFAULT_{uc}", a.get("microstepping", 8))
            setattr(control._def, f"{uc}_MOTOR_RMS_CURRENT_mA", a.get("motor_rms_current_ma", 500))
            setattr(control._def, f"{uc}_MOTOR_I_HOLD", a.get("i_hold", 0.5))
            setattr(control._def, f"MAX_VELOCITY_{uc}_mm", a.get("max_velocity_mm", 25))
            setattr(control._def, f"MAX_ACCELERATION_{uc}_mm", a.get("max_acceleration_mm", 500))
            setattr(control._def, f"SCAN_STABILIZATION_TIME_MS_{uc}", a.get("scan_stabilization_time_ms", 25))
            setattr(control._def, f"HOMING_ENABLED_{uc}", a.get("homing_enabled", False))
            setattr(control._def, f"USE_ENCODER_{uc}", enc.get("enabled", False))
            setattr(control._def, f"HAS_ENCODER_{uc}", enc.get("has_encoder", False))
            setattr(control._def, f"ENABLE_PID_{uc}", pid.get("enabled", False))
            setattr(control._def.SOFTWARE_POS_LIMIT, f"{uc}_POSITIVE", lim.get("positive", 56))
            setattr(control._def.SOFTWARE_POS_LIMIT, f"{uc}_NEGATIVE", lim.get("negative", -0.5))

        # Limit switch polarity
        for axis, uc in [("x", "X"), ("y", "Y"), ("z", "Z")]:
            polarity = s.get(axis, {}).get("home_switch_polarity")
            if polarity is not None:
                setattr(control._def, f"HOME_SWITCH_POLARITY_{uc}", polarity)

        # Slide positions
        pos = s.get("positions", {})
        loading = pos.get("loading", {})
        scanning = pos.get("scanning", {})
        if loading:
            control._def.SLIDE_POSITION.LOADING_X_MM = loading.get("x_mm", 0.5)
            control._def.SLIDE_POSITION.LOADING_Y_MM = loading.get("y_mm", 0.5)
        if scanning:
            control._def.SLIDE_POSITION.SCANNING_X_MM = scanning.get("x_mm", 20)
            control._def.SLIDE_POSITION.SCANNING_Y_MM = scanning.get("y_mm", 20)

    # ── Camera config overrides ───────────────────────────────────────────────

    main_cam_dev = mc.get_device("main_camera")
    if main_cam_dev and main_cam_dev.enabled:
        cam_cfg = main_cam_dev.config
        roi = cam_cfg.get("roi", {})
        crop = cam_cfg.get("crop", {})
        if roi:
            control._def.CAMERA_CONFIG.ROI_OFFSET_X_DEFAULT = roi.get("offset_x")
            control._def.CAMERA_CONFIG.ROI_OFFSET_Y_DEFAULT = roi.get("offset_y")
            control._def.CAMERA_CONFIG.ROI_WIDTH_DEFAULT = roi.get("width")
            control._def.CAMERA_CONFIG.ROI_HEIGHT_DEFAULT = roi.get("height")
        if crop:
            control._def.CAMERA_CONFIG.CROP_WIDTH_UNBINNED = crop.get("width")
            control._def.CAMERA_CONFIG.CROP_HEIGHT_UNBINNED = crop.get("height")
        if "binning" in cam_cfg:
            control._def.CAMERA_CONFIG.BINNING_FACTOR_DEFAULT = cam_cfg["binning"]
        if "pixel_format" in cam_cfg:
            control._def.CAMERA_CONFIG.PIXEL_FORMAT_DEFAULT = cam_cfg["pixel_format"]
        if "temperature" in cam_cfg:
            control._def.CAMERA_CONFIG.TEMPERATURE_DEFAULT = cam_cfg["temperature"]

    # Rebuild squid/config.py singletons from MachineConfig
    try:
        import squid.config
        squid.config.reconfigure_from_machine_config(mc)
    except Exception as e:
        logger.warning(f"Could not reconfigure squid.config from MachineConfig: {e}")

    logger.info("Applied MachineConfig to _def.py globals for backward compatibility")
