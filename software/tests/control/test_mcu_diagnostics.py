import json
import threading
import time

import pytest
import squid.logging
from crc import CrcCalculator, Crc8

from control.mcu_diagnostics import MCUDiagnostics, capture_thread_stacks
from control.mcu_health_logging import HealthRecordWriter, format_health
from control.microcontroller import Microcontroller, SimSerial
from tools.read_firmware_version import read_version
from tools.report_mcu_health import read_health, render_report


@pytest.fixture(autouse=True)
def isolate_health_files(monkeypatch, tmp_path):
    monkeypatch.setattr(squid.logging, "get_current_log_file_path", lambda: str(tmp_path / "main_hcs.log"))


def telemetry_packets(sequence=7, schema=2):
    words = [0] * 32
    words[0], words[1], words[31] = (schema << 8) | 1, sequence, sequence

    def put(index, value):
        value &= 0xFFFFFFFF
        words[index:index + 2] = [value >> 16, value & 65535]

    put(2, 0xFFFFF123)
    put(4, 123456)
    put(6, 1200000)
    put(8, 1234)
    put(10, -1000)
    put(12, -1016)
    put(14, -1000)
    put(16, 0x98)  # physical left limit, velocity active, no target reached
    put(18, -31)
    words[20] = (1 << 0) | (1 << 1) | (1 << 3) | (1 << 8)
    words[21] = 1 | (2 << 2) | (2 << 4)
    words[22], words[23] = 0xFA02, 65535
    put(24, 3001)
    put(26, 10)
    put(28, 2)
    words[30] = 1200
    crc = CrcCalculator(Crc8.CCITT, table_based=True)
    result = []
    for page, word in enumerate(words):
        packet = bytearray(24)
        packet[0], packet[1] = 250, 1
        packet[10:14] = (-1016).to_bytes(4, "big", signed=True)
        packet[19:23] = bytes((160 + page, word >> 8, word & 255, 0x15))
        packet[23] = crc.calculate_checksum(packet[:23])
        result.append(packet)
    return result


def feed(recorder, packets, crc_valid=True):
    for packet in packets:
        recorder.received(packet, crc_valid, 250, 500)


def test_decode_coherent_snapshot_and_motion_blockers():
    recorder = MCUDiagnostics()
    feed(recorder, telemetry_packets())
    sample = recorder.snapshot()["telemetry"][-1]
    assert sample["firmware"] == "1.5.1"
    assert sample["schema"] == 2
    assert sample["timing_scope"] == "since_previous_snapshot"
    assert sample["uptime_ms"] == 0xFFFFF123
    assert sample["z_target_usteps"] == -1000
    assert sample["z_actual_usteps"] == -1016
    assert sample["z_pid_error"] == -31
    assert sample["last_z_move_result"] == -1
    assert sample["last_z_command_id"] == 250
    assert sample["drivers"] == {"x": 1, "y": 2, "z": 2, "w": 0, "w2": 0}
    assert sample["z_physical_limits"] == 1
    assert set(sample["completion_blockers_at_sample"]) == {
        "z_position_not_at_target", "z_target_reached_flag_clear", "z_velocity_or_ramp_active",
        "z_pid_error_outside_tolerance", "x_busy",
    }


@pytest.mark.parametrize("fault", ["missing", "duplicate", "bad_crc", "sequence", "version", "schema"])
def test_incomplete_or_corrupt_snapshot_discarded_and_next_recovers(fault):
    packets = telemetry_packets()
    recorder = MCUDiagnostics()
    if fault == "missing":
        del packets[13]
    elif fault == "duplicate":
        packets.insert(13, packets[12])
    elif fault == "sequence":
        packets[-1][21] += 1
    elif fault == "version":
        packets[15][22] = 0x14
    elif fault == "schema":
        packets[0][20] = 99
    for page, packet in enumerate(packets):
        recorder.received(packet, not (fault == "bad_crc" and page == 15), 250, 500)
    assert recorder.snapshot()["telemetry"] == []
    feed(recorder, telemetry_packets(sequence=8))
    assert [s["sequence"] for s in recorder.snapshot()["telemetry"]] == [8]


