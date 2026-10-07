# Configuration System

This document describes Squid's YAML-based configuration system for managing microscope settings. The system separates hardware-level definitions (machine configs) from user preferences (user profiles), enabling type-safe configuration with Pydantic validation.

## Architecture Overview

The configuration system uses a hierarchical structure that separates concerns:

```
software/
├── machine_configs/                    # Hardware-specific (per machine)
│   ├── library/                          # Selectable machine configs (machine_config*.yaml); chosen via the startup dialog
│   ├── machine_config.yaml             # Optional pinned root device inventory (fallback; see README); may embed filter_wheel_registry / hardware_bindings
│   ├── illumination_channel_config.yaml   # Illumination channels (required)
│   ├── cameras.yaml                      # Optional: camera registry
│   ├── filter_wheels.yaml                # Optional: standalone filter wheels (ignored if embedded registry is non-empty)
│   ├── hardware_bindings.yaml            # Optional: camera→wheel mappings (ignored if embedded in machine_config.yaml)
│   └── intensity_calibrations/           # Optional: power calibration CSVs
│
└── user_profiles/                      # User preferences (per profile)
    └── {profile_name}/
        ├── channel_configs/
        │   └── general.yaml              # ObservationState definitions
        ├── observation_presets/
        │   └── {preset_name}.yaml        # Saved ObservationState presets
        └── laser_af_configs/
            └── {objective}.yaml          # Laser AF per objective (machine calibration)
```

### Design Principles

1. **Separation of Concerns**
   - **Machine configs**: Define what hardware exists (rarely changes)
   - **User profiles**: Store user preferences (changes frequently)
   - **Observation presets**: Named snapshots of complete observation configurations

2. **ObservationState as sole observation config**
   - `general.yaml` defines the available observation states with all settings
   - No per-objective override layer; users load different presets when switching objectives
   - Camera settings, illumination, and optical path all live directly on ObservationState

3. **Type Safety**
   - All configs validated with Pydantic models
   - Invalid configurations fail fast with clear error messages

4. **Schema Versioning**
   - Every YAML file includes a `version` field (currently `1.0`)
   - Enables future schema migrations without breaking existing configs

---

## Machine Configs

Machine configs define the physical hardware setup. These files live in `machine_configs/` and are typically configured once per microscope.

### machine_config.yaml (active hardware config)

The root device inventory for the microscope. Rather than editing a single file in place, configs are kept in a **library** and selected at startup:

- **`machine_configs/library/`** holds the selectable configs (`machine_config*.yaml`), one per hardware setup.
- The startup user-profile dialog shows a **Machine config** dropdown (bottom-left) listing the library. The choice is **global** — shared by every user profile, not stored per-profile.
- The selection is persisted to `cache/last_machine_config.txt` and becomes the pre-selected default next launch.

`ConfigRepository.get_machine_config()` resolves the active config in order: (1) the library config named in `cache/last_machine_config.txt`, (2) an explicit `machine_configs/machine_config.yaml`, (3) a single `machine_configs/machine_config_*.yaml` in the root, (4) a built-in default. Steps 2–3 are fallbacks for pinning a config by file; normal use goes through the library selector. See the [Machine Configs README](../machine_configs/README.md) for field-level details.

### illumination_channel_config.yaml

Defines all available illumination channels on the microscope.

```yaml
version: 1.0

# Controller port to source code mapping
# D1-D8: Laser channels, USB1-USB8: LED matrix patterns
controller_port_mapping:
  D1: 11   # 405nm laser
  D2: 12   # 488nm laser
  D3: 13   # 638nm laser
  D4: 14   # 561nm laser
  D5: 15   # 730nm laser
  USB1: 0  # LED full
  USB2: 1  # LED left_half
  USB3: 2  # LED right_half
  USB4: 3  # LED dark_field
  USB5: 4  # LED low_na

channels:
  # Brightfield LED
  - name: BF LED matrix full
    type: transillumination
    controller_port: USB1
    wavelength_nm: null
    intensity_calibration_file: null

  # Fluorescence channels
  - name: Fluorescence 405 nm Ex
    type: epi_illumination
    controller_port: D1
    wavelength_nm: 405
    intensity_calibration_file: 405.csv

  - name: Fluorescence 488 nm Ex
    type: epi_illumination
    controller_port: D2
    wavelength_nm: 488
    intensity_calibration_file: 488.csv
    # Optional: excitation filter (rare, most systems don't have this)
    excitation_filter_wheel: "Excitation Filter Wheel"
    excitation_filter_position: 2

  # ... additional channels
```

