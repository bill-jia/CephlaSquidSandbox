"""Port selection for the microcontroller.

Windows reports every USB CDC device with manufacturer "Microsoft", so a rig
that also has an LDI or CoolLED plugged in offers several candidates. Opening
the wrong one drives an unrelated device at 2 Mbaud, so selection must be exact.
"""

from types import SimpleNamespace

import pytest

import control.microcontroller as mcu


def _port(device, manufacturer="Microsoft", vid=None, serial_number=None, description="USB Serial Device"):
    return SimpleNamespace(
        device=device,
        manufacturer=manufacturer,
        vid=vid,
        pid=None,
        serial_number=serial_number,
        description=description,
    )


# The rig this was found on: unknown CDC device, LDI, Teensy, X-Light.
_RIG_PORTS = [
    _port("COM7", vid=1131, serial_number=""),
    _port("COM8", vid=12070, serial_number="00000001"),
    _port("COM9", vid=mcu.TEENSY_USB_VID, serial_number="16821080"),
    _port("COM10", manufacturer="FTDI", vid=1027, serial_number="A9KI1SXRA"),
]


@pytest.fixture
def opened(monkeypatch):
    """Record the port that would be opened instead of opening it."""
    seen = []

    def fake_serial(port, baudrate):
        seen.append((port, baudrate))
        return SimpleNamespace(port=port, baudrate=baudrate, close=lambda: None)

    monkeypatch.setattr(mcu.serial, "Serial", fake_serial)
    return seen


def _use_ports(monkeypatch, ports):
    monkeypatch.setattr(mcu.serial.tools.list_ports, "comports", lambda: list(ports))


def test_serial_number_picks_that_port(monkeypatch, opened):
    _use_ports(monkeypatch, _RIG_PORTS)

    mcu.get_microcontroller_serial_device(version="Teensy", sn="16821080")

    assert opened[0][0] == "COM9"


def test_teensy_vid_disambiguates_without_a_serial_number(monkeypatch, opened):
    _use_ports(monkeypatch, _RIG_PORTS)

    mcu.get_microcontroller_serial_device(version="Teensy")

    assert opened[0][0] == "COM9"


def test_ambiguous_candidates_raise_instead_of_guessing(monkeypatch, opened):
    # Two Teensies, no serial number configured: refuse rather than pick one.
    ports = [
        _port("COM9", vid=mcu.TEENSY_USB_VID, serial_number="16821080"),
        _port("COM11", vid=mcu.TEENSY_USB_VID, serial_number="99999999"),
    ]
    _use_ports(monkeypatch, ports)

    with pytest.raises(IOError, match="serial_number"):
        mcu.get_microcontroller_serial_device(version="Teensy")

    assert opened == []


def test_no_candidates_raise(monkeypatch, opened):
    _use_ports(monkeypatch, [_port("COM10", manufacturer="FTDI", vid=1027)])

    with pytest.raises(IOError, match="no controller found"):
        mcu.get_microcontroller_serial_device(version="Teensy")


def test_non_teensy_vid_still_selected_when_no_teensy_present(monkeypatch, opened):
    # A board that enumerates under another VID must still be found.
    _use_ports(monkeypatch, [_port("COM8", vid=12070, serial_number="00000001")])

    mcu.get_microcontroller_serial_device(version="Teensy")

    assert opened[0][0] == "COM8"


def test_failed_open_does_not_raise_from_del(monkeypatch):
    def boom(port, baudrate):
        raise mcu.serial.SerialException("port busy")

    monkeypatch.setattr(mcu.serial, "Serial", boom)

    with pytest.raises(mcu.serial.SerialException):
        mcu.MicrocontrollerSerial("COM7", 2000000)

    # The half-built object's destructor must stay quiet.
    obj = mcu.MicrocontrollerSerial.__new__(mcu.MicrocontrollerSerial)
    assert obj.close() is None
