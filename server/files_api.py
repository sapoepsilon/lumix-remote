"""File browser API: list card files, thumbnails/previews, downloads, import jobs.

Routes (see README "Files API"):
  GET  /api/files?offset=0&limit=60&kind=all|photo|video[&refresh=1]
  GET  /api/files/status
  POST /api/files/done
  GET  /api/files/{id}/thumb | /preview
  GET  /api/files/{id}/download?variant=original|raw
  POST /api/files/import {ids, dest?, include_raw?}
  GET  /api/jobs, GET /api/jobs/{id}, POST /api/jobs/{id}/cancel|pause|resume
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
from typing import Any

import aiohttp
from aiohttp import web

from lumix.camera import Busy
from lumix.didl import BrowseError
from lumix.files import FileBrowser, ModeError, NotFound

log = logging.getLogger("server.files")


def _fb(request: web.Request) -> FileBrowser:
    from server.app import session  # late import: server.app imports this module
    return session(request).files


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=str))


def _guard(fn):
    async def wrapper(request: web.Request) -> web.StreamResponse:
        try:
            return await fn(request)
        except web.HTTPException:
            raise
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
    return _json(await _fb(request).list(
        offset=int(q.get("offset", 0)), limit=int(q.get("limit", 60)), kind=q.get("kind", "all"),
        refresh=q.get("refresh") in ("1", "true", "yes")))


async def files_status(request: web.Request) -> web.Response:
    return _json(_fb(request).status())


@_guard
async def done(request: web.Request) -> web.Response:
    """Back to shooting. Running imports are checkpointed (``paused``, by
    "shooting") and continue in the next file browsing session or via
    POST /api/jobs/{id}/resume."""
    return _json(await _fb(request).leave(pause_jobs=True))


def _image(which: str):
    @_guard
    async def handler(request: web.Request) -> web.Response:
        body, ctype = await _fb(request).image(request.match_info["id"], which)
        return web.Response(body=body, content_type=ctype,
                            headers={"Cache-Control": "private, max-age=3600"})
    return handler


def _disposition(name: str) -> str:
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "")
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_name, urllib.parse.quote(name))


@_guard
async def download(request: web.Request) -> web.StreamResponse:
    fb = _fb(request)
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
        if "Content-Length" not in headers:
            # the camera may omit it; we know the size from the DIDL / Content-Range
            m = re.match(r"bytes (\d+)-(\d+)/", headers.get("Content-Range", ""))
            if r.status == 206 and m:
                headers["Content-Length"] = str(int(m.group(2)) - int(m.group(1)) + 1)
            elif r.status == 200 and res.size:
                headers["Content-Length"] = str(res.size)
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
    job = _fb(request).start_import(ids, body.get("dest"), body.get("include_raw", True))
    return _json(job.to_dict(), 202)


async def list_jobs(request: web.Request) -> web.Response:
    return _json([j.to_dict() for j in reversed(list(_fb(request).jobs.values()))])


async def get_job(request: web.Request) -> web.Response:
    job = _fb(request).jobs.get(request.match_info["id"])
    if job is None:
        return _json({"error": "no such job"}, 404)
    return _json(job.to_dict())


async def cancel_job(request: web.Request) -> web.Response:
    try:
        job = _fb(request).cancel_job(request.match_info["id"])
    except KeyError:
        return _json({"error": "no such job"}, 404)
    await asyncio.sleep(0.1)
    return _json(job.to_dict())


@_guard
async def pause_job(request: web.Request) -> web.Response:
    try:
        job = await _fb(request).pause_job(request.match_info["id"], "user")
    except KeyError:
        return _json({"error": "no such job"}, 404)
    return _json(job.to_dict())


@_guard
async def resume_job(request: web.Request) -> web.Response:
    try:
        job = _fb(request).resume_job(request.match_info["id"])
    except KeyError:
        return _json({"error": "no such job"}, 404)
    await asyncio.sleep(0.05)
    return _json(job.to_dict(), 202)


def setup(app: web.Application) -> None:
    """Routes; each camera session owns its FileBrowser (started with the session)."""
    from server.app import camera_route  # late import: server.app imports this module
    for method, path, handler in (
        ("GET", "/api/files", list_files),
        ("GET", "/api/files/status", files_status),
        ("POST", "/api/files/done", done),
        ("POST", "/api/files/import", start_import),
        ("GET", "/api/files/{id}/thumb", _image("thumb")),
        ("GET", "/api/files/{id}/preview", _image("preview")),
        ("GET", "/api/files/{id}/download", download),
        ("GET", "/api/jobs", list_jobs),
        ("GET", "/api/jobs/{id}", get_job),
        ("POST", "/api/jobs/{id}/cancel", cancel_job),
        ("POST", "/api/jobs/{id}/pause", pause_job),
        ("POST", "/api/jobs/{id}/resume", resume_job),
    ):
        camera_route(app, method, path, handler)
