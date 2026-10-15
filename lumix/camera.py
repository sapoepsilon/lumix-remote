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
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Deque, Dict, Iterator, List, Optional, Set

from . import discovery, exposure, models
from .events import EventListener
from .liveview import LiveViewReceiver
from .menu import MenuModel
from .models import ModelProfile
from .protocol import (GUARD_RESULT, MODE_SWITCHES, RATE_LIMITED, CamError, CamTransport,
                       ModeSwitchLimiter, Reply, encrypt_acc, parse_ddd, parse_nonce,
                       parse_state)
from .registry import JsonStore

log = logging.getLogger("lumix.camera")

CONFIG_DIR = os.path.expanduser(os.environ.get("LUMIX_REMOTE_HOME", "~/.config/lumix-remote"))
SUBSCRIBE_SETTLE_S = 30.0      # default max wait for the post-SUBSCRIBE mod_Play (see models)
LUMIX_SYNC_UA = "LUMIX Sync"   # User-Agent LUMIX Sync sends on every cam.cgi request
SESSION_PROBE = "mode=getsetting&type=touch_type"   # needs an accepted session
MID_OPERATION = GUARD_RESULT                         # request withheld by the mid-operation guard
# getstate interval_status while an in-camera interval run is going. Real DC-S5
# fw 2.03: rec (running) and pause (tests/fixtures/real-s5-interval-2026-10-12.log);
# "run" is LUMIX Sync's name for other bodies (docs/lumix-sync-wifi-flow.md s10).
INTERVAL_RUNNING = ("run", "rec", "pause")
# A read-only connect completes (SUBSCRIBE, live view) once the camera has been
# idle this long (interval_status not rec/pause/off, not recording, not busy).
READONLY_CLEAR_S = 10.0
# read-only connect reason for a body that got stuck and never went away since
WEDGED_READONLY = ("the camera is stuck (it ignores shutter, mode and live view commands); "
                   "switch it off and on to clear it")


class Disconnected(Exception):
    pass


def _camera_went_away(e: BaseException) -> bool:
    """The camera stopped answering (not: we hung up, it rejected the session, a
    local rate limit). Only after that can a wedged body be un-wedged."""
    if isinstance(e, Disconnected):
        text = str(e)
        return text.startswith("keepalive failed") or text.startswith("no good getstate")
    return True     # CamError / OSError / timeout: no answer at all


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
    # LUMIX Sync-style id, the same one the BLE helper writes (see registry).
    from .registry import default_client_uuid
    return default_client_uuid()


def parse_lv_quality_options(curmenu: str) -> List[str]:
    """Live view sizes a curmenu offers (``menu_item_id_liveviewsize_<v>`` with
    enable="yes"), in the camera's order (the S5: vga, qvga)."""
    return re.findall(r'id="menu_item_id_liveviewsize_([A-Za-z0-9_]+)"\s+enable="yes"', curmenu)


