"""HTTP faces of one emulated camera: cam.cgi, UPnP (:60606), files (:50001), control API.

``EmulatorNode`` binds all four sockets itself (port 0 = pick a free one) so a
power-off can really close the camera's ports (connection refused, as with a
camera that is switched off) while the control API stays up.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import socket
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from aiohttp import web

from .camera import EmuCamera, EmuConfig, Identity
from .liveview import Streamer

log = logging.getLogger("emulator.http")

UPNP_SERVER = "Panasonic/1.0 UPnP/1.0 Panasonic-UPnP-MW/1.0"


def _bind(host: str, port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.listen(64)
    s.setblocking(False)
    return s


def _parse_range(h: str, size: int) -> Optional[Tuple[int, int]]:
    """'bytes=a-b' -> (start, end inclusive); None if unsatisfiable/invalid."""
    m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", h or "")
    if not m or (not m.group(1) and not m.group(2)):
        return None
    if not m.group(1):
        n = int(m.group(2))
        if n == 0:
            return None
        return max(0, size - n), size - 1
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else size - 1
    if a >= size or b < a:
        return None
    return a, min(b, size - 1)


@web.middleware
async def _close_after(req: web.Request, handler):
    """The camera answers every request with "Connection: close" and hangs up."""
    resp = await handler(req)
    resp.force_close()
    return resp


def _camera_app() -> web.Application:
    return web.Application(middlewares=[_close_after])


class EmulatorNode:
    def __init__(self, host: str = "127.0.0.1", http_port: int = 8080, upnp_port: int = 60606,
                 dl_port: int = 50001, control_port: Optional[int] = None,
                 ident: Optional[Identity] = None, cfg: Optional[EmuConfig] = None,
                 card_photos: int = 4, card_group: int = 120):
        self.host = host
        self.ident = ident or Identity()
        self.cfg = cfg or EmuConfig()
        self._socks: Dict[str, socket.socket] = {}
        self._want = {"http": http_port, "upnp": upnp_port, "dl": dl_port}
        self.control_port = control_port if control_port is not None else (
            http_port + 1 if http_port else 0)
        self._card = (card_photos, card_group)
        self.cam: Optional[EmuCamera] = None
        self._runners: Dict[str, web.AppRunner] = {}
        self._sites: Dict[str, web.SockSite] = {}
        self._power_lock = asyncio.Lock()
        self.ports: Dict[str, int] = {}

    # --------------------------------------------------------------- lifecycle
    async def start(self) -> "EmulatorNode":
        for k, p in self._want.items():
            s = _bind(self.host, p)
            self.ports[k] = s.getsockname()[1]
            self._socks[k] = s
        cs = _bind(self.host, self.control_port)
        self.ports["control"] = cs.getsockname()[1]
        self.cam = EmuCamera(self.ident, self.cfg, self.host, self.ports["http"],
                             self.ports["upnp"], self.ports["dl"], *self._card)
        self.cam.streamer = Streamer(self.cam)
        self.cam.on_power = lambda on: asyncio.ensure_future(self._set_power(on))
        apps = {"http": self._http_app(), "upnp": self._upnp_app(), "dl": self._dl_app(),
                "control": self._control_app()}
        for k, app in apps.items():
            r = web.AppRunner(app, access_log=None, handle_signals=False)
            await r.setup()
            self._runners[k] = r
        await self._open("control", cs)
        for k in ("http", "upnp", "dl"):
            await self._open(k, self._socks.pop(k))
        log.info("%s on %s: cam.cgi :%d, upnp :%d, files :%d, control :%d",
                 self.ident.friendly_name, self.host, self.ports["http"], self.ports["upnp"],
                 self.ports["dl"], self.ports["control"])
        return self

    async def _open(self, k: str, sock: Optional[socket.socket] = None) -> None:
        if k in self._sites:
            return
        if sock is None:
            sock = _bind(self.host, self.ports[k])
        site = web.SockSite(self._runners[k], sock, shutdown_timeout=0.5)
        self._sites[k] = site
        await site.start()

    async def _close(self, k: str) -> None:
        site = self._sites.pop(k, None)
        if site is not None:
            await site.stop()

    async def _set_power(self, on: bool) -> None:
        async with self._power_lock:
            for k in ("http", "upnp", "dl"):
                if on and self.cam is not None and self.cam.powered:
                    await self._open(k)
                elif not on:
                    await self._close(k)

    async def stop(self) -> None:
        if self.cam is not None:
            if self.cam.streamer:
                self.cam.streamer.stop()
            self.cam.boot()
        for r in self._runners.values():
            await r.cleanup()
        self._runners.clear()
        self._sites.clear()

    def describe(self) -> Dict[str, Any]:
        return {"name": self.ident.friendly_name, "host": self.host, **self.ports}

    # ------------------------------------------------------------------ cam.cgi
    def _http_app(self) -> web.Application:
        app = _camera_app()

        async def cgi(req: web.Request) -> web.Response:
            cam = self.cam
            assert cam is not None
            params = {k: v for k, v in req.query.items()}
            ctype, body = cam.handle_cgi(req.remote or "", params)
            return web.Response(body=body.encode("utf-8"), headers={
                "Content-Type": ctype, "Connection": "close", "Date": cam.http_date(),
                "Server": "Panasonic"})

        async def other(req: web.Request) -> web.Response:
            return web.Response(status=404, headers={"Server": "Panasonic", "Connection": "close"})

        app.router.add_get("/cam.cgi", cgi)
        app.router.add_route("*", "/{tail:.*}", other)
        return app

    # --------------------------------------------------------------------- UPnP
    def _upnp_app(self) -> web.Application:
        app = _camera_app()

        def hdrs(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
            assert self.cam is not None
            h = {"DATE": self.cam.http_date(), "SERVER": UPNP_SERVER, "CONNECTION": "close"}
            h.update(extra or {})
            return h

        async def ddd(req: web.Request) -> web.Response:
            assert self.cam is not None
            self.cam.record("upnp", "GET ddd", "200", req.remote or "")
            return web.Response(body=self.cam.ddd().encode(),
                                headers=hdrs({"CONTENT-TYPE": 'text/xml; charset="utf-8"'}))

        async def subscribe(req: web.Request) -> web.Response:
            assert self.cam is not None
            h = {k.upper(): v for k, v in req.headers.items()}
            status, extra = self.cam.subscribe(req.remote or "", h)
            return web.Response(status=status, headers=hdrs(extra) if status == 200
                                else {"CONNECTION": "close"})

        async def unsubscribe(req: web.Request) -> web.Response:
            assert self.cam is not None
            h = {k.upper(): v for k, v in req.headers.items()}
            status = self.cam.unsubscribe(req.remote or "", h)
            return web.Response(status=status, headers=hdrs() if status == 200
                                else {"CONNECTION": "close"})

        async def control(req: web.Request) -> web.Response:
            assert self.cam is not None
            action = req.headers.get("SOAPACTION", "")
            body = await req.read()
            if "Browse" not in action:
                from .card import SOAP_FAULT
                return web.Response(status=500, body=(SOAP_FAULT % (401, "Invalid Action")).encode(),
                                    headers=hdrs({"CONTENT-TYPE": 'text/xml; charset="utf-8"'}))
            status, text = self.cam.browse(req.remote or "", body)
            return web.Response(status=status, body=text.encode("utf-8"),
                                headers=hdrs({"CONTENT-TYPE": 'text/xml; charset="utf-8"'}))

        async def notfound(req: web.Request) -> web.Response:
            return web.Response(status=404, headers={"CONNECTION": "close"})

        app.router.add_get("/Lumix/Server0/ddd", ddd)
        for svc in ("CMS", "CDS"):
            app.router.add_route("SUBSCRIBE", "/Server0/%s_event" % svc, subscribe)
            app.router.add_route("UNSUBSCRIBE", "/Server0/%s_event" % svc, unsubscribe)
        app.router.add_post("/Server0/CDS_control", control)
        app.router.add_route("*", "/{tail:.*}", notfound)
        return app

    # -------------------------------------------------------------------- files
    def _dl_app(self) -> web.Application:
        app = _camera_app()

        async def get_file(req: web.Request) -> web.StreamResponse:
            cam = self.cam
            assert cam is not None
            name = req.match_info["name"]
            hit = cam.card.lookup(name)
            if hit is None or not cam.files_allowed():
                cam.record("dl", "%s %s" % (req.method, name),
                           "404" if hit is None else "404 (not in play)", req.remote or "")
                return web.Response(status=404, headers={"Connection": "close"})
            entry, variant = hit
            conv = req.headers.get("X-CONVERT", "")
            if variant == "org" and entry.kind in ("photo", "raw") and conv:
                variant = "thumb" if conv == "SmallSize" else "preview"
            size = cam.card.file_size(entry, variant)
            ctype = cam.card.content_type(entry, variant)
            start, end, status = 0, size - 1, 200
            headers = {"Content-Type": ctype, "Connection": "close", "Date": cam.http_date()}
            rng = req.headers.get("Range")
            if cam.cfg.dl_range:
                headers["Accept-Ranges"] = "bytes"
                if rng:
                    r = _parse_range(rng, size)
                    if r is None:
                        cam.record("dl", "%s %s %s" % (req.method, name, rng), "416",
                                   req.remote or "")
                        return web.Response(status=416, headers={
                            "Content-Range": "bytes */%d" % size, "Connection": "close"})
                    start, end, status = r[0], r[1], 206
                    headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
            length = end - start + 1
            headers["Content-Length"] = str(length)
            cam.record("dl", "%s %s%s" % (req.method, name, " " + rng if rng else ""),
                       str(status), req.remote or "")
            resp = web.StreamResponse(status=status, headers=headers)
            await resp.prepare(req)
            if req.method == "HEAD":
                return resp
            pos = start
            rate = cam.cfg.dl_rate
            try:
                while pos <= end:
                    n = min(65536 if not rate else max(1024, min(65536, int(rate / 10))), end - pos + 1)
                    await resp.write(cam.card.read(entry, variant, pos, n))
                    pos += n
                    if rate:
                        await asyncio.sleep(n / rate)
                await resp.write_eof()
            except (ConnectionError, aiohttp.ClientConnectionResetError, aiohttp.ClientOSError):
                pass        # the client stopped reading (paused / cancelled import)
            return resp

        app.router.add_route("GET", "/{name}", get_file)
        app.router.add_route("HEAD", "/{name}", get_file)
        return app

    # ------------------------------------------------------------------ control
    def _control_app(self) -> web.Application:
        app = web.Application()

        def ok(data: Any) -> web.Response:
            return web.json_response(data, dumps=lambda o: json.dumps(o, indent=1, default=str))

        async def state(req: web.Request) -> web.Response:
            assert self.cam is not None
            return ok(self.cam.snapshot())

        async def get_log(req: web.Request) -> web.Response:
            assert self.cam is not None
            n = int(req.query.get("n", "200"))
            kind = req.query.get("kind")
            rows = [r for r in self.cam.log if not kind or r["kind"] == kind]
            return ok(rows[-n:])

        async def reset(req: web.Request) -> web.Response:
            assert self.cam is not None
            self.cam.streamer.stop()
            self.cam.power_on()       # fresh boot, ports (re)opened
            return ok(self.cam.snapshot())

        async def power_cycle(req: web.Request) -> web.Response:
            assert self.cam is not None
            off_s = float(req.query.get("off_s", "1.0"))
            self.cam.power_off("control API power-cycle")
            await asyncio.sleep(0)
            await self._set_power(False)
            await asyncio.sleep(off_s)
            self.cam.power_on()
            await self._set_power(True)
            return ok(self.cam.snapshot())

        async def power_off(req: web.Request) -> web.Response:
            assert self.cam is not None
            self.cam.power_off("control API")
            await self._set_power(False)
            return ok(self.cam.snapshot())

        async def power_on(req: web.Request) -> web.Response:
            assert self.cam is not None
            if not self.cam.powered:
                self.cam.power_on()
            await self._set_power(True)
            return ok(self.cam.snapshot())

        async def wedge(req: web.Request) -> web.Response:
            assert self.cam is not None
            self.cam.wedge(req.query.get("reason", "forced via control API"))
            return ok(self.cam.snapshot())

        async def unwedge(req: web.Request) -> web.Response:
            assert self.cam is not None
            self.cam.wedged, self.cam.wedge_reason = False, ""
            self.cam.hard_wedged = self.cam.faulted = False
            return ok(self.cam.snapshot())

        async def drop(req: web.Request) -> web.Response:
            assert self.cam is not None
            self.cam.drop_session(req.query.get("ip"), "control API")
            return ok(self.cam.snapshot())

        async def approve(req: web.Request) -> web.Response:
            assert self.cam is not None
            self.cam.approve()
            return ok(self.cam.snapshot())

        async def body(req: web.Request) -> web.Response:
            assert self.cam is not None
            try:
                return ok(self.cam.body_action(req.match_info["action"]))
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=400)

        async def config(req: web.Request) -> web.Response:
            assert self.cam is not None
            try:
                data = await req.json() if req.can_read_body else {}
            except ValueError:
                return web.json_response({"error": "body must be JSON"}, status=400)
            data.update({k: v for k, v in req.query.items()})
            fields = {f.name: str(f.type) for f in dataclasses.fields(EmuConfig)}
            bad: List[str] = []
            for k, v in data.items():
                if k not in fields:
                    bad.append(k)
                    continue
                t = fields[k]
                try:
                    if "bool" in t:
                        v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes")
                    elif "float" in t:
                        v = float(v)
                    elif "int" in t:
                        v = None if v in (None, "", "none") else int(v)
                    else:
                        v = str(v)
                except ValueError:
                    return web.json_response({"error": "bad value for %s" % k}, status=400)
                setattr(self.cam.cfg, k, v)
            if bad:
                return web.json_response({"error": "unknown config keys %s" % bad}, status=400)
            return ok(dataclasses.asdict(self.cam.cfg))

        app.router.add_get("/emu/state", state)
        app.router.add_get("/emu/log", get_log)
        app.router.add_post("/emu/reset", reset)
        app.router.add_post("/emu/power-cycle", power_cycle)
        app.router.add_post("/emu/power-off", power_off)
        app.router.add_post("/emu/power-on", power_on)
        app.router.add_post("/emu/wedge", wedge)
        app.router.add_post("/emu/unwedge", unwedge)
        app.router.add_post("/emu/drop-session", drop)
        app.router.add_post("/emu/approve", approve)
        app.router.add_post("/emu/body/{action}", body)
        app.router.add_post("/emu/config", config)
        return app
