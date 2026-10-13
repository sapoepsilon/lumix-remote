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

    def test_wakes_are_rate_limited(self):
        r = self.rig

        async def go():
            with self.wake('{"ok": false, "error": "wifi-wait: x", "step": "wifi-wait"}'):
                for _ in range(2):
                    st, d = await r.call("POST", "/api/cameras/emu/ble/wake", {})
                    self.assertEqual(st, 502, d)
                st, d = await r.call("POST", "/api/cameras/emu/ble/wake", {})
                self.assertEqual(st, 429, d)
                self.assertEqual(d["step"], "limit")
                self.assertIn("battery", d["error"])
                st, d = await r.call("POST", "/api/cameras/emu/ble/power-cycle", {})
                self.assertEqual(st, 429, d)            # refused before any Wi-Fi poweroff
                self.assertNotIn("poweroff", d)
            st, s = await r.call("GET", "/api/cameras/emu/ble/status")
            self.assertEqual(s["wakes"]["recent"], 2)
            self.assertEqual([h["cmd"] for h in s["history"]], ["wake", "wake"])   # no retries
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


class BleWakeVariantTest(EmuTestCase):
    """tools/ble-wake-experiment.py runs its variants through the server, so they
    share the BLE lock and the wake cap with the UI."""

    def wake(self, line):
        stub = stub_ble(os.path.join(self.tmp.name, "lumix-ble"), line)
        return mock.patch.object(ble_api, "BLE_BIN", stub)

    def test_variant_flags_reach_lumix_ble(self):
        r = self.rig

        async def go():
            with self.wake('{"ok": false, "error": "wifi-wait: x", "step": "wifi-wait"}'):
                st, d = await r.call("POST", "/api/cameras/emu/ble/wake", {"variant": "baseline"})
                self.assertEqual(st, 502, d)
                self.assertEqual(d["variant"], "baseline")
                self.assertTrue(any("wake --single-session --legacy" in x for x in d["log"]), d["log"])
                st, d = await r.call("POST", "/api/cameras/emu/ble/wake", {"variant": "notify-only"})
                self.assertTrue(any(x.startswith("stub wake --single-session") and "--legacy" not in x
                                    for x in d["log"]), d["log"])
                st, d = await r.call("POST", "/api/cameras/emu/ble/wake", {"variant": "two-session"})
                self.assertEqual(st, 429, d)               # same cap as the UI's wakes
        self.run_(go(), 30)

    def test_default_wake_is_two_session(self):
        r = self.rig

        async def go():
            with self.wake('{"ok": true}'):
                st, d = await r.call("POST", "/api/cameras/emu/ble/wake", {})
            self.assertEqual(st, 200, d)
            self.assertNotIn("variant", d)
            self.assertFalse(any("--single-session" in x for x in d["log"]), d["log"])
        self.run_(go(), 30)

    def test_unknown_variant_is_400_and_costs_no_wake(self):
        r = self.rig

        async def go():
            st, d = await r.call("POST", "/api/cameras/emu/ble/wake", {"variant": "softap"})
            self.assertEqual(st, 400, d)
            st, s = await r.call("GET", "/api/cameras/emu/ble/status")
            self.assertEqual(s["wakes"]["recent"], 0)
        self.run_(go(), 30)

    def test_refusal_hint_is_passed_to_the_ui(self):
        r = self.rig

        async def go():
            line = json.dumps({"ok": False, "step": "wifi", "refused": True, "hint": ble.REFUSED_HINT,
                               "error": "wifi: camera refused remote wake-up (camera state)"})
            with self.wake(line):
                st, d = await r.call("POST", "/api/cameras/emu/ble/wake", {})
            self.assertEqual(st, 502)
            self.assertTrue(d["refused"])
            self.assertEqual(d["hint"], ble.REFUSED_HINT)
        self.run_(go(), 30)
        with open(os.path.join(STATIC, "app.js")) as f:
            js = f.read()
        self.assertIn("wake.data && wake.data.refused", js)
        self.assertIn("wake.data.hint", js)


class ClientUuidApiTest(EmuTestCase):
    """PATCH client_uuid: restore the id the camera was paired with (BLE MEI0 +
    Wi-Fi req_acc must be the same id; the S5 hangs up on any other)."""

    def test_patch_client_uuid(self):
        r = self.rig

        async def go():
            st, d = await r.call("PATCH", "/api/cameras/emu", {"client_uuid": "4d454930-0100-1000-8001-0a0b0c0d0e0f"})
            self.assertEqual(st, 200, d)
            self.assertEqual(d["entry"]["client_uuid"], "4D454930-0100-1000-8001-0A0B0C0D0E0F")
            st, d = await r.call("PATCH", "/api/cameras/emu", {"client_uuid": "not-an-id"})
            self.assertEqual(st, 400)
            self.assertIn("client_uuid", d["error"])
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
        # ...or when lumix-ble says the camera doesn't know this computer's id
        self.assertIn("wake.needPair = !wake.ok && (wake.unsure || !!(wake.data && wake.data.need_pair))", body)
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
