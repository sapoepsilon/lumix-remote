"""Server time-lapse vs. a camera that leaves rec mode on its own.

Regression for the real-S5 wedge on 2026-10-12 16:46-16:52
(logs/camera-2026-10-12.log lines 9450-9560, the server's NOTIFY list,
tests/fixtures/real-s5-photo-fault-wedge-2026-10-12.log). Job 20261012-1146,
server engine, single drive (getsetting drivemode=normal at 16:36:24, line 9079;
interval_status off in every rec getstate), 30 s interval:

* 16:46:37.8 the body sent mod_Play and went to play with nothing but reads
  sent, 13 s after a good shot (line 9460);
* the old job answered every drop with recmode + startstream: recmodes at
  16:46:55, 16:48:25, 16:49:55, 16:50:55 (lines 9462, 9492, 9509, 9525),
  startstream err_busy into the camera's own switches (9466, 9495, 9514, 9529);
* every capture after the first drop: ok, then X_Panasonic_Cam_PRec=err_rec,
  mod_Play and no photo (remaincapacity stuck at 1849), but the job counted
  them as shots;
* 16:51:31 getstate err_busy, 16:52:05 capture err_critical, req_acc
  err_critical for good: wedged until a power cycle (lines 9549-9560).

The emulator models that sequence (EmuConfig.photo_fault_*); timings here are
scaled by S (0.2) so a 30 s interval takes 6 s. The old code sends >= 4
recmodes inside the (scaled) 5 min window and wedges the emulator; the new code
waits for the camera, sends at most 2 recmodes per 5 min, never startstreams
into a switch, does not count failed photos and pauses the job with a reason.
"""
import isolate  # noqa: F401  (temp config dir; must come first)

import asyncio
import time
import unittest
from unittest import mock

import lumix.timelapse as timelapse_mod
from lumix.camera import Camera, CameraConfig
from test_timelapse_emu import EmuTestCase, emu_cfg

S = 0.2     # time scale: real seconds * S

FAULT_CFG = emu_cfg(
    photo_fault_after=2, fault_play_after_s=13.2 * S, fault_lens_s=1.0 * S,
    fault_err_rec_s=3.7 * S, fault_bounce_play_s=1.3 * S, fault_bounce_back_s=3.0 * S,
    fault_stream_settle_s=2.0 * S, fault_wedge_window_s=300 * S, fault_wedge_after_s=6.6 * S,
    churn_window_s=20 * S)

SCALED_CAMERA = dict(
    MODE_SWITCH_MIN_GAP_S=10 * S, MODE_SWITCH_WINDOW_S=60 * S, MODE_SWITCH_MAX_WAIT_S=15 * S,
    STABLE_S=2.0 * S, STABLE_WAIT_S=8.0 * S,
    AUTO_REC_WAIT_S=20 * S, AUTO_REC_WINDOW_S=300 * S)


def recmode_times(log, since):
    return [e["t"] for e in log if e["kind"] == "cgi" and e["t"] >= since
            and "value=recmode" in e["q"]]


def max_in_window(times, window):
    return max([sum(1 for u in times if t <= u < t + window) for t in times] or [0])


class PhotoFaultTest(EmuTestCase):
    """The 16:46 sequence: the camera drops to play, fails every photo after
    that and bounces around our recmodes."""
    cfg = FAULT_CFG
    camera_timing = SCALED_CAMERA

    def setUp(self):
        p = mock.patch.object(timelapse_mod, "REC_RETRY_S", 60 * S)   # old code's recmode cadence
        p.start()
        self.addCleanup(p.stop)
        super().setUp()

    def test_no_recmode_fight_and_job_pauses(self):
        r = self.rig

        async def go():
            photos0 = r.emu.card.counts()[0]
            st, d = await r.call("POST", "/api/timelapse/start",
                                 {"interval": 30 * S, "count": 40, "name": "fault"})
            self.assertEqual(st, 201, d)
            await r.wait(lambda: r.emu.faulted, 40, "camera drops to play (fault)")
            t_fault = time.time()
            # the new code pauses the job; the old one keeps fighting until the
            # emulator wedges (that ends the wait too)
            await r.wait(lambda: r.job().state == "paused" or r.emu.hard_wedged, 150,
                         "job paused (or camera wedged)")
            await asyncio.sleep(2 * 30 * S)        # nothing more is sent after the pause
            rec = recmode_times(r.emu.log, t_fault)
            self.assertLessEqual(
                max_in_window(rec, 300 * S), Camera.AUTO_REC_MAX if hasattr(Camera, "AUTO_REC_MAX") else 2,
                "recmodes into the struggling camera at +%s s" % [round(t - t_fault, 1) for t in rec])
            self.assertFalse(r.emu.hard_wedged, r.emu.wedge_reason)
            self.assertFalse(r.emu.wedged, r.emu.wedge_reason)
            busy = [e["q"] for e in r.emu.log if e["kind"] == "cgi" and e["t"] >= t_fault
                    and "mode=startstream" in e["q"] and e["result"] != "ok"]
            self.assertEqual(busy, [], "startstream refused by the camera")
            j = r.job()
            self.assertEqual((j.state, j.paused_by), ("paused", "camera"))
            self.assertIn("protect the camera", j.message or "")
            # only real photos count: 2 good ones before the fault
            self.assertEqual(j.shots, 2)
            self.assertEqual(r.emu.card.counts()[0] - photos0, 2)
            self.assertGreaterEqual(j.failed, 1)
            self.assertTrue(any("err_rec" in e for e in j.errors), j.errors)
            # paused: the camera gets no more mode switches
            self.assertEqual(recmode_times(r.emu.log, time.time() - 30 * S), [])
        self.run_(go(), 240)


