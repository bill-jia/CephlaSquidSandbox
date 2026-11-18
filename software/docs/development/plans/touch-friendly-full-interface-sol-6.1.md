# Touch-friendly full interface implementation plan

Status: implemented on `worktree-touch-console` as an uncommitted working tree based on `5e15842fbf7870cd62985b67b8e530ad899c14f5`. Simulation review is complete; physical touchscreen and hardware verification remain.

## Objective and agreed requirements

Reorganize the full Squid interface for a 15-inch, 1920×1080 touchscreen with an available touch keyboard. Offer **Desktop / Touch** in the initial launcher/profile-selection dialog and remember the choice. Touch is a layout of the full application, not a restricted replacement for acquisition setup.

The user approved the task-based tabs, persistent actions/status, adjustable image/control split, shared plate selection, larger touch controls, and keyboard-aware text entry described below.

Terminology is settled:

- Rename the existing laser AF **Move to Target** button to **Laser AF Now** in desktop and touch interfaces, including the existing bench console where the quick focus action is exposed.
- This remains the existing move-to-target operation using the configured target. It is not Measure Displacement, initialization, calibration, or setting a reference.
- The quick autofocus action in the new layout is **Laser AF Now**. Do not label it “Autofocus now” or wire it to contrast AF.
- New live contrast AF behavior and using contrast AF to establish a laser AF reference are explicitly deferred. Preserve existing contrast AF configuration and acquisition behavior; do not add either deferred workflow.
- LED matrix modes belong inside one LED matrix illuminator control. They must not appear as independent illumination rows/channels in this UI.

## Starting point and code map

Work on `worktree-touch-console`, which includes the NDAcquisition merge at `7c0d5e06`. Inspect current status and any later changes before editing; preserve unrelated work.

| Area | Starting files / integration points |
|---|---|
| Initial launcher | `software/gui/profile_selection.py`: `ProfileSelectionDialog`, `prompt_for_profile` |
| Launch arguments and GUI construction | `software/main_hcs.py` |
| Main GUI layout, connections, persisted state | `software/gui/gui_hcs/main_window.py`: `setup_layout`, `recordTabWidget`, `onTabChanged`, GUI-state restore/save |
| Existing bench window | `software/gui/gui_hcs/touch_console.py` |
| Laser AF action and readiness | `software/gui/widgets/tracking_and_controls.py`: `LaserAutofocusControlWidget`, `btn_move_to_target`, `_apply_button_state`, `move_to_target` |
| Focus settings | `software/gui/widgets/laser_autofocus_settings.py`, existing contrast AF and focus-map widgets |
| Live/camera/stage controls | `software/gui/widgets/hardware_panels.py`, `software/gui/widgets/common.py`; locate current illumination widget implementation |
| Observation-state refresh | `main_window.py`: `_on_menu_observation_state_changed`, `_on_observation_state_changed`, `_refresh_channel_lists` |
| Acquisition UI | `software/gui/widgets/multipoint.py`, enabled acquisition-mode widgets |
| Settings models/storage | `software/control/models/gui_state.py`, `software/control/core/config/repository.py`; inspect existing preference conventions |
| Capture utility | `software/tools/screenshot_multipoint.py` |
| Existing tests | `software/tests/gui/test_touch_console.py`, `software/tests/control/test_confocal_widget_sync.py`, other relevant widget/launcher tests |

Existing baseline artifacts are local and may be untracked:
`artifacts/touch-console-2026-10-07/index.html`, its PNGs, and `capture_manifest.json`.
The prior captures show the companion expanding to 3302×1296 despite requesting 1920×1080. Address the layout minimum sizes; resizing or downscaling screenshots does not solve this.

## Intended layout

Keep the live image beside a tabbed control area. Use a splitter with a sensible initial balance; remember the split independently for each task tab. Keep image tools available without allowing desktop toolbar minimum widths to force the window offscreen.

| Tab | Content |
|---|---|
| **Live / Position** | Live illumination, one LED matrix row with mode selector and mode-specific settings, camera exposure/gain/binning, XY/Z movement, objective selection, Snap, observation-state save/load, and Laser AF Now with readiness and current target information |
| **Focus Setup** | Existing contrast AF settings, laser AF initialization/calibration/reference/target configuration and displacement measurement, focus-map configuration, associated focus-camera view where needed |
| **Plate / Navigate** | Sample format, enlarged zoomable well selector and navigation map, explicit selection versus stage-movement actions, selected-well/position feedback |
| **Wellplate Acquisition** | Existing wellplate acquisition workflow: shared selection summary, channels, tiling, Z/time, focus options, order, saving and run controls |
| **Flexible Acquisition** | Existing flexible position workflow, a usable position table and position actions, channels, tiling, Z/time, focus options, saving and run controls |
| **Other enabled modes** | One tab per supported configured mode, retaining existing functionality and hardware-dependent visibility |

