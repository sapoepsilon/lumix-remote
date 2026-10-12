"""Timelapse + quiesce against the camera emulator, end to end: the real server
app (SessionManager, Camera, FileBrowser, TimelapseEngine) talks HTTP/UDP/GENA to
an in-process emulator on ephemeral ports. No camera needed.

Covers: server engine (shots, import, MP4), busy windows (skip, no spam),
reconnect mid-run via /emu/power-cycle, resume after a server restart, the
camera-native interval engine (pause/resume), and quiesce (import paused +
resumed with Range, recording stopped before power-off, timelapse paused and
resumed when the camera is back).
"""
import isolate  # noqa: F401  (temp config dir; must come first)

import asyncio
import json
import os
import shutil
import socket
import tempfile
import time
import unittest

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from emulator.camera import EmuConfig, Identity
from emulator.servers import EmulatorNode
from lumix.registry import Registry
from server.app import build_app
from server.sessions import ServerOptions, SessionManager

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def emu_cfg(**kw) -> EmuConfig:
    base = dict(settle_min=0.3, settle_max=0.5, approval_delay_s=0.2, capture_busy_s=0.4,
                video_busy_s=0.6, rec_switch_s=0.05, sub_play_switch_s=0.1, cds_ready_s=0.1,
                session_timeout_s=0, seed=3)
    base.update(kw)
    return EmuConfig(**base)


class Rig:
    """Emulator + server app in one event loop."""

    def __init__(self, loop, home, cfg=None):
        self.loop = loop
        self.home = home
        self.cfg = cfg or emu_cfg()
        self.node = None
        self.client = None
        self.mgr = None
        self.lv_port = free_udp_port()

    def run(self, coro, timeout=60):
        return self.loop.run_until_complete(asyncio.wait_for(coro, timeout))

    async def start_emu(self):
        self.node = EmulatorNode("127.0.0.1", 0, 0, 0, 0, Identity(), self.cfg, card_photos=4, card_group=6)
        await self.node.start()

    async def start_server(self):
        reg = Registry(os.path.join(self.home, "cameras.json"))
        if reg.get("emu") is None:
            p = self.node.ports
            reg.add("Emu", cam_id="emu", fixed_ip="127.0.0.1", activate=True,
                    ports={"http": p["http"], "upnp": p["upnp"], "files": p["dl"], "liveview": self.lv_port})
        self.mgr = SessionManager(reg, ServerOptions(event_port=0, scan=False, log_dir=None))
        self.mgr.load()
        self.client = TestClient(TestServer(build_app(self.mgr)))
        await self.client.start_server()
        await self.wait(lambda: self.sess.cam.status == "ready", 20, "camera ready")

    async def stop_server(self):
        if self.client is not None:
            await self.client.close()     # on_shutdown: quiesce("server-stop"), then stop
            self.client = None

    async def stop_all(self):
        await self.stop_server()
        if self.node is not None:
            await self.node.stop()

    @property
    def sess(self):
        return self.mgr.get("emu")

    @property
    def emu(self):
        return self.node.cam

    async def call(self, method, path, body=None):
        r = await self.client.request(method, path, json=body)
        try:
            data = await r.json()
        except (aiohttp.ContentTypeError, json.JSONDecodeError):
            data = None
        return r.status, data

    async def ctl(self, path):
        async with aiohttp.ClientSession() as s:
            async with s.post("http://127.0.0.1:%d%s" % (self.node.ports["control"], path)) as r:
                return await r.json()

    async def wait(self, pred, timeout, what):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return
            await asyncio.sleep(0.1)
        evs = []
        if self.mgr is not None and self.sess is not None:
            evs = ["%.2f %s %s" % (e["ts"] % 100, e["kind"], e["text"][:140])
                   for e in list(self.sess.cam.events)[-25:]]
            j = self.sess.timelapse.job
            if j is not None:
                evs.append("job: %s" % {k: getattr(j, k) for k in ("state", "paused_by", "shots", "skipped",
                                                                    "missed", "waiting", "errors")})
        raise AssertionError("timed out waiting for %s\n%s" % (what, "\n".join(evs)))

    def job(self):
        j = self.sess.timelapse.job
        return j

    def captures(self):
        return [e for e in self.emu.log if e["kind"] == "cgi" and "value=capture" in e["q"]]


