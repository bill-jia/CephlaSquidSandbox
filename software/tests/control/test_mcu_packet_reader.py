"""Transport fragmentation, batching, CRC recovery and actual command waits."""

import pytest
from crc import CrcCalculator, Crc8

import squid.logging
from control._def import BUFFER_SIZE_LIMIT
from control.mcu_diagnostics import MCUDiagnostics
from control.microcontroller import Microcontroller, SimSerial


CRC = CrcCalculator(Crc8.CCITT, table_based=True)


def packet(value):
    data = bytearray([value] * 24)
    data[-1] = CRC.calculate_checksum(data[:-1])
    return data


class BufferedSerial:
    def __init__(self, data=b"", short_read=None):
        self.data = bytearray(data)
        self.short_read = short_read
        self.read_sizes = []
        self.polls = 0
        self.reset_ok = True

    def bytes_available(self):
        self.polls += 1
        return len(self.data)

    def read(self, count):
        assert count <= len(self.data)
        self.read_sizes.append(count)
        count = min(count, self.short_read) if self.short_read else count
        result = self.data[:count]
        del self.data[:count]
        return result

    def reset_input_buffer(self):
        if self.reset_ok:
            self.data.clear()
        return self.reset_ok


def reader(serial):
    # Exercise the real parser without starting threads or initializing hardware.
    micro = Microcontroller.__new__(Microcontroller)
    micro._serial = serial
    micro.rx_buffer_length = 24
    micro.diagnostics = MCUDiagnostics()
    micro.log = squid.logging.get_logger("test-packet-reader")
    return micro


def test_entire_backlog_uses_one_read_and_preserves_packet_order():
    packets = [packet(n) for n in range(1, 101)]
    serial = BufferedSerial(b"".join(packets))
    micro, pending = reader(serial), bytearray()
    actual = [micro._read_status_packet(pending, CRC) for _ in packets]
    assert actual == packets
    assert serial.read_sizes == [2400]
    assert serial.polls == 1
    assert not pending


@pytest.mark.parametrize("split", range(1, 24))
def test_partial_packet_survives_empty_reads_and_next_fragment(split):
    expected = packet(17)
    serial = BufferedSerial(expected[:split])
    micro, pending = reader(serial), bytearray()
    assert micro._read_status_packet(pending, CRC) is None
    assert micro._read_status_packet(pending, CRC) is None
    assert pending == expected[:split]
    serial.data.extend(expected[split:] + packet(18))
    assert micro._read_status_packet(pending, CRC) == expected
    assert micro._read_status_packet(pending, CRC) == packet(18)


def test_short_reads_and_legacy_zero_crc():
    legacy = packet(19)
    legacy[-1] = 0
    expected = [packet(17), legacy, packet(20)]
    serial = BufferedSerial(b"".join(expected), short_read=7)
    micro, pending, actual = reader(serial), bytearray(), []
    for _ in range(30):
        result = micro._read_status_packet(pending, CRC)
        if result is not None:
            actual.append(result)
        if not serial.data and not pending:
            break
    assert actual == expected


def test_crc_resynchronization_keeps_partial_tail():
    bad = packet(17)
    bad[-1] ^= 128
    expected = packet(34)
    serial = BufferedSerial(bad + expected[:13])
    micro, pending = reader(serial), bytearray()
    assert micro._read_status_packet(pending, CRC) is None
    serial.data.extend(expected[13:])
    assert micro._read_status_packet(pending, CRC) == expected
    assert micro.diagnostics.counts["crc_failure"] == 24


@pytest.mark.parametrize("success", [True, False])
def test_overflow_reset_preserves_policy_and_recovers(success):
    serial = BufferedSerial(bytes([255]) * BUFFER_SIZE_LIMIT)
    serial.reset_ok = success
    micro, pending = reader(serial), bytearray(b"partial")
    assert micro._read_status_packet(pending, CRC) is None
    assert bool(pending) is not success
    assert micro.diagnostics.counts["buffer_reset"] == 1
    assert micro.diagnostics.counts["buffer_bytes_discarded"] == (BUFFER_SIZE_LIMIT + 7 if success else 0)
    serial.reset_ok = True
    if not success:
        micro._read_status_packet(pending, CRC)
    serial.data.extend(packet(23))
    assert micro._read_status_packet(pending, CRC) == packet(23)


def test_fragmented_status_completes_real_command_waits():
    class FragmentedSim(SimSerial):
        def read(self, count=1, reconnect_tries=0):
            return super().read(min(count, 7), reconnect_tries)

    micro = Microcontroller(FragmentedSim(), reset_and_initialize=False)
    try:
        for target in (1000, -1000, 0):
            micro.move_z_to_usteps(target)
            micro.wait_till_operation_is_completed(1)
            assert micro.get_pos()[2] == target
        assert micro.diagnostics.counts["resend"] == 0
        assert micro.diagnostics.counts["crc_failure"] == 0
    finally:
        micro.close()
