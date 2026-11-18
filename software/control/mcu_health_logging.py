"""Human-readable health lines and lossless, per-recorder JSONL storage."""

import json
from pathlib import Path


def format_health(summary):
    counts = summary["counts"]

    def milliseconds(value):
        return "unavailable" if value is None else f"{value:.1f} ms"

    reader = summary.get("state", {}).get("reader_alive")
    reader_text = "running" if reader else "STOPPED" if reader is False else "unknown"
    age = summary["last_rx_age_s"]
    last_rx = "never" if age is None else f"{age:.2f} s ago"
    telemetry = summary["telemetry_state"].replace("_", " ")
    if summary["telemetry_age_s"] is not None:
        telemetry += f" ({summary['telemetry_age_s']:.2f} s old)"
    text = (
        f"[MCU-HEALTH] {'Final ' if summary.get('final') else ''}last {summary['window_s']:.0f} s: "
        f"{counts.get('rx', 0)} packets, longest gap {milliseconds(summary['rx_gap']['max_ms'])}; "
        f"retries {counts.get('resend', 0)}, failed waits {counts.get('wait_failed', 0)}, "
        f"CRC errors {counts.get('crc_failure', 0)}, discarded {counts.get('buffer_bytes_discarded', 0)} bytes; "
        f"reader {reader_text}, last packet {last_rx}; telemetry {telemetry}"
    )
    z = summary["command_completion"].get("MOVE_Z")
    if z and z["count"]:
        text += f"; Z completion max {milliseconds(z['max_ms'])}"
    return text


class HealthRecordWriter:
    """Write only from the health monitor, outside command/reader locks.

    Each append is closed/flushed; a separate file per recorder prevents mixing
    sessions and lets users archive the evidence with the main log.
    """

    def __init__(self, directory, recorder_id):
        self.path = Path(directory).resolve() / "mcu_diagnostics" / f"health-{recorder_id}.jsonl"
        self.stall_path = self.path.with_name(f"stalls-{recorder_id}.jsonl")
        self._failed = False

    def write(self, summary, logger):
        payload = json.dumps(summary, default=str)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(payload + "\n")
            if self._failed:
                logger.info("[MCU-DIAG] Detailed health file writing recovered: %s", self.path)
            self._failed = False
        except OSError as exc:
            if not self._failed:
                logger.warning(
                    "[MCU-DIAG] Cannot write health details to %s: %s. "
                    "Preserving full records in the main log until writing recovers.", self.path, exc,
                )
            self._failed = True
            logger.info("[MCU-DIAG] health %s", payload)
        logger.info("%s", format_health(summary))

    def write_stall(self, snapshot, logger):
        """Keep large early-stall histories/stacks out of the normal log."""
        payload = json.dumps(snapshot, default=str)
        try:
            self.stall_path.parent.mkdir(parents=True, exist_ok=True)
            with self.stall_path.open("a", encoding="utf-8") as stream:
                stream.write(payload + "\n")
            location = str(self.stall_path)
        except OSError as exc:
            logger.warning("[MCU-DIAG] Cannot write stall file %s: %s", self.stall_path, exc)
            logger.info("[MCU-DIAG] reader stall details %s", payload)
            location = "main log (detailed file writing failed)"
        evidence = snapshot["evidence"]
        longest = max(
            [evidence["reader_phase_age_s"], evidence["last_rx_age_s"] or 0,
             evidence["no_rx_since_start_s"] or 0]
            + [gap["seconds"] for gap in evidence["recent_gaps"]]
        )
        logger.warning(
            "[MCU-DIAG] Reader delay %.0f ms (%s); observed phase=%s, cached buffer=%s bytes; details: %s",
            longest * 1000, "ongoing" if evidence["ongoing_at_observation"] else "recovered",
            evidence["reader_phase"], evidence["last_buffer_bytes"], location,
        )
