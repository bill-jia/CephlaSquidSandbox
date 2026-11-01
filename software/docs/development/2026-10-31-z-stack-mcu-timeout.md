# Investigation: Z-stack MCU timeouts on October 31, 2026

Written November 1, 2026. This report separates evidence from the failed runs
from hypotheses and from changes made afterward. The root cause remains
unconfirmed; the subsequent firmware/client changes have not demonstrated a fix
for these particular failures.

## Assessment

Two acquisitions aborted because the client did not observe MCU command
completion within its three-second movement timeout. Both failures occurred in
the motorized Z-stack path, apparently on the first 1.5 µm step after plane 0 of
a new brightfield stack.

The strongest recorded symptom is delayed processing of MCU status packets:
the session contains 1,443 stale-read warnings, including three immediately
before the first abort. This supports investigating the communication and
completion-tracking path first. It does not identify whether the delay originated
in the client, Windows/USB transport, or firmware.

The old client also had a heartbeat/command-tracking weakness, and the firmware
had several motion-completion conditions that could keep its global busy flag
set. Either could contribute. The logs lack the command payload, received status,
and motor-controller state needed to decide between them. The two aborts need
not have the same cause.

## Evidence and provenance

The main session log began on October 30 at 19:34:50 and reported client revision
`fb52d3c9208cf2600bfa3e1678cef2a967cfa08b`, with `dirty=True`. The checkout examined
during the original investigation had the same revision. Uncommitted changes
mean the revision alone is not a complete record of the running source.

The file originally examined as `C:\Microscope_Data\logs\main_hcs.log` has since
rotated. At the time of writing it is `C:\Microscope_Data\logs\main_hcs.log.3`
(91,568,986 bytes). Rotation may change this name again; the timestamps and
messages below identify the incident independently of the filename.

Other evidence came from `main_hcs_worker.log` and these acquisition directories
under `C:\Microscope_Data\Lingling\20261030_FUCCI_254_QPM_LZ`:

- `_2026-10-30_21-02-33.366525`
- `_2026-10-31_11-19-53.135788`

Each contains `acquisition.yaml`, `cycles_manifest.yaml`, and
`acquisition_times.csv`. The original main log did not record the exact flashed
firmware build. The operator subsequently reported updating firmware from 1.1
to 1.5; that is operator-provided context rather than a version proven by the
failure log.

Times below are the local timestamps in the main log, on October 31, 2026
(America/Los_Angeles, UTC−07:00). Frame CSV timestamps are UTC. The numeric
identifier in the main log format is a native thread ID, not a process ID.

## Failure timeline

| Event | First acquisition | Second acquisition |
|---|---|---|
| Start | Oct 30, 21:02:33 | Oct 31, 11:19:53 |
| Timeout | Oct 31, 10:45:49.179 | Oct 31, 17:50:26.393 |
| Elapsed to timeout | About 13 h 43 min | About 6 h 30 min |
| Last recorded input frame | Timepoint 82, HEK293, FOV 31, plane 0 | Timepoint 39, HEK293, FOV 3, plane 0 |
| Last input frame time | 10:45:45.862 | 17:50:23.168 |
| Timeout location in original source | `move_z_for_stack()`, line 5765 | Same |

Timepoint, FOV, and plane indices are zero-based. The final rows are at lines
112,767 and 53,411 of the respective frame-time CSVs.

Both acquisition records specify 15 planes, 0.0015 mm spacing, `FROM CENTER`,
and `use_piezo: false`. The cycle acquires a brightfield Z stack for phase
processing, with fluorescence observations at the reference plane.

The preceding FOV had recorded planes through plane 14. The failing FOV recorded
plane 0 but no subsequent input frame. Together with the stack iteration code,
this indicates a failure during the requested plane-0-to-plane-1 step. The exact
raw motor command, including which backlash submove was executing, was not
logged. The failures occurred at different fields and timepoints, which weakens
the case for one repeatable XY location; it does not exclude a shared Z/mechanical
condition.

The exception path was:

```
MultiPointWorker.move_z_for_stack()
  -> CephlaStage.move_z()
     -> Microcontroller.wait_till_operation_is_completed(timeout_limit_s=3)
        -> TimeoutError
```

Three seconds is the stage client's minimum movement-wait timeout. It is not
the MCU illumination-watchdog timeout, which was configured to five seconds.

The acquisition exception handler caught the error, requested an abort, stopped
camera streaming, and finalized writers. Both datasets were subsequently logged
as sealed with an aborted status. This was a handled acquisition abort, not
evidence of an application-process crash or MCU reboot. Finalization messages
do not mean all planned frames were acquired.

## Communication evidence

Immediately before the first abort, the main log records:

| Local time | Time since last accepted status packet | Last MCU ID | Last client ID |
|---|---:|---:|---:|
| 10:45:46.306 | 0.377 s | 159 | 161 |
| 10:45:48.865 | 0.779 s | 160 | 162 |
| 10:45:49.095 | 1.009 s | 160 | 163 |

