# MCU timeout diagnostics (controller firmware 1.5.1)

For the evidence and possible causes behind this work, see the
[October 31 Z-stack timeout investigation](development/2026-10-31-z-stack-mcu-timeout.md).

Client diagnostics are always enabled; `--verbose` is not required. The client
keeps bounded in-memory histories of 2,048 status packets (about 20 seconds at
100 Hz), 1,024 command/wait/timing events, and 64 firmware snapshots. It writes
these to the normal application log as `[MCU-DIAG]` records when a command wait
times out or reports an aborted command. Successful commands do not dump history.
A separate monitor also writes a plain-language INFO-level `[MCU-HEALTH]` summary every
60 seconds and on normal controller close. The existing timeout and retry policy
is unchanged.

The client reads available serial data in batches, then parses every status
packet in wire order. It retains partial packets across reads and preserves
CRC resynchronization, legacy zero-CRC compatibility, command completion and
the existing serial-overflow reset policy. This reduces OS read/query overhead;
it does not establish the cause of the original timeout. Buffer observations
can now include bytes already read into the client's bounded parsing buffer.

At startup the log confirms that the recorder is enabled and reports the serial
port and major/minor firmware version. With 1.5.1, expect a second INFO record:

```
[MCU-DIAG] Firmware telemetry active: {"firmware": "1.5.1", ...}
```

Confirm this before a long acquisition. An older firmware image still works
with client diagnostics, but cannot provide firmware telemetry. A recorder-enabled
message alone does not confirm firmware telemetry. No GUI setting is needed.

The revised 1.5.1 image reports **schema 2**, with timing maxima per snapshot
interval. Rebuild/reflash this image to obtain the interval timings if the initial
1.5.1 image was already uploaded. Both images report version 1.5.1, so check the
`schema` field as well. The updated client still accepts schema 1 and explicitly
labels those older maxima `since_boot`.

The controller source is based on the imported upstream 1.5 driver support and
preserves its TMC2660/TMC2240 detection, motion guards, move-error reporting and
illumination behavior. This change does not flash an instrument automatically.
Build `firmware/controller` using the instructions in `firmware/README.md`, then
upload the controller image before collecting firmware diagnostics. The joystick
image is unchanged. Leave `DEBUG_MODE` and `TMC_PROBE_REPORT` off: ASCII output
must not be mixed into the binary status stream.

## Capturing shorter reader stalls

The independent diagnostic monitor checks cached reader timing every 50 ms.
At a reader phase or accepted-packet gap of at least **200 ms**, it captures
thread stacks, command state, recent packet/event histories and firmware
telemetry, at most **once every 30 seconds** per controller. Checks perform no
serial IO and do not acquire the command lock. The threshold is diagnostic;
motion timeouts and retry behavior are unchanged.

The main log gets a concise `[MCU-DIAG] Reader delay ...` warning giving the
observed phase, cached buffer size and full file path. Detailed snapshots are
saved to `mcu_diagnostics\stalls-<recorder-id>.jsonl` alongside the health file.
Each JSON line is one self-contained capture. Preserve these files for diagnosis;
they are retained across restarts and can grow during repeated stalls. If file
writing fails, the snapshot falls back to the main log.

`ongoing_at_observation=true` means the reader phase or packet silence still
exceeded the threshold when inspected. A `recovered` warning means a completed
gap was observed after the reader resumed; `recent_gaps` preserves its timing
and, for reader-phase gaps, the previous phase. The accompanying stacks describe
the time of capture, not necessarily the cause of an already-recovered stall.
Cooldown-suppressed recovered gaps are not replayed later. A continuing stall
can be sampled again after 30 seconds. Health aggregates still count gaps during
the cooldown; full failed-wait dumps remain independent of it.

The monitor is a Python thread, so it cannot capture while the whole process or
GIL is blocked. When execution resumes, completed-gap evidence may survive but
the causal stack may already be gone. Cached buffer size is the last observation,
not a new query of the USB/serial driver. Disk writing happens on the monitor,
not on the reader; a slow disk can delay subsequent diagnostic checks.

## Reading a failure

The dump has `events`, `rx`, `telemetry`, `threads`, and `snapshot` records. Histories are
written in chunks of at most 64 entries, tagged with a shared dump ID. `snapshot`
is written first and contains the wait-entry and
failure states, history limits/counts, last-reception age, reader phase/liveness,
buffer depth high-water mark, and firmware-snapshot age. State is a best-effort
observation; diagnostics never acquire the command lock or query serial during
the timeout dump. Logging occurs after releasing the condition variable used by
the reader. It sends no diagnostic commands or additional motion commands.

`threads` contains Python stack locations for all registered and unregistered
Python threads visible to the interpreter, including thread names, Python/native
IDs, filenames, functions and line numbers. Frames are listed innermost first,
limited to 64 per thread, with truncation marked. Capture happens after the wait
fails and releases its condition variable, before writing the dump. It reads no
source files or local-variable values. It can identify a blocked Python call or
lock site, but is not a native C/driver stack or a profiler. If the process cannot
execute Python at all, neither this capture nor the health monitor can run.