**Fields:**

| Field | Description |
|-------|-------------|
| `version` | Schema version (currently `1.0`) |
| `controller_port_mapping` | Maps port names to internal source codes |
| `channels[].name` | Unique identifier for the channel |
| `channels[].type` | `epi_illumination` (lasers) or `transillumination` (LED) |
| `channels[].controller_port` | Port name (D1-D8 for lasers, USB1-USB8 for LED) |
| `channels[].wavelength_nm` | Wavelength in nm (null for LED) |
| `channels[].intensity_calibration_file` | CSV file in `intensity_calibrations/` |
| `channels[].excitation_filter_wheel` | Optional: name of excitation filter wheel |
| `channels[].excitation_filter_position` | Optional: position in excitation filter wheel |

### cameras.yaml (Optional)

Maps camera IDs to hardware serial numbers. **Optional for single-camera systems.**

```yaml
version: 1.0

cameras:
  # Primary imaging camera
  - id: 1                          # Camera ID (used in channel configs and hardware_bindings)
    name: "Main Camera"            # User-friendly name for UI
    serial_number: "ABC12345"      # Camera serial number (from manufacturer)
    model: "Hamamatsu C15440"      # Optional: displayed in UI for reference

  # Secondary camera for simultaneous imaging
  - id: 2
    name: "Side Camera"
    serial_number: "DEF67890"
    model: "Basler acA2040"
```

**Fields:**

| Field | Description |
|-------|-------------|
| `version` | Schema version (`1.0`) |
| `cameras[].id` | Camera ID (must be unique, used in channel configs) |
| `cameras[].name` | User-friendly name for UI (must be unique) |
| `cameras[].serial_number` | Hardware serial number (must be unique) |
| `cameras[].model` | Optional: camera model for reference |

**Usage:**
- If `cameras.yaml` doesn't exist, the system assumes single-camera mode
- Single camera: `id` and `name` are optional (defaults applied)
- Multi-camera: `id` and `name` are required for all cameras
- Channel configs use the `id` field to reference cameras (e.g., `camera: 1`)

### filter_wheels.yaml (Optional)

Defines all filter wheels with their positions and installed filters. Channels reference filter wheels by name.

```yaml
version: 1.0

filter_wheels:
  # Emission filter wheel
  - name: "Emission Filter Wheel"
    id: 1                          # Hardware ID for controller
    type: emission                 # Filters light after sample
    positions:
      1: "Empty"
      2: "BP 525/50"               # GFP emission
      3: "BP 600/50"               # mCherry emission
      4: "BP 700/75"               # Far red emission
      5: "LP 650"                  # Long pass

  # Excitation filter wheel (optional)
  - name: "Excitation Filter Wheel"
    id: 2
    type: excitation              # Filters light before sample
    positions:
      1: "Empty"
      2: "BP 470/40"               # GFP excitation
      3: "BP 560/40"               # mCherry excitation
```

**Fields:**

| Field | Description |
|-------|-------------|
| `version` | Schema version (`1.0`) |
| `filter_wheels[].name` | User-friendly name (must be unique) |
| `filter_wheels[].id` | Hardware ID for controller (must be unique) |
| `filter_wheels[].type` | Filter wheel type: `excitation` or `emission` (optional) |
| `filter_wheels[].positions` | Map of slot number → filter name |

**Usage:**
- If `filter_wheels.yaml` doesn't exist, filter wheel settings in channels are ignored
- The same schema may be embedded under `filter_wheel_registry` in `machine_config.yaml`; a non-empty embedded list overrides `filter_wheels.yaml`.
- Filter names appear in UI dropdowns for channel configuration
- Position numbers must be ≥ 1
- Wheels here are referenced with the `standalone` source prefix in `hardware_bindings.yaml` (e.g., `standalone.1`)

