# User Profiles

The Squid software organizes per-user acquisition configuration into named
**profiles** under `software/user_profiles/{profile}/`. Each profile owns its
own observation state presets, channel configs, laser AF calibrations, and
transient GUI state — so different users (or different experimental setups on
the same instrument) can coexist without overwriting each other's settings.

Hardware-level configuration (cameras, illumination devices, IO endpoints) is
**not** per-profile and stays under `software/machine_configs/`.

## Layout

```
software/
├── machine_configs/                       # global, shared across all users
│   ├── machine_config.yaml
│   ├── illumination_channel_config.yaml
│   └── ...
├── user_profiles/
│   └── {profile}/
│       ├── channel_configs/
│       │   ├── general.yaml               # current ObservationState
│       │   └── {objective}.yaml           # per-objective overrides
│       ├── observation_presets/
│       │   └── *.yaml                     # named ObservationState snapshots
│       ├── laser_af_configs/
│       │   └── {objective}.yaml
│       └── gui_state.yaml                 # per-profile UI state (see below)
└── cache/
    └── last_active_profile.txt            # remembers the last loaded profile
```

## Selecting a profile

### Startup selector (`ProfileSelectionDialog`)

When `main_hcs.py` starts without an explicit `--profile` flag, it shows a
modal **Select User Profile** dialog (`software/gui/widgets/profile_selection.py`)
*before* any hardware initialization. The dialog:

- Lists every directory under `user_profiles/`.
- Pre-selects the last-active profile recorded in
  `cache/last_active_profile.txt`, falling back to the first profile
  alphabetically.
- **New empty profile…** creates a profile with no channel configs; defaults
  are generated on first load (via `ensure_default_configs`).
- **Duplicate selected…** copies the highlighted profile (channel configs,
  observation-state presets, and laser AF calibrations) under a new name.
- Double-clicking a profile, or pressing **Load profile**, accepts the choice.
- Cancelling exits the application without initializing hardware.

If no profiles exist yet, the dialog auto-creates `default` so the list is
never empty on first run.

### Selection priority

1. `--profile NAME` CLI flag passed to `main_hcs.py`. Created on demand if it
   doesn't already exist. **Skips the startup dialog entirely** (useful for
   scripts, kiosks, or automated launches).
2. The user's choice in the startup `ProfileSelectionDialog`. The dialog
   pre-selects the profile in `cache/last_active_profile.txt`.
3. If the dialog is cancelled, the application exits.

### Runtime switching

At runtime, the **Configuration Profile** dropdown (`ProfileWidget`) lets the
user switch profiles without restarting. *Save As* duplicates the current
profile under a new name. Switching emits `signal_profile_changed`, which
refreshes channel lists and the observation-state preset combo.

## What persists, where

Two complementary mechanisms keep the application reopening in the user's
last configuration:

1. **Live ObservationState → `channel_configs/general.yaml`.** The HCS main
   window runs a 30 s timer that calls
   `ObservationStateController.cache_current_state_to_disk()` to snapshot the
   live hardware state (camera, illumination, emission filters) into
   `general.yaml`. The same method is invoked once on shutdown. See
   `software/docs/configuration-api.md` for details. Acquisition windows are
   skipped to avoid disk contention.
2. **Transient UI state → `gui_state.yaml`.** Window geometry, tab selection,
   last objective, and other widget-level UI selections are persisted only on
   shutdown; see the table below.

## Per-profile GUI state (`gui_state.yaml`)

The following transient UI state is persisted to
`user_profiles/{profile}/gui_state.yaml` on shutdown and restored on startup:

| Field | Source widget |
|---|---|
| `last_active_objective` | `ObjectiveStore.current_objective` |
| `last_active_observation_state_name` | `ObservationStateController.current_observation_state.name` |
| `window_geometry_b64`, `window_state_b64` | `QMainWindow.saveGeometry / saveState` |
| `record_tab_index` | `recordTabWidget.currentIndex()` |
| `snap_saving_dir`, `snap_tag` | `LiveControlWidget` |
| `live_display_fps`, `autolevel_enabled`, `display_resolution_scaling` | `LiveControlWidget` |

Persistence is wired in `HighContentScreeningGui._cleanup_common`
(`_persist_gui_state`, runs after the live ObservationState flush);
restoration runs after widget construction (`_restore_gui_state`) and again
after `show()` for window geometry (`apply_persisted_window_state`, called
from `main_hcs.py`).

## Programmatic access

```python
from control.core.config.repository import ConfigRepository
from control.models.gui_state import GuiState

repo = ConfigRepository()
repo.load_profile("alice")          # creates if missing default configs

state = repo.get_gui_state()        # Optional[GuiState]
repo.save_gui_state(GuiState(last_active_objective="20x"))
```

Both `get_gui_state` and `save_gui_state` default to the current profile and
update the in-memory profile cache.
