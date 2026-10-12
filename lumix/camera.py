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
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Deque, Dict, List, Optional, Set

from . import discovery, exposure, models
from .events import EventListener
from .liveview import LiveViewReceiver
from .menu import MenuModel
from .models import ModelProfile
from .protocol import (CamError, CamTransport, Reply, encrypt_acc, parse_ddd, parse_nonce, parse_reply,
                       parse_state)
from .registry import JsonStore

log = logging.getLogger("lumix.camera")

CONFIG_DIR = os.path.expanduser(os.environ.get("LUMIX_REMOTE_HOME", "~/.config/lumix-remote"))
SUBSCRIBE_SETTLE_S = 30.0      # default max wait for the post-SUBSCRIBE mod_Play (see models)
LUMIX_SYNC_UA = "LUMIX Sync"   # User-Agent LUMIX Sync sends on every cam.cgi request
SESSION_PROBE = "mode=getsetting&type=touch_type"   # needs an accepted session
RECMODE_GUARD = "err_local_recmode_guard"            # recmode withheld by the churn guard


class Disconnected(Exception):
    pass


@dataclass
class CameraConfig:
    ip: Optional[str] = None                # fixed IP; None = discover
    device_name: str = "LumixRemote"        # shown on the camera
    lv_port: int = 49199                    # UDP live view port
    event_port: int = 49153                 # HTTP NOTIFY callback port
    scan_exclude: List[str] = field(default_factory=list)
    scan: bool = True                       # TCP-scan the local /24s when nothing else finds it
    keepalive_s: float = 1.0
    approval_timeout_s: float = 180.0
    log_dir: Optional[str] = None           # raw request/response log
    # --- per-camera identity (multi-camera; see lumix/registry.py)
    cam_id: str = "camera"                  # registry id; names the log file and callback path
    model: str = "auto"                     # model profile id, "auto" = from ddd modelNumber
    udn: Optional[str] = None               # bind to this camera (ddd UDN); None = any Lumix
    client_uuid: Optional[str] = None       # req_acc value (MEI0 id); None = this machine's
    event_path: str = "/Camera/event"       # GENA callback path on event_port
    # --- camera-side ports (real bodies: 80 / 60606 / 50001; emulators override)
    http_port: int = 80
    upnp_port: int = 60606
    files_port: int = 50001
    # IPs owned by other camera sessions: discovery skips them
    claimed_ips: Optional[Callable[[], Set[str]]] = None
    # where last IP / identity are remembered (load() -> dict, save(**kw)); None = state.json
    store: Any = None


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
    # session probe (getsetting) cadence: while live view frames are missing / otherwise
    PROBE_STALE_S = 15.0
    PROBE_EVERY_S = 60.0
    PROBE_FRAME_AGE_S = 10.0       # live view counts as missing after this
    # recmode churn guard: the 4th recmode within 20 s wedged the S5 (FINDINGS 22:01)
    RECMODE_MAX = 3
    RECMODE_WINDOW_S = 20.0
    RECMODE_TRIES = 3              # err_busy retries for recmode (stays inside the guard)

    def __init__(self, cfg: CameraConfig):
        self.cfg = cfg
        self.store = cfg.store if cfg.store is not None else JsonStore(os.path.join(CONFIG_DIR, "state.json"))
        self.profile: ModelProfile = models.resolve(cfg.model, None)
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
        self.liveview = LiveViewReceiver(cfg.lv_port, self.profile.liveview)
        self.liveview_wanted = True
        self.listener = EventListener(cfg.event_port, self._on_notify, path=cfg.event_path,
                                      upnp_port=cfg.upnp_port)
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
        self._lv_running = False           # we sent startstream and have not stopped it
        self._lv_pending: Optional[str] = None   # live-view start requested by an event
        self._lv_backoff = 10.0
        self._busy_ended = False
        self._last_ok = time.monotonic()
        self._mod_play = asyncio.Event()   # set by the mod_Play NOTIFY
        self._reconnect_req = asyncio.Event()
        # Called (ensure_future) every time a connect reaches "ready": resume hooks.
        self.ready_hooks: List[Callable[["Camera"], Any]] = []
        # Set while an in-camera interval (time-lapse) run is armed or running:
        # the body answers err_busy to capture/recmode/playmode then, which is
        # not the wedge.
        self.interval_owner: Optional[str] = None
        # Fire-and-forget helpers (NOTIFY reactions, hooks): tracked so stop()
        # can cancel and await them instead of leaving pending tasks behind.
        self._bg: Set[asyncio.Task] = set()
        self._probe_at = 0.0               # last periodic session probe (supervisor)
        self._recmode_sent: Deque[float] = deque(maxlen=8)
        self._reject_probe_at = float("-inf")  # last session probe after an err_reject (monotonic may start near 0)

    @property
    def interval_active(self) -> bool:
        return (self.interval_owner is not None
                or self.state.get("interval_status") in ("run", "pause"))

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
        self._check_reject(q, rep)
        if self._log_fh is None:
            return
        if q == "mode=getstate" and rep.ok:
            return  # 1 Hz noise; state changes are logged separately
        line = "[%s] GET /cam.cgi?%s -> %s %s\n" % (
            dt.datetime.now().strftime("%H:%M:%S.%f")[:-3], q, rep.status,
            _oneline(rep.text))
        self._log_fh.write(line)
        self._log_fh.flush()

    def _check_reject(self, q: str, rep: Reply) -> None:
        """A camera that rebooted (or dropped us) between two keepalives still
        answers getstate/getinfo before a new req_acc, so the 1 Hz getstate never
        notices; every other command answers err_reject. After such an answer,
        probe the session once (rate-limited) and reconnect if it is gone."""
        if rep.result != "err_reject" or self.status != "ready":
            return
        if q.startswith(("mode=getstate", "mode=getinfo", "mode=accctrl")):
            return
        if q == SESSION_PROBE or time.monotonic() - self._reject_probe_at < 5.0:
            return
        self._reject_probe_at = time.monotonic()
        self._spawn(self._probe_session())

    async def _probe_session(self) -> None:
        if self.transport is None:
            return
        r = await self.transport.cgi("getsetting", "touch_type", timeout=4)
        if r.result == "err_reject" and self.status == "ready":
            self.log_event("conn", "camera no longer accepts this session (it restarted or "
                                   "dropped us); reconnecting")
            self._reconnect_req.set()

    def _track_busy(self, q: str, rep: Reply) -> None:
        """Detect the S5 'camcmd wedge': every camcmd (and often startstream)
        answers err_busy until the camera is power-cycled."""
        if not (q.startswith("mode=camcmd") or q.startswith("mode=startstream")):
            return
        if "value=poweroff" in q:
            return
        if self.interval_active and rep.result != "ok":
            return      # the body is busy with its own interval run, not wedged
        if rep.result == "ok":
            if self.wedged:
                self.log_event("conn", "camera accepts commands again (wedge cleared)")
            self.busy_streak = 0
            self.wedged, self.wedged_since = False, None
            return
        if rep.result not in ("err_busy", "err_critical"):
            return
        self.busy_streak += 1
        # cmd() retries err_busy 5 x 1 s, so 10 = two commands that exhausted
        # LUMIX Sync's whole retry budget while the body is not reported busy
        if self.busy_streak >= self.profile.connect.wedge_busy_streak:
            self._mark_wedged("camcmd/startstream")

    def _mark_wedged(self, why: str) -> None:
        if self.wedged or self.body_busy:
            return
        self.wedged, self.wedged_since = True, time.time()
        self.log_event("warn", "camera is stuck answering err_busy to camcmd/startstream "
                               "(shutter, video, mode switch, live view; seen on %s). Only a "
                               "camera power cycle clears it." % why, wedged=True)
        self.bus.publish("status", self.status_dict())
        for hook in list(self.wedge_hooks):
            self._spawn(hook(self))

    def status_dict(self) -> Dict[str, Any]:
        st = self.state
        return {
            "camera_id": self.cfg.cam_id, "model": self.profile.id, "model_name": self.profile.name,
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
            name = "camera-%s.log" % dt.date.today().isoformat()
            if self.cfg.cam_id not in ("camera", ""):
                name = "camera-%s-%s.log" % (self.cfg.cam_id, dt.date.today().isoformat())
            path = os.path.join(self.cfg.log_dir, name)
            self._log_fh = open(path, "a")
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        await self._cancel_background()
        if self.transport is not None:
            if self._lv_running or not self.profile.connect.stopstream_only_if_running:
                # stopstream with no stream answers err_critical on the S5
                with contextlib.suppress(Exception):
                    await self.transport.cgi("stopstream", timeout=2)
            self._lv_running = False
            await self.transport.close()
        await self.listener.unsubscribe()
        await self.listener.stop()
        self.liveview.close()
        if self._ssdp_tr is not None:
            self._ssdp_tr.close()

    async def _run(self) -> None:
        # A previous instance may still be shutting down: retry the ports for a
        # while instead of giving up for good.
        for attempt in range(30):
            try:
                await self.listener.start()
                await self.liveview.open()
                break
            except OSError as e:
                log.error("cannot open local ports (another instance running?): %s", e)
                self._set_status("error", "cannot open ports %d/%d: %s (retrying)" % (
                    self.cfg.event_port, self.cfg.lv_port, e))
                await asyncio.sleep(2)
        else:
            self._set_status("error", "ports %d/%d stay in use; stop the other Lumix Remote "
                             "instance" % (self.cfg.event_port, self.cfg.lv_port))
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
                was_streaming, self._lv_running = self._lv_running, False
                # LUMIX Sync: on disconnect stopstream + UNSUBSCRIBE, then full reconnect
                # (stopstream only for a stream we started: else err_critical on the S5)
                if self.transport is not None and (
                        was_streaming or not self.profile.connect.stopstream_only_if_running):
                    with contextlib.suppress(Exception):
                        await self.transport.cgi("stopstream", timeout=2)
                with contextlib.suppress(Exception):
                    await self.listener.unsubscribe()
                self.listener.sid = None
                msg = "camera powered off" if self.powered_off else _friendly_error(e, self.ip)
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
            self._save_state(last_ip=None, udn=None)
            self.cfg.udn = None
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

    def _claimed(self) -> Set[str]:
        """IPs other sessions own *on our UPnP port* (emulators share an IP and
        differ by port, so claims are "ip:upnp_port")."""
        try:
            keys = set(self.cfg.claimed_ips()) if self.cfg.claimed_ips else set()
        except Exception:
            return set()
        suffix = ":%d" % self.cfg.upnp_port
        return {k[:-len(suffix)] for k in keys if k.endswith(suffix)}

    def accepts(self, ddd: Dict[str, str]) -> bool:
        """Is this device description ours? A session bound to a UDN only takes
        that camera; an unbound one takes any Lumix no other session owns."""
        if self.cfg.udn:
            return ddd.get("UDN") == self.cfg.udn
        return ddd.get("ip") not in self._claimed()

    async def _discover(self) -> Optional[str]:
        if self.ip:
            return self.ip
        self._set_status("discovering", "looking for a Lumix camera")
        cands: List[str] = []
        cached = self._load_state().get("last_ip")
        claimed = self._claimed()
        for c in (self._ssdp_seen, cached, discovery.AP_MODE_IP):
            if c and c not in cands and c not in self.cfg.scan_exclude and c not in claimed:
                cands.append(c)
        for c in cands:
            d = await discovery.probe_ddd(c, timeout=2.5, port=self.cfg.upnp_port)
            if d and self.accepts(d):
                self.ip = c
                return c
        if not self.cfg.scan:
            self._set_status("discovering", "no camera at the remembered address (LAN scan is off); "
                                            "is it on with Wi-Fi remote shooting enabled?")
            return None
        d = await discovery.scan(list(self.cfg.scan_exclude) + sorted(claimed),
                                 progress=lambda m: self._set_status("discovering", m),
                                 accept=self.accepts, port=self.cfg.upnp_port)
        if d:
            self.ip = d["ip"]
            return self.ip
        self._set_status("discovering", "no camera found; is it on with Wi-Fi remote shooting enabled?")
        return None

    # ------------------------------------------------------------- handshake
    async def _connect(self, ip: str) -> None:
        """Connect in LUMIX Sync's order (docs/lumix-sync-wifi-flow.md section 4):
        ddd, req_acc (1 s loop), SUBSCRIBE, 200 ms, raw_img_send, capability,
        allmenu, curmenu, lens, touch_type, getstate, device_name; then the
        live-view start (stopstream, recmode, startstream) once."""
        # powered_off stays set until a connect succeeds: every failed attempt
        # in between still means "the camera is off", not "out of range"
        self._set_status("connecting", "contacting %s" % ip)
        if self.transport is None or self.transport.ip != ip:
            if self.transport is not None:
                await self.transport.close()
            self.transport = CamTransport(ip, user_agent=LUMIX_SYNC_UA, trace=self._trace,
                                          port=self.cfg.http_port)
        t = self.transport
        self.liveview.camera_ip = ip
        self.busy_streak = 0
        self.wedged, self.wedged_since, self.body_busy = False, None, False
        self._lv_running = False
        # 1. device description (also proves the camera is there)
        raw = await t.get_raw(self.cfg.upnp_port, discovery.DDD_PATH, timeout=6)
        ddd = parse_ddd(raw)
        if self.cfg.udn and ddd.get("UDN") and ddd.get("UDN") != self.cfg.udn:
            if not self.ip_fixed:
                self.ip = None
            raise Disconnected("%s is another camera (%s)" % (ip, ddd.get("friendlyName")))
        self.ddd = ddd
        self.profile = models.resolve(self.cfg.model, ddd)
        self.liveview.rules = self.profile.liveview
        q = self.profile.connect
        if ddd.get("UDN"):
            self.cfg.udn = ddd["UDN"]
        self._save_state(last_ip=ip, udn=ddd.get("UDN"), model_number=ddd.get("modelNumber"),
                         friendly_name=ddd.get("friendlyName"), firmware=ddd.get("X_FirmVersion"))
        self.log_event("conn", "%s %s (fw %s): model profile %s" % (
            ddd.get("modelNumber"), ddd.get("friendlyName"), ddd.get("X_FirmVersion"), self.profile.id))
        # 2. access request; "ok_under_research*" = wait (camera may show a prompt)
        acc_type, acc_v1, acc_v2 = await self._access_values(t)
        deadline = time.monotonic() + self.cfg.approval_timeout_s
        while True:
            r = await t.cgi("accctrl", acc_type, acc_v1, acc_v2)
            if r.text.startswith("ok,"):
                break
            if r.text.startswith("ok_under_research"):
                hint = "" if "no_msg" in r.text else " (approve the connection on the camera)"
                self._set_status("waiting_approval", "camera is registering this device" + hint)
            else:
                self._set_status("connecting", "req_acc -> %s" % r.text)
            if time.monotonic() > deadline:
                raise CamError("access not granted: %s" % r.text)
            await asyncio.sleep(1.0)
        self.log_event("conn", "access granted: %s" % r.text)
        if acc_type == "req_acc_e":
            # LUMIX Sync, encrypted bodies: device_name before the SUBSCRIBE
            await t.cgi("setsetting", "device_name", self.cfg.device_name)
        # 3. event subscription, right after the accept (moves the S5 off "Connecting")
        self._mod_play.clear()
        sub_at = time.monotonic()
        await self._subscribe_safely(ip)
        await asyncio.sleep(0.2)
        # 4. read-only setup, as LUMIX Sync does it
        if q.raw_img_send:
            await t.cgi("setsetting", "raw_img_send", "enable")
        cap = await t.cgi("getinfo", "capability")
        self.capability = _capability_dict(cap)
        await self._load_menu()
        await self.refresh_lens()
        tt = await t.cgi("getsetting", "touch_type")
        self.touch_type = tt.setting_value("touch_type")
        await self._refresh_state(strict=True)
        await t.cgi("setsetting", "device_name", self.cfg.device_name)
        self._last_ok = time.monotonic()
        # 5. The SUBSCRIBE makes the S5 run its own switch into play mode, which
        # ends with a mod_Play NOTIFY 5-22 s later. Any camcmd/stopstream sent
        # before that answers err_critical and wedges the camera (seen at 20:41,
        # 21:24, 22:01 and 22:45 on 2026-10-11). LUMIX Sync never sends recmode
        # at connect, so it never hits this window. Wait for mod_Play.
        left = (q.settle_s if q.settle_event else q.settle_min_s) - (time.monotonic() - sub_at)
        if left > 0 and not self._mod_play.is_set():
            self._set_status("connecting", "finishing the connection (the camera needs 5-30 s)")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._mod_play.wait(), left)
        self.log_event("conn", "camera settled after SUBSCRIBE in %.1fs (%s)" % (
            time.monotonic() - sub_at, (q.settle_event or "settle") if self._mod_play.is_set() else "timeout"))
        await self._refresh_state(strict=True)
        # 6. live view start (the only recmode at connect)
        if self.liveview_wanted:
            await self.start_liveview("connect")
        self.connected_at = time.time()
        self.powered_off = False
        self._probe_at = time.monotonic()
        self._save_state(connected_at=int(self.connected_at), provisional=False)
        self._set_status("ready", "%s connected" % self.ddd.get("friendlyName", ip))
        for hook in list(self.ready_hooks):
            self._spawn(hook(self))

    async def _access_values(self, t: CamTransport) -> "tuple[str, str, str]":
        """accctrl type and values for this body: plain req_acc, or LUMIX Sync's
        encrypted req_acc_g (nonce) + req_acc_e when X_CamCapability says so."""
        cid = self.cfg.client_uuid or _client_uuid()
        name = self.cfg.device_name
        if self.profile.handshake_for(self.ddd) == "req_acc_g":
            g = await t.cgi("accctrl", "req_acc_g")
            try:
                nonce = parse_nonce(g.text)
            except ValueError:
                if not self.profile.handshake_fallback:
                    raise CamError("req_acc_g -> %s" % g.text[:40])
                self.log_event("conn", "req_acc_g -> %s; falling back to req_acc" % g.result)
            else:
                return "req_acc_e", encrypt_acc(cid, nonce), encrypt_acc(name, nonce)
        return "req_acc", cid, name

    async def _load_menu(self) -> None:
        assert self.transport
        key = "allmenu-%s-%s.xml" % (self.ddd.get("modelNumber", "x"), self.ddd.get("X_FirmVersion", "x"))
        cache = os.path.join(CONFIG_DIR, "cache", key)
        raw: Optional[bytes] = None
        if os.path.exists(cache):
            with open(cache, "rb") as f:
                raw = f.read()
            # LUMIX Sync always reads allmenu at connect; cheap enough to keep the order
            await self.transport.cgi("getinfo", "allmenu", timeout=20)
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
        """SUBSCRIBE once per session, only while the camera is in play mode.

        On the S5 a SUBSCRIBE while the camera is in *rec* makes it switch
        itself to play; a recmode sent during that switch answers err_critical
        and leaves every later camcmd answering err_busy until a power cycle
        (reproduced 2026-10-11 21:24). A freshly accepted camera sits in play,
        so normally no playmode is needed here.
        """
        assert self.transport
        await self._refresh_state()
        if self.profile.connect.subscribe_only_in_play and self.state.get("cammode") == "rec":
            r = await self.cmd("camcmd", value="playmode")
            for _ in range(10):
                await asyncio.sleep(0.4)
                await self._refresh_state()
                if self.state.get("cammode") == "play":
                    break
            self.log_event("conn", "playmode before SUBSCRIBE -> %s (cammode=%s)"
                           % (r.result, self.state.get("cammode")))
        await self.listener.unsubscribe()
        try:
            sub = await self.listener.subscribe(ip)
            self.log_event("conn", "event subscription: %s" % sub)
        except (OSError, asyncio.TimeoutError) as e:
            self.log_event("warn", "event subscription failed: %s" % e)

    async def cmd(self, mode: str, type: Optional[str] = None, value: Optional[str] = None,
                  value2: Optional[str] = None, timeout: float = 6.0, tries: int = 5,
                  judge_wedge: bool = True) -> Reply:
        """LUMIX Sync retry rule: on err_busy wait 1 s and retry, at most 5 tries,
        then return the last reply. No other command is sent in between.
        ``judge_wedge=False``: a short budget that runs out is not the wedge
        (time-lapse shots use fewer tries and skip instead)."""
        assert self.transport
        r = await self._send(mode, type, value, value2, timeout)
        n = 1
        while r.result == "err_busy" and n < tries and not self.wedged:
            await asyncio.sleep(1.0)
            r = await self._send(mode, type, value, value2, timeout)
            n += 1
        # One camcmd/startstream refused for LUMIX Sync's whole retry budget is the
        # wedge (2026-10-11: capture err_busy x5 never cleared without a power
        # cycle). recmode is judged by ensure_rec(), which also checks cammode.
        if (judge_wedge and n >= tries and r.result in ("err_busy", "err_critical")
                and (mode == "startstream" or (mode == "camcmd" and value not in ("recmode", "poweroff")))
                and self.profile.connect.busy_budget_exhausted_is_wedge):
            self._mark_wedged("%s %s refused %d times" % (mode, value or "", n))
        return r

    async def _send(self, mode: str, type: Optional[str], value: Optional[str],
                    value2: Optional[str], timeout: float) -> Reply:
        """One request; recmode goes through the churn guard (never more than
        RECMODE_MAX in RECMODE_WINDOW_S, whoever asks: retries, watchdog, UI)."""
        assert self.transport
        if mode == "camcmd" and value == "recmode":
            now = time.monotonic()
            recent = [t for t in self._recmode_sent if now - t < self.RECMODE_WINDOW_S]
            if len(recent) >= self.RECMODE_MAX:
                self.log_event("warn", "recmode not sent: %d already in the last %.0f s (wedge guard)"
                               % (len(recent), self.RECMODE_WINDOW_S))
                q = "mode=camcmd&value=recmode"
                return parse_reply(q, 200, b"<camrply><result>%s</result></camrply>" % RECMODE_GUARD.encode(), 0.0)
            self._recmode_sent.append(now)
        return await self.transport.cgi(mode, type, value, value2, timeout=timeout)

    async def ensure_rec(self) -> bool:
        """camcmd recmode (3 x 1 s on err_busy); True when getstate says rec."""
        assert self.transport
        r = await self.cmd("camcmd", value="recmode", tries=self.RECMODE_TRIES)
        for _ in range(8):
            await asyncio.sleep(0.4)
            await self._refresh_state()
            if self.state.get("cammode") == "rec":
                return True
        if r.result not in ("ok",):
            self.log_event("warn", "recmode -> %s" % r.result)
        # A healthy S5 in play always accepts recmode; one that refuses it for the
        # whole retry budget is wedged (2026-10-11 22:46/23:08: err_busy x5 from
        # play, power cycle needed). Flag it now instead of after a second command,
        # so nothing else (watchdog, UI, scripts) keeps sending camcmds.
        if (self.profile.connect.recmode_refused_from_play_is_wedge
                and r.result in ("err_busy", "err_critical")
                and self.state.get("cammode") == "play"):
            self._mark_wedged("recmode from play")
        return False

    async def start_liveview(self, reason: str) -> bool:
        """LUMIX Sync's live-view start, run once per lifecycle event:
        stopstream -> recmode -> startstream (each 5 x 1 s on err_busy)."""
        assert self.transport
        self._lv_restart_at = time.monotonic()
        if self.body_busy or self.wedged:
            self.log_event("lv", "live view start (%s) skipped: camera %s"
                           % (reason, "in use" if self.body_busy else "wedged"))
            return False
        q = self.profile.connect
        if self._lv_running or not q.stopstream_only_if_running:
            # stopstream with no stream running answers err_critical on the S5
            await self.cmd("stopstream")
        await self._refresh_state()
        # recmode only when needed: every recmode makes the camera echo
        # lens_Atta/mod_Rec, and recmode churn is part of how the S5 wedges
        need_rec = self.state.get("cammode") != "rec" or not q.recmode_only_if_needed
        if need_rec and not await self.ensure_rec():
            self._lv_running = False
            self.log_event("warn", "live view start (%s): camera stays in %s"
                           % (reason, self.state.get("cammode")))
            return False
        r = await self.cmd("startstream", value=str(self.cfg.lv_port))
        self._lv_running = r.ok
        self.log_event("lv", "live view start (%s): startstream -> %s" % (reason, r.result))
        return r.ok

    async def stop_liveview(self, reason: str) -> None:
        if self.transport is None or (not self._lv_running
                                      and self.profile.connect.stopstream_only_if_running):
            # stopstream with no stream running answers err_critical on the S5
            # (22:45:23 on 2026-10-11, right before the recmode that wedged it);
            # LUMIX Sync only stops a stream it started
            return
        self._lv_running = False
        with contextlib.suppress(CamError):
            await self.transport.cgi("stopstream", timeout=4)
        self.log_event("lv", "live view stopped (%s)" % reason)

    # ------------------------------------------------------------ supervisor
    async def _refresh_state(self, strict: bool = False) -> None:
        """getstate; with strict=True an err_reject raises (camera dropped us)."""
        assert self.transport
        r = await self.transport.cgi("getstate", timeout=4)
        if r.result == "err_reject" and strict:
            raise Disconnected("getstate -> err_reject (camera dropped the session)")
        if not r.ok:
            return
        self._last_ok = time.monotonic()
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
        """1 Hz getstate. Like LUMIX Sync: err_reject, 3 failed polls in a row or
        30 s without a good reply = disconnected (full reconnect)."""
        assert self.transport
        fails = 0
        tick = 0
        last_menu = time.monotonic()
        self._last_ok = time.monotonic()
        while True:
            await asyncio.sleep(self.cfg.keepalive_s)
            tick += 1
            if self._reconnect_req.is_set():
                raise Disconnected("reconnect requested")
            try:
                await self._refresh_state(strict=True)
                fails = 0
            except CamError as e:
                fails += 1
                if fails >= 3:
                    raise Disconnected("keepalive failed 3x: %s" % e)
                continue
            if time.monotonic() - self._last_ok > 30:
                raise Disconnected("no good getstate for 30 s")
            # getstate answers even without a session (camera rebooted between two
            # polls): check the session now and then, sooner while frames are missing
            age = self.liveview.age
            every = (self.PROBE_STALE_S if (self.liveview_wanted and (age is None or age > self.PROBE_FRAME_AGE_S))
                     else self.PROBE_EVERY_S)
            if time.monotonic() - self._probe_at > every and not self._op_lock.locked():
                self._probe_at = time.monotonic()
                await self._probe_session()
                if self._reconnect_req.is_set():
                    raise Disconnected("camera dropped the session")
            if (self._lv_pending and not self._op_lock.locked()
                    and time.monotonic() - self._lv_restart_at > 5):
                reason, self._lv_pending = self._lv_pending, None
                if self.liveview_wanted:
                    try:
                        async with self.exclusive("liveview"):
                            await self.start_liveview(reason)
                    except Busy:
                        self._lv_pending = reason
            try:
                if (self.state.get("cammode") == "rec" and not self.body_busy
                        and tick % 3 == 0):
                    await self.refresh_exposure()
                if self.listener.needs_renew():
                    rr = await self.listener.renew(
                        fallback=not self.profile.connect.renew_by_sid_only)
                    if rr.get("status") != 200:
                        # LUMIX Sync never re-subscribes a live session: tear down
                        raise Disconnected("subscription renew refused: %s" % rr)
                if self._menu_dirty.is_set() or time.monotonic() - last_menu > 60:
                    self._menu_dirty.clear()
                    last_menu = time.monotonic()
                    if not self._op_lock.locked() and not self.body_busy:
                        await self.refresh_menu()
                await self._liveview_watchdog()
            except CamError as e:
                log.warning("supervisor step failed: %s", e)

    async def _liveview_watchdog(self) -> None:
        """No periodic recmode/startstream (that fed the err_busy wedge). One
        restart attempt, with growing back-off, only when the camera is in rec,
        not in use and not wedged; a camera-side switch to play is respected."""
        if (not self.liveview_wanted or self._op_lock.locked() or self.body_busy
                or self.wedged or self.state.get("cammode") != "rec"):
            return
        if self.interval_active:
            # the body runs its own interval shooting: recmode/startstream answer
            # err_busy until it ends, so a restart would only hold the camera lock
            return
        age = self.liveview.age
        stale = age is None or age > 5
        if not stale:
            self._lv_backoff = 10.0
            return
        if time.monotonic() - self._lv_restart_at < self._lv_backoff:
            return
        self._lv_backoff = min(self._lv_backoff * 2, 120.0)
        async with self.exclusive("liveview"):
            await self.start_liveview("watchdog, no frames for %ss" % (round(age) if age else "?"))

    async def _on_notify(self, ev: Dict[str, Any]) -> None:
        """X_Panasonic_Cam_Sync handling as in LUMIX Sync (flow doc section 6)."""
        for p in ev.get("props", []):
            name, value = p["name"], p["value"]
            if name in ("SourceProtocolInfo", "SinkProtocolInfo"):
                continue
            self.log_event("notify", "%s=%s" % (name, value), name=name, value=value)
            if name != "X_Panasonic_Cam_Sync":
                continue
            self._menu_dirty.set()
            if value in ("busy", "busy_no_msg"):
                if not self.body_busy:
                    self.body_busy = True
                    self.bus.publish("status", self.status_dict())
                    if self._lv_running:
                        self._spawn(self.stop_liveview("camera in use"))
            elif value == "update":
                self._busy_ended = self.body_busy
                self.body_busy = False
                self.bus.publish("status", self.status_dict())
            elif value == "lens_Atta":
                # After a busy window lens_Atta means "camera usable again": restart
                # live view once. Outside a busy window it is just the camera's echo
                # of our own recmode; reacting to it would loop recmode -> lens_Atta
                # -> recmode, which wedged the S5 (2026-10-11 22:01).
                if (self.body_busy or self._busy_ended
                        or not self.profile.connect.lens_atta_restart_only_after_busy):
                    self._busy_ended = False
                    if self.liveview_wanted:
                        self._lv_pending = "lens_Atta after camera use"
                self.body_busy = False
                self.bus.publish("status", self.status_dict())
            elif value == "mod_Play":
                self._lv_pending = None   # never pull the camera back to rec
                self._lv_running = False
                self._mod_play.set()
            # mod_Play: ignored on purpose (never pull the camera back to rec)
            if value.startswith("lens") and self.transport:
                self._spawn(self.refresh_lens())

    @staticmethod
    async def _safe(coro) -> None:
        with contextlib.suppress(Exception):
            await coro

    def _spawn(self, coro) -> "asyncio.Task":
        """Run ``coro`` in the background (errors swallowed), tracked in _bg."""
        t = asyncio.ensure_future(self._safe(coro))
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    async def _cancel_background(self) -> None:
        tasks = [t for t in self._bg if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._bg.clear()

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
    def _check_usable(self) -> None:
        if self.body_busy:
            raise Busy("camera is being operated on the body (waiting for it to finish)")
        if self.wedged:
            raise Busy("camera is stuck answering err_busy; switch it off and on again")
        if self.interval_active:
            raise Busy("an in-camera interval run is in progress (stop the timelapse first)")

    async def capture(self) -> Dict[str, Any]:
        async with self.exclusive("capture") as t:
            before = self.state.get("remaincapacity")
            self._check_usable()
            r = await self.cmd("camcmd", value="capture", timeout=15)
            self.log_event("cmd", "capture -> %s" % r.result)
            # the camera needs a moment to write; watch remaincapacity / sd_access
            changed = False
            for _ in range(8):
                await asyncio.sleep(0.5)
                await self._refresh_state()
                if self.state.get("remaincapacity") != before:
                    changed = True
                    break
            return {"result": r.result, "remain_before": before,
                    "remain_after": self.state.get("remaincapacity"), "count_changed": changed}

    async def video(self, start: bool) -> Dict[str, Any]:
        async with self.exclusive("video") as t:
            if start:
                self._check_usable()
            r = await self.cmd("camcmd", value="video_recstart" if start else "video_recstop", timeout=10)
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
            r2 = await t.cgi("camctrl", "touch", "0/0", "off")  # LUMIX Sync always releases at 0/0
            self.log_event("cmd", "touch AF %d/%d -> %s, release -> %s" % (x, y, r.result, r2.result))
            return {"result": r.result, "release": r2.result}

    async def oneshot_af(self) -> Dict[str, Any]:
        async with self.exclusive("af") as t:
            self._check_usable()
            r = await self.cmd("camcmd", value="oneshot_af")
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
            r = await self.cmd("camcmd", value=value, timeout=15)
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

    async def stop_recording(self, timeout: float = 20.0) -> Dict[str, Any]:
        """Stop a running video recording and wait for the card write to finish
        (getstate rec off and sd_access off). No-op when not recording."""
        if self.transport is None or self.state.get("rec") != "on":
            return {"stopped": False, "reason": "not recording"}
        async with self.exclusive("quiesce-video", timeout=timeout):
            r = await self.cmd("camcmd", value="video_recstop", timeout=10)
            self.log_event("cmd", "video_recstop (quiesce) -> %s" % r.result)
            deadline = time.monotonic() + timeout
            written = False
            while time.monotonic() < deadline:
                await asyncio.sleep(0.4)
                with contextlib.suppress(CamError, OSError, asyncio.TimeoutError):
                    await self._refresh_state()
                if self.state.get("rec") != "on" and self.state.get("sd_access") != "on":
                    written = True
                    break
            return {"stopped": r.ok, "result": r.result, "card_written": written}

    async def unsubscribe_events(self) -> bool:
        """UNSUBSCRIBE the GENA subscription (before power-off / shutdown). The
        session keeps working for cam.cgi, but without events; LUMIX Sync never
        re-subscribes a live session, so only a reconnect brings them back."""
        had = bool(self.listener.sid)
        with contextlib.suppress(Exception):
            await self.listener.unsubscribe()
        if had:
            self.log_event("conn", "event subscription cancelled (UNSUBSCRIBE)")
        return had

    async def poweroff(self) -> Dict[str, Any]:
        async with self.exclusive("poweroff") as t:
            if (self.profile.connect.poweroff_needs_rec and self.state.get("cammode") == "play"
                    and not self.wedged):
                # the S5 answers err_reject to poweroff in play mode
                await self.ensure_rec()
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
            return self.store.load()
        except Exception:
            log.exception("camera store load failed")
            return {}

    def _save_state(self, **kw: Any) -> None:
        try:
            self.store.save(**kw)
        except Exception:
            log.exception("camera store save failed")


def _friendly_error(e: BaseException, ip: Optional[str]) -> str:
    """A connection error as a sentence (the raw text stays in the server log)."""
    log.info("connection error: %r", e)
    text = str(e)
    if (isinstance(e, asyncio.TimeoutError) or "timeout" in type(e).__name__.lower()
            or text.startswith(("TimeoutError", "ServerTimeoutError"))):
        return "camera at %s does not answer (asleep, off or out of range)" % (ip or "?")
    if ("Connect call failed" in text or "Cannot connect" in text
            or "refused" in text.lower() or "unreachable" in text.lower()):
        return "camera at %s is not reachable (off, asleep or on another network)" % (ip or "?")
    return text or type(e).__name__


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