**Excitation vs Emission Filter Wheels:**
- **Emission filter wheels** (most common, 0-1 per system): Referenced by acquisition channels via `filter_wheel` and `filter_position` fields in user profile configs
- **Excitation filter wheels** (rare): Referenced by illumination channels via `excitation_filter_wheel` and `excitation_filter_position` fields in machine config

### Confocal settings (`devices.xlight` / `devices.dragonfly`)

Everything a spinning-disk confocal unit needs lives under its own device entry
in `machine_config.yaml` — there is no separate confocal config file. The
presence of an **enabled** `xlight` or `dragonfly` device is what marks the
system as confocal.

> **Note**: The wheel declared here is referenced with the `confocal` source
> prefix in `hardware_bindings` (e.g., `confocal.1`), while wheels in
> `filter_wheels.yaml` / `filter_wheel_registry` use the `standalone` prefix.

```yaml
devices:
  xlight:
    driver: xlight
    enabled: true
    connection:
      serial_number: "A9KI1SXRA"
    config:
      sleep_time_for_wheel: 0.25       # seconds to wait after a wheel move
      validate_wheel_pos: false        # read the wheel back after each move
      illumination_iris_default: 80    # seeds confocal_hardware_settings
      emission_iris_default: 100
      emission_filter_wheel:
        name: "XLight emission wheel"  # optional; defaults to "Emission Wheel"
        positions:
          1: "Empty"
          2: "BP 525/50"
          3: "BP 600/50"
          4: "BP 700/75"
          5: "LP 650"
```

**Fields (`ConfocalDeviceSettings`):**

| Field | Default | Description |
|-------|---------|-------------|
| `sleep_time_for_wheel` | `0.25` | Seconds the driver waits after a wheel move |
| `validate_wheel_pos` | `false` | Default for `XLight.set_emission_filter(validate=...)`: read the position back after each move |
| `illumination_iris_default` | `100` | Seeds `ObservationState.confocal_hardware_settings.illumination_iris` |
| `emission_iris_default` | `100` | Seeds `ObservationState.confocal_hardware_settings.emission_iris` |
| `emission_filter_wheel.name` | `"Emission Wheel"` | Name shown in filter wheel dropdowns |
| `emission_filter_wheel.positions` | `{}` | Slot number → filter name; `len(positions)` is the slot count the driver accepts (8 = X-Light V3, 5 = Cicero). Omitted → the driver falls back to 8 slots and no wheel is published to the UI. |

Unknown keys in this block are rejected, so typos surface at startup.

**How it is consumed:**
- `MicroscopeAddons.build_from_global_config` passes `sleep_time_for_wheel`,
  `validate_wheel_pos` and the slot count into `XLight` / `XLight_Simulation`.
- `ConfigRepository.get_all_filter_wheels()` publishes the declared wheel under
  the `"confocal"` source, so `hardware_bindings` refs like `confocal.1` and the
  observation-state editor's filter-position picker both resolve it without a
  duplicate `filter_wheel_registry` entry.
- The iris defaults seed `confocal_hardware_settings` the first time an iris is
  edited, and when default configs are generated for a new profile.

**Redundant writes are skipped in the driver.**
`ObservationStateController.apply_optical_path` writes the wheel slot and both
irises on *every* observation-state apply, i.e. on every channel switch of a
multi-channel acquisition. Each of those is slow on the wire —
`sleep_time_for_wheel` per wheel move, a 2 s round trip per iris — and the usual
rig runs one quad-band filter and one aperture for the whole run. `XLight`
therefore caches what it last drove (`emission_wheel_pos`, `dichroic_wheel_pos`,
`slider_position`, `illumination_iris`, `emission_iris`, plus `spinning_disk_pos`
and `disk_motor_state`) and returns without touching serial when the request
matches. The dichroic filter slider is the biggest win: its round trip is 5 s.
The invariants:

- The cache is the driver's, not the controller's, so the confocal panel (which
  drives the same mechanisms) cannot make it stale.
- It starts as `None` — "unknown" — so a mechanism the unit will not report
  always writes, including a request for slot 1 or a fully closed iris.