class EmuTestCase(unittest.TestCase):
    cfg = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = os.path.join(self.tmp.name, "Pictures")
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.rig = Rig(self.loop, os.path.join(self.tmp.name, "home"), self.cfg)
        self.rig.run(self.rig.start_emu())
        self.rig.run(self.rig.start_server())

    def tearDown(self):
        self.rig.run(self.rig.stop_all())
        self.loop.run_until_complete(asyncio.sleep(0.1))
        self.loop.close()
        asyncio.set_event_loop(None)
        self.tmp.cleanup()

    def run_(self, coro, timeout=60):
        return self.rig.run(coro, timeout)


class ServerEngineTest(EmuTestCase):
    def test_runs_imports_and_assembles(self):
        r = self.rig

        async def go():
            before = r.emu.card.counts()[0]
            st, d = await r.call("POST", "/api/cameras/emu/timelapse",
                                 {"interval": 2, "count": 3, "import": True, "video": HAS_FFMPEG,
                                  "name": "t1", "dest": self.dest})
            self.assertEqual(st, 201, d)
            self.assertEqual(d["job"]["import_mode"], "end")       # 2 s leaves no room per shot
            await r.wait(lambda: r.job().state == "done", 40, "done")
            j = r.job()
            self.assertEqual((j.shots, j.skipped, j.failed), (3, 0, 0))
            self.assertEqual(r.emu.card.counts()[0], before + 3)
            self.assertEqual(len(j.frames), 3)
            for f in j.frames:
                self.assertTrue(f.startswith(os.path.join(self.dest, time.strftime("%Y-%m-%d"),
                                                          "timelapse-t1")), f)
                self.assertGreater(os.path.getsize(f), 100000)
            if HAS_FFMPEG:
                self.assertTrue(j.video_path and os.path.getsize(j.video_path) > 1000, j.video_error)
            # back to shooting with live view after the play-mode round trip
            await r.wait(lambda: r.sess.cam.state.get("cammode") == "rec", 5, "rec mode")
            self.assertFalse(r.sess.files.active)
            # status alias + persisted state
            st, d = await r.call("GET", "/api/timelapse")
            self.assertEqual(d["job"]["state"], "done")
            with open(os.path.join(r.home, "cameras", "emu", "timelapse.json")) as f:
                self.assertEqual(json.load(f)["job"]["shots"], 3)
            st, img = await r.call("GET", "/api/timelapse/frame.jpg")
            self.assertEqual(st, 200)
        self.run_(go(), 90)

    def test_busy_window_skips_without_spam(self):
        r = self.rig

        async def go():
            await r.call("POST", "/api/timelapse/start", {"interval": 2, "count": 3})
            await r.wait(lambda: r.job().shots >= 1, 10, "first shot")
            n_before = len(r.captures())
            r.emu.body_action("busy")                  # someone uses the body: NOTIFY busy
            await r.wait(lambda: r.sess.cam.body_busy, 5, "busy event")
            await asyncio.sleep(4.5)                   # > interval + tolerance: a slot is skipped
            self.assertEqual(len(r.captures()), n_before, "capture sent while the body was busy")
            r.emu.body_action("rec")                   # update + lens_Atta
            await r.wait(lambda: r.job().state == "done", 30, "done")
            j = r.job()
            self.assertEqual(j.shots, 3)
            self.assertGreaterEqual(j.skipped, 1)
            self.assertIn("busy", j.skips[0]["reason"])
            self.assertEqual(len(r.captures()), 3)     # no retries, no burst afterwards
            self.assertFalse(r.sess.cam.wedged)
        self.run_(go(), 60)

    def test_reconnect_mid_run(self):
        r = self.rig

        async def go():
            await r.call("POST", "/api/timelapse/start", {"interval": 2, "count": 4, "ae_lock": True})
            await r.wait(lambda: r.job().shots >= 1, 10, "first shot")
            first = r.sess.cam.connected_at
            self.assertTrue(r.job().fixed)              # exposure captured for the run
            await r.ctl("/emu/power-cycle?off_s=4")     # a real body needs seconds to boot
            await r.wait(lambda: r.job().waiting is not None, 10, "job waits for the camera")
            await r.wait(lambda: r.job().state == "done", 60, "done after reconnect")
            j = r.job()
            self.assertEqual(j.shots, 4)
            self.assertNotEqual(r.sess.cam.connected_at, first)
            self.assertGreaterEqual(j.missed, 1)
            # AE lock re-applied on the new connection
            locks = [e for e in r.emu.log if "af_ae_lock" in e["q"]]
            self.assertGreaterEqual(len(locks), 1)
        self.run_(go(), 90)

    def test_validation(self):
        r = self.rig

        async def go():
            st, d = await r.call("POST", "/api/timelapse/start", {"interval": 2})
            self.assertEqual(st, 400)
            self.assertIn("count", d["error"])
            st, d = await r.call("POST", "/api/timelapse/start", {"interval": 0.2, "count": 3})
            self.assertEqual(st, 400)
            st, d = await r.call("POST", "/api/timelapse/pause")
            self.assertEqual(st, 400)
            st, d = await r.call("POST", "/api/timelapse/start", {"interval": 60, "count": 3,
                                                                  "start_at": "+3600"})
            self.assertEqual((st, d["job"]["state"]), (201, "scheduled"))
            st, d = await r.call("POST", "/api/timelapse/start", {"interval": 2, "count": 3})
            self.assertEqual(st, 409)                  # one per camera
            st, d = await r.call("POST", "/api/timelapse/stop")
            await r.wait(lambda: r.job().state == "stopped", 10, "stopped")
            self.assertEqual(len(r.captures()), 0)
        self.run_(go(), 30)


