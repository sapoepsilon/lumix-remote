"""Model profiles: selection from ddd, handshake choice, encrypted req_acc values."""
import isolate  # noqa: F401  (temp config dir; must come first)
import json
import unittest

from lumix import models
from lumix.liveview import extract_jpeg
from lumix.protocol import encrypt_acc, parse_ddd, parse_nonce

DDD = (b'<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0" '
       b'xmlns:pana="urn:schemas-panasonic-com:pana"><device><friendlyName>S5-000001</friendlyName>'
       b'<manufacturer>Panasonic</manufacturer><modelName>LUMIX</modelName>'
       b'<modelNumber>DC-S5</modelNumber><UDN>uuid:test</UDN>%s</device></root>')


class ProfileSelection(unittest.TestCase):
    def test_s5_by_model_number(self):
        self.assertIs(models.for_ddd({"modelNumber": "DC-S5"}), models.S5)
        self.assertIs(models.for_ddd({"modelNumber": "dc-s5 "}), models.S5)

    def test_unknown_body_gets_generic(self):
        self.assertIs(models.for_ddd({"modelNumber": "DC-G9M2"}), models.GENERIC)
        self.assertIs(models.for_ddd({}), models.GENERIC)

    def test_pinned_model_wins(self):
        self.assertIs(models.resolve("generic", {"modelNumber": "DC-S5"}), models.GENERIC)
        self.assertIs(models.resolve("auto", {"modelNumber": "DC-S5"}), models.S5)
        self.assertIs(models.resolve("no-such", {"modelNumber": "DC-S5"}), models.S5)

    def test_register_new_model_keeps_generic_last(self):
        p = models.S5.derive(id="dc_s5m2", name="Lumix S5II", model_numbers=["DC-S5M2"], verified=False)
        models.register(p)
        try:
            self.assertIs(models.for_ddd({"modelNumber": "DC-S5M2"}), p)
            self.assertIs(models.PROFILES[-1], models.GENERIC)
            self.assertIn("dc_s5m2", [m["id"] for m in models.catalog()])
        finally:
            models.PROFILES.remove(p)
            models._BY_ID.pop(p.id)

    def test_profiles_are_json(self):
        for p in models.PROFILES:
            d = json.loads(json.dumps(p.to_dict()))
            self.assertTrue(d["setup_wifi"], p.id)
            self.assertIn("photo", d["auto_preset"])
            self.assertIn("video", d["auto_preset"])

    def test_s5_setup_paths(self):
        titles = " | ".join(s["title"] for s in models.S5.setup_wifi)
        for part in ("Setup", "Wi-Fi Function", "New Connection", "Remote Shooting & View", "DISP", "Via Network"):
            self.assertIn(part, titles)
        ble = " | ".join(s["title"] for s in models.S5.setup_ble)
        for part in ("Bluetooth", "ON", "SET", "Pairing", "Remote Wakeup"):
            self.assertIn(part, ble)


class Handshake(unittest.TestCase):
    def test_s5_plain_req_acc(self):
        self.assertEqual(models.S5.handshake_for(parse_ddd(DDD % b"")), "req_acc")

    def test_auto_reads_cam_capability(self):
        enc = parse_ddd(DDD % b"<pana:X_CamCapability>0101</pana:X_CamCapability>")
        self.assertEqual(enc["X_CamCapability"], "0101")
        self.assertEqual(models.GENERIC.handshake_for(enc), "req_acc_g")
        self.assertEqual(models.GENERIC.handshake_for(parse_ddd(DDD % b"")), "req_acc")
        self.assertEqual(models.GENERIC.handshake_for({"X_CamCapability": "0100"}), "req_acc")

    def test_encrypt_acc(self):
        self.assertEqual(parse_nonce("ok,0000abcd"), 0xABCD)
        with self.assertRaises(ValueError):
            parse_nonce("err_param")
        # one block, no padding: "ABCD" = 0x41424344 ^ 0x01020304 = 0x40404040 -> byte-swapped
        self.assertEqual(encrypt_acc("ABCD", 0x01020304), "40404040")
        self.assertEqual(encrypt_acc("AB", 0), encrypt_acc("AB", models_zero()))
        self.assertEqual(len(encrypt_acc("LumixRemote", 5)), 24)   # 11 chars -> 3 blocks


def models_zero() -> int:
    from lumix.protocol import ENC_ZERO_NONCE
    return ENC_ZERO_NONCE


class LiveViewRules(unittest.TestCase):
    def test_rules_offset(self):
        jpeg = b"\xff\xd8\xff\xe0" + b"x" * 40 + b"\xff\xd9"
        hdr = bytearray(80)
        hdr[30:32] = (80 - 32).to_bytes(2, "big")
        self.assertEqual(extract_jpeg(bytes(hdr) + jpeg, models.S5.liveview), jpeg)
        other = models.LiveViewRules(header_len_offset=2, header_len_add=0, soi_search_fallback=False)
        hdr2 = bytearray(80)
        hdr2[2:4] = (80).to_bytes(2, "big")
        self.assertEqual(extract_jpeg(bytes(hdr2) + jpeg, other), jpeg)
        self.assertIsNone(extract_jpeg(bytes(hdr) + jpeg, other))   # wrong layout, no fallback


if __name__ == "__main__":
    unittest.main()
