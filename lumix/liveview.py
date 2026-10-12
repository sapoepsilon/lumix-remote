"""Live view: UDP JPEG receiver and a frame hub that any number of viewers share.

After ``camcmd recmode`` and ``startstream&value=<port>`` the camera sends one
UDP datagram per frame to the requesting host. Each datagram is a binary header
followed by a JPEG. The JPEG starts at ``be16(data[30:32]) + 32`` (on the S5:
0x00de + 32 = 254); we fall back to searching for FF D8 if that offset is off.

``FrameSource`` is the interface viewers use. The Wi-Fi live view implements
it; a future HDMI capture-card source can implement the same interface and the
MJPEG/WebSocket endpoints work unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections import deque
from typing import Deque, Optional, Tuple

log = logging.getLogger("lumix.liveview")


def extract_jpeg(d: bytes) -> Optional[bytes]:
    if len(d) < 64:
        return None
    start = struct.unpack(">H", d[30:32])[0] + 32
    if not (start < len(d) - 2 and d[start:start + 2] == b"\xff\xd8"):
        start = d.find(b"\xff\xd8\xff")
        if start < 0:
            return None
    end = d.rfind(b"\xff\xd9")
    if end <= start:
        return None
    return d[start:end + 2]


class FrameSource:
    """Latest-frame broadcaster. Viewers await ``next_frame(after_seq)``."""

    name = "source"

    def __init__(self) -> None:
        self.frame: Optional[bytes] = None
        self.seq = 0
        self.ts = 0.0
        self._cond = asyncio.Condition()
        self._times: Deque[float] = deque(maxlen=60)
        self.viewers = 0

    async def publish(self, jpeg: bytes) -> None:
        now = time.time()
        async with self._cond:
            self.frame = jpeg
            self.seq += 1
            self.ts = now
            self._times.append(now)
            self._cond.notify_all()

    async def next_frame(self, after_seq: int, timeout: float = 5.0) -> Optional[Tuple[int, bytes]]:
        async with self._cond:
            try:
                await asyncio.wait_for(self._cond.wait_for(lambda: self.seq > after_seq), timeout)
            except asyncio.TimeoutError:
                return None
            assert self.frame is not None
            return self.seq, self.frame

    @property
    def fps(self) -> float:
        t = [x for x in self._times if x > time.time() - 3]
        if len(t) < 2:
            return 0.0
        return round((len(t) - 1) / (t[-1] - t[0]), 1)

    @property
    def age(self) -> Optional[float]:
        return (time.time() - self.ts) if self.ts else None

    def stats(self) -> dict:
        return {"source": self.name, "seq": self.seq, "fps": self.fps,
                "age": round(self.age, 2) if self.age is not None else None,
                "bytes": len(self.frame) if self.frame else 0, "viewers": self.viewers}


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, rx: "LiveViewReceiver"):
        self.rx = rx

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        self.rx._on_datagram(data, addr)


class LiveViewReceiver(FrameSource):
    name = "wifi"

    def __init__(self, port: int = 49199) -> None:
        super().__init__()
        self.port = port
        self.camera_ip: Optional[str] = None
        self._transport: Optional[asyncio.DatagramTransport] = None
        self.datagrams = 0
        self.bad = 0
        self.last_header: bytes = b""

    async def open(self) -> None:
        if self._transport is not None:
            return
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _Proto(self), local_addr=("0.0.0.0", self.port))
        sock = self._transport.get_extra_info("socket")
        try:
            import socket
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass
        log.info("live view UDP listening on :%d", self.port)

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def _on_datagram(self, data: bytes, addr) -> None:
        if self.camera_ip and addr[0] != self.camera_ip:
            return
        self.datagrams += 1
        jpeg = extract_jpeg(data)
        if jpeg is None:
            self.bad += 1
            return
        self.last_header = data[:len(data) - len(jpeg)] if data.endswith(jpeg) else data[:64]
        asyncio.ensure_future(self.publish(jpeg))