These are lines 304,861–304,863 of the original session file. The abort follows
at lines 304,864–304,865. The second abort is at lines 444,046–444,047.

Across this session there were 1,443 stale-read warnings. The largest reported
packet age was 1.735 seconds. The firmware source normally sends status every
10 ms. These warnings measure time since the client accepted a status packet;
they are not direct measurements of USB latency or firmware execution time.
Repeated warnings may describe the same gap, so 1,443 is a warning count, not a
count of independent communication failures.

There was no stale-read warning immediately around the second abort. That does
not prove packets were flowing normally: the warning is checked on selected
paths and periodically, and the timeout handler did not record packet age.

The main session log contained no recorded CRC failures, read-loop exceptions,
or reconnect messages. It contained no DEBUG records, so absence of retry or
mismatched-ACK messages is not evidence that those events never occurred.
Worker-log entries show processing/writing activity around both aborts, without
warnings/errors in the corresponding 10:45 and 17:50 minute windows. This argues
against a simultaneous stop of all worker processes, but does not rule out
starvation or blocking of the main process's MCU reader.

## Possible causes

The order below is an investigation priority, not a numerical probability ranking.

### 1. Delayed host reception or status processing

The stale-read warnings directly support a reception/processing delay. Possible
mechanisms include CPU scheduling pressure, Python GIL contention, a blocking
reader callback or serial operation, and delayed draining of the serial buffer.
The acquisition also performed image processing and writing, but the available
logs do not establish a causal connection between that work and the MCU delay.

The old reader silently reset its input buffer when queued data reached 4,095
bytes. This could remove useful status history after the reader fell behind;
the logs cannot establish whether that branch ran during either failure.

**Discriminating evidence:** host reader-phase gaps, buffer depth/discards and
packet-arrival timestamps, compared with firmware loop counts and timing. A
large host gap with buffered packets and small firmware gaps would favor a
client-side delay.

### 2. USB transport delay or firmware loop stall

The same stale-read symptom could arise if the host had no new packets to read:
for example, USB delivery was delayed or the firmware main loop was blocked.
No recorded reconnect proves a USB disconnection, but transport can stall
without producing a disconnect exception. Conversely, the stale-read warnings
alone do not prove the MCU stopped running.

**Discriminating evidence:** firmware uptime/loop counters and maximum loop/status
generation gaps, combined with the host buffer state. If packets stop entirely,
the last telemetry remains historical; software alone may still be unable to
separate a USB interruption from a firmware hang until communication recovers.

### 3. Original client heartbeat and command-tracking races

In the client examined during the original investigation, the heartbeat called
the ordinary `send_command()` path every 2.5 seconds. This replaced the tracked
command ID, payload, timestamp and retry counter. A stage wait observed one
global busy flag rather than the completion of an immutable move request.
Received-status processing was also not protected by the same command lock as
sending.

That is a concrete code weakness. With delayed packets, a wait could end up
tracking a heartbeat or inconsistent command state instead of the intended
move. The ID differences in the warnings are compatible with several scenarios,
including ordinary queued commands; without TX/RX history they do not prove
that a heartbeat race caused the timeout.

**Status after the update:** the client adaptation in commit `63d85d97` already
changes heartbeats to reuse the application command ID without replacing the
tracked operation, and handles received command status under the command lock.
The new diagnostics preserve those fixes. This hypothesis therefore applies
specifically to the original run, not unchanged to the updated client.

### 4. Firmware motion completion remained busy

Firmware clears the Z motion flag only when the reported motion-controller
position equals its commanded target and the running-state check is satisfied,
outside homing. The running-state check considers target-reached, velocity/ramp
state, and PID error versus tolerance when PID is enabled. Other axes can also
keep the global busy flag set after Z completes.

A driver/configuration issue, an unmet settling condition, a target discrepancy,
or another axis still marked busy could therefore produce a timeout while
status packets continued arriving. Different hardware makes driver type and
configuration worth checking, but the original logs contain no driver verdict
or register evidence. There is no demonstrated motor fault, limit collision,
or mechanical stall in these logs. Without independent feedback, a reported
motion-controller position also cannot prove physical movement.

**Discriminating evidence:** Z target/actual/focus target, STATUS register,
PID error/tolerance, per-axis busy flags, driver identity and last move result.

### 5. A relative move was repeated after a delayed acknowledgment

The client can resend a command when the received command ID fails to match
for more than 0.5 seconds. The examined firmware dispatcher does not suppress
duplicate relative moves by command ID. A command that executed but whose
acknowledgment was delayed could therefore execute another relative displacement
on resend. This could alter the target or prolong motion; it is a potential
secondary effect, not a demonstrated event in these acquisitions.

The retry messages were DEBUG-level and were not retained in the main session
log. **Discriminating evidence:** raw TX history, resend reasons, host sequence
numbers, and firmware command count/last Z command/target. The diagnostic work
does not change retry policy or add firmware duplicate suppression.

## What the later updates establish—and what they do not

