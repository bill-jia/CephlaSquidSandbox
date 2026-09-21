"""
Pytest fixtures for control module tests.

This module provides fixtures to ensure proper cleanup of Microcontroller instances,
preventing background threads from causing segfaults in subsequent tests.
"""

import logging
from unittest.mock import patch

import pytest

import control.microcontroller
import control.microscope
from control.firmware_sim_serial import FirmwareSimSerial

logger = logging.getLogger(__name__)


def _make_tracking_init(original_init, instances_list):
    """Create a wrapper that tracks Microcontroller instances."""

    def _tracking_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        instances_list.append(self)

    return _tracking_init


@pytest.fixture(autouse=True)
def cleanup_microcontrollers():
    """
    Fixture that automatically cleans up all Microcontroller instances after each test.

    This prevents background threads from causing segfaults when subsequent tests run,
    especially those involving Qt event loops. The Microcontroller.read_received_packet
    method runs in a background thread that must be stopped via close().
    """
    # Track instances created during this test (scoped to this fixture invocation)
    active_microcontrollers = []

    # Capture original __init__ at fixture runtime, not module load time
    original_init = control.microcontroller.Microcontroller.__init__

    with patch.object(
        control.microcontroller.Microcontroller, "__init__", _make_tracking_init(original_init, active_microcontrollers)
    ):
        yield

    # Clean up all tracked instances
    for micro in active_microcontrollers:
        try:
            if hasattr(micro, "terminate_reading_received_packet_thread"):
                if not micro.terminate_reading_received_packet_thread:
                    micro.close()
        except Exception as e:
            logger.warning(f"Failed to close Microcontroller in test cleanup: {e}")


def _make_tracking_factory(original_factory, instances_list):
    """Create a wrapper that tracks Microscope instances built via the factory."""

    def _tracking_factory(*args, **kwargs):
        scope = original_factory(*args, **kwargs)
        instances_list.append(scope)
        return scope

    return _tracking_factory


@pytest.fixture(autouse=True)
def cleanup_microscopes():
    """
    Fixture that automatically closes every Microscope built via
    Microscope.build_from_global_config() during a test.

    A Microscope owns a camera, illumination controller, and addons on top of
    the Microcontroller that cleanup_microcontrollers already handles - all of
    which keep background threads (memory monitor, illumination watchdog
    heartbeat, camera streaming) alive until Microscope.close() runs. Tests
    that build a fresh Microscope (many now do, e.g. RAM-estimate and
    MultiPointController fixtures) never called close(), so those threads
    piled up for the rest of the pytest session. In this environment that
    accumulation reliably crashes the interpreter after only 2-3 un-closed
    Microscope instances, well before all tests in a file get a chance to run.
    """
    active_scopes = []
    original_factory = control.microscope.Microscope.build_from_global_config

    with patch.object(
        control.microscope.Microscope,
        "build_from_global_config",
        staticmethod(_make_tracking_factory(original_factory, active_scopes)),
    ):
        yield

    for scope in active_scopes:
        try:
            scope.close()
        except Exception as e:
            logger.warning(f"Failed to close Microscope in test cleanup: {e}")


@pytest.fixture
def firmware_sim():
    """
    Provide a FirmwareSimSerial instance with automatic cleanup.

    Validation errors and command counts are cleared before each test
    to ensure test isolation.
    """
    sim = FirmwareSimSerial(strict=True)
    yield sim
    sim.close()


@pytest.fixture
def firmware_sim_nonstrict():
    """
    Provide a non-strict FirmwareSimSerial instance for negative testing.

    In non-strict mode, invalid commands log warnings instead of raising
    FirmwareProtocolError, useful for testing error handling paths.
    """
    sim = FirmwareSimSerial(strict=False)
    yield sim
    sim.close()
