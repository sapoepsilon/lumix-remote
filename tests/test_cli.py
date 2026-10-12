"""CLI tests against a fake Lumix Remote server (aiohttp in a background thread).

Run: .venv/bin/python -m unittest discover -s tests
"""
import isolate  # noqa: F401  (temp config dir; must come first)

import asyncio
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

from aiohttp import web

from lumix import registry
from lumixcli import resolve, servermgr
from lumixcli.client import Client
from lumixcli.main import main


def _opt(sid, tok, title, value, enabled=True):
    return {"id": "menu_item_id_%s_%s" % (sid, tok), "title": title, "type": sid, "value": value,
            "value2": None, "enabled": enabled}


ISO = {"id": "menu_item_id_sensitivity", "title": "Sensitivity", "section": "record_top", "path": [],
       "func_type": "x", "cmd_types": ["iso"], "enabled": True, "current": "auto",
       "current_option": "menu_item_id_sensitivity_auto", "current_title": "AUTO",
       "options": [_opt("sensitivity", "auto", "AUTO", "auto"), _opt("sensitivity", "L50", "L.50", "50", False),
                   _opt("sensitivity", "100", "100", "100"), _opt("sensitivity", "400", "400", "400")]}
EXPO = {"id": "menu_item_id_exposure3", "title": "Exposure Comp.", "section": "record_top", "path": [],
        "func_type": "x", "cmd_types": ["exposure"], "enabled": True, "current": "0",
        "current_option": "menu_item_id_exposure_0", "current_title": "0",
        "options": [{"id": "menu_item_id_exposure_m1_3", "title": "-1/3", "type": "exposure", "value": "-1/3",
                     "value2": None, "enabled": True},
                    {"id": "menu_item_id_exposure_0", "title": "0", "type": "exposure", "value": "0",
                     "value2": None, "enabled": True},
                    {"id": "menu_item_id_exposure_p1_3", "title": "+1/3", "type": "exposure", "value": "1/3",
                     "value2": None, "enabled": True}]}
PEAK = {"id": "menu_item_id_peaking", "title": "Focus Peaking", "section": "s", "path": [], "func_type": "x",
        "cmd_types": ["peaking"], "enabled": True, "current": "on", "current_option": "menu_item_id_peaking_on",
        "current_title": "On", "options": [_opt("peaking", "off", "Off", "off"), _opt("peaking", "on", "On", "on")]}
EXPOSURE = {"focal": "1055/256", "focal_title": "F4.2", "shtrspeed": "2048/256", "shtrspeed_title": "1/250",
            "aperture_options": [{"value": "939/256", "title": "F3.5"}, {"value": "1024/256", "title": "F4.0"}],
            "shutter_options": [{"value": "2048/256", "title": "1/250"}, {"value": "0/256", "title": "1\""}]}
STATUS = {"status": "ready", "message": "S5 connected", "ip": "10.0.0.5",
          "camera": {"friendlyName": "S5-TEST", "modelNumber": "DC-S5", "X_FirmVersion": "2.03"},
          "battery": "4/5", "battery_grip": "-1/0", "cammode": "rec", "recording": False,
          "rec_progress": "0", "remain_photos": "6744", "remain_video_s": "1800",
          "sd": {"current": "sd2", "sd1": "write_enable", "sd2": "write_enable", "sd1_full": "false",
                 "sd2_full": "false", "access": "off"},
          "lens": "LUMIX S 20-60", "liveview": {"fps": 24.0, "age": 0.03}, "op_owner": None, "powered_off": False}
JPEG = b"\xff\xd8\xff\xe0fakejpeg\xff\xd9"
FILES = [{"id": "11000009", "name": "P1000009.JPG", "kind": "photo", "date": "2026-10-12T01:00:00",
          "size": 800, "has_raw": True, "raw_url": "/api/files/11000009/download?variant=raw"},
         {"id": "11000008", "name": "P1000008.MP4", "kind": "video", "date": None, "size": 800,
          "has_raw": False},
         {"id": "11000007", "name": "P1000007.JPG", "kind": "photo", "date": "2026-10-12T00:59:00",
          "size": 800, "has_raw": False}]
