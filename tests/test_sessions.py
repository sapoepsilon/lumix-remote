"""Multi-camera server: per-camera routes, active-camera aliases, registry API,
port-aware claims and per-camera GENA callback paths. No network, no camera:
sessions are registered with autoconnect off, so they never discover."""
import isolate  # noqa: F401  (temp config dir; must come first)
import asyncio
import os
import socket
import tempfile
import unittest
from unittest import mock

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from lumix.events import EventListener
from lumix.registry import Registry
from server.app import build_app
from server.sessions import ServerOptions, SessionManager

OFF = {"autoconnect": False}


class SessionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"LUMIX_REMOTE_HOME": self.tmp.name})
        self.env.start()
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        reg = Registry()
        reg.add("A cam", cam_id="a", settings=OFF, fixed_ip="127.0.0.1", ports={"upnp": 1000})
        reg.add("B cam", cam_id="b", settings=OFF, fixed_ip="127.0.0.1", ports={"upnp": 2000})

        async def make():
            mgr = SessionManager(reg, ServerOptions(event_port=0))
            mgr.load()
            self.mgr = mgr
            client = TestClient(TestServer(build_app(mgr)))
            await client.start_server()
            return client
        self.client = self.loop.run_until_complete(make())

    def tearDown(self):
        self.loop.run_until_complete(self.client.close())
        self.loop.close()
        asyncio.set_event_loop(None)
        self.env.stop()
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        async def go():
            r = await self.client.request(method, path, json=body)
            return r.status, await r.json()
        return self.loop.run_until_complete(go())

    def test_alias_follows_active_camera(self):
        st, d = self.call("GET", "/api/cameras")
        self.assertEqual((st, d["active"], [c["id"] for c in d["cameras"]]), (200, "a", ["a", "b"]))
        self.assertEqual(self.call("GET", "/api/status")[1]["camera_id"], "a")
        self.assertEqual(self.call("GET", "/api/cameras/b/status")[1]["camera_id"], "b")
        self.assertEqual(self.call("GET", "/api/cameras/B cam/status")[1]["camera_id"], "b")   # nickname
        self.assertEqual(self.call("POST", "/api/cameras/b/activate")[0], 200)
        self.assertEqual(self.call("GET", "/api/status")[1]["camera_id"], "b")
        self.assertEqual(Registry().active_id(), "b")

    def test_every_camera_route_is_namespaced(self):
        for path in ("/api/connection", "/api/presets", "/api/events", "/api/settings", "/api/ble/status",
                     "/api/files/status", "/api/jobs", "/api/quick", "/api/info", "/api/state"):
            st, d = self.call("GET", path.replace("/api/", "/api/cameras/b/"))
            self.assertEqual(st, 200, path)
            st2, _ = self.call("GET", path)
            self.assertEqual(st2, 200, path)
        self.assertEqual(self.call("GET", "/api/cameras/b/connection")[1]["camera_id"], "b")
        self.assertEqual(self.call("GET", "/api/cameras/b/ble/status")[1]["camera_id"], "b")
        self.assertEqual(self.call("GET", "/api/cameras/zzz/status")[0], 404)
        self.assertEqual(self.call("POST", "/api/cameras/zzz/capture")[0], 404)

    def test_not_connected_camera_answers_409(self):
        self.assertEqual(self.call("POST", "/api/cameras/b/capture")[0], 409)

    def test_crud(self):
        st, d = self.call("POST", "/api/cameras", {"nickname": "C", "model": "dc_s5", "settings": OFF,
                                                   "ports": {"http": 8080}})
        self.assertEqual((st, d["id"], d["entry"]["model"], d["entry"]["ports"]["http"]), (201, "c", "dc_s5", 8080))
        self.assertEqual(d["entry"]["ports"]["liveview"], 49201)
        self.assertEqual(self.call("POST", "/api/cameras", {"model": "nope"})[0], 400)
        self.assertEqual(self.call("POST", "/api/cameras", {"ports": {"http": 0}})[0], 400)
        st, d = self.call("PATCH", "/api/cameras/c", {"nickname": "Studio", "settings": {"watchdog": True}})
        self.assertEqual((st, d["nickname"], d["entry"]["settings"]["watchdog"]), (200, "Studio", True))
        self.assertTrue(self.mgr.get("c").ble.watchdog_enabled)
        st, d = self.call("PATCH", "/api/cameras/c", {"model": "generic"})
        self.assertEqual(d["profile"]["id"], "generic")         # session rebuilt with the new profile
        self.assertEqual(self.call("DELETE", "/api/cameras/c")[1]["removed"], "c")
        self.assertEqual([c["id"] for c in self.call("GET", "/api/cameras")[1]["cameras"]], ["a", "b"])
        self.assertEqual(self.call("GET", "/api/models")[1]["models"][0]["id"], "dc_s5")
        self.assertIn("setup_ble", self.call("GET", "/api/models/dc_s5")[1])

    def test_no_camera(self):
        self.call("DELETE", "/api/cameras/a")
        self.call("DELETE", "/api/cameras/b")
        st, d = self.call("GET", "/api/status")
        self.assertEqual((st, d["status"]), (200, "no_camera"))
        st, d = self.call("GET", "/api/settings")
        self.assertEqual(st, 404)
        self.assertTrue(d["setup"])

    def test_claims_are_port_aware(self):
        a, b = self.mgr.get("a").cam, self.mgr.get("b").cam
        self.assertEqual(self.mgr.claimed_ips("a"), {"127.0.0.1:2000"})
        self.assertEqual(a._claimed(), set())              # b is on another UPnP port
        b.cfg.upnp_port = 1000
        self.assertEqual(b._claimed(), {"127.0.0.1"})
        self.assertFalse(b.accepts({"ip": "127.0.0.1"}))
        self.assertTrue(b.accepts({"ip": "127.0.0.2"}))
        b.cfg.udn = "uuid:x"
        self.assertTrue(b.accepts({"ip": "127.0.0.1", "UDN": "uuid:x"}))

    def test_per_camera_config(self):
        a, b = self.mgr.get("a").cam.cfg, self.mgr.get("b").cam.cfg
        self.assertEqual((a.lv_port, b.lv_port), (49199, 49200))
        self.assertEqual((a.event_path, b.event_path), ("/Camera/event/a", "/Camera/event/b"))
        self.assertEqual((a.upnp_port, b.upnp_port, a.http_port), (1000, 2000, 80))


