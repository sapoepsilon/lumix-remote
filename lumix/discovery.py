"""Find a Lumix camera on the network.

Order: explicit IP > cached IP > AP-mode address (192.168.54.1) > SSDP NOTIFY
heard on the multicast group > TCP scan of the local /24 on port 60606.

The S5 ignores SSDP M-SEARCH; it only multicasts ``ssdp:alive`` NOTIFYs when
it joins the network (LOCATION http://<ip>:60606/Lumix/Server0/ddd), so the
passive listener mostly helps when the camera is switched on while we run.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import struct
from typing import Callable, Iterable, List, Optional, Set

import aiohttp

from .protocol import parse_ddd

log = logging.getLogger("lumix.discovery")

DDD_PATH = "/Lumix/Server0/ddd"
AP_MODE_IP = "192.168.54.1"
SSDP_GROUP = ("239.255.255.250", 1900)


async def probe_ddd(ip: str, timeout: float = 2.0) -> Optional[dict]:
    """Return the parsed device description if ip is a Lumix camera."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("http://%s:60606%s" % (ip, DDD_PATH),
                             timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status != 200:
                    return None
                raw = await r.read()
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return None
    try:
        d = parse_ddd(raw)
    except Exception:
        return None
    if d.get("modelName") == "LUMIX" or d.get("manufacturer") == "Panasonic":
        d["ip"] = ip
        return d
    return None


def local_networks() -> List[ipaddress.IPv4Network]:
    """/24 networks of our IPv4 interfaces (via the default-route trick + hostname)."""
    nets: Set[ipaddress.IPv4Network] = set()
    cands = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 9))
        cands.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            cands.add(info[4][0])
    except OSError:
        pass
    for ip in cands:
        a = ipaddress.ip_address(ip)
        if a.is_private and not a.is_loopback:
            nets.add(ipaddress.ip_network(ip + "/24", strict=False))
    return sorted(nets)


async def _port_open(ip: str, port: int, timeout: float) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
        w.close()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def scan(exclude: Iterable[str] = (), timeout: float = 0.6,
               progress: Optional[Callable[[str], None]] = None) -> Optional[dict]:
    excl = set(exclude)
    for net in local_networks():
        if progress:
            progress("scanning %s :60606" % net)
        hosts = [str(h) for h in net.hosts() if str(h) not in excl]
        sem = asyncio.Semaphore(64)

        async def check(ip: str) -> Optional[str]:
            async with sem:
                return ip if await _port_open(ip, 60606, timeout) else None

        for coro in asyncio.as_completed([check(h) for h in hosts]):
            ip = await coro
            if ip:
                d = await probe_ddd(ip)
                if d:
                    return d
    return None


class SsdpListener(asyncio.DatagramProtocol):
    """Passive SSDP listener; calls on_found(ip) for Lumix NOTIFY/responses."""

    def __init__(self, on_found: Callable[[str], None]):
        self.on_found = on_found

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        if DDD_PATH.encode() in data:
            m = re.search(rb"LOCATION:\s*http://([0-9.]+):", data, re.I)
            if m:
                self.on_found(m.group(1).decode())


async def start_ssdp_listener(on_found: Callable[[str], None]) -> Optional[asyncio.DatagramTransport]:
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", SSDP_GROUP[1]))
        mreq = struct.pack("4sl", socket.inet_aton(SSDP_GROUP[0]), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError as e:
        log.warning("SSDP listener unavailable: %s", e)
        sock.close()
        return None
    tr, _ = await loop.create_datagram_endpoint(lambda: SsdpListener(on_found), sock=sock)
    return tr  # type: ignore[return-value]
