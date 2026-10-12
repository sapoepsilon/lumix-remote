"""Bluetooth LE control of a Lumix body (DC-S5 fw 2.03; LUMIX Sync protocol).

What this does: find the camera, connect, run the LUMIX Sync registration
(notify subscriptions, then write the client UUID and a friendly name -- the
same order LUMIX Sync 2.0.17 uses; see docs/bluetooth-research.md 1c), set the
clock, and ask the camera to bring Wi-Fi up in "Via Network" remote-shooting
mode, which also powers the body up from OFF when Remote Wakeup is on.

A central the camera doesn't know is cut off (HCI 0x13) on its first *read*;
registration writes come first. Whether macOS can complete the bond is still
being tested (section 1b of the research doc).

bleak needs Python >= 3.10 + pyobjc on macOS, while the server's .venv may be
an older Python, so the server runs this module as a subprocess through
``bin/lumix-ble`` (JSON on stdout) instead of importing bleak in-process.
Everything up to the bleak import is plain 3.9-compatible Python.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import re
import struct
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("lumix.ble")

PANASONIC_CID = 0x003A
NAME_RE = re.compile(r"^[A-Z0-9]{2,6}-[0-9A-F]{6}$")  # e.g. S5-XXXXXX


def _u(short: str, tail: str) -> str:
    return "%s-%s" % (short, tail)


_T = "0002a5d5c51b"

# --- service 054ac620-3214-11e6-ac0d (advertised) -----------------------
SVC_MAIN = "054ac620-3214-11e6-ac0d-" + _T
CH_INITIAL_INFORM = "03e23a31-a54c-40fa-a668-de9acdc910bb"  # read, 4 bytes BE: XOR key
CH_CHALLENGE = CH_INITIAL_INFORM                           # name used by the G9II notes
CH_DLNA_UUID = "8d08a420-3213-11e6-8aca-" + _T             # write, 16 bytes client id
CH_FRIENDLY_NAME = "cd7a71a0-3213-11e6-8f56-" + _T         # write, ASCII name
CH_RESPONSE1, CH_RESPONSE2 = CH_DLNA_UUID, CH_FRIENDLY_NAME
CH_CAMERA_SSID = "e206a5c0-3214-11e6-afe4-" + _T           # read, NUL-terminated
CH_CAMERA_AP_PASS = "c97cf1a5-3c03-4290-8c1b-9e74b9500f54"  # read, XOR(INITIAL_INFORM)
CH_UUID_APS = "bb5d2a8c-5d3b-43ad-bfc3-470cad55c1ca"        # only on newer (G9II) bodies
CH_WIFI_START = "e182ec40-3213-11e6-ab07-" + _T            # write, 1 byte
CH_WIFI_START_ACK = "e182ec41-3213-11e6-ab07-" + _T        # notify
CH_MAIN_NOTIFY2 = "e182ec42-3213-11e6-ab07-" + _T          # notify
CH_MAIN_NOTIFY3 = "e182ec43-3213-11e6-ab07-" + _T          # notify
# --- other services -----------------------------------------------------
CH_GPS = "daff1bc0-3216-11e6-91c8-" + _T                   # write (svc 1d74afe0)
CH_GPS_NOTIFY = "37701b80-32a7-11e6-bb83-" + _T
CH_CLOCK = "ead55e60-3216-11e6-a42e-" + _T                 # write (svc 34738720)
CH_CLOCK_NOTIFY = "4cf487c0-32a7-11e6-a50d-" + _T
CH_WIFI_CMD = "0d6f1880-3217-11e6-a4cb-" + _T              # write (svc 5ae57260)
CH_WIFI_CMD_ACK = "18345be1-3217-11e6-b56c-" + _T          # notify
CH_WIFI_SSID = "18345be0-3217-11e6-b56c-" + _T             # write, 32 bytes
CH_STATE_NOTIFY = "48669da0-3217-11e6-b08d-" + _T          # notify (svc 6b005f20)

# LUMIX Sync enables these notifications, in this order, before registering.
REGISTER_NOTIFY = [
    CH_CLOCK_NOTIFY, CH_GPS_NOTIFY, CH_WIFI_CMD_ACK, CH_STATE_NOTIFY,
    CH_MAIN_NOTIFY3, "da534d0a-63a3-447c-a889-aab701906af2", "d611b4a0-3217-11e6-8d98-" + _T,
    CH_WIFI_START_ACK, CH_MAIN_NOTIFY2, "7be5fe6e-475b-11e7-a919-92ebcb67fe33",
]

# Advertisement state byte (manufacturer data[0]), names from LUMIX Sync.
ADV_STATES = {0: "normal", 1: "sleep", 2: "wakeup", 3: "pairing", 4: "sleep_pow_off",
              5: "sleep_pow_on", 6: "sleep_pow_off_fast", 7: "sleep_pow_on_fast",
              8: "sleep_pow_on_autotrans", 9: "sleep_pow_off_fast_autotrans",
              10: "sleep_pow_on_fast_autotrans"}

# WIFI_CONNECT_TYPE values (CH_WIFI_CMD).
WIFI_SOFTAP, WIFI_STA, WIFI_STA_SSID_SET = 1, 2, 3

# G9II ("APS" bodies) answer constants from the Aikhjarto capture. Not used by
# S5-generation bodies, which write the plain client UUID instead.
_K1 = (0x49454D10, 0x10000130, 0x02018000, 0x450200A0, 0xFFFFFF18)
_K2 = (0x35504603, 0x00000000, 0x00000000, 0x00000000, 0xFFFFFF00)


def login_responses(challenge: bytes) -> Tuple[bytes, bytes]:
    """Return the two 20-byte answers for a 4-byte challenge."""
    if len(challenge) != 4:
        raise ValueError("challenge must be 4 bytes, got %d" % len(challenge))
    n = struct.unpack(">I", challenge)[0]
    return (struct.pack(">5I", *[n ^ k for k in _K1]), struct.pack(">5I", *[n ^ k for k in _K2]))


def client_uuid_bytes() -> bytes:
    """16-byte LUMIX Sync client id; same identity as the Wi-Fi req_acc uuid
    (lumix.camera._client_uuid): 4D454930-0100-1000-8001-<12 hex of the MAC>."""
    import uuid as _uuid
    return bytes.fromhex("4D454930010010008001%012X" % (_uuid.getnode() & 0xFFFFFFFFFFFF))


def clock_payload(now: Optional[_dt.datetime] = None) -> bytes:
    """10 bytes: year u16le, month, day, hour, minute, second, dst(0), utc offset minutes i16le."""
    now = now or _dt.datetime.now().astimezone()
    off = now.utcoffset() or _dt.timedelta(0)
    dst = now.dst() or _dt.timedelta(0)
    return struct.pack("<HBBBBBBh", now.year, now.month, now.day, now.hour, now.minute,
                       now.second, 1 if dst else 0, int(off.total_seconds() // 60))


def ssid_payload(ssid: str) -> bytes:
    raw = ssid.encode("utf-8")
    if len(raw) > 32:
        raise ValueError("SSID longer than 32 bytes")
    return raw.ljust(32, b"\0")


CONFIG_PATH = None  # set lazily: ~/.config/lumix-remote/ble.json (LUMIX_REMOTE_HOME overrides the dir)
CH_CAMERA_STA_SSID = "9c781c60-3218-11e6-9932-" + _T     # read: 1 byte + saved home SSID (S5)


def _config_path() -> str:
    import os
    d = os.path.expanduser(os.environ.get("LUMIX_REMOTE_HOME", "~/.config/lumix-remote"))
    return os.path.join(d, "ble.json")


def load_config() -> Dict[str, Any]:
    import json
    try:
        with open(_config_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(**kw: Any) -> None:
    import json
    import os
    cfg = load_config()
    cfg.update({k: v for k, v in kw.items() if v is not None})
    os.makedirs(os.path.dirname(_config_path()), exist_ok=True)
    with open(_config_path(), "w") as f:
        json.dump(cfg, f, indent=1)


def parse_sta_ssid(raw: bytes) -> Optional[str]:
    """9c781c60 value: 1 status/flag byte, then the NUL-terminated SSID."""
    if len(raw) < 2:
        return None
    s = raw[1:].split(b"\0", 1)[0].decode("utf-8", "replace").strip()
    return s or None


def current_ssid() -> Optional[str]:
    """Home network for "join network". Not secret. Order: LUMIX_HOME_SSID,
    ~/.config/lumix-remote/ble.json "home_ssid" (saved from the camera's own
    9c781c60 value by register/wake), the SSID macOS reports (redacted without Location permission), then the
    first entry of the Wi-Fi preferred-networks list."""
    import os
    import subprocess
    env = os.environ.get("LUMIX_HOME_SSID") or load_config().get("home_ssid")
    if env:
        return env
    try:
        out = subprocess.run(["ipconfig", "getsummary", "en0"], capture_output=True, text=True,
                             timeout=5).stdout
        m = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.M)
        if m and "redacted" not in m.group(1):
            return m.group(1).strip()
    except Exception:
        pass
    try:
        out = subprocess.run(["networksetup", "-listpreferredwirelessnetworks", "en0"],
                             capture_output=True, text=True, timeout=5).stdout.splitlines()
        nets = [x.strip() for x in out[1:] if x.strip()]
        if nets:
            return nets[0]
    except Exception:
        pass
    return None


# ------------------------------------------------------------------ bleak
def _bleak():
    import bleak  # noqa: F401  (imported lazily: needs Python >= 3.10 + pyobjc)
    return bleak


def _is_lumix(name: str, mfr: Dict[int, bytes], services: Optional[List[str]] = None) -> bool:
    """Lumix bodies advertise the 054ac620 service; other Panasonic gear also
    uses company id 0x003A, so the manufacturer id alone is not enough."""
    if services and SVC_MAIN in [x.lower() for x in services]:
        return True
    return PANASONIC_CID in mfr and bool(name and NAME_RE.match(name))


async def scan(timeout: float = 8.0, name: Optional[str] = None,
               stop_on_first: bool = False) -> List[Dict[str, Any]]:
    """Advertising Lumix bodies: name, address, rssi, manufacturer data."""
    bleak = _bleak()
    found: Dict[str, Dict[str, Any]] = {}

    def cb(dev, adv):
        n = adv.local_name or dev.name or ""
        if not _is_lumix(n, adv.manufacturer_data, adv.service_uuids) or (name and n != name):
            return
        m = adv.manufacturer_data.get(PANASONIC_CID)
        found[dev.address] = {"name": n, "address": dev.address, "rssi": adv.rssi,
                              "mfr": m.hex() if m is not None else None,
                              "state_byte": m[0] if m else None,
                              "state": ADV_STATES.get(m[0]) if m else None, "_dev": dev}
    async with bleak.BleakScanner(cb):
        t0 = time.time()
        while time.time() - t0 < timeout:
            await asyncio.sleep(0.25)
            if stop_on_first and found:
                break
    return list(found.values())


async def find(name: Optional[str] = None, timeout: float = 15.0):
    """BLEDevice of the first matching camera, or None."""
    res = await scan(timeout, name, stop_on_first=True)
    return res[0]["_dev"] if res else None


class LumixBle:
    """One BLE session. Use as ``async with LumixBle(name) as cam: ...``."""

    def __init__(self, name: Optional[str] = None, scan_timeout: float = 20.0,
                 on_event: Optional[Callable[[str], None]] = None):
        self.name = name
        self.scan_timeout = scan_timeout
        self.client = None
        self.notes: List[Tuple[float, str, str]] = []
        self._waiters: Dict[str, asyncio.Queue] = {}
        self._on_event = on_event or (lambda s: log.info("%s", s))
        self.t0 = time.time()
        self.disconnected = asyncio.Event()

    def ev(self, text: str) -> None:
        self._on_event("[%6.2fs] %s" % (time.time() - self.t0, text))

    async def __aenter__(self) -> "LumixBle":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def connect(self) -> None:
        bleak = _bleak()
        dev = await find(self.name, self.scan_timeout)
        if dev is None:
            raise RuntimeError("camera not advertising (name=%s)" % (self.name or "any Lumix"))
        self.ev("found %s" % (dev.name or dev.address))

        def on_disc(_c):
            self.ev("disconnected")
            self.disconnected.set()
        self.client = bleak.BleakClient(dev, disconnected_callback=on_disc, timeout=20)
        await self.client.connect()
        self.ev("connected")

    async def close(self) -> None:
        if self.client is not None and self.client.is_connected:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    # -- primitives
    async def read(self, uuid: str, timeout: float = 10.0) -> bytes:
        return bytes(await asyncio.wait_for(self.client.read_gatt_char(uuid), timeout))

    async def write(self, uuid: str, data: bytes, response: bool = True) -> None:
        self.ev("write %s %s" % (uuid[:8], data.hex()))
        await self.client.write_gatt_char(uuid, data, response=response)

    async def subscribe(self, uuid: str) -> None:
        q: asyncio.Queue = asyncio.Queue()
        self._waiters[uuid] = q

        def cb(_ch, data: bytearray, uuid=uuid):
            self.notes.append((time.time() - self.t0, uuid, bytes(data).hex()))
            self.ev("notify %s %s" % (uuid[:8], bytes(data).hex()))
            q.put_nowait(bytes(data))
        await self.client.start_notify(uuid, cb)

    async def wait_notify(self, uuid: str, timeout: float = 5.0) -> Optional[bytes]:
        q = self._waiters.get(uuid)
        if q is None:
            return None
        try:
            return await asyncio.wait_for(q.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def readable(self) -> List[str]:
        return [c.uuid for s in self.client.services for c in s.characteristics if "read" in c.properties]

    def notifiable(self) -> List[str]:
        return [c.uuid for s in self.client.services for c in s.characteristics
                if "notify" in c.properties or "indicate" in c.properties]

    async def read_all(self) -> Dict[str, str]:
        """Every readable char except the camera AP password. Call register()
        first. Some (69e4d998, a09fad60, 20f5fffc) never answer on the S5."""
        out: Dict[str, str] = {}
        for u in self.readable():
            if u == CH_CAMERA_AP_PASS:
                out[u] = "skipped (camera AP password)"
                continue
            if self.disconnected.is_set():
                break
            try:
                out[u] = (await self.read(u, 6)).hex()
            except Exception as e:
                out[u] = "ERR %r" % e
        return out

    # -- protocol
    async def subscribe_all(self) -> None:
        for u in self.notifiable():
            try:
                await self.subscribe(u)
            except Exception as e:
                self.ev("subscribe %s failed: %r" % (u[:8], e))

    async def register(self, name: str = "LumixRemote") -> None:
        """LUMIX Sync registration for S5-gen bodies: notifications, then client
        UUID and friendly name writes. Must come before any read."""
        failed = 0
        for u in REGISTER_NOTIFY:
            if self.disconnected.is_set():
                raise RuntimeError("camera dropped the link during registration")
            try:
                await self.subscribe(u)
            except Exception:
                # macOS finds no CCCDs on the S5 ("attribute not found"); the
                # camera notifies anyway, so this is harmless.
                failed += 1
        if failed:
            self.ev("notify subscriptions refused: %d/%d (expected on macOS)" % (failed, len(REGISTER_NOTIFY)))
        await self.write(CH_DLNA_UUID, client_uuid_bytes())
        await self.write(CH_FRIENDLY_NAME, name.encode("ascii", "replace")[:20])

    async def login(self) -> bytes:
        """Register, then read INITIAL_INFORM (the XOR key)."""
        await self.register()
        await self.read(CH_CAMERA_SSID)   # LUMIX Sync reads this next; keep the order
        key = await self.read(CH_INITIAL_INFORM)
        self.ev("initial_inform %s" % key.hex())
        return key

    async def set_clock(self) -> None:
        await self.write(CH_CLOCK, clock_payload())

    async def wifi_join(self, ssid: Optional[str] = None, purpose: int = 1) -> None:
        """Bring Wi-Fi up joined to a known network (the camera keeps the
        password). Verified on the S5: SSID -> connect type 03 -> start 01."""
        if not ssid:
            try:
                ssid = parse_sta_ssid(await self.read(CH_CAMERA_STA_SSID, 8))
                if ssid:
                    save_config(home_ssid=ssid)
            except Exception as e:
                self.ev("camera STA ssid read: %s" % e)
        ssid = ssid or current_ssid()
        if not ssid:
            raise RuntimeError("no home SSID (set LUMIX_HOME_SSID)")
        await self.write(CH_WIFI_SSID, ssid_payload(ssid))
        await asyncio.sleep(0.5)
        await self.write(CH_WIFI_CMD, bytes([WIFI_STA_SSID_SET]))
        await asyncio.sleep(0.5)
        await self.write(CH_WIFI_START, bytes([purpose]))


# ------------------------------------------------------------- high level
INFO_CHARS = {
    "054ac621-3214-11e6-0001-" + _T: "info1",
    "054ac622-3214-11e6-0001-" + _T: "info2",
    "054ac623-3214-11e6-0001-" + _T: "info3",
}


def _printable(b: bytes) -> str:
    s = b.rstrip(b"\0")
    try:
        t = s.decode("ascii")
        if t and t.isprintable():
            return t
    except UnicodeDecodeError:
        pass
    return b.hex()


async def pair(name: Optional[str] = None, wait: float = 300.0,
               log: Callable[[str], None] = print) -> Dict[str, Any]:
    """Wait for the camera's Pairing standby (adv state 3) and run the LUMIX
    Sync registration, then check that a read survives (= camera accepted us)."""
    t0 = time.time()
    tries = 0
    last_err = ""
    while time.time() - t0 < wait:
        res = await scan(10.0, name)
        cam = next((r for r in res if r.get("state_byte") == 3), None)
        if cam is None:
            await asyncio.sleep(2)
            continue
        tries += 1
        try:
            async with LumixBle(name, scan_timeout=15, on_event=log) as c:
                await c.register()
                await asyncio.sleep(2.0)
                ssid = await c.read(CH_CAMERA_SSID, timeout=60)
                key = await c.read(CH_INITIAL_INFORM, timeout=30)
                if c.disconnected.is_set():
                    raise RuntimeError("dropped after registration")
                return {"ok": True, "tries": tries, "camera_ssid": _printable(ssid),
                        "initial_inform_len": len(key), "notifies": c.notes,
                        "elapsed_s": round(time.time() - t0, 1)}
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
            log("try %d: %s" % (tries, last_err))
            await asyncio.sleep(3)
    return {"ok": False, "tries": tries, "error": last_err or "camera never entered Pairing (state 3)"}


async def status(name: Optional[str] = None, log: Callable[[str], None] = print,
                 connect: bool = True) -> Dict[str, Any]:
    res = await scan(10.0, name)
    if not res:
        return {"ok": False, "advertising": False}
    adv = {k: v for k, v in res[0].items() if k != "_dev"}
    out: Dict[str, Any] = {"ok": True, "advertising": True, "adv": adv}
    if not connect:
        return out
    try:
        async with LumixBle(name, on_event=log) as c:
            await c.register()
            info = {}
            for u, label in INFO_CHARS.items():
                try:
                    info[label] = _printable(await c.read(u))
                except Exception as e:
                    info[label] = "ERR %s" % e
            out["info"] = info
            out["registered"] = not any(str(v).startswith("ERR") for v in info.values())
    except Exception as e:
        out["registered"] = False
        out["error"] = "%s: %s" % (type(e).__name__, e)
    return out


def _last_ip() -> Optional[str]:
    import json
    import os
    try:
        with open(os.path.expanduser("~/.config/lumix-remote/state.json")) as f:
            return json.load(f).get("last_ip")
    except (OSError, ValueError):
        return None


def ddd_ok(ip: str, timeout: float = 2.0) -> bool:
    """True when a Lumix answers http://ip:60606/Lumix/Server0/ddd (stdlib only)."""
    import urllib.request
    try:
        with urllib.request.urlopen("http://%s:60606/Lumix/Server0/ddd" % ip, timeout=timeout) as r:
            return r.status == 200 and b"Panasonic" in r.read()
    except Exception:
        return False


