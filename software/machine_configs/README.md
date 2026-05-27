# Machine Configurations

This directory contains hardware-specific configuration files for the microscope.
These files define the physical hardware setup and should be configured once per machine.

## Selecting the active machine configuration

The hardware configuration loaded at startup is chosen from a **library** of
configs and remembered between runs.

- **`library/`** — the set of selectable machine configs (`machine_config*.yaml`).
  Add or edit files here to make them available for selection. This is the
  starting point for new setups; copy one and adapt it to your hardware.
- **Startup selector** — the user-profile dialog shown on launch has a
  *Machine config* dropdown in the bottom-left listing everything in `library/`.
  The choice is **global** (shared by every user profile), not per-profile.
- **Persistence** — the selection is written to `../cache/last_machine_config.txt`
  (just the file name) and becomes the pre-selected default on the next launch.

### Resolution order

`ConfigRepository.get_machine_config()` resolves the active config in this order:

1. The library config recorded in `cache/last_machine_config.txt` (the last selection).
2. An explicit `machine_config.yaml` placed directly in this directory.
3. A single `machine_config_*.yaml` placed directly in this directory.
4. A built-in default (used for simulation / first run with an empty library).

Steps 2–3 are fallbacks for setups that pin a config by dropping a file in the
root; normal use goes through the library + startup selector (step 1).

## Files

### `library/`
Holds the selectable machine configs (`machine_config*.yaml`), one per hardware
setup. The startup selector lists these; see *Selecting the active machine
configuration* above.

### `illumination_channel_config.yaml`
Defines all available illumination channels on this machine:
- LED matrix patterns (transillumination)
- Fluorescence laser lines (epi-illumination)
- Controller port mappings (D1-D8 for lasers, USB for LED matrix)
- Intensity calibration file references

### `confocal_config.yaml` (Optional)
Only create this file if the system has a confocal unit. Its presence indicates
that confocal settings should be included in acquisition configs.

Defines:
- Filter wheel slot to filter name mappings
- Properties available for configuration (public vs objective-specific)

### `intensity_calibrations/` (Optional, user-generated)
Contains CSV files mapping DAC percentage to optical power (mW) for each laser line.
Files are named by wavelength (e.g., `405.csv`, `488.csv`).

To generate calibration files, run: `tools/generate_intensity_calibrations.py`

### `calibration_tests/` (Optional, user-generated)
Contains CSV files with calibration test results (measured power at various set points).
Used to verify calibration accuracy.

To generate test files, run: `tools/evaluate_intensity_calibration.py`
