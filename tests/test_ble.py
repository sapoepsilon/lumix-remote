import datetime as dt
import struct
import unittest

from lumix import ble


def _encode(text: bytes, key: int) -> bytes:
    """Inverse of ble.xor_decode, for the round-trip test."""
    data = bytearray(b"\x00" + text)          # first byte of block 0 is skipped
    while len(data) % 4:
        data.append(0xFF)
    out = bytearray()
    for i in range(0, len(data), 4):
        v = struct.unpack("<I", bytes(data[i:i + 4]))[0] ^ key
        out += struct.pack(">I", v)
    return bytes(out)


class BleHelpers(unittest.TestCase):
    def test_xor_roundtrip(self):
        for key in (0x18463B17, 0):
            k = key or 0x9032782C
            self.assertEqual(ble.xor_decode(_encode(b"secret12", k), key), "secret12")
        self.assertEqual(ble.xor_decode(b"", 5), "")
        self.assertEqual(ble.xor_decode(b"\x01\x02\x03", 5), "")

    def test_sta_ssid(self):
        self.assertEqual(ble.parse_sta_ssid(bytes.fromhex("01") + b"Home Net\x00\x00"), "Home Net")
        self.assertIsNone(ble.parse_sta_ssid(b"\x01"))

    def test_ssid_payload(self):
        p = ble.ssid_payload("abc")
        self.assertEqual(len(p), 32)
        self.assertTrue(p.startswith(b"abc\x00"))
        with self.assertRaises(ValueError):
            ble.ssid_payload("x" * 33)

    def test_client_uuid(self):
        b = ble.client_uuid_bytes()
        self.assertEqual(len(b), 16)
        self.assertEqual(b[:10].hex().upper(), "4D454930010010008001")

    def test_clock_payload(self):
        tz = dt.timezone(dt.timedelta(hours=-6))
        p = ble.clock_payload(dt.datetime(2026, 10, 11, 21, 5, 7, tzinfo=tz))
        self.assertEqual(p.hex(), "ea070a0b15050700" + struct.pack("<h", -360).hex())

    def test_is_lumix(self):
        self.assertTrue(ble._is_lumix("S5-XXXXXX", {0x3A: b"\x00"}, [ble.SVC_MAIN]))
        self.assertTrue(ble._is_lumix("S5-XXXXXX", {0x3A: b"\x00"}, []))
        self.assertFalse(ble._is_lumix("S1899af2988802f71C", {0x3A: b"\x00"}, []))


if __name__ == "__main__":
    unittest.main()
