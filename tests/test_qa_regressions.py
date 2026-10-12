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


class FriendlyErrorTest(unittest.TestCase):
    def test_connection_errors_read_as_sentences(self):
        from lumix.camera import _friendly_error
        from lumix.protocol import CamError
        e = CamError("ClientConnectorError: Cannot connect to host 1.2.3.4:60606 ssl:default "
                     "[Connect call failed ('1.2.3.4', 60606)]")
        self.assertEqual(_friendly_error(e, "1.2.3.4"),
                         "camera at 1.2.3.4 is not reachable (off, asleep or on another network)")
        self.assertIn("does not answer", _friendly_error(CamError("TimeoutError: "), "1.2.3.4"))
        self.assertIn("does not answer", _friendly_error(asyncio.TimeoutError(), "1.2.3.4"))
        self.assertEqual(_friendly_error(CamError("HTTP 500 for /x"), "1.2.3.4"), "HTTP 500 for /x")


class ShutdownClosesStreamsTest(EmuTestCase):
    def test_open_ws_and_mjpeg_end_on_shutdown(self):
        r = self.rig

        async def go():
            import aiohttp
            from server.app import OPEN_STREAMS, close_streams
            await r.wait(lambda: r.sess.cam.liveview.age is not None, 10, "frames")
            ws = await r.client.ws_connect("/api/ws")
            await ws.receive()                              # cameras snapshot
            mj = await r.client.get("/api/liveview.mjpg")
            await mj.content.readuntil(b"\r\n")              # stream is flowing
            app = r.client.server.app
            await r.wait(lambda: len(app[OPEN_STREAMS]) == 2, 5, "both streams registered")
            await close_streams(app)
            while True:                                     # status ticks may be queued
                msg = await asyncio.wait_for(ws.receive(), 3)
                if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    break
            self.assertEqual((msg.type, msg.data), (aiohttp.WSMsgType.CLOSE, 1001))   # GOING_AWAY
            rest = await asyncio.wait_for(mj.content.read(), 3)   # EOF instead of hanging
            self.assertIsInstance(rest, bytes)
            mj.close()
            await ws.close()
        self.run_(go(), 30)