class SelfPlayExcursionTest(EmuTestCase):
    """The camera goes to play by itself and comes back ~3 s later (16:46:58.2
    play -> 16:47:01.3 rec): the job waits, sends no recmode, keeps shooting."""
    cfg = emu_cfg(fault_bounce_back_s=3.0 * S)
    camera_timing = SCALED_CAMERA

    def test_waits_for_the_camera_without_a_recmode(self):
        r = self.rig

        async def go():
            st, d = await r.call("POST", "/api/timelapse/start",
                                 {"interval": 30 * S, "count": 4, "name": "excursion"})
            self.assertEqual(st, 201, d)
            await r.wait(lambda: r.job().shots >= 1, 20, "first shot")
            await asyncio.sleep(0.5)
            t0 = time.time()
            r.emu.body_action("self_play")
            await r.wait(lambda: r.job().state == "done", 90, "done")
            self.assertEqual(recmode_times(r.emu.log, t0), [])
            self.assertEqual(r.job().shots, 4)
            bad = [e for e in r.emu.log if e["kind"] == "cgi" and e["t"] >= t0
                   and "mode=startstream" in e["q"] and e["result"] != "ok"]
            self.assertEqual(bad, [])
            self.assertFalse(r.emu.wedged)
        self.run_(go(), 120)


class AutoRecPolicyTest(unittest.TestCase):
    """Camera.auto_rec decisions against a scripted transport."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()
        asyncio.set_event_loop(None)

    def make(self, cammode):
        from test_camera import FakeTransport
        cam = Camera(CameraConfig(lv_port=0, event_port=0))
        cam.transport = FakeTransport(cam, cammode=cammode, answer="ok")
        self.loop.run_until_complete(cam._refresh_state())
        return cam

    def recmodes(self, cam):
        return sum(q == "mode=camcmd&value=recmode" for q in cam.transport.sent)

    def run_(self, coro):
        return self.loop.run_until_complete(coro)

    def test_waits_then_one_recmode_then_gives_up(self):
        cam = self.make("play")
        cam._mode_changed_at = time.monotonic() - 3          # dropped to play 3 s ago
        st, why = self.run_(cam.auto_rec())
        self.assertEqual(st, "wait")
        self.assertIn("nothing sent", why)
        self.assertEqual(self.recmodes(cam), 0)
        cam._mode_changed_at = time.monotonic() - Camera.AUTO_REC_WAIT_S - 1
        st, why = self.run_(cam.auto_rec())                   # one recmode for this episode
        self.assertEqual((st, self.recmodes(cam)), ("wait", 1))
        st, why = self.run_(cam.auto_rec())                   # still play: no second one
        self.assertEqual((st, self.recmodes(cam)), ("wait", 1))
        cam._auto_rec_sent_at -= Camera.AUTO_REC_WAIT_S + 1
        st, why = self.run_(cam.auto_rec())
        self.assertEqual((st, self.recmodes(cam)), ("give_up", 1))
        self.assertIn("after one recmode", why)

    def new_drop(self, cam):
        """The camera went back to rec after our recmode and dropped to play again
        (AUTO_REC_WAIT_S + 1 s ago)."""
        cam._mode_changed_at = time.monotonic() - Camera.AUTO_REC_WAIT_S - 1
        if cam._auto_rec_sent_at is not None:
            cam._auto_rec_sent_at = cam._mode_changed_at - 1

    def test_budget_of_two_per_five_minutes(self):
        cam = self.make("play")
        for n in (1, 2):
            self.new_drop(cam)
            self.assertEqual(self.run_(cam.auto_rec())[0], "wait")
            self.assertEqual(self.recmodes(cam), n)
        self.new_drop(cam)                                    # third drop within 5 min
        st, why = self.run_(cam.auto_rec())
        self.assertEqual((st, self.recmodes(cam)), ("give_up", 2))
        self.assertIn("keeps leaving rec mode", why)
        cam.reset_auto_rec()                                  # explicit Resume
        self.assertEqual(self.run_(cam.auto_rec())[0], "wait")
        self.assertEqual(self.recmodes(cam), 3)

    def test_rec_is_ready_only_when_settled(self):
        cam = self.make("rec")
        cam._unsettled_at = time.monotonic()
        self.assertEqual(self.run_(cam.auto_rec())[0], "wait")
        cam._unsettled_at -= Camera.STABLE_S
        self.assertEqual(self.run_(cam.auto_rec()), ("ready", ""))
        self.assertEqual(self.recmodes(cam), 0)


if __name__ == "__main__":
    unittest.main()
