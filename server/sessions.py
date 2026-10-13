"""Camera sessions: one Camera (+ Auto preset, file browser, BLE manager) per
registered camera, all running concurrently in the server process.

Each session gets its own UDP live-view port (from its registry entry) and its
own GENA callback path ``/Camera/event/<id>`` on the shared event port. The
"active" camera (``cameras.json`` "active") is what the un-namespaced ``/api/*``
routes, the CLI without ``--camera`` and a fresh browser tab talk to.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from lumix import models
from lumix.camera import Camera, CameraConfig
from lumix.files import FileBrowser
from lumix.presets import AutoPreset
from lumix.registry import Registry, RegistryError, RegistryStore, migrate
from lumix.timelapse import TimelapseEngine

log = logging.getLogger("server.sessions")

# Entry fields whose change needs a new Camera (fresh connect).
RESTART_FIELDS = ("model", "fixed_ip", "ports", "client_uuid", "device_name", "udn")


@dataclass
class ServerOptions:
    device_name: str = "LumixRemote"
    event_port: int = 49153
    scan_exclude: List[str] = field(default_factory=list)
    scan: bool = True
    log_dir: Optional[str] = None
    # Command-line overrides for one camera (``--camera``, default the active one):
    target: Optional[str] = None
    camera_ip: Optional[str] = None
    lv_port: Optional[int] = None
    http_port: Optional[int] = None
    upnp_port: Optional[int] = None
    files_port: Optional[int] = None
    model: Optional[str] = None
    # With --camera-ip only the target camera runs, unless all_cameras.
    all_cameras: bool = False


class CameraSession:
    def __init__(self, mgr: "SessionManager", entry: Dict[str, Any], cam: Optional[Camera] = None):
        self.mgr = mgr
        self.id: str = entry["id"]
        self.entry = entry
        self.cam = cam or Camera(mgr.camera_config(entry))
        cdir = mgr.registry.camera_dir(self.id)
        self.auto = AutoPreset(self.cam, os.path.join(cdir, "presets.json"))
        self.files = FileBrowser(self.cam, jobs_path=os.path.join(cdir, "imports.json"))
        self.timelapse = TimelapseEngine(self.cam, self.files, cdir)
        from server.ble_api import BleManager  # late: ble_api imports server.app
        self.ble = BleManager(self.cam, session=self)
        self.sources: Dict[str, Any] = {"wifi": self.cam.liveview}
        self.running = False
        self._run_watch: Optional[asyncio.Task] = None
        # quiesce hooks (server/quiesce.py): files <-> shooting, reconnect resume
        self.files.on_enter = self._files_enter
        self.files.on_leave = self._files_leave
        self.cam.ready_hooks.append(self._camera_ready)

    def resume_on_reconnect(self) -> bool:
        return bool((self.entry.get("settings") or {}).get("resume_on_reconnect", True))

    async def quiesce(self, reason: str, timeout: float = 15.0) -> Dict[str, Any]:
        from server import quiesce
        return await quiesce.quiesce(self, reason, timeout)

    async def _files_enter(self) -> None:
        from server import quiesce
        await quiesce.quiesce(self, "files", timeout=25)

    async def _files_leave(self) -> None:
        await self.timelapse.auto_resume("files")

    async def _camera_ready(self, cam: Camera) -> None:
        if not self.running:
            return
        from server import quiesce
        await quiesce.resume_after_reconnect(self)

    # an interval run seen this long with no job here is adopted (not one ending
    # right after a job's Stop, which getstate shows for a moment)
    ADOPT_AFTER_S = 2.0

    async def _watch_camera_runs(self) -> None:
        """Adopt an in-camera interval run that starts while connected.

        The S5 starts its own run for a shutter press in Time Lapse drive (the UI
        shutter after a drive-mode quick control, or the body's button). Without a
        job every command answered "stop the timelapse first" while
        /api/timelapse/stop had nothing to stop (chaos seed 2,
        tests/test_chaos.py). The reconnect hook already adopts such a run; this
        does it as soon as getstate shows it. Nothing is sent to the camera."""
        cam, tl = self.cam, self.timelapse
        seen: Optional[float] = None
        while True:
            # polls local state only (getstate is the supervisor's); a plain sleep,
            # not wait_for(queue): Python 3.9's wait_for can swallow the cancel
            await asyncio.sleep(0.5)
            if not cam.interval_running or cam.interval_owner is not None or tl.is_active():
                seen = None
                continue
            now = asyncio.get_event_loop().time()
            seen = seen if seen is not None else now
            if now - seen >= self.ADOPT_AFTER_S:
                with contextlib.suppress(Exception):
                    tl.adopt_camera_run()
                seen = None

    def reload(self) -> Dict[str, Any]:
        e = self.mgr.registry.get(self.id)
        if e is not None:
            self.entry = e
        return self.entry

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.cam.start()
        self.files.start()
        self.ble.start()
        self.timelapse.start()
        self._run_watch = asyncio.ensure_future(self._watch_camera_runs())

    async def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        if self._run_watch is not None:
            self._run_watch.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._run_watch
            self._run_watch = None
        for coro in (self.timelapse.shutdown(), self.ble.stop(), self.files.stop(), self.cam.stop()):
            with contextlib.suppress(Exception):
                await coro

    def summary(self) -> Dict[str, Any]:
        e, cam = self.reload(), self.cam
        st = cam.status_dict() if self.running else {}
        prof = cam.profile
        return {
            "id": self.id, "nickname": e.get("nickname"), "active": self.id == self.mgr.active_id,
            "model": e.get("model") or "auto", "profile": {"id": prof.id, "name": prof.name,
                                                           "verified": prof.verified},
            "running": self.running,
            "status": st.get("status", "paused"), "message": st.get("message", "not running"),
            "ip": cam.ip or e.get("fixed_ip") or e.get("last_ip"),
            "camera": {"friendlyName": e.get("friendly_name"), "modelNumber": e.get("model_number"),
                       "X_FirmVersion": e.get("firmware")},
            "provisional": bool(e.get("provisional")), "udn_bound": bool(e.get("udn")),
            "ble": {"registered": bool((e.get("ble") or {}).get("registered_at")),
                    "name": (e.get("ble") or {}).get("name"),
                    "home_ssid": (e.get("ble") or {}).get("home_ssid")},
            "ports": e.get("ports"), "settings": e.get("settings"),
            "connected_at": e.get("connected_at"), "added_at": e.get("added_at"),
            "wedged": st.get("wedged", False),
            "timelapse": (self.timelapse.job.state if self.timelapse.job
                          and self.timelapse.is_active() else None),
        }


class SessionManager:
    def __init__(self, registry: Optional[Registry] = None, opts: Optional[ServerOptions] = None):
        self.registry = registry or Registry()
        self.opts = opts or ServerOptions()
        self.sessions: Dict[str, CameraSession] = {}
        self.active_id: Optional[str] = None
        self.started = False
        self.ble_lock = asyncio.Lock()      # one Bluetooth radio for every camera
        self._lock = asyncio.Lock()
        self.migration: Optional[Dict[str, Any]] = None
        self._target: Optional[str] = None

    @classmethod
    def single(cls, cam: Camera) -> "SessionManager":
        """A manager around one existing Camera (tests, embedding); no registry entry."""
        from lumix.registry import new_entry
        mgr = cls()
        e = new_entry(cam.cfg.cam_id, "Camera", provisional=False)
        mgr.sessions[e["id"]] = CameraSession(mgr, e, cam=cam)
        mgr.active_id = e["id"]
        return mgr

    # ------------------------------------------------------------- config
    def camera_config(self, e: Dict[str, Any]) -> CameraConfig:
        o, ports = self.opts, dict(e.get("ports") or {})
        target = e["id"] == self.target_id()
        ip = (o.camera_ip if target and o.camera_ip else None) or e.get("fixed_ip") or None

        def port(name: str, override: Optional[int], default: int) -> int:
            if target and override:
                return int(override)
            return int(ports.get(name) or default)

        return CameraConfig(
            ip=ip, device_name=e.get("device_name") or o.device_name,
            lv_port=port("liveview", o.lv_port, 49199), event_port=o.event_port,
            scan_exclude=list(o.scan_exclude), scan=o.scan, log_dir=o.log_dir,
            cam_id=e["id"], model=(o.model if target and o.model else e.get("model") or "auto"),
            udn=e.get("udn"), client_uuid=e.get("client_uuid"),
            event_path="/Camera/event/%s" % e["id"],
            http_port=port("http", o.http_port, 80), upnp_port=port("upnp", o.upnp_port, 60606),
            files_port=port("files", o.files_port, 50001),
            claimed_ips=lambda cid=e["id"]: self.claimed_ips(cid),
            store=RegistryStore(self.registry, e["id"]))

    def target_id(self) -> Optional[str]:
        """The camera the command-line overrides (--camera-ip, ports, --model,
        --lv-port) apply to: --camera, else the camera active at start-up. Fixed
        once the cameras are loaded, so switching the active camera later never
        moves the overrides (a restarted session would take the other camera's
        live-view port)."""
        if self._target is not None:
            return self._target
        return self.opts.target or self.active_id

    def claimed_ips(self, for_id: str) -> Set[str]:
        """"ip:upnp_port" of every other session (current or remembered address),
        so discovery in one session never grabs another session's camera.
        Port-aware: emulators share 127.0.0.1 and differ only by port."""
        out: Set[str] = set()
        for sid, s in self.sessions.items():
            if sid == for_id:
                continue
            for ip in (s.cam.ip, s.entry.get("fixed_ip"), s.entry.get("last_ip")):
                if ip:
                    out.add("%s:%d" % (ip, s.cam.cfg.upnp_port))
        return out

    # ------------------------------------------------------------ lifecycle
    def load(self) -> None:
        """Migrate old single-camera files, make sure there is a camera to talk
        to (zero-setup first run), create a session per registered camera."""
        self.migration = migrate(self.registry)
        if self.migration:
            log.info("migrated single-camera config: %s", self.migration)
        o = self.opts
        if o.target and self.registry.get(o.target) is None:
            self.registry.add(o.target, cam_id=o.target, provisional=True)
        if not self.registry.cameras():
            # First run: one provisional camera that takes whatever Lumix it finds,
            # exactly like the single-camera server did.
            self.registry.add("Camera", cam_id="camera")
        self.active_id = self.registry.active_id()
        self._target = self.opts.target or self.active_id
        for e in self.registry.cameras():
            self.sessions[e["id"]] = CameraSession(self, e)

    def should_run(self, e: Dict[str, Any]) -> bool:
        if self.opts.camera_ip and not self.opts.all_cameras:
            return e["id"] == self.target_id()
        return bool((e.get("settings") or {}).get("autoconnect", True))

    async def start(self) -> None:
        self.started = True
        for s in list(self.sessions.values()):
            if self.should_run(s.entry):
                await s.start()

    async def stop(self) -> None:
        self.started = False
        for s in list(self.sessions.values()):
            await s.stop()

    async def quiesce_all(self, reason: str = "server-stop", timeout: float = 8.0) -> Dict[str, Any]:
        """Quiesce every running session in parallel (server stop: SIGTERM/SIGINT)."""
        run = [s for s in self.sessions.values() if s.running]
        if not run:
            return {}
        res = await asyncio.gather(*(asyncio.wait_for(s.quiesce(reason, timeout), timeout + 1)
                                     for s in run), return_exceptions=True)
        out = {}
        for s, r in zip(run, res):
            out[s.id] = r if isinstance(r, dict) else {"error": repr(r)}
            log.info("quiesce %s (%s): %s", s.id, reason, out[s.id])
        return out

    # ------------------------------------------------------------- access
    def get(self, cam_id: Optional[str]) -> Optional[CameraSession]:
        if cam_id is None:
            return self.active()
        s = self.sessions.get(cam_id)
        if s is None:
            e = self.registry.find(cam_id)        # nickname / UDN also work
            s = self.sessions.get(e["id"]) if e else None
        return s

    def active(self) -> Optional[CameraSession]:
        return self.sessions.get(self.active_id or "") or next(iter(self.sessions.values()), None)

    def list(self) -> List[Dict[str, Any]]:
        order = [e["id"] for e in self.registry.cameras()]
        return [self.sessions[i].summary() for i in order if i in self.sessions]

    def notify(self) -> None:
        """Push the camera list to every WebSocket (any session's bus)."""
        data = {"active": self.active_id, "cameras": self.list()}
        for s in self.sessions.values():
            s.cam.bus.publish("cameras", data)

    # ------------------------------------------------------------- changes
    async def add(self, nickname: Optional[str] = None, activate: bool = False,
                  **fields: Any) -> CameraSession:
        async with self._lock:
            e = self.registry.add(nickname, activate=activate, **fields)
            s = CameraSession(self, e)
            self.sessions[e["id"]] = s
            if activate or self.active_id is None:
                self.active_id = e["id"]
            if self.started and bool((e.get("settings") or {}).get("autoconnect", True)):
                await s.start()
        self.notify()
        return s

    async def remove(self, cam_id: str) -> Dict[str, Any]:
        async with self._lock:
            s = self.sessions.pop(cam_id, None)
            if s is None:
                raise RegistryError("no camera %r" % cam_id)
            if s.running:
                with contextlib.suppress(Exception):
                    await s.quiesce("remove", timeout=10)
            await s.stop()
            e = self.registry.remove(cam_id)
            self.active_id = self.registry.active_id()
        self.notify()
        return e

    async def update(self, cam_id: str, **fields: Any) -> CameraSession:
        """Change an entry; restarts the session when connection fields change."""
        async with self._lock:
            s = self.sessions.get(cam_id)
            if s is None:
                raise RegistryError("no camera %r" % cam_id)
            old = dict(s.reload())
            e = self.registry.update(cam_id, **fields)
            s.entry = e
            restart = any(k in fields and fields[k] != old.get(k) for k in RESTART_FIELDS)
            if fields.get("fixed_ip") and s.running and s.cam.ip != fields["fixed_ip"]:
                restart = True     # same entry value, but the session talks to another address
            settings = fields.get("settings") or {}
            if "watchdog" in settings:
                s.ble.watchdog_enabled = bool(settings["watchdog"])
            run = self.should_run(e) if self.started else False
            if restart:
                was = s.running
                await s.stop()
                s = CameraSession(self, e)
                self.sessions[cam_id] = s
                if was or run:
                    await s.start()
            elif run and not s.running:
                await s.start()
            elif self.started and not run and s.running and "settings" in fields:
                await s.stop()
        self.notify()
        return s

    async def set_active(self, cam_id: str) -> CameraSession:
        s = self.get(cam_id)
        if s is None:
            raise RegistryError("no camera %r" % cam_id)
        self.registry.set_active(s.id)
        self.active_id = s.id
        if self.started and not s.running and self.should_run(s.reload()):
            await s.start()
        self.notify()
        return s

    def models(self) -> List[Dict[str, Any]]:
        return models.catalog()