Provide a compact persistent strip for Live, Snap, **Laser AF Now**, objective/position status, and acquisition progress/Abort. The Live / Position tab owns their detailed settings; if an action is repeated in a tab, both affordances use the same underlying action and enabled state. All remain accessible when switching tabs, subject to existing acquisition/motion lockouts.

Plate selection has one source of truth. Acquisition pages show a concise summary and **Edit selection** navigation to Plate / Navigate; they must not maintain independent well selections. Preserve the distinction between a wellplate region selection and flexible positions.

Use approximately 48–56 logical-pixel primary touch targets, readable fonts, tall selectable rows, and wide scroll handles. Use touch scrolling for overflow rather than compressing controls. Keep advanced camera ROI/triggering, calibration, and detailed scan-order controls accessible in expandable sections or dialogs. Avoid a global stylesheet that indiscriminately increases every widget's minimum size.

The keyboard removes the prior “no text entry” restriction. Support experiment names, paths, and preset names through normal editors; provide useful numeric steppers/keypad entry with units, limits and validation. Ensure focused fields can be scrolled into view when the keyboard reduces usable space. Do not promise automatic Windows keyboard invocation without checking native behavior.

## Implementation sequence

### 1. Launcher selection and settings contract

1. Add a clearly labeled Desktop / Touch selector to the existing initial profile/machine dialog. It must be visible before heavy GUI/hardware imports and usable by touch.
2. Persist the last accepted interface choice using existing preference conventions. Prefer a machine/user-local presentation preference, separate from microscope hardware configuration. Default to Desktop when unset. Cancel must not accidentally change the remembered choice.
3. Thread an explicit layout mode into main GUI construction. Add a CLI override such as `--interface desktop|touch`; document precedence as explicit CLI override, then remembered choice, then Desktop. Ensure `--profile` startup that bypasses the picker still honors that precedence.
4. Preserve cancellation, profile creation/selection, machine selection, simulation and restart flows. Avoid breaking existing `prompt_for_profile` callers; introduce a structured launch-selection result or a compatible wrapper as appropriate.
5. Keep `--touch-console` conceptually separate: it controls the optional existing second window. Selecting Touch changes the full interface and must not automatically create duplicate windows or a second hardware owner. A runtime layout toggle is not required for this iteration.

### 2. Reorganize the full GUI without duplicating state

1. Inventory widgets and their ownership, signal connections, visibility rules, tab-change assumptions and geometry persistence before reparenting them.
2. Extract desktop/touch layout assembly into clear helpers or a small layout module. Construct controllers and state-bearing widgets once per process, then arrange them for the selected mode. Shared action proxies are acceptable; duplicate hardware controllers are not.
3. Build the touch task tabs and image/control splitter. Use stable tab identities for persistence and dispatch, not numeric indexes whose meanings change between layouts.
4. Audit `recordTabWidget`, `onTabChanged`, current-widget lookups, region preview updates and acquisition-mode routing. Visiting Live, Focus or Plate must not silently switch acquisition mode, clear regions, or drive hardware.
5. Preserve desktop layout behavior. Store geometry/splitter/tab state separately per layout, and clamp restored sizes to available screen geometry. Accommodate the taskbar/window frame at 1920×1080; the full content must fit the actual usable area.
6. Reuse observation-state refresh paths so loading a preset updates exposure, gain, illumination, objective-dependent state, filter/confocal controls and both windows consistently.

### 3. Laser AF Now action

1. Change the displayed text on `btn_move_to_target` to **Laser AF Now** while retaining internal method/attribute names unless a refactor is necessary.
2. Delegate quick actions to the existing widget action, respecting its enabled state. The existing `move_to_target()` pauses live imaging, uses `entry_target.value()`, invokes the laser controller and resumes live imaging; preserve that behavior.
3. Mirror the authoritative readiness state: this session must be initialized and a reference must exist. A cached configuration alone must not enable the action. Honor hardware absence, busy state and acquisition lockout. Show a short reason when unavailable and a route to Focus Setup.
4. Update the existing bench quick-focus control to Laser AF Now and the same delegation/readiness behavior; do not leave a similarly placed generic Autofocus button calling contrast AF.
5. Keep **Measure Displacement** and reference/configuration actions separately labeled in Focus Setup. No automatic reference creation and no new contrast AF execution path.

### 4. Illumination and plate navigation

