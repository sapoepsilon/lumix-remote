"""Entry point: python -m server [--lan] [--camera-ip IP] [--port 8770]."""

from __future__ import annotations

import argparse
import logging
import os

from aiohttp import web

from lumix.camera import Camera, CameraConfig
from server.app import build_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    p = argparse.ArgumentParser(description="Lumix Remote web server")
    p.add_argument("--lan", action="store_true", help="listen on 0.0.0.0 instead of 127.0.0.1")
    p.add_argument("--host", default=None, help="explicit bind address (overrides --lan)")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--camera-ip", default=os.environ.get("LUMIX_CAMERA_IP"),
                   help="camera IP (default: discover)")
    p.add_argument("--device-name", default="LumixRemote", help="name shown on the camera")
    p.add_argument("--lv-port", type=int, default=49199, help="UDP port for live view")
    p.add_argument("--event-port", type=int, default=49153, help="HTTP port for camera events")
    p.add_argument("--scan-exclude", default=os.environ.get("LUMIX_SCAN_EXCLUDE", ""),
                   help="comma-separated IPs the subnet scan must never contact")
    p.add_argument("--log-dir", default=os.path.join(ROOT, "logs"))
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    cfg = CameraConfig(ip=a.camera_ip or None, device_name=a.device_name, lv_port=a.lv_port,
                       event_port=a.event_port,
                       scan_exclude=[x.strip() for x in a.scan_exclude.split(",") if x.strip()],
                       log_dir=a.log_dir)
    host = a.host or ("0.0.0.0" if a.lan else "127.0.0.1")
    print("Lumix Remote on http://%s:%d" % ("127.0.0.1" if host == "0.0.0.0" else host, a.port))

    async def make_app() -> web.Application:
        # Build inside the running loop: asyncio primitives bind to it on Python 3.9.
        return build_app(Camera(cfg))

    web.run_app(make_app(), host=host, port=a.port, print=None)


if __name__ == "__main__":
    main()
