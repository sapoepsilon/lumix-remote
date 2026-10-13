"""Chaos harness smoke + deterministic repros of what it found.

tests/chaos_harness.py fires random concurrent UI traffic at the server and the
emulator and checks the camera-safety invariants. The suite runs a short
smoke; long runs are opt-in:

    LUMIX_CHAOS_SEEDS=1-50 .venv/bin/python -m unittest tests.test_chaos   (from tests/: -s tests)
    .venv/bin/python tools/chaos.py --seeds 1-50

Every failure a seed found is minimized into a test below (emulator-verified
only; the real-S5 operator confirms the fixes on the body).
"""
import isolate  # noqa: F401  (temp config dir; must come first)

import asyncio
import os
import time
import unittest

import chaos_harness
from test_timelapse_emu import EmuTestCase


class ShutterInTimeLapseDriveTest(EmuTestCase):
    """Chaos seed 2 (and 1): a quick-control setting put the drive mode on Time
    Lapse, then the UI shutter started the camera's own interval run (the S5
    does that for a shutter press in that drive mode; the body's button does it
    too). The server refused every
    command with "stop the timelapse first" while /api/timelapse/stop answered
    "no active timelapse to stop": nothing in the UI could stop the run until a
    server restart adopted it."""
    cfg = chaos_harness.chaos_emu_cfg(interval_scale=1.0)

    def test_run_started_by_the_shutter_can_be_stopped(self):
        r = self.rig

        async def go():
            st, d = await r.call("POST", "/api/setting", {"type": "drivemode", "value": "stopmotion_interval",
                                                          "value2": "interval"})
            self.assertEqual(st, 200, d)
            # the shutter (the UI's before the drive-mode check, or the body's button;
            # here sent to the emulator directly) starts the camera's own run
            r.emu.handle_cgi("127.0.0.1", {"mode": "camcmd", "value": "capture"})
            await r.wait(lambda: r.emu.interval["state"] == "rec", 10, "camera interval run")
            await r.wait(lambda: r.sess.cam.interval_running, 10, "server sees the run")
            await r.wait(lambda: r.sess.timelapse.is_active(), 10, "run adopted as a job")
            st, d = await r.call("GET", "/api/timelapse")
            self.assertTrue(d["active"], d)                     # shown, with Pause/Stop
            self.assertEqual(d["job"]["engine"], "native")
            st, d = await r.call("POST", "/api/timelapse/stop")
            self.assertEqual(st, 200, d)
            await r.wait(lambda: r.emu.interval["state"] not in ("rec", "pause"), 15, "run stopped")
            self.assertFalse(r.emu.wedged, r.emu.wedge_reason)
        self.run_(go(), 60)


class ModeSwitchCapAcrossRestartTest(EmuTestCase):
    """Found with the chaos harness' server restarts: the mode-switch limiter
    lived in the process, so a new server (restart, crash loop under a process
    supervisor) sent its handshake recmode right after the old one's playmode.
    Each restart could add one more switch -- the recmode churn that wedged the
    S5 (4th recmode in 20 s, FINDINGS 22:01). The switch times now survive the
    restart in cameras.json and the new server waits for the cap."""
    camera_timing = dict(chaos_harness.FAST_CAMERA, MODE_SWITCH_MIN_GAP_S=4.0, MODE_SWITCH_MAX_WAIT_S=6.0)

    def test_new_server_waits_for_the_previous_ones_switches(self):
        r = self.rig
        sent = []
        orig = r.emu.record

        def record(kind, what, result, ip=""):
            if kind == "cgi" and ("value=playmode" in what or "value=recmode" in what):
                sent.append((time.monotonic(), what.split("value=")[1]))
            return orig(kind, what, result, ip)
        r.emu.record = record

        async def go():
            st, d = await r.call("GET", "/api/files?limit=2")     # playmode
            self.assertEqual(st, 200, d)
            self.assertEqual(sent[-1][1], "playmode")
            await r.stop_server()
            await r.start_server()                                # camera in play: recmode
            await r.wait(lambda: sent[-1][1] == "recmode", 15, "recmode from the new server")
            gaps = [b[0] - a[0] for a, b in zip(sent, sent[1:])]
            self.assertTrue(all(g >= 4.0 - 0.05 for g in gaps), (sent, gaps))
            self.assertFalse(r.emu.wedged, r.emu.wedge_reason)
        self.run_(go(), 60)


