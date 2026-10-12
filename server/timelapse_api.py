"""Timelapse routes (see lumix/timelapse.py and README "Timelapse").

GET  /api/timelapse                 status: {job, active, history, capabilities, frame_url}
POST /api/timelapse[/start]         {interval, count?, duration?, start_at?, native?, ae_lock?,
                                     import?, import_mode?, video?, fps?, name?, dest?}
POST /api/timelapse/pause | /resume | /stop
GET  /api/timelapse/frame.jpg       last frame (live view at the moment of the last shot)

All of them also exist under /api/cameras/{cam}/... (server.app.camera_route).
"""

from __future__ import annotations

import os

from aiohttp import web

from lumix.timelapse import TimelapseError
from server.app import _body, _guard, _json, camera_route, session


def _tl_guard(fn):
    @_guard
    async def wrapper(request: web.Request) -> web.Response:
        try:
            return await fn(request)
        except TimelapseError as e:
            msg = str(e)
            busy = any(w in msg for w in ("already", "not connected", "file browsing", "busy"))
            return _json({"error": msg}, 409 if busy else 400)
    return wrapper


async def status(request: web.Request) -> web.Response:
    return _json(session(request).timelapse.status())


@_tl_guard
async def start(request: web.Request) -> web.Response:
    return _json(await session(request).timelapse.start_job(await _body(request)), 201)


@_tl_guard
async def pause(request: web.Request) -> web.Response:
    return _json(await session(request).timelapse.pause("user"))


@_tl_guard
async def resume(request: web.Request) -> web.Response:
    return _json(await session(request).timelapse.resume())


@_tl_guard
async def stop(request: web.Request) -> web.Response:
    return _json(await session(request).timelapse.stop_job())


async def frame(request: web.Request) -> web.StreamResponse:
    tl = session(request).timelapse
    if not os.path.exists(tl.frame_path):
        return _json({"error": "no frame yet"}, 404)
    return web.FileResponse(tl.frame_path, headers={"Cache-Control": "no-store",
                                                    "Content-Type": "image/jpeg"})


def setup(app: web.Application) -> None:
    camera_route(app, "GET", "/api/timelapse", status)
    camera_route(app, "POST", "/api/timelapse", start)
    camera_route(app, "POST", "/api/timelapse/start", start)
    camera_route(app, "POST", "/api/timelapse/pause", pause)
    camera_route(app, "POST", "/api/timelapse/resume", resume)
    camera_route(app, "POST", "/api/timelapse/stop", stop)
    camera_route(app, "GET", "/api/timelapse/frame.jpg", frame)