class CommandLineOverrideTest(unittest.TestCase):
    def test_overrides_stay_on_the_startup_camera(self):
        """--lv-port etc. without --camera apply to the camera active at start-up;
        activating another camera later must not hand it that port (QA 2026-10-12:
        a restarted session clashed with the first camera's live-view port)."""
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LUMIX_REMOTE_HOME": d}):
            loop = asyncio.new_event_loop()
            try:
                reg = Registry()
                reg.add("A", cam_id="a", activate=True, ports={"liveview": 49299})
                reg.add("B", cam_id="b", ports={"liveview": 49300})

                async def go():
                    mgr = SessionManager(reg, ServerOptions(lv_port=49299))
                    mgr.load()
                    await mgr.set_active("b")
                    return mgr, mgr.camera_config(reg.get("b")), mgr.camera_config(reg.get("a"))
                mgr, b, a = loop.run_until_complete(go())
                self.assertEqual(mgr.target_id(), "a")
                self.assertEqual((a.lv_port, b.lv_port), (49299, 49300))
            finally:
                loop.close()

    def test_camera_ip_runs_only_the_target(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LUMIX_REMOTE_HOME": d}):
            loop = asyncio.new_event_loop()
            try:
                reg = Registry()
                reg.add("Real", cam_id="real")

                async def go():
                    mgr = SessionManager(reg, ServerOptions(target="emu", camera_ip="127.0.0.1",
                                                            http_port=8080, upnp_port=60606))
                    mgr.load()
                    return mgr
                mgr = loop.run_until_complete(go())
                self.assertFalse(mgr.should_run(reg.get("real")))
                self.assertTrue(mgr.should_run(reg.get("emu")))
                cfg = mgr.get("emu").cam.cfg
                self.assertEqual((cfg.ip, cfg.http_port), ("127.0.0.1", 8080))
                self.assertIsNone(mgr.get("real").cam.cfg.ip)
            finally:
                loop.close()

    def test_first_run_creates_provisional_camera(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LUMIX_REMOTE_HOME": d}):
            loop = asyncio.new_event_loop()
            try:
                async def go():
                    mgr = SessionManager(Registry(), ServerOptions())
                    mgr.load()
                    return mgr
                mgr = loop.run_until_complete(go())
                self.assertEqual(list(mgr.sessions), ["camera"])
                self.assertTrue(Registry().get("camera")["provisional"])
            finally:
                loop.close()


class CallbackRoutingTest(unittest.TestCase):
    def test_notify_reaches_the_right_camera(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        got = {"a": [], "b": []}
        body = (b'<?xml version="1.0"?><e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
                b'<e:property><X_Panasonic_Cam_Sync>mod_Play</X_Panasonic_Cam_Sync></e:property>'
                b'</e:propertyset>')

        async def go():
            def h(k):
                async def on(ev):
                    got[k].append(ev["props"][0]["value"])
                return on
            la = EventListener(port, h("a"), path="/Camera/event/a")
            lb = EventListener(port, h("b"), path="/Camera/event/b")
            await asyncio.gather(la.start(), lb.start())     # concurrent start shares one server
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.request("NOTIFY", "http://127.0.0.1:%d/Camera/event/b" % port, data=body) as r:
                        self.assertEqual(r.status, 200)
                    lb.sid = "uuid:sid-b"
                    async with sess.request("NOTIFY", "http://127.0.0.1:%d/Camera/event" % port, data=body,
                                            headers={"SID": "uuid:sid-b"}) as r:
                        self.assertEqual(r.status, 200)
                    async with sess.request("NOTIFY", "http://127.0.0.1:%d/Camera/event/a" % port, data=body) as r:
                        self.assertEqual(r.status, 200)
            finally:
                await la.stop()
                await lb.stop()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(go())
        finally:
            loop.close()
        self.assertEqual(got, {"a": ["mod_Play"], "b": ["mod_Play", "mod_Play"]})


if __name__ == "__main__":
    unittest.main()
