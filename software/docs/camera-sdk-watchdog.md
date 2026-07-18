# Camera SDK watchdog

## Why

On 2026-07-02 a 96-well QPM/FUCCI timelapse (planned 116 timepoints) lost **51
timepoints** (tp 65–115, ~44%). It did not crash: the `MultiPointWorker` acquisition
thread **hard-hung mid-frame** at 23:59:47 inside a Toupcam per-capture control call
(`set_analog_gain` → native `put_ExpoAGain` region), and sat frozen for ~2.5 days
until the app was killed manually. No exception, no traceback — a deadlock in a
native driver call.

Root cause: the Toupcam **control/reconfigure** SDK calls issued before every capture
(set exposure, set gain, software trigger) had **no timeout and no watchdog**. Only
the frame-*read* paths were time-bounded. A synchronous ctypes call into the vendor
DLL that never returns cannot be interrupted from the calling thread, and the
worker's abort is cooperative (a flag checked between Python operations), so one stuck
transaction wedged the entire remaining run — and left the zarr store unfinalized.

(This is unrelated to the fast-acquisition Tucsen ring-buffer drop work; that path
— `fast_acquisition_*`, `camera_tucsen`, `nidaq_fast` — is never used by a standard
Software-Trigger multipoint timelapse.)

## What the fix does

`software/control/_sdk_watchdog.py` provides `BoundedSdkCaller`: it runs a blocking
SDK call on a dedicated helper thread and waits with a timeout. If the call doesn't
return in time it raises `CameraTimeoutError` and **latches "wedged"** so every
subsequent call fails fast instead of piling up behind the stuck one.

Two deliberate choices:

- **`CameraTimeoutError` derives from `BaseException`, not `Exception`.** The
  acquisition / observation-state code has many routine `except Exception` handlers
  that swallow per-operation glitches (e.g. "could not set analog gain, warn and
  continue"). A wedged camera is fatal, so it must sail through those to the
  top-level acquisition handler. `apply_full_observation_state` also explicitly
  re-raises it past its `set_analog_gain` `except Exception`.
- **The helper is a daemon thread + queue, not a `ThreadPoolExecutor`.** A
  `ThreadPoolExecutor` registers an `atexit` hook that *joins* its worker on
  interpreter shutdown — which would block forever on a wedged worker and re-create
  the very shutdown hang we are removing. A daemon thread is abandoned at exit.

`ToupcamCamera` routes its per-capture control methods through the caller
(`set_exposure_time`, `set_analog_gain` wholesale; `send_trigger`'s native
`Trigger()` at the leaf so the NIDAQ/MCU hardware-trigger path stays on the caller
thread), with a 15 s timeout (`TOUPCAM_CONTROL_CALL_TIMEOUT_S`) — far above any
legitimate control call, far below a multi-minute timepoint.

## Reinit-and-continue recovery

Rather than aborting on the first wedge, the worker tries to **reopen the camera and
continue**, losing at most the current FOV instead of the rest of the run.

`ToupcamCamera.reopen()`: the wedged native handle is stuck in a call that will never
return and cannot be closed safely, so it is **abandoned** (along with its stuck
daemon watchdog thread — never joined). A **fresh** `BoundedSdkCaller` is created, a
new handle is opened (`_open`), base-configured (`_configure_camera`), and the tracked
logical state (acquisition/trigger mode, ROI, exposure) is restored — all under the
fresh watchdog, so a reopen that *itself* hangs (dead USB endpoint) raises
`CameraTimeoutError` and falls through to the abort. Frame callbacks live on the camera
object, so they survive the handle swap untouched. ROI and acquisition mode are tracked
in Python fields (`self._roi`, `self._acquisition_mode`) because their getters read the
now-dead handle.

`MultiPointWorker._recover_wedged_camera()` (bounded by `MAX_CAMERA_REINIT_ATTEMPTS`,
default 3; set to 0 to disable and get pure abort): turns off illumination, calls
`camera.reopen()`, then re-runs `_seed_camera_for_first_observation_state()` — the same
startup seed — to authoritatively restore ROI/binning/**conversion-gain camera_mode**/
pixel format from the observation-state config, so post-reopen frames match the
pre-wedge configuration. It then resets worker readiness flags and skips the current
FOV. **Any** failure (reinit disabled, budget exhausted, the reopen/re-seed itself
wedging, or any error) returns `False`, and the caller re-raises so `run()` does the
clean **abort + finalize** — the recovery is strictly best-effort and never worse than
abort-only.

If recovery is disabled or exhausted, `MultiPointWorker.run()` catches
`CameraTimeoutError` and calls `request_abort_fn()`, so the run **aborts and finalizes
cleanly**: frames captured up to the wedge are saved (shard-per-z commits land during
capture), the store is finalized, and the operator is alerted — instead of a silent
multi-day freeze. A camera that keeps wedging needs an app restart; the log says so.

## Follow-ups (not implemented)

- **Silent-drop detection** — frames-per-timepoint collapsed 3240→79 before the
  wedge with only one benign Toupcam warning logged; track frame-id gaps and
  raise/abort on shortfall so a degrading run alerts hours earlier.
- **Autofocus status** — `af_status='ok'` was logged for every row despite chronic
  focus-camera (`DefaultCamera`) read timeouts, because the pre-seeded focus-map path
  returns `True` without a live read; record `stale`/`table_fallback` distinctly.
- **Teardown calls** — `_stop_exposure`/`close` still issue unguarded SDK calls; a
  wedge can make app shutdown hang (a restart is expected after a wedge anyway).

## Tests

`software/tests/test_sdk_watchdog.py` — hardware-free unit tests for the caller
(normal return, real-exception passthrough, hang→timeout+wedge-latch, fast-fail after
wedge, re-entrancy runs inline, non-blocking shutdown, `BaseException` hierarchy).
Live validation of the end-to-end abort requires the rig.
