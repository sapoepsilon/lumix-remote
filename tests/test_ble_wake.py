"""Bluetooth wake from the web UI: POST /api/cameras/{cam}/ble/wake answers the
lumix-ble JSON (200 ok / 502 failed / 409 busy), and the page wires the button.

bin/lumix-ble is replaced by a stub script, so no Bluetooth radio or camera is
touched; the camera itself is the in-process emulator (test_timelapse_emu.Rig).
"""
import isolate  # noqa: F401  (temp config dir; must come first)

import asyncio
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

from lumix import ble, registry
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


class BleSelfHealApiTest(EmuTestCase):
    """A pairing done before registered_at was saved: the first successful BLE
    login (wake, or the "Check Bluetooth pairing" probe) records it."""

    def stub(self, line):
        path = stub_ble(os.path.join(self.tmp.name, "lumix-ble"), line)
        return mock.patch.object(ble_api, "BLE_BIN", path)

    async def ble_entry(self):
        st, d = await self.rig.call("GET", "/api/cameras")
        return d["cameras"][0]["ble"]

    def test_probe_success_marks_paired(self):
        r = self.rig
        line = json.dumps({"ok": True, "advertising": True, "registered": True,
                           "adv": {"name": "S5-ABC123", "address": "AA-BB"}, "info": {}})

        async def go():
            self.assertFalse((await self.ble_entry())["registered"])
            with self.stub(line):
                st, d = await r.call("GET", "/api/cameras/emu/ble/status?probe=1")
            self.assertEqual(st, 200, d)
            self.assertTrue(d["probe"]["registered"])
            self.assertTrue(d["registered"])
            b = await self.ble_entry()
            self.assertTrue(b["registered"])
            self.assertEqual(b["name"], "S5-ABC123")
            e = r.sess.reload()["ble"]
            self.assertIsInstance(e["registered_at"], int)
            self.assertEqual(e["address"], "AA-BB")
            st, d = await r.call("GET", "/api/cameras/emu/ble/status")
            self.assertTrue(d["registered"])
        self.run_(go(), 30)

    def test_probe_failure_leaves_unpaired(self):
        r = self.rig
        line = json.dumps({"ok": True, "advertising": True, "registered": False,
                           "error": "BleakError: disconnected"})

        async def go():
            with self.stub(line):
                st, d = await r.call("GET", "/api/cameras/emu/ble/status?probe=1")
            self.assertEqual(st, 200, d)
            self.assertFalse(d["registered"])
            self.assertFalse((await self.ble_entry())["registered"])
            self.assertFalse(r.sess.reload()["ble"].get("registered_at"))
        self.run_(go(), 30)

    def test_wake_success_marks_paired(self):
        r = self.rig

        async def go():
            with self.stub('{"ok": true, "ip": "127.0.0.1"}'):
                st, d = await r.call("POST", "/api/cameras/emu/ble/wake", {})
            self.assertEqual(st, 200, d)
            self.assertTrue((await self.ble_entry())["registered"])
        self.run_(go(), 30)

    def test_wake_failure_leaves_unpaired(self):
        r = self.rig

        async def go():
            with self.stub('{"ok": false, "error": "camera not advertising"}'):
                st, d = await r.call("POST", "/api/cameras/emu/ble/wake", {})
            self.assertEqual(st, 502)
            self.assertFalse((await self.ble_entry())["registered"])
        self.run_(go(), 30)

    def test_existing_registered_at_is_kept(self):
        r = self.rig

        async def go():
            await r.sess.mgr.update("emu", ble={"registered_at": 1234})
            with self.stub('{"ok": true, "registered": true}'):
                await r.call("GET", "/api/cameras/emu/ble/status?probe=1")
            self.assertEqual(r.sess.reload()["ble"]["registered_at"], 1234)
        self.run_(go(), 30)


class MarkRegisteredTest(unittest.TestCase):
    """lumix.ble.mark_registered: what lumix-ble saves after a successful login."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"LUMIX_REMOTE_HOME": self.tmp.name})
        self.env.start()
        reg = registry.Registry()
        # what the 09:20 migration left: legacy ble.json had only home_ssid
        self.cid = reg.add("S5", ble={"home_ssid": "HomeNet"})["id"]
        os.environ["LUMIX_CAMERA_ID"] = self.cid

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def ble_entry(self):
        return registry.Registry().get(self.cid)["ble"]

    def test_saves_registration_name_address(self):
        self.assertFalse(self.ble_entry().get("registered_at"))
        self.assertTrue(ble.mark_registered("S5-ABC123", "AA-BB"))
        b = self.ble_entry()
        self.assertIsInstance(b["registered_at"], int)
        self.assertEqual(b["name"], "S5-ABC123")
        self.assertEqual(b["address"], "AA-BB")
        self.assertEqual(b["identity"], ble._client_id())
        self.assertEqual(b["home_ssid"], "HomeNet")           # untouched

    def test_idempotent_and_keeps_first_timestamp(self):
        ble.mark_registered("S5-ABC123", "AA-BB")
        first = self.ble_entry()["registered_at"]
        self.assertFalse(ble.mark_registered("S5-ABC123", "AA-BB"))
        self.assertEqual(self.ble_entry()["registered_at"], first)

    def test_ignores_non_camera_names(self):
        ble.mark_registered("Johns AirPods", None)
        self.assertIsNone(self.ble_entry().get("name"))

    def test_never_raises(self):
        with mock.patch.object(ble, "save_config", side_effect=OSError("disk full")):
            self.assertFalse(ble.mark_registered("S5-ABC123", "AA-BB"))


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

    def test_unpaired_wake_is_not_blocked(self):
        with open(os.path.join(STATIC, "app.js")) as f:
            js = f.read()
        start = js.index("async function bleWake()")
        body = js[start:js.index("\n}\n", start)]
        # no early return on a missing pairing record: it tries, and only a failure
        # shows the pairing hint
        self.assertNotIn("if (wake.needPair) { renderWake(); return; }", body)
        self.assertIn("unsure: !blePaired()", body)
        self.assertIn('fetch(cp("/api/ble/wake")', body)
        self.assertIn("wake.needPair = !wake.ok && wake.unsure", body)
        self.assertIn("may not be paired", js)
        self.assertNotIn("Needs Bluetooth pairing first", js)

    def test_check_pairing_button(self):
        with open(os.path.join(STATIC, "index.html")) as f:
            html = f.read()
        with open(os.path.join(STATIC, "app.js")) as f:
            js = f.read()
        self.assertIn('id="camblecheck"', html)
        self.assertIn("Check Bluetooth pairing", html)
        self.assertIn('$("camblecheck").addEventListener("click"', js)
        self.assertIn("/ble/status?probe=1", js)          # reuses the probe route


if __name__ == "__main__":
    unittest.main()
