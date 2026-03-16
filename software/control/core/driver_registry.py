"""
Driver registry — maps driver names to Python classes.

Uses lazy imports so that optional hardware SDKs are not required at startup.
Only the class that is actually requested gets imported.

Usage::

    from control.core.driver_registry import get_driver_class

    cls = get_driver_class("toupcam", simulate=False)
    camera = cls(**connection_kwargs, **config_kwargs)

Drivers are registered centrally in ``_register_builtin_drivers()`` using
``(module_path, class_name)`` tuples.  The actual ``import`` happens on
first ``get_driver_class`` call, so missing SDKs only cause errors for
hardware that is actually requested.
"""

import importlib
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Type

logger = logging.getLogger(__name__)


@dataclass
class _DriverEntry:
    """Internal record for a registered driver."""
    module: str
    class_name: str
    sim_module: Optional[str] = None
    sim_class_name: Optional[str] = None
    tags: List[str] = field(default_factory=list)

    _real_cls: Optional[Type] = field(default=None, repr=False)
    _sim_cls: Optional[Type] = field(default=None, repr=False)

    def resolve(self, simulate: bool = False) -> Type:
        if simulate and self.sim_module and self.sim_class_name:
            if self._sim_cls is None:
                mod = importlib.import_module(self.sim_module)
                self._sim_cls = getattr(mod, self.sim_class_name)
            return self._sim_cls
        if self._real_cls is None:
            mod = importlib.import_module(self.module)
            self._real_cls = getattr(mod, self.class_name)
        return self._real_cls


_REGISTRY: Dict[str, _DriverEntry] = {}
_INITIALIZED = False


