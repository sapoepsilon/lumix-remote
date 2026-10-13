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

from aiohttp import WSCloseCode, WSMsgType, web

from lumix.camera import Busy, Camera
from lumix.liveview import FrameSource
from server import quiesce
from server.sessions import CameraSession, SessionManager

log = logging.getLogger("server")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

SESSIONS = web.AppKey("sessions", SessionManager)
# Long-lived responses (WebSockets, MJPEG streams), closed first on shutdown so a
# browser tab never holds the graceful stop past `lumix server stop`'s SIGKILL.
OPEN_STREAMS = web.AppKey("open_streams", set)
CAMERA_PREFIX = "/api/cameras/{cam}"
# Work a request started (POST/PATCH/PUT/DELETE handlers) runs as a server-owned
# task: closing the browser tab mid-request never cancels it half-way.
DETACHED = web.AppKey("detached", set)

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


# Methods whose handlers change something (camera commands, job start/stop, BLE).
MUTATING = ("POST", "PUT", "PATCH", "DELETE")


@web.middleware
async def detach_mutations(request: web.Request, handler: Any) -> web.StreamResponse:
    """Run state-changing requests in a server-owned task.

    A client that goes away (tab closed, page reloaded, phone locked) must never
    stop what it started. aiohttp >= 3.9 does not cancel handlers on disconnect by
    default, but a server built with ``handler_cancellation=True`` (or a proxy that
    cancels) would cut a command sequence -- stopstream, recmode, startstream; a
    BLE wake; a timelapse arming the camera -- in the middle. The request only
    waits for the task; if it is cancelled the task carries on and its result is
    logged."""
    if request.method not in MUTATING:
        return await handler(request)
    task = asyncio.ensure_future(handler(request))
    keep = request.app[DETACHED]
    keep.add(task)

    def done(t: "asyncio.Task[Any]") -> None:
        keep.discard(t)
        if t.cancelled():
            return
        e = t.exception()
        if e is not None and not isinstance(e, web.HTTPException):
            log.warning("%s %s (client gone) failed: %s: %s", request.method, request.path,
                        type(e).__name__, e)

    task.add_done_callback(done)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if not task.done():
            log.info("%s %s: client went away; the request keeps running on the server",
                     request.method, request.path)
        raise


# ------------------------------------------------------------------ handlers
def _asset_version() -> str:
    """Changes whenever app.js / style.css change (cache-busting query)."""
    m = 0.0
    for name in ("app.js", "style.css"):
        try:
            m = max(m, os.path.getmtime(os.path.join(STATIC, name)))
        except OSError:
            pass
    return "%x" % int(m)


async def index(request: web.Request) -> web.Response:
    # Versioned asset URLs: a home-screen web app must not keep running a cached
    # app.js against a newer server.
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        html = f.read()
    v = _asset_version()
    for asset in ("/static/app.js", "/static/style.css"):
        html = html.replace('"%s"' % asset, '"%s?v=%s"' % (asset, v))
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})


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
    s = session(request)
    st = s.cam.status_dict()
    if not s.running:
        # not "idle" forever: say why nothing happens
        mgr = request.app[SESSIONS]
        why = ("this server was started for another camera (--camera-ip)"
               if mgr.opts.camera_ip and not mgr.opts.all_cameras
               else "Connect automatically is off for this camera")
        st.update(status="paused", message="not connecting: %s" % why)
    return _json(st)


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


CAMCTRL_DENY = ("fw_update_mode", "lens_fw_update_mode")


@_guard
async def camctrl(request: web.Request) -> web.Response:
    """camctrl passthrough ({type, value, value2?}), e.g. interval set|cancel.
    Firmware-update types are refused."""
    body = await _body(request)
    t, v = str(body["type"]), str(body["value"])
    if t in CAMCTRL_DENY:
        raise ValueError("camctrl %s is not allowed" % t)
    v2 = body.get("value2")
    cam = camera(request)
    async with cam.exclusive("camctrl") as tr:
        r = await tr.cgi("camctrl", t, v, str(v2) if v2 is not None else None)
    cam.log_event("cmd", "camctrl %s=%s%s -> %s" % (t, v, "/%s" % v2 if v2 is not None else "", r.result))
    return _json({"result": r.result, "type": t, "value": v})


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


