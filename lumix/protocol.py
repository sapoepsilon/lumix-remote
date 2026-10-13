"""Low-level cam.cgi transport for Panasonic Lumix cameras.

Every control request is an HTTP GET to ``http://<ip>/cam.cgi?mode=..&type=..&value=..&value2=..``.
Replies are either XML (``<camrply><result>ok</result>...``) or CSV text
(``ok,a,b,c`` for accctrl and getinfo&type=lens).

All requests go through one asyncio lock: the camera's web server is small and
we never want two commands in flight at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

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


# Result of a request the camera guard refused to send (never sent on the wire).
GUARD_RESULT = "err_local_mid_operation"
# Result of a mode switch the transport's rate limiter refused (never sent).
RATE_LIMITED = "err_local_rate_limited"
MODE_SWITCHES = ("recmode", "playmode", "pictmode")


class ModeSwitchLimiter:
    """Hard cap on ``camcmd recmode/playmode/pictmode`` for one camera, whoever
    sends them (connect, live view, files, time-lapse, UI, retries).

    Every confirmed S5 wedge had mode switches close together or into a switch
    the camera was making on its own: recmode churn (2026-10-11 22:01, the 4th
    recmode in 20 s), recmode during the post-SUBSCRIBE switch (21:24, 22:45),
    and 2026-10-12 16:46-16:52 (logs/camera-2026-10-12.log lines 9462-9558):
    four recmodes in 4 min answering the camera's own drops to playback, then
    err_busy/err_critical for good. ``min_gap_s`` between two switches and at
    most ``max_n`` per ``window_s``; the counts are of switches sent."""

    def __init__(self, min_gap_s: float = 10.0, max_n: int = 3, window_s: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self.min_gap_s, self.max_n, self.window_s = min_gap_s, max_n, window_s
        self.clock = clock
        self.sent: List[float] = []

    def wait_s(self) -> float:
        """Seconds until a switch would be allowed (0 = now)."""
        now = self.clock()
        self.sent = [t for t in self.sent if now - t < max(self.window_s, self.min_gap_s)]
        wait = 0.0
        if self.sent:
            wait = max(wait, self.sent[-1] + self.min_gap_s - now)
        recent = [t for t in self.sent if now - t < self.window_s]
        if len(recent) >= self.max_n:
            wait = max(wait, recent[-self.max_n] + self.window_s - now)
        return max(0.0, wait)

    def check(self) -> Optional[str]:
        """None = allowed now, else why not."""
        wait = self.wait_s()
        if wait <= 0:
            return None
        return ("mode-switch rate limit (max 1 per %.0f s, %d per %.0f s): next allowed in %.1f s"
                % (self.min_gap_s, self.max_n, self.window_s, wait))

    def note(self) -> None:
        self.sent.append(self.clock())


class CamTransport:
    """Serialised HTTP access to one camera."""

    def __init__(self, ip: str, user_agent: str = "LumixRemote/0.1",
                 trace: Optional[Callable[[str, Reply], None]] = None, port: int = 80,
                 guard: Optional[Callable[[str, Optional[str], Optional[str]],
                                          Awaitable[Optional[str]]]] = None,
                 trace_error: Optional[Callable[[str, str], None]] = None,
                 limiter: Optional[ModeSwitchLimiter] = None,
                 refused: Optional[Callable[[str, str], None]] = None):
        self.ip = ip
        self.port = port            # cam.cgi port (80 on real bodies; emulators may differ)
        self.user_agent = user_agent
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        self.trace = trace
        # Called with (query, error) when a request got no HTTP reply at all
        # (timeout, refused): without it a camera that went to sleep leaves no
        # line in the camera log.
        self.trace_error = trace_error
        # Pre-send veto: guard(mode, type, value) -> None (send) or a reason
        # (not sent; a local GUARD_RESULT reply comes back instead). The camera
        # uses it to keep mode switches away from a body that is mid-operation.
        self.guard = guard
        # Mode-switch rate limit, enforced here for every caller; refused
        # requests answer RATE_LIMITED locally. ``refused(query, reason)`` is
        # told about every request that was not sent (guard or limiter).
        self.limiter = limiter if limiter is not None else ModeSwitchLimiter()
        self.refused = refused
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
        if self.guard is not None:
            why = await self.guard(mode, type, value)
            if why:
                return self._local(q, why, GUARD_RESULT)
        switch = mode == "camcmd" and value in MODE_SWITCHES
        async with self._lock:
            if switch:
                why = self.limiter.check()
                if why:
                    return self._local(q, why, RATE_LIMITED)
                self.limiter.note()
            t0 = time.monotonic()
            sess = await self._sess()
            host = self.ip if self.port == 80 else "%s:%d" % (self.ip, self.port)
            url = URL("http://%s/cam.cgi?%s" % (host, q), encoded=True)
            try:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                    body = await r.read()
                    status = r.status
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                err = "%s: %s" % (type_name(e), e)
                if self.trace_error:
                    with contextlib.suppress(Exception):
                        self.trace_error(q, err)
                raise CamError(err) from e
            rep = parse_reply(q, status, body, time.monotonic() - t0)
        self.last_ok = time.time()
        if self.trace:
            try:
                self.trace(q, rep)
            except Exception:  # pragma: no cover - tracing must never break control
                log.exception("trace hook failed")
        return rep

    def _local(self, q: str, why: str, result: str) -> Reply:
        log.warning("not sent (%s): %s", why, q)
        if self.refused:
            with contextlib.suppress(Exception):
                self.refused(q, why)
        return parse_reply(q, 200, b"<camrply><result>%s</result></camrply>" % result.encode(), 0.0)


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
                   "UDN", "X_FirmVersion", "X_CamCategory", "X_AdditionalFunction",
                   "X_CamCapability"):
            out[tag] = (el.text or "").strip()
    return out


ENC_ZERO_NONCE = -1875740628 & 0xFFFFFFFF   # LUMIX Sync's stand-in for a zero nonce


def parse_nonce(rep_text: str) -> int:
    """req_acc_g reply ``ok,<8 hex>`` -> 32-bit nonce (ValueError otherwise)."""
    parts = rep_text.strip().split(",")
    if len(parts) < 2 or parts[0] != "ok":
        raise ValueError("no nonce in %r" % rep_text[:40])
    return int(parts[1], 16) & 0xFFFFFFFF


def encrypt_acc(text: str, nonce: int) -> str:
    """LUMIX Sync's req_acc_e field transform (docs/lumix-sync-wifi-flow.md section 3):
    pad with 0xFF to a multiple of 4, each 4-byte block as big-endian uint32
    XOR nonce, byte-swapped, printed as %08x. Untested on a real encrypted body."""
    n = (nonce & 0xFFFFFFFF) or ENC_ZERO_NONCE
    raw = bytearray(text.encode("utf-8"))
    while len(raw) % 4:
        raw.append(0xFF)
    out = []
    for i in range(0, len(raw), 4):
        v = int.from_bytes(bytes(raw[i:i + 4]), "big") ^ n
        out.append("%08x" % int.from_bytes(v.to_bytes(4, "big"), "little"))
    return "".join(out)
