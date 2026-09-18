"""A simulated microscope still gets an NI-DAQ, so nidaq-routed IO still binds.

``MicroscopeAddons`` used to build a DAQ only for real hardware, so in a
simulated run ``main_camera.trigger`` never bound, the camera got no
``hw_trigger_fn``, and Hardware trigger mode raised instead of producing frames.
Every illumination shutter routed to the DAQ was inert for the same reason.
These tests pin the wiring: which class is chosen, what the registry binds, and
that firing the trigger through the simulated DAQ costs nothing.

No hardware is constructed anywhere here. The only device entries in the test
machine configs are ``nidaq`` (simulated -> ``SimulatedNIDAQ``) and
``main_camera`` (never built by the addons builder); the real ``NIDAQ`` class is
monkeypatched when a test needs to prove it would have been selected.
"""

import logging
import time

import pytest

import control.microscope as microscope_mod
from control.core.io_controller import IORegistry, NIDAQIOController
from control.microscope import MicroscopeAddons
from control.models.machine_config import DeviceEntry, DeviceIOLine, MachineConfig
from control.models.io_endpoint_config import IOControllerType
from control.nidaq import NIDAQ, SimulatedNIDAQ, build_nidaq_config_from_io


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures — a minimal machine config with a DAQ-routed camera trigger
# ═══════════════════════════════════════════════════════════════════════════════


def _machine_config(nidaq_enabled: bool = True, nidaq_simulate: bool = False) -> MachineConfig:
    """nidaq + a camera whose trigger/readout lines live on the DAQ.

    Nothing else is declared, so the addons builder skips every other device
    without reaching a constructor.
    """
    return MachineConfig(
        devices={
            "nidaq": DeviceEntry(
                driver="nidaq",
                enabled=nidaq_enabled,
                simulate=nidaq_simulate,
                config={
                    "device_name": "Dev1",
                    "sample_rate": 100000,
                    "samples_per_channel": 1000,
                    "trigger_source": "SOFTWARE",
                    "logic_family": "THREE_POINT_THREE_V",
                },
            ),
            "main_camera": DeviceEntry(
                driver="tucsen",
                role="main",
                io={
                    "trigger": DeviceIOLine(
                        controller="nidaq",
                        signal_type="digital",
                        direction="output",
                        channel_id="port0/line6",
                    ),
                    "frame_readout": DeviceIOLine(
                        controller="nidaq",
                        signal_type="digital",
                        direction="input",
                        channel_id="port0/line7",
                    ),
                },
                config={"model": "ARIES-6506"},
            ),
            "led_488": DeviceEntry(
                driver="squid_builtin",
                io={
                    "shutter": DeviceIOLine(
                        controller="nidaq",
                        signal_type="digital",
                        direction="output",
                        channel_id="port0/line2",
                    ),
                    "intensity": DeviceIOLine(
                        controller="nidaq",
                        signal_type="analog",
                        direction="output",
                        channel_id="ao0",
                    ),
                },
            ),
        }
    )


class _FakeMicrocontroller:
    """Stands in for the MCU: no endpoint in these configs routes to it."""

    def supports_multi_port(self) -> bool:
        return False