class RestartTest(EmuTestCase):
    def test_resume_after_server_restart(self):
        r = self.rig

        async def go():
            await r.call("POST", "/api/timelapse/start", {"interval": 2, "count": 4})
            await r.wait(lambda: r.job().shots >= 1, 10, "first shot")
            await r.stop_server()                       # SIGTERM path: on_shutdown quiesce
            with open(os.path.join(r.home, "cameras", "emu", "timelapse.json")) as f:
                saved = json.load(f)["job"]
            self.assertEqual((saved["state"], saved["paused_by"]), ("paused", "server-stop"))
            shots = saved["shots"]
            await r.start_server()                      # new process, same config dir
            await r.wait(lambda: r.job() is not None and r.job().state == "running", 10, "resumed")
            self.assertGreaterEqual(r.job().shots, shots)
            self.assertEqual(r.job().message, "resumed (reconnect)")
            await r.wait(lambda: r.job().state == "done", 40, "done")
            self.assertEqual(r.job().shots, 4)
        self.run_(go(), 90)

    def test_crashed_server_resumes_running_job(self):
        r = self.rig

        async def go():
            await r.call("POST", "/api/timelapse/start", {"interval": 2, "count": 3})
            await r.wait(lambda: r.job().shots >= 1, 10, "first shot")
            # a crash: no quiesce, the file still says "running"
            async def no_quiesce(*a, **kw):
                return {}
            r.mgr.quiesce_all = no_quiesce
            await r.stop_server()
            with open(os.path.join(r.home, "cameras", "emu", "timelapse.json")) as f:
                self.assertEqual(json.load(f)["job"]["state"], "running")
            await r.start_server()
            await r.wait(lambda: r.job().state == "done", 40, "done")
            self.assertEqual(r.job().shots, 3)
        self.run_(go(), 90)


