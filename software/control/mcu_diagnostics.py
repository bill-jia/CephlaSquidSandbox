"""Bounded, passive MCU flight recorder and firmware 1.5.1 telemetry decoder.

No serial IO or disk IO occurs here. Times are host monotonic seconds, paired
with wall time at construction. Firmware words are a coherent paged snapshot,
not live values at the time the last page arrives.
"""

from collections import Counter, deque
import os
import sys
import threading
import time


TELEMETRY_PAGE_BASE = 160
TELEMETRY_PAGE_COUNT = 32
TELEMETRY_SCHEMA = 2
SUPPORTED_TELEMETRY_SCHEMAS = (1, 2)


class TimingStats:
    """Constant-memory histogram; percentile values are bucket upper bounds."""

    BOUNDS_MS = (1, 5, 10, 20, 50, 100, 300, 500, 1000, 3000, 10000)

    def __init__(self):
        self.count = 0
        self.total_ms = 0.0
        self.max_ms = 0.0
        self.buckets = [0] * (len(self.BOUNDS_MS) + 1)

    def observe(self, seconds):
        milliseconds = max(0.0, seconds * 1000)
        self.count += 1
        self.total_ms += milliseconds
        self.max_ms = max(self.max_ms, milliseconds)
        index = 0
        while index < len(self.BOUNDS_MS) and milliseconds > self.BOUNDS_MS[index]:
            index += 1
        self.buckets[index] += 1

    def summary(self):
        upper = None
        cumulative = 0
        for index, count in enumerate(self.buckets):
            cumulative += count
            if self.count and cumulative >= self.count * 0.95:
                upper = self.BOUNDS_MS[index] if index < len(self.BOUNDS_MS) else None
                break
        return {
            "count": self.count,
            "mean_ms": self.total_ms / self.count if self.count else None,
            "max_ms": self.max_ms if self.count else None,
            "p95_upper_bound_ms": upper,
            "histogram_bounds_ms": list(self.BOUNDS_MS),
            "histogram_counts": list(self.buckets),
        }


def capture_thread_stacks(max_depth=64):
    """Capture Python locations only: no locals, source-file reads or serial IO.

    Python must be able to run this function. It cannot diagnose a permanently
    GIL-blocked process or provide native C/driver stacks.
    """
    threads = {thread.ident: thread for thread in threading.enumerate()}
    frames = sys._current_frames()
    result = []
    frame = None
    try:
        for ident, frame in frames.items():
            thread = threads.get(ident)
            stack = []
            while frame is not None and len(stack) < max_depth:
                stack.append({"file": frame.f_code.co_filename, "line": frame.f_lineno,
                              "function": frame.f_code.co_name})
                frame = frame.f_back
            result.append({
                "python_ident": ident,
                "native_id": getattr(thread, "native_id", None),
                "name": thread.name if thread else "unregistered",
                "truncated": frame is not None,
                "frames_innermost_first": stack,
            })
    finally:
        # Do not retain frame objects (and their local variables) in the recorder.
        frames.clear()
        del frame
    return result


def decode_telemetry(words, version):
    def u32(index):
        return (words[index] << 16) | words[index + 1]

    def i32(index):
        value = u32(index)
        return value - (1 << 32) if value & (1 << 31) else value

    flags = words[20]
    status = u32(16)
    target, actual, pid_error = i32(10), i32(12), i32(18)
    tolerance = u32(26)
    blockers = []
    if flags & (1 << 3):
        if actual != target:
            blockers.append("z_position_not_at_target")
        if flags & ((1 << 6) | (1 << 7)):
            blockers.append("z_homing_or_preparing")
        if not status & 1:
            blockers.append("z_target_reached_flag_clear")
        if status & 0x78:
            blockers.append("z_velocity_or_ramp_active")
        if flags & (1 << 8) and abs(pid_error) >= tolerance:
            blockers.append("z_pid_error_outside_tolerance")
    for axis, bit in (("x", 1), ("y", 2), ("w", 4), ("w2", 5)):
        if flags & (1 << bit):
            blockers.append(axis + "_busy")
    result = words[23]
    return {
        "firmware": f"{version >> 4}.{version & 15}.{words[0] & 255}",
        "schema": words[0] >> 8,
        "timing_scope": "since_boot" if words[0] >> 8 == 1 else "since_previous_snapshot",
        "sequence": words[1],
        "uptime_ms": u32(2),
        "loop_count": u32(4),
        "max_loop_gap_us": u32(6),
        "valid_commands": u32(8),
        "z_target_usteps": target,
        "z_actual_usteps": actual,
        "focus_target_usteps": i32(14),
        "z_status_register": status,
        "z_pid_error": pid_error,
        "flags": flags,
        "drivers": {axis: (words[21] >> (2 * i)) & 3 for i, axis in enumerate(("x", "y", "z", "w", "w2"))},
        "last_z_command_id": words[22] >> 8,
        "last_z_opcode": words[22] & 255,
        "last_z_move_result": result - 65536 if result & 32768 else result,
        "last_z_command_age_ms": u32(24),
        "z_target_tolerance": tolerance,
        "command_crc_errors": u32(28),
        "max_status_gap_ms_saturated": words[30],
        "z_physical_limits": (status >> 7) & 3,
        "z_virtual_limits": (status >> 9) & 3,
        "completion_blockers_at_sample": blockers,
    }


