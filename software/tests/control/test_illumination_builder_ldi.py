"""Tests for the LDI branch of ``_build_illumination_controller``.

The LDI is addressed by USB serial number and takes its intensity / shutter
control modes from the machine config, so the builder must pass
``connection.serial_number`` and ``config.{intensity_mode,shutter_mode}``
through to the driver, and must build the simulation variant when the
microscope is launched simulated.

The driver classes are monkeypatched, so these tests do not touch hardware and
do not depend on the concrete ``serial_peripherals.LDI`` implementation beyond
its constructor signature.
"""

import logging

import pytest

import control.microscope
import control.serial_peripherals
from control.lighting import SerialIlluminationDevice
from control.models.machine_config import MachineConfig
from squid.abc import LightSource


class FakeLDI(LightSource):
    """Records constructor kwargs; every LightSource method is a no-op."""

    instances = []

    def __init__(self, SN=None, intensity_mode="PC", shutter_mode="PC"):
        self.kwargs = {
            "SN": SN,
            "intensity_mode": intensity_mode,
            "shutter_mode": shutter_mode,
        }
        self.initialize_count = 0
        self.shut_down_count = 0
        self.intensities = {}
        self.shutter_states = {}
        type(self).instances.append(self)

    def initialize(self):
        self.initialize_count += 1

    def set_intensity_control_mode(self, mode):
        self.intensity_mode = mode

    def get_intensity_control_mode(self):
        return self.kwargs["intensity_mode"]

    def set_shutter_control_mode(self, mode):
        self.shutter_mode = mode

    def get_shutter_control_mode(self):
        return self.kwargs["shutter_mode"]

    def set_shutter_state(self, channel, state):
        self.shutter_states[channel] = state

    def get_shutter_state(self, channel):
        return self.shutter_states.get(channel, False)

    def set_intensity(self, channel, intensity):
        self.intensities[channel] = intensity

    def get_intensity(self, channel) -> float:
        return self.intensities.get(channel, 0.0)

    def shut_down(self):
        self.shut_down_count += 1


class FakeLDISimulation(FakeLDI):
    instances = []


class _StubConfigRepo:
    """Minimal stand-in for ConfigRepository used by the legacy fallback path."""

    def get_illumination_config(self):
        return None


def _machine_config(with_connection: bool = True) -> MachineConfig:
    entry = {
        "id": "ldi",
        "driver": "ldi",
        "config": {"intensity_mode": "EXT", "shutter_mode": "PC"},
        "channels": {
            "Fluorescence 488 nm Ex": {
                "wavelength_nm": 488,
                "type": "epi_illumination",
                "serial_key": "470",
            },
            "Fluorescence 561 nm Ex": {
                "wavelength_nm": 561,
                "type": "epi_illumination",
                "serial_key": "555",
            },
        },
    }
    if with_connection:
        entry["connection"] = {"serial_number": "ABC123"}
    return MachineConfig.model_validate({"illumination_devices": [entry]})


def _build(mc, simulated):
    return control.microscope._build_illumination_controller(
        mc,
        micro=None,
        io_registry=None,
        sci_array=None,
        simulated=simulated,
        config_repo=_StubConfigRepo(),
    )


def _serial_device(controller) -> SerialIlluminationDevice:
    serial_devices = [
        d for d in controller._devices if isinstance(d, SerialIlluminationDevice)
    ]
    assert len(serial_devices) == 1, f"expected one SerialIlluminationDevice, got {serial_devices}"
    return serial_devices[0]


@pytest.fixture(autouse=True)
def _reset_instances():
    FakeLDI.instances = []
    FakeLDISimulation.instances = []
    yield


def test_ldi_receives_serial_number_and_modes(monkeypatch):
    monkeypatch.setattr(control.serial_peripherals, "LDI", FakeLDI)

    controller = _build(_machine_config(), simulated=False)

    assert len(FakeLDI.instances) == 1
    ldi = FakeLDI.instances[0]
    assert ldi.kwargs == {"SN": "ABC123", "intensity_mode": "EXT", "shutter_mode": "PC"}
    assert ldi.initialize_count == 1

    dev = _serial_device(controller)
    assert set(dev.channel_names) == {"Fluorescence 488 nm Ex", "Fluorescence 561 nm Ex"}
    assert dev._channel_serial_keys["Fluorescence 488 nm Ex"] == "470"
    assert dev._channel_serial_keys["Fluorescence 561 nm Ex"] == "555"


def test_simulated_launch_builds_ldi_simulation(monkeypatch):
    monkeypatch.setattr(control.serial_peripherals, "LDI", FakeLDI)
    monkeypatch.setattr(control.serial_peripherals, "LDI_Simulation", FakeLDISimulation)

    controller = _build(_machine_config(), simulated=True)

    assert FakeLDI.instances == []
    assert len(FakeLDISimulation.instances) == 1
    sim = FakeLDISimulation.instances[0]
    assert sim.kwargs == {"SN": "ABC123", "intensity_mode": "EXT", "shutter_mode": "PC"}
    assert sim.initialize_count == 1

    dev = _serial_device(controller)
    assert set(dev.channel_names) == {"Fluorescence 488 nm Ex", "Fluorescence 561 nm Ex"}
    assert dev._channel_serial_keys["Fluorescence 488 nm Ex"] == "470"
    assert dev._channel_serial_keys["Fluorescence 561 nm Ex"] == "555"


def test_missing_serial_number_is_skipped_with_warning(monkeypatch, caplog):
    monkeypatch.setattr(control.serial_peripherals, "LDI", FakeLDI)

    with caplog.at_level(logging.WARNING, logger="squid.illumination"):
        controller = _build(_machine_config(with_connection=False), simulated=False)

    assert FakeLDI.instances == []
    assert not [d for d in controller._devices if isinstance(d, SerialIlluminationDevice)]
    assert "Fluorescence 488 nm Ex" not in controller._channel_map

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("ldi" in m for m in warnings), warnings
    assert any("serial_number" in m for m in warnings), warnings
