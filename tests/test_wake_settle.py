"""A camera woken over Bluetooth must not fall back asleep while we wait for
the post-SUBSCRIBE mod_Play.

Real DC-S5, switch OFF, 2026-10-12 14:30 (tests/fixtures/real-s5-ble-wake-settle-2026-10-12.log):
after the BLE wake the connect reached device_name at 14:30:12.7 and then sent
nothing for 30 s waiting for a mod_Play that never came (the body was already in
play). The getstate after the wait got no reply: the camera had gone back to
sleep. LUMIX Sync starts its 1 Hz getstate right after device_name; so do we now.
The emulator models it with idle_sleep_s (asleep after that long without a
request) and post_subscribe_mod_play=False.
"""
import isolate  # noqa: F401  (temp config dir; must come first)

import asyncio
import os
import tempfile
import unittest
from unittest import mock

from lumix import models
from lumix.protocol import CamError
from test_timelapse_emu import EmuTestCase, Rig, emu_cfg


class WokenCameraSettleTest(EmuTestCase):
    cfg = emu_cfg(post_subscribe_mod_play=False, idle_sleep_s=1.6)

    def setUp(self):
        # server-side settle longer than the camera's idle-sleep (30 s vs ~30 s on the body)
        self._p = mock.patch.object(models.S5.connect, "settle_s", 3.0)
        self._p.start()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self._p.stop()

    def test_settle_wait_keeps_camera_awake_and_reaches_ready(self):
        r = self.rig

        async def go():
            cam = r.sess.cam
            self.assertIs(cam.profile, models.S5)
            self.assertTrue(r.emu.powered, "camera fell asleep during the settle wait")
            ev = [e["text"] for e in cam.events if e["kind"] == "conn"]
            self.assertTrue(any("settled after SUBSCRIBE" in t and "(timeout)" in t for t in ev), ev)
            self.assertFalse(any(e["kind"] == "power" for e in r.emu.log), list(r.emu.log)[-5:])
            # getstate kept flowing during the wait (LUMIX Sync's 1 Hz watch loop)
            gs = [e for e in r.emu.log if e["kind"] == "cgi" and e["q"] == "mode=getstate"]
            self.assertGreaterEqual(len(gs), 3)
            # and the session stays up afterwards
            await asyncio.sleep(2.5)
            self.assertEqual(cam.status, "ready")
            self.assertTrue(r.emu.powered)
        self.run_(go(), 30)


class SilentSettleFailsTest(unittest.TestCase):
    """The old behaviour (no request during the wait) loses the woken camera:
    proves the emulator knob models the incident."""

    def test_silence_puts_camera_to_sleep(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        tmp = tempfile.TemporaryDirectory()
        rig = Rig(loop, os.path.join(tmp.name, "home"),
                  emu_cfg(post_subscribe_mod_play=False, idle_sleep_s=1.0))
        try:
            rig.run(rig.start_emu())
            # an accepted session, then silence
            emu = rig.emu
            from emulator.camera import Session
            emu.sessions["127.0.0.1"] = Session(ip="127.0.0.1", uuid="x", name="t",
                                                first_req=emu.now(), state="ok", last_seen=emu.now())
            emu.handle_cgi("127.0.0.1", {"mode": "getstate"})
            loop.run_until_complete(asyncio.sleep(1.4))
            self.assertFalse(emu.powered)
            self.assertTrue(any(e["kind"] == "power" and "back to sleep" in str(e.get("result", ""))
                                for e in emu.log), list(emu.log)[-3:])
        finally:
            rig.run(rig.stop_all())
            loop.run_until_complete(asyncio.sleep(0.1))
            loop.close()
            asyncio.set_event_loop(None)
            tmp.cleanup()


class PoweredOffClearedOnAcceptTest(EmuTestCase):
    """powered_off (set by our poweroff) ends when the camera accepts us again: a
    later failure in the same connect is reported as what it is, not as
    "camera powered off" (14:30:48 said powered off about a camera that had just
    answered req_acc)."""

    def test_failure_after_accept_is_not_powered_off(self):
        r = self.rig

        async def go():
            cam = r.sess.cam
            cam.powered_off = True                      # as after camcmd poweroff
            n0 = len(cam.events)

            def lost():
                return [e["text"] for e in list(cam.events)[n0:] if e["text"].startswith("connection lost")]
            with mock.patch.object(cam, "_full_setup", side_effect=CamError("TimeoutError: ")):
                self.assertTrue(cam.request_reconnect())
                await r.wait(lambda: len(lost()) >= 2, 15, "drop + failed connect after accept")
            msgs = lost()
            self.assertEqual(msgs[0], "connection lost: camera powered off")   # before the accept
            self.assertNotIn("powered off", msgs[1])                            # after it
            self.assertFalse(cam.powered_off)
            await r.wait(lambda: cam.status == "ready", 20, "reconnected")
        self.run_(go(), 40)


if __name__ == "__main__":
    unittest.main()