- TX events include raw bytes, a monotonically increasing host TX sequence,
  the tracked command sequence, heartbeat/resend classification, calling thread,
  write duration and byte count. The host sequence disambiguates 8-bit wire-ID
  rollover. Heartbeats retain the application command ID under the 1.5 client
  contract; they do not replace the tracked operation.
- RX entries include the full 24 bytes, CRC validity, packet gap, and the pending
  host ID/sequence observed before the reader acquires the command lock.
- Reader events distinguish serial polling/reading, command-lock acquisition,
  status processing and external callbacks. A phase gap over 100 ms is retained.
  Buffer discards, CRC resynchronization failures, reconnect attempts in the
  reader, exceptions and retry reasons are also recorded. TX duration includes
  any reconnect performed internally by the serial wrapper.
- Relative Z events include requested displacement, raw direction, axis configuration,
  microsteps and backlash submove. Stack moves include timepoint, region, FOV,
  and source/target planes; exceptions retain their full traceback and context.

Interpret the evidence together:

| Evidence | Interpretation |
|---|---|
| Fresh status, matching command, `IN_PROGRESS`, Z not at target | Investigate motion/driver/limits using firmware state. |
| Fresh status, Z at target, driver completion flags not satisfied | Inspect target-reached, velocity/ramp state and PID error/tolerance. |
| Z finished but another axis busy | Firmware's global busy status is held by another axis. |
| Large host RX gap, growing serial backlog, small firmware loop/status gaps | Host reception/processing delay is more likely. |
| Large firmware loop or status-generation gap | MCU-side processing stalled or slowed. |
| No new status or complete telemetry | Last snapshot is historical; absence alone cannot distinguish USB loss from a firmware hang. |

Host times use monotonic seconds with a Unix/monotonic clock anchor. Firmware
uptime is an independent 32-bit millisecond clock. In schema 2, firmware max-gap
values reset at each snapshot and describe the interval ending at that sample,
normally about 320 ms. A gap crossing a snapshot boundary is retained in the
next interval. Schema 1 maxima are high-water marks since boot. Neither kind
is necessarily attributable to the latest command; consult `timing_scope`. Counter
rollover and MCU restart must be considered when comparing samples. A telemetry
sample is captured at its first page, roughly 310 ms before its final page; it is
not a live snapshot taken at timeout. Serial backlog can make it older still.
Driver position is the TMC4361A motion-controller coordinate, not independent
proof of physical movement. PID and encoder configuration determine what feedback
is available.

## Periodic health summaries and short-run timing report

`[MCU-HEALTH]` lines appear in the main application log, normally
`C:\Microscope_Data\logs\main_hcs.log`, at INFO level. For example:

```text
[MCU-HEALTH] last 60 s: 6000 packets, longest gap 24.0 ms; retries 0, failed waits 0, CRC errors 0, discarded 0 bytes; reader running, last packet 0.01 s ago; telemetry fresh (0.45 s old); Z completion max 18.0 ms
```

These show observations, not a blanket "healthy" verdict. Full records (including
histograms, counters and decoded firmware telemetry) are saved separately to
`C:\Microscope_Data\logs\mcu_diagnostics\health-<recorder-id>.jsonl`, one JSON object
per line. The directory follows the configured main log's directory. A startup
`[MCU-DIAG] health details:` line gives the exact path; each controller session has
a separate file. Files are retained across restarts, so archive the `mcu_diagnostics`
folder along with the main log when sharing a failure. Failure-triggered history
and thread-stack dumps still appear in the main log.

A separate `MCU-health`
thread collects cached data without querying serial or taking the command lock.
Thus it can report a stalled reader while that thread is blocked. It does not
prevent a hard process hang, and a permanent GIL block prevents it from running.
Each detailed record is flushed/closed after writing. If writing fails, a warning
explains the problem and full JSON records fall back to the main log until file
writing recovers, so the evidence is retained. The most recent partial minute
may still be lost if the process is terminated abruptly.

Each detailed record carries a recorder ID, a reporting-window duration, counter deltas
and lifetime totals, accepted-packet gap statistics, serial-write timing,
observed buffer sizes/discards, reader phase/liveness, retry/failure counts,
telemetry age/state, the latest decoded snapshot, and observed firmware timing
maxima grouped by their timing scope. `telemetry_state=fresh` means the first
page of the last complete snapshot arrived within 1.5 seconds; also inspect
`last_rx_age_s`. It does not prove the underlying physical stage is healthy or
that buffered data was generated recently.

Completed commands are grouped by opcode. Their duration is measured from the
original host send to observed completion, including writes, retries, transport
and scheduling delays. Heartbeats do not reset that clock. Commands spanning a
report boundary are counted in the window in which they complete. These are
host-observed command latencies, not physical motor-only timings or camera
exposure measurements. Initialization and other commands that replace a pending
tracked operation contribute to `command_superseded`; this count alone is not
proof of a fault.

