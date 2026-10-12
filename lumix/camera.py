"""High-level camera controller: discovery, handshake, subscription, keepalive,
reconnect, live view, typed commands and a pub/sub bus for the web layer.

Concurrency model
-----------------
* ``CamTransport`` serialises single HTTP requests (one in flight at a time).
* ``Camera.exclusive(owner)`` is an asyncio lock for *multi-step* operations
  (capture, set-and-verify, a future timelapse shot, a future play-mode file
  download). UI commands take it briefly; a long-running feature takes it per
  step, so UI commands interleave between steps instead of colliding.
* The supervisor loop (keepalive, subscription renewal, live-view watchdog)
  only issues single requests and never takes the exclusive lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Deque, Dict, List, Optional, Set

from . import discovery, exposure
from .events import EventListener
from .liveview import LiveViewReceiver
from .menu import MenuModel
from .protocol import CamError, CamTransport, Reply, parse_ddd, parse_state

log = logging.getLogger("lumix.camera")

CONFIG_DIR = os.path.expanduser(os.environ.get("LUMIX_REMOTE_HOME", "~/.config/lumix-remote"))
BUSY_OK = {"ok", "err_busy", "err_critical"}  # recmode replies that may still mean "in rec"


class Disconnected(Exception):
    pass


@dataclass
class CameraConfig:
    ip: Optional[str] = None                # fixed IP; None = discover
    device_name: str = "LumixRemote"        # shown on the camera
    lv_port: int = 49199                    # UDP live view port
    event_port: int = 49153                 # HTTP NOTIFY callback port
    scan_exclude: List[str] = field(default_factory=list)
    keepalive_s: float = 1.0
    approval_timeout_s: float = 180.0
    log_dir: Optional[str] = None           # raw request/response log


class Bus:
    """Fan-out of JSON-able messages to any number of async subscribers."""

    def __init__(self) -> None:
        self._subs: Set[asyncio.Queue] = set()

    def publish(self, kind: str, data: Any) -> None:
        msg = {"type": kind, "data": data, "ts": time.time()}
        for q in list(self._subs):
            if q.qsize() > 200:
                continue  # slow consumer; drop
            q.put_nowait(msg)

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue]:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        try:
            yield q
        finally:
            self._subs.discard(q)


def _client_uuid() -> str:
    # LUMIX Sync-style id: fixed prefix + 12 hex chars unique to this machine.
    node = uuid.getnode() & 0xFFFFFFFFFFFF
    return "4D454930-0100-1000-8001-%012X" % node


class Camera:
    def __init__(self, cfg: CameraConfig):
        self.cfg = cfg
        self.bus = Bus()
        self.status = "idle"
        self.status_msg = ""
        self.ip: Optional[str] = cfg.ip
        self.ip_fixed = cfg.ip is not None
        self.ddd: Dict[str, str] = {}
        self.capability: Dict[str, Any] = {}
        self.lens: Dict[str, Any] = {}
        self.touch_type: Optional[str] = None
        self.state: Dict[str, str] = {}
        self.exposure: Dict[str, Optional[str]] = {"focal": None, "shtrspeed": None}
        self.menu: Optional[MenuModel] = None
        self.events: Deque[Dict[str, Any]] = deque(maxlen=300)
        self.connected_at: Optional[float] = None
        self.liveview = LiveViewReceiver(cfg.lv_port)
        self.liveview_wanted = True
        self.listener = EventListener(cfg.event_port, self._on_notify)
        self.transport: Optional[CamTransport] = None
        self._op_lock = asyncio.Lock()
        self.op_owner: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._lv_restart_at = 0.0
        self._menu_dirty = asyncio.Event()
        self._ssdp_seen: Optional[str] = None
        self._ssdp_tr = None
        self._log_fh = None
        self.powered_off = False
        self.rec_started_at: Optional[float] = None
        # err_busy wedge tracking (see probe-results/FINDINGS.md "err_busy root cause")
        self.busy_streak = 0               # consecutive err_busy/err_critical to camcmd/startstream
        self.wedged = False                # camcmd stuck in err_busy; only a power cycle clears it
        self.wedged_since: Optional[float] = None
        self.body_busy = False             # camera NOTIFY "busy": someone is operating the body
        # Called once per wedge episode (e.g. a Bluetooth power-cycle hook).
        self.wedge_hooks: List[Any] = []
        self._reconnect_req = asyncio.Event()

    # ------------------------------------------------------------------ utils
    def _set_status(self, status: str, msg: str = "") -> None:
        if (status, msg) != (self.status, self.status_msg):
            self.status, self.status_msg = status, msg
            log.info("status: %s %s", status, msg)
            self.bus.publish("status", self.status_dict())

    def log_event(self, kind: str, text: str, **extra: Any) -> None:
        ev = {"ts": time.time(), "kind": kind, "text": text}
        ev.update(extra)
        self.events.append(ev)
        self.bus.publish("event", ev)

    def _trace(self, q: str, rep: Reply) -> None:
        self._track_busy(q, rep)
        if self._log_fh is None:
            return
        if q == "mode=getstate" and rep.ok:
            return  # 1 Hz noise; state changes are logged separately
        line = "[%s] GET /cam.cgi?%s -> %s %s\n" % (
            dt.datetime.now().strftime("%H:%M:%S.%f")[:-3], q, rep.status,
            _oneline(rep.text))
        self._log_fh.write(line)
        self._log_fh.flush()

    def _track_busy(self, q: str, rep: Reply) -> None:
        """Detect the S5 'camcmd wedge': every camcmd (and often startstream)
        answers err_busy until the camera is power-cycled."""
        if not (q.startswith("mode=camcmd") or q.startswith("mode=startstream")):
            return
        if "value=poweroff" in q:
            return
        if rep.result == "ok":
            if self.wedged:
                self.log_event("conn", "camera accepts commands again (wedge cleared)")
            self.busy_streak = 0
            self.wedged, self.wedged_since = False, None
            return
        if rep.result not in ("err_busy", "err_critical"):
            return
        self.busy_streak += 1
        if self.busy_streak >= 4 and not self.wedged and not self.body_busy:
            self.wedged, self.wedged_since = True, time.time()
            self.log_event("warn", "camera is stuck answering err_busy to camcmd/startstream "
                                   "(shutter, video, mode switch, live view). Only a camera "
                                   "power cycle clears it.", wedged=True)
            self.bus.publish("status", self.status_dict())
            for hook in list(self.wedge_hooks):
                asyncio.ensure_future(self._safe(hook(self)))

    def status_dict(self) -> Dict[str, Any]:
        st = self.state
        return {
            "status": self.status, "message": self.status_msg, "ip": self.ip,
            "camera": {k: self.ddd.get(k) for k in ("friendlyName", "modelNumber", "X_FirmVersion",
                                                    "serialNumber")},
            "connected_at": self.connected_at, "op_owner": self.op_owner,
            "battery": st.get("batt"), "battery_grip": st.get("batt_grip"),
            "cammode": st.get("cammode"), "recording": st.get("rec") == "on",
            "rec_progress": st.get("progress_time"), "rec_started_at": self.rec_started_at,
            "remain_photos": st.get("remaincapacity"), "remain_video_s": st.get("video_remaincapacity"),
            "sd": {"current": st.get("current_sd"), "sd1": st.get("sdcardstatus"),
                   "sd2": st.get("sd2_cardstatus"), "sd1_full": st.get("sd_full"),
                   "sd2_full": st.get("sd2_full"), "access": st.get("sd_access")},
            "temperature": st.get("temperature"), "lens": self.lens.get("name"),
            "liveview": self.liveview.stats(), "events_sub": bool(self.listener.sid),
            "powered_off": self.powered_off,
            "wedged": self.wedged, "wedged_since": self.wedged_since, "body_busy": self.body_busy,
        }

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self.cfg.log_dir:
            os.makedirs(self.cfg.log_dir, exist_ok=True)
            path = os.path.join(self.cfg.log_dir, "camera-%s.log" % dt.date.today().isoformat())
            self._log_fh = open(path, "a")
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self.transport is not None:
            with contextlib.suppress(Exception):
                await self.transport.cgi("stopstream", timeout=2)
            await self.transport.close()
        await self.listener.unsubscribe()
        await self.listener.stop()
        self.liveview.close()
        if self._ssdp_tr is not None:
            self._ssdp_tr.close()

    async def _run(self) -> None:
        try:
            await self.listener.start()
            await self.liveview.open()
        except OSError as e:
            log.error("cannot open local ports (another instance running?): %s", e)
            self._set_status("error", "cannot open ports %d/%d: %s" % (
                self.cfg.event_port, self.cfg.lv_port, e))
            return
        self._ssdp_tr = await discovery.start_ssdp_listener(self._on_ssdp)
        failures = 0
        while True:
            try:
                ip = await self._discover()
                if ip is None:
                    await self._pause(5)
                    continue
                await self._connect(ip)
                failures = 0
                await self._supervise()
            except asyncio.CancelledError:
                raise
            except (CamError, Disconnected, OSError, asyncio.TimeoutError) as e:
                failures += 1
                self.connected_at = None
                self.listener.sid = None
                msg = "camera powered off" if self.powered_off else str(e)
                self._set_status("reconnecting", msg)
                self.log_event("conn", "connection lost: %s" % msg)
                if failures >= 3 and not self.ip_fixed:
                    self.ip = None  # rediscover; DHCP may have moved it
                await self._pause(min(2 * failures, 10))
            except Exception as e:  # never let the supervisor die
                log.exception("unexpected error")
                self._set_status("error", repr(e))
                await asyncio.sleep(5)

    def request_reconnect(self, ip: Optional[str] = None, forget: bool = False) -> None:
        """Drop the session and connect again (to ``ip`` if given; ``forget``
        clears the remembered camera so discovery starts from scratch)."""
        if forget:
            self._save_state(last_ip=None)
            self.ip_fixed = False
            self.ip = None
        if ip:
            self.ip = ip
        elif not self.ip_fixed and self.status != "ready":
            self.ip = None
        self._reconnect_req.set()

    async def _pause(self, seconds: float) -> None:
        """Sleep, but wake up early on request_reconnect()."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._reconnect_req.wait(), seconds)
        self._reconnect_req.clear()

    def _on_ssdp(self, ip: str) -> None:
        if ip != self._ssdp_seen:
            self._ssdp_seen = ip
            self.log_event("ssdp", "camera announced itself at %s" % ip)

    async def _discover(self) -> Optional[str]:
        if self.ip:
            return self.ip
        self._set_status("discovering", "looking for a Lumix camera")
        cands: List[str] = []
        cached = self._load_state().get("last_ip")
        for c in (self._ssdp_seen, cached, discovery.AP_MODE_IP):
            if c and c not in cands and c not in self.cfg.scan_exclude:
                cands.append(c)
        for c in cands:
            d = await discovery.probe_ddd(c, timeout=2.5)
            if d:
                self.ip = c
                return c
        d = await discovery.scan(self.cfg.scan_exclude,
                                 progress=lambda m: self._set_status("discovering", m))
        if d:
            self.ip = d["ip"]
            return self.ip
        self._set_status("discovering", "no camera found; is it on with Wi-Fi remote shooting enabled?")
        return None

    # ------------------------------------------------------------- handshake
    async def _connect(self, ip: str) -> None:
        self.powered_off = False
        self._set_status("connecting", "contacting %s" % ip)
        if self.transport is None or self.transport.ip != ip:
            if self.transport is not None:
                await self.transport.close()
            self.transport = CamTransport(ip, trace=self._trace)
        t = self.transport
        self.liveview.camera_ip = ip
        # 1. device description (also proves the camera is there)
        raw = await t.get_raw(60606, discovery.DDD_PATH, timeout=6)
        self.ddd = parse_ddd(raw)
        self._save_state(last_ip=ip)
        await self._refresh_state()
        # 2. access request; "ok_under_research*" = wait (camera may show a prompt)
        deadline = time.monotonic() + self.cfg.approval_timeout_s
        while True:
            r = await t.cgi("accctrl", "req_acc", _client_uuid(), self.cfg.device_name)
            if r.text.startswith("ok,"):
                break
            if r.text.startswith("ok_under_research"):
                hint = "" if "no_msg" in r.text else " (approve the connection on the camera)"
                self._set_status("waiting_approval", "camera is registering this device" + hint)
            else:
                self._set_status("connecting", "req_acc -> %s" % r.text)
            if time.monotonic() > deadline:
                raise CamError("access not granted: %s" % r.text)
            await asyncio.sleep(1.5)
        self.log_event("conn", "access granted: %s" % r.text)
        await t.cgi("setsetting", "device_name", self.cfg.device_name)
        # 3. event subscription (moves the S5 off its "Connecting" screen)
        self.busy_streak = 0
        self.wedged, self.wedged_since, self.body_busy = False, None, False
        await self._subscribe_safely(ip)
        # 4. capabilities, menus, lens
        cap = await t.cgi("getinfo", "capability")
        self.capability = _capability_dict(cap)
        await self._load_menu()
        await self.refresh_lens()
        tt = await t.cgi("getsetting", "touch_type")
        self.touch_type = tt.setting_value("touch_type")
        # 5. record mode + live view
        await self.ensure_rec()
        await self._start_stream()
        self.connected_at = time.time()
        self._set_status("ready", "%s connected" % self.ddd.get("friendlyName", ip))

    async def _load_menu(self) -> None:
        assert self.transport
        key = "allmenu-%s-%s.xml" % (self.ddd.get("modelNumber", "x"), self.ddd.get("X_FirmVersion", "x"))
        cache = os.path.join(CONFIG_DIR, "cache", key)
        raw: Optional[bytes] = None
        if os.path.exists(cache):
            with open(cache, "rb") as f:
                raw = f.read()
        if raw is None:
            r = await self.transport.cgi("getinfo", "allmenu", timeout=20)
            if not r.ok:
                self.log_event("warn", "allmenu failed: %s" % r.result)
                return
            raw = r.text.encode()
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "wb") as f:
                f.write(raw)
        self.menu = MenuModel(raw)
        await self.refresh_menu()

    async def refresh_menu(self) -> List[str]:
        if self.menu is None or self.transport is None:
            return []
        r = await self.transport.cgi("getinfo", "curmenu", timeout=10)
        if not r.ok:
            return []
        changed = self.menu.apply_curmenu(r.text.encode())
        if changed:
            self.bus.publish("settings", {"changed": changed})
        return changed

    async def refresh_lens(self) -> None:
        assert self.transport
        r = await self.transport.cgi("getinfo", "lens")
        self.lens = exposure.parse_lens(r.csv) if r.csv else {}

    async def _subscribe_safely(self, ip: str) -> None:
        """SUBSCRIBE only while the camera is in play mode, then let it settle.

        On the S5 a fresh SUBSCRIBE while the camera is in *rec* mode makes it
        switch itself to play; a recmode sent during that switch answers
        err_critical and leaves every later camcmd answering err_busy until a
        power cycle (reproduced 2026-10-11 21:24). After boot the camera is in
        play mode, so the normal connect is unaffected; this matters for
        re-subscribing on a running session.
        """
        assert self.transport
        await self._refresh_state()
        if self.state.get("cammode") == "rec":
            r = await self.transport.cgi("camcmd", value="playmode")
            for _ in range(10):
                await asyncio.sleep(0.4)
                await self._refresh_state()
                if self.state.get("cammode") == "play":
                    break
            self.log_event("conn", "playmode before SUBSCRIBE -> %s (cammode=%s)"
                           % (r.result, self.state.get("cammode")))
        try:
            sub = await self.listener.subscribe(ip)
            self.log_event("conn", "event subscription: %s" % sub)
        except (OSError, asyncio.TimeoutError) as e:
            self.log_event("warn", "event subscription failed: %s" % e)
            return
        # Give the camera's own reaction to the subscription time to finish.
        modes = []
        for _ in range(6):
            await asyncio.sleep(0.5)
            await self._refresh_state()
            modes.append(self.state.get("cammode"))
        self.log_event("conn", "after SUBSCRIBE cammode %s" % "/".join(str(m) for m in modes))

    async def _resubscribe(self) -> None:
        """Subscription lost (renew refused): pause live view, re-subscribe from
        play mode, then back to rec + live view."""
        assert self.transport and self.ip
        async with self.exclusive("resubscribe"):
            await self.transport.cgi("stopstream", timeout=4)
            await self._subscribe_safely(self.ip)
            if self.liveview_wanted:
                await self.ensure_rec()
                await self._start_stream()

    async def ensure_rec(self) -> bool:
        """Put the camera in record mode. err_busy can mean 'already in rec'."""
        assert self.transport
        for attempt in range(3):
            r = await self.transport.cgi("camcmd", value="recmode")
            await asyncio.sleep(0.3)
            await self._refresh_state()
            if self.state.get("cammode") == "rec":
                return True
            if r.result not in BUSY_OK:
                self.log_event("warn", "recmode -> %s" % r.result)
            await asyncio.sleep(1)
        return False

    async def _start_stream(self) -> None:
        assert self.transport
        r = await self.transport.cgi("startstream", value=str(self.cfg.lv_port))
        self._lv_restart_at = time.monotonic()
        if not r.ok:
            self.log_event("warn", "startstream -> %s" % r.result)

    # ------------------------------------------------------------ supervisor
    async def _refresh_state(self) -> None:
        assert self.transport
        r = await self.transport.cgi("getstate", timeout=4)
        if not r.ok:
            return
        new = parse_state(r)
        if new != self.state:
            old = self.state
            self.state = new
            diff = {k: v for k, v in new.items() if old.get(k) != v}
            if old and self._log_fh:
                self._log_fh.write("[%s] state change %s\n" % (
                    dt.datetime.now().strftime("%H:%M:%S.%f")[:-3], diff))
                self._log_fh.flush()
            if old and old.get("rec") != new.get("rec"):
                self.rec_started_at = time.time() if new.get("rec") == "on" else None
                self.log_event("rec", "video recording %s" % ("started" if new.get("rec") == "on" else "stopped"))
            if old and old.get("remaincapacity") != new.get("remaincapacity"):
                self.log_event("state", "remaining shots %s -> %s" % (old.get("remaincapacity"),
                                                                       new.get("remaincapacity")))
            if old and old.get("cammode") != new.get("cammode"):
                self.log_event("state", "camera mode %s -> %s" % (old.get("cammode"), new.get("cammode")))
            self.bus.publish("status", self.status_dict())

    async def refresh_exposure(self) -> None:
        assert self.transport
        changed = False
        for k in ("focal", "shtrspeed"):
            r = await self.transport.cgi("getsetting", k, timeout=4)
            v = r.setting_value(k) if r.ok else None
            if v != self.exposure.get(k):
                self.exposure[k] = v
                changed = True
        if changed:
            self.bus.publish("exposure", self.exposure_dict())

    async def _supervise(self) -> None:
        assert self.transport
        fails = 0
        tick = 0
        last_menu = time.monotonic()
        while True:
            await asyncio.sleep(self.cfg.keepalive_s)
            tick += 1
            if self._reconnect_req.is_set():
                raise Disconnected("reconnect requested")
            try:
                await self._refresh_state()
                fails = 0
            except CamError as e:
                fails += 1
                if fails >= 3:
                    raise Disconnected("keepalive failed 3x: %s" % e)
                continue
            try:
                if self.state.get("cammode") == "rec" and tick % 2 == 0:
                    await self.refresh_exposure()
                if self.listener.needs_renew():
                    rr = await self.listener.renew(fallback=False)
                    if rr.get("status") != 200:
                        self.log_event("warn", "subscription renew refused (%s); re-subscribing" % rr)
                        await self._resubscribe()
                if self._menu_dirty.is_set() or time.monotonic() - last_menu > 15:
                    self._menu_dirty.clear()
                    last_menu = time.monotonic()
                    if not self._op_lock.locked():
                        await self.refresh_menu()
                await self._liveview_watchdog()
            except CamError as e:
                log.warning("supervisor step failed: %s", e)

    async def _liveview_watchdog(self) -> None:
        if not self.liveview_wanted or self._op_lock.locked():
            return
        age = self.liveview.age
        stale = age is None or age > 3
        if stale and time.monotonic() - self._lv_restart_at > 5:
            if self.state.get("cammode") != "rec":
                await self.ensure_rec()
            await self._start_stream()
            self.log_event("lv", "live view (re)started")

    async def _on_notify(self, ev: Dict[str, Any]) -> None:
        for p in ev.get("props", []):
            name, value = p["name"], p["value"]
            if name in ("SourceProtocolInfo", "SinkProtocolInfo"):
                continue
            self.log_event("notify", "%s=%s" % (name, value), name=name, value=value)
            if name == "X_Panasonic_Cam_Sync":
                self._menu_dirty.set()
                # "busy" = someone operates the camera body; "update"/lens_* when done
                self.body_busy = value == "busy"
                if value.startswith("lens") and self.transport:
                    asyncio.ensure_future(self._safe(self.refresh_lens()))

    @staticmethod
    async def _safe(coro) -> None:
        with contextlib.suppress(Exception):
            await coro

    # ----------------------------------------------------------- exclusive
    @contextlib.asynccontextmanager
    async def exclusive(self, owner: str, timeout: float = 30.0) -> AsyncIterator[CamTransport]:
        """Hold the camera for a multi-step operation. Raises Busy on timeout."""
        try:
            await asyncio.wait_for(self._op_lock.acquire(), timeout)
        except asyncio.TimeoutError:
            raise Busy("camera busy (%s)" % self.op_owner)
        self.op_owner = owner
        try:
            if self.transport is None or self.status != "ready":
                raise Busy("camera not connected (%s)" % self.status)
            yield self.transport
        finally:
            self.op_owner = None
            self._op_lock.release()

    # ------------------------------------------------------------- commands
    async def capture(self) -> Dict[str, Any]:
        async with self.exclusive("capture") as t:
            before = self.state.get("remaincapacity")
            r = await t.cgi("camcmd", value="capture", timeout=15)
            self.log_event("cmd", "capture -> %s" % r.result)
            # the camera needs a moment to write; watch remaincapacity / sd_access
            changed = False
            for _ in range(8):
                await asyncio.sleep(0.5)
                await self._refresh_state()
                if self.state.get("remaincapacity") != before:
                    changed = True
                    break
            # Some bodies need this to leave the review screen; harmless otherwise.
            await t.cgi("camcmd", value="autoreviewunlock")
            return {"result": r.result, "remain_before": before,
                    "remain_after": self.state.get("remaincapacity"), "count_changed": changed}

    async def video(self, start: bool) -> Dict[str, Any]:
        async with self.exclusive("video") as t:
            r = await t.cgi("camcmd", value="video_recstart" if start else "video_recstop", timeout=10)
            self.log_event("cmd", "%s -> %s" % ("video_recstart" if start else "video_recstop", r.result))
            await asyncio.sleep(0.5)
            await self._refresh_state()
            return {"result": r.result, "recording": self.state.get("rec") == "on"}

    async def touch_af(self, x: int, y: int) -> Dict[str, Any]:
        """Touch AF at x,y in 0..1000 live-view coordinates."""
        x, y = max(0, min(1000, int(x))), max(0, min(1000, int(y)))
        async with self.exclusive("touch") as t:
            # S5 (touch_type 5): press with value2=on, release with value2=off.
            # A second "on" without a release answers err_reject.
            r = await t.cgi("camctrl", "touch", "%d/%d" % (x, y), "on")
            await asyncio.sleep(0.15)
            r2 = await t.cgi("camctrl", "touch", "%d/%d" % (x, y), "off")
            self.log_event("cmd", "touch AF %d/%d -> %s, release -> %s" % (x, y, r.result, r2.result))
            return {"result": r.result, "release": r2.result}

    async def oneshot_af(self) -> Dict[str, Any]:
        async with self.exclusive("af") as t:
            r = await t.cgi("camcmd", value="oneshot_af")
            self.log_event("cmd", "oneshot_af -> %s" % r.result)
            return {"result": r.result}

    async def set_raw(self, cmd_type: str, value: str, value2: Optional[str] = None,
                      mode: str = "setsetting") -> Dict[str, Any]:
        async with self.exclusive("setting") as t:
            r = await t.cgi(mode, cmd_type, value, value2)
            self.log_event("cmd", "%s %s=%s%s -> %s" % (mode, cmd_type, value,
                                                         "/" + value2 if value2 else "", r.result))
            await asyncio.sleep(0.2)
            changed = await self.refresh_menu()
            if cmd_type in ("focal", "shtrspeed"):
                await self.refresh_exposure()
            return {"result": r.result, "changed": changed}

    async def set_setting(self, setting_id: str, option_id: str) -> Dict[str, Any]:
        if self.menu is None or setting_id not in self.menu.settings:
            raise KeyError(setting_id)
        s = self.menu.settings[setting_id]
        opt = next((o for o in s.options if o.id == option_id), None)
        if opt is None:
            raise KeyError(option_id)
        if opt.value is None:
            raise ValueError("option %s needs a parameter value" % option_id)
        res = await self.set_raw(opt.type, opt.value, opt.value2, opt.mode)
        res["setting"] = s.to_dict()
        return res

    async def camcmd(self, value: str) -> Dict[str, Any]:
        """Any camcmd (capture, video_recstart, oneshot_af, recmode, playmode, ...)."""
        if value == "poweroff":
            return await self.poweroff()
        async with self.exclusive("camcmd") as t:
            r = await t.cgi("camcmd", value=value, timeout=15)
            self.log_event("cmd", "camcmd %s -> %s" % (value, r.result))
            await asyncio.sleep(0.3)
            await self._refresh_state()
            return {"result": r.result, "text": r.text if r.xml is None else None}

    async def getsetting(self, cmd_type: str) -> Dict[str, Any]:
        if self.transport is None:
            raise Busy("camera not connected (%s)" % self.status)
        r = await self.transport.cgi("getsetting", cmd_type)
        val = r.xml.find("settingvalue").attrib if r.xml is not None and r.xml.find("settingvalue") is not None else {}
        return {"result": r.result, "type": cmd_type, "values": dict(val)}

    async def zoom(self, action: str) -> Dict[str, Any]:
        if action not in ("tele-normal", "tele-fast", "wide-normal", "wide-fast", "zoomstop"):
            raise ValueError(action)
        async with self.exclusive("zoom") as t:
            r = await t.cgi("camcmd", value=action)
            return {"result": r.result}

    async def poweroff(self) -> Dict[str, Any]:
        async with self.exclusive("poweroff") as t:
            r = await t.cgi("camcmd", value="poweroff")
            self.log_event("cmd", "poweroff -> %s" % r.result)
            if r.ok:
                self.powered_off = True
            return {"result": r.result}

    def exposure_dict(self) -> Dict[str, Any]:
        f, s = exposure.parse_code(self.exposure.get("focal")), exposure.parse_code(self.exposure.get("shtrspeed"))
        return {
            "focal": self.exposure.get("focal"), "focal_title": exposure.aperture_label(f) if f is not None else None,
            "shtrspeed": self.exposure.get("shtrspeed"),
            "shtrspeed_title": exposure.shutter_label(s) if s is not None else None,
            "aperture_options": exposure.aperture_options(self.lens),
            "shutter_options": exposure.shutter_options(self.lens),
        }

    # ----------------------------------------------------------- persistence
    def _load_state(self) -> Dict[str, Any]:
        try:
            with open(os.path.join(CONFIG_DIR, "state.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_state(self, **kw: Any) -> None:
        st = self._load_state()
        st.update(kw)
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(os.path.join(CONFIG_DIR, "state.json"), "w") as f:
            json.dump(st, f)


def _oneline(text: str, limit: int = 400) -> str:
    t = " ".join(text.split())
    return t if len(t) <= limit else t[:limit] + "...(%d bytes)" % len(text)


class Busy(Exception):
    pass


def _capability_dict(rep: Reply) -> Dict[str, Any]:
    if rep.xml is None:
        return {}
    x = rep.xml
    return {
        "proto": (x.findtext("comm_proto_ver") or ""),
        "model": x.findtext("productinfo/modelname"),
        "camcmds": [e.text for e in x.iter("camcmd")],
        "specs": [e.text for e in x.iter("camspec")],
    }