class NoCamcmdInsideReconnectSettleTest(EmuTestCase):
    """Chaos seed 14: Files -> "done" (recmode waiting for the mode-switch cap,
    holding the camera) while a reconnect ran the handshake. The handshake's
    SUBSCRIBE opened the S5's own switch window and the waiting recmode went out
    inside it: err_critical, wedged (FINDINGS 21:24 / 22:45). Nothing but reads
    may reach the camera between a SUBSCRIBE and its mod_Play."""
    cfg = chaos_harness.chaos_emu_cfg(settle_min=2.5, settle_max=3.0)
    camera_timing = dict(chaos_harness.FAST_CAMERA, MODE_SWITCH_MIN_GAP_S=3.0, MODE_SWITCH_MAX_WAIT_S=4.0)

    def test_files_done_during_reconnect(self):
        r = self.rig

        async def go():
            st, d = await r.call("GET", "/api/files?limit=2")          # playmode
            self.assertEqual(st, 200, d)
            done = asyncio.ensure_future(r.call("POST", "/api/files/done"))   # recmode waits ~3 s
            await asyncio.sleep(0.3)
            st, d = await r.call("POST", "/api/connection/reconnect", {})
            self.assertEqual(st, 200, d)
            await done
            await r.wait(lambda: r.sess.cam.status == "ready", 30, "ready after the reconnect")
            await asyncio.sleep(1.0)
            self.assertFalse(r.emu.wedged, r.emu.wedge_reason)
        self.run_(go(), 60)


class HeldBackRecmodeIsRetriedTest(EmuTestCase):
    """Scenario 13 (emulator rehearsal of /tmp scenario scripts): after a server
    restart the connect put the camera in play (SUBSCRIBE only in play); the
    recmode back was withheld by the mode-switch cap (3 per window) and never
    retried: status "ready", camera in playback, no live view until the next
    shutter press. A held-back switch must run once the cap allows it."""
    # 2 per window here: the first connect's recmode + the restart's playmode fill it
    camera_timing = dict(chaos_harness.FAST_CAMERA, MODE_SWITCH_MIN_GAP_S=0.5, MODE_SWITCH_MAX=2,
                         MODE_SWITCH_WINDOW_S=8.0, MODE_SWITCH_MAX_WAIT_S=1.0)

    def test_live_view_comes_back_after_the_cap(self):
        r = self.rig

        async def go():
            await r.wait(lambda: r.emu.cammode == "rec" and r.sess.cam._lv_running, 10, "live view")
            await r.stop_server()
            await r.start_server()               # playmode before SUBSCRIBE fills the cap
            await r.wait(lambda: r.emu.cammode == "play", 10, "connect switched to play")
            await r.wait(lambda: r.emu.cammode == "rec" and r.sess.cam._lv_running, 25,
                         "back in rec with live view once the cap allows")
            self.assertFalse(r.emu.wedged, r.emu.wedge_reason)
            # the retried recmode counted against auto_rec()'s budget
            self.assertEqual(len(r.sess.cam._auto_recmodes), 1)
        self.run_(go(), 60)

    def _held_back(self, block):
        """Restart until the recmode is held back by the cap, apply ``block``,
        then check that the queued retry sends no recmode."""
        r = self.rig

        async def go():
            await r.wait(lambda: r.emu.cammode == "rec" and r.sess.cam._lv_running, 10, "live view")
            await r.stop_server()
            await r.start_server()
            cam = r.sess.cam
            await r.wait(lambda: cam._lv_pending_cap is not None, 15, "recmode queued after the cap")
            n = len([e for e in r.emu.log if "value=recmode" in e["q"]])
            await block(cam)
            await asyncio.sleep(10)                  # past the cap (8 s window)
            self.assertEqual(len([e for e in r.emu.log if "value=recmode" in e["q"]]), n,
                             "queued recmode sent although it must not")
            self.assertIsNone(cam._lv_pending)
            self.assertFalse(r.emu.wedged, r.emu.wedge_reason)
        self.run_(go(), 60)

    def test_queued_recmode_respects_the_auto_rec_budget(self):
        async def used_up(cam):
            now = time.monotonic()
            cam._auto_recmodes.extend([now - 1] * cam.AUTO_REC_MAX)   # two this window
        self._held_back(used_up)

    def test_queued_recmode_never_fires_while_the_body_is_busy(self):
        async def busy(cam):
            self.rig.emu.body_action("busy")
            await self.rig.wait(lambda: cam.body_busy, 5, "busy")
        self._held_back(busy)

    def test_queued_recmode_never_fires_into_an_interval_run(self):
        async def interval(cam):
            cam.interval_owner = "test"          # an in-camera run armed/running
        self._held_back(interval)

    def test_queued_recmode_dropped_when_the_camera_changes_mode_itself(self):
        async def own_switch(cam):
            # the camera moves on its own (no NOTIFY): auto_rec()'s case, not ours
            self.rig.emu.set_mode("rec")
            await self.rig.wait(lambda: cam.state.get("cammode") == "rec", 5, "rec seen")
            self.rig.emu.set_mode("play")
            await self.rig.wait(lambda: cam.state.get("cammode") == "play", 5, "play seen")
        self._held_back(own_switch)


