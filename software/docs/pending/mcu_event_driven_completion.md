# MCU Firmware: Event-Driven Completion Packets

**Task**: eliminate the ~10 ms mean / ~20 ms max of pure reporting latency that
every MCU command carries, by making the Teensy send a status packet the moment
a command completes instead of waiting for the next 10 ms periodic packet.

**Status**: planned, not started. This document is a self-contained handoff —
it carries everything needed to implement without other context. Verified
line-by-line against the repo on 2026-07-18 (branch `worktree-af-bugfix-batch`,
which only touches host-side Python; the firmware tree is identical on
`worktree-zarr-upload-debug` and master). Provenance: "Event-driven MCU
completion packets" proposal, rated **strong** by adversarial review — see
`docs/pending/autofocus_improvement_proposals.md` Tier 1 #2.

**Host-side changes required: none.** This is firmware-only.

---

## Why (measured)

Command completion is only ever learned from the periodic status packet, and
motion-done detection itself runs on a 10 ms poll. So every command pays a
uniform 0–20 ms (mean ~10 ms) reporting tax on top of its real execution time:

- Laser-AF events measured at 228 ms mean contain 4 AF-laser toggles at
  ~11.5 ms each (46 ms) plus 1–5 move completions — **~45–65 ms (20–28 %) of
  every laser-AF event is packet-cadence wait**
  (`docs/pending/laser_autofocus_speedup.md` has the timer breakdown).
- Illumination/pin commands are *atomic* in firmware (they complete inside the
  command callback), so their **entire** measured ~10 ms cost is cadence wait.
- Fleet-wide: every capture's illumination on/off pair and every XY/Z move in
  every acquisition pays the same tax — thousands of commands per plate.
  A 864-FOV laser-AF seed scan carries ~35–45 s of pure cadence wait.

## How completion reporting works today

Firmware (`firmware/controller/`):

- `main_controller_teensy41.ino:38-64` — `loop()` order is
  `process_serial_message()` → `send_position_update()` → `check_position()`.
- `src/serial_communication.cpp:3-38` — `process_serial_message` dispatches the
  command callback. Long-running commands set `mcu_cmd_execution_in_progress`;
  atomic commands (all of `src/commands/light_commands.cpp:5-8`, incl. the AF
  laser's `SET_PIN_LEVEL` → `callback_set_pin_level`,
  `src/commands/commands.cpp:109-114`) never set it — they are complete when
  the callback returns.
- `src/serial_communication.cpp:40-108` — `send_position_update` transmits the
  24-byte status packet, gated on `us_since_last_pos_update >=
  interval_send_pos_update` (**10 000 µs**, `src/constants.h:110`).
- `src/operations.cpp:494-529` — `check_position` detects motion completion
  (TMC4361A position compare AND `!isRunning`) and clears
  `mcu_cmd_execution_in_progress`, gated on `interval_check_position`
  (**10 000 µs**, `src/constants.h:111`). So a finished move waits up to 10 ms
  to be *detected*, then up to 10 ms more to be *reported*.
- Other sites that clear `mcu_cmd_execution_in_progress`:
  `check_limits` (`src/operations.cpp:531-568`), the `finalize_homing_*`
  family (`src/operations.cpp:331-406`), and immediate-command handlers (e.g.
  `callback_reset`, `src/commands/commands.cpp:272`).
- `src/globals.cpp:99` — `flag_send_pos_update` is **vestigial**: declared, set
  only in commented-out legacy `.ino` code. Free to repurpose or delete.

Host (`software/control/microcontroller.py`) — why zero changes are needed:

- The read loop polls serial at 0.1 ms (`:1665`) and notifies a condition
  variable per received packet (`:1798-1799`).
- `wait_till_operation_is_completed` blocks on that CV (`:1841-1857`) — it
  reacts to *whenever* a packet arrives; earlier packets simply release it
  earlier.
- Packet handling is idempotent (`:1716-1759`): duplicate / out-of-cadence
  packets are harmless, and the 0.5 s command-resend timeout (`:636`) only
  gets safer with faster acks.

## The change

Three firmware edits, all sending from **main-loop context** (never an ISR —
avoids racing the TX buffer):

