"""UPnP GENA event subscription (camera -> us push events).

We run a tiny HTTP server reachable by the camera (default port 49153) and send
``SUBSCRIBE /Server0/CMS_event`` to the camera's :60606 with our callback URL.
The camera then POSTs ``NOTIFY`` requests whose body is a UPnP propertyset;
Panasonic-specific events arrive as ``<X_Panasonic_Cam_Sync>value</...>``
(seen on the S5: ``lens_Atta`` after recmode, ``mod_Play`` on a mode change).

On the S5 the subscription is also part of what takes the camera off its
"Connecting" screen; without it ``startstream`` answers ok but sends nothing.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
import xml.etree.ElementTree as ET
from typing import Awaitable, Callable, Dict, List, Optional

from aiohttp import web

log = logging.getLogger("lumix.events")

UA = "Panasonic Android/1 DM-CP"
EventHandler = Callable[[Dict[str, object]], Awaitable[None]]


def local_ip_for(remote_ip: str) -> str:
    """Our address on the interface that routes to remote_ip (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((remote_ip, 9))
        return s.getsockname()[0]
    finally:
        s.close()


def parse_propertyset(body: bytes) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return out
    for prop in root:
        for var in prop:
            name = var.tag.split("}", 1)[-1]
            out.append({"name": name, "value": (var.text or "").strip()})
    return out


async def raw_request(ip: str, port: int, method: str, path: str,
                      headers: List[tuple], timeout: float = 6.0) -> Dict[str, object]:
    """Minimal HTTP/1.1 request for non-standard methods (SUBSCRIBE/UNSUBSCRIBE)."""
    req = "%s %s HTTP/1.1\r\nHOST: %s:%d\r\n%s\r\n" % (
        method, path, ip, port, "".join("%s: %s\r\n" % kv for kv in headers))
    reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    try:
        writer.write(req.encode())
        await writer.drain()
        data = b""
        try:
            while True:
                chunk = await asyncio.wait_for(reader.read(65535), timeout)
                if not chunk:
                    break
                data += chunk
                if b"\r\n\r\n" in data and b"CONTENT-LENGTH: 0" in data.upper():
                    break
        except asyncio.TimeoutError:
            pass
    finally:
        writer.close()
    text = data.decode("utf-8", "replace")
    m = re.match(r"HTTP/1\.\d (\d+)", text)
    hdrs: Dict[str, str] = {}
    for line in text.split("\r\n")[1:]:
        if not line:
            break
        k, _, v = line.partition(":")
        hdrs[k.strip().upper()] = v.strip()
    return {"status": int(m.group(1)) if m else 0, "headers": hdrs, "raw": text}


class EventListener:
    """HTTP server for NOTIFY callbacks + subscription bookkeeping."""

    def __init__(self, port: int = 49153, on_event: Optional[EventHandler] = None):
        self.port = port
        self.on_event = on_event
        self._runner: Optional[web.AppRunner] = None
        self.sid: Optional[str] = None
        self.sub_ip: Optional[str] = None
        self.sub_time = 0.0
        self.timeout_s = 300
        self.last_event = 0.0
        self.count = 0

    async def start(self) -> None:
        if self._runner is not None:
            return
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port, reuse_address=True)
        await site.start()
        log.info("event callback server on :%d", self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _handle(self, req: web.Request) -> web.Response:
        body = await req.read()
        self.last_event = time.time()
        self.count += 1
        props = parse_propertyset(body)
        ev = {"method": req.method, "sid": req.headers.get("SID"), "seq": req.headers.get("SEQ"),
              "from": req.remote, "props": props}
        log.debug("NOTIFY %s", ev)
        if self.on_event is not None:
            try:
                await self.on_event(ev)
            except Exception:
                log.exception("event handler failed")
        return web.Response(status=200)

    async def subscribe(self, camera_ip: str) -> Dict[str, object]:
        me = local_ip_for(camera_ip)
        r = await raw_request(camera_ip, 60606, "SUBSCRIBE", "/Server0/CMS_event", [
            ("User-Agent", UA),
            ("CALLBACK", "<http://%s:%d/Camera/event>" % (me, self.port)),
            ("NT", "upnp:event"),
            ("TIMEOUT", "Second-%d" % self.timeout_s),
        ])
        hdrs = r["headers"]  # type: ignore[assignment]
        if r["status"] == 200:
            self.sid = hdrs.get("SID")  # type: ignore[union-attr]
            self.sub_ip = camera_ip
            self.sub_time = time.time()
            m = re.search(r"Second-(\d+)", hdrs.get("TIMEOUT", ""))  # type: ignore[union-attr]
            if m:
                self.timeout_s = int(m.group(1))
        return {"status": r["status"], "sid": self.sid, "callback_ip": me}

    async def renew(self) -> Dict[str, object]:
        """Renew with the SID; fall back to a fresh subscription."""
        if self.sid and self.sub_ip:
            r = await raw_request(self.sub_ip, 60606, "SUBSCRIBE", "/Server0/CMS_event", [
                ("User-Agent", UA), ("SID", self.sid), ("TIMEOUT", "Second-%d" % self.timeout_s)])
            if r["status"] == 200:
                self.sub_time = time.time()
                return {"status": 200, "sid": self.sid, "renewed": True}
        assert self.sub_ip
        return await self.subscribe(self.sub_ip)

    def needs_renew(self) -> bool:
        return bool(self.sid) and time.time() - self.sub_time > self.timeout_s * 0.6

    async def unsubscribe(self) -> None:
        if self.sid and self.sub_ip:
            try:
                await raw_request(self.sub_ip, 60606, "UNSUBSCRIBE", "/Server0/CMS_event",
                                  [("User-Agent", UA), ("SID", self.sid)], timeout=3)
            except (OSError, asyncio.TimeoutError):
                pass
        self.sid = None