JOB = {"id": "job1", "type": "import", "state": "done", "dest": "/x/Pictures/Lumix", "folder": None,
       "paused_by": None, "files_total": 2, "files_done": 1, "files_skipped": 1, "files_failed": 0,
       "bytes_total": 1600, "bytes_done": 1600, "errors": [], "items": 2}


class FakeServer:
    """Records requests; per-path canned answers can be overridden by tests."""

    def __init__(self):
        self.requests = []
        self.overrides = {}
        self.status = dict(STATUS)
        self.loop = asyncio.new_event_loop()
        self.port = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(10)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        app = web.Application()
        app.router.add_route("*", "/api/ws", self.ws)
        app.router.add_route("*", "/{tail:.*}", self.handle)
        self.runner = web.AppRunner(app)
        self.loop.run_until_complete(self.runner.setup())
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        self.loop.run_until_complete(site.start())
        self.port = site._server.sockets[0].getsockname()[1]
        self._ready.set()
        self.loop.run_forever()

    def close(self):
        async def stop():
            await self.runner.cleanup()
        asyncio.run_coroutine_threadsafe(stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)
        self.loop.close()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    async def ws(self, request):
        sock = web.WebSocketResponse()
        await sock.prepare(request)
        await sock.send_json({"type": "status", "data": self.status})
        await sock.send_json({"type": "events", "data": [{"ts": 1.0, "kind": "conn", "text": "old"}]})
        await sock.send_json({"type": "status", "data": self.status})  # unchanged tick: hidden
        await sock.send_json({"type": "event", "data": {"ts": time.time(), "kind": "notify", "text": "lens_Atta"}})
        await sock.send_json({"type": "status", "data": dict(self.status, recording=True)})
        await sock.send_json({"type": "settings", "data": ["menu_item_id_peaking"]})
        async for _ in sock:  # until the client hangs up
            pass
        return sock

    async def handle(self, request):
        body = None
        if request.can_read_body:
            body = await request.json()
        self.requests.append((request.method, request.path, body))
        key = "%s %s" % (request.method, request.path)
        if key in self.overrides:
            code, data = self.overrides[key]
            return web.json_response(data, status=code)
        p = request.path
        if p == "/api/status":
            return web.json_response(self.status)
        if p == "/api/settings":
            return web.json_response({"ready": True, "model": "S5", "settings": [ISO, EXPO, PEAK]})
        if p == "/api/info":
            return web.json_response({"ddd": {}, "lens": {"name": "L"}, "capability": {}, "touch_type": "5",
                                      "exposure": EXPOSURE})
        if p == "/api/quick":
            return web.json_response({"items": [{"key": "iso", "label": "ISO", "setting": ISO}], "exposure": EXPOSURE})
        if p.startswith("/api/getsetting/"):
            t = p.rsplit("/", 1)[1]
            return web.json_response({"result": "ok", "type": t, "values": {t: "auto"}})
        if p.startswith("/api/settings/") and request.method == "POST":
            return web.json_response({"result": "ok", "changed": [], "setting": dict(ISO, current_title="400")})
        if p == "/api/setting":
            return web.json_response({"result": "ok", "changed": []})
        if p == "/api/capture":
            return web.json_response({"result": "ok", "remain_before": "10", "remain_after": "9", "count_changed": True})
        if p.startswith("/api/video/"):
            return web.json_response({"result": "ok", "recording": p.endswith("start")})
        if p == "/api/touch":
            return web.json_response({"result": "ok", "release": "ok"})
        if p in ("/api/af", "/api/zoom", "/api/power/off"):
            return web.json_response({"result": "ok"})
        if p == "/api/camcmd":
            return web.json_response({"result": "ok", "text": None})
        if p == "/api/events":
            return web.json_response([{"ts": 1.0, "kind": "cmd", "text": "a"}, {"ts": 2.0, "kind": "warn", "text": "b"}])
        if p == "/api/liveview.jpg":
            return web.Response(body=JPEG, content_type="image/jpeg")
        if p == "/api/files":
            kind = request.query.get("kind", "all")
            items = [f for f in FILES if kind == "all" or f["kind"] == kind]
            off, lim = int(request.query.get("offset", 0)), int(request.query.get("limit", 60))
            return web.json_response({"total": len(items), "items": items[off:off + lim]})
        if p == "/api/files/status":
            return web.json_response({"active": True, "cammode": "play", "idle_left_s": 99.0,
                                      "default_dest": "/x/Pictures/Lumix"})
        if p == "/api/files/done":
            return web.json_response({"left": True, "paused_jobs": []})
        if p.startswith("/api/files/") and p.endswith("/download"):
            fid = p.split("/")[3]
            f = next(x for x in FILES if x["id"] == fid)
            name = f["name"] if request.query.get("variant") != "raw" else f["name"][:-4] + ".RW2"
            body = (fid * 100).encode()
            return web.Response(body=body, headers={
                "Content-Disposition": "attachment; filename=\"%s\"" % name})
        if p == "/api/files/import":
            return web.json_response(dict(JOB, state="queued"), status=202)
        if p.startswith("/api/jobs/") and request.method == "GET":
            return web.json_response(JOB)
        if p == "/api/jobs":
            return web.json_response([JOB])
        return web.json_response({"error": "nope"}, status=404)