class MCUDiagnostics:
    """Keep ~20 seconds of 100 Hz RX plus independent command/event history."""

    def __init__(self, event_limit=1024, rx_limit=2048, telemetry_limit=64):
        self._lock = threading.Lock()
        self.events = deque(maxlen=event_limit)
        self.rx = deque(maxlen=rx_limit)
        self.telemetry = deque(maxlen=telemetry_limit)
        self.started = time.monotonic()
        self.wall_time = time.time()
        self.recorder_id = f"{os.getpid()}-{time.monotonic_ns()}"
        self.counts = Counter()
        self.last_rx = None
        self.max_rx_gap_s = 0.0
        self.reader_tick = self.started
        self.reader_phase = "starting"
        self.backlog = 0
        self.max_backlog = 0
        self.max_reader_gap_s = 0.0
        self._pages = []
        self._page_started = None
        self._page_version = None
        self._health_started = self.started
        self._health_counts = Counter()
        self._health_rx_gaps = TimingStats()
        self._health_command_times = {}
        self._health_write_times = TimingStats()
        self._health_max_reader_gap = 0.0
        self._health_max_backlog = 0
        self._health_fw_maxima = {}
        self._pending_command = None
        self._stall_gaps = deque(maxlen=32)

    def event(self, kind, **fields):
        now = time.monotonic()
        with self._lock:
            self.counts[kind] += 1
            self.events.append(dict(t=now, kind=kind, thread=threading.get_native_id(), **fields))
            if kind == "tx_start" and fields.get("tx_kind") == "command":
                if self._pending_command is not None:
                    self.counts["command_superseded"] += 1
                self._pending_command = (fields["command_sequence"], fields["opcode"], now)
            elif kind in ("tx_end", "tx_error"):
                self._health_write_times.observe(fields["duration_s"])
            elif kind in ("command_complete", "command_aborted"):
                pending = self._pending_command
                if pending is not None and pending[0] == fields.get("host_sequence"):
                    if kind == "command_complete":
                        stats = self._health_command_times.setdefault(pending[1], TimingStats())
                        stats.observe(now - pending[2])
                    self._pending_command = None
            elif kind == "buffer_reset" and fields.get("success"):
                self.counts["buffer_bytes_discarded"] += fields["bytes_discarded"]
            if kind == "tx_start":
                self.counts["tx_" + fields["tx_kind"]] += 1

    def reader_poll(self, phase, backlog=None):
        now = time.monotonic()
        with self._lock:
            gap = now - self.reader_tick
            self.max_reader_gap_s = max(self.max_reader_gap_s, gap)
            self._health_max_reader_gap = max(self._health_max_reader_gap, gap)
            if gap > 0.1:
                event = dict(t=now, kind="reader_gap", seconds=gap, previous_phase=self.reader_phase)
                self.events.append(event)
                self._stall_gaps.append(event)
                self.counts["reader_gap"] += 1
            self.reader_tick = now
            self.reader_phase = phase
            if backlog is not None:
                self.backlog = backlog
                self.max_backlog = max(self.max_backlog, backlog)
                self._health_max_backlog = max(self._health_max_backlog, backlog)

    def received(self, packet, crc_valid, pending_id, host_sequence):
        """Called before acquiring command lock, so RX vs tracking stalls differ."""
        now = time.monotonic()
        packet = bytes(packet)
        with self._lock:
            gap = None if self.last_rx is None else now - self.last_rx
            self.last_rx = now
            self.max_rx_gap_s = max(self.max_rx_gap_s, gap or 0)
            if gap is not None:
                self._health_rx_gaps.observe(gap)
                if gap > 0.1:
                    self._stall_gaps.append(dict(t=now, kind="rx_gap", seconds=gap))
            self.counts["rx"] += 1
            if not crc_valid:
                self.counts["legacy_zero_crc_accepted"] += 1
            self.rx.append(dict(t=now, raw=packet.hex(), crc_valid=crc_valid, gap_s=gap,
                                pending_id=pending_id, host_sequence=host_sequence))
            page = packet[19] - TELEMETRY_PAGE_BASE
            if not crc_valid or not 0 <= page < TELEMETRY_PAGE_COUNT:
                self._pages = []
                return None
            word = (packet[20] << 8) | packet[21]
            if page == 0:
                self._pages = []
                self._page_started = now
                self._page_version = packet[22]
            if page != len(self._pages) or packet[22] != self._page_version:
                self.counts["telemetry_incomplete"] += 1
                self._pages = []
                return None
            self._pages.append(word)
            if page != TELEMETRY_PAGE_COUNT - 1:
                return None
            words, self._pages = self._pages, []
            if words[0] >> 8 not in SUPPORTED_TELEMETRY_SCHEMAS or words[1] != words[31]:
                self.counts["telemetry_invalid"] += 1
                return None
            result = decode_telemetry(words, packet[22])
            result.update(t=now, first_page_t=self._page_started, assembly_s=now - self._page_started)
            self.telemetry.append(result)
            scope = result["timing_scope"]
            maxima = self._health_fw_maxima.setdefault(scope, {"max_loop_gap_us": 0, "max_status_gap_ms": 0})
            maxima["max_loop_gap_us"] = max(maxima["max_loop_gap_us"], result["max_loop_gap_us"])
            maxima["max_status_gap_ms"] = max(maxima["max_status_gap_ms"], result["max_status_gap_ms_saturated"])
            self.counts["telemetry"] += 1
            return result if self.counts["telemetry"] == 1 else None

    def stall_evidence(self, threshold_s, after):
        """Cheap cached observations, without resetting minute aggregates.

        Completed gaps let the monitor observe a stall even if it could not run
        until the reader recovered. Such stacks describe recovery, not the past.
        """
        now = time.monotonic()
        with self._lock:
            phase_age = now - self.reader_tick
            rx_age = now - (self.last_rx if self.last_rx is not None else self.started)
            gaps = [gap for gap in self._stall_gaps if gap["t"] > after and gap["seconds"] >= threshold_s]
            ongoing = phase_age >= threshold_s or rx_age >= threshold_s
            if not ongoing and not gaps:
                return None
            return {
                "observed_monotonic_s": now, "threshold_s": threshold_s,
                "ongoing_at_observation": ongoing,
                "reader_phase": self.reader_phase, "reader_phase_age_s": phase_age,
                "last_rx_age_s": None if self.last_rx is None else rx_age,
                "no_rx_since_start_s": rx_age if self.last_rx is None else None,
                "last_buffer_bytes": self.backlog, "recent_gaps": gaps,
            }

    def health_summary(self):
        """Take/reset minute aggregates; preserve the flight-recorder histories.

        Called by a separate monitor thread so a stuck reader still has its
        last-known state reported. No command lock or serial queries are used.
        """
        now = time.monotonic()
        with self._lock:
            sample = self.telemetry[-1] if self.telemetry else None
            rx_age = None if self.last_rx is None else now - self.last_rx
            telemetry_age = None if sample is None else now - sample["first_page_t"]
            state = "not_received" if sample is None else "fresh" if telemetry_age <= 1.5 else "stale"
            delta = {key: value - self._health_counts[key] for key, value in self.counts.items()}
            summary = {
                "format": 1, "recorder_id": self.recorder_id,
                "unix_s": time.time(), "monotonic_s": now, "window_s": now - self._health_started,
                "counts": delta, "total_counts": dict(self.counts),
                "rx_gap": self._health_rx_gaps.summary(),
                "last_rx_age_s": rx_age,
                "reader_phase": self.reader_phase, "reader_phase_age_s": now - self.reader_tick,
                "max_reader_gap_s": self._health_max_reader_gap,
                "last_buffer_bytes": self.backlog, "max_buffer_bytes": self._health_max_backlog,
                "command_completion": {opcode: stats.summary() for opcode, stats in self._health_command_times.items()},
                "serial_write": self._health_write_times.summary(),
                "telemetry_state": state, "telemetry_age_s": telemetry_age,
                "latest_telemetry": sample, "firmware_sample_maxima": self._health_fw_maxima,
            }
            self._health_started = now
            self._health_counts = self.counts.copy()
            self._health_rx_gaps = TimingStats()
            self._health_command_times = {}
            self._health_write_times = TimingStats()
            self._health_max_reader_gap = 0.0
            self._health_max_backlog = 0
            self._health_fw_maxima = {}
            return summary

    def snapshot(self):
        now = time.monotonic()
        with self._lock:
            return {
                "format": 1, "recorder_id": self.recorder_id, "captured_monotonic_s": now,
                "clock_anchor": {"monotonic_s": self.started, "unix_s": self.wall_time},
                "counts": dict(self.counts),
                "last_rx_age_s": None if self.last_rx is None else now - self.last_rx,
                "max_rx_gap_s": self.max_rx_gap_s,
                "reader_phase": self.reader_phase, "reader_phase_age_s": now - self.reader_tick,
                "max_reader_gap_s": self.max_reader_gap_s,
                "last_buffer_bytes": self.backlog, "max_buffer_bytes": self.max_backlog,
                "telemetry_age_s": None if not self.telemetry else now - self.telemetry[-1]["first_page_t"],
                "telemetry_partial_pages": len(self._pages),
                "limits": {"events": self.events.maxlen, "rx": self.rx.maxlen, "telemetry": self.telemetry.maxlen},
                "events": list(self.events), "rx": list(self.rx), "telemetry": list(self.telemetry),
            }
