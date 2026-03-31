# ObservationState v3 Migration

## Summary

`AcquisitionChannel` has been deprecated and replaced by `ObservationState` as the
single acquisition unit. This document explains the new model and why the change
was made.

## Why

`AcquisitionChannel` conflated per-illumination-source data (which illuminator,
intensity, on/off) with per-camera data (exposure, gain) and per-observation-state
data (filter position, z-offset, confocal iris). This meant every illumination
source carried its own copy of camera settings, even though there is only one
exposure/gain per camera per acquisition step.

`ObservationState` correctly separates:
- **Per-camera settings** (`CameraSettings`) -- one set of exposure/gain/pixel_format
- **Per-illuminator state** (`IlluminatorState`) -- which source, intensity, on/off
- **Optical path** -- emission filters, z-offset, confocal iris

## New Model (v3)

```
ObservationState
  name: str                              # Preset name (used for filenames)
  version: 3
  confocal_mode: bool

  camera_settings: CameraSettings        # exposure_time_ms, gain_mode, pixel_format
  camera_live: CameraLiveSnapshot        # ROI, binning, trigger mode

  illuminator_states: List[IlluminatorState]
  emission_filter_positions: Dict        # filter wheel positions
  z_offset_um: float
  confocal_hardware_settings: ConfocalSettings  # iris apertures

  display_color: str                     # hex color for UI
  channel_groups: List[ChannelGroup]     # multi-camera groups
  enable_channel_auto_filter_switching: bool
```

```
IlluminatorState
  illumination_channel: str     # name referencing IlluminationChannelConfig
  intensity: float              # 0-100%
  on: bool                      # logical on/off
  led_matrix_mode: str          # LED pattern key (optional)
```

## Key Naming Distinctions

| Name | What it is | Defined in |
|------|-----------|------------|
| `ObservationState` | Complete light-path config for one acquisition step | `models/observation_state.py` |
| `IlluminatorState` | Runtime state of one illumination source | `models/observation_state.py` |
| `IlluminationChannel` | Hardware definition of a physical light source (static) | `models/illumination_config.py` |
| `IlluminationController` | Dispatches intensity/on/off commands to correct hardware device | `lighting.py` |

`IlluminatorState.illumination_channel` is a name string that references
`IlluminationChannel.name`. The `IlluminationController` uses this name to route
commands to the correct hardware (Teensy LED matrix, NIDAQ, serial laser, etc.).

## YAML Format (v3 observation presets)

```yaml
version: 3
name: 488_only
confocal_mode: false
display_color: '#1FFF00'
illuminator_states:
- illumination_channel: Fluorescence 488 nm Ex
  intensity: 1.0
  'on': true
- illumination_channel: BF LED matrix full
  intensity: 0.0
  'on': false
  led_matrix_mode: bf_full
camera_states:
  camera:
    camera_settings:
      exposure_time_ms: 2.0
      gain_mode: 0.984
      pixel_format: MONO16
    z_offset_um: 0.0
    emission_filter_positions:
      '1': 1
    camera_live:
      exposure_time_ms: 2.0
      analog_gain: 0.984
      ...
enable_channel_auto_filter_switching: true
```

## Code Paths

### Collect (save preset from live state)
`collect_observation_state()` reads hardware state and builds v3 `ObservationState`.

### Apply (load preset to hardware)
`apply_observation_state()` sets camera exposure/gain from `camera_settings`,
syncs illumination from `illuminator_states`, sets filter wheel from
`emission_filter_positions`.

### LiveController
`current_observation_state: ObservationState` tracks the active observation state.
`set_observation_state(state)` applies it to hardware.

### Multipoint Acquisition
Only uses `observation_state_names: List[str]`. Each name is loaded via
`config_repo.load_observation_preset(name)`, applied to hardware, then acquired.

## What Still Uses AcquisitionChannel

`GeneralChannelConfig` and `ObjectiveChannelConfig` (the general.yaml /
objective.yaml YAML schemas) still use `AcquisitionChannel` internally. These
are read-only configuration containers that will be migrated in a future update.
The `default_config_generator.py` and `migrate_acquisition_configs.py` tools
also use `AcquisitionChannel` for config file generation/migration.