1. Reuse the current illumination controller's canonical illuminator/mode model. Do not derive touch rows by blindly listing every legacy observation-state illuminator entry.
2. Render one LED matrix row with its mode dropdown. Mode-specific parameters appear within that control. Preserve genuinely independent fluorescence/other light sources. Confirm loaded legacy states cannot create duplicate BF/DF/DPC matrix rows.
3. Ensure intensity, on/off and mode changes refresh correctly from presets and companion interactions without feedback loops or extra hardware writes. Apply the same row semantics to the existing bench interface if it remains exposed.
4. Enlarge the plate selector/map and provide touch-friendly zoom/selection. Make select-versus-move behavior explicit so selecting wells does not unexpectedly move the stage.
5. Preserve selected wells, generated regions and flexible positions across navigation and acquisition tabs. Add Current Position and Go To Selected must be distinguishable and appropriately gated.

### 5. Acquisition and touch details

1. Preserve all existing enabled acquisition capabilities, including channel editing, per-position options, tiling, Z/time, focus choices, scan order and save/upload options. Move advanced settings without silently dropping them.
2. Keep the persistent strip visible during acquisitions; disable conflicting live/motion actions consistently across every tab and optional window. Show progress and Abort regardless of selected tab.
3. Give position tables and channel editors comfortable row heights; provide explicit reorder buttons where a drag-only operation would be difficult by touch.
4. Check normal, expanded, empty and populated states. Support scrolling when the keyboard is open. Avoid automatic tab changes that interrupt text entry.

### 6. Regression verification

Add focused tests for the new behavioral contracts, not assertions that merely duplicate layout implementation:

- Launcher persistence, cancellation, first-run defaults, explicit CLI precedence and `--profile` bypass.
- Correct startup in Desktop and Touch, with and without the optional bench console.
- Laser AF Now delegates to move-to-target with the configured target; never dispatches contrast AF; disabled for missing reference, unverified cached state, absent hardware and acquisition lockout.
- Preset loads synchronize camera/illumination/confocal displays, and changes do not duplicate signal-driven hardware operations.
- One LED matrix row across mode changes and legacy state loading, with independent fluorescence sources retained.
- Plate selection and acquisition mode survive task-tab changes; acquisition actions still reach the intended mode.
- Touch layout fits the available 1920×1080 desktop without enlarging itself; restored desktop geometry does not break touch layout.
- Progress/Abort and lockout remain correct from each task page.

Run the relevant existing touch-console, confocal synchronization, live-control, observation-state and acquisition-widget tests. Exercise one short simulated acquisition through each implemented acquisition mode where supported. Report unavailable hardware-specific coverage separately. Do not change tests just to accept regressions introduced by rearrangement.

## Required review artifact

Extend the existing screenshot utility or extract reusable capture helpers. Deliver a reproducible command that launches the real GUI with simulated hardware, captures both layout modes and generates an HTML review gallery automatically. The prior gallery was manually assembled; gallery generation is part of this implementation.

Save the new review bundle under `artifacts/touch-interface-YYYY-MM-DD/` (use a unique suffix if needed). Preserve the October 7 baseline. Include:

- `index.html`: labeled clickable full-resolution images, layout/tab/state captions, requested and actual sizes, links to baseline captures when available, and a concise list of changes and remaining limitations.
- Launcher screenshot showing the Desktop / Touch choice.
- Touch screenshots for Live / Position, Focus Setup, Plate / Navigate, each enabled acquisition-mode tab, and progress/Abort during a short simulated acquisition.
- Desktop screenshots proving the existing layout remains usable and the button reads Laser AF Now.
- Optional bench-console screenshots if it is still supported, demonstrating the corrected laser AF action and single LED matrix control.
- Empty and populated examples; representative channels, selected wells, flexible positions and Z/time settings. Use disposable fixture profiles/configuration so screenshots do not depend on personal presets.
- Laser AF unavailable and ready states where simulation supports them. If readiness must be injected for visual review, label that screenshot as a fixture state and do not claim hardware validation.
- Keyboard-visible/focused-text-field review where the native keyboard can be exercised. Otherwise state clearly that keyboard occlusion still needs a manual check; a simulated reduced viewport is useful but not equivalent.
- `capture_manifest.json`: branch/commit and dirty status, simulation/profile/fixture details, timestamp, Qt backend, screen geometry/scaling, requested viewport, actual logical and image sizes, pixel ratio and capture state for each image.
- Capture log and a short validation summary with the exact reproduction command.

On Windows use the native Qt `windows` backend; the earlier offscreen run rendered without fonts. Capture the actual widget at its real size. Fail or clearly flag unexpected size expansion instead of resizing PNGs to hide it. Include available-screen bounds so full-window capture outside the monitor cannot be mistaken for a successful fit.

