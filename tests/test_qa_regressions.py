"""Regressions from the 2026-10-12 emulator QA pass (docs/qa-matrix.md).

Against the in-process emulator (see test_timelapse_emu.Rig): a camera that
restarts between two keepalives, stopstream discipline on stop, the Auto preset
when the camera refuses everything, and background-task cleanup.
"""
import isolate  # noqa: F401  (temp config dir; must come first)

import asyncio
import unittest

from lumix.camera import Camera, CameraConfig
from test_timelapse_emu import EmuTestCase


def _stopstream_critical(emu):
    return [e for e in emu.log if e["kind"] == "cgi" and e["q"].startswith("mode=stopstream")
            and e["result"] == "err_critical"]


class RebootDetectionTest(EmuTestCase):
    def test_command_after_camera_restart_reconnects(self):
        r = self.rig

        async def go():
            await r.wait(lambda: r.sess.cam.liveview.age is not None, 10, "frames")
            await r.ctl("/emu/reset")            # fresh boot: getstate still answers
            st, d = await r.call("POST", "/api/touch", {"x": 500, "y": 500})
            self.assertEqual(d["result"], "err_reject")
            await r.wait(lambda: r.sess.cam.status != "ready", 10, "session dropped")
            await r.wait(lambda: r.sess.cam.status == "ready", 20, "reconnected")
            st, d = await r.call("POST", "/api/touch", {"x": 500, "y": 500})
            self.assertEqual(d["result"], "ok")
            self.assertTrue(any("no longer accepts this session" in e["text"]
                                for e in r.sess.cam.events))
        self.run_(go(), 60)

    def test_restart_noticed_without_any_command(self):
        r = self.rig

        async def go():
            cam = r.sess.cam
            cam.PROBE_STALE_S, cam.PROBE_FRAME_AGE_S = 1.0, 1.0
            await r.wait(lambda: cam.liveview.age is not None, 10, "frames")
            await r.ctl("/emu/reset")
            await r.wait(lambda: cam.status != "ready", 15, "restart noticed")
            await r.wait(lambda: cam.status == "ready" and cam.liveview.age is not None
                         and cam.liveview.age < 1, 20, "live view back")
        self.run_(go(), 60)


class StopstreamDisciplineTest(EmuTestCase):
    def test_no_stopstream_without_a_stream(self):
        r = self.rig

        async def go():
            await r.wait(lambda: r.sess.cam.liveview.age is not None, 10, "frames")
            await r.stop_server()                # quiesce stops the stream, then Camera.stop()
            self.assertEqual(_stopstream_critical(r.emu), [])
        self.run_(go(), 30)

    def test_no_stopstream_after_session_drop_without_stream(self):
        r = self.rig

        async def go():
            await r.wait(lambda: r.sess.cam.liveview.age is not None, 10, "frames")
            await r.call("GET", "/api/files?limit=1")       # file browsing: play mode
            self.assertFalse(r.sess.cam._lv_running)     # play mode: no stream of ours
            await r.ctl("/emu/drop-session")
            await r.wait(lambda: r.sess.cam.status != "ready", 10, "drop noticed")
            await r.wait(lambda: r.sess.cam.status == "ready", 20, "reconnected")
            self.assertEqual(_stopstream_critical(r.emu), [])
        self.run_(go(), 60)


class AutoRefusedTest(EmuTestCase):
    def test_auto_not_flipped_when_camera_refuses(self):
        r = self.rig

        async def go():
            await r.ctl("/emu/reset")            # session gone, getstate still ok
            st, d = await r.call("POST", "/api/presets/auto", {"on": True, "target": "photo"})
            self.assertEqual(st, 409, d)
            self.assertIn("Auto not applied", d["error"])
            st, d = await r.call("GET", "/api/presets")
            self.assertFalse(d["auto"]["on"])
        self.run_(go(), 30)


class BackgroundTaskTest(unittest.TestCase):
    def test_stop_cancels_spawned_tasks(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            async def go():
                cam = Camera(CameraConfig(lv_port=0, event_port=0))
                t = cam._spawn(asyncio.sleep(3600))
                await asyncio.sleep(0)
                await cam.stop()
                return t
            t = loop.run_until_complete(go())
            self.assertTrue(t.done())
            pending = [x for x in asyncio.all_tasks(loop) if not x.done()]
            self.assertEqual(pending, [])
        finally:
            loop.close()
            asyncio.set_event_loop(None)


if __name__ == "__main__":
    unittest.main()


class ShootingLeavesFilesTest(EmuTestCase):
    def test_capture_while_browsing_switches_back(self):
        r = self.rig

        async def go():
            st, d = await r.call("GET", "/api/files?limit=1")
            self.assertEqual(st, 200)
            self.assertEqual(r.emu.cammode, "play")
            st, d = await r.call("POST", "/api/capture")
            self.assertEqual((st, d["result"]), (200, "ok"))
            self.assertFalse(r.sess.files.active)
            self.assertEqual(r.sess.cam.state.get("cammode"), "rec")
        self.run_(go(), 40)

    def test_capture_during_import_is_refused_with_reason(self):
        r = self.rig

        async def go():
            st, d = await r.call("GET", "/api/files?limit=2")
            r.emu.cfg.dl_rate = 50000           # slow card reads: the import keeps running
            st, j = await r.call("POST", "/api/files/import", {"ids": [d["items"][0]["id"]], "dest": self.dest})
            self.assertEqual(st, 202)
            await r.wait(lambda: r.sess.files.running_jobs(), 5, "import running")
            st, d = await r.call("POST", "/api/capture")
            self.assertEqual(st, 409)
            self.assertIn("import", d["error"])
            r.emu.cfg.dl_rate = 0
            await r.call("POST", "/api/jobs/%s/cancel" % j["id"])
        self.run_(go(), 40)