class VideoStopRightAfterStartTest(EmuTestCase):
    """Scenario 07, 0 s gap: start and stop sent together. The stop looked at
    getstate (rec off: the start had not reached the camera yet), answered
    "was not recording", then the start ran and the camera kept recording.
    A stop after a start always wins."""

    def test_stop_sent_with_the_start_stops_the_recording(self):
        r = self.rig

        async def go():
            await r.wait(lambda: r.sess.cam._lv_running, 10, "live view")
            start = asyncio.ensure_future(r.call("POST", "/api/video/start"))
            await asyncio.sleep(0)
            stop = asyncio.ensure_future(r.call("POST", "/api/video/stop"))
            (st1, d1), (st2, d2) = await start, await stop
            self.assertEqual((st1, d1["result"]), (200, "ok"), d1)
            self.assertEqual(st2, 200, d2)
            await asyncio.sleep(1.0)
            self.assertFalse(r.emu.recording, (d1, d2))
        self.run_(go(), 60)


class ShutterBurstPacedOnCardWritesTest(EmuTestCase):
    """Scenario 05: 10 shutter presses in 5 s. Each capture went out while the
    card was still being written (getstate sd_access on) and got err_busy; the
    retries landed, but err_busy is what a wedging S5 answers too. Captures now
    wait for the card write instead."""
    cfg = chaos_harness.chaos_emu_cfg(capture_busy_s=1.2)

    def test_burst_never_hits_err_busy(self):
        r = self.rig

        async def go():
            await r.wait(lambda: r.sess.cam._lv_running, 10, "live view")
            st, d = await r.call("POST", "/api/setting", {"type": "drivemode", "value": "normal"})
            self.assertEqual(st, 200, d)
            before = r.emu.card.counts()[0]
            calls = []
            for _ in range(10):
                calls.append(asyncio.ensure_future(r.call("POST", "/api/capture")))
                await asyncio.sleep(0.5)
            res = [await c for c in calls]
            self.assertTrue(all(st == 200 and d["result"] == "ok" for st, d in res), res)
            busy = [e for e in r.emu.log if e["kind"] == "cgi" and "value=capture" in e["q"]
                    and e["result"] != "ok"]
            self.assertEqual(busy, [])
            await asyncio.sleep(1.5)
            self.assertEqual(r.emu.card.counts()[0] - before, 10)
            self.assertFalse(r.emu.wedged, r.emu.wedge_reason)
        self.run_(go(), 90)


class ImportDuringReconnectTest(EmuTestCase):
    """Chaos seed 39: an import that started (or resumed) while a reconnect was
    running failed for good with "Busy: camera not connected (connecting)"
    (ensure_play could not hold the camera). It now waits paused for the
    connection and resumes when the camera is ready, like after a power-off."""
    cfg = chaos_harness.chaos_emu_cfg(dl_rate=2_000_000, settle_min=1.5, settle_max=2.0)

    def test_import_survives_a_reconnect(self):
        r = self.rig

        async def go():
            st, d = await r.call("GET", "/api/files?limit=2&kind=photo")
            ids = [i["id"] for i in d["items"]]
            st, d = await r.call("POST", "/api/files/done")
            await r.call("POST", "/api/connection/reconnect", {})
            await r.wait(lambda: r.sess.cam.status != "ready", 10, "reconnecting")
            st, job = await r.call("POST", "/api/files/import",
                                   {"ids": ids, "dest": os.path.join(self.tmp.name, "P"), "include_raw": False})
            self.assertEqual(st, 202, job)
            fb = r.sess.files
            await r.wait(lambda: fb.jobs[job["id"]].state in ("done", "failed", "done_with_errors"), 40,
                         "import finished")
            j = fb.jobs[job["id"]]
            self.assertEqual(j.state, "done", j.errors)
            self.assertEqual(len(j.saved), 2)
            self.assertFalse(r.emu.wedged, r.emu.wedge_reason)
        self.run_(go(), 90)

    def test_import_that_cannot_hold_the_camera_waits_for_the_reconnect(self):
        """A reconnect longer than ensure_play's wait (real S5: 5-22 s settle +
        approval): the job pauses ("reconnect") and resumes once ready."""
        from lumix.camera import Busy
        r = self.rig

        async def go():
            st, d = await r.call("GET", "/api/files?limit=2&kind=photo")
            ids = [i["id"] for i in d["items"]]
            fb = r.sess.files
            orig, calls = fb.ensure_play, []

            async def ensure_play():
                calls.append(r.sess.cam.status)
                if len(calls) == 1:
                    raise Busy("camera not connected (connecting)")
                return await orig()
            fb.ensure_play = ensure_play
            await r.call("POST", "/api/connection/reconnect", {})
            await r.wait(lambda: r.sess.cam.status != "ready", 10, "reconnecting")
            st, job = await r.call("POST", "/api/files/import",
                                   {"ids": ids, "dest": os.path.join(self.tmp.name, "P"), "include_raw": False})
            await r.wait(lambda: fb.jobs[job["id"]].state != "queued" and len(calls) >= 1, 10, "job ran")
            await r.wait(lambda: fb.jobs[job["id"]].state in ("done", "failed"), 40, "import finished")
            j = fb.jobs[job["id"]]
            self.assertEqual(j.state, "done", j.errors)
            self.assertEqual(len(j.saved), 2)
        self.run_(go(), 90)


