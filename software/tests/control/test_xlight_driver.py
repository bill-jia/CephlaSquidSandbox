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

    closed: bool = False

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.serial = object()

    def open_ser(self, *args, **kwargs):
        pass

    def close(self):
        type(self).closed = True
        type(self).calls.append(("close", None))

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


def make_xlight(monkeypatch, idc_response=FakeSerialDevice.idc_response, **kwargs):
    fake = type(
        "FakeSerialDeviceForTest",
        (FakeSerialDevice,),
        {"commands": [], "calls": [], "closed": False, "idc_response": idc_response},
    )
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


class TestEmissionFilterWriteSkipping:
    """A wheel move the hardware is already making is not written again.

    ``sleep_time_for_wheel`` is paid on every move, and a multi-channel
    acquisition asks for the same slot on every channel switch.
    """

    def test_repeat_request_writes_once(self, monkeypatch):
        xlight, fake = make_xlight(monkeypatch)
        xlight.set_emission_filter(3)
        xlight.set_emission_filter(3)
        assert xlight.set_emission_filter(3) == 3
        assert fake.commands == ["B3\r"]
        assert xlight.emission_wheel_pos == 3

    def test_changed_request_writes_again(self, monkeypatch):
        xlight, fake = make_xlight(monkeypatch)
        xlight.set_emission_filter(3)
        xlight.set_emission_filter(4)
        xlight.set_emission_filter(4)
        xlight.set_emission_filter(3)
        assert fake.commands == ["B3\r", "B4\r", "B3\r"]
        assert xlight.emission_wheel_pos == 3

    def test_first_request_always_writes(self, monkeypatch):
        """The wheel is unknown at startup, so even slot 1 is written once."""
        xlight, fake = make_xlight(monkeypatch)
        assert xlight.emission_wheel_pos is None
        xlight.set_emission_filter(1)
        assert fake.commands == ["B1\r"]

    def test_string_and_int_slots_are_the_same_slot(self, monkeypatch):
        xlight, fake = make_xlight(monkeypatch)
        xlight.set_emission_filter("3")
        xlight.set_emission_filter(3)
        assert fake.commands == ["B3\r"]

    def test_failed_write_is_retried(self, monkeypatch):
        """A write that raises leaves no cached slot for the next call to skip."""
        xlight, fake = make_xlight(monkeypatch)
        xlight.set_emission_filter(2)

        fail = {"on": True}
        record = xlight.serial_connection.write

        def maybe_boom(command):
            if fail["on"]:
                raise sp.SerialDeviceError("no response")
            record(command)

        monkeypatch.setattr(xlight.serial_connection, "write", maybe_boom)
        with pytest.raises(sp.SerialDeviceError):
            xlight.set_emission_filter(4)
        assert xlight.emission_wheel_pos is None

        fail["on"] = False
        assert xlight.set_emission_filter(4) == 4
        assert fake.commands == ["B2\r", "B4\r"]

    def test_readback_seeds_the_cache(self, monkeypatch):
        """The confocal panel reads the wheel at startup; that read counts."""
        xlight, fake = make_xlight(monkeypatch)
        monkeypatch.setattr(xlight.serial_connection, "write_and_check", lambda *a, **k: "rB4")
        assert xlight.get_emission_filter() == 4
        fake.commands.clear()
        assert xlight.set_emission_filter(4) == 4
        assert fake.commands == []

    def test_validated_move_still_reads_back(self, monkeypatch):
        """Skipping redundant writes must not disturb validate_wheel_pos."""
        xlight, fake = make_xlight(monkeypatch, validate_wheel_pos=True)
        xlight.set_emission_filter(3)
        xlight.set_emission_filter(3)
        xlight.set_emission_filter(2)
        assert fake.calls == [("check", "B3\r"), ("check", "B2\r")]
        assert xlight.emission_wheel_pos == 2

    def test_extraction_move_is_never_skipped(self, monkeypatch):
        xlight, fake = make_xlight(monkeypatch)
        xlight.set_emission_filter(3)
        xlight.set_emission_filter(3, extraction=True)
        assert fake.commands == ["B3\r", "B3m\r"]

    def test_simulation_lands_on_the_requested_slot(self):
        xlight = sp.XLight_Simulation()
        assert xlight.set_emission_filter(1) == 1  # equals the simulated start
        assert xlight.emission_wheel_pos == 1
        assert xlight.set_emission_filter(4) == 4
        assert xlight.set_emission_filter(4) == 4
        assert xlight.emission_wheel_pos == 4


