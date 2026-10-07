"""Software-trigger routing: default resolution, validation, and what it reaches.

A SOFTWARE_TRIGGER request is either virtualized onto the camera's hardware
trigger line or issued as the SDK's own software-trigger command.  That used to
be inferred from "did anyone hand the camera a hw_trigger_fn?", which was always
true, so a trigger line wired to a controller that never reached the camera only
surfaced as a mid-acquisition frame timeout.  These tests pin the explicit,
validated config decision that replaced it.

No hardware and no SDK: the camera side is exercised through CameraConfig and a
stub AbstractCamera subclass.
"""

import pytest

import squid.config
from control.models.machine_config import (
    DeviceEntry,
    DeviceIOLine,
    MachineConfig,
    SoftwareTriggerRouting,
    resolve_software_trigger_routing,
)
from squid.config import CameraConfig, CameraPixelFormat, CameraVariant


# ═══════════════════════════════════════════════════════════════════════════════
# Default resolution
# ═══════════════════════════════════════════════════════════════════════════════


def test_default_is_hardware_line_when_a_trigger_endpoint_is_declared():
    assert (
        resolve_software_trigger_routing(None, True) is SoftwareTriggerRouting.HARDWARE_LINE
    )


def test_default_is_native_without_a_trigger_endpoint():
    assert resolve_software_trigger_routing(None, False) is SoftwareTriggerRouting.NATIVE


@pytest.mark.parametrize("declares_endpoint", [True, False])
def test_explicit_native_wins_over_the_default(declares_endpoint):
    assert (
        resolve_software_trigger_routing("native", declares_endpoint)
        is SoftwareTriggerRouting.NATIVE
    )


def test_explicit_hardware_line_with_an_endpoint_is_accepted():
    assert (
        resolve_software_trigger_routing("hardware_line", True)
        is SoftwareTriggerRouting.HARDWARE_LINE
    )


def test_hardware_line_without_an_endpoint_is_rejected():
    with pytest.raises(ValueError, match="io.trigger endpoint"):
        resolve_software_trigger_routing("hardware_line", False)


def test_unknown_routing_value_is_rejected():
    with pytest.raises(ValueError, match="Unknown software_trigger_routing"):
        resolve_software_trigger_routing("free_run_snap", True)


def test_enum_value_round_trips():
    assert (
        resolve_software_trigger_routing(SoftwareTriggerRouting.NATIVE, True)
        is SoftwareTriggerRouting.NATIVE
    )


# ═══════════════════════════════════════════════════════════════════════════════
# MachineConfig validation
# ═══════════════════════════════════════════════════════════════════════════════


def _camera_entry(io=None, config=None):
    return DeviceEntry(
        driver="tucsen",
        role="main",
        io=io or {},
        config=config or {},
    )


_NIDAQ_TRIGGER = DeviceIOLine(controller="nidaq", channel_id="port0/line6")


def test_machine_config_rejects_hardware_line_without_a_trigger_line():
    with pytest.raises(ValueError, match="main_camera"):
        MachineConfig(
            devices={
                "main_camera": _camera_entry(
                    config={"software_trigger_routing": "hardware_line"}
                )
            }
        )


def test_machine_config_rejects_an_unknown_routing_value():
    with pytest.raises(ValueError, match="Unknown software_trigger_routing"):
        MachineConfig(
            devices={
                "main_camera": _camera_entry(
                    io={"trigger": _NIDAQ_TRIGGER},
                    config={"software_trigger_routing": "sideways"},
                )
            }
        )


def test_machine_config_accessor_resolves_the_default():
    mc = MachineConfig(
        devices={"main_camera": _camera_entry(io={"trigger": _NIDAQ_TRIGGER})}
    )
    assert (
        mc.get_software_trigger_routing("main_camera")
        is SoftwareTriggerRouting.HARDWARE_LINE
    )
    # A camera with no trigger line at all falls back to the native SDK command.
    assert (
        MachineConfig(devices={"main_camera": _camera_entry()}).get_software_trigger_routing(
            "main_camera"
        )
        is SoftwareTriggerRouting.NATIVE
    )


# ═══════════════════════════════════════════════════════════════════════════════
# CameraConfig field, built from a device entry
# ═══════════════════════════════════════════════════════════════════════════════


def test_camera_config_defaults_to_native():
    cfg = CameraConfig(
        camera_type=CameraVariant.TUCSEN, default_pixel_format=CameraPixelFormat.MONO16
    )
    assert cfg.software_trigger_routing is SoftwareTriggerRouting.NATIVE
    assert cfg.trigger_endpoint_description is None


def test_camera_config_from_device_carries_routing_and_endpoint():
    dev = _camera_entry(io={"trigger": _NIDAQ_TRIGGER}, config={"model": "ARIES-6506"})
    cfg = squid.config._build_camera_config_from_device(dev)

    assert cfg.software_trigger_routing is SoftwareTriggerRouting.HARDWARE_LINE
    assert cfg.trigger_endpoint_description == "nidaq port0/line6"


