# Firmware

## This fork's firmware integration

The controller is **1.5.1**, based on upstream firmware **1.5**, commit
`da109c5a0c7727d6d57962e5e454b359b6de47dd`. This includes the 1.2–1.4 move,
filter-wheel, and illumination fixes plus per-axis TMC2660/TMC2240 driver
auto-detection. The client compatibility fixes preserve this fork's stage
configuration, illumination routing, and imaging features.

Patch 1.5.1 adds passive, CRC-protected diagnostics in the three reserved status
bytes without changing packet length or the major/minor version byte. It reports
the patch version through telemetry. The client automatically records recent
commands/status and dumps them on a failed wait; see
[MCU diagnostics](../software/docs/mcu-diagnostics.md) for activation checks,
the telemetry layout and interpretation. The client version tool reports 1.5.1.
The revised 1.5.1 image uses telemetry schema 2 for timing maxima per snapshot
interval; the client also accepts the earlier schema 1 since-boot values. Verify
schema 2 in the startup telemetry log after flashing the revised image.

The initial 1.4 integration assumed TMC2660 hardware. USB version reporting alone
does not verify the motor driver type or motor power. Use 1.5 when the controller
may have TMC2240 drivers; unidentified axes reject motion instead of being driven
with an assumed driver configuration. FastLED 3.10.3 and PacketSerial 1.4.0 remain
pinned following upstream #725 to avoid ASCII logging on the binary status link.

