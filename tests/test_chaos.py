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
        self.run_(go(), 60)


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
