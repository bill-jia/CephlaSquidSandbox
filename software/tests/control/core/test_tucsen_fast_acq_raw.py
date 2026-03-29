"""Unit tests for Tucsen fast-acquisition raw byte unpacking."""

import numpy as np

from control.core.fast_acquisition_writer import FastAcquisitionWriter
from control.camera_tucsen import (
    camera_mode_name_to_packing,
    decode_tucsen_cms12,
    decode_tucsen_hdr16,
    decode_tucsen_hs11,
    decode_tucsen_raw_bytes,
    max_frame_bytes_for_tucsen_mode,
)


def test_camera_mode_name_to_packing():
    assert camera_mode_name_to_packing("hdr") == "hdr16"
    assert camera_mode_name_to_packing("cms") == "cms12"
    assert camera_mode_name_to_packing("high_speed") == "hs11"
    assert camera_mode_name_to_packing("sensitivity") == "cms12"
    assert camera_mode_name_to_packing("speed") == "hs11"
    assert camera_mode_name_to_packing(None) == "hdr16"


def test_max_frame_bytes():
    h, w = 4, 4
    n = 16
    assert max_frame_bytes_for_tucsen_mode(h, w, "hdr16") == n * 2
    assert max_frame_bytes_for_tucsen_mode(h, w, "cms12") == (n * 3 + 1) // 2
    assert max_frame_bytes_for_tucsen_mode(h, w, "hs11") == (n * 3 + 1) // 2


def test_decode_hdr16_roundtrip():
    h, w = 2, 3
    flat = np.arange(6, dtype=np.uint16)
    raw = flat.tobytes()
    out = decode_tucsen_hdr16(raw, h, w)
    np.testing.assert_array_equal(out, flat.reshape(h, w))


def test_decode_hdr16_underfilled_zero_pads():
    """Fewer bytes than H*W*2: FastAcquisitionWriter pads before decode; missing pixels are zero."""
    h, w = 2, 2
    raw = np.array([1, 2], dtype=np.uint16).tobytes()  # 4 bytes, need 8
    exp = h * w * 2
    padded = FastAcquisitionWriter._pad_raw_to_expected(raw, exp)
    out = decode_tucsen_hdr16(padded, h, w)
    expected = np.array([[1, 2], [0, 0]], dtype=np.uint16)
    np.testing.assert_array_equal(out, expected)


def test_decode_cms12_synthetic_pair():
    """Two 12-bit pixels packed in three bytes (MSB first). Third byte is bits 11-4 of pixel 1."""
    p0, p1 = 0xABC, 0x123
    b0 = (p0 >> 4) & 0xFF
    b1 = ((p0 & 0xF) << 4) | (p1 & 0xF)
    b2 = (p1 >> 4) & 0xFF
    raw = bytes([b0, b1, b2])
    out = decode_tucsen_cms12(raw, 1, 2)
    assert out.shape == (1, 2)
    assert int(out[0, 0]) == p0
    assert int(out[0, 1]) == p1


def test_decode_cms12_underfilled_zero_pads():
    h, w = 2, 2
    n = 4
    expected_bytes = (n * 3 + 1) // 2
    raw = bytes([0xFF] * (expected_bytes - 2))  # short by 2
    padded = FastAcquisitionWriter._pad_raw_to_expected(raw, expected_bytes)
    out = decode_tucsen_cms12(padded, h, w)
    assert out.shape == (h, w)
    assert out.dtype == np.uint16


def test_decode_cms12_small_frame():
    h, w = 2, 2
    n = 4
    raw = bytearray((n * 3 + 1) // 2)
    # four pixels: two pairs, 6 bytes
    pairs = [(0x100, 0x200), (0x300, 0x400)]
    i = 0
    for p0, p1 in pairs:
        raw[i] = (p0 >> 4) & 0xFF
        raw[i + 1] = ((p0 & 0xF) << 4) | (p1 & 0xF)
        raw[i + 2] = (p1 >> 4) & 0xFF
        i += 3
    out = decode_tucsen_cms12(bytes(raw), h, w)
    assert int(out[0, 0]) == 0x100
    assert int(out[0, 1]) == 0x200
    assert int(out[1, 0]) == 0x300
    assert int(out[1, 1]) == 0x400


def test_decode_hs11_shift():
    """11-bit values as 12-bit container with LSB zero: unpack CMS then >> 1."""
    h, w = 1, 2
    p0_11, p1_11 = 0x5AB, 0x123
    p0_12 = (p0_11 << 1) & 0xFFF
    p1_12 = (p1_11 << 1) & 0xFFF
    b0 = (p0_12 >> 4) & 0xFF
    b1 = ((p0_12 & 0xF) << 4) | (p1_12 & 0xF)
    b2 = (p1_12 >> 4) & 0xFF
    raw = bytes([b0, b1, b2])
    out = decode_tucsen_hs11(raw, h, w)
    assert int(out[0, 0]) == p0_11
    assert int(out[0, 1]) == p1_11


def test_decode_tucsen_raw_bytes_dispatch():
    h, w = 1, 1
    raw16 = np.array([0x1234], dtype=np.uint16).tobytes()
    a = decode_tucsen_raw_bytes("hdr16", raw16, h, w)
    assert a.shape == (1, 1) and int(a[0, 0]) == 0x1234


def test_pack_roundtrip_cms12_hs11():
    h, w = 2, 2
    pixels_11 = np.array([[100, 200], [300, 400]], dtype=np.uint16)
    p12 = (pixels_11.astype(np.int32) << 1) & 0xFFF
    raw = bytearray((h * w * 3 + 1) // 2)
    flat = p12.ravel()
    i = 0
    for j in range(0, len(flat), 2):
        p0 = int(flat[j])
        p1 = int(flat[j + 1])
        raw[i] = (p0 >> 4) & 0xFF
        raw[i + 1] = ((p0 & 0xF) << 4) | (p1 & 0xF)
        raw[i + 2] = (p1 >> 4) & 0xFF
        i += 3
    out_hs = decode_tucsen_hs11(bytes(raw), h, w)
    np.testing.assert_array_equal(out_hs, pixels_11)
