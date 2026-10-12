"""UDP live-view sender: recorded S5 header + generated JPEG, ~25 fps.

Datagram layout (from probe-results/20261011-203625/liveview_headers.txt):
254-byte header, then the JPEG. Header bytes 0-1 = datagram length (BE16;
real S5 2026-10-12: 0x695d with a 26973-byte datagram),
9-14 = camera MAC, 15-16 = 0x0021 (packet type 33 = image), 30-31 = BE16 0x00de
(JPEG offset - 32). Frames are sent from an ephemeral source port to the IP that
sent ``startstream`` and the port it named, one frame per datagram.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
import zlib
from typing import Optional

from . import MODELS
from .jpeg import FrameCache

log = logging.getLogger("emulator.liveview")


class Streamer:
    def __init__(self, cam) -> None:
        self.cam = cam
        hdr = bytearray(bytes.fromhex(MODELS[cam.ident.model]["lv_header"]))
        hdr[9:15] = bytes.fromhex(cam.ident.mac_hex)
        self.header = bytes(hdr)
        hue = (zlib.crc32(cam.ident.serial.encode()) % 1000) / 1000.0
        # liveviewsize vga = 640x360 (80x45 blocks), qvga = 320x184 (40x23), as the real S5
        self.caches = {"vga": FrameCache(hue), "qvga": FrameCache(hue, w=40, h=23)}
        self.seq = 0
        self.errors = 0
        self._task: Optional[asyncio.Task] = None
        self._sock: Optional[socket.socket] = None

    def ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def datagram(self) -> bytes:
        self.seq += 1
        stamp = "%s %d" % (time.strftime("%H:%M:%S"), self.seq % 10000)
        if getattr(self.cam, "stream_size", "vga") == "qvga":
            cache, pad = self.caches["qvga"], 12800 + (self.seq * 7919) % 900    # real: 12.8-13.6 KB
        else:
            cache, pad = self.caches["vga"], 22700 + (self.seq * 7919) % 8300    # real: 22.7-31.0 KB
        jpeg = cache.frame(self.seq, stamp, pad_to=pad - len(self.header))
        total = len(self.header) + len(jpeg)
        return struct.pack(">H", total & 0xFFFF) + self.header[2:] + jpeg

    async def _run(self) -> None:
        cam = self.cam
        period = 1.0 / max(1.0, cam.cfg.fps)
        nxt = time.monotonic()
        idle = 0
        try:
            while True:
                if cam.stream_should_send():
                    idle = 0
                    if self._sock is None:
                        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        try:
                            self._sock.bind((cam.host, 0))
                        except OSError:
                            self._sock.bind(("", 0))
                        # macOS caps datagrams at SO_SNDBUF (9216 by default); frames are ~30 KB
                        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 17)
                        self._sock.setblocking(False)
                    try:
                        self._sock.sendto(self.datagram(), cam.stream_target)
                        cam.frames_sent += 1
                    except OSError as e:          # receiver not listening yet etc.
                        self.errors += 1
                        if self.errors in (1, 100, 1000):
                            log.warning("live view sendto %s: %s", cam.stream_target, e)
                else:
                    idle += 1
                    if idle > 10 / period and not cam.stream_open:
                        break                      # stream closed for 10 s: park the task
                nxt += period
                await asyncio.sleep(max(0.0, nxt - time.monotonic()))
                if time.monotonic() - nxt > 1:
                    nxt = time.monotonic()
        finally:
            if self._sock is not None:
                self._sock.close()
                self._sock = None
