# Fast Acquisition — NIDAQ plot, shutter modes, and frame-rate

This covers the camera-timing features of the Fast Acquisition tab and its linked NIDAQ
waveform plot: the exposure-window overlay, the Tucsen Aries shutter modes, the reported
max frame rate, and interactive plot navigation.

## Camera exposure-window overlay

When the NIDAQ widget is linked to Fast Acquisition (the "Link to Fast Acquisition"
checkbox) **and** the "Show camera exposure overlay" checkbox is enabled, the Analog-Output
and Digital-Output plots are overlaid with **shaded bands showing when the camera sensor is
actually integrating light**, not just where the trigger pulses land. The overlay is **off by
default** because computing and drawing thousands of frame bands is expensive; toggle it on
when you want it. Two bands per frame:

- **Any row exposing** (light red): the union of every row's integration window,
  `[trigger, trigger + exposure + readout]`. For a rolling/global-reset sensor this is wider
  than the exposure alone because rows are read out sequentially.
- **All rows co-exposing** (darker red): the interval during which *every* row is integrating
  simultaneously — the window in which strobed/pulsed illumination reaches the whole frame
  uniformly. Its position depends on the shutter mode (below).

A thin dotted line marks each trigger/exposure start. The geometry is computed by
`camera_exposure_window_bars()` in `control/core/fast_acquisition_controller.py`; the
per-frame readout skew comes from `camera.get_readout_time_ms()`.

The overlay updates live as you change frame rate, frame count, total time, DAQ sample rate,
or **exposure time**; a shutter-mode change made in the Camera Settings block is picked up
within ~500 ms. It is cleared when the toggle is off, when unlinked, or while acquiring.

### Timing per shutter mode (Tucsen Aries, manual §3.3)

Let `t` = trigger, `E` = exposure, `R` = rolling readout skew (row-0-start to last-row-start).

| Mode | Any-row window | All-rows co-exposure |
|---|---|---|
| **Rolling** | `[t, t + E + R]` | `[t + R, t + E]` (empty if `E ≤ R`) |
| **Global Reset** | `[t, t + E + R]` | `[t, t + E]` |
| Global shutter (other cameras) | `[t, t + E]` | `[t, t + E]` |

- **Rolling**: each row starts exposure one line-time after the previous, so all rows only
  overlap in the middle of the exposure (and not at all when the exposure is shorter than the
  readout).
- **Global Reset**: all rows are reset and start exposure together, then read out rolling, so
  later rows integrate slightly longer. For clean strobed imaging the light should be gated
  off right after the first row finishes (`t + E`), which is the co-exposure window shown.

## Shutter mode (Tucsen Aries 6506 / 6510)

The Aries supports two sensor shutter modes — **Rolling** and **Global Reset** — selectable
from the *Shutter* dropdown in the **live Camera Settings block** (alongside exposure, gain
mode, binning, and ROI). (The Aries has no true global shutter; "Global Reset" is a
rolling-readout emulation.) The dropdown appears only for cameras that expose more than one
readout mode. Selecting a mode writes the GenICam `SensorShutterMode` node
(`camera_tucsen.py: set_readout_mode`) and persists across the vendor SDK close/reopen that
fast acquisition performs (the mode is snapshotted and re-asserted in
`_reopen_camera_to_reset_sdk_state`, otherwise it would silently revert to Rolling mid-run).
`get_readout_mode()` is cache-first, so the fast-acquisition exposure overlay can read the
current mode cheaply.

Modes map to `CameraReadoutMode`: `ROLLING` ↔ `"Rolling"`, `ROLLING_WITH_GLOBAL_RESET` ↔
`"GlobalReset"`.

## Max frame rate readout

The "max N Hz" label beside the frame-rate spinbox shows the camera's reported
`AcquisitionMaxFrameRate` — the **same GenICam node used in the streaming log messages**, read
fresh so the label always matches the camera's current state. On the Aries this is
`≈ 1/(exposure + readout)` (manual §3.13), so it reflects the current exposure / ROI / mode.
The label turns red when the requested frame rate exceeds it.

(The separate exposure-independent readout time used for the exposure-window overlay is still
derived in `camera_tucsen.py: _update_readout_period` / `get_readout_time_ms`, by reading
`AcquisitionMaxFrameRate` and `ExposureTime` fresh together and subtracting.)

## Interactive zoom / pan

The plot has a matplotlib navigation toolbar (pan, box-zoom, back/forward, **home** to reset,
save). The two **output** plots (AO and DO) share the x (time) axis, so zooming/panning one
moves both — the point is to line features up in time against the exposure windows. The
**acquired-input** plot (AI) has its own timebase and is zoomed independently.

Each redraw re-establishes the toolbar's Home target as the current full view (the nav
history is reset with `update()` + `push_current()` after autoscaling), so **Home always
returns to the full FOV** after dragging/zooming. Interactive zoom is preserved across overlay
redraws at the same acquisition duration (restored on top of the Home baseline) and resets to
the full view when the duration changes.
