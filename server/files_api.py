"""File browser API: list card files, thumbnails/previews, downloads, import jobs.

Routes (see README "Files API"):
  GET  /api/files?offset=0&limit=60&kind=all|photo|video[&refresh=1]
  GET  /api/files/status
  POST /api/files/done
  GET  /api/files/{id}/thumb | /preview
  GET  /api/files/{id}/download?variant=original|raw
  POST /api/files/import {ids, dest?, include_raw?}
  GET  /api/jobs, GET /api/jobs/{id}, POST /api/jobs/{id}/cancel
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from typing import Any

import aiohttp
from aiohttp import web

from lumix.camera import Busy
from lumix.didl import BrowseError
from lumix.files import FileBrowser, ModeError, NotFound

log = logging.getLogger("server.files")

FILES = web.AppKey("files", FileBrowser)


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=str))


def _guard(fn):
    async def wrapper(request: web.Request) -> web.StreamResponse:
        try:
            return await fn(request)
        except NotFound as e:
            return _json({"error": "not found: %s" % e}, 404)
        except (Busy, ModeError) as e:
            return _json({"error": str(e)}, 409)
        except BrowseError as e:
            return _json({"error": str(e)}, 502)
        except (KeyError, ValueError, TypeError) as e:
            return _json({"error": "bad request: %s" % e}, 400)
        except (ConnectionResetError, asyncio.CancelledError):
            raise
        except Exception as e:
            log.exception("files handler failed")
            return _json({"error": "%s: %s" % (type(e).__name__, e)}, 502)
    return wrapper


@_guard
async def list_files(request: web.Request) -> web.Response:
    q = request.query
    return _json(await request.app[FILES].list(
        offset=int(q.get("offset", 0)), limit=int(q.get("limit", 60)), kind=q.get("kind", "all"),
        refresh=q.get("refresh") in ("1", "true", "yes")))


async def files_status(request: web.Request) -> web.Response:
    return _json(request.app[FILES].status())


@_guard
async def done(request: web.Request) -> web.Response:
    return _json(await request.app[FILES].leave())


def _image(which: str):
    @_guard
    async def handler(request: web.Request) -> web.Response:
        body, ctype = await request.app[FILES].image(request.match_info["id"], which)
        return web.Response(body=body, content_type=ctype,
                            headers={"Cache-Control": "private, max-age=3600"})
    return handler


def _disposition(name: str) -> str:
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "")
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_name, urllib.parse.quote(name))


@_guard
async def download(request: web.Request) -> web.StreamResponse:
    fb = request.app[FILES]
    variant = request.query.get("variant", "original")
    rng = request.headers.get("Range")
    async with fb.open_stream(request.match_info["id"], variant, rng) as (r, name, res):
        if r.status not in (200, 206, 416):
            raise ModeError("camera HTTP %d for %s" % (r.status, res.name))
        headers = {"Content-Disposition": _disposition(name), "Accept-Ranges": "bytes",
                   "Content-Type": r.headers.get("Content-Type", "application/octet-stream")}
        for h in ("Content-Length", "Content-Range", "Last-Modified"):
            if h in r.headers:
                headers[h] = r.headers[h]
        resp = web.StreamResponse(status=r.status, headers=headers)
        await resp.prepare(request)
        if request.method == "HEAD":
            return resp
        try:
            async for chunk in r.content.iter_chunked(256 * 1024):
                await resp.write(chunk)
                fb.touch()
        except (ConnectionResetError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.info("download %s aborted: %s", name, e)
            return resp
        await resp.write_eof()
        return resp


@_guard
async def start_import(request: web.Request) -> web.Response:
    body = await request.json() if request.can_read_body else {}
    ids = body["ids"]
    if not isinstance(ids, list):
        raise ValueError("ids must be a list")
    job = request.app[FILES].start_import(ids, body.get("dest"), body.get("include_raw", True))
    return _json(job.to_dict(), 202)


async def list_jobs(request: web.Request) -> web.Response:
    return _json([j.to_dict() for j in reversed(list(request.app[FILES].jobs.values()))])


async def get_job(request: web.Request) -> web.Response:
    job = request.app[FILES].jobs.get(request.match_info["id"])
    if job is None:
        return _json({"error": "no such job"}, 404)
    return _json(job.to_dict())


async def cancel_job(request: web.Request) -> web.Response:
    try:
        job = request.app[FILES].cancel_job(request.match_info["id"])
    except KeyError:
        return _json({"error": "no such job"}, 404)
    await asyncio.sleep(0.1)
    return _json(job.to_dict())


def setup(app: web.Application) -> None:
    from server.app import CAMERA  # late import: server.app imports this module
    fb = FileBrowser(app[CAMERA])
    app[FILES] = fb
    r = app.router
    r.add_get("/api/files", list_files)
    r.add_get("/api/files/status", files_status)
    r.add_post("/api/files/done", done)
    r.add_post("/api/files/import", start_import)
    r.add_get("/api/files/{id}/thumb", _image("thumb"))
    r.add_get("/api/files/{id}/preview", _image("preview"))
    r.add_get("/api/files/{id}/download", download)
    r.add_get("/api/jobs", list_jobs)
    r.add_get("/api/jobs/{id}", get_job)
    r.add_post("/api/jobs/{id}/cancel", cancel_job)

    async def on_startup(app: web.Application) -> None:
        fb.start()

    async def on_cleanup(app: web.Application) -> None:
        await fb.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