class Camera:
    # session probe (getsetting) cadence: while live view frames are missing / otherwise
    PROBE_STALE_S = 15.0
    PROBE_EVERY_S = 60.0
    PROBE_FRAME_AGE_S = 10.0       # live view counts as missing after this
    # Mode-switch rate limit (recmode/playmode/pictmode), enforced in CamTransport
    # for every caller: the 4th recmode within 20 s wedged the S5 (FINDINGS 22:01),
    # four in 4 min while it kept dropping to playback did too (2026-10-12 16:52).
    MODE_SWITCH_MIN_GAP_S = 10.0
    MODE_SWITCH_MAX = 3
    MODE_SWITCH_WINDOW_S = 60.0
    MODE_SWITCH_MAX_WAIT_S = 15.0  # cmd() waits this long at most for a free slot
    RECMODE_TRIES = 2              # recmode err_busy retry (the retry waits for the limiter)
    # startstream only once the camera has been in rec, with no mode/lens event,
    # for this long: real S5 2026-10-12 startstream 0.15 s / 1.2 s after its switch
    # to rec -> err_busy, 2.45 s / 2.65 s after -> ok (camera log lines 9495-9496,
    # 9514-9515); 16:46:58 / 16:50:57 it was sent into the camera's own bounce
    # back to play (lines 9465-9466, 9527-9529).
    STABLE_S = 2.0
    # How long a taken-but-unwritten shutter blocks rec/play switches. The S5 held
    # one for 56 s (2026-10-15 09:02:11 -> 09:03:07); after this the guard lets go
    # (not verified on the body whether a switch is safe then).
    CAPTURE_PENDING_MAX_S = 120.0
    # After X_Panasonic_Cam_PRec=err_*, the body drops to playback on its own
    # (2026-10-12 16:47-16:50); a playmode of ours 5 s after one answered
    # err_critical and wedged it (2026-10-15 09:03:07 -> 09:03:12).
    PHOTO_ERROR_SETTLE_S = 10.0
    STABLE_WAIT_S = 8.0            # start_liveview waits this long for a settled rec
    # Automatic return to rec (time-lapse) when the camera left rec on its own:
    # wait (reads only) this long for it to come back by itself, then one recmode;
    # at most AUTO_REC_MAX such recmodes per AUTO_REC_WINDOW_S, then give up.
    AUTO_REC_WAIT_S = 20.0
    AUTO_REC_MAX = 2
    AUTO_REC_WINDOW_S = 300.0
    LV_QUALITY_GAP_S = 2.0         # pause between setsetting liveviewsize and the stream restart

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
        # live view size (setsetting liveviewsize): what the camera reports and the
        # values its curmenu offers (menu_item_id_liveviewsize_*, best first)
        self.lv_quality: Optional[str] = None
        self.lv_quality_options: List[str] = []
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
        self._lv_pending_after = 0.0             # ... not before this (monotonic)
        self._lv_pending_cap: Optional[float] = None  # set: a cap-held recmode retry (scheduled at)
        self._last_recmode = ""                  # result of ensure_rec()'s last recmode
        # video starts on their way (API handler or video(True)): a stop waits for
        # them instead of answering "was not recording" from a stale getstate
        self._video_starts = 0
        self._video_started = asyncio.Event()
        self._video_started.set()
        self._lv_backoff = 10.0
        self._busy_ended = False
        self._last_ok = time.monotonic()
        self._mod_play = asyncio.Event()   # set by the mod_Play NOTIFY
        self._reconnect_req = asyncio.Event()
        # Address of the handshake in progress (None outside _connect): a
        # reconnect request for the same camera during it is already satisfied.
        self._connecting_ip: Optional[str] = None
        # True between a handshake's SUBSCRIBE and the camera's mod_Play (or the
        # settle timeout): the S5's own switch window, see _pre_send
        self._settle_open = False
        self._subscribed_at = float("-inf")     # monotonic time of the handshake's SUBSCRIBE
        # (type, value) of an accctrl request the camera is still researching:
        # stop() cancels it with req_acc_can, as LUMIX Sync does.
        self._acc_waiting: Optional[tuple] = None
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
        # last cammode change (getstate) and last mode/lens event (NOTIFY or getstate)
        self._mode_changed_at = float("-inf")
        self._unsettled_at = float("-inf")
        self._auto_recmodes: Deque[float] = deque(maxlen=8)   # auto_rec() recmodes (monotonic)
        self._auto_rec_sent_at: Optional[float] = None         # auto_rec()'s recmode in this episode
        # last X_Panasonic_Cam_PRec error NOTIFY: (monotonic, value), e.g. err_rec
        self.photo_error: Optional[tuple] = None
        # monotonic time of a shutter the camera took ("capture -> ok") with no photo
        # written yet (remaincapacity unchanged, no X_Panasonic_Cam_PRec error).
        # 2026-10-15 09:02:11 the S5 sat like that for 56 s; the file browser's
        # stopstream then aborted it (err_rec) and its playmode 5 s later answered
        # err_critical: wedged until a power cycle.
        self.capture_pending_at: Optional[float] = None
        # True once the camera stopped answering after a wedge (switched off, out of
        # Wi-Fi): only then can a reconnect find it un-wedged. A reconnect to a body
        # that never went away keeps the wedge (2026-10-15 09:08:41: the reconnect after
        # a refused poweroff cleared it and sent playmode -> err_busy x3 into it).
        self._gone_since_wedge = False
        self._reject_probe_at = float("-inf")  # last session probe after an err_reject (monotonic may start near 0)
        self._state_at = float("-inf")     # monotonic time of the last good getstate
        # Set (to the reason) when the connect found the camera mid-operation (an
        # in-camera interval run, a video recording, the body in use) and stopped
        # after the read-only part: no playmode/recmode, no SUBSCRIBE, no
        # setsetting, no live view until the camera is idle again.
        self.readonly: Optional[str] = None
        self._readonly_idle_since: Optional[float] = None

    @property
    def interval_active(self) -> bool:
        return self.interval_owner is not None or self.interval_running

    @property
    def interval_running(self) -> bool:
        """getstate says the body runs (or holds paused) its own interval run."""
        return self.state.get("interval_status") in INTERVAL_RUNNING   # S5 running = rec

    def interval_info(self) -> Dict[str, Any]:
        st = self.state
        try:
            remain: Optional[int] = int(st.get("interval_remain_num") or "")
        except ValueError:
            remain = None
        return {"status": st.get("interval_status"), "running": self.interval_running,
                "remain": remain if self.interval_running else None,
                "end_time": st.get("interval_end_time") if self.interval_running else None}

    # ------------------------------------------------- mid-operation guard
    # 2026-10-12 11:55 (real S5, tests/fixtures/real-s5-interval-restart-incident-2026-10-12.log): an in-camera interval
    # run was paused by the old quiesce, the reconnect sent camcmd playmode (ok),
    # recmode 34 s later -> err_critical, then err_busy for good; the job resume's
    # setsetting interval_shoot_interval -> err_critical. Only a power cycle
    # recovered it. Mode switches while the body is busy with its own operation
    # are the known wedge trigger, so one guard sits in front of the transport.
    def mid_operation(self) -> Optional[str]:
        """Why the camera must not be switched between rec/play right now (None = idle)."""
        st = self.state
        iv = st.get("interval_status")
        if iv in INTERVAL_RUNNING:
            rem = st.get("interval_remain_num")
            return "an in-camera interval run is %s%s" % (
                "paused on the camera" if iv == "pause" else "in progress",
                " (%s shots left)" % rem if rem else "")
        if st.get("rec") == "on":
            return "the camera is recording video"
        if self.body_busy:
            return "the camera is being operated on the body"
        now = time.monotonic()
        if self.capture_pending_at is not None:
            age = now - self.capture_pending_at
            if age < self.CAPTURE_PENDING_MAX_S:
                return ("the camera is still taking a photo (shutter accepted %.0f s ago, nothing written "
                        "yet; check the camera for a self-timer or focus wait)" % age)
            self.capture_pending_at = None
        if self.photo_error is not None and now - self.photo_error[0] < self.PHOTO_ERROR_SETTLE_S:
            return ("the camera just reported a photo error (%s) and is switching itself; wait %.0f s"
                    % (self.photo_error[1], self.PHOTO_ERROR_SETTLE_S - (now - self.photo_error[0])))
        return None

    async def fresh_state(self, max_age: float = 1.5) -> None:
        """getstate unless the last good one is younger than ``max_age`` s."""
        if self.transport is None or time.monotonic() - self._state_at < max_age:
            return
        with contextlib.suppress(CamError, OSError, asyncio.TimeoutError):
            await self._refresh_state()

    async def mode_switch_blocked(self) -> Optional[str]:
        """Fresh-state check before a rec/play switch: the reason it must not happen."""
        await self.fresh_state(0.0)
        return self.mid_operation()

    async def interval_blocked(self) -> Optional[str]:
        """Reason any write (setsetting, shutter, startstream) must wait: an
        in-camera interval run is going (rec or pause). Re-reads a stale state,
        and a cached 'running' once more before refusing (a run that just ended)."""
        await self.fresh_state(0.0 if self.interval_running else 1.5)
        return self.mid_operation() if self.interval_running else None

    def rec_settled_in(self) -> Optional[float]:
        """Seconds until the camera counts as settled in rec (0 = now), None = not in rec."""
        if self.state.get("cammode") != "rec":
            return None
        return max(0.0, self.STABLE_S - (time.monotonic() - self._unsettled_at))

    def stream_blocked(self) -> Optional[str]:
        """Why startstream must not go out now (None = fine): the camera is not in
        rec, changed mode / sent a mode or lens event less than STABLE_S ago, or
        reports busy."""
        if self.body_busy:
            return "the camera reports busy"
        left = self.rec_settled_in()
        if left is None:
            return "the camera is in %s, not rec" % (self.state.get("cammode") or "an unknown mode")
        if left > 0:
            return "the camera changed mode %.1f s ago (startstream needs %.0f s in rec)" % (
                time.monotonic() - self._unsettled_at, self.STABLE_S)
        return None

    async def _pre_send(self, mode: str, typ: Optional[str], value: Optional[str]) -> Optional[str]:
        """CamTransport guard: veto requests that could wedge a busy body.

        * camcmd recmode/playmode/pictmode: never while mid_operation() (interval
          run rec/pause, video recording, body busy). State is re-read first.
        * any other camcmd (except poweroff), setsetting, startstream: never during
          an in-camera interval run. The shutter during a run restarts it with the
          full image count (real S5 11:38:17); setsetting during a paused run after
          a mode switch answered err_critical (11:56:33). Not verified on the body:
          whether setsetting/startstream alone (no mode switch) harm a running run;
          they are withheld anyway.
        * startstream: only in a settled rec (``stream_blocked``).
        * camcmd/stopstream: never between a handshake's SUBSCRIBE and its
          settle (``_settle_open``; err_critical + wedge on the S5).
        getstate/getinfo/getsetting/accctrl/stopstream/camctrl (interval
        pause|restart|stop, touch) always pass. The mode-switch rate limit
        sits in CamTransport itself (``ModeSwitchLimiter``)."""
        if self._settle_open and mode in ("camcmd", "stopstream"):
            # chaos seed 14 (tests/test_chaos.py): a recmode that was waiting for the
            # mode-switch cap went out inside a reconnect's switch window -> wedge
            why = "the camera is finishing the connection (its own switch after SUBSCRIBE)"
        elif mode == "camcmd" and value in MODE_SWITCHES:
            why = await self.mode_switch_blocked()
        elif (mode == "camcmd" and value != "poweroff") or mode in ("setsetting", "startstream"):
            why = await self.interval_blocked()
            if why is None and mode == "startstream":
                why = self.stream_blocked()
        else:
            return None
        if why:
            what = "%s %s" % (mode, value if mode == "camcmd" else typ or value or "")
            self.log_event("warn", "%s not sent: %s" % (what.strip(), why), guard=True)
        return why

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

    def _trace_refused(self, q: str, why: str) -> None:
        """A request the guard or the rate limiter kept back: one line in the
        camera log (it never reached the camera)."""
        if self._log_fh is None:
            return
        self._log_fh.write("[%s] NOT SENT /cam.cgi?%s (%s)\n" % (
            dt.datetime.now().strftime("%H:%M:%S.%f")[:-3], q, why))
        self._log_fh.flush()

    def _trace_note(self, text: str) -> None:
        if self._log_fh is None:
            return
        self._log_fh.write("[%s] %s\n" % (dt.datetime.now().strftime("%H:%M:%S.%f")[:-3], text))
        self._log_fh.flush()

    def _trace_error(self, q: str, err: str) -> None:
        if self._log_fh is None:
            return
        self._log_fh.write("[%s] GET /cam.cgi?%s -> no reply (%s)\n" % (
            dt.datetime.now().strftime("%H:%M:%S.%f")[:-3], q, err.rstrip(": ")))
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
        if rep.result == "err_critical" and ("value=playmode" in q or "value=recmode" in q):
            # every camcmd play/recmode -> err_critical on record was followed by err_busy
            # until a power cycle: 2026-10-11 22:01:18 and 22:45:28, 2026-10-12 11:56:17,
            # 2026-10-15 09:03:12 (playmode; the oneshot_af at 09:08:33 then err_busy x5)
            self._mark_wedged("camcmd %s -> err_critical" % ("playmode" if "value=playmode" in q
                                                               else "recmode"))
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
        self._gone_since_wedge = False
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
            "liveview_quality": self.lv_quality_info(),
            "powered_off": self.powered_off,
            "wedged": self.wedged, "wedged_since": self.wedged_since, "body_busy": self.body_busy,
            "mid_operation": self.mid_operation(), "readonly": self.readonly,
            "interval": self.interval_info(),
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
        await self._cancel_access_request()
        if self.transport is not None:
            was_streaming, self._lv_running = self._lv_running, False   # before the await
            if was_streaming or not self.profile.connect.stopstream_only_if_running:
                # stopstream with no stream answers err_critical on the S5
                with contextlib.suppress(Exception):
                    await self.transport.cgi("stopstream", timeout=2)
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
                if self.wedged and _camera_went_away(e):
                    self._gone_since_wedge = True    # off / off the network: a power cycle may follow
                self._set_status("reconnecting", msg)
                self.log_event("conn", "connection lost: %s" % msg)
                if failures >= 3 and not self.ip_fixed:
                    self.ip = None  # rediscover; DHCP may have moved it
                await self._pause(min(2 * failures, 10))
            except Exception as e:  # never let the supervisor die
                log.exception("unexpected error")
                self._set_status("error", repr(e))
                await asyncio.sleep(5)

    def request_reconnect(self, ip: Optional[str] = None, forget: bool = False) -> bool:
        """Drop the session and connect again (to ``ip`` if given; ``forget``
        clears the remembered camera so discovery starts from scratch).

        While a handshake to the same camera is already running the request is
        dropped (returns False): it used to stay latched and tear the brand-new
        session down ~1 s after "ready", re-running req_acc, SUBSCRIBE and the
        playmode/recmode switch that wedges the S5 (2026-10-12 09:23-09:25:
        three needless full reconnects in a row). One connect = one handshake."""
        if self._connecting_ip is not None and not forget and (not ip or ip == self._connecting_ip):
            self.log_event("conn", "reconnect request ignored: already connecting to %s"
                           % self._connecting_ip)
            return False
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
        return True

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
        # Requests made before this handshake started are satisfied by it; a
        # leftover flag would drop the session right after "ready".
        self._reconnect_req.clear()
        self._acc_waiting = None
        self._connecting_ip = ip
        try:
            await self._handshake(ip)
        finally:
            self._connecting_ip = None

    async def _handshake(self, ip: str) -> None:
        """Connect in LUMIX Sync's order (docs/lumix-sync-wifi-flow.md section 4):
        ddd, req_acc (1 s loop), SUBSCRIBE, 200 ms, raw_img_send, capability,
        allmenu, curmenu, lens, touch_type, getstate, device_name; then the
        live-view start (stopstream, recmode, startstream) once."""
        # powered_off stays set until the camera accepts us again: every failed
        # attempt before that still means "the camera is off", not "out of range"
        self._set_status("connecting", "contacting %s" % ip)
        if self.transport is None or self.transport.ip != ip:
            if self.transport is not None:
                await self.transport.close()
            self.transport = CamTransport(
                ip, user_agent=LUMIX_SYNC_UA, trace=self._trace, port=self.cfg.http_port,
                guard=self._pre_send, trace_error=self._trace_error, refused=self._trace_refused,
                limiter=self._mode_switch_limiter())
        t = self.transport
        self.liveview.camera_ip = ip
        self._settle_open = False
        self.busy_streak = 0
        self.body_busy = False
        self.capture_pending_at = None
        self._lv_running = False
        self.readonly, self._readonly_idle_since = None, None
        # 1. device description (also proves the camera is there)
        try:
            raw = await t.get_raw(self.cfg.upnp_port, discovery.DDD_PATH, timeout=6)
        except BaseException:
            if self.wedged:
                self._gone_since_wedge = True    # off / off the network: a power cycle may follow
            raise
        if self.wedged and self._gone_since_wedge:
            self.log_event("conn", "camera was away since it got stuck; assuming it was switched off and on")
            self.wedged, self.wedged_since = False, None
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
                self._acc_waiting = None
                break
            if r.text.startswith("ok_under_research"):
                self._acc_waiting = (acc_type, acc_v1)
                hint = "" if "no_msg" in r.text else " (approve the connection on the camera)"
                self._set_status("waiting_approval", "camera is registering this device" + hint)
            else:
                self._acc_waiting = None
                self._set_status("connecting", "req_acc -> %s" % r.text)
            if time.monotonic() > deadline:
                await self._cancel_access_request()
                raise CamError("access not granted: %s" % r.text)
            await asyncio.sleep(1.0)
        self.log_event("conn", "access granted: %s" % r.text)
        # it answered req_acc, so it is on: a failure from here on is not "powered off"
        self.powered_off = False
        # 2b. Read the camera's state before anything can change its mode. A body
        # that is mid-operation (in-camera interval run, video recording) gets a
        # read-only connect: no device_name/raw_img_send, no SUBSCRIBE (in rec it
        # makes the S5 switch itself to play, FINDINGS 21:24), no playmode/recmode,
        # no live view. 2026-10-12 11:55:43: the old connect sent playmode into a
        # paused interval run and the recmode after it wedged the camera.
        await self._refresh_state(strict=True)
        busy = self.mid_operation()
        if self.wedged and not busy:
            # same body, never off the network since it got stuck: a reconnect doesn't
            # clear the wedge, and a playmode/SUBSCRIBE into it only collects err_busy
            busy = WEDGED_READONLY
        if busy:
            await self._readonly_setup(t, busy)
            return
        if acc_type == "req_acc_e":
            # LUMIX Sync, encrypted bodies: device_name before the SUBSCRIBE
            await t.cgi("setsetting", "device_name", self.cfg.device_name)
        await self._full_setup(ip)

    async def _full_setup(self, ip: str, deferred: bool = False) -> None:
        """Steps 3-6 of the connect (LUMIX Sync order): SUBSCRIBE, read-only
        setup, settle, the one live-view start; then "ready". Also completes a
        read-only connect once the camera is idle (``_complete_readonly``)."""
        t = self.transport
        assert t is not None
        q = self.profile.connect
        # 3. event subscription, right after the accept (moves the S5 off "Connecting")
        self._mod_play.clear()
        sub_at = time.monotonic()
        await self._subscribe_safely(ip)
        # the camera's switch window opens at the SUBSCRIBE, not before the playmode
        # (which may wait up to MODE_SWITCH_MAX_WAIT_S for the cap): chaos seeds
        # 206/207, tests/test_chaos.py SettleCountsFromTheSubscribeTest
        sub_at = max(sub_at, self._subscribed_at)
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
            if not deferred:
                self._set_status("connecting", "finishing the connection (the camera needs 5-30 s)")
            await self._settle_wait(left)
        self._settle_open = False
        self.log_event("conn", "camera settled after SUBSCRIBE in %.1fs (%s)" % (
            time.monotonic() - sub_at, (q.settle_event or "settle") if self._mod_play.is_set() else "timeout"))
        await self._refresh_state(strict=True)
        self.readonly, self._readonly_idle_since = None, None
        # 6. live view start (the only recmode at connect), at the saved size
        if self.liveview_wanted:
            await self._apply_lv_quality()
            await self.start_liveview("connect")
        self._probe_at = time.monotonic()
        if deferred:
            # same session (connected_at and the ready hooks belong to the read-only connect)
            self._set_status("ready", "%s connected" % self.ddd.get("friendlyName", ip))
            return
        self.connected_at = time.time()
        self.powered_off = False
        self._save_state(connected_at=int(self.connected_at), provisional=False)
        self._set_status("ready", "%s connected" % self.ddd.get("friendlyName", ip))
        for hook in list(self.ready_hooks):
            self._spawn(hook(self))

    async def _settle_wait(self, left: float) -> None:
        """Wait up to ``left`` s for the post-SUBSCRIBE mod_Play, polling getstate
        at the keepalive rate meanwhile: LUMIX Sync starts its 1 Hz CameraWatchLoop
        right after device_name and never leaves the camera without a request.
        A camera woken over Bluetooth with its switch OFF waits in a standby (play,
        lens unpowered) and goes back to sleep when nothing reaches it for ~30 s: on 2026-10-12 14:30:12-14:30:41 we sent nothing
        while waiting (no mod_Play came; the body was already in play), the getstate
        after the wait got no reply and the camera was asleep again
        (tests/fixtures/real-s5-ble-wake-settle-2026-10-12.log). getstate is a read
        and safe inside the switch window; only camcmd/stopstream wedge there.
        err_reject or 3 failed polls in a row = disconnected, as in the supervisor."""
        deadline = time.monotonic() + left
        fails = 0
        while not self._mod_play.is_set():
            rem = deadline - time.monotonic()
            if rem <= 0:
                return
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._mod_play.wait(), min(rem, self.cfg.keepalive_s))
            if self._mod_play.is_set() or deadline - time.monotonic() <= 0:
                return
            try:
                await self._refresh_state(strict=True)
                fails = 0
            except CamError as e:
                fails += 1
                if fails >= 3:
                    raise Disconnected("keepalive failed 3x while finishing the connection: %s" % e)

    async def _readonly_setup(self, t: CamTransport, why: str) -> None:
        """Connect without touching the camera's mode: only reads (capability,
        allmenu, curmenu, lens, touch_type, getstate; the real S5 answered
        curmenu/touch_type/focal/shtrspeed/getstate normally during a running
        interval run, server camera log 11:46-11:55, before the restart). The supervisor
        finishes the connect (``_complete_readonly``) when the camera is idle."""
        self.readonly, self._readonly_idle_since = why, None
        self.log_event("conn", "camera is mid-operation (%s): connected read-only; no mode switch, "
                               "event subscription or live view until it is done" % why)
        cap = await t.cgi("getinfo", "capability")
        self.capability = _capability_dict(cap)
        await self._load_menu()
        await self.refresh_lens()
        tt = await t.cgi("getsetting", "touch_type")
        self.touch_type = tt.setting_value("touch_type")
        await self._refresh_state(strict=True)
        self._last_ok = time.monotonic()
        self.connected_at = time.time()
        self.powered_off = False
        self._probe_at = time.monotonic()
        self._save_state(connected_at=int(self.connected_at), provisional=False)
        self._set_status("ready", "%s connected read-only: %s" % (self.ddd.get("friendlyName", t.ip), why))
        for hook in list(self.ready_hooks):
            self._spawn(hook(self))

    async def _complete_readonly(self) -> None:
        """Supervisor tick: finish a read-only connect once the camera has been
        idle for READONLY_CLEAR_S (interval run over: not rec/pause and not the
        ~5 s 'off' finish window). Not verified on the body yet: the SUBSCRIBE +
        live view start right after an interval run ends
        (scripts/real-verify-interval-restart.sh checks it)."""
        why = self.mid_operation() or (WEDGED_READONLY if self.wedged else None)
        if why or self.state.get("interval_status") == "off":
            if why and why != self.readonly:
                self.readonly = why
                self._set_status("ready", "%s connected read-only: %s"
                                 % (self.ddd.get("friendlyName", self.ip), why))
            self._readonly_idle_since = None
            return
        now = time.monotonic()
        if self._readonly_idle_since is None:
            self._readonly_idle_since = now
            return
        if now - self._readonly_idle_since < READONLY_CLEAR_S or self._op_lock.locked():
            return
        self.log_event("conn", "camera idle again: finishing the connection (events, live view)")
        async with self.exclusive("connect"):
            if await self.mode_switch_blocked() is None and self.transport is not None:
                await self._full_setup(self.transport.ip, deferred=True)

    async def _cancel_access_request(self) -> None:
        """LUMIX Sync: giving up while the camera still researches our req_acc
        (it may be showing an approve prompt) sends ``accctrl req_acc_can`` so
        the camera does not keep waiting for a controller that is gone."""
        pending, self._acc_waiting = self._acc_waiting, None
        if pending is None or self.transport is None:
            return
        with contextlib.suppress(Exception):
            r = await self.transport.cgi("accctrl", "req_acc_can", pending[1], timeout=2)
            self.log_event("conn", "access request cancelled: req_acc_can -> %s" % r.text[:40])

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
        opts = parse_lv_quality_options(r.text)
        if opts:
            self.lv_quality_options = opts
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
            if r.result == RATE_LIMITED and self.state.get("cammode") == "rec":
                # never SUBSCRIBE in rec (the 21:24 trigger); connect again later
                raise Disconnected("playmode before SUBSCRIBE withheld by the mode-switch rate limit")
        # From the SUBSCRIBE to the settle (_full_setup) nothing but reads may reach
        # the camera, also not from an operation that began before this (re)connect
        # (_pre_send)
        self._settle_open = True
        self._subscribed_at = time.monotonic()
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
        """One request. A mode switch waits (at most MODE_SWITCH_MAX_WAIT_S) for
        the transport's rate limiter instead of failing at once (a recmode that
        never comes leaves the camera in playback); beyond that the transport
        refuses it locally (RATE_LIMITED)."""
        assert self.transport
        lim = getattr(self.transport, "limiter", None)
        if mode == "camcmd" and value in MODE_SWITCHES and lim is not None:
            wait = lim.wait_s()
            if 0 < wait <= self.MODE_SWITCH_MAX_WAIT_S:
                self.log_event("lv", "%s in %.0f s (mode-switch rate limit)" % (value, wait))
                await asyncio.sleep(wait)
        r = await self.transport.cgi(mode, type, value, value2, timeout=timeout)
        if r.result == RATE_LIMITED:
            self.log_event("warn", "camcmd %s not sent: mode-switch rate limit (max 1 per %.0f s, "
                                   "%d per %.0f s)" % (value, self.MODE_SWITCH_MIN_GAP_S,
                                                       self.MODE_SWITCH_MAX, self.MODE_SWITCH_WINDOW_S))
        return r

    async def ensure_rec(self) -> bool:
        """camcmd recmode (RECMODE_TRIES on err_busy, the retry waits for the rate limit);
        True when getstate says rec."""
        assert self.transport
        r = await self.cmd("camcmd", value="recmode", tries=self.RECMODE_TRIES)
        self._last_recmode = r.result
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

    def reset_auto_rec(self) -> None:
        """Forget auto_rec()'s recmode budget (an explicit user Resume)."""
        self._auto_recmodes.clear()
        self._auto_rec_sent_at = None

    async def auto_rec(self, who: str = "timelapse") -> "tuple[str, str]":
        """Automatic way back to a usable rec mode, for unattended jobs: never
        fight the camera. Returns ("ready" | "wait" | "give_up", reason).

        Real S5, 2026-10-12 16:46-16:52 (logs/camera-2026-10-12.log 9450-9560,
        NOTIFYs from the server's event list): the body dropped to playback on its
        own (mod_Play 16:46:37, 13 s after a good shot, nothing sent but reads),
        bounced rec -> play -> rec around our recmodes, and every capture after
        that ended in X_Panasonic_Cam_PRec=err_rec + mod_Play with no photo. The
        old time-lapse answered each drop with recmode + startstream (four
        recmodes in 4 min, four startstream err_busy) and the camera wedged for
        good at 16:51:31 (getstate err_busy, capture/req_acc err_critical).

        * in rec: "ready" once settled (STABLE_S with no mode/lens event).
        * left rec: reads only (the supervisor's 1 Hz getstate) for
          AUTO_REC_WAIT_S, in case it comes back by itself; then ONE recmode for
          this episode, at most AUTO_REC_MAX per AUTO_REC_WINDOW_S.
        * still not in rec AUTO_REC_WAIT_S after that recmode, or the budget is
          used up: "give_up" (the caller pauses its job with the reason)."""
        await self.fresh_state(1.0)
        now = time.monotonic()
        mode = self.state.get("cammode")
        if mode == "rec":
            left = self.rec_settled_in() or 0.0
            if self.body_busy:
                return "wait", "camera busy (in use on the body)"
            if left > 0:
                return "wait", "camera just switched to rec (settling, %.1f s)" % left
            return "ready", ""
        if self.wedged:
            return "give_up", "camera stuck: it ignores commands (switch it off and on)"
        since = now - self._mode_changed_at
        sent = self._auto_rec_sent_at
        if sent is not None and sent >= self._mode_changed_at:
            # our one recmode for this episode did not bring it back
            if now - sent >= self.AUTO_REC_WAIT_S:
                return "give_up", ("the camera stays in %s mode %.0f s after one recmode; not "
                                   "sending more (mode switches into a struggling S5 wedge it)"
                                   % (mode, now - sent))
            return "wait", "camera in %s after one recmode %.0f s ago (waiting, nothing sent)" % (
                mode, now - sent)
        if since < self.AUTO_REC_WAIT_S:
            return "wait", ("camera switched to %s mode on its own %.0f s ago; waiting for it "
                            "to come back (nothing sent)" % (mode, since))
        recent = [t for t in self._auto_recmodes if now - t < self.AUTO_REC_WINDOW_S]
        if len(recent) >= self.AUTO_REC_MAX:
            return "give_up", ("the camera keeps leaving rec mode on its own (%d recmodes in "
                               "the last %.0f min did not hold); not sending more"
                               % (len(recent), self.AUTO_REC_WINDOW_S / 60))
        why = self.mid_operation()
        if why:
            return "wait", why
        self._auto_recmodes.append(now)
        self._auto_rec_sent_at = now
        self.log_event("lv", "%s: camera in %s for %.0f s; sending one recmode (%d/%d in %.0f min)"
                       % (who, mode, since, len(recent) + 1, self.AUTO_REC_MAX,
                          self.AUTO_REC_WINDOW_S / 60))
        r = await self.cmd("camcmd", value="recmode", tries=1)
        await self._refresh_state()
        return "wait", "sent one recmode (%s); waiting for rec to settle" % r.result

    def _cap_retry_blocked(self, scheduled_at: float) -> Optional[str]:
        """Why a cap-held recmode retry (start_liveview) must not run now: the
        camera changed mode since (a drop to play on its own is auto_rec()'s to
        handle: 20 s reads, one recmode per drop, AUTO_REC_MAX per window), it is
        busy / in an interval run / wedged, or auto_rec()'s budget is used up."""
        if self.state.get("cammode") == "rec":
            return None if not self._lv_running else "camera already in rec"
        if self._mode_changed_at > scheduled_at:
            return "the camera changed mode on its own since"
        if self.body_busy or self.wedged or self.interval_active or self.readonly:
            return "camera busy, wedged or mid-operation"
        why = self.mid_operation()
        if why:
            return why
        now = time.monotonic()
        recent = [t for t in self._auto_recmodes if now - t < self.AUTO_REC_WINDOW_S]
        if len(recent) >= self.AUTO_REC_MAX:
            return "%d automatic recmodes in the last %.0f min" % (len(recent), self.AUTO_REC_WINDOW_S / 60)
        return None

    async def start_liveview(self, reason: str) -> bool:
        """LUMIX Sync's live-view start, run once per lifecycle event:
        stopstream -> recmode -> startstream (each 5 x 1 s on err_busy)."""
        assert self.transport
        self._lv_restart_at = time.monotonic()
        if self.body_busy or self.wedged:
            self.log_event("lv", "live view start (%s) skipped: camera %s"
                           % (reason, "in use" if self.body_busy else "wedged"))
            return False
        # recmode/startstream into an in-camera interval run is the 11:55 wedge
        # trigger; a read-only connect has no event subscription yet
        why = await self.interval_blocked() or self.readonly
        if why:
            self.log_event("lv", "live view start (%s) skipped: %s" % (reason, why))
            return False
        q = self.profile.connect
        if self._lv_running or not q.stopstream_only_if_running:
            # stopstream with no stream running answers err_critical on the S5;
            # clear the flag first so a concurrent stop_liveview() can't send a
            # second one (chaos seed 98, tests/test_chaos.py NoDoubleStopstreamTest)
            self._lv_running = False
            await self.cmd("stopstream")
        await self._refresh_state()
        # recmode only when needed: every recmode makes the camera echo
        # lens_Atta/mod_Rec, and recmode churn is part of how the S5 wedges
        need_rec = self.state.get("cammode") != "rec" or not q.recmode_only_if_needed
        if need_rec and not await self.ensure_rec():
            self._lv_running = False
            self.log_event("warn", "live view start (%s): camera stays in %s"
                           % (reason, self.state.get("cammode")))
            lim = getattr(self.transport, "limiter", None)
            if self._last_recmode == RATE_LIMITED and lim is not None and self.liveview_wanted:
                # withheld locally (never sent): run it once the cap allows instead
                # of leaving the camera in the play mode we put it in (scenario 13,
                # tests/test_chaos.py HeldBackRecmodeIsRetriedTest). A switch to
                # play on the body clears this (_lv_pending = None on mod_Play).
                self._lv_pending = "%s, after the mode-switch cap" % reason.split(", after")[0]
                self._lv_pending_after = time.monotonic() + lim.wait_s() + 0.1
                self._lv_pending_cap = time.monotonic()
                self.log_event("lv", "live view start retried in %.0f s (mode-switch cap)" % lim.wait_s())
            return False
        why = await self._wait_rec_settled()
        if why:
            self._lv_running = False
            self.log_event("lv", "live view start (%s): no startstream, %s" % (reason, why))
            return False
        r = await self.cmd("startstream", value=str(self.cfg.lv_port))
        self._lv_running = r.ok
        self.log_event("lv", "live view start (%s): startstream -> %s" % (reason, r.result))
        return r.ok

    async def _wait_rec_settled(self) -> Optional[str]:
        """Poll getstate (reads only, ~2 Hz) until the camera has been in rec with
        no mode/lens event for STABLE_S; the reason when it does not (left rec,
        busy, or still moving after STABLE_WAIT_S)."""
        deadline = time.monotonic() + self.STABLE_WAIT_S
        while True:
            why = self.stream_blocked()
            if why is None:
                return None
            if self.state.get("cammode") != "rec" or self.body_busy or time.monotonic() >= deadline:
                return why
            await asyncio.sleep(min(0.5, max(0.05, self.rec_settled_in() or 0.5)))
            with contextlib.suppress(CamError, OSError, asyncio.TimeoutError):
                await self._refresh_state()

    # ------------------------------------------------------ live view quality
    def lv_quality_wanted(self) -> str:
        """The saved live view size, else the model default (the best stable one)."""
        q = self._load_state().get("liveview_quality")
        return q if isinstance(q, str) and q else self.profile.liveview.default_quality

    def lv_quality_info(self) -> Dict[str, Any]:
        return {"current": self.lv_quality, "wanted": self.lv_quality_wanted(),
                "options": self.lv_quality_options or list(self.profile.liveview.qualities),
                "default": self.profile.liveview.default_quality}

    async def _apply_lv_quality(self) -> None:
        """At connect, before the live-view start: read liveviewsize and set the
        saved size if the camera has another one (2 s before the next command)."""
        assert self.transport
        r = await self.transport.cgi("getsetting", "liveviewsize")
        self.lv_quality = r.setting_value("liveviewsize") if r.ok else None
        want = self.lv_quality_wanted()
        opts = self.lv_quality_options or list(self.profile.liveview.qualities)
        if self.lv_quality is None or want == self.lv_quality or want not in opts:
            return
        r = await self.transport.cgi("setsetting", "liveviewsize", want)
        self.log_event("lv", "live view quality %s -> %s: %s" % (self.lv_quality, want, r.result))
        if r.ok:
            self.lv_quality = want
        await asyncio.sleep(self.LV_QUALITY_GAP_S)

    async def set_liveview_quality(self, quality: str) -> Dict[str, Any]:
        """Save + apply a live view size: setsetting liveviewsize, then (2 s later)
        the usual stopstream -> startstream restart; the S5 keeps sending the
        old size until the stream is restarted. Not connected: saved for the
        next connect."""
        opts = self.lv_quality_options or list(self.profile.liveview.qualities)
        if quality not in opts:
            raise ValueError("liveview_quality must be one of %s" % ", ".join(opts))
        self._save_state(liveview_quality=quality)
        if self.transport is None or self.status != "ready" or self.readonly:
            return {"result": "saved", "applied": False, "liveview_quality": self.lv_quality_info()}
        await self.refuse_mid_run("Changing the live view quality")
        async with self.exclusive("liveview-quality") as t:
            self._check_usable()
            r = await t.cgi("setsetting", "liveviewsize", quality)
            self.log_event("cmd", "setsetting liveviewsize=%s -> %s" % (quality, r.result))
            if not r.ok:
                return {"result": r.result, "applied": False, "liveview_quality": self.lv_quality_info()}
            self.lv_quality = quality
            restarted = None
            if self.liveview_wanted and self._lv_running:
                await asyncio.sleep(self.LV_QUALITY_GAP_S)
                restarted = await self.start_liveview("live view quality %s" % quality)
        self.bus.publish("status", self.status_dict())
        return {"result": "ok", "applied": True, "restarted": restarted,
                "liveview_quality": self.lv_quality_info()}

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
        self._last_ok = self._state_at = time.monotonic()
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
            if old.get("remaincapacity") != new.get("remaincapacity"):
                self.capture_pending_at = None
            if old and old.get("remaincapacity") != new.get("remaincapacity"):
                self.log_event("state", "remaining shots %s -> %s" % (old.get("remaincapacity"),
                                                                       new.get("remaincapacity")))
            if old.get("cammode") != new.get("cammode"):
                self._mode_changed_at = self._unsettled_at = time.monotonic()
                if old:
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
            if self.readonly:
                with contextlib.suppress(Busy):
                    await self._complete_readonly()
            if (self._lv_pending and not self._op_lock.locked()
                    and time.monotonic() - self._lv_restart_at > 5
                    and time.monotonic() >= self._lv_pending_after):
                reason, self._lv_pending = self._lv_pending, None
                cap_retry, self._lv_pending_cap = self._lv_pending_cap, None
                drop = self._cap_retry_blocked(cap_retry) if cap_retry is not None else None
                if drop:
                    self.log_event("lv", "queued recmode (%s) dropped: %s" % (reason, drop))
                elif self.liveview_wanted:
                    if cap_retry is not None:
                        # counts against auto_rec()'s budget like its own recmode
                        self._auto_recmodes.append(time.monotonic())
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
                or self.wedged or self.readonly or self.state.get("cammode") != "rec"):
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
            self._trace_note("NOTIFY %s=%s" % (name, value))
            if name == "X_Panasonic_Cam_PRec" and value.startswith("err"):
                # photo recording failed on the body. Real S5 2026-10-12 16:47:27,
                # 16:48:32, 16:50:02: capture -> ok, err_rec ~3.7 s later, no photo
                # written (remaincapacity unchanged), the body drops to playback
                self.photo_error = (time.monotonic(), value)
                self.capture_pending_at = None
                self.log_event("warn", "camera reports a photo recording error (%s): no photo "
                                       "was written" % value)
            if name != "X_Panasonic_Cam_Sync":
                continue
            if value in ("mod_Play", "mod_Rec", "lens_Atta", "lens_Deta", "busy", "busy_no_msg"):
                self._unsettled_at = time.monotonic()
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
                        self._lv_pending_cap = None
                self.body_busy = False
                self.bus.publish("status", self.status_dict())
            elif value == "mod_Play":
                self._lv_pending = None   # never pull the camera back to rec
                self._lv_pending_cap = None
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
    async def refuse_mid_run(self, what: str) -> None:
        """Busy when an in-camera interval run (rec or pause) is going: ``what``
        (settings, shutter, Auto preset, ...) has to wait until it ends."""
        why = await self.interval_blocked()
        if why:
            raise Busy("%s not possible: %s. The camera shoots on its own; stop the timelapse "
                       "first (Pause keeps the run paused, still locked)" % (what, why))

    def _check_usable(self) -> None:
        if self.body_busy:
            raise Busy("camera is being operated on the body (waiting for it to finish)")
        if self.wedged:
            raise Busy("the camera is stuck and ignores commands; switch it off and on again")
        if self.interval_active:
            raise Busy("an in-camera interval run is in progress (stop the timelapse first)")

    async def drive_is_interval(self, t: CamTransport) -> bool:
        """Drive mode Time Lapse (``stopmotion_interval``/``interval``): the shutter
        would start the camera's own interval run, not take a photo. getstate's
        ``burst_interval_status`` is ``on`` for it (real S5 2026-10-11: on with Time
        Lapse, off with Single); getsetting drivemode tells Time Lapse from Stop
        Motion Animation, which takes one frame per press."""
        if self.state.get("burst_interval_status") != "on":
            return False
        r = await t.cgi("getsetting", "drivemode", timeout=4)
        el = r.xml.find("settingvalue") if r.ok and r.xml is not None else None
        if el is None or el.get("drivemode") is None:
            return False
        return el.get("drivemode") == "stopmotion_interval" and (el.text or "").strip() in ("interval", "")

    async def capture(self) -> Dict[str, Any]:
        async with self.exclusive("capture") as t:
            before = self.state.get("remaincapacity")
            self._check_usable()
            if await self.drive_is_interval(t):
                # QA F4: the photo shutter started an in-camera interval run
                raise DriveIsTimeLapse("Drive is Time Lapse \u2014 use the Timelapse tab or switch "
                                       "drive to Single (the shutter would start the camera's own "
                                       "interval run)")
            await self._wait_card_write()
            t_sent = time.monotonic()
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
            if r.result == "ok" and not changed and not self._photo_error_after(t_sent):
                self.capture_pending_at = t_sent
                self.log_event("warn", "capture accepted but no photo written yet; rec/play switches wait "
                                       "until it is (or the camera reports an error)")
            return {"result": r.result, "remain_before": before,
                    "remain_after": self.state.get("remaincapacity"), "count_changed": changed,
                    "pending": self.capture_pending_at is not None}

    def _photo_error_after(self, t: float) -> bool:
        return self.photo_error is not None and self.photo_error[0] >= t

    CARD_WRITE_WAIT_S = 8.0

    async def _wait_card_write(self) -> None:
        """Pace the shutter on the card write: while getstate says sd_access on,
        the S5 answers a capture with err_busy (scenario 05: 6-9 of 10 presses in
        5 s; tests/test_chaos.py ShutterBurstPacedOnCardWritesTest). Reads only."""
        deadline = time.monotonic() + self.CARD_WRITE_WAIT_S
        await self._refresh_state()
        while self.state.get("sd_access") == "on" and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            await self._refresh_state()

    @contextlib.contextmanager
    def video_starting(self) -> Iterator[None]:
        """Mark a video start in flight (from the request, before any leave-Files
        or lock wait), so a stop sent right after it waits for it."""
        self._video_starts += 1
        self._video_started.clear()
        try:
            yield
        finally:
            self._video_starts -= 1
            if self._video_starts <= 0:
                self._video_starts = 0
                self._video_started.set()

    async def video(self, start: bool) -> Dict[str, Any]:
        if start:
            with self.video_starting():
                return await self._video(True)
        waited = False
        if self._video_starts:
            # scenario 07 (tests/test_chaos.py): start + stop sent together; the stop
            # must land after the start, never before it
            waited = True
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._video_started.wait(), 60)
        if not waited and self.state.get("rec") == "off":
            # nothing to stop; the S5 answers err_reject to a stray video_recstop
            return {"result": "ok", "recording": False, "note": "was not recording"}
        return await self._video(False)

    async def _video(self, start: bool) -> Dict[str, Any]:
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
        await self.refuse_mid_run("Changing camera settings")
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
        mode_now = self.state.get("cammode")
        if (self.profile.connect.recmode_only_if_needed
                and ((value == "recmode" and mode_now == "rec") or (value == "playmode" and mode_now == "play"))):
            # pointless and not harmless: recmode in rec with a stream answers err_busy and
            # counts towards the recmode churn that wedges the S5
            return {"result": "ok", "text": "already in %s mode (not sent)" % mode_now}
        await self.refuse_mid_run("camcmd %s" % value)
        if value in MODE_SWITCHES:
            why = await self.mode_switch_blocked()
            if why:
                raise Busy("camcmd %s not sent: %s (switching modes now can wedge the camera)"
                           % (value, why))
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
    def _mode_switch_limiter(self) -> ModeSwitchLimiter:
        """The transport's mode-switch cap, seeded with the switches an earlier
        server process sent (a restart must not reset the camera's count)."""
        lim = ModeSwitchLimiter(self.MODE_SWITCH_MIN_GAP_S, self.MODE_SWITCH_MAX, self.MODE_SWITCH_WINDOW_S)
        lim.restore(self._load_state().get("mode_switches") or [])
        lim.on_note = lambda: self._save_state(mode_switches=lim.export())
        return lim

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
    """The camera can't do it now (HTTP 409). ``code``: machine-readable reason
    for clients that offer a fix (e.g. ``drive_timelapse``)."""
    code: Optional[str] = None


class DriveIsTimeLapse(Busy):
    code = "drive_timelapse"


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