**(a) Ack every command immediately.** In `process_serial_message`, after the
command callback returns, force the next `send_position_update` to fire:
simplest correct mechanism is `us_since_last_pos_update =
interval_send_pos_update + 1` (an `elapsedMicros` — assignment is supported)
and let the existing loop-ordered call transmit it in the same `loop()` pass.
Atomic commands then ack in ~wire time. Do **not** add a second TX path.

**(b) Report busy→idle transitions immediately.** Add a central helper, e.g.
`clear_busy_and_flag_send()` (or repurpose the vestigial
`flag_send_pos_update`), that clears `mcu_cmd_execution_in_progress` AND forces
the packet, and use it at **every** clearing site: `check_position`,
`check_limits`, and all `finalize_homing_*` variants. Note
`callback_reset`-style immediate handlers are already covered by (a) — do not
double-handle them. Any site you miss degrades gracefully to the 10 ms
heartbeat, but the helper makes the invariant self-maintaining.

**(c) Detect motion-done faster.** Drop `interval_check_position` from
10 000 µs to 1 000 µs. The check is a cheap TMC4361A SPI position compare, and
false early completion is impossible: it requires exact target position AND
`!isRunning`. This removes the *detection* half of the stacked 10+10 ms.

**Keep** `interval_send_pos_update = 10 000 µs` as the periodic heartbeat —
position streaming during motion and the host's stale-read watchdog depend on
its cadence. The change only *adds* event-driven packets.

Wire-time reality check: the link is native USB CDC (SerialUSB, 480 Mbps), not
a 2 Mbaud UART — packet serialization is negligible; Windows-side USB latency
is ~0.1–1 ms. Post-change command ack latency should land around 1 ms
(bounded by (c) for moves, by USB scheduling for atomic commands).

## Expected impact (verifier-recomputed)

| Where | Saving |
|---|---|
| Per atomic MCU command (illumination, pin, AF laser) | ~5–10 ms |
| Per motion command (moves are 1–2 MCU commands; down-Z is 2 due to backlash comp) | ~10 ms mean, ~20 ms max, per command |
| Per laser-AF event (228 ms mean today) | ~45–65 ms (20–28 %) |
| 864-FOV laser-AF seed scan | ~35–45 s |
| Ordinary acquisitions (every capture's illumination pair + every move) | tens of seconds per plate |
| Contrast-AF event (~13 moves + toggles) | ~250–350 ms |

## Validation plan

1. **Bench first, not on a rig** (and never flash/test while an acquisition is
   running — the MCU has a single-owner invariant; a second opener breaks the
   running session).
2. `software/tools/microcontroller_stress_test.py` against the flashed Teensy:
   confirm command/ack round-trips, exercise the resend path, and confirm the
   host tolerates the higher packet rate (it polls at 0.1 ms, so it will).
3. Measure ack latency before/after: time `wait_till_operation_is_completed`
   around a no-op-distance move and around `set_pin_level` — expect ~10 ms
   mean → ~1 ms.
4. Homing regression: run a full home_xyz (the `finalize_homing_*` clearing
   sites are the least-exercised path).
5. On-rig: one instrumented multipoint run; the laser-AF `af:*` timers
   (attached automatically during multipoint) should show `move_to_target`
   dropping by ~45–65 ms per event with unchanged success rates in
   `autofocus_log.csv`.

## Risks & notes

- **Operational cost is the real cost**: every rig's Teensy must be reflashed.
  Coordinate with rig schedules.
- Firmware sends stay in `loop()` context — (a) and (b) as specified never
  transmit from an interrupt.
- The 20 ms `SCAN_STABILIZATION_TIME_MS_Z` settle and all host-side timing are
  intentionally unchanged; this change must not be bundled with settle-time
  tuning (separate experiment, separate rollback).
- Divergence guard / cross-correlation verify / multipoint fallbacks only ever
  see *faster* completions — no interaction.
- If packet rate on the wire matters for debugging, note a burst of commands
  now produces a packet per command instead of coalescing — the host is
  idempotent, but any third-party serial sniffer scripts may need adjusting.

## Non-goals

- Do not change the packet format, command protocol, or `cmd_id` semantics —
  the single-in-flight-command model stays (other pending ideas that need
  multi-in-flight, like mid-move trigger bursts, are explicitly out of scope).
- Do not lower the 10 ms heartbeat interval.
- No host-side edits. If you find yourself editing `microcontroller.py`, the
  firmware change is off-plan.
