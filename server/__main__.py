"""Entry point: python -m server [--lan] [--camera-ip IP] [--port 8770]."""

from __future__ import annotations

import argparse
import atexit
import logging
import os
import signal
import time

from aiohttp import web

from lumix.camera import Camera, CameraConfig
from server.app import build_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


PIDFILE = os.path.join(os.path.expanduser(os.environ.get("LUMIX_REMOTE_HOME", "~/.config/lumix-remote")),
                       "server.pid")


def _take_over_pidfile() -> None:
    """Only one server may own the camera session and its callback ports: stop a
    previous instance recorded in the pidfile and wait until it is gone."""
    try:
        with open(PIDFILE) as f:
            old = int(f.read().strip() or 0)
    except (OSError, ValueError):
        old = 0
    if old and old != os.getpid():
        try:
            os.kill(old, signal.SIGTERM)
            print("stopping previous server (pid %d)" % old)
            for _ in range(50):
                time.sleep(0.2)
                os.kill(old, 0)
            os.kill(old, signal.SIGKILL)
            time.sleep(0.5)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
    os.makedirs(os.path.dirname(PIDFILE), exist_ok=True)
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))

    def _cleanup() -> None:
        try:
            with open(PIDFILE) as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(PIDFILE)
        except OSError:
            pass
    atexit.register(_cleanup)


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

    _take_over_pidfile()

    async def make_app() -> web.Application:
        # Build inside the running loop: asyncio primitives bind to it on Python 3.9.
        return build_app(Camera(cfg))

    web.run_app(make_app(), host=host, port=a.port, print=None)


if __name__ == "__main__":
    main()
