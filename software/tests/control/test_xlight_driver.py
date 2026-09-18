"""Unit tests for the CrestOptics X-Light serial driver (control.serial_peripherals.XLight).

The serial layer is faked out entirely: every test monkeypatches
``control.serial_peripherals.SerialDevice`` with a recorder, so no COM port is touched.

The slot count and the "read the wheel back" flag come from the device entry
(``devices.xlight.config`` in machine_config.yaml), not from module globals.
"""

from typing import List

import pytest

import control.serial_peripherals as sp
from control.models.machine_config import ConfocalDeviceSettings, DeviceEntry


class FakeSerialDevice:
    """Stand-in for SerialDevice that records the commands written to it."""

    commands: List[str] = []
    calls: List[tuple] = []
    # Valid hex so XLight._connect_and_detect picks the V3 protocol.
    idc_response: str = "00000FFF"

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.serial = object()

    def open_ser(self, *args, **kwargs):
        pass

    def close(self):
        pass

    def write(self, command):
        type(self).commands.append(command)
        type(self).calls.append(("write", command))

    def write_and_check(self, command, expected_response, **kwargs):
        type(self).commands.append(command)
        type(self).calls.append(("check", command))
        return expected_response

    def write_and_read(self, command, **kwargs):
        type(self).commands.append(command)
        type(self).calls.append(("read", command))
        return type(self).idc_response


def make_xlight(monkeypatch, **kwargs):
    fake = type("FakeSerialDeviceForTest", (FakeSerialDevice,), {"commands": [], "calls": []})
    monkeypatch.setattr(sp, "SerialDevice", fake)
    kwargs.setdefault("sleep_time_for_wheel", 0.0)
    xlight = sp.XLight("SN", **kwargs)
    fake.commands.clear()  # drop the idc handshake
    fake.calls.clear()
    return xlight, fake


class TestEmissionFilterSlotCount:
    """set_emission_filter validates against the instance's slot count."""

    def test_rejects_slot_beyond_count(self, monkeypatch):
        xlight, _ = make_xlight(monkeypatch, emission_filter_positions=5)
        with pytest.raises(ValueError, match="must be 1-5"):
            xlight.set_emission_filter(6)

    def test_accepts_slot_within_count(self, monkeypatch):
        xlight, fake = make_xlight(monkeypatch, emission_filter_positions=5)
        assert xlight.set_emission_filter(5) == 5
        assert fake.commands == ["B5\r"]

    def test_rejects_zero(self, monkeypatch):
        xlight, _ = make_xlight(monkeypatch, emission_filter_positions=8)
        with pytest.raises(ValueError, match="must be 1-8"):
            xlight.set_emission_filter(0)

    def test_simulation_rejects_slot_beyond_count(self):
        xlight = sp.XLight_Simulation(emission_filter_positions=3)
        with pytest.raises(ValueError, match="must be 1-3"):
            xlight.set_emission_filter(4)
        assert xlight.set_emission_filter(3) == 3

    def test_simulation_defaults_to_eight_slots(self):
        xlight = sp.XLight_Simulation()
        assert xlight.emission_filter_positions == 8
        assert xlight.set_emission_filter(8) == 8
        with pytest.raises(ValueError):
            xlight.set_emission_filter(9)

    def test_slot_count_comes_from_declared_wheel(self, monkeypatch):
        """len(positions) on the device entry is the slot count the driver enforces."""
        entry = DeviceEntry(
            driver="xlight",
            config={"emission_filter_wheel": {"positions": {1: "Empty", 2: "LP 500"}}},
        )
        settings = ConfocalDeviceSettings.from_device_entry(entry)
        xlight, _ = make_xlight(monkeypatch, emission_filter_positions=settings.emission_filter_positions)
        assert xlight.set_emission_filter(2) == 2
        with pytest.raises(ValueError, match="must be 1-2"):
            xlight.set_emission_filter(3)


class TestValidateWheelPos:
    """The instance's validate_wheel_pos flag is the default for set_emission_filter."""

    def test_validate_off_writes_without_readback(self, monkeypatch):
        xlight, fake = make_xlight(monkeypatch, validate_wheel_pos=False)
        xlight.set_emission_filter(3)
        assert fake.calls == [("write", "B3\r")]
        assert xlight.emission_wheel_pos == 3

    def test_validate_on_reads_back(self, monkeypatch):
        xlight, fake = make_xlight(monkeypatch, validate_wheel_pos=True)
        xlight.set_emission_filter(3)
        assert fake.calls == [("check", "B3\r")]
        assert xlight.emission_wheel_pos == 3

    def test_explicit_validate_overrides_instance_flag(self, monkeypatch):
        xlight, fake = make_xlight(monkeypatch, validate_wheel_pos=False)
        xlight.set_emission_filter(2, validate=True)
        assert fake.calls == [("check", "B2\r")]
        assert xlight.emission_wheel_pos == 2

    def test_disabled_wheel_is_a_noop(self, monkeypatch):
        xlight, fake = make_xlight(monkeypatch, disable_emission_filter_wheel=True)
        assert xlight.set_emission_filter(3) == -1
        assert fake.commands == []