- `XLight.__init__` calls `seed_caches_from_hardware()`, which reads back every
  mechanism the unit says it has (`rB`, `rC`, `rP`, `rJ`, `rV`, `rD`, `rN`).
  This is what makes the skipping safe: a cache guessed as 0 on a unit whose
  iris is physically at 100 makes "close the iris to 0" look redundant, so it is
  silently dropped — the hardware stays open while the software believes it
  closed. Each read is individually best-effort; one that fails logs a warning
  and leaves that cache unknown rather than breaking startup.
- It is cleared before the write and set only after the write returns, so a
  command that raises leaves the position unknown and the next apply retries.
- `validate_wheel_pos` is unchanged for the moves that do happen: a skipped move
  reads nothing back because nothing moved. An `extraction=True` move is a
  different command and is never skipped.
- `XLight_Simulation` skips the same way; its cache starts at the simulated
  unit's known state rather than `None`, because there it *is* the hardware.

`Dragonfly` has no such cache: its `set_emission_filter` is port-keyed and its
commands are ~0.1 s, so the confocal branch of `apply_optical_path` for a
Dragonfly still writes every time.

### Camera trigger routing (`devices.main_camera`)

A camera declares the line that triggers it under `io.trigger`, and how a
**software** trigger is delivered under `config.software_trigger_routing`:

```yaml
devices:
  main_camera:
    driver: tucsen
    role: main
    io:
      trigger:                        # the line that actually fires the sensor.
        controller: nidaq             # Used by Hardware trigger mode AND by
        signal_type: digital          # Software trigger mode when
        direction: output             # software_trigger_routing is hardware_line.
        channel_id: "port0/line6"
        display_name: "Main camera trigger"
    config:
      software_trigger_routing: hardware_line   # or: native
```

| Value | What `camera.send_trigger()` does |
|-------|-----------------------------------|
| `hardware_line` | The camera is programmed for hardware (Standard) trigger and every "software" trigger is a pulse on the `io.trigger` endpoint. Illumination stays software-controlled by the worker (LED steady-on across the exposure). |
| `native` | The camera's own SDK software-trigger command (GenICam `TriggerSoftwarePulse` / `TUCCM_TRIGGER_SOFTWARE`). No IO endpoint is involved. |

**Default** (resolved in one place,
`control.models.machine_config.resolve_software_trigger_routing`):
`hardware_line` when the camera declares an `io.trigger` endpoint, `native`
otherwise. An explicit value always wins.

**Validation.** `hardware_line` without an `io.trigger` endpoint is rejected when
the machine config loads, and an unknown value is rejected by name. If the
endpoint exists in the config but its controller is disabled or unavailable, the
camera raises a `CameraError` the first time the acquisition mode is set —
before a run starts — instead of timing out on a frame mid-acquisition.

**Why it is explicit.** The Tucsen Aries' native software trigger does not
reliably start exposures, so that rig must use `hardware_line`; the trigger line
is then load-bearing for ordinary software-triggered acquisition, not just for
Hardware trigger mode. Pointing `io.trigger` at a controller/channel that does
not physically reach the camera produces no error anywhere — the triggers simply
go nowhere — so the routing in effect and the endpoint it resolves to are logged
at INFO during startup:

```
Main camera SW trigger routing: hardware_line via nidaq port0/line6 (machine config declares: nidaq port0/line6)
```

and the same description is printed in the "Timed out waiting … for a frame"
error, so a misrouted line is legible rather than inferred.

### Simulation (`--simulation`, `devices.<name>.simulate`)

Two switches decide whether a device opens real hardware, and **either one is
enough to simulate it** (`control.microscope._should_simulate`):

| Switch | Scope |
|--------|-------|
| `--simulation` on the command line | every device |
| `devices.<name>.simulate: true` | that device only |

Per-device flags are what make mixed setups work — e.g. `main_camera.simulate:
true` with a real `nidaq` drives the DAQ's trigger line for an external
frame-grabbing application while Squid synthesizes its own frames. Never gate a
device on the global flag alone.

`enabled: false` is a different statement: the device does not exist on this rig
and nothing is built for it, simulated or otherwise.

**The NI-DAQ is simulated, not skipped.** An enabled `devices.nidaq` always
produces an object — `SimulatedNIDAQ` when simulating, `NIDAQ` otherwise — built
from the same IO endpoints either way, and logged at startup:

