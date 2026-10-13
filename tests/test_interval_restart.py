"""In-camera interval run vs. server restart, and the mid-operation guard.

Regression for the 2026-10-12 11:55 real-S5 incident
(tests/fixtures/real-s5-interval-restart-incident-2026-10-12.log): a native interval run was going, the
server was restarted, quiesce sent ``camctrl interval pause``, the reconnect
sent ``camcmd playmode`` into the paused run (ok), ``recmode`` 34 s later
answered err_critical and the camera stayed wedged (err_busy) until a power
cycle; the job resume's ``setsetting interval_shoot_interval`` -> err_critical.

Only behaviour seen on the real body is relied on: getstate interval_status
rec/pause/off/enable and interval_remain_num counting down
(tests/fixtures/real-s5-interval-2026-10-12.log), read-only getsetting/curmenu
answering during a run (11:46-11:55), playmode into a paused run -> ok and the
recmode after it -> err_critical (11:55:43 / 11:56:17). NOT verified on the
body yet (camera wedged at the time of writing): that a server that stays off
the camera during a run lets it finish untouched, and the connect completion
(SUBSCRIBE, recmode) right after a run ends. scripts/real-verify-interval-restart.sh
checks both on the real camera.
"""
import isolate  # noqa: F401  (temp config dir; must come first)

import asyncio
import json
import os
import time
import unittest

from lumix.protocol import CamTransport, ModeSwitchLimiter
from test_timelapse_emu import EmuTestCase, emu_cfg

MODE_SWITCH = ("value=recmode", "value=playmode", "value=pictmode")


def writes(log, since, until=None):
    """Requests in the emulator log between two times that could disturb a run:
    mode switches, setsetting, startstream, shutter, SUBSCRIBE."""
    out = []
    for e in log:
        if e["t"] < since or (until is not None and e["t"] > until):
            continue
        q = e["q"]
        if e["kind"] == "cgi" and (any(m in q for m in MODE_SWITCH) or "mode=setsetting" in q
                                   or "mode=startstream" in q or "value=capture" in q
                                   or "type=interval&value=pause" in q):
            out.append(q)
        elif e["kind"] == "upnp" and q.startswith("SUBSCRIBE new"):
            out.append(q)
    return out