async def _shooting(request: web.Request) -> Camera:
    """Shooting commands while file browsing is on (play mode, where the camera
    answers err_reject): leave browsing first, unless an import is copying from
    the card (then 409; `lumix files done` / closing Files pauses it)."""
    s = session(request)
    fb = s.files
    if fb.active:
        if fb.running_jobs() or fb.pins:
            raise Busy("the camera is in playback for an import from the card; wait for it, "
                       "or leave Files (`lumix files done`) to pause it")
        r = await fb.leave()
        if not r.get("left") or s.cam.state.get("cammode") != "rec":
            raise Busy("the camera did not switch back from playback (%s)" % r.get("recmode", r.get("reason")))
    cam = s.cam
    if cam.state.get("cammode") == "play" and cam.liveview_wanted and not cam.interval_active:
        # playback chosen on the body (or a mode switch that didn't happen): a shutter
        # press is an explicit request to shoot, so switch back like leaving Files does
        async with cam.exclusive("shooting"):
            ok = await cam.start_liveview("shutter pressed in playback")
        if not ok and cam.state.get("cammode") != "rec":
            raise Busy("the camera stays in playback (%s); switch it to shooting on the body"
                       % ("wedged" if cam.wedged else "recmode refused"))
    return cam


@_guard
async def capture(request: web.Request) -> web.Response:
    return _json(await (await _shooting(request)).capture())


@_guard
async def video_start(request: web.Request) -> web.Response:
    with camera(request).video_starting():      # a stop sent now waits for this start
        return _json(await (await _shooting(request)).video(True))


@_guard
async def video_stop(request: web.Request) -> web.Response:
    return _json(await camera(request).video(False))


@_guard
async def touch(request: web.Request) -> web.Response:
    b = await _body(request)
    x, y = int(b["x"]), int(b["y"])
    return _json(await (await _shooting(request)).touch_af(x, y))


@_guard
async def af(request: web.Request) -> web.Response:
    return _json(await (await _shooting(request)).oneshot_af())


@_guard
async def zoom(request: web.Request) -> web.Response:
    b = await _body(request)
    return _json(await camera(request).zoom(b["action"]))


@_guard
async def poweroff(request: web.Request) -> web.Response:
    b = await _body(request)
    if b.get("confirm") is not True:
        return _json({"error": "send {\"confirm\": true}; the camera cannot be woken over Wi-Fi",
                      "plan": quiesce.plan(session(request), "poweroff")}, 400)
    return _json(await quiesce.power_off(session(request)))


async def power_plan(request: web.Request) -> web.Response:
    """What power-off (or ?reason=server-stop|remove|files|shooting) would pause."""
    return _json(quiesce.plan(session(request), request.query.get("reason", "poweroff")))


@_guard
async def quiesce_now(request: web.Request) -> web.Response:
    b = await _body(request)
    return _json(await session(request).quiesce(str(b.get("reason") or "poweroff")))


@_guard
async def resume_now(request: web.Request) -> web.Response:
    return _json(await quiesce.resume_after_reconnect(session(request), force=True))


async def events(request: web.Request) -> web.Response:
    return _json(list(camera(request).events))


def _source(request: web.Request) -> FrameSource:
    name = request.query.get("source", "wifi")
    src = session(request).sources.get(name)
    if src is None:
        raise web.HTTPNotFound(text="no frame source %r" % name)
    return src


async def frame(request: web.Request) -> web.Response:
    """Latest JPEG. ``?after=<seq>`` waits (up to 5 s) for a newer frame than
    the X-Frame-Seq a poller saw last, so polling never returns duplicates."""
    src = _source(request)
    try:
        after = int(request.query.get("after", "0"))
    except ValueError:
        raise web.HTTPBadRequest(text="after must be an integer frame sequence")
    seq, jpg = src.seq, src.frame
    if jpg is None or seq <= after:
        got = await src.next_frame(max(after, 0) if jpg is not None else 0, timeout=5)
        if got is None:
            return _json({"error": "no live view frame yet" if jpg is None else "no newer frame"}, 503)
        seq, jpg = got
    hdr = {"Cache-Control": "no-store", "X-Frame-Seq": str(seq)}
    if src.size:
        hdr["X-Frame-Width"], hdr["X-Frame-Height"] = str(src.size[0]), str(src.size[1])
    return web.Response(body=jpg, content_type="image/jpeg", headers=hdr)