class NativeEngineTest(EmuTestCase):
    cfg = emu_cfg(interval_scale=1.0)

    def test_native_run_with_pause(self):
        r = self.rig

        async def go():
            st, d = await r.call("GET", "/api/timelapse")
            self.assertTrue(d["capabilities"]["native"])
            await r.call("POST", "/api/setting", {"type": "drivemode", "value": "normal"})
            drive0 = r.emu.settings["drivemode"][0]
            st, d = await r.call("POST", "/api/timelapse/start",
                                 {"interval": 2, "count": 4, "native": True, "import": True,
                                  "dest": self.dest, "name": "nat"})
            self.assertEqual(st, 201, d)
            await r.wait(lambda: r.emu.interval["state"] == "run", 10, "camera interval run")
            self.assertEqual(r.emu.curmenu_value("menu_item_id_interval_shoot_num"), "4")
            self.assertEqual(r.emu.curmenu_value("menu_item_id_interval_shoot_interval"), "2")
            # the UI shutter is refused while the body runs its own interval (not the wedge)
            st, d = await r.call("POST", "/api/capture")
            self.assertEqual(st, 409)
            await r.wait(lambda: r.job().shots >= 1, 10, "first native shot")
            st, d = await r.call("POST", "/api/timelapse/pause")
            self.assertEqual(d["job"]["state"], "paused")
            self.assertEqual(r.emu.interval["state"], "pause")
            st, d = await r.call("POST", "/api/timelapse/resume")
            await r.wait(lambda: r.emu.interval["state"] == "run", 5, "camera run again")
            await r.wait(lambda: r.job().state == "done", 40, "done")
            j = r.job()
            self.assertEqual(j.shots, 4)
            self.assertEqual(len(j.frames), 4)
            self.assertEqual(r.emu.settings["drivemode"][0], drive0)   # restored
            self.assertIsNone(r.sess.cam.interval_owner)
            self.assertFalse(r.sess.cam.wedged)
            # frames are the camera's Interval group, oldest first
            names = [os.path.basename(f) for f in j.frames]
            self.assertEqual(names, sorted(names))
        self.run_(go(), 90)


