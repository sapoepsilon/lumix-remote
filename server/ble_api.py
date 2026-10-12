"""Bluetooth power control: wake / power-cycle the camera, plus an opt-in watchdog.

GET  /api/ble/status         manager state, last results; ?probe=1 also scans + reads info over BLE
POST /api/ble/wake           BLE login + "start Wi-Fi via network" (powers up from OFF with Remote Wakeup ON)
POST /api/ble/power-cycle    Wi-Fi camcmd poweroff -> wait -> BLE wake -> reconnect
POST /api/ble/watchdog       {"enabled": true|false} toggle the watchdog at runtime
POST /api/ble/register       first-time registration (camera on Bluetooth > SET > Pairing); the
                             Mac's Wi-Fi briefly joins the camera's SoftAP, then rejoins home

bleak runs in its own interpreter (.venv-ble, Python >= 3.10 + pyobjc), so every
BLE action is a ``bin/lumix-ble`` subprocess that prints one JSON line.

Watchdog (off by default; LUMIX_BLE_WATCHDOG=1 turns it on): when the camera has
been wedged (camcmd/startstream answering err_busy, Camera.wedged) for longer
than LUMIX_BLE_WEDGE_AFTER seconds (60), power-cycle it over BLE. Backoff
doubles after each cycle that does not clear the wedge (LUMIX_BLE_BACKOFF, 120 s
start), and at most LUMIX_BLE_MAX_PER_HOUR (3) cycles run per rolling hour.
LUMIX_BLE_NAME pins the advertised name (e.g. S5-XXXXXX) when the camera's
registry entry has none.

Per camera: every route also exists under /api/cameras/{cam}/ble/...; the
subprocess gets LUMIX_CAMERA_ID so it reads and writes that camera's "ble"
section of cameras.json. The watchdog switch is stored in the camera's
settings ("watchdog"). One Bluetooth radio: BLE actions of all cameras are
serialised by one lock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from aiohttp import web

from lumix.camera import Camera
from server.app import _body, _guard, _json, camera_route, session

log = logging.getLogger("server.ble")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLE_BIN = os.path.join(ROOT, "bin", "lumix-ble")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_on(name: str) -> bool:
    return os.environ.get(name, "") in ("1", "true", "yes", "on")


class BleManager:
    def __init__(self, cam: Camera, session: Any = None):
        self.cam = cam
        self.session = session        # server.sessions.CameraSession (None: stand-alone)
        self.lock: asyncio.Lock = session.mgr.ble_lock if session is not None else asyncio.Lock()
        self.running: Optional[str] = None
        self.history: Deque[Dict[str, Any]] = deque(maxlen=20)
        settings = (session.entry.get("settings") or {}) if session is not None else {}
        self.watchdog_enabled = bool(settings.get("watchdog")) or _env_on("LUMIX_BLE_WATCHDOG")
        self.wedge_after = _env_float("LUMIX_BLE_WEDGE_AFTER", 60)
        self.max_per_hour = int(_env_float("LUMIX_BLE_MAX_PER_HOUR", 3))
        self.backoff0 = _env_float("LUMIX_BLE_BACKOFF", 120)
        self.backoff = self.backoff0
        self.cycles: Deque[float] = deque()
        self.next_allowed = 0.0
        self.watchdog_note = ""
        self._task: Optional[asyncio.Task] = None

    @property
    def name(self) -> Optional[str]:
        """Advertised BLE name: the camera's registry entry, else LUMIX_BLE_NAME."""
        if self.session is not None:
            n = (self.session.reload().get("ble") or {}).get("name")
            if n:
                return n
        return os.environ.get("LUMIX_BLE_NAME") or None

    # ---------------------------------------------------------------- runner
    async def run(self, *args: str, timeout: float = 240) -> Dict[str, Any]:
        if not os.access(BLE_BIN, os.X_OK):
            return {"ok": False, "error": "bin/lumix-ble missing"}
        name = self.name
        cmd = [BLE_BIN] + (["--name", name] if name else []) + list(args)
        env = dict(os.environ)
        if self.session is not None:
            env["LUMIX_CAMERA_ID"] = self.session.id
        async with self.lock:
            self.running = args[0]
            t0 = time.time()
            self.cam.log_event("ble", "lumix-ble %s" % " ".join(args))
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)

                async def pump_err() -> List[str]:
                    lines: List[str] = []
                    assert proc.stderr
                    async for raw in proc.stderr:
                        s = raw.decode("utf-8", "replace").rstrip()
                        lines.append(s)
                        log.info("ble: %s", s)
                    return lines
                err_task = asyncio.ensure_future(pump_err())
                try:
                    out, _ = await asyncio.wait_for(
                        asyncio.gather(proc.stdout.read(), proc.wait()), timeout)  # type: ignore[union-attr]
                except asyncio.TimeoutError:
                    proc.kill()
                    res: Dict[str, Any] = {"ok": False, "error": "timeout after %ds" % timeout}
                else:
                    lines = [x for x in out.decode("utf-8", "replace").splitlines() if x.strip()]
                    try:
                        res = json.loads(lines[-1]) if lines else {"ok": False, "error": "no output"}
                    except ValueError:
                        res = {"ok": False, "error": "bad output: %s" % lines[-1][:200]}
                tail = await err_task
                res["log"] = tail[-40:]
            finally:
                self.running = None
            entry = {"ts": t0, "cmd": args[0], "ok": bool(res.get("ok")),
                     "elapsed_s": round(time.time() - t0, 1), "error": res.get("error")}
            self.history.append(entry)
            if self.session is not None:
                self.session.reload()      # the subprocess may have updated cameras.json
            self.cam.log_event("ble", "%s -> %s (%.0fs)%s" % (
                args[0], "ok" if entry["ok"] else "FAILED", entry["elapsed_s"],
                (": %s" % entry["error"]) if entry["error"] else ""))
            return res

    # --------------------------------------------------------------- actions
    async def wake(self) -> Dict[str, Any]:
        args = ["wake"]
        if self.cam.ip:
            args += ["--camera-ip", self.cam.ip]
        res = await self.run(*args)
        if res.get("ok"):
            self.cam.request_reconnect(ip=res.get("ip") or None)
        return res

    async def power_cycle(self, reason: str = "manual") -> Dict[str, Any]:
        """camcmd poweroff over Wi-Fi (in-process, this server is already
        registered), wait for the camera to drop off, then BLE wake."""
        t0 = time.time()
        res: Dict[str, Any] = {"reason": reason, "ip": self.cam.ip}
        # The S5 rejects camcmd poweroff (err_reject) in play mode: go to rec first.
        try:
            if self.cam.state.get("cammode") != "rec":
                async with self.cam.exclusive("ble-power-cycle"):
                    res["recmode"] = await self.cam.ensure_rec()
            res["poweroff"] = await asyncio.wait_for(self.cam.poweroff(), 20)
            if res["poweroff"].get("result") == "err_reject":
                async with self.cam.exclusive("ble-power-cycle"):
                    await self.cam.ensure_rec()
                res["poweroff_retry"] = await asyncio.wait_for(self.cam.poweroff(), 20)
        except Exception as e:
            res["poweroff_error"] = "%s: %s" % (type(e).__name__, e)
        loop = asyncio.get_running_loop()
        from lumix.ble import ddd_ok  # stdlib-only helpers; bleak is not imported
        if self.cam.ip:
            for _ in range(20):
                if not await loop.run_in_executor(None, ddd_ok, self.cam.ip, 1.0):
                    break
                await asyncio.sleep(1)
        res["off_s"] = round(time.time() - t0, 1)
        if self.cam.ip and await loop.run_in_executor(None, ddd_ok, self.cam.ip, 1.0):
            # Wedged S5: poweroff answers err_reject and there is no BLE power-off.
            res["ok"] = False
            res["error"] = "camera refused poweroff and stayed on (wedged?)"
            res["total_s"] = round(time.time() - t0, 1)
            return res
        await asyncio.sleep(_env_float("LUMIX_BLE_OFF_WAIT", 8))
        res["wake"] = await self.wake()
        res["ok"] = bool(res["wake"].get("ok"))
        if not res["ok"]:
            res["error"] = res["wake"].get("error")
        res["total_s"] = round(time.time() - t0, 1)
        return res

    # -------------------------------------------------------------- watchdog
    def start(self) -> None:
        self._task = asyncio.ensure_future(self._watch())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self._watch_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ble watchdog")

    async def _watch_tick(self) -> None:
        cam = self.cam
        if not cam.wedged:
            if self.backoff != self.backoff0 and cam.status == "ready":
                self.backoff = self.backoff0
            self.watchdog_note = ""
            return
        if not self.watchdog_enabled or self.lock.locked():
            return
        now = time.time()
        stuck = now - (cam.wedged_since or now)
        if stuck < self.wedge_after:
            self.watchdog_note = "wedged %ds, cycling at %ds" % (stuck, self.wedge_after)
            return
        while self.cycles and now - self.cycles[0] > 3600:
            self.cycles.popleft()
        if len(self.cycles) >= self.max_per_hour:
            self.watchdog_note = "hourly limit reached (%d)" % self.max_per_hour
            return
        if now < self.next_allowed:
            self.watchdog_note = "backoff, next try in %ds" % (self.next_allowed - now)
            return
        self.cycles.append(now)
        self.next_allowed = now + self.backoff
        self.backoff = min(self.backoff * 2, 3600)
        cam.log_event("warn", "BLE watchdog: camera wedged for %ds, power-cycling" % stuck)
        res = await self.power_cycle("watchdog")
        self.watchdog_note = "last cycle %s" % ("ok" if res.get("ok") else "failed: %s" % res.get("error"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "camera_id": self.session.id if self.session is not None else None,
            "name": self.name, "running": self.running, "busy": self.lock.locked(),
            "registered": bool(((self.session.entry.get("ble") or {}) if self.session is not None
                                else {}).get("registered_at")),
            "history": list(self.history),
            "watchdog": {"enabled": self.watchdog_enabled, "wedge_after_s": self.wedge_after,
                         "max_per_hour": self.max_per_hour, "cycles_last_hour":
                         sum(1 for t in self.cycles if time.time() - t < 3600),
                         "backoff_s": self.backoff, "note": self.watchdog_note,
                         "camera_wedged": self.cam.wedged, "wedged_since": self.cam.wedged_since},
        }


def _m(request: web.Request) -> BleManager:
    return session(request).ble


async def ble_status(request: web.Request) -> web.Response:
    m = _m(request)
    out = m.to_dict()
    if request.query.get("probe") in ("1", "true"):
        if m.lock.locked():
            out["probe"] = {"ok": False, "error": "busy (%s)" % m.running}
        else:
            out["probe"] = await m.run("status", timeout=60)
    return _json(out)


@_guard
async def ble_wake(request: web.Request) -> web.Response:
    m = _m(request)
    if m.lock.locked():
        return _json({"error": "BLE busy (%s)" % m.running}, 409)
    res = await m.wake()
    return _json(res, 200 if res.get("ok") else 502)


@_guard
async def ble_power_cycle(request: web.Request) -> web.Response:
    m = _m(request)
    if m.lock.locked():
        return _json({"error": "BLE busy (%s)" % m.running}, 409)
    res = await m.power_cycle("api")
    return _json(res, 200 if res.get("ok") else 502)


@_guard
async def ble_register(request: web.Request) -> web.Response:
    m = _m(request)
    if m.lock.locked():
        return _json({"error": "BLE busy (%s)" % (m.running or "another camera")}, 409)
    res = await m.run("register", timeout=420)
    if res.get("ok"):
        m.cam.log_event("ble", "Bluetooth registration done; this computer can now wake the camera")
        if m.session is not None:
            m.session.mgr.notify()
    return _json(res, 200 if res.get("ok") else 502)


@_guard
async def ble_watchdog(request: web.Request) -> web.Response:
    b = await _body(request)
    on = b.get("enabled")
    if not isinstance(on, bool):
        raise ValueError("enabled must be true or false")
    m = _m(request)
    m.watchdog_enabled = on
    if m.session is not None:
        await m.session.mgr.update(m.session.id, settings={"watchdog": on})
    return _json(m.to_dict()["watchdog"])


def setup(app: web.Application) -> None:
    """Routes; each camera session owns its BleManager (started with the session)."""
    camera_route(app, "GET", "/api/ble/status", ble_status)
    camera_route(app, "POST", "/api/ble/wake", ble_wake)
    camera_route(app, "POST", "/api/ble/power-cycle", ble_power_cycle)
    camera_route(app, "POST", "/api/ble/watchdog", ble_watchdog)
    camera_route(app, "POST", "/api/ble/register", ble_register)