class TestIrisWriteSkipping:
    """Each iris round-trip sleeps 2 s, so the same aperture is written once."""

    def test_repeat_value_writes_once(self, monkeypatch):
        xlight, fake = make_xlight(monkeypatch)
        xlight.set_illumination_iris(80)
        xlight.set_illumination_iris(80)
        xlight.set_emission_iris(60)
        xlight.set_emission_iris(60)
        assert fake.commands == ["J800\r", "V600\r"]

    def test_changed_value_writes_again(self, monkeypatch):
        xlight, fake = make_xlight(monkeypatch)
        xlight.set_illumination_iris(80)
        xlight.set_illumination_iris(50)
        xlight.set_emission_iris(60)
        xlight.set_emission_iris(60)
        xlight.set_emission_iris(10)
        assert fake.commands == ["J800\r", "J500\r", "V600\r", "V100\r"]

    def test_closed_iris_is_written_once_at_startup(self, monkeypatch):
        """0 is a real aperture, not "unknown" - the first request must write."""
        xlight, fake = make_xlight(monkeypatch)
        assert xlight.illumination_iris is None
        assert xlight.emission_iris is None
        xlight.set_illumination_iris(0)
        xlight.set_illumination_iris(0)
        assert fake.commands == ["J0\r"]

    def test_failed_write_is_retried(self, monkeypatch):
        xlight, fake = make_xlight(monkeypatch)
        xlight.set_emission_iris(60)

        fail = {"on": True}
        record = xlight.serial_connection.write_and_read

        def maybe_boom(command, **kwargs):
            if fail["on"]:
                raise sp.SerialDeviceError("no response")
            return record(command, **kwargs)

        monkeypatch.setattr(xlight.serial_connection, "write_and_read", maybe_boom)
        with pytest.raises(sp.SerialDeviceError):
            xlight.set_emission_iris(20)
        assert xlight.emission_iris is None

        fail["on"] = False
        assert xlight.set_emission_iris(20) == 20
        assert fake.commands == ["V600\r", "V200\r"]

    def test_simulation_lands_on_the_requested_value(self):
        xlight = sp.XLight_Simulation()
        assert xlight.illumination_iris == 100  # a real unit powers up wide open
        assert xlight.set_illumination_iris(0) == 0
        assert xlight.set_illumination_iris(70) == 70
        assert xlight.set_illumination_iris(70) == 70
        assert xlight.illumination_iris == 70
        assert xlight.set_emission_iris(40) == 40
        assert xlight.emission_iris == 40


class TestClose:
    """close() parks the spinning disk motor (when present) and releases the port."""

    def test_stops_motor_then_closes_port(self, monkeypatch):
        xlight, fake = make_xlight(monkeypatch, idc_response="00000FFF")
        assert xlight.has_spinning_disk_motor
        xlight.close()
        assert fake.calls == [("check", "N0\r"), ("close", None)]
        assert fake.closed
        assert xlight.disk_motor_state is False

    def test_skips_motor_command_when_absent(self, monkeypatch):
        xlight, fake = make_xlight(monkeypatch, idc_response="00000FFE")
        assert not xlight.has_spinning_disk_motor
        xlight.close()
        assert fake.commands == []
        assert fake.calls == [("close", None)]
        assert fake.closed

    def test_port_closes_even_if_motor_stop_fails(self, monkeypatch):
        xlight, fake = make_xlight(monkeypatch, idc_response="00000FFF")

        def boom(*args, **kwargs):
            raise sp.SerialDeviceError("no response")

        monkeypatch.setattr(xlight.serial_connection, "write_and_check", boom)
        xlight.close()
        assert fake.closed

    def test_simulation_close_parks_motor(self):
        xlight = sp.XLight_Simulation()
        xlight.set_disk_motor_state(True)
        xlight.close()
        assert xlight.disk_motor_state is False