Isolate simulated capture writes: observation-state timers, multipoint settings, GUI geometry, selected profile/machine, stage caches and acquisition outputs must not overwrite the operator's real state. Prefer a disposable configuration root/profile and temporary output directory over restoring shared files after the fact. Ensure cleanup of windows, simulated hardware and child processes on failure as well as success; avoid unattended modal dialogs.

## Completion and handoff

Implementation is complete when Desktop / Touch launches reliably, the agreed pages are usable at the target screen size, the laser AF action and LED matrix semantics are correct, acquisition and state synchronization remain functional, and the generated gallery is ready for joint review.

Update `software/docs/touch-console.md` and launch documentation to explain the full Touch layout, optional bench window, preference/CLI behavior, Laser AF Now semantics and capture command. Remove or qualify obsolete claims that touch use prohibits text entry or acquisition building.

In the final implementation response, link directly to the gallery and plan, report the tested commit/state and test results, summarize material limitations, and list anything needing physical touchscreen/hardware verification. Do not claim physical gesture, keyboard, monitor-placement or laser AF validation based only on simulated screenshots.

## Implementation review — October 7, 2026

The implementation includes launcher preference/CLI precedence, separate layout state,
full task pages using the existing acquisition widgets, persistent actions/status/Abort,
shared zoomable plate selection, touch scrolling and focus visibility, the authoritative
Laser AF Now action, and one LED matrix row in both full and bench interfaces.
Launch and touch-console documentation have been updated.

Review the [generated gallery](../../../../artifacts/touch-interface-2026-10-07-completed/index.html),
[manifest](../../../../artifacts/touch-interface-2026-10-07-completed/capture_manifest.json)
and [validation details](../../../../artifacts/touch-interface-2026-10-07-completed/validation.txt).
The [run without the bench console](../../../../artifacts/touch-interface-2026-10-07-completed-no-bench/index.html)
also passed. Native Windows Qt captures retain their actual 1904×992 content size
within the requested 1920×1080 target; focus settings and bench views fit their
1000×700 and 1280×800 viewports. Both layouts completed short simulated Flexible
and Wellplate acquisitions with image files, plus Simple Recording start/stop.
Capture writes use disposable configuration roots and output directories.

Focused regression verification: **316 passed**, with three dependency deprecation
warnings. An additional region/snapshot check produced **41 passed, 2 failed**:
`test_go_to_retracts_z_before_the_xy_move` has outdated `MagicMock` retraction-call
expectations, and `test_observation_snapshot_continuous_capture_uses_saved_illumination_state`
constructs a worker without `file_saving_option`. The failing tests, stage helper,
worker implementation and `go_to` operation are unchanged by this work.

Physical touch gestures, native Windows keyboard occlusion/invocation, monitor
placement/scaling and real laser autofocus remain manual checks. The laser-ready
image is an injected appearance fixture. Fast Acquisition, tracking, template and
fluidics workflows retain their configured pages but need their own hardware
coverage; they are outside the capture fixture.

## Touch layout refinements — October 7, 2026

The latest Touch layout removes the Snap panel and duplicate Start Live control
from Live / Position. The original Snap folder/tag controls live in the top
ribbon, and camera exposure/gain/mode/binning/shutter share one row. Navigation
uses the original motion buttons in a 64-pixel XY joypad and a separate Z arrow
pair. The histogram is 25% narrower with a matching Autolevel button above it;
live-image padding is reduced while retaining aspect ratio. Task tabs sit above
the right controls, LED matrix popup items are taller, and power sliders have
larger tracks and handles.

Saving/name editors share their width 30/70 throughout Touch. Flexible and
Wellplate acquisition saving bars and Acquire Current FOV/Start buttons remain
fixed while the remaining settings scroll. These are the original controls,
retaining their controller connections and acquisition lockouts.

The [latest gallery](../../../../artifacts/touch-interface-2026-10-07-joypad-reviewed/index.html)
contains 30 native Windows captures, with no viewport or screen-fit failures.
Both layouts fit 1904×984 content in the available 1920×1032 screen area and
completed simulated Flexible/Wellplate acquisitions producing three/two images,
plus recording start/stop. The capture checks verify both directions of each
stage axis, Z unit conversion, the camera row, 30/70 editors, fixed acquisition
controls during scrolling, histogram/Autolevel widths and Start/Stop availability.
The latest focused regression run passed **99 tests** with three dependency
deprecation warnings. This is uncommitted work on `worktree-touch-console`, based
on `5e15842fbf7870cd62985b67b8e530ad899c14f5`; source hashes and the reproduction
command are in the gallery manifest. The physical checks above remain pending.