The imported firmware 1.5 includes explicit move-error reporting from earlier
versions, filter-wheel and strobe fixes, and per-axis TMC2660/TMC2240 driver
identification. The accompanying client changes fix the heartbeat tracking and
status-locking concerns described above. These are relevant improvements, but
they do not retrospectively identify the cause of the October 31 failures.

Firmware 1.5.1 adds passive telemetry to the three reserved status bytes while
preserving the 24-byte packet and the major/minor version contract. The client
now keeps bounded command/status histories and dumps detailed state on failed
waits, with acquisition-plane and backlash context. It also preserves the full
exception traceback. See [MCU diagnostics](../mcu-diagnostics.md) for the exact
fields, packet layout, startup checks and interpretation limits.

The revised 1.5.1 schema-2 Teensy build succeeded, the native firmware test suite
passed, and 183 client regression tests passed with one existing skip. These checks verify the
implementation and compatibility paths; they are not an instrument reproduction
or proof that the original failure is fixed.

## Remaining diagnostic ambiguities with 1.5.1

The new diagnostics should substantially narrow a recurrence, but do not
guarantee a unique root cause. Their strongest distinction is whether completion
updates were missing/delayed or the firmware was still reporting unfinished
motion, and which sampled condition prevented completion.

| Ambiguity | What the current diagnostics reveal | Additional evidence that may be needed |
|---|---|---|
| USB interruption versus a completely hung MCU | If packets stop entirely, telemetry stops too. The last snapshot is historical. Uptime, loop counts and timing after recovery may help, but cannot always distinguish the two. | USB/OS tracing or independent MCU observation, such as a diagnostic GPIO heartbeat captured outside the USB connection. |
| Why the client reader was delayed | Reader phases, timing gaps, serial backlog and failure-time Python stacks can locate a delay or lock site, but do not conclusively separate CPU scheduling pressure, GIL contention, or blocking inside a native serial/OS call. | Repeated stack sampling, CPU/scheduler profiling, native stacks and serial/OS tracing as indicated by the recorded phase. |
| Mechanical or electrical cause of unfinished motion | Target/controller position, completion flags and PID error show the motion-control state. Driver identity is reported, but the full power-driver fault registers and an independent measurement of physical movement are not. Binding, insufficient torque, missed steps or electrical faults may remain indistinguishable. | Appropriate power-driver fault telemetry, encoder/independent position measurements, or targeted mechanical and electrical checks. |
| Brief firmware events between samples | One coherent firmware snapshot is captured and transmitted over roughly 320 ms. Short-lived conditions can occur and disappear between samples. Host status history has finer timing, but firmware telemetry is not a complete event trace. | A firmware event buffer or latched fault/completion-reason fields, retrieved after a failure. |
| Why an axis other than Z remains busy | Per-axis flags identify which axis holds the global busy status. Detailed target, driver-status and PID telemetry focuses on Z; ordinary XY positions alone do not explain another axis's completion failure. | Equivalent target/status/PID telemetry for the implicated axis. |

Timing evidence also needs careful interpretation. In the revised 1.5.1 image
(telemetry schema 2), maximum-gap fields reset at each snapshot; the initial
schema 1 image reports high-water marks since boot. A completed telemetry cycle
describes the state captured at its first page. Even an interval maximum is not
automatically attributable to the failing move, and queued USB data can delay
delivery of an otherwise valid snapshot.

A successful acquisition after these updates would not establish the original
cause. Firmware and client behavior changed together, including the heartbeat
tracking fix, so success alone cannot identify which change helped. Similarly,
the bounded histories and sampled telemetry can show a resend and a changed
target without necessarily reconstructing every intervening firmware action.

Before reproduction, diagnostics were extended with Python thread stacks on
failed waits, independent INFO-level health summaries every minute, and firmware
interval maxima. The summaries preserve packet/command timing and error trends
beyond the bounded raw-history window and can report a stuck reader. They cannot
run during a permanent process/GIL hang, and failure-time Python stacks do not
expose the internals of native drivers. The read-only `report_mcu_health.py` tool
reads the full per-session health records in the `mcu_diagnostics` folder next to
the main log; the main log now contains concise `[MCU-HEALTH]` lines and the full
file's location. Preserve both when sharing a failure. The tool
summarizes these records for a short representative hardware check; see the
[short-run procedure](../mcu-diagnostics.md#periodic-health-summaries-and-short-run-timing-report).

## Next acquisition

Use the updated client and controller 1.5.1, and confirm an INFO log entry saying
`Firmware telemetry active` with firmware `1.5.1` and schema `2` before starting the long run.
The recorder is automatic and does not require `--verbose`.

Keep the normal Z, camera and processing workload for a representative run.
If a timeout recurs, preserve the entire session log and its `[MCU-DIAG]` dump,
the worker log, and the acquisition metadata/frame-time files before rotation.
The first questions will be whether status was still arriving, whether its
command ID matched, and which firmware completion conditions remained unmet.

Increasing the timeout alone would not answer those questions. A successful
run after the updates would be encouraging, but would not by itself distinguish
which change helped or exclude an intermittent hardware/transport problem.
