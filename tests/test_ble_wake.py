"""Bluetooth wake from the web UI: POST /api/cameras/{cam}/ble/wake answers the
lumix-ble JSON (200 ok / 502 failed / 409 busy), and the page wires the button.

bin/lumix-ble is replaced by a stub script, so no Bluetooth radio or camera is
touched; the camera itself is the in-process emulator (test_timelapse_emu.Rig).
"""
import isolate  # noqa: F401  (temp config dir; must come first)

import asyncio
import os
import stat
import sys
import unittest
from unittest import mock

from server import ble_api
from test_timelapse_emu import EmuTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "server", "static")


def stub_ble(path, line, delay=0.0):
    """A fake bin/lumix-ble that logs to stderr, sleeps, and prints one JSON line."""
    with open(path, "w") as f:
        f.write("#!%s\nimport sys, time\nsys.stderr.write('stub %%s\\n' %% ' '.join(sys.argv[1:]))\n"
                "time.sleep(%r)\nprint(%r)\n" % (sys.executable, delay, line))
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


class BleWakeApiTest(EmuTestCase):
    def wake(self, line, delay=0.0):
        stub = stub_ble(os.path.join(self.tmp.name, "lumix-ble"), line, delay)
        return mock.patch.object(ble_api, "BLE_BIN", stub)

    def test_wake_ok_returns_json(self):
        r = self.rig

        async def go():
            st, d = await r.call("GET", "/api/cameras/emu/ble/status")
            self.assertEqual(st, 200)
            self.assertFalse(d["registered"])                    # what the UI's paired check mirrors
            st, cams = await r.call("GET", "/api/cameras")
            self.assertIn("registered", cams["cameras"][0]["ble"])
            with self.wake('{"ok": true, "ip": "127.0.0.1", "elapsed_s": 1.0}'):
                st, d = await r.call("POST", "/api/cameras/emu/ble/wake", {})
            self.assertEqual(st, 200, d)
            self.assertTrue(d["ok"])
            self.assertEqual(d["ip"], "127.0.0.1")
            self.assertTrue(any(x.startswith("stub wake") for x in d["log"]), d["log"])
            st, s = await r.call("GET", "/api/cameras/emu/ble/status")
            self.assertEqual([(h["cmd"], h["ok"]) for h in s["history"]], [("wake", True)])
        self.run_(go(), 30)

    def test_wake_failure_is_502_with_error(self):
        r = self.rig

        async def go():
            with self.wake('{"ok": false, "error": "camera not found over Bluetooth"}'):
                st, d = await r.call("POST", "/api/cameras/emu/ble/wake", {})
            self.assertEqual(st, 502)
            self.assertFalse(d["ok"])
            self.assertEqual(d["error"], "camera not found over Bluetooth")
        self.run_(go(), 30)

    def test_second_wake_while_running_is_409(self):
        r = self.rig

        async def go():
            with self.wake('{"ok": true}', delay=1.5):
                first = asyncio.ensure_future(r.call("POST", "/api/cameras/emu/ble/wake", {}))
                await r.wait(lambda: r.sess.ble.lock.locked(), 5, "wake running")
                st, d = await r.call("POST", "/api/cameras/emu/ble/wake", {})
                self.assertEqual(st, 409)
                self.assertIn("BLE busy", d["error"])
                st, d = await first
                self.assertEqual(st, 200, d)
        self.run_(go(), 30)


class WakeUiWiringTest(unittest.TestCase):
    def test_page_has_wake_controls(self):
        with open(os.path.join(STATIC, "index.html")) as f:
            html = f.read()
        with open(os.path.join(STATIC, "app.js")) as f:
            js = f.read()
        for el_id in ("btnwake", "bannerwake", "cswake", "wakestate", "cswakestate", "powertext"):
            self.assertIn('id="%s"' % el_id, html)
            self.assertIn('$("%s")' % el_id, js)
        self.assertIn('cp("/api/ble/wake")', js)          # per-camera route via cp()
        self.assertIn("Run setup again…", js)             # unpaired: points at pairing


if __name__ == "__main__":
    unittest.main()