def test_ring_limits_and_legacy_packets():
    recorder = MCUDiagnostics(event_limit=3, rx_limit=4, telemetry_limit=2)
    for i in range(9):
        recorder.event("test", value=i)
    for sequence in (65535, 0, 1):
        feed(recorder, telemetry_packets(sequence))
    legacy = bytearray(24)
    recorder.received(legacy, False, 0, 0)
    snapshot = recorder.snapshot()
    assert [e["value"] for e in snapshot["events"]] == [6, 7, 8]
    assert len(snapshot["rx"]) == 4
    assert [s["sequence"] for s in snapshot["telemetry"]] == [0, 1]
    assert snapshot["counts"]["legacy_zero_crc_accepted"] == 1
    # Snapshot is JSON serializable and remains stable as new events arrive.
    json.dumps(snapshot)
    recorder.event("later")
    assert snapshot["events"][-1]["value"] == 8


def test_timeout_dump_needs_no_serial_io_and_preserves_pending_move(monkeypatch, caplog):
    micro = Microcontroller(SimSerial(), reset_and_initialize=False)
    try:
        micro.terminate_reading_received_packet_thread = True
        micro.thread_read_received_packet.join(timeout=2)
        assert not micro.thread_read_received_packet.is_alive()
        micro.move_z_usteps(-16)
        micro.send_heartbeat()
        pending = bytes(micro.last_command)
        sequence = micro._diagnostic_command_sequence
        feed(micro.diagnostics, telemetry_packets())

        def forbidden(*args, **kwargs):
            raise AssertionError("Timeout diagnostics must not query serial")

        monkeypatch.setattr(micro._serial, "bytes_available", forbidden)
        monkeypatch.setattr(micro._serial, "write", forbidden)
        with pytest.raises(TimeoutError, match="Current mcu operation timed out"):
            micro.wait_till_operation_is_completed(0.01)
        summary = next(json.loads(r.message.split("snapshot ", 1)[1])
                       for r in caplog.records if "[MCU-DIAG] snapshot " in r.message)
        assert summary["at_failure"]["command"] == pending.hex()
        assert summary["wait_entry"]["host_sequence"] == sequence
        assert summary["at_failure"]["reader_alive"] is False
        assert summary["telemetry_age_s"] is not None
        assert "threads_captured_monotonic_s" in summary
        thread_rows = [json.loads(r.message.split("offset=0 ", 1)[1])
                       for r in caplog.records if "[MCU-DIAG] threads " in r.message and "offset=0 " in r.message]
        assert any(frame["function"] == "wait_till_operation_is_completed"
                   for row in thread_rows for thread in row for frame in thread["frames_innermost_first"])
        assert bytes(micro.last_command) == pending
        assert micro.is_busy()
        events = micro.diagnostics.snapshot()["events"]
        assert any(e.get("tx_kind") == "heartbeat" for e in events)
    finally:
        micro.close()


class PacketStream:
    def __init__(self, data):
        self.data = bytes(data)

    def read(self, count):
        data, self.data = self.data[:count], self.data[count:]
        return data


@pytest.mark.parametrize("schema", [1, 2])
def test_version_reader_supports_patch_and_old_firmware(schema):
    packets = telemetry_packets(schema=schema)
    # Attach in the middle of the snapshot; allow a noisy prefix and resync.
    stream = PacketStream(b"garbage" + b"".join(packets[12:] + packets))
    assert read_version(stream, timeout=1) == (1, 5, 1)
    legacy = SimSerial.response_bytes_for(1, 0, 0, 0, 0, 0, False, False, firmware_version=(1, 5))
    assert read_version(PacketStream(bytes(legacy) * 4), timeout=1) == (1, 5)


def test_reader_timing_gap_records_previous_phase(monkeypatch):
    recorder = MCUDiagnostics()
    before = recorder.reader_tick
    monkeypatch.setattr(time, "monotonic", lambda: before + 1)
    recorder.reader_poll("packet_read", backlog=500)
    snapshot = recorder.snapshot()
    assert snapshot["max_reader_gap_s"] == 1
    assert snapshot["events"][-1]["previous_phase"] == "starting"
    assert snapshot["max_buffer_bytes"] == 500


class TelemetrySerial(SimSerial):
    """Exercise the actual serial parser with telemetry interleaved in status."""

    def _respond_to(self, command):
        super()._respond_to(command)
        packet = bytearray(self.response_buffer[-24:])
        del self.response_buffer[-24:]
        crc = CrcCalculator(Crc8.CCITT, table_based=True)
        for page in telemetry_packets():
            packet[19:23] = page[19:23]
            packet[23] = crc.calculate_checksum(packet[:23])
            self.response_buffer.extend(packet)
        self._update_internal_state()


