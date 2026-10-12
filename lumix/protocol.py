"""Low-level cam.cgi transport for Panasonic Lumix cameras.

Every control request is an HTTP GET to ``http://<ip>/cam.cgi?mode=..&type=..&value=..&value2=..``.
Replies are either XML (``<camrply><result>ok</result>...``) or CSV text
(``ok,a,b,c`` for accctrl and getinfo&type=lens).

All requests go through one asyncio lock: the camera's web server is small and
we never want two commands in flight at once.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import aiohttp
from yarl import URL

log = logging.getLogger("lumix.protocol")

# Characters the camera expects literally in values ("1/3", "4:3", "2304/256").
_SAFE = "/:._-"


class CamError(Exception):
    """Transport failure (timeout, refused, bad HTTP status)."""


@dataclass
class Reply:
    query: str
    status: int
    text: str
    elapsed: float
    xml: Optional[ET.Element] = None
    result: str = ""
    csv: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.result == "ok"

    def find(self, path: str) -> Optional[str]:
        if self.xml is None:
            return None
        el = self.xml.find(path)
        return el.text if el is not None else None

    def setting_value(self, name: str) -> Optional[str]:
        """Value from ``<settingvalue name="..."/>`` (getsetting replies)."""
        if self.xml is None:
            return None
        el = self.xml.find("settingvalue")
        if el is None:
            return None
        return el.get(name)

    def to_dict(self) -> Dict[str, Any]:
        return {"query": self.query, "result": self.result, "ms": round(self.elapsed * 1000)}


def parse_reply(query: str, status: int, raw: bytes, elapsed: float) -> Reply:
    text = raw.decode("utf-8", "replace").strip()
    rep = Reply(query=query, status=status, text=text, elapsed=elapsed)
    if text.startswith("<"):
        try:
            rep.xml = ET.fromstring(raw)
            r = rep.xml.find("result")
            rep.result = (r.text or "").strip() if r is not None else ""
        except ET.ParseError:
            rep.result = "err_parse"
    else:
        rep.csv = text.split(",")
        rep.result = rep.csv[0] if rep.csv else ""
    return rep


def build_query(mode: str, type: Optional[str] = None, value: Optional[str] = None,
                value2: Optional[str] = None) -> str:
    parts = [("mode", mode)]
    if type is not None:
        parts.append(("type", type))
    if value is not None:
        parts.append(("value", value))
    if value2 is not None:
        parts.append(("value2", value2))
    return "&".join("%s=%s" % (k, urllib.parse.quote(str(v), safe=_SAFE)) for k, v in parts)


class CamTransport:
    """Serialised HTTP access to one camera."""

    def __init__(self, ip: str, user_agent: str = "LumixRemote/0.1",
                 trace: Optional[Callable[[str, Reply], None]] = None):
        self.ip = ip
        self.user_agent = user_agent
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        self.trace = trace
        self.last_ok = 0.0

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # force_close: the camera answers "Connection: close" anyway and keeping
            # sockets around just produces stale-connection errors.
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True, limit=4),
                headers={"User-Agent": self.user_agent},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def get_raw(self, port: int, path: str, timeout: float = 6.0) -> bytes:
        sess = await self._sess()
        url = URL("http://%s:%d%s" % (self.ip, port, path), encoded=True)
        try:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                body = await r.read()
                if r.status != 200:
                    raise CamError("HTTP %d for %s" % (r.status, path))
                return body
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise CamError("%s: %s" % (type(e).__name__, e)) from e

    async def cgi(self, mode: str, type: Optional[str] = None, value: Optional[str] = None,
                  value2: Optional[str] = None, timeout: float = 6.0) -> Reply:
        q = build_query(mode, type, value, value2)
        async with self._lock:
            t0 = time.monotonic()
            sess = await self._sess()
            url = URL("http://%s/cam.cgi?%s" % (self.ip, q), encoded=True)
            try:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                    body = await r.read()
                    status = r.status
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                raise CamError("%s: %s" % (type_name(e), e)) from e
            rep = parse_reply(q, status, body, time.monotonic() - t0)
        self.last_ok = time.time()
        if self.trace:
            try:
                self.trace(q, rep)
            except Exception:  # pragma: no cover - tracing must never break control
                log.exception("trace hook failed")
        return rep


def type_name(e: BaseException) -> str:
    return type(e).__name__


def parse_state(rep: Reply) -> Dict[str, str]:
    """getstate -> flat dict of the <state> children."""
    out: Dict[str, str] = {}
    if rep.xml is None:
        return out
    st = rep.xml.find("state")
    if st is None:
        return out
    for child in st:
        out[child.tag] = (child.text or "").strip()
    return out


def parse_ddd(raw: bytes) -> Dict[str, str]:
    """UPnP device description -> friendlyName, modelNumber, UDN, firmware, ..."""
    root = ET.fromstring(raw)
    out: Dict[str, str] = {}
    for el in root.iter():
        tag = el.tag.split("}", 1)[-1]
        if tag in ("friendlyName", "manufacturer", "modelName", "modelNumber", "serialNumber",
                   "UDN", "X_FirmVersion", "X_CamCategory", "X_AdditionalFunction"):
            out[tag] = (el.text or "").strip()
    return out
