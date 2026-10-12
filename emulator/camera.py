"""The emulated camera: modes, session, subscriptions, wedge realism, settings.

Everything time-dependent is scheduled on the asyncio loop; all handlers are
plain methods that the HTTP layer (``servers.py``) calls. Behaviour follows the
recordings in probe-results/ and probe-results/FINDINGS.md; anything not
observed on the real S5 is marked "guess" here and in docs/emulator.md.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

from . import MODELS, load_data
from .card import Card

log = logging.getLogger("emulator")

XML_HEAD = '<?xml version="1.0" encoding="UTF-8"?>\n'

# The recorded S5 NOTIFY right after SUBSCRIBE (SEQ 0).
CMS_INITIAL = (
    '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">\n  <e:property>\n'
    '    <SourceProtocolInfo>http-get:*:image/jpeg:DLNA.ORG_PN=JPEG_LRG;DLNA.ORG_OP=01;DLNA.ORG_CI=0;'
    'DLNA.ORG_FLAGS=00900000000000000000000000000000,http-get:*:image/jpeg:DLNA.ORG_PN=JPEG_MED;'
    'DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=00900000000000000000000000000000,http-get:*:image/jpeg:'
    'DLNA.ORG_PN=JPEG_SM;DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=00900000000000000000000000000000,'
    'http-get:*:image/jpeg:DLNA.ORG_PN=JPEG_TN;DLNA.ORG_OP=01;DLNA.ORG_CI=1;'
    'DLNA.ORG_FLAGS=00900000000000000000000000000000,http-get:*:application/octet-stream,'
    'http-get:*:video/mp4:DLNA.ORG_PN=AVC_MP4_BL_L31_HD_AAC;DLNA.ORG_OP=01;DLNA.ORG_CI=0;'
    'DLNA.ORG_FLAGS=01100000000000000000000000000000,http-get:*:video/mp4:'
    'DLNA.ORG_PN=AVC_MP4_MP_HD_1080i_AAC;DLNA.ORG_OP=01;DLNA.ORG_CI=0;'
    'DLNA.ORG_FLAGS=01100000000000000000000000000000</SourceProtocolInfo>\n  </e:property>\n'
    '  <e:property>\n    <SinkProtocolInfo></SinkProtocolInfo>\n  </e:property>\n'
    '  <e:property>\n    <CurrentConnectionIDs>0</CurrentConnectionIDs>\n  </e:property>\n'
    '</e:propertyset>\n')
EVENT_BODY = ('<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">\n  <e:property>\n'
              '    <%s>%s</%s>\n  </e:property>\n</e:propertyset>\n')

# camcmd values LUMIX Sync knows (docs/lumix-sync-wifi-flow.md section 14); others -> err_param
CAMCMDS = {"recmode", "playmode", "capture", "capture_cancel", "video_recstart",
           "video_recstop", "autoreviewunlock", "menu_entry", "menu_exit", "lcd_on", "oneshot_af",
           "touchafrelease", "touchaerelease", "touchafaerelease", "touch_af_cancel",
           "touch_ae_cancel", "af_area_reset", "4kphoto_marking", "alert_off", "poweroff",
           "tele-normal", "tele-fast", "wide-normal", "wide-fast", "zoomstop"}
CAMCTRL_OK = {"touch_trace", "touchaf_chg_pos", "touchaf_chg_area", "pinch", "touchcapt_ctrl",
              "touchae_ctrl", "af_ae_lock", "asst_disp", "change_disp_mag", "focus", "livecomp",
              "hrs", "program_shift", "shtrspeed_syncro", "frame_ctrl"}

GETSTATE_REC = ["batt", "batt_per", "rec", "cammode", "video_remaincapacity", "remaincapacity",
                "sdcardstatus", "sd_memory", "version", "temperature", "burst_interval_status",
                "sd_access", "rem_disp_typ", "progress_time", "operate", "stop_motion_num",
                "stop_motion", "lens", "interval_status", "sdi_state", "sd2_cardstatus",
                "sd2_memory", "sd2_access", "current_sd", "backupmode", "batt_grip",
                "batt_per_grip", "warn_disp", "cinelike", "slotfunc", "sd_usage_rate", "sd_full",
                "sd2_full", "mf_guide", "interval_remain_num", "interval_end_time",
                "video_priority_disp", "lc_state", "lc_expo_time", "lc_shoot_num",
                "lc_elapsed_sec", "hrs_num", "hrs_state"]
GETSTATE_PLAY = ["batt", "batt_per", "cammode", "sdcardstatus", "sd_memory", "version", "play",
                 "temperature", "sd_access", "lens", "add_location_data", "sd2_cardstatus",
                 "sd2_memory", "sd2_access", "current_sd", "batt_grip", "batt_per_grip",
                 "sd_full", "sd2_full"]
STATE_DEFAULTS = {
    "batt": "5/5", "batt_per": "-1", "video_remaincapacity": "1800", "remaincapacity": "6744",
    "sdcardstatus": "write_enable", "sd_memory": "set", "temperature": "low", "sd_access": "off",
    "rem_disp_typ": "num", "operate": "enable/enable", "stop_motion_num": "0",
    "stop_motion": "off", "lens": "normal", "sdi_state": "none", "sd2_cardstatus": "write_enable",
    "sd2_memory": "set", "sd2_access": "off", "current_sd": "sd2", "backupmode": "off",
    "batt_grip": "-1/0", "batt_per_grip": "-1", "warn_disp": "no_disp", "cinelike": "off",
    "slotfunc": "relay2_1", "sd_usage_rate": "0", "sd_full": "false", "sd2_full": "false",
    "mf_guide": "feet", "video_priority_disp": "off", "lc_state": "off", "lc_expo_time": "1",
    "lc_shoot_num": "0", "lc_elapsed_sec": "0", "hrs_num": "0/0", "hrs_state": "off",
    "play": "stop", "add_location_data": "off",
}
# getsetting values seen in logs/camera-2026-10-11.log; other types answer err_critical
GETSETTING_DEFAULTS = {
    "focal": ("1055/256", ""), "shtrspeed": ("0/256", ""), "iso": ("auto", ""),
    "exposure": ("0", ""), "focusmode": ("mf", ""), "afmode": ("1area_asst", "auto"),
    "drivemode": ("normal", ""), "lightmetering": ("multi", ""), "whitebalance": ("auto", ""),
    "touch_type": ("5", ""), "ex_tele_conv": ("off", ""), "peaking": ("off", ""),
    "photostyle": ("vivid", ""), "colormode": ("off", ""), "qmenu_disp_style": ("normal", ""),
    "play_sort_mode": ("folder", ""),
}
# setsetting types accepted besides every cmd_type found in allmenu
SETSETTING_EXTRA = {"device_name", "raw_img_send", "current_sd", "photostyle", "photostyle2",
                    "colormode", "peaking", "overlay", "focal", "shtrspeed", "iso", "exposure",
                    "whitebalance", "focusmode", "afmode", "drivemode", "lightmetering",
                    "liveviewsize"}

CAMERA_EPOCH = dt.datetime(2020, 2, 26, 8, 45, 0)   # the test body's clock was never set


@dataclass
class EmuConfig:
    wedge_realism: bool = True
    settle_min: float = 5.0          # post-SUBSCRIBE mod_Play delay window (observed 5-22 s)
    settle_max: float = 22.0
    rec_switch_s: float = 0.3        # recmode/playmode -> getstate cammode flips
    sub_play_switch_s: float = 0.8   # SUBSCRIBE in rec -> silent switch to play (< 1 s observed)
    capture_busy_s: float = 1.2      # card write after capture (guess)
    video_busy_s: float = 2.0        # card write after video_recstop (guess)
    approval: str = "auto"           # auto | prompt | refuse
    approval_delay_s: float = 1.0    # first req_acc -> ok_under_research_no_msg; ok after this
    session_timeout_s: float = 30.0  # no request for this long = session dropped (guess); 0 = off
    stream_keepalive_s: float = 10.0  # no request from the viewer = stream stops (guess)
    fps: float = 25.0
    cds_ready_s: float = 0.6         # Browse answers 701 right after the switch to play
    churn_count: int = 4             # recmode #4 within churn_window_s -> err_critical + wedge
    churn_window_s: float = 20.0
    interval_scale: float = 1.0      # time-lapse interval multiplier (0.01 = 100x faster)
    browse_max: int = 100            # max items per Browse page (guess)
    dl_range: bool = True            # honour Range on :50001 (unverified on the S5)
    dl_rate: float = 0.0             # :50001 throughput cap in bytes/s (0 = unlimited; for QA)
    seed: Optional[int] = None


@dataclass
class Identity:
    index: int = 0
    mac: str = "02:4C:58:E0:00:01"   # locally administered, never a real Panasonic OUI
    serial: str = "EMU0000001"
    model: str = "dc-s5"

    @property
    def mac_hex(self) -> str:
        return self.mac.replace(":", "").upper()

    @property
    def friendly_name(self) -> str:
        return "S5-" + self.mac_hex[-6:]

    @classmethod
    def for_index(cls, i: int, model: str = "dc-s5") -> "Identity":
        return cls(index=i, mac="02:4C:58:E0:00:%02X" % (i + 1), serial="EMU%07d" % (i + 1),
                   model=model)


@dataclass
class Sub:
    sid: str
    callback: str
    ip: str
    expires: float
    seq: int = 0


@dataclass
class Session:
    ip: str
    uuid: str
    name: str
    first_req: float
    state: str = "pending"          # pending | ok
    last_seen: float = 0.0


class EmuCamera:
    def __init__(self, ident: Identity, cfg: EmuConfig, host: str, http_port: int,
                 upnp_port: int, dl_port: int, card_photos: int = 4, card_group: int = 120):
        self.ident, self.cfg, self.host = ident, cfg, host
        self.http_port, self.upnp_port, self.dl_port = http_port, upnp_port, dl_port
        self.model = MODELS[ident.model]
        self.rng = random.Random(cfg.seed)
        self._card_args = (card_photos, card_group)
        self.data = {k: load_data(ident.model, f) for k, f in
                     (("ddd", "ddd.xml"), ("capability", "capability.xml"),
                      ("allmenu", "allmenu.xml"), ("curmenu", "curmenu.xml"))}
        self._allmenu_index()
        self.log: Deque[Dict[str, Any]] = deque(maxlen=2000)
        self.on_power: Optional[Callable[[bool], Any]] = None   # set by the server layer
        self.streamer: Any = None                                # set by the server layer
        self._handles: List[asyncio.TimerHandle] = []
        self._tasks: Set[asyncio.Task] = set()
        self.powered = True
        self.boot()

    # ------------------------------------------------------------ lifecycle
    def boot(self) -> None:
        """Fresh power-on state: play mode, no session, no subscription, not wedged."""
        for h in self._handles:
            h.cancel()
        self._handles = []
        for t in list(self._tasks):
            t.cancel()
        self._tasks = set()
        self.boot_mono = time.monotonic()
        self.cammode = "play"
        self.play_since = time.monotonic() - 10
        self.sessions: Dict[str, Session] = {}
        self.dropped: Set[str] = set()
        self.subs: Dict[str, Sub] = {}
        self.sid_counter = 0
        self.subscribed = False         # a SUBSCRIBE took the camera off "Connecting"
        self.settle_until: Optional[float] = None
        self.wedged = False
        self.wedge_reason = ""
        self.wedged_at: Optional[float] = None
        self.busy_until = 0.0
        self.body_busy = False
        self.recmodes: Deque[float] = deque(maxlen=16)
        self.stream_open = False        # startstream ok and no stopstream since
        self.stream_alive = False       # frames actually flowing
        self.stream_target: Optional[Tuple[str, int]] = None
        self.stream_toggle = False
        self.frames_sent = 0
        self.recording = False
        self.rec_started = 0.0
        self.touch_on = False
        self.interval = {"armed": False, "research": False, "state": "enable", "remain": 0,
                         "group": None, "task": None}
        self.settings: Dict[str, Tuple[str, str]] = dict(GETSETTING_DEFAULTS)
        self.curmenu = self.data["curmenu"]
        self._sync_settings_from_curmenu()
        self.card = Card(photos=self._card_args[0], group_size=self._card_args[1],
                         base_date="2020-02-10T13:15:54")
        self.counters: Dict[str, int] = {}
        self.event_log: Deque[Dict[str, Any]] = deque(maxlen=500)

    def power_off(self, reason: str = "") -> None:
        self.record("power", "off", reason)
        self.powered = False
        if self.streamer:
            self.streamer.stop()
        self.boot()                      # volatile state is gone
        self.powered = False
        if self.on_power:
            self.on_power(False)

    def power_on(self) -> None:
        self.boot()
        self.powered = True
        self.record("power", "on", "")
        if self.on_power:
            self.on_power(True)

    # --------------------------------------------------------------- helpers
    def now(self) -> float:
        return time.monotonic()

    def camera_clock(self) -> dt.datetime:
        return CAMERA_EPOCH + dt.timedelta(seconds=self.now() - self.boot_mono)

    def http_date(self) -> str:
        return self.camera_clock().strftime("%a, %d %b %Y %H:%M:%S GMT")

    def later(self, delay: float, fn: Callable[[], Any]) -> None:
        loop = asyncio.get_event_loop()
        self._handles.append(loop.call_later(max(0.0, delay), fn))
        if len(self._handles) > 256:
            self._handles = [h for h in self._handles if not h.cancelled()][-128:]

    def spawn(self, coro) -> None:
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def record(self, kind: str, what: str, result: str, ip: str = "") -> None:
        self.log.append({"t": round(time.time(), 3), "kind": kind, "ip": ip, "q": what,
                         "result": result, "cammode": self.cammode, "wedged": self.wedged})

    def in_settle(self) -> bool:
        return self.settle_until is not None and self.now() < self.settle_until

    def is_busy(self) -> bool:
        return self.now() < self.busy_until or self.body_busy

    def wedge(self, why: str) -> None:
        if not self.wedged:
            self.wedged, self.wedge_reason, self.wedged_at = True, why, time.time()
            log.warning("[%s] WEDGED: %s", self.ident.friendly_name, why)
            self.record("wedge", why, "wedged")

    def set_mode(self, mode: str, delay: float = 0.0) -> None:
        def apply() -> None:
            if self.cammode != mode:
                self.cammode = mode
                if mode == "play":
                    self.play_since = self.now()
                    self.stream_alive = False      # the camera stops the stream on its own
        if delay <= 0:
            apply()
        else:
            self.later(delay, apply)

    # ---------------------------------------------------------------- events
    def emit(self, var: str, value: str, delay: float = 0.0) -> None:
        def fire() -> None:
            self.event_log.append({"t": round(time.time(), 3), "var": var, "value": value})
            body = EVENT_BODY % (var, value, var)
            for sub in list(self.subs.values()):
                self.spawn(self._notify(sub, body))
        if delay <= 0:
            fire()
        else:
            self.later(delay, fire)

    async def _notify(self, sub: Sub, body: str) -> None:
        seq = sub.seq
        sub.seq += 1
        u = urllib.parse.urlparse(sub.callback)
        host, port = u.hostname or sub.ip, u.port or 80
        data = body.encode()
        req = ("NOTIFY %s HTTP/1.1\r\nHOST: %s:%d\r\nCONTENT-TYPE: text/xml; charset=\"utf-8\"\r\n"
               "CONTENT-LENGTH: %d\r\nNT: upnp:event\r\nNTS: upnp:propchange\r\nSID: %s\r\n"
               "SEQ: %d\r\nCONNECTION: close\r\n\r\n" % (u.path or "/", host, port, len(data),
                                                         sub.sid, seq)).encode() + data
        try:
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(
                    host, port, local_addr=(self.host, 0)), 3)
            except OSError:
                r, w = await asyncio.wait_for(asyncio.open_connection(host, port), 3)
            w.write(req)
            await w.drain()
            try:
                await asyncio.wait_for(r.read(1024), 3)
            except asyncio.TimeoutError:
                pass
            w.close()
            self.record("notify", "%s SEQ %d -> %s" % (sub.sid[-17:], seq, sub.callback),
                        body.split(">")[3].split("<")[0] if seq else "CMS initial")
        except (OSError, asyncio.TimeoutError) as e:
            self.record("notify", "%s SEQ %d -> %s" % (sub.sid, seq, sub.callback),
                        "failed: %s" % e)

    # ------------------------------------------------------------ allmenu
    def _allmenu_index(self) -> None:
        """(cmd_type, cmd_value) -> parent item id, from allmenu."""
        root = ET.fromstring(self.data["allmenu"].encode())
        self.menu_types: Set[str] = set()
        self.menu_map: Dict[Tuple[str, str], List[str]] = {}
        self.value_items: Dict[str, str] = {}     # cmd_type -> item id of a "__value__" item

        def walk(el: ET.Element, parent_item: Optional[str]) -> None:
            for c in el:
                if c.tag == "item":
                    if c.get("cmd_mode") == "setsetting":
                        t, v = c.get("cmd_type", ""), c.get("cmd_value", "")
                        self.menu_types.add(t)
                        if v == "__value__" and c.get("id"):
                            self.value_items.setdefault(t, c.get("id", ""))
                        if parent_item:
                            self.menu_map.setdefault((t, v), []).append(parent_item)
                    walk(c, c.get("id"))
                else:
                    walk(c, parent_item)
        walk(root, None)

    def _curmenu_set(self, item_id: str, value: str) -> None:
        # curmenu items carry other attributes too (active="yes" on the S5)
        pat = re.compile(r'(<item id="%s"[^>]*? value=")[^"]*(")' % re.escape(item_id))
        self.curmenu = pat.sub(lambda m: m.group(1) + value + m.group(2), self.curmenu, count=1)

    def _sync_settings_from_curmenu(self) -> None:
        """The recorded getsetting values and curmenu come from different moments
        (e.g. curmenu says EV +1/3 and drive Time Lapse, the log getsetting 0 and
        normal). A real body answers both from one state, so getsetting follows
        curmenu wherever curmenu holds a value allmenu offers for that type."""
        parents: Dict[str, List[str]] = {}
        for (t, v), items in self.menu_map.items():
            for it in items:
                if it not in parents.setdefault(t, []):
                    parents[t].append(it)
        for typ in list(self.settings):
            for item in parents.get(typ, []):
                v = self.curmenu_value(item)
                if v and (typ, v) in self.menu_map:
                    self.settings[typ] = (v, self.settings[typ][1])
                    break

    def curmenu_value(self, item_id: str) -> Optional[str]:
        m = re.search(r'<item id="%s"[^>]*? value="([^"]*)"' % re.escape(item_id), self.curmenu)
        return m.group(1) if m else None

    # ================================================================ cam.cgi
    def handle_cgi(self, ip: str, params: Dict[str, str]) -> Tuple[str, str]:
        """-> (content_type, body). Every reply is HTTP 200, like the camera."""
        mode = params.get("mode", "")
        typ, value, value2 = params.get("type"), params.get("value"), params.get("value2")
        q = "&".join("%s=%s" % (k, params[k]) for k in ("mode", "type", "value", "value2")
                     if k in params)
        self.counters[mode] = self.counters.get(mode, 0) + 1
        ctype, body, result = self._dispatch(ip, mode, typ, value, value2)
        self.record("cgi", q, result, ip)
        return ctype, body

    @staticmethod
    def _xml(result: str, extra: str = "") -> Tuple[str, str, str]:
        return "text/xml", XML_HEAD + "<camrply><result>%s</result>%s</camrply>" % (result, extra), result

    def _session_check(self, ip: str, mode: str) -> Optional[str]:
        """None = allowed, else the error result."""
        s = self.sessions.get(ip)
        now = self.now()
        if s is not None and s.state == "ok":
            if self.cfg.session_timeout_s and now - s.last_seen > self.cfg.session_timeout_s:
                self.drop_session(ip, "idle %.0fs" % (now - s.last_seen))
                return "err_reject"
            s.last_seen = now
            return None
        if ip in self.dropped:
            return "err_reject"
        # never connected: the S5 answered getstate before req_acc (session.log 20:39:17)
        if mode in ("getstate", "getinfo"):
            return None
        return "err_reject"

    def drop_session(self, ip: Optional[str] = None, why: str = "") -> None:
        for k in ([ip] if ip else list(self.sessions)):
            if k in self.sessions:
                del self.sessions[k]
                self.dropped.add(k)
                self.record("session", "dropped %s" % k, why, k)
        if self.stream_target and (ip is None or self.stream_target[0] == ip):
            self.stream_alive = False

    def _dispatch(self, ip: str, mode: str, typ: Optional[str], value: Optional[str],
                  value2: Optional[str]) -> Tuple[str, str, str]:
        if mode == "accctrl":
            return self._accctrl(ip, typ, value, value2)
        err = self._session_check(ip, mode)
        if err:
            return self._xml(err)
        if mode == "getstate":
            return self._getstate()
        if mode == "getinfo":
            return self._getinfo(typ)
        if mode == "getsetting":
            if typ in self.settings:
                v, text = self.settings[typ]
                return self._xml("ok", '<settingvalue %s="%s">%s</settingvalue>' % (typ, v, text))
            return self._xml("err_critical")
        if mode == "setsetting":
            return self._setsetting(typ, value, value2)
        if mode == "camcmd":
            return self._camcmd(value or "")
        if mode == "camctrl":
            return self._camctrl(typ or "", value or "", value2)
        if mode == "startstream":
            return self._startstream(ip, value)
        if mode == "stopstream":
            return self._stopstream()
        if mode == "get_content_info":
            if self.cfg.wedge_realism and self.wedged:
                return self._xml("err_busy")
            if self.cammode != "play":
                return self._xml("err_reject")            # guess
            total, top = self.card.counts()
            return ("text/xml", XML_HEAD + "<camrply> <result>ok</result> <current_position>%d"
                    "</current_position> <total_content_number>%d</total_content_number> "
                    "<content_number>%d</content_number> </camrply>" % (top - 1, total, top), "ok")
        if mode in ("playcmd", "editcmd", "notify"):
            return self._xml("ok" if self.cammode == "play" or mode == "notify" else "err_reject")
        return self._xml("err_param")

    # ---------------------------------------------------------------- accctrl
    def _accctrl(self, ip: str, typ: Optional[str], value: Optional[str],
                 value2: Optional[str]) -> Tuple[str, str, str]:
        name = self.ident.friendly_name
        if typ in ("req_acc_g", "req_acc_e", "req_acc_a"):
            return "text/plain", "err_param", "err_param"   # the S5 has no encrypted handshake
        if typ == "req_acc_can":
            self.sessions.pop(ip, None)
            return "text/plain", "ok", "ok"
        if typ != "req_acc" or not value:
            return "text/plain", "err_param", "err_param"
        now = self.now()
        s = self.sessions.get(ip)
        if s is None or s.uuid != value:
            s = Session(ip=ip, uuid=value, name=value2 or "", first_req=now, last_seen=now)
            self.sessions[ip] = s
        if s.state == "ok":
            s.last_seen = now
            return self._acc_ok(name)
        if self.cfg.approval == "refuse":
            return "text/plain", "err_user_refused", "err_user_refused"
        if self.cfg.approval == "prompt":
            r = "ok_under_research,%s,remote,encrypted" % name
            return "text/plain", r, "ok_under_research"
        if now - s.first_req < self.cfg.approval_delay_s:
            r = "ok_under_research_no_msg,%s,remote,encrypted" % name
            return "text/plain", r, "ok_under_research_no_msg"
        s.state, s.last_seen = "ok", now
        self.dropped.discard(ip)
        # one remote at a time: a new client replaces the old one (guess)
        for other in [k for k in self.sessions if k != ip and self.sessions[k].state == "ok"]:
            self.drop_session(other, "replaced by %s" % ip)
        return self._acc_ok(name)

    def _acc_ok(self, name: str) -> Tuple[str, str, str]:
        return "text/plain", "ok,%s,remote,encrypted" % name, "ok"

    def approve(self) -> None:
        for s in self.sessions.values():
            if s.state == "pending":
                s.state = "ok"
                s.last_seen = self.now()
                self.dropped.discard(s.ip)

    # --------------------------------------------------------------- getstate
    def state_values(self) -> Dict[str, str]:
        v = dict(STATE_DEFAULTS)
        v["cammode"] = self.cammode
        v["version"] = self.model["version"]
        v["rec"] = "on" if self.recording else "off"
        v["progress_time"] = str(int(self.now() - self.rec_started)) if self.recording else "0"
        v["sd_access"] = "on" if self.now() < self.busy_until else "off"
        v["sd2_access"] = v["sd_access"]
        total, _ = self.card.counts()
        v["remaincapacity"] = str(max(0, 6800 - total))
        v["burst_interval_status"] = ("on" if self.settings.get("drivemode", ("",))[0]
                                      == "stopmotion_interval" else "off")
        iv = self.interval
        v["interval_status"] = iv["state"]
        num = self.curmenu_value("menu_item_id_interval_shoot_num") or "1080"
        v["interval_remain_num"] = str(iv["remain"]) if iv["state"] != "enable" else num
        end = self.camera_clock() + dt.timedelta(minutes=13)
        v["interval_end_time"] = "%d/%d/%d/%d/%d" % (end.year, end.month, end.day, end.hour,
                                                     end.minute)
        if iv.get("warn"):
            v["warn_disp"] = iv["warn"]
        return v

    def _getstate(self) -> Tuple[str, str, str]:
        v = self.state_values()
        keys = GETSTATE_REC if self.cammode == "rec" else GETSTATE_PLAY
        inner = "".join("<%s>%s</%s>" % (k, v[k], k) for k in keys)
        return self._xml("ok", "<state>%s</state>" % inner)

    # ---------------------------------------------------------------- getinfo
    def _getinfo(self, typ: Optional[str]) -> Tuple[str, str, str]:
        if typ == "capability":
            return "text/xml", self.data["capability"], "ok"
        if typ == "allmenu":
            return "text/xml", self.data["allmenu"], "ok"
        if typ == "curmenu":
            return "text/xml", self.curmenu, "ok"
        if typ == "lens":
            return "text/plain", self.model["lens"], "ok"
        return self._xml("err_param")

    # ------------------------------------------------------------- setsetting
    def _setsetting(self, typ: Optional[str], value: Optional[str],
                    value2: Optional[str]) -> Tuple[str, str, str]:
        if not typ or value is None:
            return self._xml("err_param")
        if typ not in self.menu_types and typ not in SETSETTING_EXTRA:
            return self._xml("err_param")
        if (typ, value) in self.menu_map:
            for item in self.menu_map[(typ, value)]:
                self._curmenu_set(item, value)
        elif typ in self.value_items:
            # free-value item (interval_shoot_num, interval_shoot_interval, ...): the
            # item itself carries the value in curmenu
            self._curmenu_set(self.value_items[typ], value)
        elif typ not in SETSETTING_EXTRA and (typ, "__value__") not in self.menu_map:
            return self._xml("err_param")            # not a value allmenu offers
        old_text = self.settings.get(typ, ("", ""))[1]
        self.settings[typ] = (value, old_text if typ == "afmode" else "")
        if typ == "drivemode" and value != "stopmotion_interval":
            self._interval_stop("enable")
        return self._xml("ok")

    # ----------------------------------------------------------------- camcmd
    def _camcmd(self, value: str) -> Tuple[str, str, str]:
        if value not in CAMCMDS:
            return self._xml("err_param")       # parser still works while wedged
        realism = self.cfg.wedge_realism
        if realism and self.wedged:
            if value == "poweroff" and self.cammode == "play":
                return self._xml("err_reject")
            return self._xml("err_busy")
        if realism and self.in_settle():
            # SUBSCRIBE made the S5 start its own switch to play; a camcmd landing in that
            # switch answers err_critical and leaves camcmd stuck (FINDINGS 21:24, 22:45)
            self.wedge("camcmd %s %.1fs before the post-SUBSCRIBE mod_Play"
                       % (value, (self.settle_until or 0) - self.now()))
            if value == "recmode":
                # queued events arrive, the camera ends in rec, startstream gives 0 frames
                self.settle_until = None
                self.emit("X_Panasonic_Cam_Sync", "lens_Atta", 0.3)
                self.emit("X_Panasonic_Cam_Sync", "mod_Play", 0.5)
                self.emit("X_Panasonic_Cam_Sync", "mod_Rec", 0.7)
                self.set_mode("rec", 0.7)
            return self._xml("err_critical")
        if realism and self.is_busy():
            return self._xml("err_busy")
        iv = self.interval
        if iv["state"] in ("run", "pause") and value in ("capture", "recmode", "playmode",
                                                         "video_recstart"):
            return self._xml("err_busy")
        if value == "recmode":
            return self._recmode()
        if value == "playmode":
            if self.recording:
                return self._xml("err_busy")
            if self.cammode != "play":
                self.set_mode("play", self.cfg.rec_switch_s)
                self.emit("X_Panasonic_Cam_Sync", "mod_Play", self.cfg.rec_switch_s + 0.1)  # guess
            return self._xml("ok")
        if value == "capture":
            if self.cammode != "rec":
                return self._xml("err_reject")
            if self.recording:
                return self._xml("err_reject")          # movie_shot_disable
            if iv["armed"] and self.settings["drivemode"][0] == "stopmotion_interval":
                self._interval_start()
                return self._xml("ok")
            self.card.add_photo(now=True)
            if realism:
                self.busy_until = self.now() + self.cfg.capture_busy_s
            return self._xml("ok")
        if value == "video_recstart":
            if self.cammode != "rec" or self.recording:
                return self._xml("err_reject")
            self.recording, self.rec_started = True, self.now()
            return self._xml("ok")
        if value == "video_recstop":
            if not self.recording:
                return self._xml("err_reject")
            secs = int(self.now() - self.rec_started)
            self.recording = False
            self.card.add_video("mp4", seconds=max(1, secs), now=True)
            if realism:
                self.busy_until = self.now() + self.cfg.video_busy_s
            return self._xml("ok")
        if value == "oneshot_af":
            return self._xml("ok" if self.cammode == "rec" else "err_reject")
        if value == "poweroff":
            if realism and self.cammode == "play":
                return self._xml("err_reject")          # logged 2026-10-11
            self.later(0.5, lambda: self.power_off("camcmd poweroff"))
            return self._xml("ok")
        if value in ("touchafrelease", "touchafaerelease", "touch_af_cancel"):
            self.touch_on = False
        return self._xml("ok")

    def _recmode(self) -> Tuple[str, str, str]:
        if self.cammode == "rec" and self.stream_alive:
            return self._xml("err_busy")                # "already rec" (FINDINGS 20:42)
        now = self.now()
        self.recmodes.append(now)
        recent = [t for t in self.recmodes if now - t <= self.cfg.churn_window_s]
        if self.cfg.wedge_realism and len(recent) >= self.cfg.churn_count:
            # recmode churn (22:01): the camera switches itself to play and wedges
            self.wedge("recmode churn: %d recmodes in %.0fs" % (len(recent), now - recent[0]))
            self.set_mode("play", 0.2)
            self.emit("X_Panasonic_Cam_Sync", "mod_Play", 0.3)
            return self._xml("err_critical")
        self.set_mode("rec", self.cfg.rec_switch_s)
        self.emit("X_Panasonic_Cam_Sync", "lens_Atta", 0.2)
        self.emit("X_Panasonic_Cam_Sync", "mod_Rec", 0.45)
        return self._xml("ok")

    # ---------------------------------------------------------------- camctrl
    def _camctrl(self, typ: str, value: str, value2: Optional[str]) -> Tuple[str, str, str]:
        if typ == "touch":
            if value2 == "on":
                if self.touch_on:
                    return self._xml("err_reject")      # second "on" without a release
                self.touch_on = True
                return self._xml("ok")
            if value2 == "off":
                self.touch_on = False
                return self._xml("ok")
            return self._xml("err_param")
        if typ == "interval":
            return self._interval_ctrl(value)
        if typ == "stop_motion":
            return self._xml("ok")
        if typ in CAMCTRL_OK:
            return self._xml("ok" if self.cammode == "rec" or typ == "touchcapt_ctrl"
                             else "err_reject")
        return self._xml("err_param")

    # ---------------------------------------------------------------- interval
    def _interval_ctrl(self, value: str) -> Tuple[str, str, str]:
        iv = self.interval
        if value == "set":
            if self.settings["drivemode"][0] != "stopmotion_interval":
                return self._xml("err_reject")
            if not iv["research"] and not iv["armed"]:
                iv["research"] = True                   # app repeats "set" while this lasts
                return self._xml("ok_under_research")
            iv["research"], iv["armed"] = False, True
            iv.pop("warn", None)
            return self._xml("ok")
        if value == "pause" and iv["state"] == "run":
            iv["state"] = "pause"
            return self._xml("ok")
        if value in ("restart", "continue") and iv["state"] == "pause":
            iv["state"] = "run"
            return self._xml("ok")
        if value in ("cancel", "stop"):
            if iv["state"] in ("run", "pause"):
                self._interval_stop("enable", warn="interval_canceled" if value == "cancel" else
                                    "interval_finish")
                return self._xml("ok")
            iv["armed"] = iv["research"] = False
            return self._xml("ok")
        return self._xml("err_reject")

    def _interval_start(self) -> None:
        iv = self.interval
        num = int(self.curmenu_value("menu_item_id_interval_shoot_num") or 1080)
        secs = float(self.curmenu_value("menu_item_id_interval_shoot_interval") or 30)
        iv.update(state="run", remain=num, armed=False, group=self.card.start_group("Interval"))
        iv.pop("warn", None)
        period = max(0.05, secs * self.cfg.interval_scale)

        async def run() -> None:
            while iv["remain"] > 0:
                if iv["state"] == "run":
                    self.card.add_photo(group=iv["group"], now=True)
                    iv["remain"] -= 1
                    if self.cfg.wedge_realism:
                        self.busy_until = self.now() + min(self.cfg.capture_busy_s, period / 2)
                await asyncio.sleep(period)
            self._interval_stop("enable", warn="interval_finish")
        task = asyncio.ensure_future(run())
        iv["task"] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _interval_stop(self, state: str, warn: Optional[str] = None) -> None:
        iv = self.interval
        t = iv.get("task")
        if t is not None and t is not asyncio.current_task():
            t.cancel()
        iv.update(state=state, armed=False, research=False, task=None, remain=0)
        if warn:
            iv["warn"] = warn

    # -------------------------------------------------------------- live view
    def _startstream(self, ip: str, value: Optional[str]) -> Tuple[str, str, str]:
        try:
            port = int(value or "")
            assert 0 < port < 65536
        except (ValueError, AssertionError):
            return self._xml("err_param")
        if self.cfg.wedge_realism and self.wedged:
            # alternates ok/err_busy and never delivers a frame (FINDINGS 21:10)
            self.stream_toggle = not self.stream_toggle
            if not self.stream_toggle:
                return self._xml("err_busy")
            self.stream_open, self.stream_alive = True, False
            self.stream_target = (ip, port)
            return self._xml("ok")
        if self.cfg.wedge_realism and self.is_busy():
            return self._xml("err_busy")
        if self.cammode != "rec":
            return self._xml("err_reject")              # guess: no stream outside rec
        self.stream_open = True
        self.stream_target = (ip, port)
        # before any SUBSCRIBE the S5 sits on "Connecting": ok, but 0 datagrams (20:36 run)
        self.stream_alive = self.subscribed or not self.cfg.wedge_realism
        if self.streamer:
            self.streamer.ensure_running()
        return self._xml("ok")

    def _stopstream(self) -> Tuple[str, str, str]:
        realism = self.cfg.wedge_realism
        if realism and self.in_settle():
            self.wedge("stopstream %.1fs before the post-SUBSCRIBE mod_Play"
                       % ((self.settle_until or 0) - self.now()))
            return self._xml("err_critical")
        if not self.stream_open:
            return self._xml("err_critical" if realism else "ok")
        self.stream_open = self.stream_alive = False
        self.stream_target = None
        return self._xml("ok")

    def stream_should_send(self) -> bool:
        if not (self.powered and self.stream_open and self.stream_alive and self.stream_target):
            return False
        if self.cammode != "rec" or (self.cfg.wedge_realism and self.wedged):
            return False
        s = self.sessions.get(self.stream_target[0])
        if s is None or s.state != "ok":
            self.stream_alive = False
            return False
        if self.cfg.stream_keepalive_s and self.now() - s.last_seen > self.cfg.stream_keepalive_s:
            self.stream_alive = False                   # no keepalive: camera stops streaming
            self.record("stream", "stopped", "no request for %.0fs" % (self.now() - s.last_seen))
            return False
        return True

    # ================================================================== UPnP
    def ddd(self) -> str:
        t = self.data["ddd"]
        for k, v in (("{friendly_name}", self.ident.friendly_name), ("{serial}", self.ident.serial),
                     ("{mac_hex}", self.ident.mac_hex), ("{host}", self.host),
                     ("{upnp_port}", str(self.upnp_port))):
            t = t.replace(k, v)
        return t

    def subscribe(self, ip: str, headers: Dict[str, str]) -> Tuple[int, Dict[str, str]]:
        sid = headers.get("SID")
        cb = headers.get("CALLBACK")
        tmo = re.search(r"Second-(\d+)", headers.get("TIMEOUT", "")) or None
        secs = min(int(tmo.group(1)), 1800) if tmo else 300
        if sid:
            if cb or headers.get("NT"):
                self.record("upnp", "SUBSCRIBE renew with CALLBACK/NT", "400", ip)
                return 400, {}
            sub = self.subs.get(sid)
            if sub is None or sub.expires < self.now():
                self.subs.pop(sid, None)
                self.record("upnp", "SUBSCRIBE renew %s" % sid, "412", ip)
                return 412, {}
            sub.expires = self.now() + secs
            self.record("upnp", "SUBSCRIBE renew %s" % sid, "200", ip)
            return 200, {"SID": sid, "TIMEOUT": "Second-%d" % secs}
        m = re.match(r"<([^>]+)>", cb or "")
        if not m or headers.get("NT") != "upnp:event":
            self.record("upnp", "SUBSCRIBE bad CALLBACK/NT", "412", ip)
            return 412, {}
        self.sid_counter += 1
        sid = "uuid:4D454931-%04X-1000-8000-%s" % (self.sid_counter + 4, self.ident.mac_hex)
        sub = Sub(sid=sid, callback=m.group(1), ip=ip, expires=self.now() + secs)
        self.subs[sid] = sub
        self.subscribed = True
        self.record("upnp", "SUBSCRIBE new -> %s" % sub.callback, "200 " + sid, ip)
        self.later(0.07, lambda: self.spawn(self._notify(sub, CMS_INITIAL)))
        # The S5 treats the subscriber as a DLNA client: in rec it silently switches to
        # play within ~1 s; mod_Play follows 5-22 s after the SUBSCRIBE (FINDINGS 21:24).
        if self.cammode == "rec":
            self.set_mode("play", self.cfg.sub_play_switch_s)
        delay = self.rng.uniform(self.cfg.settle_min, self.cfg.settle_max)
        self.settle_until = self.now() + delay
        until = self.settle_until

        def settled() -> None:
            if self.settle_until == until:
                self.settle_until = None
                self.set_mode("play")
                self.emit("X_Panasonic_Cam_Sync", "mod_Play")
        self.later(delay, settled)
        return 200, {"SID": sid, "TIMEOUT": "Second-%d" % secs}

    def unsubscribe(self, ip: str, headers: Dict[str, str]) -> int:
        sid = headers.get("SID", "")
        if sid in self.subs:
            del self.subs[sid]
            self.record("upnp", "UNSUBSCRIBE %s" % sid, "200", ip)
            return 200
        self.record("upnp", "UNSUBSCRIBE %s" % sid, "412", ip)
        return 412

    def browse(self, ip: str, body: bytes) -> Tuple[int, str]:
        from .card import SOAP_FAULT
        vals: Dict[str, str] = {}
        try:
            for el in ET.fromstring(body).iter():
                vals[el.tag.split("}", 1)[-1]] = (el.text or "").strip()
        except ET.ParseError:
            return 500, SOAP_FAULT % (402, "Invalid Args")
        oid = vals.get("ObjectID", "0")
        q = "Browse %s %s %s+%s" % (oid, vals.get("BrowseFlag"), vals.get("StartingIndex"),
                                   vals.get("RequestedCount"))
        if self.cammode != "play" or self.now() - self.play_since < self.cfg.cds_ready_s:
            self.record("upnp", q, "701 (not in play)", ip)
            return 500, SOAP_FAULT % (701, "No such object")
        try:
            start = int(vals.get("StartingIndex") or 0)
            count = int(vals.get("RequestedCount") or 0)
        except ValueError:
            return 500, SOAP_FAULT % (402, "Invalid Args")
        base = "http://%s:%d" % (self.host, self.dl_port)
        text = self.card.browse(oid, vals.get("BrowseFlag", "BrowseDirectChildren"), start,
                                count, base, self.cfg.browse_max)
        if text is None:
            self.record("upnp", q, "701", ip)
            return 500, SOAP_FAULT % (701, "No such object")
        self.record("upnp", q, "200", ip)
        return 200, text

    def files_allowed(self) -> bool:
        return self.cammode == "play"

    # ============================================================ control API
    def body_action(self, action: str) -> Dict[str, Any]:
        """Simulate someone using the camera body."""
        ev = "X_Panasonic_Cam_Sync"
        if action == "play":
            self.set_mode("play")
            self.emit(ev, "mod_Play", 0.05)
        elif action == "rec":
            was_busy = self.body_busy
            self.body_busy = False
            self.set_mode("rec")
            if was_busy:
                self.emit(ev, "update", 0.05)
            self.emit(ev, "lens_Atta", 0.1)
            self.emit(ev, "mod_Rec", 0.2)
        elif action == "busy":
            self.body_busy = True
            self.stream_alive = False
            self.emit(ev, "busy", 0.05)
        elif action == "update":
            self.body_busy = False
            self.emit(ev, "update", 0.05)
        elif action == "lens_detach":
            self.emit(ev, "lens_Deta", 0.05)
        elif action == "lens_attach":
            self.emit(ev, "lens_Atta", 0.05)
        else:
            raise ValueError("unknown body action %r" % action)
        self.record("body", action, "ok")
        return {"ok": True, "action": action}

    def snapshot(self) -> Dict[str, Any]:
        return {
            "name": self.ident.friendly_name, "serial": self.ident.serial, "mac": self.ident.mac,
            "model": self.ident.model, "host": self.host,
            "ports": {"http": self.http_port, "upnp": self.upnp_port, "dl": self.dl_port},
            "powered": self.powered, "cammode": self.cammode, "wedged": self.wedged,
            "wedge_reason": self.wedge_reason, "subscribed": self.subscribed,
            "settling": self.in_settle(),
            "settle_left": round((self.settle_until or 0) - self.now(), 2) if self.in_settle() else 0,
            "busy": self.is_busy(), "body_busy": self.body_busy, "recording": self.recording,
            "sessions": {k: {"uuid": s.uuid, "name": s.name, "state": s.state}
                         for k, s in self.sessions.items()},
            "dropped": sorted(self.dropped),
            "subscriptions": [{"sid": s.sid, "callback": s.callback, "seq": s.seq}
                              for s in self.subs.values()],
            "stream": {"open": self.stream_open, "alive": self.stream_alive,
                       "target": list(self.stream_target) if self.stream_target else None,
                       "frames_sent": self.frames_sent},
            "interval": {k: v for k, v in self.interval.items() if k not in ("task", "group")},
            "card": dict(zip(("total", "top_level"), self.card.counts())),
            "settings": {k: v[0] for k, v in self.settings.items()},
            "config": asdict(self.cfg),
            "counters": self.counters,
            "events": list(self.event_log)[-30:],
        }
