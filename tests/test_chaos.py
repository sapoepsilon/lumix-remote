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
    does that for a shutter press in that drive mode). The server refused every
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
            st, d = await r.call("POST", "/api/capture")
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
