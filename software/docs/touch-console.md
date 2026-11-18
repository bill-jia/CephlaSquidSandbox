# Full Touch Interface and Bench Console

Select **Desktop / Touch** in the initial profile/machine dialog, or launch with
`--interface touch`. The accepted choice is remembered locally; cancelling leaves
it unchanged. Explicit `--interface desktop|touch` takes precedence over the
remembered choice (including with `--profile`), with Desktop as the first-run
default. A CLI override applies only to that launch.

The full Touch interface supports experiment names, paths, preset names and
acquisition building with normal keyboard entry. It has these task pages:

| Task | Controls |
| --- | --- |
| Live / Position | Live illumination, one LED matrix with mode selector, camera exposure/gain/binning, expandable ROI/trigger settings, XY/Z, objectives, Snap and observation presets |
| Focus Setup | Existing contrast AF settings, laser initialization/reference/target/measurement, focus maps, and a focus-camera/calibration dialog |
| Plate / Navigate | Sample format, enlarged zoomable well selection, map zoom and explicit movement controls |
| Wellplate Acquisition | Shared well-selection summary, channels, tiling, Z/time, focus, scan order, saving and run controls |
| Flexible Acquisition | Independent position table, position actions and the complete acquisition controls |
| Other configured modes | Existing enabled modes, including Simple Recording, Fast Acquisition, tracking, templates and fluidics |

Live, Snap, **Laser AF Now**, position/objective status and progress/Abort remain
available above the pages. The Snap saving folder, Browse button and tag editor
live in this top ribbon. Autolevel sits above the live histogram. The task-tab
ribbon is above the right-hand controls, allowing the image to use the full
height below the action ribbon. LED matrix dropdown entries have tall touch rows,
and illumination sliders have wider tracks and 48-pixel handles.
Live / Position has a single camera-settings row and a 64-pixel XY joypad with
separate Z up/down arrows, using the existing stage-step controls. Start Live
appears once in the top ribbon. The histogram is 25% narrower, with a matching
Autolevel button, and live-image padding is reduced while preserving aspect ratio.
Save-folder/name editors share their available width 30/70, including the Snap
folder/tag pair. Flexible and Wellplate Acquisition keep saving/name fields above
the scrolling settings and Acquire Current FOV/Start buttons below them.
Acquisition lockouts apply to the underlying controls
and their proxies. Visiting Live, Focus or Plate preserves the active acquisition
mode and regions. Choosing an acquisition page selects that acquisition workflow.

There is one well-selection model. **Edit selection** opens Plate / Navigate;
flexible positions remain independent. A well tap selects; **Go To Selected Well**
moves the stage. Map double-tap movement requires its explicit toggle. Flexible
position taps select by default; **Go To Selected Position**, Previous and Next
request movement, and the existing move-on-click checkbox can opt in to it.

Primary touch targets are 48–52 logical pixels, with tall rows, wide scrollbars,
touch scrolling, numeric steppers and explicit channel/loop reorder buttons.
Overflow scrolls. The image/control splitter remembers a separate balance for
each task; window/tab state is stored separately for Desktop and Touch. Image
tools remain accessible through a horizontal toolbar scroller. Restored window
geometry is clamped to the available screen.

**Laser AF Now** delegates to the existing move-to-target operation using its
configured target, pauses/resumes live imaging, and requires successful
initialization in this session plus a reference. Cached calibration alone does
not enable it. Measurement, calibration and reference controls remain separately
labeled in Focus Setup. Contrast AF retains its existing acquisition behavior;
live contrast AF and contrast-based reference establishment remain deferred.

The optional bench window below is independently enabled by `--touch-console
[SCREEN]`. Selecting Touch opens the full interface alone. Both windows share
one microscope, controllers and observation-state refresh paths.

## Reproducible review gallery

The compact camera row, XY/Z joypad, narrower histogram and fixed acquisition
bars are shown in the
[current gallery](../../artifacts/touch-interface-2026-10-07-joypad-reviewed/index.html).
The [earlier ribbon gallery](../../artifacts/touch-interface-2026-10-07-ribbon-reviewed/index.html)
is retained for comparison.
The [initial full-interface gallery](../../artifacts/touch-interface-2026-10-07-completed/index.html)
is retained with its manifest, capture log and validation results. A separate
[gallery without the bench window](../../artifacts/touch-interface-2026-10-07-completed-no-bench/index.html)
verifies both layouts without the companion.

From `software/`, in the Squid Python environment:

```powershell
$env:QT_QPA_PLATFORM = "windows"
python tools/screenshot_touch_interface.py ../artifacts/touch-interface-2026-10-07-review --bench
```

