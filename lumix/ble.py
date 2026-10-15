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
import contextlib
import datetime as _dt
import logging
import os
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
# CH_WIFI_START purpose LUMIX Sync uses for "Remote shooting" (5 = SoftAP variant
# for the *_fast sleep states). Proven on the S5 with Remote Wakeup, 2026-10-12.
PURPOSE_REMOTE = 3
PURPOSE_SOFTAP = 5     # with connect type 01; HomeActivity.java:4736, I5/g.java:936-967

# Advertised states (docs/ble-power-protocol.md section 2). In a sleep state LUMIX
# Sync wakes the body with one connection and runs the Wi-Fi request on a second
# one once the camera advertises "wakeup" (HomeActivity.java:1230-1259).
SLEEP_STATES = frozenset({1, 4, 5, 6, 7, 8, 9, 10})
FAST_STATES = frozenset({6, 7, 9, 10})       # X0/Y0, P5/b.java:819-823
AWAKE_STATES = frozenset({0, 2})             # normal, wakeup

# Connecting to a camera that sleeps with the switch OFF takes longer: 24-27 s
# for CoreBluetooth to bring the link up on the S5 (bleak's old 20 s cut it off).
CONNECT_TIMEOUT = 45.0
# How long to wait for e182ec41 (Wi-Fi start result) after the start write. The
# app has no fixed wait (it reacts whenever the notify comes); 10 s is ours.
START_ACK_WAIT = 10.0
# Before any notification has arrived on the link we don't know whether this host
# gets them (macOS refuses every CCCD write on the S5, yet the camera's notifies
# still arrive: 2026-10-15 09:09:40 "notify e182ec41 01" after 0/10 subscriptions).
# A short wait costs little and catches the refusal (it came 0.21 s after the write).
START_ACK_BLIND_WAIT = 2.0
# A BLE wake while the camera is on Wi-Fi does harm: twice it made the S5 drop the
# session it had with us (2026-10-15 08:29:26 e182ec40=03 -> 08:29:30 getstate
# err_reject; 08:53:13 -> 08:53:15), once it refused (e182ec41 = 01 at 09:09:40,
# the camera stuck but on Wi-Fi) and then went off the network for good.
ON_WIFI_ERROR = ("the camera is on Wi-Fi already (it answers at its address), so a Bluetooth wake can't help: "
                 "it makes the camera drop the connection or refuse. Wait for the reconnect; if the camera "
                 "is stuck, switch it off and on by hand")


class BleStepError(RuntimeError):
    """A BLE failure tagged with the step it happened in, so a caller (the web
    UI) can say "connect: ..." instead of a bare "TimeoutError:"."""

    def __init__(self, step: str, message: str, need_pair: bool = False):
        super().__init__("%s: %s" % (step, message))
        self.step = step
        self.need_pair = need_pair


REFUSED_HINT = ("The camera said no to remote wake-up (e182ec41 = 01; LUMIX Sync shows \"Due to the state of "
                "the camera, this function cannot be used. Make sure the camera is not in operation\"). "
                "Seen on the S5 when it was still on Wi-Fi with a session open, and stuck (2026-10-15 "
                "09:09). If the camera doesn't answer on Wi-Fi either, switch it off and on by hand; "
                "otherwise close its menu or playback and try again.")


class BleRefused(BleStepError):
    """e182ec41 = 01 after the Wi-Fi start write: the camera refused
    (ON_BT_CANNOT_REMOTE_WAKEUP, I5/g.java:497-509, HomeActivity.java:1097-1121)."""

    def __init__(self, message: str = "camera refused remote wake-up (camera state)"):
        super().__init__("wifi", message)
        self.refused = True


def trace_line(text: str, t0: Optional[float] = None) -> str:
    """'ble.trace <wall clock> [+rel] text': one grep finds the experiment in
    /tmp/lumix-server.log (the server logs lumix-ble stderr as 'ble: ...')."""
    now = time.time()
    ts = _dt.datetime.fromtimestamp(now).strftime("%H:%M:%S.%f")[:-3]
    rel = (" +%.2fs" % (now - t0)) if t0 is not None else ""
    return "ble.trace %s%s %s" % (ts, rel, text)


def describe(e: BaseException) -> str:
    """One-line error text that is never empty ("TimeoutError: " was)."""
    if isinstance(e, BleStepError):
        return str(e)
    msg = str(e).strip()
    if isinstance(e, asyncio.TimeoutError) or type(e).__name__ == "TimeoutError":
        return "timed out" + (" (%s)" % msg if msg else "")
    return "%s: %s" % (type(e).__name__, msg) if msg else type(e).__name__


def error_fields(e: BaseException, step: Optional[str] = None) -> Dict[str, Any]:
    """{"error", "step", "need_pair"} for a lumix-ble JSON result."""
    if not isinstance(e, BleStepError) and step:
        e = BleStepError(step, describe(e))
    out: Dict[str, Any] = {"error": describe(e)}
    if isinstance(e, BleStepError):
        out["step"] = e.step
        if e.need_pair:
            out["need_pair"] = True
    if isinstance(e, BleRefused):
        out["refused"] = True
        out["hint"] = REFUSED_HINT
    if isinstance(e, BtleServerStuck):
        out["btle_stuck"] = True
    return out


# ------------------------------------------------- macOS Bluetooth daemon guard
# /usr/sbin/BTLEServer has twice stuck at ~100% CPU for hours after BLE wake
# attempts, which kills a Bluetooth mouse. Never start (or keep poking) the radio
# while it is in that state.
BTLE_DAEMON = "BTLEServer"
BTLE_CPU_LIMIT = 80.0          # percent; above this in both samples = stuck
BTLE_SAMPLE_GAP = 1.0          # seconds between the two samples
BTLE_STUCK_MESSAGE = ("macOS Bluetooth daemon (BTLEServer) is stuck at 100% CPU; "
                      "run: sudo killall -9 BTLEServer")


