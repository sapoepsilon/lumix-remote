"""aiohttp web app: JSON API, MJPEG live view, WebSocket push, static UI.

Every camera route exists twice: ``/api/<x>`` talks to the active camera (the
CLI and older clients use these), ``/api/cameras/{cam}/<x>`` to a given one.
Register camera routes with ``camera_route()`` so both forms exist.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional

from aiohttp import WSMsgType, web

from lumix.camera import Busy, Camera
from lumix.liveview import FrameSource
from server.sessions import CameraSession, SessionManager

log = logging.getLogger("server")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

SESSIONS = web.AppKey("sessions", SessionManager)
CAMERA_PREFIX = "/api/cameras/{cam}"

# Quick controls are looked up by the camera's own cmd_type, so they follow
# whatever allmenu exposes rather than hard-coded ids.
QUICK = [
    ("recmode", "Mode"), ("iso", "ISO"), ("exposure", "Exp. comp"),
    ("whitebalance", "WB"), ("focusmode", "Focus"), ("drivemode", "Drive"),
    ("quality", "Photo quality"), ("pictsize", "Photo size"), ("videoquality", "Video quality"),
]

# Extension point: feature modules (timelapse, files, hdmi, ...) add themselves here.
# Each is a callable setup(app) that can add routes with camera_route(), register
# a FrameSource in session(request).sources, and use camera.exclusive(owner).
FEATURES: List[Callable[[web.Application], None]] = []


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=str))


class NoCamera(web.HTTPNotFound):
    def __init__(self, cam_id: Optional[str]):
        super().__init__(text=json.dumps({"error": "no camera %s" % (repr(cam_id) if cam_id else "set up"),
                                          "setup": cam_id is None}),
                         content_type="application/json")


def session(request: web.Request) -> CameraSession:
    """The camera session a request is for: /api/cameras/{cam}/... or the active one."""
    cid = request.match_info.get("cam")
    s = request.app[SESSIONS].get(cid)
    if s is None:
        raise NoCamera(cid)
    return s


def camera(request: web.Request) -> Camera:
    return session(request).cam


def camera_route(app: web.Application, method: str, path: str, handler: Any) -> None:
    """Add ``/api/<x>`` (active camera) and ``/api/cameras/{cam}/<x>``."""
    assert path.startswith("/api/"), path
    app.router.add_route(method, path, handler)
    app.router.add_route(method, CAMERA_PREFIX + path[len("/api"):], handler)


def _guard(fn):
    async def wrapper(request: web.Request) -> web.Response:
        try:
            return await fn(request)
        except web.HTTPException:
            raise
        except Busy as e:
            return _json({"error": str(e)}, 409)
        except (KeyError, ValueError) as e:
            return _json({"error": "bad request: %s" % e}, 400)
        except Exception as e:  # camera transport errors etc.
            log.exception("handler failed")
            return _json({"error": "%s: %s" % (type(e).__name__, e)}, 502)
    return wrapper


async def _body(request: web.Request) -> Dict[str, Any]:
    if request.can_read_body:
        try:
            return await request.json()
        except ValueError:
            pass
    return {}


# ------------------------------------------------------------------ handlers
async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(os.path.join(STATIC, "index.html"),
                            headers={"Cache-Control": "no-cache"})


async def manifest(request: web.Request) -> web.FileResponse:
    return web.FileResponse(os.path.join(STATIC, "manifest.webmanifest"),
                            headers={"Content-Type": "application/manifest+json"})


NO_CAMERA_STATUS: Dict[str, Any] = {
    "camera_id": None, "status": "no_camera", "message": "no camera set up", "ip": None,
    "camera": {}, "liveview": {}, "connected_at": None, "recording": False, "wedged": False,
}


async def status(request: web.Request) -> web.Response:
    if "cam" not in request.match_info and request.app[SESSIONS].active() is None:
        return _json(NO_CAMERA_STATUS)
    return _json(camera(request).status_dict())


async def settings(request: web.Request) -> web.Response:
    cam = camera(request)
    if cam.menu is None:
        return _json({"settings": [], "ready": False})
    return _json({"ready": True, "model": cam.menu.model,
                  "settings": [s.to_dict() for s in cam.menu.all()]})


async def quick(request: web.Request) -> web.Response:
    cam = camera(request)
    items = []
    if cam.menu is not None:
        for cmd_type, label in QUICK:
            s = cam.menu.by_cmd_type(cmd_type)
            if s is not None:
                items.append({"key": cmd_type, "label": label, "setting": s.to_dict()})
    return _json({"items": items, "exposure": cam.exposure_dict()})


@_guard
async def set_setting(request: web.Request) -> web.Response:
    body = await _body(request)
    return _json(await camera(request).set_setting(request.match_info["id"], body["option"]))


@_guard
async def set_raw(request: web.Request) -> web.Response:
    """setsetting with any type/value (e.g. focal=1024/256, shtrspeed=2048/256)."""
    body = await _body(request)
    t = str(body["type"])
    if t == "device_name":
        raise ValueError("device_name is managed by the server")
    v2 = body.get("value2")
    return _json(await camera(request).set_raw(t, str(body["value"]), str(v2) if v2 is not None else None))


async def get_setting_one(request: web.Request) -> web.Response:
    cam = camera(request)
    sid = request.match_info["id"]
    if cam.menu is None or sid not in cam.menu.settings:
        return _json({"error": "unknown setting %s" % sid}, 404)
    return _json(cam.menu.settings[sid].to_dict())


@_guard
async def getsetting(request: web.Request) -> web.Response:
    """Raw getsetting&type=<type> (e.g. focal, shtrspeed, iso, touch_type)."""
    return _json(await camera(request).getsetting(request.match_info["type"]))


@_guard
async def camcmd(request: web.Request) -> web.Response:
    """camcmd passthrough. poweroff must go through /api/power/off."""
    body = await _body(request)
    value = str(body["value"])
    if value == "poweroff":
        raise ValueError("use POST /api/power/off with {\"confirm\": true}")
    return _json(await camera(request).camcmd(value))


async def state_raw(request: web.Request) -> web.Response:
    return _json(camera(request).state)


async def info(request: web.Request) -> web.Response:
    cam = camera(request)
    return _json({"ddd": cam.ddd, "capability": cam.capability, "lens": cam.lens,
                  "touch_type": cam.touch_type, "exposure": cam.exposure_dict()})


@_guard
async def capture(request: web.Request) -> web.Response:
    return _json(await camera(request).capture())


@_guard
async def video_start(request: web.Request) -> web.Response:
    return _json(await camera(request).video(True))


@_guard
async def video_stop(request: web.Request) -> web.Response:
    return _json(await camera(request).video(False))


@_guard
async def touch(request: web.Request) -> web.Response:
    b = await _body(request)
    return _json(await camera(request).touch_af(int(b["x"]), int(b["y"])))


@_guard
async def af(request: web.Request) -> web.Response:
    return _json(await camera(request).oneshot_af())


@_guard
async def zoom(request: web.Request) -> web.Response:
    b = await _body(request)
    return _json(await camera(request).zoom(b["action"]))


@_guard
async def poweroff(request: web.Request) -> web.Response:
    b = await _body(request)
    if b.get("confirm") is not True:
        return _json({"error": "send {\"confirm\": true}; the camera cannot be woken over Wi-Fi"}, 400)
    return _json(await camera(request).poweroff())


async def events(request: web.Request) -> web.Response:
    return _json(list(camera(request).events))


def _source(request: web.Request) -> FrameSource:
    name = request.query.get("source", "wifi")
    src = session(request).sources.get(name)
    if src is None:
        raise web.HTTPNotFound(text="no frame source %r" % name)
    return src


async def frame(request: web.Request) -> web.Response:
    src = _source(request)
    if src.frame is None:
        got = await src.next_frame(0, timeout=5)
        if got is None:
            return _json({"error": "no live view frame yet"}, 503)
    return web.Response(body=src.frame, content_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


async def mjpeg(request: web.Request) -> web.StreamResponse:
    src = _source(request)
    boundary = "lumixframe"
    resp = web.StreamResponse(headers={
        "Content-Type": "multipart/x-mixed-replace; boundary=%s" % boundary,
        "Cache-Control": "no-store", "Pragma": "no-cache"})
    await resp.prepare(request)
    src.viewers += 1
    seq = 0
    try:
        while True:
            got = await src.next_frame(seq, timeout=10)
            if got is None:
                continue
            seq, jpg = got
            await resp.write(("--%s\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                              % (boundary, len(jpg))).encode() + jpg + b"\r\n")
    except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
        pass
    finally:
        src.viewers -= 1
    return resp


async def ws(request: web.Request) -> web.WebSocketResponse:
    mgr = request.app[SESSIONS]
    alias = "cam" not in request.match_info      # /api/ws follows the active camera
    sess = session(request) if not alias else mgr.active()
    sock = web.WebSocketResponse(heartbeat=20)
    await sock.prepare(request)
    if sess is None:
        await sock.send_json({"type": "status", "data": NO_CAMERA_STATUS})
        await sock.send_json({"type": "cameras", "data": {"active": None, "cameras": []}})
        while mgr.active() is None and not sock.closed:
            await asyncio.sleep(1)
        await sock.close()
        return sock
    cam = sess.cam
    await sock.send_json({"type": "cameras", "data": {"active": mgr.active_id, "cameras": mgr.list()}},
                         dumps=lambda o: json.dumps(o, default=str))
    await sock.send_json({"type": "status", "data": cam.status_dict()}, dumps=lambda o: json.dumps(o, default=str))
    await sock.send_json({"type": "exposure", "data": cam.exposure_dict()})
    await sock.send_json({"type": "events", "data": list(cam.events)[-100:]})
    async with cam.bus.subscribe() as q:
        async def pump() -> None:
            while True:
                msg = await q.get()
                await sock.send_str(json.dumps(msg, default=str))

        async def tick() -> None:  # live-view stats/rec timer refresh
            while True:
                await asyncio.sleep(1)
                if alias and mgr.active() is not sess:
                    # the active camera changed: let the client reconnect to the new one
                    await sock.send_str(json.dumps({"type": "active", "data": {"active": mgr.active_id}}))
                    await sock.close()
                    return
                if mgr.sessions.get(sess.id) is not sess:     # removed or restarted
                    await sock.close()
                    return
                await sock.send_str(json.dumps({"type": "status", "data": cam.status_dict()}, default=str))

        tasks = [asyncio.ensure_future(pump()), asyncio.ensure_future(tick())]
        try:
            async for m in sock:
                if m.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            for t in tasks:
                t.cancel()
    return sock


# ---------------------------------------------------------------------- app
def build_app(mgr: "SessionManager | Camera") -> web.Application:
    """App for a SessionManager (a bare Camera is wrapped in a one-camera manager)."""
    if isinstance(mgr, Camera):
        mgr = SessionManager.single(mgr)
    app = web.Application()
    app[SESSIONS] = mgr
    r = app.router
    r.add_get("/", index)
    r.add_get("/manifest.webmanifest", manifest)
    r.add_static("/static", STATIC)
    for method, path, handler in (
        ("GET", "/api/status", status),
        ("GET", "/api/settings", settings),
        ("GET", "/api/settings/{id}", get_setting_one),
        ("POST", "/api/settings/{id}", set_setting),
        ("GET", "/api/getsetting/{type}", getsetting),
        ("POST", "/api/camcmd", camcmd),
        ("GET", "/api/state", state_raw),
        ("GET", "/api/info", info),
        ("POST", "/api/setting", set_raw),
        ("GET", "/api/quick", quick),
        ("POST", "/api/capture", capture),
        ("POST", "/api/video/start", video_start),
        ("POST", "/api/video/stop", video_stop),
        ("POST", "/api/touch", touch),
        ("POST", "/api/af", af),
        ("POST", "/api/zoom", zoom),
        ("POST", "/api/power/off", poweroff),
        ("GET", "/api/events", events),
        ("GET", "/api/liveview.jpg", frame),
        ("GET", "/api/liveview.mjpg", mjpeg),
        ("GET", "/api/ws", ws),
    ):
        camera_route(app, method, path, handler)
    from server import cameras_api  # /api/cameras, /api/models (imports this module)
    cameras_api.setup(app)
    from server import ui_api  # presets + connection (imports this module)
    ui_api.setup(app)
    from server import files_api  # card files + import jobs (imports this module)
    files_api.setup(app)
    from server import ble_api  # Bluetooth wake / power-cycle + err_busy watchdog
    ble_api.setup(app)
    for setup in FEATURES:
        setup(app)

    async def on_startup(app: web.Application) -> None:
        await mgr.start()

    async def on_cleanup(app: web.Application) -> None:
        await mgr.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app
