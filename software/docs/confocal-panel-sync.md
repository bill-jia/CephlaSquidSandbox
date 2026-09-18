# Confocal panel ↔ observation state ↔ X-Light

How the spinning-disk confocal panel (`gui/widgets/hardware_panels.py`,
`SpinningDiskConfocalWidget`) stays in step with the hardware and with the
observation state that is loaded.

## The failure this replaced

The panel seeded most of its controls from the unit at construction but not the
two iris sliders — they started at the widget minimum, 0, on a rig whose irises
were physically wide open. Two things followed:

1. The panel lied: restart the software and both irises read 0 at 100%.
2. The displayed 0 was what the next interaction pushed to hardware — and
   because the driver skips a write whose value matches its cache, and the cache
   also started at 0, applying "iris 0" to a unit at 100 was silently dropped.
   Software and hardware then disagreed permanently.

Nothing told the panel when an observation state was applied from anywhere but
the live channel dropdown either, so a preset loaded from the menu or the preset
widget moved the optics while the panel kept showing the previous state.

## Three places, three responsibilities

**The driver owns the truth and the parsimony.** `XLight.__init__` calls
`seed_caches_from_hardware()` to read every mechanism the unit reports having,
and each setter returns early when the request matches its cache (see
"Redundant writes are skipped in the driver" in `configuration-system.md`).
Skipping is only safe because the cache is seeded, and the cache is invalidated
to `None` before a write and set after it, so a failed write retries.

**The observation state owns the light path.** `ConfocalSettings`
(`control/models/observation_state.py`) carries `illumination_iris`,
`emission_iris`, `dichroic_position` and `filter_slider_position`; the emission
wheel lives on `ObservationState.emission_filter_positions["default"]` and the
disk on `ObservationState.confocal_mode`. **Every `ConfocalSettings` field
defaults to `None`, and `None` means "leave the hardware alone".** Presets saved
before a field existed simply do not carry it, and loading one must not invent a
position — that would move a dichroic, or a 5 s slider, on every old preset.
`ObservationStateController.apply_optical_path` skips every `None`.

The disk *motor* is deliberately not part of an observation state: it either
spins for the session or it does not.

**The panel only displays, or asks.** It never writes a mechanism that belongs to
the state; it emits a signal and
`ObservationStateController.set_emission_filter_position` /
`set_dichroic_position` / `set_filter_slider_position` record the choice on the
live state and then drive the hardware. That is what makes a user's manual pick
survive into a saved preset.

## `sync_from_observation_state(state=None)`

The panel's single refresh entry point. Called at construction (with `None`), on
`liveControlWidget.signal_live_configuration`, on
`multipointController.signal_current_configuration`, and from
`HighContentScreeningGui._on_observation_state_changed()` — the seam both the
preset widget and the menu already funnel through.

Per control the displayed value is **the value carried by `state` if it has one,
otherwise the driver's last-known hardware value**. Never a widget default: a
control falling back to its minimum is the original bug.

Loop avoidance, which is load-bearing — this runs on every channel switch:

- every change signal is blocked for the duration (`_block_change_signals`), so
  a refresh cannot drive a 2 s iris write, a 5 s slider move, or write back onto
  the state being displayed;
- `set_confocal_mode_display` updates `disk_position_state` and the button label
  without re-emitting `signal_toggle_confocal_widefield`;
- the motor button is set with `setChecked`, which emits `toggled`, never
  `clicked` — and `clicked` is what drives the motor;
- values come from the driver's caches, not from serial. The blocking read-back
  is attempted at most once per mechanism per panel, so a mechanism the unit
  will not report cannot put a serial round-trip on the UI thread on every
  channel switch of a running acquisition.

## The filter slider and the UI thread

A slider move is 5 s, so the panel runs it on a worker thread
(`utils.threaded_operation_helper`) and emits `signal_filter_slider_changed`
from *that* thread. `make_connections` therefore connects it with
`Qt.DirectConnection`, so the controller runs on the worker thread instead of
being queued back onto the UI thread — which would reintroduce the stall the
thread exists to avoid.

## Per-parameter summary

| Parameter | Seeded from hardware | Follows a loaded state | Driver cache gates writes |
|---|---|---|---|
| Illumination iris | driver `rJ` + panel | `confocal_hardware_settings.illumination_iris` | yes (2 s) |
| Emission iris | driver `rV` + panel | `confocal_hardware_settings.emission_iris` | yes (2 s) |
| Emission filter wheel | driver `rB` + panel | `emission_filter_positions["default"]` | yes (`sleep_time_for_wheel`) |
| Dichroic wheel | driver `rC` + panel | `confocal_hardware_settings.dichroic_position` | yes (`sleep_time_for_wheel`) |
| Dichroic filter slider | driver `rP` + panel | `confocal_hardware_settings.filter_slider_position` | yes (5 s) |
| Disk position (confocal/widefield) | driver `rD` + `Microscope._sync_confocal_mode_from_hardware` | `confocal_mode` | guarded in `apply_confocal_mode`, not in the driver |
| Disk motor | driver `rN` + panel button | not in an observation state, by design | no (a stop must always be sent) |

## Tests

`tests/control/test_confocal_widget_sync.py` (driver seeding over a faked serial
port, plus the panel against `XLight_Simulation`) and the dichroic/slider and
parsimony cases in `tests/control/core/test_observation_state_confocal.py`.