async def lv_quality(request: web.Request) -> web.Response:
    return _json(camera(request).lv_quality_info())


@_guard
async def set_lv_quality(request: web.Request) -> web.Response:
    """{"liveview_quality": "vga"|"qvga"}: saved, set on the camera, stream restarted."""
    b = await _body(request)
    q = b.get("liveview_quality", b.get("quality"))
    if not isinstance(q, str):
        raise ValueError("send {\"liveview_quality\": \"vga\"}")
    return _json(await camera(request).set_liveview_quality(q))


async def lv_stats(request: web.Request) -> web.Response:
    """Frame source stats; for the Wi-Fi source also the UDP receiver counters and
    the latest datagram header (hex)."""
    src = _source(request)
    d = src.debug() if hasattr(src, "debug") else src.stats()
    if src is session(request).cam.liveview:
        d["liveview_quality"] = session(request).cam.lv_quality_info()
    return _json(d)


async def mjpeg(request: web.Request) -> web.StreamResponse:
    src = _source(request)
    boundary = "lumixframe"
    resp = web.StreamResponse(headers={
        "Content-Type": "multipart/x-mixed-replace; boundary=%s" % boundary,
        "Cache-Control": "no-store", "Pragma": "no-cache"})
    await resp.prepare(request)
    task = asyncio.current_task()
    request.app[OPEN_STREAMS].add(task)
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
        request.app[OPEN_STREAMS].discard(task)
    return resp


async def ws(request: web.Request) -> web.WebSocketResponse:
    mgr = request.app[SESSIONS]
    alias = "cam" not in request.match_info      # /api/ws follows the active camera
    sess = session(request) if not alias else mgr.active()
    sock = web.WebSocketResponse(heartbeat=20)
    await sock.prepare(request)
    request.app[OPEN_STREAMS].add(sock)
    try:
        return await _ws_session(request, mgr, alias, sess, sock)
    finally:
        request.app[OPEN_STREAMS].discard(sock)


async def _ws_session(request: web.Request, mgr: SessionManager, alias: bool,
                      sess: Optional[CameraSession], sock: web.WebSocketResponse) -> web.WebSocketResponse:
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
    app = web.Application(middlewares=[detach_mutations])
    app[SESSIONS] = mgr
    app[OPEN_STREAMS] = set()
    app[DETACHED] = set()
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
        ("POST", "/api/camctrl", camctrl),
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
        ("GET", "/api/power/plan", power_plan),
        ("POST", "/api/quiesce", quiesce_now),
        ("POST", "/api/resume", resume_now),
        ("GET", "/api/events", events),
        ("GET", "/api/liveview.jpg", frame),
        ("GET", "/api/liveview.mjpg", mjpeg),
        ("GET", "/api/liveview/stats", lv_stats),
        ("GET", "/api/liveview/quality", lv_quality),
        ("POST", "/api/liveview/quality", set_lv_quality),
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
    from server import timelapse_api  # server-driven + in-camera time-lapse
    timelapse_api.setup(app)
    for setup in FEATURES:
        setup(app)

    async def on_startup(app: web.Application) -> None:
        await mgr.start()

    async def on_shutdown(app: web.Application) -> None:
        # Browser tabs first: open WebSockets / MJPEG streams would otherwise keep
        # the graceful stop waiting until the SIGKILL.
        await close_streams(app)
        # SIGTERM/SIGINT: checkpoint imports, pause timelapses, stop recordings,
        # live view and event subscriptions before the sessions go away. Bounded:
        # `lumix server stop` / a new server SIGKILLs after 10 s.
        await mgr.quiesce_all("server-stop", timeout=7.0)

    async def on_cleanup(app: web.Application) -> None:
        await mgr.stop()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.on_cleanup.append(on_cleanup)
    return app


async def close_streams(app: web.Application) -> None:
    """Close every open WebSocket (GOING_AWAY) and end every MJPEG stream."""
    items = list(app[OPEN_STREAMS])
    socks = [x for x in items if isinstance(x, web.WebSocketResponse)]
    tasks = [x for x in items if isinstance(x, asyncio.Task)]
    for t in tasks:
        t.cancel()
    if socks:
        await asyncio.wait([asyncio.ensure_future(w.close(code=WSCloseCode.GOING_AWAY,
                                                          message=b"server shutdown"))
                            for w in socks], timeout=2)
    app[OPEN_STREAMS].clear()
