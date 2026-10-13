"""tools/ble-wake-experiment.py without a radio, a camera or a server: it refuses
to run while the server's BLE worker is busy or the wake cap is used up, parses
the macOS Wi-Fi scan without keeping other networks' names, and sends its
variant to the server's wake route."""
import isolate  # noqa: F401  (temp config dir; must come first)

import asyncio
import importlib.util
import os
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("ble_wake_experiment",
                                              os.path.join(ROOT, "tools", "ble-wake-experiment.py"))
exp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exp)


class PreflightTest(unittest.TestCase):
    def test_busy_and_cap_refuse(self):
        self.assertIn("busy", exp.preflight({"busy": True, "running": "wake"}))
        self.assertIn("wake cap", exp.preflight({"busy": False, "wakes": {"blocked": "already tried 2"}}))
        self.assertIsNone(exp.preflight({"busy": False, "wakes": {"blocked": None}}))


class WifiParseTest(unittest.TestCase):
    def test_camera_ap_and_redaction(self):
        doc = {"SPAirPortDataType": [{"spairport_airport_interfaces": [{
            "spairport_airport_other_local_wireless_networks": [
                {"_name": "<redacted>", "spairport_network_channel": "1 (2GHz, 20MHz)"},
                {"_name": "S5-ABC123", "spairport_network_channel": "6 (2GHz, 20MHz)"},
                {"_name": "Neighbour", "spairport_network_channel": "36 (5GHz, 80MHz)"},
            ]}]}]}
        w = exp.parse_wifi(doc)
        self.assertEqual(w["n"], 3)
        self.assertEqual(w["redacted"], 1)
        self.assertEqual(w["camera_ssids"], ["S5-ABC123"])
        self.assertNotIn("Neighbour", repr(w))


class RunTest(unittest.TestCase):
    def args(self, **kw):
        a = exp.argparse.Namespace(variant="two-session", server="http://srv", camera=None, name="S5-ABC123",
                                   camera_ip=None, pre=0.0, post=0.0, no_adv=True, no_wifi_scan=True,
                                   out_dir=tempfile.mkdtemp())
        a.__dict__.update(kw)
        return a

    def test_refuses_when_busy_without_waking(self):
        calls = []

        def api(server, path, body=None, timeout=10):
            calls.append((path, body))
            return 200, {"busy": True, "running": "wake"}
        with mock.patch.object(exp, "api", api):
            code, out = asyncio.run(exp.run(self.args()))
        self.assertEqual(code, 2)
        self.assertIn("busy", out["refused"])
        self.assertEqual(calls, [("/api/ble/status", None)])

    def test_posts_the_variant_and_summarises(self):
        calls = []

        def api(server, path, body=None, timeout=10):
            calls.append((path, body))
            if path.endswith("status"):
                return 200, {"busy": False, "wakes": {"blocked": None, "recent": 0}}
            return 502, {"ok": False, "refused": True, "step": "wifi", "mode": "single-session",
                         "error": "wifi: camera refused remote wake-up (camera state)"}
        with mock.patch.object(exp, "api", api):
            code, out = asyncio.run(exp.run(self.args(variant="notify-only", camera="s5")))
        self.assertEqual(code, 0)
        self.assertEqual(calls[-1], ("/api/cameras/s5/ble/wake", {"variant": "notify-only"}))
        self.assertTrue(out["summary"]["refused"])
        path = exp.save(out, self.args().out_dir)
        self.assertTrue(path.endswith("-notify-only.json"))


if __name__ == "__main__":
    unittest.main()
