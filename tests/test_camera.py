"""Camera wedge handling against a scripted fake transport (no camera needed)."""
import isolate  # noqa: F401  (temp config dir; must come first)
import asyncio
import unittest
from unittest import mock

from lumix.camera import Camera, CameraConfig
from lumix.protocol import (MODE_SWITCHES, RATE_LIMITED, CamError, CamTransport, ModeSwitchLimiter,
                            parse_reply)

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

    def _limited(self, cam):
        """FakeTransport with the real ModeSwitchLimiter on a fake clock."""
        clock = [1000.0]
        lim = ModeSwitchLimiter(Camera.MODE_SWITCH_MIN_GAP_S, Camera.MODE_SWITCH_MAX,
                                Camera.MODE_SWITCH_WINDOW_S, clock=lambda: clock[0])
        tr = cam.transport
        tr.limiter = lim
        real = tr.cgi

        async def cgi(mode, type=None, value=None, value2=None, timeout=6.0):
            if mode == "camcmd" and value in MODE_SWITCHES:
                if lim.check():
                    tr.sent.append("NOT SENT %s" % value)
                    return parse_reply("q", 200, (RESULT % RATE_LIMITED).encode(), 0.0)
                lim.note()
            return await real(mode, type, value, value2, timeout)
        tr.cgi = cgi
        return clock

    def test_mode_switch_limiter_caps(self):
        t = [0.0]
        lim = ModeSwitchLimiter(10.0, 3, 60.0, clock=lambda: t[0])
        self.assertIsNone(lim.check())
        lim.note()                                   # t=0
        t[0] = 5.0
        self.assertIn("next allowed in 5.0 s", lim.check())   # 1 per 10 s
        t[0] = 10.0
        self.assertIsNone(lim.check())
        lim.note()
        t[0] = 20.0
        lim.note()
        t[0] = 30.0                                  # 3 in the last 60 s
        self.assertAlmostEqual(lim.wait_s(), 30.0)
        t[0] = 60.0
        self.assertIsNone(lim.check())

    def test_transport_refuses_excess_switches_locally(self):
        t = [0.0]
        refused = []
        lim = ModeSwitchLimiter(10.0, 3, 60.0, clock=lambda: t[0])
        tr = CamTransport("127.0.0.1", port=9, limiter=lim, refused=lambda q, why: refused.append(q))
        lim.note()
        r = self.loop.run_until_complete(tr.cgi("camcmd", value="recmode"))
        self.assertEqual(r.result, RATE_LIMITED)
        r = self.loop.run_until_complete(tr.cgi("camcmd", value="playmode"))
        self.assertEqual(r.result, RATE_LIMITED)
        self.assertEqual(refused, ["mode=camcmd&value=recmode", "mode=camcmd&value=playmode"])
        # anything else is not rate limited (goes to the wire: nothing listens on :9)
        with self.assertRaises(CamError):
            self.loop.run_until_complete(tr.cgi("camcmd", value="capture", timeout=2))
        self.loop.run_until_complete(tr.close())

    def test_cmd_waits_for_a_slot_then_refuses(self):
        cam = self.make(cammode="play", answer="ok")
        clock = self._limited(cam)
        waits = []
        real_sleep = asyncio.sleep

        async def fake_sleep(s, *a, **k):
            waits.append(s)
            clock[0] += s                            # time passes
            await real_sleep(0)

        with mock.patch("lumix.camera.asyncio.sleep", fake_sleep):
            for _ in range(3):
                r = self.loop.run_until_complete(cam.cmd("camcmd", value="recmode"))
                self.assertEqual(r.result, "ok")
            # 3 sent at t, t+10, t+20; the 4th would have to wait ~40 s > MODE_SWITCH_MAX_WAIT_S
            r = self.loop.run_until_complete(cam.cmd("camcmd", value="playmode"))
        self.assertEqual(r.result, RATE_LIMITED)
        self.assertEqual([round(w) for w in waits], [10, 10])
        self.assertEqual(sum(q == "mode=camcmd&value=recmode" for q in cam.transport.sent), 3)
        self.assertEqual(cam.transport.sent[-1], "NOT SENT playmode")
        cam.transport.answer = "err_busy"
        self.assertFalse(self.run_fast(cam.ensure_rec()))
        self.assertFalse(cam.wedged)                         # withheld, not refused

    def test_startstream_needs_a_settled_rec(self):
        cam = self.make(cammode="rec", answer="ok")
        cam.state = {"cammode": "rec"}
        cam._unsettled_at = __import__("time").monotonic()
        self.assertIn("changed mode", cam.stream_blocked())
        cam._unsettled_at -= Camera.STABLE_S
        self.assertIsNone(cam.stream_blocked())
        cam.body_busy = True
        self.assertIn("busy", cam.stream_blocked())
        cam.body_busy = False
        cam.state = {"cammode": "play"}
        cam.transport.cammode = "play"
        self.assertIn("play", cam.stream_blocked())
        why = self.loop.run_until_complete(cam._pre_send("startstream", None, "49199"))
        self.assertIn("play", why)

    def test_no_recmode_passthrough_in_rec_and_no_stray_recstop(self):
        cam = self.make(cammode="rec", answer="ok")
        cam.status = "ready"
        cam.state = {"cammode": "rec", "rec": "off"}
        self.assertIn("not sent", self.run_fast(cam.camcmd("recmode"))["text"])
        self.assertEqual(self.run_fast(cam.video(False))["note"], "was not recording")
        self.assertEqual(cam.transport.sent, [])


if __name__ == "__main__":
    unittest.main()
