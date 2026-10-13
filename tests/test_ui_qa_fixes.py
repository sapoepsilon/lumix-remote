"""Server side of the 2026-10-12 headless-browser QA pass (F1 F3 F4 F5 F6 F7 F9
F18), against the emulator. The UI side was checked with Playwright against
the emulator (not part of this suite)."""
import isolate  # noqa: F401  (temp config dir; must come first)

import asyncio
import os
import tempfile
import time
import unittest

from lumix.registry import Registry
from server.sessions import ServerOptions, SessionManager
from test_timelapse_emu import EmuTestCase, emu_cfg


def _camcmds(emu, value):
    return [e for e in emu.log if e["kind"] == "cgi" and "mode=camcmd" in e["q"]
            and "value=%s" % value in e["q"]]


class FilesLeaveQueuedTest(EmuTestCase):
    """F1: closing Files while the switch to playback is still running. The
    quiesce hook (pausing a timelapse, stopping a recording) is slowed down so
    the done request lands in the part of the enter before play mode is set."""

    def setUp(self):
        super().setUp()
        fb = self.rig.sess.files
        hook = fb.on_enter

        async def slow_enter():
            await asyncio.sleep(1.0)
            if hook is not None:
                await hook()
        fb.on_enter = slow_enter

    def test_leave_during_enter_is_queued_and_runs_after_it(self):
        r = self.rig
        fb = r.sess.files

        async def go():
            await r.wait(lambda: r.sess.cam.liveview.age is not None, 10, "frames")
            listing = asyncio.ensure_future(r.call("GET", "/api/files?limit=60"))
            await r.wait(lambda: fb._entering > 0, 5, "enter in flight")
            st, d = await r.call("POST", "/api/files/done", {})
            self.assertEqual(st, 200, d)
            self.assertTrue(d.get("queued"), d)
            await listing
            # the queued leave ran: back in rec with live view wanted again
            await r.wait(lambda: not fb.active and r.sess.cam.state.get("cammode") == "rec", 15,
                         "back to rec")
            self.assertTrue(r.sess.cam.liveview_wanted)
            self.assertIsNone(fb._leave_pending)
            # the recmode respected the transport's mode-switch gap after the playmode
            play, rec = _camcmds(r.emu, "playmode")[-1], _camcmds(r.emu, "recmode")[-1]
            self.assertGreaterEqual(rec["t"] - play["t"], r.sess.cam.MODE_SWITCH_MIN_GAP_S - 0.05)
        self.run_(go(), 40)

    def test_reopening_files_drops_the_queued_leave(self):
        r = self.rig
        fb = r.sess.files

        async def go():
            first = asyncio.ensure_future(r.call("GET", "/api/files?limit=60"))
            await r.wait(lambda: fb._entering > 0, 5, "enter in flight")
            st, d = await r.call("POST", "/api/files/done", {})
            self.assertTrue(d.get("queued"), d)
            st, d = await r.call("GET", "/api/files?limit=60")     # user is back in Files
            self.assertEqual(st, 200, d)
            await first
            await asyncio.sleep(1.5)
            self.assertTrue(fb.active)
            self.assertEqual(r.sess.cam.state.get("cammode"), "play")
            await r.call("POST", "/api/files/done", {})
        self.run_(go(), 40)


class ImportsOutliveFilesTest(EmuTestCase):
    """F6: leaving Files mid-import keeps the import going (UI sends keep_imports)."""
    cfg = emu_cfg(dl_rate=400000)

    def test_import_continues_then_camera_goes_back_to_shooting(self):
        r = self.rig
        fb = r.sess.files

        async def go():
            st, d = await r.call("GET", "/api/files?limit=60")
            ids = [x["id"] for x in d["items"]][:3]
            st, j = await r.call("POST", "/api/files/import", {"ids": ids, "dest": self.dest})
            self.assertEqual(st, 202, j)
            await r.wait(lambda: fb.jobs[j["id"]].state == "running", 5, "import running")
            st, d = await r.call("POST", "/api/files/done", {"keep_imports": True})
            self.assertEqual(st, 200, d)
            self.assertEqual((d["left"], d["importing"], d["leave_after_jobs"]), (False, [j["id"]], True))
            self.assertEqual(fb.jobs[j["id"]].state, "running")           # not paused
            await r.wait(lambda: fb.jobs[j["id"]].state == "done", 60, "import done")
            await r.wait(lambda: not fb.active and r.sess.cam.state.get("cammode") == "rec", 20,
                         "back to shooting after the import")
            self.assertFalse(fb.leave_after_jobs)
        self.run_(go(), 90)

    def test_plain_done_still_pauses_imports(self):
        # the CLI's `lumix files done` keeps its meaning
        r = self.rig
        fb = r.sess.files

        async def go():
            st, d = await r.call("GET", "/api/files?limit=60")
            st, j = await r.call("POST", "/api/files/import", {"ids": [d["items"][0]["id"]], "dest": self.dest})
            await r.wait(lambda: fb.jobs[j["id"]].state == "running", 5, "import running")
            st, d = await r.call("POST", "/api/files/done", {})
            self.assertEqual(d["paused_jobs"], [j["id"]])
            self.assertEqual(fb.jobs[j["id"]].state, "paused")
        self.run_(go(), 60)


