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

## Frame-drop detection

`CameraFrame.frame_id` is a synthetic `+1` counter, so it can never reveal a drop —
which is why the 2026-07-02 collapse from 3240 to 79 frames/timepoint logged almost
nothing. `_on_frame_callback` now pulls the camera's real hardware frame sequence
(`ToupcamFrameInfoV2.seq`, previously discarded by passing `None` to `PullImageV2`) and,
on the normal capture path, `_note_frame_seq()` flags any gap (`seq − prev − 1 > 0`) as
a `[FRAME-DROP]` warning and accumulates `get_dropped_frame_count()`. A reset/wrap goes
negative and is ignored (no false alarm); the tracker resets on every stream (re)start
and on `reopen()`. The fast-acquisition path is excluded (it has its own accounting).

## Autofocus status

`autofocus_log.csv` wrote `af_status='ok'` for every row despite chronic focus-camera
read timeouts, because `_run_laser_af_refresh` returned `True` both for a live
measurement **and** for the stale-anchor+table fallback, and the table path never reads
the focus camera at all. `perform_autofocus`/`_run_laser_af_refresh` now record a
distinct `self._last_af_status` — `ok` (live measurement), `stale` (live read failed →
fell back to the stale anchor + table offset), `table` (no live read this FOV), `failed`
(no Z set), `skipped` (AF enabled but not performed) — and `_record_autofocus_event`
writes it. Focus-camera read failures now surface as `stale`/`failed` rows instead of
hiding behind `ok`.

## Follow-ups (not implemented)

- **Act on drops** — drop detection currently surfaces gaps (warning + counter); it does
  not yet abort the timepoint or trigger a reinit on a shortfall. Wiring
  `get_dropped_frame_count()` into the worker to react (not just log) is the next step.
- **Teardown calls** — `_stop_exposure` still issues unguarded SDK calls (`stop_streaming`
  and `close` are now guarded); a wedge there is benign since a restart is expected.

## Tests

`software/tests/test_sdk_watchdog.py` — hardware-free unit tests for the caller
(normal return, real-exception passthrough, hang→timeout+wedge-latch, fast-fail after
wedge, re-entrancy runs inline, non-blocking shutdown, `BaseException` hierarchy).
Live validation of the end-to-end abort requires the rig.
