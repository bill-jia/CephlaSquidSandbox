"""Watchdog for blocking vendor camera-SDK calls.

Background
----------
A synchronous vendor-SDK call (a ctypes call into a native driver DLL) that
never returns cannot be interrupted from the calling thread. On 2026-07-02 a
96-well timelapse lost 51 of 116 timepoints when the acquisition worker parked
forever inside a Toupcam gain/reconfigure SDK call: the whole run froze mid-frame
and had to be killed manually ~2.5 days later. The frame-*read* paths were already
time-bounded, but the per-capture control/reconfigure calls (set exposure, set
gain, trigger) had no timeout and no watchdog, so one stuck transaction wedged the
entire acquisition.

This module turns an unbounded native hang into a fatal, catchable
``CameraTimeoutError``: the blocking call runs on a dedicated helper thread and the
caller waits with a timeout. On timeout the caller latches "wedged" — every later
call raises immediately — so the acquisition unwinds instead of piling calls behind
the stuck one or freezing forever.

Two deliberate design choices:

* ``CameraTimeoutError`` derives from :class:`BaseException`, NOT :class:`Exception`.
  The acquisition / observation-state code is full of routine ``except Exception``
  handlers that swallow per-operation glitches (e.g. "could not set analog gain,
  warning and continue"). A wedged camera is fatal, not a glitch, and must sail
  through those handlers to the top-level acquisition handler, which aborts and
  finalizes cleanly. Making it a BaseException guarantees it cannot be swallowed.

* The helper thread is a plain ``daemon`` thread fed by a queue, NOT a
  ``concurrent.futures.ThreadPoolExecutor``. A ThreadPoolExecutor registers an
  ``atexit`` hook that JOINS its worker on interpreter shutdown — which would block
  forever on a wedged worker and re-create the very shutdown hang we are removing.
  A daemon thread is abandoned at exit, so the process can always terminate.

The stuck helper thread is unrecoverable (blocked in native code that will never
return), so a wedge is terminal for that camera: the process must be restarted to
recover it. That is strictly better than an invisible multi-day freeze that loses
the store's finalize.
"""

import queue
import threading


class CameraTimeoutError(BaseException):
    """A vendor camera-SDK call did not return within its watchdog timeout.

    Derives from :class:`BaseException` (not :class:`Exception`) on purpose so it is
    not caught by the many routine ``except Exception`` handlers in the acquisition
    path — a wedged camera is fatal and must propagate to the top-level acquisition
    handler for a clean abort + finalize. See the module docstring.
    """


# Sentinel enqueued to ask the worker thread to exit.
_STOP = object()


class BoundedSdkCaller:
    """Run blocking vendor-SDK calls on one dedicated daemon thread, with a timeout.

    Usage::

        self._sdk = BoundedSdkCaller(default_timeout_s=15.0, log=self._log, name="toupcam")
        self._sdk.call("put_ExpoAGain", lambda: self._camera.put_ExpoAGain(v))

    Guarantees
    ----------
    * A call that exceeds ``timeout_s`` raises :class:`CameraTimeoutError` instead of
      blocking the caller forever.
    * After the first timeout the caller is *latched wedged*: every later call raises
      :class:`CameraTimeoutError` immediately (fast, no wait). The native call that
      wedged keeps running on the (daemon, abandoned) helper thread — we never submit
      behind it — so the caller unwinds within one acquisition iteration and the
      process can still exit.
    * Re-entrant: a call issued from the worker thread itself runs inline, so a
      wrapped method that calls another wrapped method does not deadlock the single
      worker.
    * Real SDK errors (e.g. HRESULTException raised by ``fn``) are re-raised
      unchanged on the caller thread — only an actual *hang* becomes a
      CameraTimeoutError, so non-hang behavior is unaffected.

    Only *control* calls (set exposure/gain, trigger, reconfigure) should be routed
    through this. The frame-arrival callback path must NOT — it runs on the SDK's own
    callback thread and is already bounded by the worker's frame-wait timeout.
    """

    def __init__(self, default_timeout_s: float, log, name: str = "sdk"):
        self._default_timeout_s = float(default_timeout_s)
        self._log = log
        self._name = name
        self._queue: "queue.Queue" = queue.Queue()
        self._wedged = False
        self._worker_thread_id = None
        self._ready = threading.Event()
        self._worker = threading.Thread(
            target=self._run, name=f"{name}-watchdog", daemon=True
        )
        self._worker.start()
        # Ensure _worker_thread_id is populated before any call() can consult it.
        self._ready.wait()

    @property
    def is_wedged(self) -> bool:
        return self._wedged

    def _run(self):
        self._worker_thread_id = threading.get_ident()
        self._ready.set()
        while True:
            job = self._queue.get()
            if job is _STOP:
                return
            fn, result_box, done = job
            try:
                result_box[0] = ("ok", fn())
            except BaseException as e:  # noqa: BLE001 - capture & hand back to caller thread
                result_box[0] = ("err", e)
            finally:
                done.set()

    def call(self, label, fn, timeout_s=None):
        """Run ``fn()`` bounded by a timeout; return its result or raise CameraTimeoutError."""
        # Re-entrancy: already on the worker thread -> run inline. Enqueueing to our
        # own single worker from within a job would wait on ourselves forever.
        if threading.get_ident() == self._worker_thread_id:
            return fn()

        if self._wedged:
            raise CameraTimeoutError(
                f"{self._name} camera SDK is wedged (an earlier call never returned); "
                f"refusing '{label}'. Restart the app to recover the camera."
            )

        timeout_s = self._default_timeout_s if timeout_s is None else float(timeout_s)
        result_box = [None]
        done = threading.Event()
        self._queue.put((fn, result_box, done))

        if not done.wait(timeout_s):
            # The job is still running on the worker thread and will never return.
            # Latch wedged so no further work is enqueued behind it.
            self._wedged = True
            self._log.error(
                f"[CAMERA-WATCHDOG] {self._name} SDK call '{label}' did not return within "
                f"{timeout_s:.1f}s. Marking the camera wedged and failing the acquisition. "
                f"The helper thread is stuck in a native driver call and cannot be recovered — "
                f"restart the application to reset the camera."
            )
            raise CameraTimeoutError(
                f"{self._name} camera SDK call '{label}' timed out after {timeout_s:.1f}s"
            )

        status, payload = result_box[0]
        if status == "err":
            raise payload
        return payload

    def shutdown(self):
        """Ask the worker to exit. Never blocks (the worker is a daemon thread)."""
        if not self._wedged:
            self._queue.put(_STOP)
