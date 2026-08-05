# Bench Touch Console

A finger-driven second window for scopes with no eyepiece and no room for a keyboard and mouse.

The console lives on a small touchscreen next to the microscope and covers what has to happen with
a hand on the sample: look at it, find it, focus it, snap a frame, and start a saved acquisition.
Everything else — building acquisitions, calibration, analysis — happens on the full GUI, which you
reach by remoting into the scope PC from your desk.

```
┌───────────────────────────┐        ┌──────────────────────────────┐
│  Touchscreen at the bench │        │  Desk PC (15–30 s walk)      │
│  TouchConsoleWindow       │        │  remote session → full GUI   │
└─────────────┬─────────────┘        └───────────────┬──────────────┘
              │                                      │
              └──────────────┬───────────────────────┘
                             ▼
                 ONE Squid process on the scope PC
                 (hardware is single-owner)
```

## Setting it up

### 1. Remote access — do this first

**Do not use Windows RDP.** `mstsc` disconnects and locks the physical console session: the
touchscreen goes black the moment you remote in from your desk, and Qt receives monitor-detach
geometry events that scramble the two-window layout.

Use a tool that *mirrors* the console session instead — UltraVNC/TightVNC, Parsec, Sunshine +
Moonlight, or NoMachine. At 15–30 s walking distance even plain VNC is fast enough.

Also:

- Keep a display physically attached, or fit an HDMI dummy plug, so desktop geometry never changes.
- Disable sleep and lock on the scope PC.
- **Set both monitors to the same Windows display scaling** (100% on both is safest). Qt5's
  per-screen DPI handling is unreliable, and the console is laid out in absolute pixels.

### 2. Hardware

A 10–15" HDMI+USB or USB-C touch monitor on an articulating arm. Avoid tablet-as-second-display
software (spacedesk, Duet) — it adds encode/decode latency and USB flakiness for no benefit when the
panel can simply be cabled.

### 3. Launch

```
Squid.bat --touch-console          # use the configured screen index
Squid.bat --touch-console 1        # explicit screen index
```

Or set `TOUCH_CONSOLE_ENABLED = True` in `control/_def.py` to have it always on.

If the requested screen index does not exist, the console opens as an ordinary resizable window on
the primary screen and logs a warning — which is also how you develop against it on a single-monitor
machine.

## Configuration

All in `control/_def.py`:

| Flag | Default | Meaning |
|---|---|---|
| `TOUCH_CONSOLE_ENABLED` | `False` | Always show the console (equivalent to passing `--touch-console`) |
| `TOUCH_CONSOLE_SCREEN` | `1` | Index into `QGuiApplication.screens()` |
| `TOUCH_CONSOLE_FPS` | `15` | Console display rate, independent of the main window's |
| `TOUCH_CONSOLE_MAX_DIM` | `1024` | Frames are resized down to this largest dimension |
| `TOUCH_CONSOLE_SNAP_DIR` | `<DEFAULT_SAVING_PATH>/bench_snaps` | Where Snap writes |
| `TOUCH_CONSOLE_PRESET_DIR` | `<DEFAULT_SAVING_PATH>/bench_presets` | Scanned for `*.yaml` acquisition presets |

To offer a one-tap acquisition at the bench, save it from the full GUI as usual and copy the
resulting `acquisition.yaml` into `TOUCH_CONSOLE_PRESET_DIR` under a descriptive filename — the
filename becomes the button label.

## What the panel can do

- **Live** — start/stop, snap, autolevel
- **Channel** — one button per saved Observation State preset
- **Exposure / gain** — ± steppers
- **Illumination** — per-illuminator on/off, a coarse slider plus ±1% fine steppers, and a master off
- **Focus** — Z jog at 1 / 5 / 25 µm with press-and-hold repeat, and autofocus
- **Stage** — XY D-pad at 0.05 / 0.2 / 1.0 mm, plus **tap the image to center that point**
- **Objective** — one button per configured objective
- **Acquire** — one button per preset, with confirm, live progress, and abort

Zoom is by button (`+` / `−` / `Fit`) and by pinch. Single-finger drag pans, because Qt synthesizes
mouse events for unhandled touches and pyqtgraph's ViewBox handles those.

## What it deliberately cannot do

- **No text entry.** There is no keyboard at the bench. The snap directory is fixed by config and
  filenames are generated; acquisition presets are picked from a list, never typed.
- **No acquisition building.** Regions, z-stacks, timelapses, and channel sets are configured on the
  full GUI.
- **No closing.** `closeEvent` is ignored unless the main window is shutting down, so a stray touch
  cannot strand you at the bench with no display.
- **Nothing during an acquisition** except watching progress and pressing Abort — every other
  control is disabled while the acquisition owns the scope.

## How it works

### One process, two windows

The console is a second `QMainWindow` in the existing app, not a separate program. It has to be:
hardware in this codebase is single-owner and nothing arbitrates across processes.