async def wait_wifi(ip: Optional[str], timeout: float) -> Optional[float]:
    """Seconds until the camera answers :60606 ddd at ip (None on timeout)."""
    if not ip:
        return None
    t0 = time.time()
    loop = asyncio.get_running_loop()
    while time.time() - t0 < timeout:
        if await loop.run_in_executor(None, ddd_ok, ip):
            return round(time.time() - t0, 1)
        await asyncio.sleep(1.0)
    return None


async def wake(name: Optional[str] = None, ssid: Optional[str] = None, wifi_wait: float = 90.0,
               camera_ip: Optional[str] = None, log: Callable[[str], None] = print) -> Dict[str, Any]:
    """Connect, register, sync clock, start Wi-Fi joined to the home network."""
    t0 = time.time()
    out: Dict[str, Any] = {"ok": False}
    ip = camera_ip or _last_ip()
    out["ip"] = ip
    took = None
    async with LumixBle(name, scan_timeout=30, on_event=log) as c:
        out["ble_connected_s"] = round(time.time() - t0, 1)
        await c.login()
        try:
            await c.set_clock()
        except Exception as e:
            log("clock: %r" % e)
        await c.wifi_join(ssid)
        out["wifi_cmd_s"] = round(time.time() - t0, 1)
        # Stay connected while the camera brings Wi-Fi up: in the run that came
        # up from Wi-Fi-off, the camera itself dropped BLE once Wi-Fi was up.
        took = await wait_wifi(ip, wifi_wait)
        out["notifies"] = c.notes
    out["wifi_up_s"] = round(time.time() - t0, 1) if took is not None else None
    out["ok"] = took is not None
    if not out["ok"]:
        out["error"] = "camera did not answer on %s:60606 within %ds" % (ip, wifi_wait)
    return out


