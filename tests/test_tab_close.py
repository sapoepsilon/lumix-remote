"""Closing the browser tab must never stop what it started.

Everything the web UI starts runs on the server: timelapse jobs (server engine
and the camera's own interval run), video recording, imports, live view (shared
by every MJPEG consumer) and Bluetooth actions. A page going away only drops
its WebSocket, its MJPEG stream and whatever request it had in flight. These
tests close exactly those and check that the work goes on.

aiohttp's TestServer runs with ``handler_cancellation=True`` (a disconnect
cancels the handler), the worst case: the ``detach_mutations`` middleware must
keep state-changing requests running anyway.
"""
import isolate  # noqa: F401  (temp config dir; must come first)

import asyncio
import os
import time
import unittest

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from server.app import DETACHED, detach_mutations
from test_timelapse_emu import EmuTestCase, emu_cfg


async def _abort(url: str, method: str = "POST", body=None, after: float = 0.1) -> None:
    """Send a request and drop the connection after ``after`` seconds (a tab closing)."""
    async with aiohttp.ClientSession() as s:
        try:
            await asyncio.wait_for(s.request(method, url, json=body if body is not None else {}), after)
        except asyncio.TimeoutError:
            pass


class DetachMiddlewareTest(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()
        asyncio.set_event_loop(None)

    def _run(self, middlewares):
        done = []

        async def slow(request):
            await asyncio.sleep(0.5)          # e.g. touch press ... release
            done.append(request.method)
            return web.json_response({"ok": True})

        async def go():
            app = web.Application(middlewares=middlewares)
            app[DETACHED] = set()
            app.router.add_post("/slow", slow)
            app.router.add_get("/slow", slow)
            client = TestClient(TestServer(app))      # handler_cancellation=True
            await client.start_server()
            try:
                await _abort(str(client.make_url("/slow")), "POST")
                await _abort(str(client.make_url("/slow")), "GET")
                await asyncio.sleep(1.0)
            finally:
                await client.close()
            return done

        return self.loop.run_until_complete(go())

    def test_cancelling_server_cuts_a_plain_handler(self):
        # control: without the middleware this test server does cancel on disconnect
        self.assertEqual(self._run([]), [])

    def test_mutating_request_finishes_after_the_client_left(self):
        # POST runs to the end; a GET (read-only) may still be cut
        self.assertEqual(self._run([detach_mutations]), ["POST"])


class TabCloseEmuTest(EmuTestCase):
    """The real server app against the emulator; 'the page' = a WebSocket + an
    MJPEG viewer + in-flight requests, all closed at once."""
    cfg = emu_cfg(dl_rate=600000)

    async def open_page(self):
        r = self.rig
        ws = await r.client.ws_connect("/api/ws")
        mj = await r.client.get("/api/liveview.mjpg")
        self.assertEqual(mj.status, 200)
        await mj.content.read(2000)           # frames are flowing
        await r.wait(lambda: r.sess.cam.liveview.viewers >= 1, 5, "MJPEG viewer")
        return ws, mj

    async def close_page(self, page):
        ws, mj = page
        await ws.close()
        mj.close()
        await self.rig.wait(lambda: self.rig.sess.cam.liveview.viewers == 0, 5, "viewer gone")
        return time.time()

    def sent_after(self, t0):
        """cam.cgi queries the emulator got after ``t0``."""
        return [e["q"] for e in list(self.rig.emu.log) if e["kind"] == "cgi" and e["t"] >= t0 - 0.001]

    def stopstreams_after(self, t0):
        return [q for q in self.sent_after(t0) if "mode=stopstream" in q]

    def test_server_timelapse_keeps_shooting_after_the_page_closes(self):
        r = self.rig

        async def go():
            page = await self.open_page()
            st, d = await r.call("POST", "/api/timelapse/start", {"interval": 2, "count": 3})
            self.assertEqual(st, 201, d)
            await r.wait(lambda: r.job().shots >= 1, 10, "first shot")
            n = await self.close_page(page)
            await r.wait(lambda: r.job().state == "done", 30, "done with no page open")
            j = r.job()
            self.assertEqual((j.shots, j.skipped, j.missed, j.paused_by), (3, 0, 0, None))
            # live view kept for the job and later consumers: no stopstream, still flowing
            self.assertEqual(self.stopstreams_after(n), [])
            self.assertTrue(r.sess.cam._lv_running)
            age = r.sess.cam.liveview.age
            self.assertTrue(age is not None and age < 2, age)
            # a reopened page gets the job state (status + push on connect)
            st, d = await r.call("GET", "/api/timelapse")
            self.assertEqual((d["job"]["state"], d["job"]["shots"]), ("done", 3))
        self.run_(go(), 60)

    def test_native_run_keeps_going_after_the_page_closes(self):
        r = self.rig

        async def go():
            page = await self.open_page()
            st, d = await r.call("POST", "/api/timelapse/start",
                                 {"interval": 2, "count": 3, "native": True, "name": "nat"})
            self.assertEqual(st, 201, d)
            await r.wait(lambda: r.emu.interval["state"] == "rec", 10, "camera interval run")
            n = await self.close_page(page)
            await r.wait(lambda: r.job().state == "done", 40, "native run done with no page")
            self.assertEqual(r.job().shots, 3)
            self.assertIsNone(r.job().paused_by)
            # nothing paused or stopped the body's run: no interval pause/cancel, no stopstream
            sent = self.sent_after(n)
            self.assertFalse([q for q in sent if "type=interval" in q and ("pause" in q or "cancel" in q)], sent)
            self.assertEqual(self.stopstreams_after(n), [])
        self.run_(go(), 90)

    def test_recording_keeps_running_after_the_page_closes(self):
        r = self.rig

        async def go():
            page = await self.open_page()
            # the tab closes while video/start is still in flight
            await _abort(str(r.client.make_url("/api/video/start")), after=0.05)
            await r.wait(lambda: r.sess.cam.state.get("rec") == "on", 5, "recording")
            started = r.sess.cam.rec_started_at
            await self.close_page(page)
            await asyncio.sleep(1.5)
            self.assertEqual(r.sess.cam.state.get("rec"), "on")
            # the reopened page sees the recording and its start time (the timer)
            st, d = await r.call("GET", "/api/status")
            self.assertTrue(d["recording"])
            self.assertEqual(d["rec_started_at"], started)
            st, d = await r.call("POST", "/api/video/stop")
            self.assertFalse(d["recording"])
        self.run_(go(), 30)

    def test_touch_af_is_never_left_pressed(self):
        r = self.rig

        async def go():
            n = time.time()
            await _abort(str(r.client.make_url("/api/touch")), body={"x": 500, "y": 500}, after=0.05)
            await asyncio.sleep(1.0)
            touches = [q for q in self.sent_after(n) if "type=touch" in q]
            self.assertEqual(len(touches), 2, touches)        # press, then the release
            self.assertIn("value2=off", touches[1])
        self.run_(go(), 20)

    def test_import_finishes_and_files_stay_open_while_it_runs(self):
        r = self.rig
        fb = r.sess.files
        fb.idle_timeout_s = 0.5        # the files idle timer must not cut an import

        async def go():
            page = await self.open_page()
            st, d = await r.call("GET", "/api/files?limit=60")
            ids = [x["id"] for x in d["items"]][:4]
            st, d = await r.call("POST", "/api/files/import", {"ids": ids, "dest": self.dest})
            self.assertIn(st, (200, 201, 202), d)
            jid = (d.get("job") or d)["id"]
            await self.close_page(page)
            t0 = time.monotonic()
            await r.wait(lambda: fb.jobs[jid].state in ("done", "done_with_errors", "paused", "failed"),
                         60, "import finished")
            self.assertEqual(fb.jobs[jid].state, "done")
            self.assertEqual(fb.jobs[jid].files_done, 4)
            self.assertGreater(time.monotonic() - t0, 0.5)     # outlived the idle timeout
            self.assertTrue(os.path.isdir(self.dest))
        self.run_(go(), 90)

    def test_reopened_page_finds_running_import(self):
        r = self.rig

        async def go():
            st, d = await r.call("GET", "/api/files?limit=60")
            ids = [x["id"] for x in d["items"]][:4]
            await _abort(str(r.client.make_url("/api/files/import")),
                         body={"ids": ids, "dest": self.dest}, after=0.05)
            await r.wait(lambda: any(j.state == "running" for j in r.sess.files.jobs.values()), 10,
                         "import running")
            st, jobs = await r.call("GET", "/api/jobs")       # what reattachJobs() reads
            self.assertTrue([j for j in jobs if j["state"] in ("queued", "running")], jobs)
            await r.wait(lambda: all(j.state == "done" for j in r.sess.files.jobs.values()), 60, "done")
        self.run_(go(), 90)

    def test_ble_status_says_since_when_an_action_runs(self):
        # a reopened page shows a wake that a closed page started, with its elapsed time
        r = self.rig

        async def go():
            st, d = await r.call("GET", "/api/ble/status")
            self.assertEqual(st, 200)
            self.assertIn("running_since", d)
            self.assertIsNone(d["running_since"])
        self.run_(go(), 20)


if __name__ == "__main__":
    unittest.main()