class ShutterGuardsTest(EmuTestCase):
    def test_photo_in_time_lapse_drive_is_refused_with_a_code(self):
        # F4: the recorded S5 sits in Drive = Time Lapse; the photo shutter would
        # start the camera's own interval run
        r = self.rig

        async def go():
            await r.wait(lambda: r.sess.cam.state.get("burst_interval_status") == "on", 10, "drive state")
            st, d = await r.call("POST", "/api/capture")
            self.assertEqual(st, 409, d)
            self.assertEqual(d["code"], "drive_timelapse")
            self.assertIn("Drive is Time Lapse", d["error"])
            self.assertEqual(_camcmds(r.emu, "capture"), [])
            self.assertEqual(r.emu.interval["state"], "enable")          # no run started
            # the one-tap fix in the UI: drive to Single, then the shutter works
            st, d = await r.call("POST", "/api/setting", {"type": "drivemode", "value": "normal"})
            self.assertEqual((st, d.get("result")), (200, "ok"), d)
            st, d = await r.call("POST", "/api/capture")
            self.assertEqual((st, d.get("result")), (200, "ok"), d)
            self.assertEqual(r.emu.interval["state"], "enable")
        self.run_(go(), 40)

    def test_wedged_shutter_answers_at_once(self):
        # F5: known wedge -> immediate 409 with a human sentence, nothing sent
        r = self.rig

        async def go():
            r.sess.cam.wedged = True
            n = len(r.emu.log)
            t0 = time.monotonic()
            st, d = await r.call("POST", "/api/capture")
            self.assertEqual(st, 409, d)
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertIn("stuck", d["error"])
            self.assertNotIn("err_busy", d["error"])
            self.assertFalse([e for e in list(r.emu.log)[n:] if "mode=camcmd" in e.get("q", "")])
            st, d = await r.call("POST", "/api/af")
            self.assertEqual(st, 409, d)
            r.sess.cam.wedged = False
        self.run_(go(), 20)

    def test_playback_shutter_fails_fast_when_the_switch_would_wait(self):
        # F5: in playback with the mode-switch limiter full: say so now
        r = self.rig

        async def go():
            r.emu.settings["drivemode"] = ("normal", "")
            await r.ctl("/emu/body/play")
            await r.wait(lambda: r.sess.cam.state.get("cammode") == "play", 10, "camera in play")
            lim = r.sess.cam.transport.limiter
            lim.wait_s = lambda: 9.0
            t0 = time.monotonic()
            st, d = await r.call("POST", "/api/capture")
            self.assertEqual(st, 409, d)
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertIn("playback", d["error"])
        self.run_(go(), 20)


class TimelapseWedgedTest(EmuTestCase):
    def test_timelapse_refuses_a_wedged_camera(self):
        # F3
        r = self.rig

        async def go():
            r.sess.cam.wedged = True
            st, d = await r.call("POST", "/api/timelapse/start", {"interval": 2, "count": 3})
            self.assertEqual(st, 409, d)
            self.assertIn("stuck", d["error"])
            self.assertIsNone(r.sess.timelapse.job)
            r.sess.cam.wedged = False
        self.run_(go(), 20)


class FilesNoCameraTest(EmuTestCase):
    def test_files_fail_fast_without_a_camera(self):
        # F7: was 20 s of skeleton tiles
        r = self.rig

        async def go():
            await r.ctl("/emu/power-off")
            await r.wait(lambda: r.sess.cam.status not in ("ready", "connecting", "waiting_approval"),
                         30, "camera gone")
            t0 = time.monotonic()
            st, d = await r.call("GET", "/api/files?limit=60")
            self.assertEqual(st, 409, d)
            self.assertLess(time.monotonic() - t0, 1.5)
            self.assertIn("not connected", d["error"])
            await r.ctl("/emu/power-on")
        self.run_(go(), 60)


class ReconnectIpValidationTest(EmuTestCase):
    def test_invalid_addresses_are_rejected(self):
        # F9
        r = self.rig

        async def go():
            for bad in ("abc", "999.1.1.1", "1.2.3", "::1"):
                st, d = await r.call("POST", "/api/connection/reconnect", {"ip": bad})
                self.assertEqual(st, 400, (bad, d))
                self.assertIn("IPv4", d["error"])
            st, d = await r.call("POST", "/api/cameras", {"nickname": "X", "ip": "999.1.1.1"})
            self.assertEqual(st, 400, d)
        self.run_(go(), 20)


class EffectivePortsTest(unittest.TestCase):
    def test_summary_shows_the_command_line_live_view_port(self):
        # F18: camera details showed 49199 although the server ran with --lv-port
        async def go():
            with tempfile.TemporaryDirectory() as d:
                reg = Registry(os.path.join(d, "cameras.json"))
                reg.add("Cam", cam_id="cam", activate=True)
                mgr = SessionManager(reg, ServerOptions(lv_port=5601, scan=False))
                mgr.load()
                s = mgr.get("cam").summary()
                self.assertEqual(s["effective_ports"]["liveview"], 5601)
                self.assertEqual(s["effective_ports"]["http"], 80)
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
