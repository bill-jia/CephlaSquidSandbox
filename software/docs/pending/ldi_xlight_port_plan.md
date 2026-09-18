# LDI + X-Light V3 rig: port from the legacy fork

Status: **config written, code changes pending** (2026-09-17).

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

### 1. LDI builder ignores the config entry (required)

`microscope.py::_build_illumination_controller` does
`serial_peripherals.LDI()` — no serial number, no modes. `LDI.__init__`
hard-codes `SN="00000001"` and reads `control._def.LDI_INTENSITY_MODE` /
`LDI_SHUTTER_MODE`, which `config_bridge.apply_machine_config` never sets.

Change:
- `LDI.__init__(self, SN, intensity_mode="PC", shutter_mode="PC")`; drop the
  `_def.LDI_*` globals (no other reader).
- Builder passes `dev_entry.connection.serial_number` and
  `dev_entry.config.get("intensity_mode")` / `("shutter_mode")`.
- Fail loudly when the SN is not found: `SerialDevice` currently leaves
  `self.serial = None` and the first write raises `AttributeError`. Raise a
  `SerialDeviceError` naming the SN instead.

### 2. LDI never receives its mode commands (required)

`_build_serial_device` calls `light_source.initialize()` only, and
`LDI.initialize()` sends just `run!`. The legacy
`IlluminationController._configure_light_source` sent `INT_MODE=PC` and
`SH_MODE=PC` after `run!`. If the LDI was last left in EXT mode, every
serial `set:` / `shutter:` command is ignored and the lasers never fire.

Change: `LDI.initialize()` = `run!` → `set_intensity_control_mode(self.intensity_mode)`
→ `set_shutter_control_mode(self.shutter_mode)`.

### 3. Simulation drops the LDI entirely (recommended)

`elif driver == "ldi" and not simulated:` skips the device, so a simulated
launch of this config shows no laser channels. Build
`LDI_Simulation()` when simulated (same for `celesta` / `versalase`;
`coolled_pe400` has the same gap).

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

### 5. Confocal config is a global file, not per library config (recommended)

`ConfigRepository.get_confocal_config()` always reads
`machine_configs/confocal_config.yaml`, shared by every library entry. On a
machine that hosts several library configs (this sandbox), creating that
file would make the CoolLED rigs think they have a confocal.

Change: add `confocal: Optional[ConfocalConfig]` to `MachineConfig` and have
`get_confocal_config()` prefer the embedded block, mirroring
`filter_wheel_registry` / `hardware_bindings`. The new YAML already carries
the block under `confocal:` (currently stored in `model_extra`, harmless).

Related: the observation-state editor's filter-position picker
(`gui/widgets/monitoring.py::_populate_filter_positions_for_combo`) reads
only the standalone `filter_wheel_registry`, so the X-Light wheel is
declared there rather than as a `confocal` wheel. Either teach the picker to
merge `config_repo.get_all_filter_wheels()` or keep the standalone
declaration and document it.

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