class QuiesceTest(EmuTestCase):
    cfg = emu_cfg(dl_rate=600_000)

    def test_import_paused_on_shooting_and_resumed_with_range(self):
        r = self.rig

        async def go():
            st, d = await r.call("GET", "/api/files?limit=2&kind=photo")
            ids = [i["id"] for i in d["items"]]
            st, job = await r.call("POST", "/api/files/import",
                                   {"ids": ids, "dest": self.dest, "include_raw": False})
            jid = job["id"]
            fb = r.sess.files
            await r.wait(lambda: fb.jobs[jid].bytes_done > 300_000, 15, "transfer running")
            st, d = await r.call("POST", "/api/files/done")          # files -> shooting
            self.assertTrue(d["left"])
            self.assertEqual(d["paused_jobs"], [jid])
            st, j = await r.call("GET", "/api/jobs/" + jid)
            self.assertEqual((j["state"], j["paused_by"]), ("paused", "shooting"))
            parts = [os.path.join(dp, f) for dp, _, fs in os.walk(self.dest) for f in fs if f.endswith(".part")]
            self.assertEqual(len(parts), 1)
            await r.wait(lambda: r.sess.cam.state.get("cammode") == "rec", 5, "rec mode")
            with open(os.path.join(r.home, "cameras", "emu", "imports.json")) as f:
                self.assertEqual(json.load(f)["jobs"][0]["state"], "paused")
            # the power-off plan lists nothing for a paused import
            st, plan = await r.call("GET", "/api/power/plan")
            self.assertFalse([i for i in plan["items"] if i["kind"] == "import"])
            # back to file browsing: the import continues where it stopped
            st, d = await r.call("GET", "/api/files?limit=1")
            await r.wait(lambda: fb.jobs[jid].state == "done", 30, "import done")
            ranges = [e for e in r.emu.log if e["kind"] == "dl" and "bytes=" in e["q"]]
            self.assertTrue(ranges, "no Range request on resume")
            self.assertEqual(ranges[0]["result"], "206")
            saved = fb.jobs[jid].saved
            self.assertEqual(len(saved), 2)
            items = {i.name: i for i in fb.by_id.values()}
            for p in saved:
                self.assertEqual(os.path.getsize(p), items[os.path.basename(p)].original.size)
        self.run_(go(), 90)

    def test_poweroff_quiesces_and_resumes_on_reconnect(self):
        r = self.rig

        async def go():
            await r.call("POST", "/api/timelapse/start", {"interval": 2, "count": 40})
            await r.wait(lambda: r.job().shots >= 1, 10, "first shot")
            st, d = await r.call("POST", "/api/video/start")
            self.assertEqual(d["result"], "ok")
            await r.wait(lambda: r.sess.cam.state.get("rec") == "on", 5, "recording")
            videos = r.emu.card.counts()[0]
            st, plan = await r.call("GET", "/api/power/plan")
            kinds = [i["kind"] for i in plan["items"]]
            self.assertEqual(kinds[:2], ["recording", "timelapse"])
            self.assertIn("events", kinds)
            st, d = await r.call("POST", "/api/power/off", {})        # no confirm: plan only
            self.assertEqual(st, 400)
            self.assertIn("plan", d)
            st, d = await r.call("POST", "/api/power/off", {"confirm": True})
            self.assertEqual(d["result"], "ok", d)
            q = d["quiesce"]
            self.assertEqual([x["kind"] for x in q["paused"]], ["timelapse"])
            stopped = {x["kind"]: x for x in q["stopped"]}
            self.assertTrue(stopped["recording"]["card_written"])
            self.assertIn("events", stopped)
            self.assertEqual(q["errors"], [])
            # recording was stopped (and written) before the camera went off
            self.assertEqual(r.job().state, "paused")
            self.assertEqual(r.job().paused_by, "poweroff")
            log = [e["q"] for e in r.emu.log if e["kind"] == "cgi"]
            i_stop = max(i for i, q_ in enumerate(log) if "video_recstop" in q_)
            i_off = max(i for i, q_ in enumerate(log) if "value=poweroff" in q_)
            self.assertLess(i_stop, i_off)
            self.assertGreater(len([e for e in r.emu.log if e["kind"] == "upnp" and "UNSUB" in e["q"]]), 0)
            await r.wait(lambda: not r.emu.powered, 5, "camera off")
            await r.wait(lambda: r.sess.cam.status != "ready", 10, "server notices the camera is off")
            shots = r.job().shots
            self.assertGreaterEqual(videos, 1)
            # switched back on: reconnect resumes the paused timelapse
            await r.ctl("/emu/power-on")
            await r.wait(lambda: r.job().state == "running", 30, "resumed after reconnect")
            await r.wait(lambda: r.job().shots > shots, 10, "shooting again")
            st, d = await r.call("POST", "/api/timelapse/stop")
            await r.wait(lambda: r.job().state == "stopped", 10, "stopped")
        self.run_(go(), 90)

    def test_entering_files_pauses_timelapse_and_leaving_resumes(self):
        r = self.rig

        async def go():
            await r.call("POST", "/api/timelapse/start", {"interval": 2, "count": 40})
            await r.wait(lambda: r.job().shots >= 1, 10, "first shot")
            st, d = await r.call("GET", "/api/files?limit=1")
            self.assertEqual(st, 200)
            self.assertEqual((r.job().state, r.job().paused_by), ("paused", "files"))
            st, d = await r.call("POST", "/api/files/done")
            await r.wait(lambda: r.job().state == "running", 5, "resumed after files")
            n = r.job().shots
            await r.wait(lambda: r.job().shots > n, 10, "shooting again")
            await r.call("POST", "/api/timelapse/stop")
        self.run_(go(), 60)

    def test_remove_camera_quiesces(self):
        r = self.rig

        async def go():
            await r.call("POST", "/api/timelapse/start", {"interval": 2, "count": 40})
            await r.wait(lambda: r.job().shots >= 1, 10, "first shot")
            tl = r.sess.timelapse
            st, d = await r.call("DELETE", "/api/cameras/emu")
            self.assertEqual(st, 200)
            self.assertEqual((tl.job.state, tl.job.paused_by), ("paused", "remove"))
            n = len(r.captures())
            await asyncio.sleep(2.5)
            self.assertEqual(len(r.captures()), n)
        self.run_(go(), 30)


if __name__ == "__main__":
    unittest.main()
