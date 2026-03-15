from __future__ import annotations

from typing import Optional, TYPE_CHECKING
from control.microcontroller import Microcontroller

if TYPE_CHECKING:
    from control.core.io_controller import BoundEndpoint


class PiezoStage:
    def __init__(
        self,
        microcontroller: Microcontroller,
        config: dict,
        piezo_endpoint: Optional["BoundEndpoint"] = None,
    ):
        self._mc = microcontroller
        self._config = config
        self._current_position_um = 0
        self._home_position_um = config.get("OBJECTIVE_PIEZO_HOME_UM", 20)
        self._range_um = config.get("OBJECTIVE_PIEZO_RANGE_UM", 300)
        self._control_voltage_range = config.get("OBJECTIVE_PIEZO_CONTROL_VOLTAGE_RANGE", 5)
        self._flip_direction = config.get("OBJECTIVE_PIEZO_FLIP_DIR", False)
        self._piezo_ep = piezo_endpoint

    def move_to(self, position_um: float) -> None:
        """Move piezo to absolute position in micrometers"""
        if not 0 <= position_um <= self._range_um:
            raise ValueError(f"Position {position_um}um outside valid range 0-{self._range_um}um")

        if self._piezo_ep is not None:
            dac = int(65535 * (position_um / self._range_um))
            if self._flip_direction:
                dac = 65535 - dac
            self._piezo_ep.set_analog(dac)
        else:
            self._mc.set_piezo_um(position_um)
        self._current_position_um = position_um

    def move_relative(self, delta_um: float) -> None:
        """Move piezo by relative amount in micrometers"""
        new_pos = self._current_position_um + delta_um
        self.move_to(new_pos)

    def home(self) -> None:
        """Move piezo to home position"""
        self.move_to(self._home_position_um)

    @property
    def position(self) -> float:
        """Current position in micrometers"""
        return self._current_position_um

    @property
    def range_um(self) -> float:
        """Maximum range in micrometers"""
        return self._range_um