class IndexAssetVersionTest(unittest.TestCase):
    def test_index_references_versioned_assets(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            from aiohttp.test_utils import make_mocked_request
            from server.app import index
            resp = loop.run_until_complete(index(make_mocked_request("GET", "/")))
            html = resp.text
            self.assertRegex(html, r'src="/static/app\.js\?v=[0-9a-f]+"')
            self.assertRegex(html, r'href="/static/style\.css\?v=[0-9a-f]+"')
            self.assertEqual(resp.headers["Cache-Control"], "no-cache")
        finally:
            loop.close()
            asyncio.set_event_loop(None)


class ForgetAndAddressTest(EmuTestCase):
    def test_forget_then_set_address_reconnects(self):
        r = self.rig

        async def go():
            st, d = await r.call("POST", "/api/connection/reconnect", {"forget": True})
            self.assertEqual(st, 200)
            self.assertIsNone(r.mgr.registry.get("emu").get("fixed_ip"))
            await r.wait(lambda: r.sess.cam.status in ("discovering", "reconnecting"), 10, "searching")
            st, d = await r.call("PATCH", "/api/cameras/emu", {"ip": "127.0.0.1"})
            self.assertEqual(st, 200)
            await r.wait(lambda: r.sess.cam.status == "ready", 20, "ready again")
        self.run_(go(), 60)

    def test_same_fixed_ip_brings_a_wandering_session_back(self):
        r = self.rig

        async def go():
            await r.call("POST", "/api/connection/reconnect", {"ip": "127.0.0.9"})
            await r.wait(lambda: r.sess.cam.ip == "127.0.0.9", 10, "on the wrong address")
            st, d = await r.call("PATCH", "/api/cameras/emu", {"ip": "127.0.0.1"})   # unchanged entry
            await r.wait(lambda: r.sess.cam.status == "ready" and r.sess.cam.ip == "127.0.0.1", 30, "back")
        self.run_(go(), 60)


class PausedStatusTest(EmuTestCase):
    def test_not_running_camera_says_why(self):
        r = self.rig

        async def go():
            st, d = await r.call("PATCH", "/api/cameras/emu", {"settings": {"autoconnect": False}})
            self.assertEqual(st, 200)
            st, d = await r.call("GET", "/api/status")
            self.assertEqual(d["status"], "paused")
            self.assertIn("Connect automatically is off", d["message"])
        self.run_(go(), 30)


class ShutterFromPlaybackTest(EmuTestCase):
    def test_capture_after_body_playback_switches_back(self):
        r = self.rig

        async def go():
            await r.wait(lambda: r.sess.cam.liveview.age is not None, 10, "frames")
            await r.ctl("/emu/body/play")             # someone pressed playback on the body
            await r.wait(lambda: r.sess.cam.state.get("cammode") == "play", 10, "camera in play")
            st, d = await r.call("POST", "/api/capture")
            self.assertEqual((st, d.get("result")), (200, "ok"), d)
            self.assertEqual(r.sess.cam.state.get("cammode"), "rec")
            st, d = await r.call("POST", "/api/video/stop")          # not recording: nothing sent
            self.assertEqual(d.get("note"), "was not recording")
            self.assertFalse(any("video_recstop" in e["q"] for e in r.emu.log if e["kind"] == "cgi"))
        self.run_(go(), 40)


def _subscribes(emu):
    return [e for e in emu.log if e["kind"] == "upnp" and e["q"].startswith("SUBSCRIBE new")]


class OneConnectOneHandshakeTest(EmuTestCase):
    """2026-10-12 09:23-09:25 on the S5: a reconnect request that arrived while a
    handshake was running stayed latched, and the supervisor threw the brand-new
    session away ~1.5 s after "ready" ("Disconnected('reconnect requested')"),
    three times in a row. Each round re-ran req_acc, SUBSCRIBE and the
    playmode -> recmode switch: the window that wedges the camera."""

    def _hook_mid_handshake(self, cam, action):
        orig = cam.listener.subscribe
        out = {}

        async def subscribe(ip):
            res = await orig(ip)
            if "done" not in out:
                out["done"] = True
                out["result"] = await action()
            return res
        cam.listener.subscribe = subscribe
        return out

    def test_reconnect_request_during_connect_keeps_the_new_session(self):
        r = self.rig

        async def go():
            cam = r.sess.cam
            out = self._hook_mid_handshake(cam, lambda: r.call("POST", "/api/connection/reconnect", {}))
            cam.request_reconnect()                       # one explicit reconnect while ready
            await r.wait(lambda: "result" in out, 15, "reconnect POSTed mid-handshake")
            st, d = out["result"]
            self.assertEqual(st, 200)
            await r.wait(lambda: cam.status == "ready", 20, "ready")
            subs = len(_subscribes(r.emu))
            lost = sum("connection lost" in e["text"] for e in cam.events)
            await asyncio.sleep(4)                        # several supervisor ticks
            self.assertEqual(cam.status, "ready")
            self.assertEqual(len(_subscribes(r.emu)), subs, "the new session was torn down")
            self.assertEqual(sum("connection lost" in e["text"] for e in cam.events), lost)
            self.assertTrue(d.get("already_connecting"), d)
            self.assertTrue(any("already connecting" in e["text"] for e in cam.events))
        self.run_(go(), 60)

    def test_reconnect_to_another_address_mid_connect_still_happens(self):
        r = self.rig

        async def go():
            cam = r.sess.cam
            out = self._hook_mid_handshake(cam, lambda: asyncio.sleep(0, cam.request_reconnect(ip="127.0.0.9")))
            cam.request_reconnect()
            await r.wait(lambda: "result" in out, 15, "second request mid-handshake")
            self.assertTrue(out["result"])
            await r.wait(lambda: cam.ip == "127.0.0.9" and cam.status != "ready", 15, "moved on")
        self.run_(go(), 60)

    def test_ble_wake_after_the_camera_already_connected_keeps_the_session(self):
        r = self.rig

        async def go():
            cam = r.sess.cam
            ble = r.sess.ble

            async def fake_run(*args, **kw):
                return {"ok": True, "ip": cam.ip}
            ble.run = fake_run
            subs = len(_subscribes(r.emu))
            res = await ble.wake()
            await asyncio.sleep(3)
            self.assertEqual(cam.status, "ready")
            self.assertEqual(len(_subscribes(r.emu)), subs, "BLE wake threw the session away")
            self.assertEqual(res.get("reconnect"), "already connected")
        self.run_(go(), 30)


class AccessRequestCancelTest(unittest.TestCase):
    """LUMIX Sync cancels a req_acc the camera is still researching
    (accctrl req_acc_can) instead of leaving the camera waiting for us."""

    def test_stop_while_waiting_for_approval_sends_req_acc_can(self):
        from emulator.camera import Identity
        from emulator.servers import EmulatorNode
        from test_timelapse_emu import emu_cfg, free_udp_port
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            async def go():
                node = EmulatorNode("127.0.0.1", 0, 0, 0, 0, Identity(), emu_cfg(approval="prompt"))
                await node.start()
                p = node.ports
                cam = Camera(CameraConfig(ip="127.0.0.1", lv_port=free_udp_port(), event_port=0,
                                          http_port=p["http"], upnp_port=p["upnp"], files_port=p["dl"],
                                          scan=False, store=_MemStore()))
                cam.start()
                try:
                    for _ in range(100):
                        if cam.status == "waiting_approval":
                            break
                        await asyncio.sleep(0.1)
                    self.assertEqual(cam.status, "waiting_approval")
                finally:
                    await cam.stop()
                    await node.stop()
                return [e for e in node.cam.log if e["kind"] == "cgi" and "type=req_acc_can" in e["q"]]
            cans = loop.run_until_complete(asyncio.wait_for(go(), 30))
            self.assertEqual(len(cans), 1, cans)
        finally:
            loop.close()
            asyncio.set_event_loop(None)


class _MemStore:
    def __init__(self):
        self.d = {}

    def load(self):
        return dict(self.d)

    def save(self, **kw):
        self.d.update(kw)
