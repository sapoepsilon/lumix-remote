"""Routes used by the web UI beyond the core API: presets (Auto) and connection.

POST /api/presets/auto      {"on": true|false, "target": "photo"|"video"}
GET  /api/presets           current preset state (auto on/off, saved manual values)
GET  /api/connection        status + remembered camera + camera-side setup steps
POST /api/connection/reconnect  {"ip"?: "...", "forget"?: true}

All of them also exist under /api/cameras/{cam}/... (see server.app.camera_route).
"""

from __future__ import annotations

from aiohttp import web

from lumix import models
from server.app import _body, _guard, _json, camera_route, session

# Kept for older clients; the steps now come from the camera's model profile.
CAMERA_STEPS = [
    "Menu → Setup (wrench) → Wi-Fi",
    "Wi-Fi Function → New Connection",
    "Remote Shooting & View",
    "Press DISP to switch to \"Via Network\"",
    "Pick your home Wi-Fi (same network as this computer)",
    "Approve this device on the camera if it asks",
]


async def presets(request: web.Request) -> web.Response:
    return _json({"auto": session(request).auto.to_dict()})


@_guard
async def preset_auto(request: web.Request) -> web.Response:
    b = await _body(request)
    on = b.get("on", True)
    if not isinstance(on, bool):
        raise ValueError("on must be true or false")
    return _json(await session(request).auto.set(on, str(b.get("target", "photo"))))


async def connection(request: web.Request) -> web.Response:
    s = session(request)
    cam = s.cam
    st = cam.status_dict()
    prof = cam.profile
    if not cam.ddd and (s.entry.get("model") or "auto") == models.AUTO:
        prof = models.S5     # not identified yet: show the verified body's exact menu paths
    steps = [x["title"] for x in prof.setup_wifi] + ["Approve this device on the camera if it asks"]
    return _json({"camera_id": s.id, "nickname": s.entry.get("nickname"),
                  "status": st["status"], "message": st["message"], "ip": cam.ip,
                  "ip_fixed": cam.ip_fixed, "last_ip": cam._load_state().get("last_ip"),
                  "camera": st["camera"], "connected_at": st["connected_at"],
                  "liveview": st["liveview"], "steps": steps,
                  "model": {"id": prof.id, "name": prof.name, "setup_wifi": prof.setup_wifi,
                            "setup_ble": prof.setup_ble, "approve_hint": prof.approve_hint,
                            "menu_assist": prof.to_dict()["menu_assist"]}})


@_guard
async def reconnect(request: web.Request) -> web.Response:
    b = await _body(request)
    ip = b.get("ip")
    if ip is not None and not isinstance(ip, str):
        raise ValueError("ip must be a string")
    sess = session(request)
    forget = bool(b.get("forget"))
    started = sess.cam.request_reconnect(ip=(ip or "").strip() or None, forget=forget)
    if forget and sess.entry.get("fixed_ip") and not ip:
        # "search from scratch" must not leave a fixed address behind in cameras.json
        # (the session would ignore it, and setting the same address again later
        # would not reconnect); this restarts the session without it
        from server.app import SESSIONS
        await request.app[SESSIONS].update(sess.id, fixed_ip=None)
    # already_connecting: a handshake to this camera is running; it is not
    # restarted (that would re-run req_acc/SUBSCRIBE, the S5 wedge window)
    return _json({"ok": True, "already_connecting": not started})


def setup(app: web.Application) -> None:
    camera_route(app, "GET", "/api/presets", presets)
    camera_route(app, "POST", "/api/presets/auto", preset_auto)
    camera_route(app, "GET", "/api/connection", connection)
    camera_route(app, "POST", "/api/connection/reconnect", reconnect)
