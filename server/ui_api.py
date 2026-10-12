"""Routes used by the web UI beyond the core API: presets (Auto) and connection.

POST /api/presets/auto      {"on": true|false, "target": "photo"|"video"}
GET  /api/presets           current preset state (auto on/off, saved manual values)
GET  /api/connection        status + remembered camera + camera-side setup steps
POST /api/connection/reconnect  {"ip"?: "...", "forget"?: true}
"""

from __future__ import annotations

from aiohttp import web

from lumix.presets import AutoPreset
from server.app import CAMERA, _body, _guard, _json

AUTO = web.AppKey("auto_preset", AutoPreset)

CAMERA_STEPS = [
    "Menu → Setup (wrench) → Wi-Fi",
    "Wi-Fi Function → New Connection",
    "Remote Shooting & View",
    "Press DISP to switch to \"Via Network\"",
    "Pick your home Wi-Fi (same network as this computer)",
    "Approve this device on the camera if it asks",
]


async def presets(request: web.Request) -> web.Response:
    return _json({"auto": request.app[AUTO].to_dict()})


@_guard
async def preset_auto(request: web.Request) -> web.Response:
    b = await _body(request)
    on = b.get("on", True)
    if not isinstance(on, bool):
        raise ValueError("on must be true or false")
    return _json(await request.app[AUTO].set(on, str(b.get("target", "photo"))))


async def connection(request: web.Request) -> web.Response:
    cam = request.app[CAMERA]
    st = cam.status_dict()
    return _json({"status": st["status"], "message": st["message"], "ip": cam.ip,
                  "ip_fixed": cam.ip_fixed, "last_ip": cam._load_state().get("last_ip"),
                  "camera": st["camera"], "connected_at": st["connected_at"],
                  "liveview": st["liveview"], "steps": CAMERA_STEPS})


@_guard
async def reconnect(request: web.Request) -> web.Response:
    b = await _body(request)
    ip = b.get("ip")
    if ip is not None and not isinstance(ip, str):
        raise ValueError("ip must be a string")
    request.app[CAMERA].request_reconnect(ip=(ip or "").strip() or None, forget=bool(b.get("forget")))
    return _json({"ok": True})


def setup(app: web.Application) -> None:
    app[AUTO] = AutoPreset(app[CAMERA])
    r = app.router
    r.add_get("/api/presets", presets)
    r.add_post("/api/presets/auto", preset_auto)
    r.add_get("/api/connection", connection)
    r.add_post("/api/connection/reconnect", reconnect)
