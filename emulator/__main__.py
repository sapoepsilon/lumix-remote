"""python -m emulator [--host 127.0.0.1] [--http-port 8080] [--upnp-port 60606] [--dl-port 50001]
                    [--control-port 8081] [--model dc-s5] [--wedge-realism on|off] [--count N]"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import signal
from typing import List

from . import MODELS
from .camera import EmuConfig, Identity
from .servers import EmulatorNode


def _range(s: str) -> List[float]:
    a, _, b = s.partition("-")
    lo = float(a)
    return [lo, float(b) if b else lo]


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m emulator",
                                description="Lumix (DC-S5) Wi-Fi camera emulator")
    p.add_argument("--host", default="127.0.0.1", help="bind address (the 'camera IP')")
    p.add_argument("--http-port", type=int, default=8080, help="cam.cgi port (real camera: 80)")
    p.add_argument("--upnp-port", type=int, default=60606, help="ddd/CDS/GENA port")
    p.add_argument("--dl-port", type=int, default=50001, help="file download port")
    p.add_argument("--control-port", type=int, default=None,
                   help="control API port (default: http-port + 1)")
    p.add_argument("--model", default="dc-s5", choices=sorted(MODELS))
    p.add_argument("--wedge-realism", default="on", choices=["on", "off"],
                   help="reproduce the S5's err_critical/err_busy wedge failure modes")
    p.add_argument("--count", type=int, default=1, help="run N cameras in this process")
    p.add_argument("--port-stride", type=int, default=10,
                   help="with --count: camera i uses every port + i*stride")
    p.add_argument("--host-step", action="store_true",
                   help="with --count: camera i uses host + i (127.0.0.1, .2, ...) and the same "
                        "ports (macOS needs 'sudo ifconfig lo0 alias 127.0.0.2 up' first)")
    p.add_argument("--settle", default="5-22", type=_range,
                   help="post-SUBSCRIBE mod_Play delay window in seconds, e.g. 5-22 or 1")
    p.add_argument("--approval", default="auto", choices=["auto", "prompt", "refuse"])
    p.add_argument("--session-timeout", type=float, default=30.0,
                   help="idle seconds before the camera drops the session (0 = never)")
    p.add_argument("--fps", type=float, default=25.0)
    p.add_argument("--interval-scale", type=float, default=1.0,
                   help="time-lapse interval multiplier (0.01 = 100x faster)")
    p.add_argument("--card-photos", type=int, default=4)
    p.add_argument("--card-group", type=int, default=120, help="photos in the time-lapse group")
    p.add_argument("--no-range", action="store_true", help=":50001 ignores Range (200 + full body)")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    def cfg() -> EmuConfig:
        return EmuConfig(wedge_realism=a.wedge_realism == "on", settle_min=a.settle[0],
                         settle_max=a.settle[1], approval=a.approval,
                         session_timeout_s=a.session_timeout, fps=a.fps,
                         interval_scale=a.interval_scale, dl_range=not a.no_range, seed=a.seed)

    async def run() -> None:
        nodes = []
        for i in range(a.count):
            host, off = a.host, i * a.port_stride
            if a.host_step:
                host, off = str(ipaddress.ip_address(a.host) + i), 0
            ctrl = (a.control_port if a.control_port is not None else a.http_port + 1) + off
            node = EmulatorNode(host, a.http_port + off, a.upnp_port + off, a.dl_port + off,
                                ctrl, Identity.for_index(i, a.model), cfg(),
                                a.card_photos, a.card_group)
            await node.start()
            nodes.append(node)
            d = node.describe()
            print("camera %d %s: http://%s:%d/cam.cgi  upnp :%d  files :%d  control "
                  "http://%s:%d/emu/state" % (i, d["name"], host, d["http"], d["upnp"], d["dl"],
                                              host, d["control"]), flush=True)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        for n in nodes:
            await n.stop()

    asyncio.run(run())


if __name__ == "__main__":
    main()
