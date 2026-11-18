"""Summarize persisted MCU health records without opening hardware.

Example (repository root):
    python software/tools/report_mcu_health.py C:\\Microscope_Data\\logs\\main_hcs.log --last 10

Use a few minutes of representative acquisition first. This reports observed
timing, not an automatic pass/fail verdict on instrument performance.
"""

import argparse
from collections import Counter, deque
import json
from pathlib import Path


def read_health(path, last=10):
    rows = deque(maxlen=last)
    malformed = 0
    details_path = None
    with Path(path).open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            details_marker = "[MCU-DIAG] health details: "
            if details_marker in line:
                try:
                    details_path, _ = json.JSONDecoder().raw_decode(line.split(details_marker, 1)[1])
                except ValueError:
                    malformed += 1
                continue
            marker = "[MCU-DIAG] health "
            if marker in line:
                payload = line.split(marker, 1)[1]
            elif line.startswith("{"):
                payload = line
            else:
                continue
            try:
                # Logging appends filename/line after the JSON object.
                row, _ = json.JSONDecoder().raw_decode(payload)
                if row.get("format") != 1 or "recorder_id" not in row:
                    raise ValueError("Unsupported health record")
                rows.append(row)
            except (ValueError, AttributeError):
                malformed += 1
    if isinstance(details_path, str):
        details = Path(details_path)
        # Also support a main log and its diagnostics folder copied together.
        local = Path(path).parent / "mcu_diagnostics" / details.name
        if local.is_file():
            details = local
        if details.is_file() and details.resolve() != Path(path).resolve():
            # JSONL files contain records only, never references to other logs.
            extra = deque(maxlen=last)
            with details.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                        if row.get("format") != 1 or "recorder_id" not in row:
                            raise ValueError("Unsupported health record")
                        extra.append(row)
                    except (ValueError, AttributeError):
                        malformed += 1
            # Include fallback main-log records if file writing failed mid-run.
            # A coarse monotonic clock can give distinct records the same time.
            combined = {json.dumps(r, sort_keys=True): r for r in [*rows, *extra]}
            rows = sorted(combined.values(), key=lambda r: (r["monotonic_s"], r["unix_s"]))[-last:]
        elif not rows:
            raise FileNotFoundError(f"Health details file unavailable: {details}. Copy mcu_diagnostics with the main log.")
    return list(rows), malformed


def merged_timing(summaries):
    summaries = [row for row in summaries if row.get("count", 0)]
    count = sum(row["count"] for row in summaries)
    if not count:
        return "no samples"
    mean = sum(row["mean_ms"] * row["count"] for row in summaries) / count
    maximum = max(row["max_ms"] for row in summaries)
    return f"n={count}, mean={mean:.3f} ms, max={maximum:.3f} ms"


def render_report(rows, malformed=0):
    if not rows:
        return "No MCU health records found. Allow at least one minute of the updated client to run."
    # Never silently merge different processes/controllers or app restarts.
    latest = rows[-1]
    recorder = latest["recorder_id"]
    rows = [row for row in rows if row["recorder_id"] == recorder]
    counts = Counter()
    for row in rows:
        counts.update(row["counts"])
    sample = latest.get("latest_telemetry") or {}
    lines = [
        f"MCU recorder: {recorder}",
        f"Windows: {len(rows)} covering {sum(row['window_s'] for row in rows):.1f} s",
        f"Firmware: {sample.get('firmware', 'unavailable')}; schema: {sample.get('schema', 'unavailable')}",
        f"Telemetry: {latest['telemetry_state']}; age={latest['telemetry_age_s']} s",
        f"Last RX age: {latest['last_rx_age_s']} s; reader alive: {latest.get('state', {}).get('reader_alive', 'unknown')}",
        f"RX packets: {counts['rx']}; packet gaps: {merged_timing(row['rx_gap'] for row in rows)}",
        f"Retries: {counts['resend']}; failed waits: {counts['wait_failed']}; CRC resync failures: {counts['crc_failure']}",
        f"Buffer discards: {counts['buffer_reset']} attempts / {counts['buffer_bytes_discarded']} bytes",
        f"Largest observed buffer: {max(row['max_buffer_bytes'] for row in rows)} bytes",
        f"Serial writes: {merged_timing(row['serial_write'] for row in rows)}",
        "Completed command latency (host-observed; includes serial/scheduling delays and retries):",
    ]
    opcodes = sorted({opcode for row in rows for opcode in row["command_completion"]})
    for opcode in opcodes:
        timings = (row["command_completion"].get(opcode, {}) for row in rows)
        lines.append(f"  {opcode}: {merged_timing(timings)}")
    if not opcodes:
        lines.append("  no completed commands in these windows")
    for scope in sorted({scope for row in rows for scope in row["firmware_sample_maxima"]}):
        maxima = [row["firmware_sample_maxima"][scope] for row in rows if scope in row["firmware_sample_maxima"]]
        lines.append(
            f"Firmware timing ({scope}): loop max={max(row['max_loop_gap_us'] for row in maxima)} us, "
            f"status-generation max={max(row['max_status_gap_ms'] for row in maxima)} ms"
        )
    lines.append("Compare equivalent exposure, Z and processing workloads; a clean short run does not rule out a rare fault.")
    if malformed:
        lines.append(f"Ignored {malformed} malformed/unsupported health records.")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--last", type=int, default=10, help="Use the last N health records (default 10)")
    args = parser.parse_args()
    if args.last < 1:
        parser.error("--last must be positive")
    try:
        rows, malformed = read_health(args.log, args.last)
    except OSError as exc:
        parser.exit(1, f"Could not read log: {exc}\n")
    print(render_report(rows, malformed))
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
