"""Camera registry + model profile routes.

GET    /api/cameras                  {"active", "cameras": [summary...], "models": [...]}
POST   /api/cameras                  {"nickname"?, "model"?, "ip"?, "ports"?, "settings"?, "activate"?}
GET    /api/cameras/{cam}            summary + "entry" (registry) + "profile" (full model profile)
PATCH  /api/cameras/{cam}            {"nickname"?, "model"?, "ip"?, "ports"?, "settings"?, "device_name"?, "client_uuid"?,
                                      "ble"?: {"name"?, "home_ssid"?}}   (POST works too)
DELETE /api/cameras/{cam}            forget the camera (stops its session)
POST   /api/cameras/{cam}/activate   make it the camera behind the /api/* aliases
GET    /api/models                   model profiles (short)
GET    /api/models/{id}              one profile, full (setup steps, quirks, capabilities)

{cam} is the camera id; a nickname works too.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Dict

from aiohttp import web

from lumix import models, registry
from lumix.registry import RegistryError
from server.app import SESSIONS, _body, _json, session

PORT_KEYS = ("http", "upnp", "files", "liveview")


def _fields(b: Dict[str, Any]) -> Dict[str, Any]:
    """Validated registry fields from a request body (ValueError on bad input)."""
    out: Dict[str, Any] = {}
    if "nickname" in b:
        n = str(b["nickname"] or "").strip()
        if not n:
            raise ValueError("nickname must not be empty")
        out["nickname"] = n[:64]
    if "model" in b:
        m = str(b["model"] or "auto")
        if m != models.AUTO and models.get(m) is None:
            raise ValueError("unknown model %r (see /api/models)" % m)
        out["model"] = m
    for key in ("ip", "fixed_ip"):
        if key in b:
            ip = (b[key] or "").strip() if isinstance(b[key], (str, type(None))) else None
            if ip is None and b[key] is not None:
                raise ValueError("ip must be a string")
            if ip:
                ipaddress.ip_address(ip)
            out["fixed_ip"] = ip or None
    if "ports" in b:
        if not isinstance(b["ports"], dict):
            raise ValueError("ports must be an object")
        ports = {}
        for k, v in b["ports"].items():
            if k not in PORT_KEYS:
                raise ValueError("unknown port %r (%s)" % (k, ", ".join(PORT_KEYS)))
            p = int(v)
            if not 1 <= p <= 65535:
                raise ValueError("port %s out of range" % k)
            ports[k] = p
        out["ports"] = ports
    if "settings" in b:
        if not isinstance(b["settings"], dict):
            raise ValueError("settings must be an object")
        st = {}
        for k, v in b["settings"].items():
            if k not in ("autoconnect", "watchdog", "resume_on_reconnect"):
                raise ValueError("unknown setting %r" % k)
            if not isinstance(v, bool):
                raise ValueError("%s must be true or false" % k)
            st[k] = v
        out["settings"] = st
    if "device_name" in b:
        out["device_name"] = str(b["device_name"] or "LumixRemote")[:32]
    if "client_uuid" in b:
        # The id this computer is paired as (BLE MEI0 write + Wi-Fi req_acc). Set it
        # back to the one the camera knows when a pairing was made under another id.
        cu = str(b["client_uuid"] or "").strip().upper()
        if not registry.CLIENT_UUID_RE.match(cu):
            raise ValueError("client_uuid must look like 4D454930-0100-1000-8001-XXXXXXXXXXXX")
        out["client_uuid"] = cu
    if "ble" in b:
        if not isinstance(b["ble"], dict):
            raise ValueError("ble must be an object")
        out["ble"] = {k: (str(v) if v is not None else None) for k, v in b["ble"].items()
                      if k in ("name", "home_ssid")}
    return out


def _err(e: Exception) -> web.Response:
    return _json({"error": str(e)}, 404 if isinstance(e, RegistryError) and "no camera" in str(e) else 400)


async def list_cameras(request: web.Request) -> web.Response:
    m = request.app[SESSIONS]
    return _json({"active": m.active_id, "cameras": m.list(), "models": models.catalog()})


async def add_camera(request: web.Request) -> web.Response:
    m = request.app[SESSIONS]
    b = await _body(request)
    try:
        f = _fields(b)
        s = await m.add(f.pop("nickname", None), activate=bool(b.get("activate")), **f)
    except (ValueError, RegistryError) as e:
        return _err(e)
    return _json(_detail(s), 201)


def _detail(s: Any) -> Dict[str, Any]:
    d = s.summary()
    d["entry"] = s.reload()
    d["profile"] = s.cam.profile.to_dict()
    d["status_detail"] = s.cam.status_dict() if s.running else None
    return d


async def get_camera(request: web.Request) -> web.Response:
    return _json(_detail(session(request)))


async def update_camera(request: web.Request) -> web.Response:
    s = session(request)
    b = await _body(request)
    try:
        s = await request.app[SESSIONS].update(s.id, **_fields(b))
    except (ValueError, RegistryError) as e:
        return _err(e)
    return _json(_detail(s))


async def delete_camera(request: web.Request) -> web.Response:
    s = session(request)
    try:
        e = await request.app[SESSIONS].remove(s.id)
    except RegistryError as e:
        return _err(e)
    m = request.app[SESSIONS]
    return _json({"removed": e["id"], "active": m.active_id})


async def activate(request: web.Request) -> web.Response:
    s = session(request)
    s = await request.app[SESSIONS].set_active(s.id)
    return _json(_detail(s))


async def list_models(request: web.Request) -> web.Response:
    return _json({"models": models.catalog(), "auto": models.AUTO})


async def get_model(request: web.Request) -> web.Response:
    p = models.get(request.match_info["id"])
    if p is None:
        return _json({"error": "unknown model %r" % request.match_info["id"]}, 404)
    return _json(p.to_dict())


def setup(app: web.Application) -> None:
    r = app.router
    r.add_get("/api/cameras", list_cameras)
    r.add_post("/api/cameras", add_camera)
    r.add_get("/api/cameras/{cam}", get_camera)
    r.add_patch("/api/cameras/{cam}", update_camera)
    r.add_post("/api/cameras/{cam}", update_camera)
    r.add_delete("/api/cameras/{cam}", delete_camera)
    r.add_post("/api/cameras/{cam}/activate", activate)
    r.add_get("/api/models", list_models)
    r.add_get("/api/models/{id}", get_model)