Timing aggregates use constant-memory histograms over the entire reporting
window, independently of the shorter raw history. The reported p95 is a bucket
upper bound, not an exact percentile. It is null when there are no samples or
the percentile exceeds the largest 10-second bucket; count/max/histogram
distinguish those cases. Completed packet/reader gaps are separate from an
ongoing silence: inspect `last_rx_age_s` and `reader_phase_age_s` too.

Before the long reproduction, run a few minutes with the intended shortest
exposures, Z motion, camera and image-processing workload. Confirm schema 2
telemetry, allow at least one health summary, then run this read-only report from
the repository root:

```powershell
python software/tools/report_mcu_health.py C:\Microscope_Data\logs\main_hcs.log --last 10
```

The command finds the detailed file from the main log automatically. You can also
pass a `health-*.jsonl` file directly; older main logs containing JSON still work.
It summarizes the latest recorder's recent windows, highlighting packet gaps,
retries/discards, Z/other command latencies and firmware timing. It does not open
hardware or issue commands. Compare equivalent workloads when looking for a
timing regression, and inspect acquisition frame timestamps and existing camera
warnings for frame delivery. This report is not an automatic instrument pass/fail
test and does not measure added overhead against an uninstrumented baseline by
itself. A clean short run does not rule out an intermittent failure.

## Binary protocol extension

Packets remain 24 bytes. Positions, command ID/status, buttons, version byte and
CRC retain their offsets. Byte 22 still encodes major/minor `0x15`, so existing
version comparisons continue to work. The patch version is in telemetry page 0.
`software/tools/read_firmware_version.py` understands both legacy zero-filled
reserved bytes and telemetry, and reports `1.5.1` for this image without sending
commands.

Bytes 19–21 carry one page: byte 19 is `0xA0 + page` (0–31), bytes 20–21 are a
big-endian 16-bit word. Page 0 captures and freezes the snapshot, then one word
is included in each ordinary status packet. A whole cycle normally takes about
320 ms. There are no extra USB packets or heartbeat/command-ID changes. Three
additional non-destructive Z register reads occur once per cycle; diagnostic code
does not read clear-on-read `EVENTS`, write driver registers, or alter motion flags.

The decoder requires all pages in order, a stable major/minor byte, supported schema 1 or 2,
matching sequence bookends, and a valid CRC on every page. It discards incomplete
cycles and waits for the next page 0. Legacy zero-CRC acceptance in the normal
status parser never qualifies a corrupt telemetry page as valid.

| Word/page | Meaning |
|---|---|
| 0 | High byte: schema 2 (earlier image: 1); low byte: firmware patch 1 |
| 1, 31 | Matching 16-bit snapshot sequence, wrapping |
| 2–3 | MCU uptime, ms |
| 4–5 | Main-loop count |
| 6–7 | Maximum main-loop entry gap, µs, since previous snapshot (schema 1: since boot) |
| 8–9 | Valid received command count, including heartbeats/retries |
| 10–11 | Z commanded target, signed microsteps |
| 12–13 | Z TMC4361A actual position, signed microsteps |
| 14–15 | Focus-control target, signed microsteps |
| 16–17 | Z TMC4361A STATUS register |
| 18–19 | Z PID error register, signed |
| 20 | Flags: bit 0 global busy; 1 X; 2 Y; 3 Z; 4 W; 5 W2; 6 Z homing; 7 Z preparing; 8 Z PID enabled; 9 watchdog enabled |
| 21 | Two bits per driver, X/Y/Z/W/W2 order: 0 unknown, 1 TMC2660, 2 TMC2240 |
| 22 | Last Z move command: high byte ID, low byte opcode |
| 23 | Last Z move return code, signed 16-bit |
| 24–25 | Age of last Z move command, ms |
| 26–27 | Z target tolerance |
| 28–29 | Received command CRC-error count |
| 30 | Maximum status-generation gap since previous snapshot, ms, saturated at 65,535 (schema 1: since boot) |

32-bit values use the high word first. Z commands here mean MOVE_Z/MOVETO_Z;
homing and focus-wheel movement are represented by flags/targets, not as new Z
command IDs. Return-code sentinels: -32768 no Z command yet; -32767 callback not
returned; -32766 unidentified driver rejection. Otherwise it is the unmodified
`tmc4361A_moveTo()` return value. Completion-blocker labels in the client are an
interpretation of the sampled conditions, not a new firmware error code.

## Verification

Python tests: `tests/control/test_mcu_diagnostics.py` covers packet loss, corruption,
sequence rollover, bounded history, timeout/thread snapshots, full-window health
aggregation, independent monitoring, health-report parsing and passive version reading.
Run it with existing MCU/protocol/watchdog tests. Native firmware tests include
`test_telemetry`, including interval reset and timestamp rollover; build the Teensy image as well. Hardware timing and physical
movement still require an instrument run.