def run(server_url, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["--server", server_url] + list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = FakeServer()

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()

    def setUp(self):
        self.srv.requests.clear()
        self.srv.overrides.clear()
        self.srv.status = dict(STATUS)

    def cli(self, *argv):
        return run(self.srv.url, *argv)

    def posts(self):
        return [(p, b) for m, p, b in self.srv.requests if m == "POST"]

    def test_status_human_and_json(self):
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("battery    4/5", out)
        self.assertIn("6744 photos", out)
        self.assertIn("sd2 write_enable (active)", out)
        code, out, _ = self.cli("status", "--json")  # --json after the subcommand works too
        self.assertEqual(json.loads(out)["remain_photos"], "6744")

    def test_server_env_and_unreachable(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        code, out, err = run("127.0.0.1:%d" % port, "status")
        self.assertEqual(code, 3)
        self.assertIn("lumix server start", err)
        os.environ["LUMIX_SERVER"] = self.srv.url
        try:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(main(["status"]), 0)
        finally:
            del os.environ["LUMIX_SERVER"]

    def test_set_menu_setting_by_cmd_type_and_title(self):
        code, out, _ = self.cli("set", "iso", "400")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.posts(), [("/api/settings/menu_item_id_sensitivity",
                                         {"option": "menu_item_id_sensitivity_400"})])
        self.assertIn("AUTO -> 400 (ok)", out)
        self.srv.requests.clear()
        self.assertEqual(self.cli("set", "Exposure Comp.", "+1/3")[0], 0)
        self.assertEqual(self.posts()[0][1], {"option": "menu_item_id_exposure_p1_3"})
        self.srv.requests.clear()
        self.assertEqual(self.cli("set", "exposure3", "-1/3", "--json")[0], 0)  # leading "-" value
        self.assertEqual(self.posts()[0][1], {"option": "menu_item_id_exposure_m1_3"})
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.cli("status", "--bogus")

    def test_set_unknown_is_usage_error_without_request(self):
        code, _, err = self.cli("set", "iso", "999")
        self.assertEqual(code, 2)
        self.assertIn("allowed: auto, L50, 100, 400", err)
        self.assertEqual(self.posts(), [])
        code, _, err = self.cli("set", "nosuch", "1")
        self.assertEqual(code, 2)

    def test_set_aperture_shutter_and_raw(self):
        self.assertEqual(self.cli("set", "aperture", "4")[0], 0)
        self.assertEqual(self.cli("set", "shutter", "250")[0], 0)
        self.assertEqual(self.cli("set", "--raw", "focal", "1024/256")[0], 0)
        self.assertEqual(self.posts(), [("/api/setting", {"type": "focal", "value": "1024/256"}),
                                        ("/api/setting", {"type": "shtrspeed", "value": "2048/256"}),
                                        ("/api/setting", {"type": "focal", "value": "1024/256"})])

    def test_camera_error_exit_code_and_raw_message(self):
        self.srv.overrides["POST /api/capture"] = (200, {"result": "err_busy", "remain_before": "9",
                                                         "remain_after": "9", "count_changed": False})
        code, out, err = self.cli("shoot")
        self.assertEqual(code, 1)
        self.assertIn("err_busy", err)
        code, out, err = self.cli("shoot", "--json")
        self.assertEqual(json.loads(out)["result"], "err_busy")

    def test_http_409_busy(self):
        self.srv.overrides["POST /api/af"] = (409, {"error": "camera busy with capture"})
        code, _, err = self.cli("af")
        self.assertEqual(code, 1)
        self.assertIn("409", err)
        self.assertIn("camera busy with capture", err)

    def test_touch_release_error(self):
        self.srv.overrides["POST /api/touch"] = (200, {"result": "ok", "release": "err_reject"})
        self.assertEqual(self.cli("focus", "1", "2")[0], 1)

    def test_focus_percent_and_fraction(self):
        self.assertEqual(self.cli("focus", "50%", "0.25")[0], 0)
        self.assertEqual(self.posts(), [("/api/touch", {"x": 500, "y": 250})])
        self.assertEqual(self.cli("focus", "1200", "5")[0], 2)

    def test_video_toggle_shoot_af_zoom_cmd(self):
        self.cli("video", "toggle")
        self.srv.status["recording"] = True
        self.cli("video", "toggle")
        self.cli("video", "start")
        self.cli("shoot")
        self.cli("af")
        self.cli("zoom", "tele", "--fast")
        self.cli("cmd", "recmode")
        self.assertEqual([p for p, _ in self.posts()],
                         ["/api/video/start", "/api/video/stop", "/api/video/start", "/api/capture",
                          "/api/af", "/api/zoom", "/api/camcmd"])
        self.assertEqual(self.posts()[5][1], {"action": "tele-fast"})
        self.assertEqual(self.posts()[6][1], {"value": "recmode"})

    def test_poweroff_needs_yes(self):
        code, _, err = self.cli("poweroff")
        self.assertEqual(code, 2)
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.cli("poweroff", "--yes")[0], 0)
        self.assertEqual(self.posts(), [("/api/power/off", {"confirm": True})])

    def test_get_variants(self):
        code, out, _ = self.cli("get")
        self.assertIn("F4.2", out)
        self.assertIn("AUTO", out)
        code, out, _ = self.cli("get", "iso", "--json")
        self.assertEqual(json.loads(out)["id"], "menu_item_id_sensitivity")
        code, out, _ = self.cli("get", "--raw", "focal")
        self.assertEqual(out.strip(), "focal = auto")
        code, out, _ = self.cli("get", "shutter")
        self.assertIn("1/250", out)

    def test_settings_list_and_options(self):
        code, out, _ = self.cli("settings", "list", "--filter", "iso")
        self.assertIn("sensitivity", out)
        self.assertIn("L50*", out)
        self.assertNotIn("peaking", out)
        code, out, _ = self.cli("settings", "list", "--names")
        self.assertEqual(out.split(), ["sensitivity", "exposure3", "peaking", "aperture", "shutter"])
        code, out, _ = self.cli("settings", "options", "exposure3", "--names")
        self.assertEqual(out.split(), ["m1_3", "0", "p1_3"])
        code, out, _ = self.cli("settings", "list", "--json")
        self.assertEqual(len(json.loads(out)), 3)

    def test_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f.jpg")
            self.assertEqual(self.cli("snapshot", "-o", path)[0], 0)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), JPEG)

    def test_events_list_and_follow(self):
        code, out, _ = self.cli("events", "-n", "1")
        self.assertEqual(out.count("\n"), 1)
        self.assertIn("warn", out)
        code, out, _ = self.cli("events", "-f", "--count", "3")
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertIn("lens_Atta", lines[0])
        self.assertIn("recording=True", lines[1])
        self.assertIn("settings changed: peaking", lines[2])
        code, out, _ = self.cli("events", "-f", "--json", "--all", "--count", "2")
        self.assertEqual(json.loads(out.splitlines()[0])["type"], "status")

    def test_files_list(self):
        code, out, _ = self.cli("files", "list")
        self.assertEqual(code, 0)
        self.assertIn("P1000009.JPG   photo 2026-10-12 01:00:00", out)
        self.assertIn("+RAW", out)
        self.assertIn("3 of 3", out)
        code, out, _ = self.cli("files", "list", "--kind", "video", "--names")
        self.assertEqual(out.split(), ["P1000008.MP4"])
        code, out, _ = self.cli("files", "list", "--limit", "1")
        self.assertIn("(more: --offset 1, or --all)", out)
        code, out, _ = self.cli("files", "status")
        self.assertIn("default import folder: /x/Pictures/Lumix", out)

    def test_files_download(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, err = self.cli("files", "download", "p1000009.jpg", "--raw", "-o", d)
            self.assertEqual(code, 0, err)
            self.assertEqual(sorted(os.listdir(d)), ["P1000009.JPG", "P1000009.RW2"])
            self.assertEqual(os.path.getsize(os.path.join(d, "P1000009.JPG")), 800)
            code, out, _ = self.cli("files", "download", "11000009", "-o", d)
            self.assertIn("skipped", out)                       # same size already there
            code, out, _ = self.cli("files", "download", "--latest", "2", "-o", d, "--json")
            self.assertEqual(len(json.loads(out)), 2)
        code, _, err = self.cli("files", "download", "NOPE.JPG")
        self.assertEqual(code, 2)
        self.assertIn("not on the card: NOPE.JPG", err)
        code, _, err = self.cli("files", "download")
        self.assertEqual(code, 2)

    def test_files_import_and_jobs(self):
        code, out, err = self.cli("files", "import", "--latest", "2", "--dest", "/tmp/x", "--no-raw")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.posts()[-1], ("/api/files/import", {"ids": ["11000009", "11000008"],
                                                                 "include_raw": False, "dest": "/tmp/x"}))
        self.assertIn("job1 done             2/2 files", out)
        self.assertIn("1 already there", out)
        code, out, _ = self.cli("files", "import", "P1000007.JPG", "--no-wait", "--json")
        self.assertEqual(json.loads(out)["state"], "queued")
        code, out, _ = self.cli("files", "jobs")
        self.assertIn("job1", out)
        self.srv.overrides["GET /api/jobs/job2"] = (200, dict(JOB, id="job2", state="failed",
                                                               errors=[{"error": "stalled"}]))
        self.srv.overrides["POST /api/files/import"] = (202, dict(JOB, id="job2", state="queued"))
        code, out, err = self.cli("files", "import", "P1000007.JPG")
        self.assertEqual(code, 1)
        self.assertIn("error: stalled", out)
        code, out, _ = self.cli("files", "done")
        self.assertIn("back to shooting", out)
        self.assertIn(("/api/files/done", {}), self.posts())

    def test_timelapse_commands(self):
        job = {"id": "j1", "name": "sunset", "engine": "server", "state": "running", "paused_by": None,
               "interval_s": 10.0, "shots": 12, "target": 360, "skipped": 1, "missed": 0, "failed": 0,
               "waiting": None, "eta_s": 3480, "next_shot_at": None, "auto_import": True,
               "import_mode": "end", "frames_total": 0, "folder": "/x/timelapse-sunset", "video": True,
               "video_path": None, "video_error": None, "ae_lock": False, "skips": [{"slot": 4, "reason": "camera busy"}],
               "errors": [], "message": None}
        st = {"job": job, "capabilities": {"native": True, "ffmpeg": True}}
        for path in ("/api/timelapse", "/api/timelapse/start", "/api/timelapse/pause",
                     "/api/timelapse/resume", "/api/timelapse/stop"):
            self.srv.overrides[("GET " if path == "/api/timelapse" else "POST ") + path] = (200, st)
        code, out, err = self.cli("timelapse", "start", "--interval", "10", "--count", "360",
                                  "--native", "--import", "--video", "--duration", "1h", "--start-at", "+60")
        self.assertEqual(code, 0, err)
        body = self.posts()[-1][1]
        self.assertEqual((body["interval"], body["count"], body["duration"], body["native"],
                          body["import"], body["video"]), (10.0, 360, 3600.0, True, True, True))
        self.assertAlmostEqual(body["start_at"], time.time() + 60, delta=5)
        self.assertIn("shots      12 of 360", out)
        self.assertIn("camera busy", out)
        code, out, _ = self.cli("timelapse", "status")
        self.assertIn("timelapse sunset (server engine): running", out)
        code, out, _ = self.cli("timelapse", "status", "--json")
        self.assertEqual(json.loads(out)["job"]["shots"], 12)
        for act in ("pause", "resume", "stop"):
            self.assertEqual(self.cli("timelapse", act)[0], 0)
        self.assertEqual([p for p, _ in self.posts()][-3:],
                         ["/api/timelapse/pause", "/api/timelapse/resume", "/api/timelapse/stop"])
        code, _, err = self.cli("timelapse", "start", "--interval", "10")
        self.assertEqual(code, 2)
        self.assertIn("--count", err)
        code, _, err = self.cli("timelapse", "start", "--interval", "ten", "--count", "3")
        self.assertEqual(code, 2)

    def test_poweroff_shows_plan(self):
        plan = {"reason": "poweroff", "resume_on_reconnect": True, "items": [
            {"kind": "import", "action": "pause", "resumable": True, "label": "Import: 2 of 5 files"},
            {"kind": "recording", "action": "stop", "resumable": False, "label": "Video recording"}]}
        self.srv.overrides["GET /api/power/plan"] = (200, plan)
        code, _, err = self.cli("poweroff")
        self.assertEqual(code, 2)
        self.assertIn("pause  Import: 2 of 5 files (resumable)", err)
        self.assertIn("stop   Video recording", err)
        self.srv.overrides["POST /api/power/off"] = (200, {"result": "ok", "quiesce": {
            "paused": [{"kind": "import", "label": "Import: 2 of 5 files"}],
            "stopped": [{"kind": "recording", "label": "Video recording"}], "errors": []}})
        code, out, _ = self.cli("poweroff", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("paused  Import: 2 of 5 files", out)
        self.assertIn("stopped Video recording", out)
        self.assertIn("poweroff: ok", out)


class CamerasCliTest(unittest.TestCase):
    """`--camera` routing and `lumix cameras ...`, through the server and offline."""

    @classmethod
    def setUpClass(cls):
        cls.srv = FakeServer()

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()

    def setUp(self):
        self.srv.requests.clear()
        self.srv.overrides.clear()

    def test_camera_flag_namespaces_camera_routes(self):
        self.srv.overrides["GET /api/cameras/b-cam/status"] = (200, dict(STATUS, camera_id="b-cam"))
        code, out, err = run(self.srv.url, "--camera", "b-cam", "status")
        self.assertEqual(code, 0, err)
        code, out, err = run(self.srv.url, "shoot", "-c", "b-cam")
        paths = [p for _, p, _ in self.srv.requests]
        self.assertIn("/api/cameras/b-cam/status", paths)
        self.assertIn("/api/cameras/b-cam/capture", paths)
        os.environ["LUMIX_CAMERA"] = "b-cam"
        try:
            run(self.srv.url, "info")
        finally:
            del os.environ["LUMIX_CAMERA"]
        self.assertIn("/api/cameras/b-cam/info", [p for _, p, _ in self.srv.requests])
        c = Client("http://x", camera="b cam")
        self.assertEqual(c.path("/api/cameras"), "/api/cameras")
        self.assertEqual(c.path("/api/models/dc_s5"), "/api/models/dc_s5")
        self.assertEqual(c.path("/api/files?limit=3"), "/api/cameras/b%20cam/files?limit=3")
        self.assertTrue(c.ws_url().endswith("/api/cameras/b%20cam/ws"))

    def test_cameras_commands_via_server(self):
        cams = {"active": "s5", "cameras": [
            {"id": "s5", "nickname": "S5", "active": True, "status": "ready", "ip": "10.0.0.5",
             "camera": {"modelNumber": "DC-S5"}, "profile": {"name": "Lumix S5"}, "ble": {"registered": True}},
            {"id": "b", "nickname": "B", "active": False, "status": "discovering", "ip": None,
             "camera": {}, "profile": {"name": "Other"}, "ble": {}}]}
        self.srv.overrides.update({
            "GET /api/cameras": (200, cams),
            "POST /api/cameras": (201, {"id": "studio", "nickname": "Studio", "active": False}),
            "POST /api/cameras/b/activate": (200, {"id": "b", "nickname": "B"}),
            "PATCH /api/cameras/b": (200, {"id": "b", "nickname": "Bee"}),
            "DELETE /api/cameras/b": (200, {"removed": "b", "active": "s5"}),
        })
        code, out, err = run(self.srv.url, "cameras", "list")
        self.assertEqual(code, 0, err)
        self.assertIn("* s5", out)
        self.assertIn("[ble]", out)
        code, out, _ = run(self.srv.url, "cameras", "add", "Studio", "--model", "dc_s5", "--ip", "10.0.0.7",
                           "--http-port", "8080")
        self.assertIn("added studio", out)
        body = [b for m, p, b in self.srv.requests if m == "POST" and p == "/api/cameras"][0]
        self.assertEqual(body, {"nickname": "Studio", "model": "dc_s5", "ip": "10.0.0.7", "ports": {"http": 8080}})
        self.assertIn("active camera: b", run(self.srv.url, "cameras", "use", "b")[1])
        self.assertIn("'Bee'", run(self.srv.url, "cameras", "rename", "b", "Bee")[1])
        self.assertIn("removed b", run(self.srv.url, "cameras", "remove", "b")[1])

    def test_cameras_offline_edits_registry(self):
        with tempfile.TemporaryDirectory() as d:
            home = os.environ.get("LUMIX_REMOTE_HOME")
            os.environ["LUMIX_REMOTE_HOME"] = d
            dead = "http://127.0.0.1:9"
            try:
                self.assertIn("added a-cam", run(dead, "cameras", "add", "A cam")[1])
                self.assertIn("added b-cam", run(dead, "cameras", "add", "B cam", "--ip", "127.0.0.1",
                                                 "--http-port", "8080")[1])
                code, out, _ = run(dead, "cameras", "list")
                self.assertIn("* a-cam", out)
                self.assertIn("server not running", out)
                run(dead, "cameras", "use", "B cam")
                run(dead, "cameras", "rename", "a-cam", "Main")
                reg = registry.Registry()
                self.assertEqual(reg.active_id(), "b-cam")
                self.assertEqual(reg.get("a-cam")["nickname"], "Main")
                self.assertEqual(reg.get("b-cam")["ports"]["http"], 8080)
                run(dead, "cameras", "remove", "b-cam")
                self.assertEqual([c["id"] for c in reg.cameras()], ["a-cam"])
                self.assertEqual(run(dead, "cameras", "use", "nope")[0], 2)
            finally:
                if home:
                    os.environ["LUMIX_REMOTE_HOME"] = home


class ResolveTest(unittest.TestCase):
    def test_find_setting(self):
        s = [ISO, EXPO, PEAK]
        for name in ("menu_item_id_sensitivity", "sensitivity", "iso", "ISO", "Sensitivity"):
            self.assertIs(resolve.find_setting(s, name), ISO)
        with self.assertRaises(resolve.ResolveError):
            resolve.find_setting(s, "zzz")

    def test_find_option(self):
        self.assertEqual(resolve.find_option(EXPO, "-1/3")["id"], "menu_item_id_exposure_m1_3")
        self.assertEqual(resolve.find_option(EXPO, "m1_3")["id"], "menu_item_id_exposure_m1_3")
        self.assertEqual(resolve.find_option(ISO, "l.50")["id"], "menu_item_id_sensitivity_L50")
        self.assertEqual(resolve.find_option(PEAK, "OFF")["id"], "menu_item_id_peaking_off")

    def test_exposure(self):
        opts = EXPOSURE["aperture_options"]
        self.assertEqual(resolve.find_exposure(opts, "F4.0", "focal")["value"], "1024/256")
        self.assertEqual(resolve.find_exposure(opts, "3.5", "focal")["value"], "939/256")
        sh = EXPOSURE["shutter_options"]
        self.assertEqual(resolve.find_exposure(sh, "1s", "shtrspeed")["value"], "0/256")
        with self.assertRaises(resolve.ResolveError):
            resolve.find_exposure(opts, "F1.2", "focal")

    def test_parse_coord(self):
        self.assertEqual(resolve.parse_coord("500"), 500)
        self.assertEqual(resolve.parse_coord("12.5%"), 125)
        self.assertEqual(resolve.parse_coord("0.333"), 333)
        with self.assertRaises(resolve.ResolveError):
            resolve.parse_coord("-1")


class ServerManagerTest(unittest.TestCase):
    """start/stop with a stand-in run.sh that serves /api/status on the given port."""

    def test_start_status_stop(self):
        with tempfile.TemporaryDirectory() as d:
            script = os.path.join(d, "run.sh")
            with open(script, "w") as f:
                f.write("#!/bin/sh\nexec %s -c '\nimport sys, http.server\n"
                        "port = int(sys.argv[sys.argv.index(\"--port\") + 1])\n"
                        "http.server.HTTPServer((\"127.0.0.1\", port), http.server.SimpleHTTPRequestHandler)"
                        ".serve_forever()' \"$@\"\n" % sys.executable)
            os.chmod(script, 0o755)
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            url = "http://127.0.0.1:%d" % port
            home = os.environ.get("LUMIX_REMOTE_HOME")
            os.environ.update(LUMIX_RUN_SH=script, LUMIX_LOG_DIR=d, LUMIX_REMOTE_HOME=d)
            pidfile = os.path.join(d, "server.pid")       # the server's own pidfile, unified
            try:
                self.assertEqual(registry.pidfile_path(), pidfile)
                code, out, err = run(url, "server", "status")
                self.assertEqual(code, 1)
                code, out, err = run(url, "server", "start")
                self.assertEqual(code, 0, out + err)
                self.assertIn("started pid", out)
                self.assertTrue(os.path.exists(pidfile))
                code, out, err = run(url, "--json", "server", "status")
                st = json.loads(out)
                self.assertTrue(st["reachable"] and st["managed"])
                code, out, err = run(url, "server", "start")
                self.assertIn("already running", out)
                code, out, err = run(url, "server", "stop")
                self.assertEqual(code, 0)
                self.assertIn("stopped pid", out)
                self.assertFalse(os.path.exists(pidfile))
                time.sleep(0.3)
                self.assertEqual(run(url, "server", "status")[0], 1)
            finally:
                os.environ.pop("LUMIX_RUN_SH", None)
                os.environ.pop("LUMIX_LOG_DIR", None)
                if home:
                    os.environ["LUMIX_REMOTE_HOME"] = home

    def test_legacy_logs_pidfile_is_adopted(self):
        """A pidfile from an older `lumix server start` (logs/lumix-server.pid) moves
        to the unified one, so `lumix server stop` still finds that server."""
        with tempfile.TemporaryDirectory() as d:
            home = os.environ.get("LUMIX_REMOTE_HOME")
            os.environ.update(LUMIX_LOG_DIR=d, LUMIX_REMOTE_HOME=os.path.join(d, "cfg"))
            try:
                with open(os.path.join(d, "lumix-server.pid"), "w") as f:
                    f.write("%d\n" % os.getpid())
                st = servermgr.status(Client("http://127.0.0.1:9"))
                self.assertEqual(st["pidfile"], os.path.join(d, "cfg", "server.pid"))
                self.assertEqual(st["pid"], os.getpid())
                self.assertFalse(os.path.exists(os.path.join(d, "lumix-server.pid")))
            finally:
                os.environ.pop("LUMIX_LOG_DIR", None)
                if home:
                    os.environ["LUMIX_REMOTE_HOME"] = home


if __name__ == "__main__":
    unittest.main()


class EventPrinterCamerasTest(unittest.TestCase):
    def test_cameras_message_is_one_line(self):
        from lumixcli.main import EventPrinter
        msg = {"type": "cameras", "data": {"active": "a", "cameras": [{"id": "a", "status": "ready"},
                                                                       {"id": "b", "status": "reconnecting"}]}}
        lines = EventPrinter(False, True).handle(msg)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith("cameras *a ready, b reconnecting"))
        self.assertEqual(EventPrinter(False, False).handle(msg), [])
        self.assertEqual(json.loads(EventPrinter(True, True).handle(msg)[0])["type"], "cameras")
