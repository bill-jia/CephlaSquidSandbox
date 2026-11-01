"""Read the running controller's version without sending any commands.

Close Squid/serial monitors first, then run:
    python software/tools/read_firmware_version.py COM9

This checks three consecutive CRC-valid 24-byte status packets, including the
patch version in telemetry when present. It does not
construct a Microcontroller, reset the board, initialize drivers, or move axes.
"""

import argparse
import time

import serial


def crc8(data):
    value = 0
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = ((value << 1) ^ (0x07 if value & 0x80 else 0)) & 0xFF
    return value


def read_version(connection, timeout=5.0):
    data = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data.extend(connection.read(256))
        while len(data) >= 72:
            packets = [data[offset : offset + 24] for offset in (0, 24, 48)]
            if all(
                packet[1] <= 4
                and (packet[19:22] == b"\x00\x00\x00" or 160 <= packet[19] < 192)
                and packet[22] >> 4 > 0
                and crc8(packet[:23]) == packet[23]
                for packet in packets
            ) and len({packet[22] for packet in packets}) == 1:
                version = packets[0][22]
                for packet in packets:
                    if packet[19] == 160 and packet[20] in (1, 2):
                        return version >> 4, version & 0x0F, packet[21]
                if all(packet[19:22] == b"\x00\x00\x00" for packet in packets):
                    return version >> 4, version & 0x0F
                # Valid paged telemetry, but wait for its version header.
                del data[:24]
                continue
            del data[0]
    raise TimeoutError("No confirmed version in status packets. Check the controller port and close other serial clients.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", help="Controller serial port, e.g. COM9")
    args = parser.parse_args()
    # Avoid asserting control lines on open. Teensy USB serial does not need
    # DTR/RTS here, and 2 Mbaud is its normal application setting.
    connection = serial.Serial(port=None, baudrate=2000000, timeout=0.2)
    connection.dtr = False
    connection.rts = False
    connection.port = args.port
    try:
        connection.open()
        version = ".".join(map(str, read_version(connection)))
        print(f"{args.port}: running firmware {version} (3 consecutive CRC-valid status packets)")
    except (serial.SerialException, TimeoutError) as exc:
        parser.exit(1, f"Could not read firmware version: {exc}\n")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
