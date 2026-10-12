"""Entry point: python -m server [--lan] [--camera ID] [--camera-ip IP] [--port 8770].

Runs one session per camera in ~/.config/lumix-remote/cameras.json (created on
first run; older single-camera config files are migrated). Camera options on
the command line (--camera-ip, ports, --model) apply to --camera ID (created if
new) or the active camera; with --camera-ip only that camera runs.
"""

from __future__ import annotations

import argparse
import atexit
import logging
import os
import signal
import time

from aiohttp import web

from lumix import models
from lumix.registry import Registry, migrate_pidfile, pidfile_path
from server.app import build_app
from server.sessions import ServerOptions, SessionManager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The one pidfile: `lumix server start|stop|status` reads the same file.
PIDFILE = pidfile_path()
LEGACY_PIDFILES = [os.path.join(ROOT, "logs", "lumix-server.pid")]


def _take_over_pidfile() -> None:
    """Only one server may own the camera session and its callback ports: stop a
    previous instance recorded in the pidfile and wait until it is gone."""
    try:
        with open(PIDFILE) as f:
            old = int(f.read().strip() or 0)
    except (OSError, ValueError):
        old = 0
    if not old:
        migrate_pidfile(LEGACY_PIDFILES)      # a server started by an older `lumix server start`
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
    p.add_argument("--camera", default=os.environ.get("LUMIX_CAMERA") or None, metavar="ID",
                   help="camera the options below apply to (created if new; default: the active one)")
    p.add_argument("--camera-ip", default=os.environ.get("LUMIX_CAMERA_IP"),
                   help="camera IP (default: discover); only this camera runs unless --all-cameras")
    p.add_argument("--all-cameras", action="store_true",
                   help="with --camera-ip: keep the other registered cameras running too")
    p.add_argument("--model", default=None, choices=[models.AUTO] + [m.id for m in models.PROFILES],
                   help="model profile for this run (default: the camera's setting, auto-detect)")
    p.add_argument("--camera-http-port", type=int, default=None, metavar="PORT",
                   help="camera cam.cgi port (default 80; for emulators)")
    p.add_argument("--camera-upnp-port", type=int, default=None, metavar="PORT",
                   help="camera ddd/SUBSCRIBE/ContentDirectory port (default 60606; for emulators)")
    p.add_argument("--camera-files-port", type=int, default=None, metavar="PORT",
                   help="camera file server port (default 50001; for emulators)")
    p.add_argument("--device-name", default="LumixRemote", help="name shown on the camera")
    p.add_argument("--lv-port", type=int, default=None,
                   help="UDP port for live view (default: the camera's, 49199 for the first)")
    p.add_argument("--event-port", type=int, default=49153,
                   help="HTTP port for camera events (shared by all cameras)")
    p.add_argument("--scan-exclude", default=os.environ.get("LUMIX_SCAN_EXCLUDE", ""),
                   help="comma-separated IPs the subnet scan must never contact")
    p.add_argument("--no-scan", action="store_true", default=os.environ.get("LUMIX_NO_SCAN") == "1",
                   help="never TCP-scan the LAN; find cameras only by fixed/remembered IP, SSDP, AP address")
    p.add_argument("--log-dir", default=os.path.join(ROOT, "logs"))
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    if a.camera is not None and not re_id(a.camera):
        p.error("--camera must be lowercase letters, digits, - or _")
    opts = ServerOptions(
        device_name=a.device_name, event_port=a.event_port,
        scan_exclude=[x.strip() for x in a.scan_exclude.split(",") if x.strip()],
        scan=not a.no_scan, log_dir=a.log_dir, target=a.camera, camera_ip=a.camera_ip or None, lv_port=a.lv_port,
        http_port=a.camera_http_port, upnp_port=a.camera_upnp_port,
        files_port=a.camera_files_port, model=a.model, all_cameras=a.all_cameras)
    host = a.host or ("0.0.0.0" if a.lan else "127.0.0.1")
    print("Lumix Remote on http://%s:%d" % ("127.0.0.1" if host == "0.0.0.0" else host, a.port))

    _take_over_pidfile()

    async def make_app() -> web.Application:
        # Build inside the running loop: asyncio primitives bind to it on Python 3.9.
        mgr = SessionManager(Registry(), opts)
        mgr.load()
        if mgr.migration:
            print("migrated the single-camera config to cameras.json (camera %r; old files in legacy/)"
                  % mgr.migration["camera"])
        for e in mgr.registry.cameras():
            run = mgr.should_run(e)
            print("  camera %-12s %-16s %s%s" % (e["id"], e.get("nickname") or "",
                                                 "active" if e["id"] == mgr.active_id else "",
                                                 "" if run else " (not started)"))
        return build_app(mgr)

    # shutdown_timeout: open browser connections are closed in on_shutdown; this
    # only bounds stragglers, well inside the 10 s before `lumix server stop` SIGKILLs
    web.run_app(make_app(), host=host, port=a.port, print=None, shutdown_timeout=3.0)


def re_id(s: str) -> bool:
    from lumix.registry import ID_RE
    return bool(ID_RE.match(s))


if __name__ == "__main__":
    main()