class NativeRestartTest(EmuTestCase):
    cfg = emu_cfg(interval_scale=1.0)

    async def _start_native(self, count, interval=2, name="nr"):
        r = self.rig
        st, d = await r.call("POST", "/api/timelapse/start",
                             {"interval": interval, "count": count, "native": True, "name": name})
        self.assertEqual(st, 201, d)
        await r.wait(lambda: r.emu.interval["state"] == "rec", 10, "camera interval run")
        await r.wait(lambda: r.job().shots >= 1, 10, "first native shot")

    def test_server_restart_leaves_the_run_alone_and_adopts_it(self):
        r = self.rig

        async def go():
            photos0 = r.emu.card.counts()[0]
            await self._start_native(6)
            t_stop = time.time()
            await r.stop_server()                       # SIGTERM path: on_shutdown quiesce
            # 1. nothing paused, the camera keeps shooting on its own
            self.assertEqual(r.emu.interval["state"], "rec")
            self.assertEqual(writes(r.emu.log, t_stop), [])
            with open(os.path.join(r.home, "cameras", "emu", "timelapse.json")) as f:
                saved = json.load(f)["job"]
            self.assertEqual(saved["state"], "running")
            self.assertTrue(saved["native"]["armed"])
            remain_at_stop = r.emu.interval["remain"]
            await asyncio.sleep(2.5)                     # a shot while no server runs
            self.assertLess(r.emu.interval["remain"], remain_at_stop)
            # 2. new server: read-only connect, the job re-attaches from getstate
            t_start = time.time()
            await r.start_server()
            cam = r.sess.cam
            self.assertIsNotNone(cam.readonly)
            self.assertIn("interval run", cam.readonly)
            await r.wait(lambda: r.job().state == "running" and "server restarted" in (r.job().message or ""),
                         10, "job adopted")
            # 3. UI data: run progress from getstate even after the restart
            st, s = await r.call("GET", "/api/status")
            self.assertTrue(s["interval"]["running"])
            self.assertIsInstance(s["interval"]["remain"], int)
            self.assertTrue(s["readonly"])
            st, d = await r.call("GET", "/api/timelapse")
            self.assertTrue(d["active"])
            # 4. the run completes on the camera; zero mode switches / writes meanwhile
            await r.wait(lambda: r.emu.interval["state"] not in ("rec", "pause"), 30, "camera run over")
            t_end = time.time()
            self.assertEqual(writes(r.emu.log, t_start, t_end), [])
            await r.wait(lambda: r.job().state == "done", 20, "job done")
            self.assertEqual(r.job().shots, 6)
            self.assertEqual(r.emu.card.counts()[0] - photos0, 6)
            self.assertFalse(r.emu.wedged)
            self.assertFalse(cam.wedged)
            # 5. idle again: the connection is finished (events + live view) afterwards
            await r.wait(lambda: cam.readonly is None, 30, "connect completed after the run")
            self.assertTrue(cam.listener.sid)
            self.assertFalse(r.emu.wedged)
        self.run_(go(), 120)

    def test_run_paused_by_the_user_stays_paused_across_a_restart(self):
        r = self.rig

        async def go():
            await self._start_native(5)
            st, d = await r.call("POST", "/api/timelapse/pause")     # explicit user Pause
            self.assertEqual(d["job"]["state"], "paused")
            self.assertEqual(r.emu.interval["state"], "pause")
            await r.stop_server()
            t_start = time.time()
            await r.start_server()
            cam = r.sess.cam
            self.assertIn("paused", cam.readonly or "")
            await asyncio.sleep(3)
            # the incident: no playmode/recmode/setsetting into the paused run
            self.assertEqual(writes(r.emu.log, t_start), [])
            self.assertEqual((r.job().state, r.job().paused_by), ("paused", "user"))
            self.assertEqual(r.emu.interval["state"], "pause")
            # files / settings / camcmd mode switches are refused with a reason
            st, d = await r.call("GET", "/api/files?limit=1")
            self.assertGreaterEqual(st, 400)
            self.assertIn("interval run", json.dumps(d))
            st, d = await r.call("POST", "/api/setting", {"type": "interval_shoot_interval", "value": "30"})
            self.assertEqual(st, 409, d)
            st, d = await r.call("POST", "/api/camcmd", {"value": "playmode"})
            self.assertEqual(st, 409, d)
            self.assertEqual(writes(r.emu.log, t_start), [])
            # explicit Resume restarts it on the camera
            st, d = await r.call("POST", "/api/timelapse/resume")
            await r.wait(lambda: r.emu.interval["state"] == "rec", 5, "camera run again")
            await r.wait(lambda: r.job().state == "done", 30, "done")
            self.assertEqual(r.job().shots, 5)
            self.assertFalse(r.emu.wedged)
        self.run_(go(), 120)

    def test_lost_run_is_not_rearmed_after_restart(self):
        r = self.rig

        async def go():
            await self._start_native(30)
            await r.stop_server()
            r.emu._interval_stop("enable")              # cancelled on the body meanwhile
            t_start = time.time()
            await r.start_server()
            self.assertIsNone(r.sess.cam.readonly)
            await r.wait(lambda: r.job().state == "paused", 10, "job paused, not re-armed")
            self.assertEqual(r.job().paused_by, "camera")
            self.assertIn("Resume re-arms", r.job().message)
            await asyncio.sleep(1.5)
            self.assertEqual([q for q in writes(r.emu.log, t_start)
                              if "interval" in q or "capture" in q], [])
            self.assertEqual(r.emu.interval["state"], "enable")
        self.run_(go(), 90)

    def test_run_started_without_a_job_is_adopted(self):
        r = self.rig

        async def go():
            # a run started outside this server (body / another app), plain transport
            raw = CamTransport("127.0.0.1", port=r.node.ports["http"])
            for typ, v, v2 in (("drivemode", "stopmotion_interval", "interval"),
                               ("interval_shoot_interval", "1", None), ("interval_shoot_num", "4", None)):
                self.assertTrue((await raw.cgi("setsetting", typ, v, v2)).ok)
            self.assertTrue((await raw.cgi("camcmd", value="capture")).ok)
            await raw.close()
            await r.wait(lambda: r.emu.interval["state"] == "rec", 5, "camera run")
            await r.stop_server()
            t_start = time.time()
            await r.start_server()
            await r.wait(lambda: r.job() is not None and r.job().native.get("adopted"), 10, "adopted job")
            self.assertEqual(r.job().engine, "native")
            await r.wait(lambda: r.job().state == "done", 20, "done")
            self.assertEqual(writes(r.emu.log, t_start, t_start + 4), [])
            self.assertFalse(r.emu.wedged)
        self.run_(go(), 90)

    def test_power_plan_on_server_stop_keeps_the_run(self):
        r = self.rig

        async def go():
            await self._start_native(20)
            st, plan = await r.call("GET", "/api/power/plan?reason=server-stop")
            tl = [i for i in plan["items"] if i["kind"] == "timelapse"]
            self.assertEqual(tl[0]["action"], "keep")
            st, plan = await r.call("GET", "/api/power/plan?reason=poweroff")
            tl = [i for i in plan["items"] if i["kind"] == "timelapse"]
            self.assertEqual(tl[0]["action"], "pause")     # power-off still pauses
            await r.call("POST", "/api/timelapse/stop")
            await r.wait(lambda: r.job().state == "stopped", 10, "stopped")
        self.run_(go(), 60)


FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                       "real-s5-interval-restart-incident-2026-10-12.log")


class IncidentTraceTest(unittest.TestCase):
    def test_real_trace_backs_the_emulator_model(self):
        """What the emulator's incident model rests on, straight from the real log."""
        with open(FIXTURE) as f:
            lines = [l for l in f if l.startswith("[") and "GET /cam.cgi?" in l]

        def result(q):
            l = next(l for l in lines if q in l)
            return l.split("<result>")[1].split("<")[0] if "<result>" in l else l.rsplit("-> 200 ", 1)[1].strip()

        self.assertEqual(result("type=interval&value=pause"), "ok")
        self.assertEqual(result("value=playmode"), "ok")                   # into the paused run
        self.assertEqual(result("value=recmode"), "err_critical")          # the wedge
        self.assertEqual(result("type=interval_shoot_interval"), "err_critical")
        after = [l for l in lines if "getsetting&type=touch_type" in l][1:]
        self.assertTrue(after and all("err_busy" in l for l in after))
        # the old connect sent playmode right after req_acc, before any interval check
        i_acc = max(i for i, l in enumerate(lines) if "accctrl" in l)
        self.assertIn("value=playmode", lines[i_acc + 1])


class GuardTest(EmuTestCase):
    cfg = emu_cfg(interval_scale=1.0)

    def test_guard_withholds_mode_switches_during_a_run(self):
        r = self.rig

        async def go():
            st, d = await r.call("POST", "/api/timelapse/start",
                                 {"interval": 2, "count": 20, "native": True, "name": "g"})
            self.assertEqual(st, 201, d)
            await r.wait(lambda: r.emu.interval["state"] == "rec", 10, "camera run")
            cam = r.sess.cam
            t0 = time.time()
            for v in ("playmode", "recmode", "pictmode", "capture"):
                rep = await cam.cmd("camcmd", value=v)
                self.assertEqual(rep.result, "err_local_mid_operation", v)
            rep = await cam.transport.cgi("setsetting", "interval_shoot_interval", "30")
            self.assertEqual(rep.result, "err_local_mid_operation")
            self.assertFalse(await cam.start_liveview("test"))
            self.assertEqual(writes(r.emu.log, t0), [])
            # reads and the interval controls still go through
            self.assertTrue((await cam.transport.cgi("getsetting", "drivemode")).ok)
            st, d = await r.call("POST", "/api/timelapse/start", {"interval": 2, "count": 3})
            self.assertEqual(st, 409)
            await r.call("POST", "/api/timelapse/stop")
            await r.wait(lambda: r.job().state == "stopped", 10, "stopped")
            self.assertEqual(r.emu.interval["state"], "enable")
            # the run is over: a mode switch is allowed again
            rep = await cam.cmd("camcmd", value="playmode")
            self.assertEqual(rep.result, "ok")
            self.assertFalse(r.emu.wedged)
        self.run_(go(), 60)

    def test_emulator_reproduces_the_incident_without_the_guard(self):
        """The emulator models the real 11:55 sequence: playmode into a paused run
        (ok), then recmode -> err_critical and the camera wedged; setsetting ->
        err_critical; getsetting err_busy afterwards. A transport without the
        guard (like the old connect) walks straight into it."""
        r = self.rig

        async def go():
            st, d = await r.call("POST", "/api/timelapse/start",
                                 {"interval": 2, "count": 20, "native": True, "name": "inc"})
            await r.wait(lambda: r.emu.interval["state"] == "rec", 10, "camera run")
            # no guard and no mode-switch rate limit, like the old connect
            raw = CamTransport("127.0.0.1", port=r.node.ports["http"],
                               limiter=ModeSwitchLimiter(min_gap_s=0, max_n=99, window_s=1))
            try:
                self.assertTrue((await raw.cgi("camctrl", "interval", "pause")).ok)
                await asyncio.sleep(1.0)                    # no shot in flight (body busy)
                self.assertEqual((await raw.cgi("camcmd", value="playmode")).result, "ok")
                await asyncio.sleep(0.3)
                self.assertEqual((await raw.cgi("camcmd", value="recmode")).result, "err_critical")
                self.assertTrue(r.emu.wedged)
                self.assertEqual((await raw.cgi("camcmd", value="recmode")).result, "err_busy")
                self.assertEqual((await raw.cgi("setsetting", "interval_shoot_interval", "30")).result,
                                 "err_critical")
                self.assertEqual((await raw.cgi("getsetting", "touch_type")).result, "err_busy")
            finally:
                await raw.close()
        self.run_(go(), 60)


if __name__ == "__main__":
    unittest.main()
