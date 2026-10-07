"""Unit tests for the Lumencor LDI serial driver (control.serial_peripherals.LDI).

The serial layer is faked out entirely: every test monkeypatches
``control.serial_peripherals.SerialDevice`` with a recorder, so no COM port is touched.
"""

from typing import List

import pytest

import control.serial_peripherals as sp
from control.lighting import IntensityControlMode, ShutterControlMode


class FakeSerialDevice:
    """Stand-in for SerialDevice that records the commands written to it."""

    # Set by make_fake_serial_device() for each test.
    commands: List[str] = []
    port_found: bool = True
    read_response: str = "SET:405=10,470=20\r"

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        # SerialDevice leaves .serial as None when no port matches the requested serial number.
        self.serial = object() if self.port_found else None

    def open_ser(self, *args, **kwargs):
        pass

    def close(self):
        pass

    def write_and_check(self, command, expected_response, **kwargs):
        type(self).commands.append(command)
        return expected_response

    def write_and_read(self, command, **kwargs):
        type(self).commands.append(command)
        return type(self).read_response


def make_fake_serial_device(monkeypatch, port_found: bool = True) -> type:
    """Install a fresh FakeSerialDevice subclass as control.serial_peripherals.SerialDevice."""
    fake = type("FakeSerialDeviceForTest", (FakeSerialDevice,), {"commands": [], "port_found": port_found})
    monkeypatch.setattr(sp, "SerialDevice", fake)
    return fake


def make_ldi(monkeypatch, SN: str = "X", intensity_mode: str = "PC", shutter_mode: str = "PC"):
    fake = make_fake_serial_device(monkeypatch)
    ldi = sp.LDI(SN, intensity_mode, shutter_mode)
    fake.commands.clear()  # drop anything the constructor might have sent
    return ldi, fake


def test_initialize_pushes_pc_modes(monkeypatch):
    ldi, fake = make_ldi(monkeypatch, "X", "PC", "PC")
    ldi.initialize()
    assert fake.commands == ["run!\r", "INT_MODE=PC\r", "SH_MODE=PC\r"]
    assert ldi.intensity_mode == IntensityControlMode.Software
    assert ldi.shutter_mode == ShutterControlMode.Software


def test_initialize_pushes_ext_modes(monkeypatch):
    ldi, fake = make_ldi(monkeypatch, "X", "EXT", "EXT")
    assert ldi.intensity_mode == IntensityControlMode.SquidControllerDAC
    assert ldi.shutter_mode == ShutterControlMode.TTL

    ldi.initialize()
    assert fake.commands == ["run!\r", "INT_MODE=EXT\r", "SH_MODE=EXT\r"]


def test_mode_strings_are_case_insensitive(monkeypatch):
    ldi, _ = make_ldi(monkeypatch, "X", "ext", "pc")
    assert ldi.intensity_mode == IntensityControlMode.SquidControllerDAC
    assert ldi.shutter_mode == ShutterControlMode.Software


def test_set_intensity_command(monkeypatch):
    ldi, fake = make_ldi(monkeypatch)
    ldi.set_intensity("470", 12.5)
    assert fake.commands == ["set:470=12.50\r"]


def test_set_shutter_state_command(monkeypatch):
    ldi, fake = make_ldi(monkeypatch)
    ldi.set_shutter_state("470", True)
    assert fake.commands == ["shutter:470=True\r"]


def test_opening_a_second_channel_closes_the_first(monkeypatch):
    ldi, fake = make_ldi(monkeypatch)
    ldi.set_shutter_state("470", True)
    fake.commands.clear()

    ldi.set_shutter_state("555", True)
    assert fake.commands == ["shutter:470=False\r", "shutter:555=True\r"]
    assert ldi.active_channel == "555"


@pytest.mark.parametrize(
    "intensity_mode,shutter_mode,bad_value",
    [("BOGUS", "PC", "BOGUS"), ("PC", "TTL", "TTL")],
)
def test_invalid_mode_raises_value_error(monkeypatch, intensity_mode, shutter_mode, bad_value):
    make_fake_serial_device(monkeypatch)
    with pytest.raises(ValueError) as excinfo:
        sp.LDI("X", intensity_mode, shutter_mode)
    assert bad_value in str(excinfo.value)


def test_missing_serial_number_raises_serial_device_error(monkeypatch):
    make_fake_serial_device(monkeypatch, port_found=False)

    class _Port:
        device = "COM7"
        serial_number = "12345678"

    monkeypatch.setattr(sp.list_ports, "comports", lambda: [_Port()])

    with pytest.raises(sp.SerialDeviceError) as excinfo:
        sp.LDI("nope")
    message = str(excinfo.value)
    assert "nope" in message
    assert "12345678" in message


def test_get_intensity_parses_the_response(monkeypatch):
    ldi, fake = make_ldi(monkeypatch)
    assert ldi.get_intensity(470) == 20
    assert ldi.get_intensity("405") == 10
    assert fake.commands == ["set?\r", "set?\r"]


def test_simulation_mirrors_the_constructor():
    sim = sp.LDI_Simulation()
    assert sim.SN is None
    assert sim.intensity_mode == IntensityControlMode.Software
    assert sim.shutter_mode == ShutterControlMode.Software

    sim = sp.LDI_Simulation("X", "EXT", "EXT")
    assert sim.intensity_mode == IntensityControlMode.SquidControllerDAC
    assert sim.shutter_mode == ShutterControlMode.TTL
    sim.initialize()

    with pytest.raises(ValueError):
        sp.LDI_Simulation("X", "PC", "nonsense")
