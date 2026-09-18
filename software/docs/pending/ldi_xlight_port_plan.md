# LDI + X-Light V3 rig: port from the legacy fork

Status (2026-09-17): config written; **gaps 1-3 implemented** on branch
`ldi-xlight-port` (tests: `tests/control/test_ldi_driver.py`,
`tests/control/test_illumination_builder_ldi.py`); gaps 4-8 pending.
Untested on the rig.

Config: `machine_configs/library/machine_config_Squid+_LDI_XLight_TucsenAries6506.yaml`
Source: `C:\Users\jialab\Desktop\Squid_XLightV3\software\configuration_Squid+_Tucsen_LDI_XLight.ini`

## How the legacy fork drives this hardware

| Subsystem | Legacy fork (ini + `_def` globals) | This codebase |
|---|---|---|
| LDI intensity | `LDI_INTENSITY_MODE=PC` → serial `set:<line>=<pct>` | `SerialIlluminationDevice.set_intensity` → `LDI.set_intensity(serial_key, pct)` |
| LDI shutter | `LDI_SHUTTER_MODE=PC` → serial `shutter:<line>=True/False` | `SerialIlluminationDevice.turn_on/off` → `LDI.set_shutter_state` (no `io.shutter` line) |
| LDI line aliasing | `LDI.channel_mappings` 488→470, 561→555, 638→640 (applied by `IlluminationController`) | **Not applied** on the `illumination_devices` path; `serial_key` is sent verbatim, so the alias is spelled out in the YAML |
| LDI port | hard-coded SN `"00000001"`, 9600 8N1 | same driver, same hard-coded SN (see gap 1) |
| MCU role for lasers | none (DAC + TTL bypassed) | none |
| X-Light | `xlight_serial_number`, `xlight_sleep_time_for_wheel`, `xlight_validate_wheel_pos`, `XLIGHT_ILLUMINATION_IRIS_DEFAULT` | `devices.xlight` (`connection.serial_number`, `config.sleep_time_for_wheel`); the other three are `_def` constants config_bridge never sets (gap 4) |
| Emission filter per channel | `filter_position` in `general.yaml` (the ini's `xlight_emission_filter_mapping` was dead code) | `ObservationState.emission_filter_positions["default"]` → `apply_optical_path` → `xlight.set_emission_filter` |
| Iris per objective | `confocal_hardware_settings` in objective YAML | `ObservationState.confocal_hardware_settings` (seeded from `confocal_config.yaml` model) |
| Confocal / widefield | `Microscope._sync_confocal_mode_from_hardware` + widget button | identical |
| Disk motor | GUI button only, never auto-started | identical |
| Laser AF | MCU pin 15 gate, Daheng MER2-630-60U3M focus camera | `devices.laser_af.io.laser_gate = teensy pin:15`, `devices.focus_camera` daheng |
| Camera | Tucsen ARIES-6506, MCU trigger, Software trigger default | same |
| Shutdown | never shuts down LDI or X-Light | same gap (gap 6) |

## Gaps that need code changes

Ordered by "will not work at all" → "quality of life".

### 1. LDI builder ignores the config entry — DONE

`LDI(SN, intensity_mode="PC", shutter_mode="PC")` /
`LDI_Simulation(SN=None, ...)`; the `_def.LDI_*` globals are gone.
`microscope.py::_build_serial_light_source` passes
`connection.serial_number` and `config.intensity_mode/shutter_mode` from the
`illumination_devices` entry (missing SN → `ValueError` naming the device).
A serial number with no matching COM port raises `SerialDeviceError`
listing the ports that were found.

### 2. LDI never receives its mode commands — DONE

`LDI.initialize()` now sends `run!` → `INT_MODE=PC|EXT` → `SH_MODE=PC|EXT`.

### 3. Simulation drops the LDI entirely — DONE

Simulated launches build `LDI_Simulation` and `CoolLEDpE400_Simulation`
through the same `_build_serial_device` path as the real drivers.
`celesta`, `andor_laser`, `versalase` have no simulation class and are still
skipped when simulated.

### 4. X-Light config keys are not bridged (recommended)

`config_bridge` sets only `XLIGHT_SERIAL_NUMBER` and
`XLIGHT_SLEEP_TIME_FOR_WHEEL`. The YAML also carries
`validate_wheel_pos`, `emission_filter_positions`,
`illumination_iris_default`, `emission_iris_default`; today they are inert.

Change: bridge them to `XLIGHT_VALIDATE_WHEEL_POS`,
`XLIGHT_EMISSION_FILTER_POSITIONS`, `XLIGHT_ILLUMINATION_IRIS_DEFAULT`,
`XLIGHT_EMISSION_IRIS_DEFAULT`. Caveat: `control/models/confocal_models.py`
copies the iris defaults into `CONFOCAL_MODELS` **at import time**, so the
registry must read `_def` lazily (property or function) or the bridge runs
too late. Better: pass `sleep_time_for_wheel` and `validate_wheel_pos` into
`XLight.__init__` and stop reading `_def` in `apply_optical_path`.

### 5. Remove `confocal_config.yaml` and nest confocal settings under the device (recommended)

`ConfigRepository.get_confocal_config()` reads a global
`machine_configs/confocal_config.yaml` that has never existed on any rig
(only the `.example` does). Its only runtime effects are (a) seeding iris
defaults through `ConfocalConfig.model` → `CONFOCAL_MODELS` →
`_def.XLIGHT_*_IRIS_DEFAULT` when the GUI first edits an iris, and (b)
contributing wheels under the `"confocal"` source of
`get_all_filter_wheels()`, which only multi-camera `hardware_bindings`
resolution reads. `save_confocal_config` has no callers, and the X-Light
self-reports its wheels and irises through the `idc` capability bits, so
`model` is redundant.

Change (removal, not a new loader):
- Delete `ConfocalConfig`, `confocal_models.py`, `get/has/save_confocal_config`,
  `confocal_config.yaml.example`, the docs sections, and their tests.
- Move everything into `devices.xlight.config` (same shape for `dragonfly`):
  `sleep_time_for_wheel`, `validate_wheel_pos`, `illumination_iris_default`,
  `emission_iris_default`, and `emission_filter_wheel: {positions: {1: …, 8: …}}`
  (slot count = `len(positions)`, replacing `XLIGHT_EMISSION_FILTER_POSITIONS`).
- Iris seeding reads the device config defaults, or better the hardware at
  startup (`xlight.get_illumination_iris()` / `get_emission_iris()`), the way
  `_sync_confocal_mode_from_hardware` already reads the disk position.
- `get_all_filter_wheels()["confocal"]` is derived from the xlight/dragonfly
  device entry, and the observation-state editor's filter-position picker
  (`gui/widgets/monitoring.py::_populate_filter_positions_for_combo`) uses
  `get_all_filter_wheels()` instead of only the standalone registry.
- Then drop the `confocal:` block and the standalone `filter_wheel_registry`
  entry from the new library YAML; both exist only to work around the above.

### 6. Shutdown leaves the LDI and disk untouched (recommended)

`Microscope.close()` closes MCU, filter wheel, focus camera, camera. It
never calls `illumination_controller` shut_down (which would run
`LDI.shut_down`: intensity 0 + shutter closed on every line + port closed)
and never touches `addons.xlight` (disk motor keeps spinning, port stays
open). Add both; `XLight` needs a `close()` that stops the motor and closes
the serial connection.

### 7. Hardware trigger semantics (document only)

`main_camera.trigger` on the Teensy sends `control_illumination=True`,
which strobes MCU ports only. Multipoint opens the LDI serial shutter
before the trigger regardless of trigger mode, and live opens it at
`start_live`, so hardware trigger works but with the shutter held open for
the whole window. Per-frame strobing needs `shutter_mode: EXT` plus a TTL
line (MCU D-port or NI-DAQ) declared as `io.shutter` on each LDI channel.

### 8. Library-wide config validation test (nice to have)

No test loads every file in `machine_configs/library/`. Add one that
`MachineConfig.model_validate`s each and asserts `validate_io_lines() == []`.

## Suggested order

1. Gaps 1 + 2 together (one PR: LDI ctor/initialize + builder), with a
   unit test that a fake serial sees `run!`, `INT_MODE=PC`, `SH_MODE=PC`,
   `set:470=…`, `shutter:470=True` for the 488 nm channel.
2. Gap 3 (sim device) so the config can be exercised off-rig.
3. Gap 5 (embedded confocal) then gap 4 (bridge / ctor args).
4. Gap 6 (shutdown).
5. Gap 8.

Then on the rig: confirm the LDI USB serial number (the `"00000001"`
default is the legacy hard-code, not a measured value), the X-Light `idc`
capability bits, camera `reverse_x/reverse_y`, and recalibrate laser AF
for each objective (per-objective files live in the user profile).