```
Building simulated NI-DAQ 'Dev1' (ao=['ao0'], do=[2, 6], di=[7])
```

That object is what registers the NIDAQ controller in the `IORegistry`, so with
it every nidaq-routed endpoint binds in simulation: `main_camera.trigger` (so
Hardware trigger mode and `software_trigger_routing: hardware_line` both work
against the simulated camera), `main_camera.frame_readout`, and the illumination
shutter/intensity lines. `SimulatedNIDAQ` records live AO/DO state and returns
from `send_edge_pulse` immediately — no pulse-width sleep, and no readout-line
diagnostic, since there is no DI line to sample.

Waveform-driven and stimulus-only observation states also run in simulation
(`SimulatedNIDAQ` arms, fires on `start_trigger`, and completes after the
waveform's own duration). **Fast acquisition does not**: it is a DAQ-clocked
pulse train the camera answers with frames, and a simulated DAQ emits no pulses,
so the Fast Acquisition tab is left out when the DAQ is simulated and the reason
is logged. With a real DAQ it stays available, simulated camera or not.

### hardware_bindings.yaml (Optional)

Maps cameras to their associated filter wheels using **source-qualified references**. This file is only needed for multi-camera systems where each camera uses a different emission filter wheel. The same schema may be embedded as `hardware_bindings` on `machine_config.yaml` and overrides this file when present.

**Physical controller:** enable `devices.emission_filter_wheel` in `machine_config.yaml` and set `config.controller_type` (e.g. `SQUID`) so the filter wheel is constructed with the rest of the microscope.

**Source-Qualified References:**

Filter wheels can come from two sources:
- **`standalone`**: Defined in `filter_wheels.yaml` (or the embedded `filter_wheel_registry`)
- **`confocal`**: Derived from the confocal device entry's `config.emission_filter_wheel`

References use the format `source.identifier` where identifier can be an ID or name:
- `confocal.1` - confocal wheel with ID 1
- `standalone.Emission Wheel` - standalone wheel named "Emission Wheel"

```yaml
version: 1.0

emission_filter_wheels:
  # Camera ID -> source-qualified wheel reference
  1: confocal.1                    # Camera 1 uses confocal wheel ID 1
  2: standalone.1                  # Camera 2 uses standalone wheel ID 1
  3: "standalone.Side Emission"    # Camera 3 uses standalone wheel by name
```

**Fields:**

| Field | Description |
|-------|-------------|
| `version` | Schema version (`1.0`) |
| `emission_filter_wheels` | Map of camera ID to source-qualified wheel reference |

**Implicit Binding (Single Camera + Single Wheel):**

If `hardware_bindings.yaml` doesn't exist and the system has exactly one camera and one emission filter wheel, the binding is implicit - no configuration needed.

**When to Create This File:**
- Multi-camera systems with separate emission wheels per camera
- Systems where camera 1 should use a confocal wheel and camera 2 a standalone wheel
- Any setup where automatic binding won't work correctly

---

## User Profiles

User profiles store acquisition settings that vary by user or experiment. Each profile is a directory under `user_profiles/`.

### Profile Management

**Creating a Profile:**
- Profiles are directories under `user_profiles/`
- Contains `channel_configs/` and `laser_af_configs/` subdirectories
- Default configs are auto-generated if profile has no configs

**Switching Profiles:**
- Profile switch clears cached configs
- New profile's configs are loaded on demand

**Save As (Copy Profile):**
- Copies all YAML files from source to destination profile
- Useful for creating variants of existing configurations

### channel_configs/general.yaml

Defines the available observation states. Each observation state is a complete light-path configuration for one acquisition step, including camera settings, illumination, and optical path.

```yaml
version: 3
observation_states:
  - name: Fluorescence 488 nm Ex
    version: 3
    confocal_mode: false
    display_color: '#1FFF00'
    camera_settings:
      exposure_time_ms: 20.0
      gain_mode: 10.0
    illuminator_states:
      - illumination_channel: Fluorescence 488 nm Ex
        intensity: 20.0
        'on': true
    emission_filter_positions:
      default: 2
    z_offset_um: 0.0

  - name: BF LED matrix full
    version: 3
    confocal_mode: false
    display_color: '#FFFFFF'
    camera_settings:
      exposure_time_ms: 20.0
      gain_mode: 10.0
    illuminator_states:
      - illumination_channel: BF LED matrix full
        intensity: 5.0
        'on': true
    emission_filter_positions:
      default: 1
    z_offset_um: 0.0
channel_groups: []
```

### observation_presets/{name}.yaml

Saved ObservationState presets. These are complete snapshots of all settings (camera, illumination, optical path) that can be loaded to restore a specific configuration. Presets are objective-independent — users load different presets when switching objectives if different settings are needed.

### laser_af_configs/{objective}.yaml

Laser autofocus configuration per objective. Contains calibration data and detection parameters.

```yaml
version: 1.0

# Crop region
x_offset: 0
y_offset: 0
width: 1536
height: 256

# Calibration
pixel_to_um: 1.0
x_reference: null
has_reference: false
calibration_timestamp: ""
# NOTE: the calibration sweep distance is NOT here. It is machine policy scaled
# by objective magnification: devices.laser_af.config.calibration in the machine
# config (see docs/laser-autofocus.md).

# Detection parameters
laser_af_range: 100.0
laser_af_averaging_n: 3
spot_detection_mode: dual_right
displacement_success_window_um: 1.0

# Spot detection
spot_crop_size: 100
correlation_threshold: 0.9
y_window: 96
x_window: 20
min_peak_width: 10.0
min_peak_distance: 10.0
min_peak_prominence: 0.25
spot_spacing: 100.0
filter_sigma: null

# Camera settings
focus_camera_exposure_time_ms: 0.2
focus_camera_analog_gain: 0.0

# Reference image (base64 encoded)
reference_image: null
reference_image_shape: null
reference_image_dtype: null
```

---

## Default Config Generation

When a profile has no existing configs, the system auto-generates defaults:

1. **Trigger**: Profile loaded without `general.yaml`
2. **Source**: Uses `illumination_channel_config.yaml` as template
3. **Process**:
   - Creates one observation state per illumination channel
   - Sets display colors based on wavelength (fluorescence) or white (LED)
   - Uses default exposure (20ms), gain (10), intensity (20% fluorescence, 5% LED)
   - Generates only `general.yaml` (no per-objective files)

**Note**: Default generation is skipped if legacy XML configs exist (migration should run first).

---

## Acquisition Output

When running an acquisition, the effective configuration is saved to the experiment directory:

```
experiment_output/
└── acquisition_channels.yaml
```

This file captures the exact settings used, including:
- Objective name
- Confocal mode state
- All channel configurations (merged and with overrides applied)

---

## Best Practices

### For Users

1. **Use profiles for different experiments**
   - Create a profile for each experiment type
   - Use "Save As" to create variants

2. **Use observation presets for different objectives**
   - Save a preset for each objective/experiment combination
   - Load presets when switching objectives to restore optimal settings

3. **Set z_offset for parfocal correction**
   - If channels aren't parfocal, set z_offset in general.yaml

### For System Administrators

1. **Machine configs are global**
   - Changes affect all users
   - Test changes before deploying

2. **Keep intensity calibrations updated**
   - Re-run calibration if laser power changes
   - Store calibration CSVs in `machine_configs/intensity_calibrations/`

3. **Confocal presence matters**
   - Enable `devices.xlight` (or `devices.dragonfly`) only if a confocal unit exists
   - An enabled confocal device is what enables confocal settings in acquisition configs

---

## Troubleshooting

### "No channels available"

- Verify `general.yaml` exists in profile's `channel_configs/`
- Check `illumination_channel_config.yaml` has channels defined
- Ensure illumination channel names match between files

### "Illumination channel not found"

- The `illumination_channel` field in `general.yaml` must reference a channel defined in `illumination_channel_config.yaml`
- Check for typos in channel names

### "Profile not found"

- Profile directory must exist under `user_profiles/`
- Profile must have `channel_configs/` subdirectory

### Settings not persisting

- Changes to UI update `general.yaml` directly
- Verify the correct profile is active
- Check file permissions

---

## See Also

- [Configuration API Reference](configuration-api.md) - Developer documentation
- [Configuration Migration](configuration-migration.md) - Upgrading from legacy format
- [Machine Configs README](../machine_configs/README.md) - Hardware setup guide