- Camera drivers open exclusively — `TUCAM_Dev_Open` (`camera_tucsen.py`), `pvc.init_pvcam()`
  (`camera_photometrics.py`), `DeviceAccessType_Control` (`camera_ids.py`).
- The microcontroller serial port is exclusive per-process on Windows (`microcontroller.py`).
- `nidaq.py` documents a single-owner invariant: one DAQmx task holds every DO line the system
  touches.

Every concurrency guard in the codebase is an in-process `threading.Lock`. A second process that
built its own `Microscope` would fail at open or corrupt state.

### The console drives widgets, not controllers

`ObservationStateController` is the single mediator for exposure, gain, and illumination — but it is
Qt-free and emits no change notifications. Two UIs writing to it directly would silently disagree
about the current state.

So the console never calls controllers for state-bearing operations. It pokes the *same input
controls the main GUI owns* and lets their existing signal chains apply to hardware and refresh the
main window:

| Console action | What it actually calls |
|---|---|
| Live on/off | `liveControlWidget.btn_live.click()` |
| Snap | `liveControlWidget.snap_frame()` |
| Autolevel | `liveControlWidget.btn_autolevel.click()` |
| Channel preset | `run_load_observation_state(...)` (same call as `ObservationStateWidget`) |
| Exposure / gain | `cameraSettingWidget.entry_exposureTime.setValue(...)` |
| Objective | `objectivesWidget.dropdown.setCurrentText(...)` |
| XY / Z jog | `navigationWidget.set_delta{X,Y,Z}` + `move_{x,y,z}_{forward,backward}` |
| Autofocus | `autofocusController.autofocus()` |
| Tap-to-center | `imageDisplayWindow.image_click_coordinates` → `move_from_click_image` |

Consequences worth knowing: divergence between the two UIs is structurally impossible, no existing
widget needed to change, and the console degrades gracefully — any widget that does not exist in a
given machine configuration simply makes its console control a no-op.

Reads go the other way through a 5 Hz `QTimer`, guarded by a `_syncing` flag so a refresh never
fights an in-flight interaction.

### The frame path

The console registers its **own** `QtStreamHandler` via `camera.add_frame_callback()`, alongside the
main window's. That gives it an independent frame rate and independent decimation, so the panel
stays responsive without forcing the main display to degrade.

Two things to know about decimation:

- `display_resolution_scaling` (the main GUI's "Display Resolution" slider) is a **center crop**, not
  a resize — it reduces field of view, not pixel count.
- `display_max_dim` is a **true resize**, via `downsampled_views.downsample_to_max_dim()`. This is
  what the console uses. Cost is ~1.6 ms for a 2400×2400 MONO16 frame, so ~2.5% of one core at
  15 fps.

Camera callbacks arrive on driver threads, so every connection into the console's display uses
`Qt.QueuedConnection` — Qt's `AutoConnection` degrades to `DirectConnection` for non-`QThread`
senders. This is the same discipline documented on `QtMultiPointController`.

When the main window is minimized, its display is paused (`changeEvent` → `streamHandler.disable_display()`)
so the process is not pushing 11 MB frames into two pyqtgraph `ImageItem`s for a window nobody is
looking at.

### Launching acquisitions

The console reuses `MicroscopeControlServer._cmd_run_acquisition_from_yaml` rather than duplicating
its ~100 lines of YAML parsing, hardware validation, and region configuration. It constructs a
`MicroscopeControlServer` purely as a command executor and never calls `start()`, so no socket is
opened and no port is bound.

**That call runs on a worker thread, and must.** It was written for the control server's socket
thread and marshals onto the GUI thread internally — via `QTimer.singleShot` plus a blocking wait,
and a `BlockingQueuedConnection`. Calling it *from* the GUI thread would deadlock on the latter and
silently time out on the former. The outcome comes back through a Qt signal.

## Caveats

- **A wedged camera SDK call is terminal for the process** (`control/_sdk_watchdog.py`), and this rig
  has already lost a timelapse to an unguarded Toupcam `set_analog_gain`. The console does not add
  any raw camera setters; it goes through the same widgets and therefore the same guards. Keep it
  that way when extending it.
- The console depends on the main GUI's widget attribute and slot names. That coupling is deliberate
  — it is what makes divergence impossible — but it means renaming e.g. `entry_exposureTime` needs a
  matching change here. Each access is guarded, so a rename degrades to a dead button rather than a
  crash.
- Observation-state *presets* and observation *states* are different things. The channel buttons
  load presets (`config_repo.list_observation_presets()`), which is the path the main GUI's
  `ObservationStateWidget` uses.

## See also

- [Running the software](running-the-software.md) — launchers and startup
- [MCP integration](mcp_integration.md) — the control server the acquisition launcher reuses
- [Configuration system](configuration-system.md) — profiles and observation states