The updated Squid W/W2 wheel client requires firmware >= 1.2, including when
hardware initialization is skipped. Flash the new controller firmware before
starting that client with a Squid wheel enabled. Other wheel backends (including
XLight's built-in wheels) are unaffected. A failed wheel home remains an error;
the client does not continue acquisition while claiming a known wheel position.

Before deploying on an instrument:

1. Save its current working firmware image and client revision for rollback.
2. Run `pio test -e native` and `pio run -e teensy41` in `firmware/controller`.
3. Verify XYZ direction, travel scaling, homing, limits, and commanded current.
4. Verify every configured W/W2 slot after repeated homes and initialization.
5. Test rejected moves and heartbeat traffic during long moves; the client must
   report rejection promptly without treating a keepalive as move completion.
6. Switch MCU-controlled illumination channels during hardware-triggered live
   capture at exposure times below and above 30 ms; the previous port must go low.
7. Verify autofocus, acquisition abort/finalization, and illumination shutdown.

Building does not flash a controller. Upload only after identifying the intended
board, following the instructions below.

## Directory Structure

```
firmware/
├── controller/          # Main motion controller (Teensy 4.1)
├── joystick/            # Joystick/control panel (Teensy LC)
└── legacy/              # Archived firmware versions
```

## Building with PlatformIO (Recommended)

[PlatformIO](https://platformio.org/) is the recommended build system for firmware development. It provides consistent builds, dependency management, and command-line tooling.

### Installation

```bash
# Using pip
pip install platformio

# Or using Homebrew (macOS)
brew install platformio
```

`pio run -e teensy41 -t upload` flashes the controller with the [Teensy Loader](https://www.pjrc.com/teensy/loader.html) application, which PlatformIO bundles in its Teensy platform tools (`tool-teensy`) — there is no separate uploader to install. During upload the Teensy Loader window opens and attempts to reboot the board into the bootloader. If it asks for manual program mode, briefly press and release the Program button on the controller's Teensy board, then let the loader finish programming and rebooting it.

> **Note:** The Teensy Loader is a graphical application, so uploading normally requires a desktop (display) environment. On platforms where the GUI app is unavailable (e.g. Linux ARM), PlatformIO automatically falls back to the command-line loader (`teensy_loader_cli`).

On Linux, install the [PJRC udev rules](https://www.pjrc.com/teensy/00-teensy.rules) into `/etc/udev/rules.d/` so non-root users can flash. A copy ships with PlatformIO at `~/.platformio/packages/tool-teensy/00-teensy.rules`.

### Quick Start

Run these commands from the repository root. Controller and joystick are
separate PlatformIO projects; `-e` selects an environment within the project
specified by `-d`.

```bash
# Build and upload controller firmware
pio run -d firmware/controller -e teensy41 -t upload

# Build and upload joystick firmware
pio run -d firmware/joystick -e teensyLC -t upload
```

The joystick firmware is unchanged by this integration and does not need to be
reflashed for controller firmware 1.5. Uploading it requires USB access to the
joystick's own Teensy LC, rather than the controller's Teensy 4.1 USB port.

The controller defaults to `teensy41` for ordinary builds and uploads. The
`native` environment is only for `pio test -e native`; building it with `pio run`
has no application entry point and fails with a missing `main`/`WinMain` error.

**Important:** Before uploading, verify only one Teensy is connected:
```bash
pio device list
```
If multiple devices appear, disconnect the extras before uploading. The upload tool may not warn you and could flash the wrong board.

### Common Commands

| Command | Description |
|---------|-------------|
| `pio run` | Compile firmware |
| `pio run -t upload` | Compile and upload to device |
| `pio run -t clean` | Clean build artifacts |
| `pio device monitor` | Open serial monitor |
| `pio run -t upload && pio device monitor` | Upload and monitor |
| `pio test -e native` | Run unit tests (no hardware required) |

### Running Tests

Unit tests run on your host machine without needing hardware:

```bash
cd firmware/controller
pio test -e native
```

**Example output:**
```
test_crc8:      test_crc8_empty_data           [PASSED]
test_crc8:      test_crc8_single_byte_zero     [PASSED]
test_protocol:  test_command_ids_are_unique    [PASSED]
...
================= 11 test cases: 11 succeeded =================
```

Tests are located in `controller/test/` and use the [Unity](https://github.com/ThrowTheSwitch/Unity) test framework.

### Build Output

After successful compilation, the firmware binary is located at:
- `.pio/build/teensy41/firmware.hex` (controller)
- `.pio/build/teensyLC/firmware.hex` (joystick)

### Troubleshooting

**Device not found during upload:**
- Ensure Teensy is connected via USB
- Check that no other application is using the serial port
- If firmware is unresponsive, press the button on Teensy to enter bootloader mode

**Permission denied (Linux):**
```bash
sudo usermod -a -G dialout $USER
# Log out and back in
```

**First build is slow:**
- PlatformIO downloads toolchains and libraries on first run
- Subsequent builds are much faster (incremental compilation)

## Building with Arduino IDE (Alternative)

If you prefer Arduino IDE:

### Controller (Teensy 4.1)

1. Install [Teensyduino](https://www.pjrc.com/teensy/teensyduino.html)
2. Open `controller/main_controller_teensy41.ino` in Arduino IDE
3. Select Board: "Teensy 4.1"
4. Click Upload

### Joystick (Teensy LC)

1. Install [Teensyduino](https://www.pjrc.com/teensy/teensyduino.html)
2. Open `joystick/control_panel_teensyLC.ino` in Arduino IDE
3. Select Board: "Teensy LC"
4. Click Upload

## Controller

The main motion controller firmware for Teensy 4.1. Handles:
- XYZ stage motion control (TMC4361A + TMC2660 or TMC2240 drivers)
- Illumination control (lasers and LED matrix)
- Camera triggering
- Serial communication with host software

### Driver identification and firmware 1.5 limits

Each stage axis is identified at boot as TMC2660, TMC2240, or unknown. An unknown
axis rejects host moves, homing, and PID activation; panel-driven motion is also
blocked. `INITIALIZE` retries unidentified XYZ axes. W/W2 are probed on each wheel
initialization. Firmware 1.5 also refuses unencodable motor-current requests and
reports `CMD_EXECUTION_ERROR`; the client surfaces that error instead of waiting
for a completion that cannot arrive.

The [upstream 1.5 release notes](https://github.com/Cephla-Lab/Squid/releases/tag/firmware-v1.5)
record these remaining limits:

- TMC2240's boot-time StallGuard configuration can inhibit X/Y until `INITIALIZE`.
- TMC2240 chopper settings can cause an audible 13–15 kHz whine during motion.
- The TMC2660 cold-boot probe was bench-verified, but warm re-probing with a
  populated TMC2660 W/W2 slot was not. Test repeated wheel initialization on such
  instruments before deployment. XLight's separately controlled wheels do not
  use these W/W2 driver slots.

Keep `TMC_PROBE_REPORT` disabled in an application image. Its ASCII diagnostic
lines share the binary status port and can corrupt the host's position parsing.
A reported version of 1.5 confirms the application is running, but motor motion,
limits, homing, and illumination still require instrument validation.

### Configuration

Hardware-specific settings are in `src/def/def_v1.h`. This includes:
- Motor parameters (steps per rev, microstepping, current)
- Stage limits and velocities
- Joystick sensitivity
- Limit switch polarity

### Build Options

**Disable Laser Safety Interlock:**

By default, the firmware includes laser safety interlock detection. To disable it:

```bash
PLATFORMIO_BUILD_FLAGS="-DDISABLE_LASER_INTERLOCK" pio run -e teensy41 -t upload
```

> **WARNING:** Only use this flag for systems without lasers installed. Disabling the interlock removes laser safety protection.

### Source Structure

```
controller/
├── main_controller_teensy41.ino    # Entry point
├── platformio.ini                   # PlatformIO config
├── test/                            # Unit tests (run with pio test -e native)
│   ├── test_crc8/                  # CRC8 checksum tests
│   └── test_protocol/              # Protocol/command ID tests
└── src/
    ├── commands/                    # Command handlers
    │   ├── commands.cpp/h          # General commands
    │   ├── light_commands.cpp/h    # Illumination control
    │   └── stage_commands.cpp/h    # Motion control
    ├── def/
    │   └── def_v1.h                # Hardware configuration
    ├── tmc/                         # TMC stepper driver library
    ├── utils/
    │   └── crc8.cpp/h              # CRC calculation
    ├── init.cpp/h                   # Initialization routines
    ├── operations.cpp/h             # Main loop operations
    ├── serial_communication.cpp/h   # Serial protocol handling
    ├── functions.cpp/h              # Utility functions
    ├── globals.cpp/h                # Global state variables
    └── constants.h                  # Constants and pin definitions
```

## Joystick

Control panel firmware for Teensy LC. Handles:
- Joystick X/Y axis input
- Rotary encoder for focus control
- Button states
- Serial communication with main controller

## Legacy

Archived firmware versions kept for reference. Not actively maintained.
