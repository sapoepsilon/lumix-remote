"""Camera wedge handling against a scripted fake transport (no camera needed)."""
import isolate  # noqa: F401  (temp config dir; must come first)
import asyncio
import unittest
from unittest import mock

from lumix.camera import Camera, CameraConfig
from lumix.protocol import parse_reply

STATE = ('<?xml version="1.0"?><camrply><result>ok</result><state>'
         '<cammode>%s</cammode></state></camrply>')
RESULT = '<?xml version="1.0"?><camrply><result>%s</result></camrply>'


class FakeTransport:
    """Answers getstate with ``cammode`` and every other request with ``answer``."""

    def __init__(self, cam, cammode="play", answer="err_busy"):
        self.cam, self.cammode, self.answer = cam, cammode, answer
        self.sent = []

    async def cgi(self, mode, type=None, value=None, value2=None, timeout=6.0):
        q = "mode=%s" % mode + ("&type=%s" % type if type else "") + ("&value=%s" % value if value else "")
        self.sent.append(q)
        text = STATE % self.cammode if mode == "getstate" else RESULT % self.answer
        rep = parse_reply(q, 200, text.encode(), 0.0)
        self.cam._trace(q, rep)
        return rep


class WedgeTest(unittest.TestCase):
    def run(self, result=None):
        # one event loop per test: Camera() needs a running loop on Python 3.9
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            return super().run(result)
        finally:
            self.loop.close()
            asyncio.set_event_loop(None)

    def make(self, **kw):
        cam = Camera(CameraConfig(lv_port=0, event_port=0))
        cam.transport = FakeTransport(cam, **kw)
        return cam

    def run_fast(self, coro):
        real_sleep = asyncio.sleep

        async def no_wait(_s, *a, **k):
            await real_sleep(0)

        with mock.patch("lumix.camera.asyncio.sleep", no_wait):
            return self.loop.run_until_complete(coro)

    def test_recmode_refused_from_play_flags_wedge_after_one_command(self):
        cam = self.make(cammode="play", answer="err_busy")
        ok = self.run_fast(cam.ensure_rec())
        self.assertFalse(ok)
        self.assertTrue(cam.wedged)
        self.assertEqual(sum(q == "mode=camcmd&value=recmode" for q in cam.transport.sent), Camera.RECMODE_TRIES)

    def test_recmode_ok_does_not_flag(self):
        cam = self.make(cammode="rec", answer="ok")
        self.assertTrue(self.run_fast(cam.ensure_rec()))
        self.assertFalse(cam.wedged)

    def test_body_busy_is_not_a_wedge(self):
        cam = self.make(cammode="play", answer="err_busy")
        cam.body_busy = True
        self.run_fast(cam.ensure_rec())
        self.assertFalse(cam.wedged)

    def test_wedged_camera_gets_no_further_camcmd(self):
        cam = self.make(cammode="play", answer="err_busy")
        self.run_fast(cam.ensure_rec())
        cam.transport.sent.clear()
        self.assertFalse(self.run_fast(cam.start_liveview("test")))
        self.assertEqual([q for q in cam.transport.sent if "camcmd" in q or "stream" in q], [])

    def test_stop_liveview_without_stream_sends_nothing(self):
        cam = self.make(answer="err_critical")
        self.run_fast(cam.stop_liveview("file browsing"))
        self.assertEqual(cam.transport.sent, [])
        cam._lv_running = True
        self.run_fast(cam.stop_liveview("file browsing"))
        self.assertEqual(cam.transport.sent, ["mode=stopstream"])
        self.assertFalse(cam._lv_running)

    def test_capture_refused_for_whole_budget_flags_wedge(self):
        """Emulator report: capture err_busy x5 on a healthy session left wedged=false."""
        cam = self.make(cammode="rec", answer="err_busy")
        cam.status = "ready"
        fired = []

        async def hook(c):
            fired.append(c)
        cam.wedge_hooks.append(hook)
        res = self.run_fast(cam.capture())
        self.assertEqual(res["result"], "err_busy")
        self.assertTrue(cam.wedged)
        self.assertEqual(sum(q == "mode=camcmd&value=capture" for q in cam.transport.sent), 5)
        self.run_fast(asyncio.sleep(0))
        self.assertEqual(len(fired), 1)

    def test_single_busy_answer_is_not_a_wedge(self):
        cam = self.make(cammode="rec", answer="ok")
        answers = iter(["err_busy", "ok"])
        real = cam.transport.cgi

        async def cgi(mode, type=None, value=None, value2=None, timeout=6.0):
            if mode == "camcmd":
                cam.transport.answer = next(answers, "ok")
            return await real(mode, type, value, value2, timeout)
        cam.transport.cgi = cgi
        self.run_fast(cam.cmd("camcmd", value="oneshot_af"))
        self.assertFalse(cam.wedged)

    def test_busy_body_capture_is_not_a_wedge(self):
        cam = self.make(cammode="rec", answer="err_busy")
        self.run_fast(cam.cmd("camcmd", value="capture"))
        self.assertTrue(cam.wedged)
        cam2 = self.make(cammode="rec", answer="err_busy")
        cam2.body_busy = True
        self.run_fast(cam2.cmd("camcmd", value="capture"))
        self.assertFalse(cam2.wedged)

    def test_recmode_churn_guard_waits_for_a_slot(self):
        cam = self.make(cammode="play", answer="ok")
        waits = []
        real_sleep = asyncio.sleep

        async def fake_sleep(s, *a, **k):
            waits.append(s)
            for i in range(len(cam._recmode_sent)):          # time passes
                cam._recmode_sent[i] -= s
            await real_sleep(0)

        with mock.patch("lumix.camera.asyncio.sleep", fake_sleep):
            for _ in range(4):
                self.loop.run_until_complete(cam.cmd("camcmd", value="recmode"))
        self.assertEqual(sum(q == "mode=camcmd&value=recmode" for q in cam.transport.sent), 4)
        self.assertEqual(len(waits), 1)                      # only the 4th waited
        self.assertGreater(waits[0], 19)                     # ~20 s after the first one
        now = __import__("time").monotonic()
        self.assertLessEqual(sum(now - t < Camera.RECMODE_WINDOW_S for t in cam._recmode_sent), 3)

    def test_recmode_guard_refuses_when_it_may_not_wait(self):
        cam = self.make(cammode="play", answer="ok")
        cam.RECMODE_MAX_WAIT_S = 0
        for _ in range(4):
            r = self.run_fast(cam.cmd("camcmd", value="recmode"))
        self.assertEqual(r.result, "err_local_recmode_guard")
        self.assertEqual(sum(q == "mode=camcmd&value=recmode" for q in cam.transport.sent), 3)
        cam.transport.answer = "err_busy"
        self.assertFalse(self.run_fast(cam.ensure_rec()))
        self.assertFalse(cam.wedged)                         # withheld, not refused

    def test_no_recmode_passthrough_in_rec_and_no_stray_recstop(self):
        cam = self.make(cammode="rec", answer="ok")
        cam.status = "ready"
        cam.state = {"cammode": "rec", "rec": "off"}
        self.assertIn("not sent", self.run_fast(cam.camcmd("recmode"))["text"])
        self.assertEqual(self.run_fast(cam.video(False))["note"], "was not recording")
        self.assertEqual(cam.transport.sent, [])


if __name__ == "__main__":
    unittest.main()