def _build_addons(mc: MachineConfig, simulated: bool = True) -> MicroscopeAddons:
    return MicroscopeAddons.build_from_global_config(
        stage=None,
        micro=_FakeMicrocontroller(),
        simulated=simulated,
        machine_config=mc,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Which class gets built
# ═══════════════════════════════════════════════════════════════════════════════


def test_simulated_build_creates_a_simulated_nidaq():
    addons = _build_addons(_machine_config(), simulated=True)

    assert isinstance(addons.nidaq, SimulatedNIDAQ)
    # Built from the IO endpoints, not from an empty default config.
    assert addons.nidaq.do_lines == [2, 6]
    assert addons.nidaq.di_lines == [7]
    assert addons.nidaq.ao_channels == ["ao0"]


def test_per_device_simulate_flag_alone_simulates_the_nidaq(monkeypatch):
    # No global --simulation, but devices.nidaq.simulate: true.
    monkeypatch.setattr(microscope_mod, "NIDAQ", _never_constructed("NIDAQ"))

    addons = _build_addons(_machine_config(nidaq_simulate=True), simulated=False)

    assert isinstance(addons.nidaq, SimulatedNIDAQ)


def test_real_hardware_run_still_selects_the_real_class(monkeypatch):
    """Per-device flags decide: "simulated camera, real DAQ" must stay real.

    The real NIDAQ opens DAQmx tasks, so we assert on the *class chosen* with a
    stand-in rather than constructing one.
    """
    built = {}

    class _StandInForRealNIDAQ:
        def __init__(self, **config):
            built["config"] = config

    monkeypatch.setattr(microscope_mod, "NIDAQ", _StandInForRealNIDAQ)

    addons = _build_addons(_machine_config(nidaq_simulate=False), simulated=False)

    assert isinstance(addons.nidaq, _StandInForRealNIDAQ)
    assert not isinstance(addons.nidaq, SimulatedNIDAQ)
    assert built["config"]["device_name"] == "Dev1"


def test_disabled_nidaq_builds_nothing(monkeypatch):
    """A rig with no DAQ is unchanged: no object, real or simulated."""
    monkeypatch.setattr(microscope_mod, "NIDAQ", _never_constructed("NIDAQ"))
    monkeypatch.setattr(microscope_mod, "SimulatedNIDAQ", _never_constructed("SimulatedNIDAQ"))

    addons = _build_addons(_machine_config(nidaq_enabled=False), simulated=True)

    assert addons.nidaq is None
    assert addons.io_registry is not None
    assert addons.io_registry.get("main_camera.trigger") is None


def _never_constructed(name: str):
    class _Forbidden:
        def __init__(self, **_config):
            raise AssertionError(f"{name} must not be constructed here")

    return _Forbidden


# ═══════════════════════════════════════════════════════════════════════════════
# What the registry binds in simulation
# ═══════════════════════════════════════════════════════════════════════════════


def test_nidaq_endpoints_bind_in_simulation():
    addons = _build_addons(_machine_config(), simulated=True)
    registry = addons.io_registry

    for name in (
        "main_camera.trigger",
        "main_camera.frame_readout",
        "led_488.shutter",
        "led_488.intensity",
    ):
        bound = registry.get(name)
        assert bound is not None, f"{name} did not bind against a simulated NI-DAQ"
        assert bound.controller_type is IOControllerType.NIDAQ

    assert registry.validate() == []


def test_shutter_endpoint_reaches_the_simulated_daq():
    addons = _build_addons(_machine_config(), simulated=True)

    addons.io_registry.get("led_488.shutter").set_digital(True)
    addons.io_registry.get("led_488.intensity").set_analog(3.3)

    live = addons.nidaq.get_live_output_state()
    assert live["do"][2] is True
    assert live["ao"]["ao0"] == pytest.approx(3.3)


# ═══════════════════════════════════════════════════════════════════════════════
# Firing the trigger — the per-frame path, so it must be free
# ═══════════════════════════════════════════════════════════════════════════════


def _simulated_registry(mc: MachineConfig):
    io_config = mc.collect_io_endpoints()
    daq = SimulatedNIDAQ(
        **build_nidaq_config_from_io(
            device_name="Dev1", base_config=mc.get_device("nidaq").config, io_config=io_config,
        )
    )
    return daq, IORegistry(config=io_config, microcontroller=_FakeMicrocontroller(), nidaq=daq)


def test_simulated_nidaq_constructs():
    """Regression: __init__ re-applied its config positionally and raised."""
    daq = SimulatedNIDAQ(device_name="Dev1", sample_rate_hz=1000.0, samples_per_channel=10)

    assert daq.device_name == "Dev1"


def test_trigger_pulse_is_immediate_and_silent(caplog):
    mc = _machine_config()
    daq, registry = _simulated_registry(mc)
    trigger = registry.get("main_camera.trigger")

    with caplog.at_level(logging.WARNING):
        started = time.monotonic()
        for _ in range(20):
            trigger.send_trigger(control_illumination=True, illumination_on_time_us=20000)
        elapsed = time.monotonic() - started

    # 20 ms of requested pulse width x 20 frames would be 0.4 s if the simulated
    # DAQ honoured the sleep the real one uses.
    assert elapsed < 0.2, f"simulated trigger pulses took {elapsed:.3f}s"
    assert caplog.records == []


def test_readout_diagnostic_does_not_stall_or_warn_in_simulation(caplog):
    """The real DAQ samples a DI line for 250 ms and warns when it sees nothing.

    There is no line to sample in simulation, so the diagnostic must be a no-op
    rather than 250 ms and a "camera is NOT receiving the pulse" warning on
    every frame.
    """
    mc = _machine_config()
    daq, registry = _simulated_registry(mc)
    controller = registry.get_controller(IOControllerType.NIDAQ)
    assert isinstance(controller, NIDAQIOController)
    controller.set_trigger_readout_line(7, window_ms=250.0)

    with caplog.at_level(logging.WARNING):
        started = time.monotonic()
        registry.get("main_camera.trigger").send_trigger()
        elapsed = time.monotonic() - started

    assert elapsed < 0.05
    assert caplog.records == []


def test_camera_trigger_fn_fires_through_the_simulated_daq(monkeypatch):
    """What the camera is handed as hw_trigger_fn actually reaches the DAQ."""
    mc = _machine_config()
    daq, registry = _simulated_registry(mc)
    pulses = []
    monkeypatch.setattr(
        daq, "send_edge_pulse", lambda line, **kwargs: pulses.append((line, kwargs))
    )

    trigger = registry.get("main_camera.trigger")
    trigger.send_trigger(control_illumination=True, illumination_on_time_us=5000)

    assert pulses == [(6, {"pulse_width_us": 5000, "readout_line": None, "readout_window_ms": 250.0})]