def test_live_reader_decodes_telemetry_without_changing_status_or_position(caplog):
    micro = Microcontroller(TelemetrySerial(), reset_and_initialize=False)
    try:
        micro.move_z_to_usteps(-1016)
        micro.wait_till_operation_is_completed(1)
        deadline = time.monotonic() + 2
        while not micro.diagnostics.snapshot()["telemetry"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert micro.get_pos()[2] == -1016
        assert not micro.is_busy()
        assert micro.firmware_version == (1, 5)
        assert micro.diagnostics.snapshot()["telemetry"][-1]["firmware"] == "1.5.1"
        assert any("Firmware telemetry active" in r.message for r in caplog.records)
    finally:
        micro.close()


def test_old_schema_retains_since_boot_semantics():
    recorder = MCUDiagnostics()
    feed(recorder, telemetry_packets(schema=1))
    summary = recorder.health_summary()
    assert summary["latest_telemetry"]["timing_scope"] == "since_boot"
    assert "since_boot" in summary["firmware_sample_maxima"]
    assert "since_previous_snapshot" not in summary["firmware_sample_maxima"]


def test_health_aggregates_outlive_rings_and_reset_without_losing_pending_command(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    recorder = MCUDiagnostics(event_limit=3, rx_limit=4, telemetry_limit=2)
    feed(recorder, telemetry_packets())
    recorder.event("tx_start", tx_kind="command", command_sequence=1, opcode="MOVE_Z")
    now[0] = 0.2
    recorder.event("resend", reason="ack_id_timeout")
    recorder.event("tx_start", tx_kind="resend", command_sequence=1, opcode="MOVE_Z")
    now[0] = 0.5
    recorder.event("command_complete", host_sequence=1)
    recorder.event("tx_end", duration_s=0.005)
    recorder.event("buffer_reset", success=True, bytes_discarded=5000)
    for _ in range(20):
        recorder.event("resend", reason="test")
    recorder.event("tx_start", tx_kind="command", command_sequence=2, opcode="MOVE_Z")
    now[0] = 60
    first = recorder.health_summary()
    assert first["window_s"] == 60
    assert first["counts"]["rx"] == 32
    assert first["counts"]["resend"] == 21
    assert first["counts"]["buffer_bytes_discarded"] == 5000
    assert first["command_completion"]["MOVE_Z"]["mean_ms"] == 500
    assert first["command_completion"]["MOVE_Z"]["p95_upper_bound_ms"] == 500
    assert first["serial_write"]["mean_ms"] == 5
    assert first["telemetry_state"] == "stale"
    assert first["latest_telemetry"]["schema"] == 2
    assert len(recorder.snapshot()["rx"]) == 4
    now[0] = 61
    recorder.event("command_complete", host_sequence=2)
    second = recorder.health_summary()
    assert second["counts"]["resend"] == 0
    assert second["counts"]["rx"] == 0
    assert second["command_completion"]["MOVE_Z"]["max_ms"] == 60500
    assert second["command_completion"]["MOVE_Z"]["p95_upper_bound_ms"] is None
    assert second["firmware_sample_maxima"] == {}
    assert first["firmware_sample_maxima"]["since_previous_snapshot"]["max_loop_gap_us"] == 1200000


def test_health_monitor_reports_stopped_reader_without_serial_io(monkeypatch, caplog, tmp_path):
    monkeypatch.setattr(Microcontroller, "DIAGNOSTIC_HEALTH_INTERVAL_S", 0.02)
    micro = Microcontroller(SimSerial(), reset_and_initialize=False)
    try:
        micro.terminate_reading_received_packet_thread = True
        micro.thread_read_received_packet.join(timeout=2)

        def forbidden(*args, **kwargs):
            raise AssertionError("Health summaries must not query serial")

        monkeypatch.setattr(micro._serial, "bytes_available", forbidden)
        monkeypatch.setattr(micro._serial, "write", forbidden)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if any("reader STOPPED" in r.message for r in caplog.records):
                break
            time.sleep(0.01)
        assert any("reader STOPPED" in r.message for r in caplog.records)
        assert micro._diagnostic_health_thread.is_alive()
    finally:
        micro.close()
    assert not micro._diagnostic_health_thread.is_alive()
    records, malformed = read_health(next((tmp_path / "mcu_diagnostics").glob("*.jsonl")))
    assert not malformed
    assert records[-1]["final"]
    assert not records[-1]["state"]["reader_alive"]
    assert not any("[MCU-DIAG] health {" in r.message for r in caplog.records)
    assert any("[MCU-HEALTH] Final" in r.message for r in caplog.records)


def test_stack_capture_identifies_blocked_thread_without_locals():
    ready, release = threading.Event(), threading.Event()

    def blocked_worker():
        secret_local = "must not appear in diagnostics"
        ready.set()
        release.wait(5)
        return secret_local

    thread = threading.Thread(target=blocked_worker, name="diagnostic-test-blocker")
    thread.start()
    try:
        assert ready.wait(2)
        stacks = capture_thread_stacks()
        row = next(row for row in stacks if row["name"] == thread.name)
        assert row["native_id"] == thread.native_id
        assert any(frame["function"] == "blocked_worker" for frame in row["frames_innermost_first"])
        assert "must not appear in diagnostics" not in json.dumps(stacks)
    finally:
        release.set()
        thread.join(timeout=2)


def test_health_report_reads_log_trailer_and_separates_sessions(tmp_path):
    recorder = MCUDiagnostics()
    feed(recorder, telemetry_packets())
    summary = recorder.health_summary()
    old = dict(summary, recorder_id="previous-controller")
    old["counts"] = {"wait_failed": 999}
    log = tmp_path / "main_hcs.log"
    log.write_text(
        "prefix [MCU-DIAG] health " + json.dumps(old) + " (microcontroller.py:1)\n"
        + "prefix [MCU-DIAG] health malformed\n"
        + "prefix [MCU-DIAG] health " + json.dumps(summary) + " (microcontroller.py:1)\n",
        encoding="utf-8",
    )
    rows, malformed = read_health(log)
    report = render_report(rows, malformed)
    assert "Firmware: 1.5.1; schema: 2" in report
    assert "failed waits: 0" in report
    assert "Windows: 1" in report
    assert "Ignored 1" in report


def test_health_summary_and_lossless_file(tmp_path, caplog):
    recorder = MCUDiagnostics()
    feed(recorder, telemetry_packets())
    recorder.event("resend")
    recorder.event("wait_failed")
    summary = recorder.health_summary()
    summary["state"] = {"reader_alive": True}
    writer = HealthRecordWriter(tmp_path, recorder.recorder_id)
    writer.write(summary, squid.logging.get_logger("test-health"))
    assert json.loads(writer.path.read_text(encoding="utf-8")) == summary
    text = format_health(summary)
    assert "retries 1, failed waits 1" in text
    assert "reader running" in text
    assert "telemetry fresh" in text
    assert "{" not in text and "histogram" not in text
    assert not any("[MCU-DIAG] health {" in r.message for r in caplog.records)
    # Main logs copied with their diagnostics folder retain a usable reference.
    main_log = tmp_path / "main_hcs.log"
    main_log.write_text(
        '[MCU-DIAG] health details: ' + json.dumps(str(tmp_path / "old-location" / writer.path.name))
        + ' (microcontroller.py:1)\n' + text + '\n', encoding="utf-8",
    )
    rows, malformed = read_health(main_log)
    assert rows == [summary] and malformed == 0


def test_health_file_failure_preserves_records_and_recovers(tmp_path, caplog):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    recorder = MCUDiagnostics()
    writer = HealthRecordWriter(blocked, recorder.recorder_id)
    logger = squid.logging.get_logger("test-health")
    first = recorder.health_summary()
    writer.write(first, logger)
    second = recorder.health_summary()
    writer.write(second, logger)
    assert sum("Cannot write health details" in r.message for r in caplog.records) == 1
    assert sum("[MCU-DIAG] health {" in r.message for r in caplog.records) == 2
    blocked.unlink()
    third = recorder.health_summary()
    writer.write(third, logger)
    assert json.loads(writer.path.read_text(encoding="utf-8")) == third
    assert any("writing recovered" in r.message for r in caplog.records)
    main_log = tmp_path / "main_hcs.log"
    main_log.write_text(
        "[MCU-DIAG] health details: " + json.dumps(str(writer.path)) + "\n"
        + "[MCU-DIAG] health " + json.dumps(first) + "\n"
        + "[MCU-DIAG] health " + json.dumps(second) + "\n", encoding="utf-8",
    )
    rows, malformed = read_health(main_log)
    assert rows == [first, second, third] and malformed == 0


def test_health_summary_does_not_call_missing_data_healthy():
    summary = MCUDiagnostics().health_summary()
    text = format_health(summary)
    assert "longest gap unavailable" in text
    assert "last packet never" in text
    assert "telemetry not received" in text
    assert "reader unknown" in text
