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

Profile selection priority at startup:

1. `--profile NAME` CLI flag passed to `main_hcs.py`. Created on demand if it
   doesn't already exist.
2. The profile recorded in `cache/last_active_profile.txt` (set automatically
   each time a profile is loaded).
3. The first profile alphabetically under `user_profiles/`.
4. A freshly created `default` profile, populated from
   `illumination_channel_config.yaml`.

At runtime, the **Configuration Profile** dropdown (`ProfileWidget`) lets the
user switch profiles. *Save As* duplicates the current profile under a new
name. Switching emits `signal_profile_changed`, which refreshes channel lists
and the observation-state preset combo.

## Per-profile GUI state (`gui_state.yaml`)

To make sure the application reopens in the same configuration the user left
it, the following transient UI state is persisted to
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
(`_persist_gui_state`); restoration runs after widget construction
(`_restore_gui_state`) and again after `show()` for window geometry
(`apply_persisted_window_state`, called from `main_hcs.py`).

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