def register_driver(
    name: str,
    module: str,
    class_name: str,
    sim_module: Optional[str] = None,
    sim_class_name: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> None:
    """Register a driver by name.

    Args:
        name: Unique driver key (used in ``machine_config.yaml``).
        module: Dotted module path for the real class.
        class_name: Class name within *module*.
        sim_module: Dotted module path for the simulation class (may be
            the same as *module*).
        sim_class_name: Simulation class name.
        tags: Optional tags for filtering (e.g. ``["camera"]``,
            ``["light_source"]``).
    """
    if name in _REGISTRY:
        logger.debug(f"Overwriting driver registration for '{name}'")
    _REGISTRY[name] = _DriverEntry(
        module=module,
        class_name=class_name,
        sim_module=sim_module,
        sim_class_name=sim_class_name,
        tags=tags or [],
    )


def get_driver_class(name: str, simulate: bool = False) -> Type:
    """Look up and lazily import a driver class.

    Args:
        name: Registered driver key.
        simulate: If *True* and a simulation variant exists, return that instead.

    Returns:
        The resolved Python class.

    Raises:
        KeyError: If *name* is not registered.
        ImportError: If the driver module cannot be imported.
    """
    _ensure_initialized()
    if name not in _REGISTRY:
        raise KeyError(
            f"No driver registered as '{name}'. "
            f"Available: {sorted(_REGISTRY.keys())}"
        )
    return _REGISTRY[name].resolve(simulate)


def is_registered(name: str) -> bool:
    _ensure_initialized()
    return name in _REGISTRY


def list_drivers(tag: Optional[str] = None) -> List[str]:
    """Return registered driver names, optionally filtered by tag."""
    _ensure_initialized()
    if tag is None:
        return sorted(_REGISTRY.keys())
    return sorted(k for k, v in _REGISTRY.items() if tag in v.tags)


def _ensure_initialized() -> None:
    global _INITIALIZED
    if not _INITIALIZED:
        _register_builtin_drivers()
        _INITIALIZED = True


# ═══════════════════════════════════════════════════════════════════════════════
# Built-in driver registrations
# ═══════════════════════════════════════════════════════════════════════════════

def _register_builtin_drivers() -> None:
    """Register all built-in drivers.

    Each call is cheap — no imports happen here.  The actual module
    import is deferred until ``get_driver_class`` resolves the entry.
    """

    # ── Cameras ────────────────────────────────────────────────────────────
    _sim_cam = ("squid.camera.utils", "SimulatedCamera")

    register_driver(
        "toupcam", "control.camera_toupcam", "ToupcamCamera",
        *_sim_cam, tags=["camera"],
    )
    register_driver(
        "daheng", "control.camera", "DefaultCamera",
        *_sim_cam, tags=["camera"],
    )
    register_driver(
        "flir", "control.camera_flir", "FLIRCamera",
        *_sim_cam, tags=["camera"],
    )
    register_driver(
        "hamamatsu", "control.camera_hamamatsu", "HamamatsuCamera",
        *_sim_cam, tags=["camera"],
    )
    register_driver(
        "tucsen", "control.camera_tucsen", "TucsenCamera",
        *_sim_cam, tags=["camera"],
    )
    register_driver(
        "photometrics", "control.camera_photometrics", "PhotometricsCamera",
        *_sim_cam, tags=["camera"],
    )
    register_driver(
        "andor_camera", "control.camera_andor", "AndorCamera",
        *_sim_cam, tags=["camera"],
    )
    register_driver(
        "retiga", "control.camera_retiga", "RetigaElectroCamera",
        *_sim_cam, tags=["camera"],
    )

    # ── Light sources ──────────────────────────────────────────────────────
    register_driver(
        "coolled_pe400",
        "control.serial_peripherals_coolled", "CoolLEDpE400",
        "control.serial_peripherals_coolled", "CoolLEDpE400_Simulation",
        tags=["light_source"],
    )
    register_driver(
        "ldi",
        "control.serial_peripherals", "LDI",
        "control.serial_peripherals", "LDI_Simulation",
        tags=["light_source"],
    )
    register_driver(
        "celesta",
        "control.celesta", "CELESTA",
        tags=["light_source"],
    )
    register_driver(
        "andor_laser",
        "control.illumination_andor", "AndorLaser",
        tags=["light_source"],
    )
    register_driver(
        "cellx",
        "control.serial_peripherals", "CellX",
        "control.serial_peripherals", "CellX_Simulation",
        tags=["light_source"],
    )
    register_driver(
        "sci_microscopy_led",
        "control.serial_peripherals", "SciMicroscopyLEDArray",
        "control.serial_peripherals", "SciMicroscopyLEDArray_Simulation",
        tags=["light_source", "led_array"],
    )

    # ── Stages ─────────────────────────────────────────────────────────────
    register_driver(
        "cephla", "squid.stage.cephla", "CephlaStage",
        tags=["stage"],
    )
    register_driver(
        "prior", "squid.stage.prior", "PriorStage",
        tags=["stage"],
    )

    # ── Filter wheels ──────────────────────────────────────────────────────
    register_driver(
        "squid_filter_wheel",
        "squid.filter_wheel_controller.cephla", "SquidFilterWheel",
        "squid.filter_wheel_controller.utils", "SimulatedFilterWheelController",
        tags=["filter_wheel"],
    )
    register_driver(
        "optospin",
        "squid.filter_wheel_controller.optospin", "Optospin",
        "squid.filter_wheel_controller.utils", "SimulatedFilterWheelController",
        tags=["filter_wheel"],
    )
    register_driver(
        "zaber",
        "squid.filter_wheel_controller.zaber", "ZaberFilterController",
        "squid.filter_wheel_controller.utils", "SimulatedFilterWheelController",
        tags=["filter_wheel"],
    )

    # ── Spinning disk / confocal ───────────────────────────────────────────
    register_driver(
        "xlight",
        "control.serial_peripherals", "XLight",
        "control.serial_peripherals", "XLight_Simulation",
        tags=["confocal"],
    )
    register_driver(
        "dragonfly",
        "control.serial_peripherals", "Dragonfly",
        "control.serial_peripherals", "Dragonfly_Simulation",
        tags=["confocal"],
    )

    # ── Microcontroller ────────────────────────────────────────────────────
    register_driver(
        "teensy",
        "control.microcontroller", "Microcontroller",
        "control.microcontroller", "Microcontroller",
        tags=["microcontroller"],
    )

    # ── NI-DAQ ─────────────────────────────────────────────────────────────
    register_driver(
        "nidaq",
        "control.nidaq", "NIDAQ",
        "control.nidaq", "SimulatedNIDAQ",
        tags=["nidaq"],
    )

    # ── Miscellaneous ──────────────────────────────────────────────────────
    register_driver(
        "objective_piezo",
        "control.piezo", "PiezoStage",
        tags=["piezo"],
    )
    register_driver(
        "objective_changer",
        "control.objective_changer_2_pos_controller", "ObjectiveChanger2PosController",
        "control.objective_changer_2_pos_controller", "ObjectiveChanger2PosController_Simulation",
        tags=["objective_changer"],
    )
    register_driver(
        "nl5",
        "control.NL5", "NL5",
        "control.NL5", "NL5_Simulation",
        tags=["laser_combiner"],
    )

    logger.debug(f"Registered {len(_REGISTRY)} built-in drivers")