def test_camera_config_from_device_without_a_trigger_line_is_native():
    cfg = squid.config._build_camera_config_from_device(_camera_entry())

    assert cfg.software_trigger_routing is SoftwareTriggerRouting.NATIVE
    assert cfg.trigger_endpoint_description is None


def test_camera_config_from_device_honours_an_explicit_native_override():
    dev = _camera_entry(
        io={"trigger": _NIDAQ_TRIGGER},
        config={"software_trigger_routing": "native"},
    )
    cfg = squid.config._build_camera_config_from_device(dev)

    assert cfg.software_trigger_routing is SoftwareTriggerRouting.NATIVE
    # The line is still described — it exists, it is just not how triggers travel.
    assert cfg.trigger_endpoint_description == "nidaq port0/line6"


# ═══════════════════════════════════════════════════════════════════════════════
# describe_trigger_routing (AbstractCamera default)
# ═══════════════════════════════════════════════════════════════════════════════


class _RoutingOnlyCamera:
    """Just enough of AbstractCamera to exercise the base describe_trigger_routing."""

    from squid.abc import AbstractCamera

    describe_trigger_routing = AbstractCamera.describe_trigger_routing

    def __init__(self, config):
        self._config = config


def _describe(routing, endpoint):
    cfg = CameraConfig(
        camera_type=CameraVariant.TUCSEN,
        default_pixel_format=CameraPixelFormat.MONO16,
        software_trigger_routing=routing,
        trigger_endpoint_description=endpoint,
    )
    return _RoutingOnlyCamera(cfg).describe_trigger_routing()


def test_describe_names_the_endpoint():
    assert (
        _describe(SoftwareTriggerRouting.HARDWARE_LINE, "nidaq port0/line6")
        == "hardware_line via nidaq port0/line6"
    )


def test_describe_flags_hardware_line_with_no_endpoint():
    described = _describe(SoftwareTriggerRouting.HARDWARE_LINE, None)
    assert "hardware_line" in described
    assert "no io.trigger endpoint" in described


def test_describe_native_without_an_endpoint():
    assert _describe(SoftwareTriggerRouting.NATIVE, None) == "native"


# ═══════════════════════════════════════════════════════════════════════════════
# End-of-acquisition wait after a frame-timeout abort
# ═══════════════════════════════════════════════════════════════════════════════


class _FakeCamera:
    def get_total_frame_time(self):
        return 100.0  # ms -> _frame_wait_timeout_s() == 10.1 s

    def describe_trigger_routing(self):
        return "hardware_line via nidaq port0/line6"


def _worker_stub():
    """A MultiPointWorker with only the fields the wait path touches."""
    import threading

    import squid.logging
    from control.core.multi_point_worker import MultiPointWorker

    w = MultiPointWorker.__new__(MultiPointWorker)
    w._log = squid.logging.get_logger("test-worker")
    w.camera = _FakeCamera()
    w._aborted_on_frame_timeout = False
    w._ready_for_next_trigger = threading.Event()
    w._image_callback_idle = threading.Event()
    w._image_callback_idle.set()
    return w


def test_outstanding_frame_wait_is_skipped_after_a_frame_timeout_abort():
    import time

    w = _worker_stub()
    w._note_frame_timeout_abort()
    assert w._aborted_on_frame_timeout

    started = time.monotonic()
    w._wait_for_outstanding_callback_images()
    # The frame is never coming; teardown must not sit on _frame_wait_timeout_s().
    assert time.monotonic() - started < 1.0


def test_outstanding_frame_wait_still_blocks_on_the_normal_path():
    import threading
    import time

    w = _worker_stub()
    # Normal completion: the last frame lands a moment after teardown starts.
    threading.Timer(0.2, w._ready_for_next_trigger.set).start()

    started = time.monotonic()
    w._wait_for_outstanding_callback_images()
    elapsed = time.monotonic() - started

    assert 0.15 < elapsed < 5.0, "the normal-completion wait must not be skipped"


def test_worker_describes_the_camera_trigger_routing():
    w = _worker_stub()
    assert w._describe_camera_trigger_routing() == "hardware_line via nidaq port0/line6"


def test_worker_routing_description_tolerates_a_camera_without_the_accessor():
    w = _worker_stub()
    w.camera = object()
    assert w._describe_camera_trigger_routing() == "unknown"


# ═══════════════════════════════════════════════════════════════════════════════
# Every shipped library config resolves to a routing that can actually fire
# ═══════════════════════════════════════════════════════════════════════════════


def test_library_configs_resolve_a_usable_routing():
    from pathlib import Path

    import yaml

    library = Path(__file__).resolve().parents[2] / "machine_configs" / "library"
    configs = sorted(library.glob("machine_config*.yaml"))
    assert configs

    for path in configs:
        with open(path, encoding="utf-8") as f:
            mc = MachineConfig.model_validate(yaml.safe_load(f))
        cam = mc.get_device("main_camera")
        if cam is None or not cam.enabled:
            continue
        routing = mc.get_software_trigger_routing("main_camera")
        if routing is SoftwareTriggerRouting.HARDWARE_LINE:
            assert "trigger" in cam.io, f"{path.name}: hardware_line with no trigger line"