class BtleServerStuck(BleStepError):
    """BTLEServer is above BTLE_CPU_LIMIT in two samples 1 s apart. The message
    is the exact user-facing text (no "step:" prefix)."""

    def __init__(self, message: str = BTLE_STUCK_MESSAGE):
        RuntimeError.__init__(self, message)
        self.step = "bluetooth"
        self.need_pair = False
        self.btle_stuck = True


def btle_cpu() -> Optional[float]:
    """Highest %CPU of any BTLEServer process (`ps -Ao pcpu,comm`); None when
    it isn't running or ps can't be read (never blocks a BLE operation)."""
    import subprocess
    try:
        out = subprocess.run(["ps", "-Ao", "pcpu,comm"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    best: Optional[float] = None
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or parts[1].rsplit("/", 1)[-1] != BTLE_DAEMON:
            continue
        try:
            v = float(parts[0])
        except ValueError:
            continue
        best = v if best is None else max(best, v)
    return best


async def check_btle_server(gap: float = BTLE_SAMPLE_GAP) -> None:
    """Raise BtleServerStuck when BTLEServer's CPU is above 80% in two samples
    ``gap`` s apart. A first sample at or under the limit already settles it, so
    the usual cost is one `ps`. Call before any BLE scan or connect."""
    if os.environ.get("LUMIX_BLE_SKIP_BTLE_CHECK") == "1":
        return
    loop = asyncio.get_running_loop()
    first = await loop.run_in_executor(None, btle_cpu)
    if first is None or first <= BTLE_CPU_LIMIT:
        return
    await asyncio.sleep(gap)
    second = await loop.run_in_executor(None, btle_cpu)
    if second is not None and second > BTLE_CPU_LIMIT:
        raise BtleServerStuck()

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


def _client_id() -> str:
    """The selected camera's client UUID (cameras.json), else this machine's
    default 4D454930-0100-1000-8001-<12 hex of the MAC>; the same id the Wi-Fi
    req_acc uses, so the camera knows us on both links."""
    try:
        reg, cid = _camera_entry()
        e = reg.get(cid) if cid else None
        if e and e.get("client_uuid"):
            return str(e["client_uuid"])
    except Exception:
        pass
    from .registry import default_client_uuid
    return default_client_uuid()


def client_uuid_bytes() -> bytes:
    """16-byte LUMIX Sync client id (see _client_id)."""
    return bytes.fromhex(_client_id().replace("-", ""))


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


CH_CAMERA_STA_SSID = "9c781c60-3218-11e6-9932-" + _T     # read: 1 byte + saved home SSID (S5)

# Per-camera BLE data lives in the camera's entry of ~/.config/lumix-remote/cameras.json
# ("ble": name, address, identity, home_ssid, registered_at). LUMIX_CAMERA_ID picks the
# camera (the server sets it for every lumix-ble subprocess); default = the active one.
_BLE_KEYS = {"camera_name": "name", "address": "address", "identity": "identity",
             "home_ssid": "home_ssid", "registered_at": "registered_at"}


def _camera_entry() -> "Tuple[Any, Optional[str]]":
    import os
    from . import registry
    reg = registry.Registry()
    registry.migrate(reg)      # older single-camera ble.json/state.json
    return reg, os.environ.get("LUMIX_CAMERA_ID") or reg.active_id()


def load_config() -> Dict[str, Any]:
    """BLE data of the selected camera, with the old ble.json key names."""
    reg, cid = _camera_entry()
    e = reg.get(cid) if cid else None
    if not e:
        return {}
    b = e.get("ble") or {}
    out = {old: b.get(new) for old, new in _BLE_KEYS.items() if b.get(new) is not None}
    if e.get("fixed_ip") or e.get("last_ip"):
        out["last_ip"] = e.get("fixed_ip") or e.get("last_ip")
    return out


def save_config(**kw: Any) -> None:
    reg, cid = _camera_entry()
    ble = {_BLE_KEYS[k]: v for k, v in kw.items() if k in _BLE_KEYS}
    if not ble:
        return
    if cid is None or reg.get(cid) is None:
        name = kw.get("camera_name")
        m = NAME_RE.match(name or "")
        reg.add(name.split("-")[0] if m else "Camera", ble=ble)
        return
    reg.update(cid, ble=ble)


def mark_registered(name: Optional[str] = None, address: Optional[str] = None) -> bool:
    """Self-heal: a BLE login whose reads succeeded proves the camera knows our
    client UUID, so record that in the camera's entry (registered_at, identity,
    name, address). Covers pairings that ran before registered_at was saved.
    Keeps an existing registered_at. Returns True when something was written."""
    try:
        cur = load_config()
        kw: Dict[str, Any] = {}
        if not cur.get("registered_at"):
            kw["registered_at"] = int(time.time())
        cid = _client_id()
        if cur.get("identity") != cid:
            kw["identity"] = cid
        if name and NAME_RE.match(name) and cur.get("camera_name") != name:
            kw["camera_name"] = name
        if address and cur.get("address") != address:
            kw["address"] = address
        if kw:
            save_config(**kw)
        return bool(kw)
    except Exception as e:  # never fail a wake/status over bookkeeping
        log.warning("could not save BLE registration: %s", e)
        return False


def parse_sta_ssid(raw: bytes) -> Optional[str]:
    """9c781c60 value: 1 status/flag byte, then the NUL-terminated SSID."""
    if len(raw) < 2:
        return None
    s = raw[1:].split(b"\0", 1)[0].decode("utf-8", "replace").strip()
    return s or None


def current_ssid() -> Optional[str]:
    """Home network for "join network". Not secret. Order: LUMIX_HOME_SSID,
    the camera's "home_ssid" in cameras.json (saved from the camera's own
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
SCAN_START_TIMEOUT = 15.0     # scanner.start() hung on a sick BTLEServer
STOP_TIMEOUT = 8.0            # scanner.stop() / client.disconnect()

# Every scanner/client this process has started and not yet stopped. release_all()
# (end of each wake / lumix-ble command) stops whatever a failure path missed.
_LIVE_SCANNERS: set = set()
_LIVE_CLIENTS: set = set()


async def _stop_scanner(scanner: Any) -> None:
    try:
        await asyncio.wait_for(scanner.stop(), STOP_TIMEOUT)
    except BaseException as e:      # incl. a second cancel: the scan must still be forgotten
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        log.warning("scanner.stop failed: %s", describe(e))
    finally:
        _LIVE_SCANNERS.discard(scanner)


async def _disconnect_client(client: Any) -> None:
    try:
        await asyncio.wait_for(client.disconnect(), STOP_TIMEOUT)
    except BaseException as e:
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        log.warning("client.disconnect failed: %s", describe(e))
    finally:
        _LIVE_CLIENTS.discard(client)


async def release_all() -> None:
    """Stop every scan and disconnect every client still registered."""
    for sc in list(_LIVE_SCANNERS):
        await _stop_scanner(sc)
    for cl in list(_LIVE_CLIENTS):
        await _disconnect_client(cl)


def _bleak():
    import bleak  # noqa: F401  (imported lazily: needs Python >= 3.10 + pyobjc)
    return bleak


def _is_lumix(name: str, mfr: Dict[int, bytes], services: Optional[List[str]] = None) -> bool:
    """Lumix bodies advertise the 054ac620 service; other Panasonic gear also
    uses company id 0x003A, so the manufacturer id alone is not enough."""
    if services and SVC_MAIN in [x.lower() for x in services]:
        return True
    return PANASONIC_CID in mfr and bool(name and NAME_RE.match(name))


def adv_matches(n: str, mfr: Optional[bytes], name: Optional[str], mfr_addr: Optional[str]) -> bool:
    """Is this advertisement our camera? The BT address the OS reports can
    rotate (two resolvable addresses at once on 2026-10-12 23:22), so match the
    name or the address bytes the camera puts in its manufacturer data (bytes
    after the state byte). LUMIX Sync re-finds the camera by a "publicAddress",
    not the OS address (HomeActivity.java:1234-1240); that it is these bytes is
    an inference. No filter: any Lumix."""
    if not name and not mfr_addr:
        return True
    if name and n == name:
        return True
    return bool(mfr_addr and mfr is not None and len(mfr) > 1 and mfr[1:].hex() == mfr_addr.lower())


async def scan(timeout: float = 8.0, name: Optional[str] = None,
               stop_on_first: bool = False, mfr_addr: Optional[str] = None,
               stop_on_state: bool = False) -> List[Dict[str, Any]]:
    """Advertising Lumix bodies: name, address, rssi, manufacturer data.
    ``stop_on_state``: stop at the first match that carries a state byte (an
    advertisement without manufacturer data doesn't count).
    Refuses (BtleServerStuck) while macOS's BTLEServer is stuck at 100% CPU."""
    await check_btle_server()
    bleak = _bleak()
    found: Dict[str, Dict[str, Any]] = {}

    def cb(dev, adv):
        n = adv.local_name or dev.name or ""
        m = adv.manufacturer_data.get(PANASONIC_CID)
        if not _is_lumix(n, adv.manufacturer_data, adv.service_uuids) or not adv_matches(n, m, name, mfr_addr):
            return
        found[dev.address] = {"name": n, "address": dev.address, "rssi": adv.rssi,
                              "mfr": m.hex() if m is not None else None,
                              "mfr_addr": m[1:].hex() if m is not None and len(m) > 1 else None,
                              "state_byte": m[0] if m else None,
                              "state": ADV_STATES.get(m[0]) if m else None, "_dev": dev}
    scanner = bleak.BleakScanner(cb)
    _LIVE_SCANNERS.add(scanner)
    try:
        try:
            await asyncio.wait_for(scanner.start(), SCAN_START_TIMEOUT)
        except asyncio.TimeoutError:
            raise BleStepError("scan", "Bluetooth scan didn't start within %.0f s" % SCAN_START_TIMEOUT)
        t0 = time.time()
        while time.time() - t0 < timeout:
            await asyncio.sleep(0.25)
            if stop_on_first and found:
                break
            if stop_on_state and any(f["state_byte"] is not None for f in found.values()):
                break
    finally:
        # also on error and on cancellation (a wake timeout): never leave a scan running
        await _stop_scanner(scanner)
    return list(found.values())


async def find(name: Optional[str] = None, timeout: float = 15.0, mfr_addr: Optional[str] = None):
    """BLEDevice of the first matching camera, or None."""
    res = await scan(timeout, name, stop_on_first=True, mfr_addr=mfr_addr)
    return res[0]["_dev"] if res else None


async def wait_state(name: Optional[str], mfr_addr: Optional[str], want: "frozenset[int]" = AWAKE_STATES,
                     timeout: float = 40.0, every: float = 1.0, log: Callable[[str], None] = print,
                     t0: Optional[float] = None) -> Optional[int]:
    """Poll the advertisement (one ~1 s scan per round) until the camera
    advertises a state in ``want``; return it, else the last state seen (None
    when it never showed up). Every change is logged as ble.trace."""
    start = time.time()
    last: Optional[Tuple[int, ...]] = None
    seen: Optional[int] = None
    while time.time() - start < timeout:
        try:
            res = await scan(every, name, mfr_addr=mfr_addr)
        except BtleServerStuck:
            raise
        except Exception as e:
            log(trace_line("adv scan failed: %s" % describe(e), t0))
            res = []
            await asyncio.sleep(every)
        states = tuple(sorted({r["state_byte"] for r in res if r.get("state_byte") is not None}))
        if states != last:
            log(trace_line("adv state %s" % (", ".join("%d %s" % (x, ADV_STATES.get(x, "?")) for x in states)
                                             or "not advertising"), t0))
            last = states
        if states:
            seen = states[-1]
        hit = [x for x in states if x in want]
        if hit:
            return hit[0]
    return seen


class LumixBle:
    """One BLE session. Use as ``async with LumixBle(name) as cam: ...``."""

    def __init__(self, name: Optional[str] = None, scan_timeout: float = 20.0,
                 on_event: Optional[Callable[[str], None]] = None,
                 connect_timeout: float = CONNECT_TIMEOUT, connect_tries: int = 2,
                 mfr_addr: Optional[str] = None, answer_clock: bool = True):
        self.name = name
        self.mfr_addr = mfr_addr            # manufacturer-data address bytes (hex), see adv_matches
        self.scan_timeout = scan_timeout
        self.connect_timeout = connect_timeout
        self.connect_tries = max(1, connect_tries)
        # LUMIX Sync writes the clock only when the camera asks on 4cf487c0
        # (I5/g.java:389-391); never unrequested.
        self.answer_clock = answer_clock
        self.client = None
        self.found_name: Optional[str] = None   # advertised name of the camera we connected to
        self.address: Optional[str] = None
        self.notes: List[Tuple[float, str, str]] = []
        self._waiters: Dict[str, asyncio.Queue] = {}
        self._cbs: Dict[str, Callable[..., None]] = {}
        self.subscribed: set = set()           # notify subscriptions the OS confirmed
        # chars that delivered a notification on this link, whatever the CCCD write
        # said: on macOS + S5 every start_notify fails ("attribute not found") and the
        # camera notifies anyway (2026-10-13 14:45:39 18345be1, 2026-10-15 09:09:40 e182ec41)
        self.observed: set = set()
        self._ap: Optional[Tuple[str, str]] = None   # camera AP (ssid, password): memory only, never logged
        self._on_event = on_event or (lambda s: log.info("%s", s))
        self.t0 = time.time()
        self.disconnected = asyncio.Event()

    def ev(self, text: str) -> None:
        self._on_event(trace_line(text, self.t0))

    async def __aenter__(self) -> "LumixBle":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def connect(self) -> None:
        """Scan, then connect. A sleeping S5 (switch OFF, Remote Wakeup on)
        advertises slowly and takes 24-27 s to accept the link, so the connect
        timeout is long and one fresh scan + connect is retried."""
        await check_btle_server()
        bleak = _bleak()
        last: Optional[BaseException] = None
        for attempt in range(1, self.connect_tries + 1):
            dev = await find(self.name, self.scan_timeout, mfr_addr=self.mfr_addr)
            if dev is None:
                raise BleStepError("scan", "camera not advertising over Bluetooth after %.0f s (%s); is it "
                                   "in range with Bluetooth on (and Remote Wakeup on, if switched off)?"
                                   % (self.scan_timeout, self.name or "any Lumix"))
            self.ev("found %s" % (dev.name or dev.address))
            self.found_name, self.address = dev.name or None, dev.address
            self.disconnected.clear()

            def on_disc(_c):
                self.ev("state: disconnected")
                self.disconnected.set()
            client = self.client = bleak.BleakClient(dev, disconnected_callback=on_disc,
                                                     timeout=self.connect_timeout)
            _LIVE_CLIENTS.add(client)
            self.ev("state: connecting (try %d)" % attempt)
            try:
                await client.connect()
            except BaseException as e:
                # Cancellation (wake timeout) must also drop the half-open link, not just errors.
                await _disconnect_client(client)
                if not isinstance(e, Exception):
                    raise
                last = e            # TimeoutError / BleakError
                self.ev("connect try %d failed: %s" % (attempt, describe(e)))
                if attempt < self.connect_tries:
                    await check_btle_server()    # a failed connect is what wedges the daemon
                continue
            self.ev("state: connected")
            return
        raise BleStepError("connect", "camera found but the Bluetooth link didn't come up (%s, %d tries of %.0f s)"
                           % (describe(last) if last else "?", self.connect_tries, self.connect_timeout))

    async def close(self) -> None:
        # Disconnect even when is_connected is False: a link that is still being
        # set up (or half torn down) is not "connected" but is not released either.
        if self.client is not None:
            if self.client.is_connected:
                self.ev("state: closing")
            await _disconnect_client(self.client)

    # -- primitives
    async def read(self, uuid: str, timeout: float = 10.0) -> bytes:
        return bytes(await asyncio.wait_for(self.client.read_gatt_char(uuid), timeout))

    async def write(self, uuid: str, data: bytes, response: bool = True, timeout: float = 10.0) -> None:
        self.ev("write %s %s" % (uuid[:8], data.hex()))
        await asyncio.wait_for(self.client.write_gatt_char(uuid, data, response=response), timeout)

    def _queue(self, uuid: str) -> asyncio.Queue:
        q = self._waiters.get(uuid)
        if q is None:
            q = self._waiters[uuid] = asyncio.Queue()
        return q

    @property
    def notify_observable(self) -> bool:
        """Notifications reach us on this link: the OS confirmed a subscription,
        or one arrived anyway (the CCCD result on macOS says nothing about that)."""
        return bool(self.subscribed or self.observed)

    def _on_notify(self, uuid: str, data: bytes) -> None:
        self.observed.add(uuid)
        self.notes.append((round(time.time() - self.t0, 2), uuid, data.hex()))
        self.ev("notify %s %s%s" % (uuid[:8], data.hex(), _notify_meaning(uuid, data)))
        self._queue(uuid).put_nowait(data)
        if uuid == CH_CLOCK_NOTIFY and self.answer_clock:
            asyncio.ensure_future(self._answer_clock())

    async def _answer_clock(self) -> None:
        try:
            await self.set_clock()
        except Exception as e:
            self.ev("clock answer failed: %s" % describe(e))

    async def subscribe(self, uuid: str) -> None:
        self._queue(uuid)
        cb = self._cbs.get(uuid)
        if cb is None:
            def cb(_ch, data: bytearray, uuid=uuid):
                self._on_notify(uuid, bytes(data))
            self._cbs[uuid] = cb
        try:
            await self.client.start_notify(uuid, cb)
        except Exception:
            # The CCCD write failed, but the camera may notify anyway: keep our
            # callback registered so those notifications still reach _on_notify.
            self._keep_callback(uuid, cb)
            raise
        self.subscribed.add(uuid)

    def _keep_callback(self, uuid: str, cb: Callable[..., None]) -> None:
        """bleak's CoreBluetooth delegate files the callback by handle before it
        writes the CCCD and leaves it there on failure; make sure it is there
        (a no-op on other backends)."""
        d = getattr(getattr(self.client, "_backend", None), "_delegate", None)
        cbs = getattr(d, "_characteristic_notify_callbacks", None)
        if cbs is None:
            return
        try:
            ch = self.client.services.get_characteristic(uuid)
        except Exception:
            return
        if ch is not None and ch.handle not in cbs:
            cbs[ch.handle] = cb

    async def wait_notify(self, uuid: str, timeout: float = 5.0) -> Optional[bytes]:
        q = self._waiters.get(uuid)
        if q is None:
            return None
        try:
            return await asyncio.wait_for(q.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def drain(self, uuid: str) -> None:
        q = self._waiters.get(uuid)
        while q is not None and not q.empty():
            q.get_nowait()

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
                # macOS finds no CCCDs on the S5 before registration ("attribute
                # not found"); rediscover_cccds() retries once we are registered.
                failed += 1
        if failed:
            self.ev("notify subscriptions refused: %d/%d before registration" % (failed, len(REGISTER_NOTIFY)))
        await self.write(CH_DLNA_UUID, client_uuid_bytes())
        await self.write(CH_FRIENDLY_NAME, name.encode("ascii", "replace")[:20])

    def _forget_notify(self, uuid: str) -> None:
        """bleak (CoreBluetooth) keeps the callback of a start_notify that failed
        and then refuses a second start_notify ("already started"); drop it."""
        d = getattr(getattr(self.client, "_backend", None), "_delegate", None)
        cbs = getattr(d, "_characteristic_notify_callbacks", None)
        if cbs is None:
            return
        try:
            ch = self.client.services.get_characteristic(uuid)
        except Exception:
            return
        if ch is not None:
            cbs.pop(ch.handle, None)
            getattr(d, "_characteristic_notification_discriminators", {}).pop(ch.handle, None)

    def _cccd_count(self, uuid: str) -> Optional[int]:
        try:
            ch = self.client.services.get_characteristic(uuid)
            return sum(1 for x in ch.descriptors if x.uuid.lower().startswith("00002902"))
        except Exception:
            return None

    async def rediscover_cccds(self, uuids: Optional[List[str]] = None) -> int:
        """After registration, rediscover services/descriptors and enable notify
        on the LUMIX Sync chars that failed before; return how many are on.

        Before registration CoreBluetooth lists no CCCDs on the S5, so every
        start_notify failed with "attribute not found" and e182ec41 (Wi-Fi start
        result), e182ec43 (camera requests) and 4cf487c0 (clock request) were
        never seen. Real-device verified: no."""
        uuids = list(uuids or REGISTER_NOTIFY)
        todo = [u for u in uuids if u not in self.subscribed]
        if not todo or self.client is None:
            return len(uuids) - len(todo)
        for u in todo:
            self._forget_notify(u)
        be = getattr(self.client, "_backend", None)
        if be is not None and hasattr(be, "_get_services") and hasattr(be, "services"):
            old = be.services
            try:
                be.services = None
                await asyncio.wait_for(be._get_services(), 20)
                self.ev("services rediscovered")
            except Exception as e:
                be.services = old
                self.ev("service rediscovery failed: %s" % describe(e))
        ok = 0
        for u in todo:
            if self.disconnected.is_set():
                break
            try:
                await asyncio.wait_for(self.subscribe(u), 5)
                ok += 1
                self.ev("notify on %s (cccd %s)" % (u[:8], self._cccd_count(u)))
            except Exception as e:
                self.ev("notify %s still refused (cccd %s): %s" % (u[:8], self._cccd_count(u), describe(e)))
        on = len(uuids) - len(todo) + ok
        self.ev("notify subscriptions after registration: %d/%d on" % (on, len(uuids)))
        return on

    async def login(self, legacy: bool = False) -> bytes:
        """Register, then read the XOR key, the camera SSID and the AP
        password in LUMIX Sync's order (K5/j.java:191-205), then enable the
        notifications that failed before registration.

        ``legacy``: our pre-2026-10-13 order (camera SSID, then key; no AP
        password read, no rediscovery), kept for the baseline experiment.

        A camera that doesn't know our client id drops the link right after
        the friendly-name write (seen on the S5 when the id had changed: HCI
        disconnect ~0.3 s after the write, before any read). That is the one
        failure re-pairing (or restoring the old id) fixes, so it is tagged
        need_pair."""
        try:
            await self.register()
            if legacy:
                await self.read(CH_CAMERA_SSID)
                key = await self.read(CH_INITIAL_INFORM)
            else:
                key = await self.read(CH_INITIAL_INFORM)
                ap_ssid = _printable(await self.read(CH_CAMERA_SSID))
                raw_pw = await self.read(CH_CAMERA_AP_PASS)
                with contextlib.suppress(Exception):
                    self._ap = (ap_ssid, xor_decode(raw_pw, struct.unpack(">I", key[:4])[0]))
        except Exception as e:
            if self.disconnected.is_set():
                raise BleStepError(
                    "login", "the camera hung up right after this computer's id (%s) was sent, so it "
                    "doesn't know that id. Pair again (camera: Bluetooth > SET > Pairing), or restore the "
                    "id it was paired with" % _client_id(), need_pair=True) from e
            raise BleStepError("login", describe(e)) from e
        self.ev("state: logged in (initial_inform %s)" % key.hex())
        if not legacy:
            await self.rediscover_cccds()
        return key

    async def set_clock(self) -> None:
        await self.write(CH_CLOCK, clock_payload())

    async def _write_connect_type(self, value: int) -> None:
        """LUMIX Sync retries this write 3 times, 500 ms apart (I5/g.java:349-361)."""
        for i in range(3):
            try:
                await self.write(CH_WIFI_CMD, bytes([value]))
                return
            except Exception as e:
                if i == 2 or self.disconnected.is_set():
                    raise
                self.ev("connect type write retry %d: %s" % (i + 1, describe(e)))
                await asyncio.sleep(0.5)

    async def wait_start_ack(self, timeout: float = 10.0) -> Optional[bytes]:
        """e182ec41 after the Wi-Fi start write, or None (timeout / link lost)."""
        t = asyncio.ensure_future(self.wait_notify(CH_WIFI_START_ACK, timeout))
        d = asyncio.ensure_future(self.disconnected.wait())
        try:
            await asyncio.wait({t, d}, timeout=timeout + 0.5, return_when=asyncio.FIRST_COMPLETED)
        finally:
            d.cancel()
        if not t.done():
            t.cancel()
            return None
        return t.result()

    async def wifi_join(self, ssid: Optional[str] = None, purpose: int = PURPOSE_REMOTE,
                        sta: bool = True, softap: bool = False, legacy: bool = False,
                        start_ack_wait: Optional[float] = None) -> Optional[bytes]:
        """Bring Wi-Fi up joined to a known network (the camera keeps the
        password), the way LUMIX Sync does for Remote shooting
        (I5/g.java:777-824, 969-998):

          SSID -> connect type 03 ("SSID set") -> camera acks 18345be1
          -> connect type 02 if the ack is 00, else 01 (g.java:333-366)
          -> start e182ec40 = purpose -> camera answers e182ec41 (00 ok, 01 refused).

        Returns the e182ec41 value (None when the camera sent none within
        ``start_ack_wait`` s). Raises BleRefused on 01.

        A non-zero ack means the camera wants its own access point: reported,
        not followed, unless ``softap`` (that takes this Mac off the LAN).
        ``softap`` alone runs LUMIX Sync's purpose-5 path: connect type 01, then
        start 05 (g.java:936-967). ``legacy``: the pre-2026-10-13 sequence
        (fixed 0.3 s pause, no wait for e182ec41)."""
        if start_ack_wait is None:
            start_ack_wait = START_ACK_WAIT
        if softap and purpose == PURPOSE_SOFTAP:
            self.drain(CH_WIFI_START_ACK)
            await self._write_connect_type(WIFI_SOFTAP)
            await self.write(CH_WIFI_START, bytes([PURPOSE_SOFTAP]))
            return await self._start_result(start_ack_wait)
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
        self.drain(CH_WIFI_CMD_ACK)              # drop stale acks
        await self.write(CH_WIFI_SSID, ssid_payload(ssid))
        await self.write(CH_WIFI_CMD, bytes([WIFI_STA_SSID_SET]))
        ack = await self.wait_notify(CH_WIFI_CMD_ACK, 5.0)
        ctype = WIFI_STA
        if ack is None:
            self.ev("no join ack from the camera; sending STA anyway")
        elif ack[:1] != b"\x00":
            if not softap:
                raise BleStepError("wifi", "the camera answered %s to joining %r (it wants its own access point; "
                                   "is that network saved on the camera?)" % (ack.hex(), ssid))
            ctype = WIFI_SOFTAP
        if sta or ctype == WIFI_SOFTAP:
            if legacy:
                await self.write(CH_WIFI_CMD, bytes([ctype]))
                await asyncio.sleep(0.3)
            else:
                await self._write_connect_type(ctype)    # write response = the camera took it
        self.drain(CH_WIFI_START_ACK)
        await self.write(CH_WIFI_START, bytes([purpose]))
        if legacy:
            return None
        return await self._start_result(start_ack_wait)

    async def _start_result(self, wait: float) -> Optional[bytes]:
        """The camera's answer on e182ec41 to the Wi-Fi start write. Waited for
        whenever notifications reach us, even with 0 confirmed subscriptions:
        2026-10-15 09:09:40 the S5 sent "e182ec41 01" (refused) on a link where
        all 10 CCCD writes had failed, and the old code didn't wait for it."""
        if not self.notify_observable:
            wait = min(wait, START_ACK_BLIND_WAIT)
        res = await self.wait_start_ack(wait)
        if res is None and not self.notify_observable:
            self.ev("wifi start result: none in %.0f s (no notification has arrived on this link, so a "
                    "refusal may not be visible)%s" % (wait, " (link lost)" if self.disconnected.is_set() else ""))
        else:
            self.ev("wifi start result: %s" % (res.hex() + _notify_meaning(CH_WIFI_START_ACK, res)
                                               if res is not None else
                                               "none in %.0f s%s" % (wait, " (link lost)" if self.disconnected.is_set()
                                                                     else "")))
        if res is not None and res[:1] == b"\x01":
            raise BleRefused()
        return res


_E182EC43 = {1: "camera wants Wi-Fi (transfer)", 2: "camera resets the request", 3: "camera asks to read SSID",
             4: "camera wants Wi-Fi (transfer)"}


def _notify_meaning(uuid: str, data: bytes) -> str:
    """Short gloss for the trace (docs/ble-power-protocol.md section 1)."""
    v = data[0] if data else None
    if uuid == CH_WIFI_START_ACK:
        return {0: " (wifi start ok)", 1: " (REFUSED: camera state)"}.get(v, "")
    if uuid == CH_MAIN_NOTIFY3:
        return " (%s)" % _E182EC43[v] if v in _E182EC43 else ""
    if uuid == CH_CLOCK_NOTIFY:
        return " (camera asks for the time)"
    if uuid == CH_WIFI_CMD_ACK:
        return " (join ack: %s)" % ("STA" if v == 0 else "wants SoftAP")
    return ""


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
                mark_registered(c.found_name or name, c.address)
                return {"ok": True, "tries": tries, "camera_ssid": _printable(ssid),
                        "initial_inform_len": len(key), "notifies": c.notes,
                        "elapsed_s": round(time.time() - t0, 1)}
        except BtleServerStuck:
            raise
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
            if out["registered"]:
                out["saved"] = mark_registered(c.found_name or name, c.address)
    except Exception as e:
        out["registered"] = False
        out.update(error_fields(e, "login"))
    return out


def _last_ip() -> Optional[str]:
    try:
        return load_config().get("last_ip")
    except Exception:
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


ADV_SLEEP_GAP_S = 100.0   # longest gap between a sleeping camera's advertising bursts, plus margin


async def adv_snapshot(name: Optional[str] = None, timeout: float = 10.0,
                       log: Callable[[str], None] = print, t0: Optional[float] = None) -> Dict[str, Any]:
    """One scan before connecting: the advertised state byte (byte 2 of the
    0x003A manufacturer data) and the address bytes to re-find the camera.
    Keeps scanning for up to ``timeout`` s until an advertisement carries the
    state byte (one without manufacturer data doesn't end the scan); returns
    the last match without a state when none did. {} when nothing was found;
    raises only BtleServerStuck."""
    try:
        res = await scan(timeout, name, stop_on_state=True)
    except BtleServerStuck:
        raise
    except Exception as e:
        log(trace_line("adv scan failed: %s" % describe(e), t0))
        return {}
    if not res:
        log(trace_line("adv: camera not advertising (%s)" % (name or "any Lumix"), t0))
        return {}
    best = next((r for r in res if r.get("state_byte") is not None), res[0])
    adv = {k: v for k, v in best.items() if k != "_dev"}
    log(trace_line("adv state %s %s (mfr %s)" % (adv.get("state_byte"), adv.get("state"), adv.get("mfr")), t0))
    return adv


async def wake(name: Optional[str] = None, ssid: Optional[str] = None, wifi_wait: float = 90.0,
               camera_ip: Optional[str] = None, log: Callable[[str], None] = print,
               purpose: int = PURPOSE_REMOTE, sta: bool = True, single_session: bool = False,
               legacy: bool = False, softap: bool = False, wifi_if: str = "en0",
               wake_wait: float = 40.0) -> Dict[str, Any]:
    """Bring the camera onto the home network over BLE, then wait for :60606.
    Powers the body up from switch-OFF when Remote Wakeup is on. Never raises:
    a failure comes back as {"ok": false, "step": scan|connect|login|wifi|
    wifi-wait, "error": "<step>: ..."} (plus "refused"/"hint" when the camera
    answered e182ec41 = 01).

    1. Scan once and record the advertised state.
    2. Sleep state (1, 4-10), the LUMIX Sync way (HomeActivity.java:1230-1259):
       stage 1 connects and logs in to wake the body, then disconnects; poll the
       advertisement every ~1 s for up to ``wake_wait`` s until it is 0 or 2.
    3. Connect, log in (key, camera SSID, AP password; notifications on),
       SSID -> 03 -> ack -> 02/01 -> start 03, wait for e182ec41.
    No unrequested clock write: the clock is answered on 4cf487c0 only.

    ``single_session``: skip step 2 (the pre-2026-10-13 shape).
    ``legacy``: single session with the old bytes (clock written up front, old
    read order, no notify rediscovery, fixed 0.3 s pause) -- the baseline.
    ``softap``: purpose 05 + connect type 01, then this Mac joins the camera's
    own access point (it leaves the home LAN for the duration)."""
    t0 = time.time()
    out: Dict[str, Any] = {"ok": False}
    ip = camera_ip or _last_ip()
    out["ip"] = ip
    took = None

    def tr(text: str) -> None:
        log(trace_line(text, t0))

    if legacy:
        single_session = True
    try:
        await check_btle_server()        # before the first scan, not after a failed connect
        # A sleeping S5 (state 6) advertises in ~10 s bursts about every 90 s
        # (2026-10-13 12:19-12:21); a 10 s look misses it and drops to single-session.
        adv = await adv_snapshot(name, ADV_SLEEP_GAP_S, log, t0)
    except BtleServerStuck as e:
        out.update(error_fields(e))
        tr("refused: %s" % out["error"])
        return out
    state = adv.get("state_byte")
    mfr_addr = adv.get("mfr_addr")
    out["adv_state_before"] = state
    out["adv_state_name_before"] = adv.get("state")
    # No state after a full ADV_SLEEP_GAP_S look: assume asleep. Two-session is
    # safe for an awake camera too: stage 1 is a plain connect + login + close
    # (no Wi-Fi or power write), and wait_state() returns on the first 1 s scan
    # that shows 0/2, so an awake body costs one extra connection.
    two = (not single_session) and (state in SLEEP_STATES or state is None)
    if two and state is None:
        tr("WARNING: no advertised state after %.0f s; using two-session anyway (safe if the camera is "
           "awake, one extra connection)" % ADV_SLEEP_GAP_S)
        out["note_state"] = "no advertised state seen; two-session used anyway"
    out["mode"] = "legacy" if legacy else "two-session" if two else "single-session"
    if softap:
        out["mode"] += "+softap"
        purpose = PURPOSE_SOFTAP
    if state in FAST_STATES and not softap:
        out["note"] = ("camera advertised a _fast sleep state; LUMIX Sync asks for the camera's own access "
                       "point here (purpose 05, connect type 01), so it may ignore the STA request")
    tr("wake start: mode %s, adv state %s" % (out["mode"], state))
    notes: List[Tuple[float, str, str]] = []
    c = LumixBle(name, scan_timeout=ADV_SLEEP_GAP_S, on_event=log, mfr_addr=mfr_addr, answer_clock=not legacy)
    step = "connect"
    try:
        if two:
            tr("stage 1: connect + login to wake the body")
            await c.connect()
            out["stage1_connected_s"] = round(time.time() - t0, 1)
            step = "login"
            await c.login()
            out["registered"] = True
            out["saved"] = mark_registered(c.found_name or name, c.address)
            notes += c.notes
            await c.close()
            tr("stage 1 done; waiting up to %.0f s for the camera to advertise normal/wakeup" % wake_wait)
            step = "wake-wait"
            woke = await wait_state(name, mfr_addr, AWAKE_STATES, wake_wait, 1.0, log, t0)
            out["adv_state_after_wake"] = woke
            out["woke_s"] = round(time.time() - t0, 1)
            tr("stage 2: state %s; reconnecting" % woke)
            c = LumixBle(name, scan_timeout=20, on_event=log, mfr_addr=mfr_addr, answer_clock=True)
            step = "connect"
        await c.connect()
        out["ble_connected_s"] = round(time.time() - t0, 1)
        step = "login"
        await c.login(legacy=legacy)
        # login() read INITIAL_INFORM: an unknown central is cut off before that
        out["registered"] = True
        out["saved"] = mark_registered(c.found_name or name, c.address)
        out["notify_on"] = len(c.subscribed)
        if legacy:
            try:
                await c.set_clock()
            except Exception as e:
                log("clock: %s" % describe(e))
        step = "wifi"
        res = await c.wifi_join(ssid, purpose=purpose, sta=sta, softap=softap, legacy=legacy)
        out["wifi_start_result"] = res.hex() if res is not None else None
        out["wifi_cmd_s"] = round(time.time() - t0, 1)
        step = "wifi-wait"
        if softap:
            took = await _softap_check(c, wifi_if, wifi_wait, out, tr)
        else:
            # Stay connected while the camera brings Wi-Fi up (11-40 s); the camera
            # drops BLE itself once it is on the network.
            took = await wait_wifi(ip, wifi_wait)
    except Exception as e:
        out.update(error_fields(e, step))
        tr("failed at %s: %s" % (step, out.get("error")))
    finally:
        # Whatever happened (error, two connect timeouts, cancellation): no client
        # left connecting/connected and no scan running.
        out["notifies"] = notes + c.notes
        out["notify_seen"] = sorted(u[:8] for u in c.observed)
        with contextlib.suppress(Exception):
            await c.close()
        await release_all()
    out["wifi_up_s"] = round(time.time() - t0, 1) if took is not None else None
    out["ok"] = took is not None
    if not out["ok"] and "error" not in out:
        out["step"] = "wifi-wait"
        out["error"] = ("wifi-wait: the camera took the Wi-Fi command but didn't answer on %s:60606 within %ds"
                        % (ip, wifi_wait) if ip else "wifi-wait: no camera IP known to check")
    tr("wake end: ok=%s wifi_up_s=%s" % (out["ok"], out["wifi_up_s"]))
    return out


async def _softap_check(c: LumixBle, wifi_if: str, wait: float, out: Dict[str, Any],
                        tr: Callable[[str], None]) -> Optional[float]:
    """--softap only: join the camera AP like LUMIX Sync (HomeActivity.java:
    1449-1458), check :60606 at 192.168.54.1, then rejoin the home network.
    The AP password stays in memory."""
    if not c._ap or not c._ap[0]:
        raise BleStepError("wifi-wait", "camera AP SSID unknown (login didn't read it)")
    ap_ssid, pw = c._ap
    t0 = time.time()
    loop = asyncio.get_running_loop()
    tr("softap: joining the camera AP %s (this Mac leaves the LAN)" % ap_ssid)
    try:
        r = await loop.run_in_executor(None, lambda: _sh("networksetup", "-setairportnetwork", wifi_if, ap_ssid,
                                                         *([pw] if pw else [])))
        if r:
            tr("softap: networksetup: %s" % (r.replace(pw, "***") if pw else r))
        while time.time() - t0 < wait:
            if await loop.run_in_executor(None, ddd_ok, "192.168.54.1", 2.0):
                took = round(time.time() - t0, 1)
                out["softap_up_s"] = took
                tr("softap: camera answers on its own AP after %.1f s" % took)
                return took
            await asyncio.sleep(1.0)
        return None
    finally:
        _sh("networksetup", "-removepreferredwirelessnetwork", wifi_if, ap_ssid)
        _sh("networksetup", "-setairportpower", wifi_if, "off")
        _sh("networksetup", "-setairportpower", wifi_if, "on")
        tr("softap: left the camera AP; home Wi-Fi rejoining")


def wifi_poweroff(ip: str, server: Optional[str], log: Callable[[str], None] = print) -> Dict[str, Any]:
    """camcmd poweroff, through the Lumix Remote server if it runs, else directly."""
    import json
    import urllib.request
    if server:
        try:
            import os
            cam = os.environ.get("LUMIX_CAMERA_ID")
            path = "/api/cameras/%s/power/off" % cam if cam else "/api/power/off"
            req = urllib.request.Request(server.rstrip("/") + path, data=b"{\"confirm\": true}",
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                return {"via": "server", "reply": json.loads(r.read() or b"{}")}
        except Exception as e:
            log("server poweroff failed (%s); trying the camera directly" % e)
    # Same client id as the server's req_acc, so the camera knows us already.
    cid = _client_id()
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
    t0 = time.time()
    out: Dict[str, Any] = {"ok": False}
    cid = _client_id()
    while time.time() - t0 < wait:
        res = await scan(5.0, name)
        if any(r.get("state_byte") == 3 for r in res):
            break
    else:
        out["error"] = "camera never entered Pairing (MENU > Setup > Bluetooth > SET > Pairing)"
        return out
    ap_ssid = None
    c = LumixBle(name, scan_timeout=20, on_event=log)
    try:
        await c.connect()
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
        await release_all()
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
        save_config(camera_name=name or ap_ssid, registered_at=int(time.time()),
                    identity=cid, address=getattr(getattr(c, "client", None), "address", None))
    out["elapsed_s"] = round(time.time() - t0, 1)
    return out