Use a new destination for each run. The utility generates `index.html`, clickable
full-resolution PNGs, `capture_manifest.json`, `capture.log` and `validation.txt`.
It captures the launcher, Desktop and Touch pages, empty/populated acquisition
states, run settings, real short simulated Flexible/Wellplate acquisitions and
Simple Recording, plus optional bench views. Images retain their actual widget
size; viewport/screen overflow causes capture failure and is flagged in the gallery.
The manifest records branch/commit/dirty state, source hashes, fixture details,
Qt backend, screen/scaling, requested/actual sizes and acquisition image counts.

Each layout runs in a separate disposable configuration root, working directory,
profile and output folder. Presentation, observation timers, multipoint caches,
stage caches, application caches and logs are isolated. Child processes and
simulated hardware are cleaned up on failure and success. The October 7 baseline
is preserved and linked from the generated gallery.

The ready laser AF screenshot is an injected visual fixture. A reduced viewport
with a focused field approximates keyboard occlusion; native Windows keyboard
invocation/occlusion, physical gestures, monitor placement and real laser AF still
need manual hardware review. Focused editors scroll into view when the viewport
resizes; the application does not automatically invoke the Windows keyboard.
Hardware-specific Fast Acquisition, tracking, template and fluidics coverage is
separate from the default simulation fixture.

## Optional bench console

A finger-driven second window for scopes with no eyepiece and no room for a keyboard and mouse.

The console lives on a small touchscreen next to the microscope and covers what has to happen with
a hand on the sample: look at it, find it, focus it, snap a frame, and start a saved acquisition.
Acquisition building, calibration and analysis use the full Desktop or Touch GUI,
which can be used locally with the available keyboard or through a remote session.

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
- **Illumination** — one LED matrix with a mode selector, independent illuminator on/off, and ±1% fine steppers, and a master off
- **Focus** — Z jog at 1 / 5 / 25 µm with press-and-hold repeat, and Laser AF Now
- **Stage** — XY D-pad at 0.05 / 0.2 / 1.0 mm, plus **tap the image to center that point**
- **Objective** — one button per configured objective
- **Acquire** — one button per preset, with confirm, live progress, and abort

Zoom is by button (`+` / `−` / `Fit`) and by pinch. Single-finger drag pans, because Qt synthesizes
mouse events for unhandled touches and pyqtgraph's ViewBox handles those.

## What it deliberately cannot do

- **Preset-oriented controls.** The optional console uses generated filenames. The snap directory is fixed by config and
  filenames are generated; acquisition presets are picked from a list, never typed.
- **Acquisition building uses the full interface.** Regions, z-stacks, timelapses, and channel sets are configured on the
  full Desktop or Touch GUI.
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
| Laser AF Now | `laserAutofocusControlWidget.btn_move_to_target.click()` with readiness and acquisition lockout |
| Tap-to-center | `imageDisplayWindow.image_click_coordinates` → `move_from_click_image` |

Consequences worth knowing: divergence between the two UIs is structurally impossible, presentation controls share the same authoritative state, and the console degrades gracefully — any widget that does not exist in a
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

## Capturing both interfaces for review

From `software/`, using the Squid Python environment:

```powershell
$env:QT_QPA_PLATFORM = "windows"
python tools/screenshot_multipoint.py ../artifacts/touch-console-2026-10-07 --touch-console --populate 3 --enable-zt
```

This constructs one simulated microscope and both real GUI windows, starts a simulated
live stream, and captures the console at its top, middle, and bottom scroll positions.
It also captures the complete companion window with the Flexible and Wellplate tabs
selected, plus the existing panel detail images. No physical microscope is required.
The default profile may contain no observation or acquisition presets; the captures
show those empty states rather than inventing presets.

Use `--profile NAME` to select another existing profile, `--touch-width` and
`--touch-height` for the bench dimensions (default 1280 by 800), and
`--companion-width` and `--companion-height` for the requested companion dimensions
(default 1920 by 1080). Qt minimum layout sizes can enlarge a window: the generated
`capture_manifest.json` records actual image dimensions, logical window dimensions,
and pixel ratios. These are whole-window Qt captures, including content extending
beyond the desktop, rather than screenshots of a physical or virtual monitor.
On Windows use the native backend above: the offscreen backend can render without
fonts. The utility suppresses periodic observation-state persistence and multipoint
configuration saves, and skips GUI close handlers that save simulated stage positions.

The console omits the desktop image toolbar, whose minimum widths otherwise force
the window wider than a 1280-pixel touchscreen. Its own status and zoom controls remain.
Loading a console preset refreshes the companion camera, illumination, and confocal
controls through the same callback used by the main menu. Updates to the companion's
preset lists also refresh the console's channel buttons.

## October 7 baseline

The original captures showed the desktop companion expanding to 3302 by 1296.
The full Touch implementation above replaces those layout proposals. Both new
layouts contain toolbar and control minimum sizes through scrollable panels.
The baseline gallery remains available for comparison; simulated captures do not
validate physical touch gestures, monitor placement, native keyboard behavior
or real camera/laser performance.