def wifi_poweroff(ip: str, server: Optional[str], log: Callable[[str], None] = print) -> Dict[str, Any]:
    """camcmd poweroff, through the Lumix Remote server if it runs, else directly."""
    import json
    import urllib.request
    if server:
        try:
            req = urllib.request.Request(server.rstrip("/") + "/api/power/off", data=b"{\"confirm\": true}",
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                return {"via": "server", "reply": json.loads(r.read() or b"{}")}
        except Exception as e:
            log("server poweroff failed (%s); trying the camera directly" % e)
    import uuid as _uuid
    # Same client id as lumix.camera._client_uuid(), so the camera knows us already.
    cid = "4D454930-0100-1000-8001-%012X" % (_uuid.getnode() & 0xFFFFFFFFFFFF)
    base = "http://%s/cam.cgi?" % ip
    replies = []
    for q in ("mode=accctrl&type=req_acc&value=%s&value2=LumixRemote" % cid,
              "mode=camcmd&value=recmode",          # poweroff is err_reject in play mode
              "mode=camcmd&value=poweroff"):
        try:
            with urllib.request.urlopen(base + q, timeout=8) as r:
                replies.append(r.read().decode("utf-8", "replace")[:200])
        except Exception as e:
            replies.append("ERR %s" % e)
    return {"via": "direct", "reply": replies}


async def power_cycle(name: Optional[str] = None, camera_ip: Optional[str] = None,
                      server: Optional[str] = "http://127.0.0.1:8770", off_wait: float = 8.0,
                      wifi_wait: float = 90.0, ssid: Optional[str] = None,
                      log: Callable[[str], None] = print) -> Dict[str, Any]:
    ip = camera_ip or _last_ip()
    t0 = time.time()
    out: Dict[str, Any] = {"ok": False, "ip": ip}
    loop = asyncio.get_running_loop()
    if ip and await loop.run_in_executor(None, ddd_ok, ip):
        out["poweroff"] = await loop.run_in_executor(None, wifi_poweroff, ip, server, log)
        t1 = time.time()
        while time.time() - t1 < 20 and await loop.run_in_executor(None, ddd_ok, ip, 1.0):
            await asyncio.sleep(1.0)
        out["off_s"] = round(time.time() - t0, 1)
    else:
        out["poweroff"] = "skipped (camera not on Wi-Fi)"
    went_off = "off_s" in out and not (ip and ddd_ok(ip, 1.0))
    out["camera_went_off"] = went_off
    if "off_s" in out and not went_off:
        # Typical when the camera is wedged: every camcmd, poweroff included,
        # answers err_busy/err_reject. There is no BLE power-off command.
        out["error"] = "camera refused poweroff (wedged?); it stayed on"
        return out
    await asyncio.sleep(off_wait)
    w = await wake(name, ssid=ssid, wifi_wait=wifi_wait, camera_ip=ip, log=log)
    out["wake"] = w
    out["ok"] = bool(w.get("ok"))
    out["total_s"] = round(time.time() - t0, 1)
    return out


# ------------------------------------------------- first-time registration
def xor_decode(raw: bytes, key: int) -> str:
    """LUMIX Sync string de-obfuscation (camera AP password, etc.): per 4-byte
    block, big-endian word XOR key, emitted little-endian; the first byte of
    the first block is skipped and 0xFF ends the string. key 0 -> 0x9032782C."""
    if not raw or len(raw) % 4:
        return ""
    key = key or 0x9032782C
    out = bytearray()
    for i in range(0, len(raw), 4):
        b = struct.pack("<I", struct.unpack(">I", raw[i:i + 4])[0] ^ key)
        out += b[1:] if i == 0 else b
    if 0xFF in out:
        out = out[:out.index(0xFF)]
    return out.decode("latin-1").strip().strip("\0")


def _sh(*cmd: str, timeout: float = 60) -> str:
    import subprocess
    r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
    return (r.stdout + r.stderr).strip()


def _http(url: str, timeout: float = 4) -> str:
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        return "ERR %s" % e


async def first_register(name: Optional[str] = None, wait: float = 300.0, wifi_if: str = "en0",
                         device_name: str = "LumixRemote",
                         log: Callable[[str], None] = print) -> Dict[str, Any]:
    """One-time registration, the LUMIX Sync way (camera on Bluetooth > SET > Pairing):

    1. BLE: write client UUID + friendly name, read the camera's SoftAP SSID,
       INITIAL_INFORM and (XOR-obfuscated) AP password.
    2. This Mac's Wi-Fi joins the camera SoftAP (it leaves the home network for
       ~30-60 s; there is no way around it without a second interface).
    3. accctrl req_acc at 192.168.54.1 with the same MEI0 UUID -> camera says
       "Pairing completed".
    4. Forget the camera AP and toggle Wi-Fi so macOS rejoins the home network.
    The AP password is used in-process only and never logged or returned.
    """
    import uuid as _uuid
    t0 = time.time()
    out: Dict[str, Any] = {"ok": False}
    cid = "4D454930-0100-1000-8001-%012X" % (_uuid.getnode() & 0xFFFFFFFFFFFF)
    while time.time() - t0 < wait:
        res = await scan(5.0, name)
        if any(r.get("state_byte") == 3 for r in res):
            break
    else:
        out["error"] = "camera never entered Pairing (MENU > Setup > Bluetooth > SET > Pairing)"
        return out
    ap_ssid = None
    c = LumixBle(name, scan_timeout=20, on_event=log)
    await c.connect()
    try:
        await c.register(device_name)
        ap_ssid = _printable(await c.read(CH_CAMERA_SSID, 20))
        key = struct.unpack(">I", await c.read(CH_INITIAL_INFORM, 20))[0]
        pw = xor_decode(await c.read(CH_CAMERA_AP_PASS, 20), key)
        out["camera_ap"] = ap_ssid
        out["ap_password_set"] = bool(pw)
        out["home_ip_before"] = _sh("ipconfig", "getifaddr", wifi_if)
        log("joining camera AP %s" % ap_ssid)
        args = ["networksetup", "-setairportnetwork", wifi_if, ap_ssid] + ([pw] if pw else [])
        r = _sh(*args)
        if r:
            log("networksetup: %s" % r.replace(pw, "***") if pw else r)
        for _ in range(40):
            if _sh("ipconfig", "getifaddr", wifi_if).startswith("192.168.54."):
                break
            await asyncio.sleep(1)
        reply = ""
        for _ in range(40):
            reply = _http("http://192.168.54.1/cam.cgi?mode=accctrl&type=req_acc&value=%s&value2=%s"
                          % (cid, device_name))
            if reply.startswith("ok,"):
                break
            await asyncio.sleep(1.5)
        out["req_acc"] = reply.strip()[:60]
        out["ok"] = reply.startswith("ok,")
        await asyncio.sleep(3)
    finally:
        try:
            await c.close()
        except Exception:
            pass
        if ap_ssid:
            _sh("networksetup", "-removepreferredwirelessnetwork", wifi_if, ap_ssid)
            _sh("networksetup", "-setairportpower", wifi_if, "off")
            _sh("networksetup", "-setairportpower", wifi_if, "on")
            for _ in range(60):
                ip = _sh("ipconfig", "getifaddr", wifi_if)
                if ip and not ip.startswith("192.168.54."):
                    break
                await asyncio.sleep(1)
            out["home_ip_after"] = _sh("ipconfig", "getifaddr", wifi_if)
    if out["ok"]:
        save_config(camera_name=name or ap_ssid, registered_at=int(time.time()))
    out["elapsed_s"] = round(time.time() - t0, 1)
    return out
