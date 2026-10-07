"""Unit tests for the camera SDK watchdog (control/_sdk_watchdog.py).

Hardware-free: imports only control._sdk_watchdog (control/__init__.py is empty),
so this exercises the timeout/wedge/re-entrancy logic without touching any camera,
NIDAQ, or driver module. Placed at the top-level tests/ dir so the hardware-ish
tests/control/conftest.py is not collected.
"""

import logging
import threading
import time

import pytest

from control._sdk_watchdog import BoundedSdkCaller, CameraTimeoutError


LOG = logging.getLogger("test_sdk_watchdog")


@pytest.fixture
def caller():
    c = BoundedSdkCaller(default_timeout_s=0.3, log=LOG, name="test")
    yield c
    c.shutdown()


def test_fatal_error_is_baseexception_not_exception():
    # A wedge must sail through routine `except Exception` handlers, so it must NOT
    # be an Exception subclass.
    assert issubclass(CameraTimeoutError, BaseException)
    assert not issubclass(CameraTimeoutError, Exception)


def test_normal_call_returns_value(caller):
    assert caller.call("ok", lambda: 21 * 2) == 42
    assert caller.is_wedged is False


def test_real_exception_from_fn_propagates_unchanged(caller):
    def boom():
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        caller.call("boom", boom)
    # A normal error is not a wedge.
    assert caller.is_wedged is False


def test_hang_raises_camera_timeout_and_latches_wedged(caller):
    release = threading.Event()

    def hang():
        release.wait(10)  # blocks until released; bounded so the daemon can exit

    try:
        t0 = time.monotonic()
        with pytest.raises(CameraTimeoutError):
            caller.call("hang", hang, timeout_s=0.2)
        elapsed = time.monotonic() - t0
        # Timed out roughly at the deadline, not instantly and not forever.
        assert 0.15 <= elapsed <= 2.0
        assert caller.is_wedged is True

        # Once wedged, subsequent calls fail FAST (no waiting on the stuck worker).
        t1 = time.monotonic()
        with pytest.raises(CameraTimeoutError):
            caller.call("after_wedge", lambda: 1)
        assert time.monotonic() - t1 < 0.1
    finally:
        release.set()


def test_reentrant_call_runs_inline_without_deadlock(caller):
    # A wrapped method that calls another wrapped method (i.e. call() invoked from
    # within a job on the worker thread) must run inline, or the single worker would
    # wait on itself forever. Use a short timeout so a deadlock would fail fast.
    def outer():
        # This inner call executes on the worker thread -> must run inline.
        return caller.call("inner", lambda: 7, timeout_s=0.2)

    assert caller.call("outer", outer, timeout_s=0.5) == 7
    assert caller.is_wedged is False


def test_wedged_caller_can_be_replaced_by_a_fresh_one():
    # This is the primitive ToupcamCamera.reopen() relies on: on a wedge it stands up
    # a NEW caller (fresh daemon thread, not wedged) and abandons the old one. Verify a
    # fresh caller is fully independent of a wedged predecessor.
    old = BoundedSdkCaller(default_timeout_s=0.2, log=LOG, name="old")
    release = threading.Event()
    try:
        with pytest.raises(CameraTimeoutError):
            old.call("hang", lambda: release.wait(10), timeout_s=0.2)
        assert old.is_wedged is True

        # reopen() would create a fresh caller and shut the old (wedged) one down.
        new = BoundedSdkCaller(default_timeout_s=0.2, log=LOG, name="new")
        old.shutdown()  # non-blocking; old's stuck daemon thread is abandoned
        try:
            assert new.is_wedged is False
            assert new.call("ok", lambda: "recovered") == "recovered"
        finally:
            new.shutdown()
    finally:
        release.set()


def test_shutdown_is_non_blocking_even_when_wedged():
    c = BoundedSdkCaller(default_timeout_s=0.2, log=LOG, name="test-shutdown")
    release = threading.Event()
    try:
        with pytest.raises(CameraTimeoutError):
            c.call("hang", lambda: release.wait(10), timeout_s=0.2)
        assert c.is_wedged is True
        # Must return promptly; the worker is stuck but is a daemon thread.
        t0 = time.monotonic()
        c.shutdown()
        assert time.monotonic() - t0 < 0.1
    finally:
        release.set()