class NoDoubleStopstreamTest(EmuTestCase):
    """Chaos seed 98: at a server stop a live-view restart (quality toggle) and
    the shutdown's stop_liveview both saw _lv_running and both sent stopstream;
    the second one, with no stream, is err_critical on the S5. The flag is
    cleared before the request goes out, so only one stop is sent."""

    def test_restart_and_stop_together_send_one_stopstream(self):
        r = self.rig

        async def go():
            cam = r.sess.cam
            await r.wait(lambda: cam._lv_running, 10, "live view")
            n = len([e for e in r.emu.log if "mode=stopstream" in e["q"]])
            a = asyncio.ensure_future(cam.start_liveview("restart"))
            await asyncio.sleep(0)
            await cam.stop_liveview("server stop")
            await a
            stops = [e for e in r.emu.log if "mode=stopstream" in e["q"]][n:]
            self.assertTrue(stops)
            self.assertEqual([e["result"] for e in stops if e["result"] != "ok"], [])
            self.assertFalse(r.emu.wedged, r.emu.wedge_reason)
        self.run_(go(), 30)


class SettleCountsFromTheSubscribeTest(EmuTestCase):
    """Chaos seeds 206/207 (real timing, after the limiter history survived the
    restart): the playmode before SUBSCRIBE waited 13 s for the mode-switch cap,
    and the post-SUBSCRIBE settle (30 s) was counted from before that wait, so
    the server stopped waiting 13 s early and its recmode landed inside the
    camera's own switch window: err_critical, wedged. The settle now counts
    from the SUBSCRIBE itself. Scaled: cap 2 per 6 s, settle 4 s, camera 3 s."""
    cfg = chaos_harness.chaos_emu_cfg(settle_min=3.0, settle_max=3.2)
    camera_timing = dict(chaos_harness.FAST_CAMERA, MODE_SWITCH_MIN_GAP_S=0.1, MODE_SWITCH_MAX=2,
                         MODE_SWITCH_WINDOW_S=6.0, MODE_SWITCH_MAX_WAIT_S=7.0)

    def test_playmode_waiting_for_the_cap_does_not_shorten_the_settle(self):
        from unittest import mock
        r = self.rig

        async def go():
            cam = r.sess.cam
            p = mock.patch.object(cam.profile.connect, "settle_s", 4.0)
            p.start()
            self.addCleanup(p.stop)
            await r.wait(lambda: cam._lv_running, 10, "live view")
            await asyncio.sleep(6.5)                                  # cap empty again
            st, d = await r.call("GET", "/api/files?limit=2")         # playmode
            st, d = await r.call("POST", "/api/files/done")           # recmode: cap full
            self.assertEqual(r.emu.cammode, "rec")
            await r.call("POST", "/api/connection/reconnect", {})     # playmode waits ~6 s
            await r.wait(lambda: cam.status != "ready", 10, "reconnecting")
            await r.wait(lambda: cam.status == "ready", 40, "ready after the reconnect")
            self.assertFalse(r.emu.wedged, r.emu.wedge_reason)
        self.run_(go(), 90)


class ChaosSmoke(unittest.TestCase):
    """A few short seeds with every action, one server restart each."""

    def test_smoke(self):
        for seed in (101, 102):
            r = chaos_harness.run_seed(seed, duration=8, tabs=3, restarts=1)
            self.assertTrue(r.ok, r.report())


@unittest.skipUnless(os.environ.get("LUMIX_CHAOS_SEEDS"), "set LUMIX_CHAOS_SEEDS=1-50 for long chaos runs")
class ChaosLongRun(unittest.TestCase):
    def test_seeds(self):
        dur = float(os.environ.get("LUMIX_CHAOS_DURATION", "20"))
        bad = []
        for seed in chaos_harness.parse_seeds(os.environ["LUMIX_CHAOS_SEEDS"]):
            r = chaos_harness.run_seed(seed, duration=dur)
            if not r.ok:
                bad.append(r.report())
        self.assertFalse(bad, "\n\n".join(bad))


if __name__ == "__main__":
    unittest.main()
