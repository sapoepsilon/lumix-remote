#!/usr/bin/env python3
"""One BLE wake experiment against a real camera, with everything around it logged.

    .venv-ble/bin/python tools/ble-wake-experiment.py --variant two-session
    .venv-ble/bin/python tools/ble-wake-experiment.py --variant notify-only
    .venv-ble/bin/python tools/ble-wake-experiment.py --variant baseline

Variants (docs/ble-power-protocol.md sections 7-8):
  baseline     one BLE session, the pre-2026-10-13 bytes (clock up front, old read
               order, no notify rediscovery): reproduces today's failure
  notify-only  one session with the new login: notifications re-enabled after
               registration, e182ec41 awaited, clock only on 4cf487c0
  two-session  the LUMIX Sync shape: from a sleep state, log in once to wake the
               body, wait for adv state 0/2, reconnect and send the STA join

How it gets the BLE link: the Lumix Remote server owns the Bluetooth link and the
wake cap (2 wakes per 10 min, shared by the UI, the API and the watchdog), so
this tool never opens its own BLE connection. It asks the server to run the wake
(POST /api/ble/wake {"variant": ...}) and refuses to start when the server is
unreachable, its BLE worker is busy, or the cap is used up. The server's
lumix-ble helper logs each step as "ble.trace" lines in /tmp/lumix-server.log.
Needs a server started after this change (the variant field is new).

Meanwhile it only listens and looks:
  - advertised state byte every 2 s (passive BLE scan, no connection);
  - macOS Wi-Fi scan for the camera's own access point (S5-xxxxxx) with
    `system_profiler -json SPAirPortDataType`. Without Location permission macOS
    redacts SSIDs there, so the tool also records networks that appear during
    the wake by channel/PHY/security (other networks' names are never stored);
  - the camera on the LAN: TCP connect to <camera ip>:60606 and the ARP cache
    (`arp -n`, no packets sent). Only the configured camera IP is contacted.

The JSON result goes to logs/ble-experiments/<time>-<variant>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import datetime as _dt
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lumix import ble  # noqa: E402  (bleak itself is imported lazily, for the adv scan)

VARIANTS = ("baseline", "two-session", "notify-only")
CAMERA_AP_RE = re.compile(r"^[A-Z0-9]{2,6}-[0-9A-F]{6}$")   # the S5's own AP: S5-XXXXXX
OUT_DIR = os.path.join(ROOT, "logs", "ble-experiments")


# ------------------------------------------------------------------ server API
def api(server: str, path: str, body: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0) -> Tuple[int, Dict[str, Any]]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server.rstrip("/") + path, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"error": "HTTP %d" % e.code}


def ble_path(camera: Optional[str], tail: str) -> str:
    return ("/api/cameras/%s/ble/%s" % (camera, tail)) if camera else "/api/ble/%s" % tail


def preflight(status: Dict[str, Any]) -> Optional[str]:
    """Why the experiment must not start now (None = go)."""
    if status.get("busy"):
        return "the server's BLE worker is busy (%s); wait for it to finish" % (status.get("running") or "?")
    blocked = (status.get("wakes") or {}).get("blocked")
    if blocked:
        return "wake cap: %s" % blocked
    return None


# ------------------------------------------------------------------ Wi-Fi scan
def parse_wifi(doc: Dict[str, Any]) -> Dict[str, Any]:
    """system_profiler -json SPAirPortDataType -> camera-AP matches + fingerprints.
    Names of other networks are not kept."""
    nets: List[Dict[str, Any]] = []
    for item in doc.get("SPAirPortDataType") or []:
        for ifc in item.get("spairport_airport_interfaces") or []:
            nets += ifc.get("spairport_airport_other_local_wireless_networks") or []
            cur = ifc.get("spairport_current_network_information")
            if isinstance(cur, dict):
                nets.append(cur)
    names = [str(n.get("_name") or "") for n in nets]
    redacted = sum(1 for x in names if not x or "redacted" in x)
    cams = sorted({x for x in names if CAMERA_AP_RE.match(x)})
    fps = ["%s|%s|%s" % (n.get("spairport_network_channel"), n.get("spairport_network_phymode"),
                         n.get("spairport_security_mode")) for n in nets]
    return {"n": len(nets), "redacted": redacted, "camera_ssids": cams, "fingerprints": fps}


def wifi_scan() -> Dict[str, Any]:
    try:
        out = subprocess.run(["system_profiler", "-json", "SPAirPortDataType"], capture_output=True,
                             text=True, timeout=30).stdout
        return parse_wifi(json.loads(out or "{}"))
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}


# ------------------------------------------------------------------ LAN check
def lan_check(ip: str) -> Dict[str, bool]:
    tcp = False
    try:
        with socket.create_connection((ip, 60606), timeout=1.0):
            tcp = True
    except OSError:
        pass
    arp = False
    try:
        out = subprocess.run(["arp", "-n", ip], capture_output=True, text=True, timeout=5).stdout
        arp = bool(re.search(r"\bat [0-9a-f]{1,2}(:[0-9a-f]{1,2}){5}\b", out, re.I))
    except Exception:
        pass
    return {"tcp60606": tcp, "arp": arp}


# ------------------------------------------------------------------ experiment
class Recorder:
    def __init__(self) -> None:
        self.t0 = time.time()
        self.events: List[Dict[str, Any]] = []
        self.first: Dict[str, float] = {}

    def rel(self) -> float:
        return round(time.time() - self.t0, 2)

    def add(self, kind: str, **kw: Any) -> None:
        e = {"t": self.rel(), "kind": kind}
        e.update(kw)
        self.events.append(e)
        print("ble.trace %s +%.2fs experiment %s %s" % (
            _dt.datetime.now().strftime("%H:%M:%S.%f")[:-3], e["t"], kind,
            json.dumps(kw, default=str)), file=sys.stderr, flush=True)

    def mark(self, key: str) -> None:
        self.first.setdefault(key, self.rel())


async def adv_loop(rec: Recorder, name: Optional[str], mfr_addr: Optional[str], stop: asyncio.Event,
                   every: float = 2.0) -> None:
    last = None
    while not stop.is_set():
        t = time.time()
        try:
            res = await ble.scan(1.5, name, mfr_addr=mfr_addr)
            states = sorted({r["state_byte"] for r in res if r.get("state_byte") is not None})
        except Exception as e:
            states = None
            rec.add("adv-error", error=ble.describe(e))
        if states is not None:
            rec.add("adv", states=states, names=[ble.ADV_STATES.get(s) for s in states],
                    changed=states != last)
            for s in states:
                rec.mark("adv_state_%d" % s)
            last = states
        await _pause(stop, every - (time.time() - t))


async def wifi_loop(rec: Recorder, base: "collections.Counter[str]", stop: asyncio.Event,
                    every: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        t = time.time()
        w = await loop.run_in_executor(None, wifi_scan)
        if "error" in w:
            rec.add("wifi-error", error=w["error"])
        else:
            new = list((collections.Counter(w["fingerprints"]) - base).elements())
            rec.add("wifi", n=w["n"], redacted=w["redacted"], camera_ssids=w["camera_ssids"], new=new)
            if w["camera_ssids"]:
                rec.mark("camera_ap_seen")
            if new:
                rec.mark("new_wifi_network")
        await _pause(stop, every - (time.time() - t))


async def lan_loop(rec: Recorder, ip: str, stop: asyncio.Event, every: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    last = None
    while not stop.is_set():
        t = time.time()
        r = await loop.run_in_executor(None, lan_check, ip)
        if r != last:
            rec.add("lan", **r)
            last = r
        if r["tcp60606"]:
            rec.mark("lan_60606_up")
        if r["arp"]:
            rec.mark("arp_present")
        await _pause(stop, every - (time.time() - t))


async def _pause(stop: asyncio.Event, secs: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), max(0.05, secs))
    except asyncio.TimeoutError:
        pass


async def run(a: argparse.Namespace) -> Tuple[int, Dict[str, Any]]:
    if a.camera:
        os.environ["LUMIX_CAMERA_ID"] = a.camera
    cfg: Dict[str, Any] = {}
    try:
        cfg = ble.load_config()
    except Exception:
        pass
    name = a.name or cfg.get("camera_name")
    ip = a.camera_ip or cfg.get("last_ip")
    out: Dict[str, Any] = {"variant": a.variant, "started_at": _dt.datetime.now().isoformat(timespec="seconds"),
                           "server": a.server, "camera_id": a.camera, "name": name, "camera_ip": ip}
    try:
        st, status = api(a.server, ble_path(a.camera, "status"))
    except Exception as e:
        return 2, dict(out, refused="server unreachable (%s); this tool only wakes through the server" % e)
    if st != 200:
        return 2, dict(out, refused="server answered %d to the BLE status" % st)
    why = preflight(status)
    if why:
        return 2, dict(out, refused=why)
    out["cap_before"] = status.get("wakes")
    rec = Recorder()
    stop = asyncio.Event()
    tasks = []
    mfr_addr = None
    if not a.no_adv:
        snap = await ble.adv_snapshot(name, 6.0, log=lambda s: print(s, file=sys.stderr), t0=rec.t0)
        mfr_addr = snap.get("mfr_addr")
        out["adv_state_before"] = snap.get("state_byte")
        tasks.append(asyncio.ensure_future(adv_loop(rec, name, mfr_addr, stop)))
    if not a.no_wifi_scan:
        loop = asyncio.get_running_loop()
        w0 = await loop.run_in_executor(None, wifi_scan)
        out["wifi_before"] = {k: w0.get(k) for k in ("n", "redacted", "camera_ssids", "error")}
        base = collections.Counter(w0.get("fingerprints") or [])
        tasks.append(asyncio.ensure_future(wifi_loop(rec, base, stop)))
    if ip:
        tasks.append(asyncio.ensure_future(lan_loop(rec, ip, stop)))
    else:
        out["lan_note"] = "no camera IP configured; LAN check skipped"
    await asyncio.sleep(a.pre)
    rec.add("wake-request", variant=a.variant)
    rec.mark("wake_request")
    loop = asyncio.get_running_loop()
    try:
        code, res = await loop.run_in_executor(
            None, lambda: api(a.server, ble_path(a.camera, "wake"), {"variant": a.variant}, timeout=600))
    except Exception as e:
        code, res = 0, {"ok": False, "error": "request failed: %s" % e}
    rec.mark("wake_reply")
    rec.add("wake-reply", http=code, ok=res.get("ok"), step=res.get("step"), error=res.get("error"),
            refused=res.get("refused"))
    await asyncio.sleep(a.post)
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    out.update({
        "ended_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "wake_http": code, "wake": res, "first_s": rec.first, "events": rec.events,
        "summary": {
            "wake_ok": bool(res.get("ok")), "refused": bool(res.get("refused")),
            "error": res.get("error"), "mode": res.get("mode"),
            "adv_state_before": res.get("adv_state_before", out.get("adv_state_before")),
            "adv_state_after_wake": res.get("adv_state_after_wake"),
            "wifi_start_result": res.get("wifi_start_result"),
            "camera_ap_seen_s": rec.first.get("camera_ap_seen"),
            "new_wifi_network_s": rec.first.get("new_wifi_network"),
            "lan_60606_up_s": rec.first.get("lan_60606_up"),
        },
    })
    return 0, out


def save(out: Dict[str, Any], out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "%s-%s.json" % (time.strftime("%Y%m%d-%H%M%S"), out.get("variant")))
    with open(path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    return path


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--variant", required=True, choices=VARIANTS)
    p.add_argument("--server", default="http://127.0.0.1:8770", help="Lumix Remote server")
    p.add_argument("--camera", default=None, help="camera id (default: the server's active camera)")
    p.add_argument("--name", default=None, help="advertised BLE name (default: from cameras.json)")
    p.add_argument("--camera-ip", default=None, help="camera IP for the LAN check (default: last known)")
    p.add_argument("--pre", type=float, default=6.0, help="seconds to observe before the wake")
    p.add_argument("--post", type=float, default=60.0, help="seconds to observe after the wake returns")
    p.add_argument("--no-adv", action="store_true", help="skip the BLE advertisement scan")
    p.add_argument("--no-wifi-scan", action="store_true", help="skip the macOS Wi-Fi scan")
    p.add_argument("--out-dir", default=OUT_DIR)
    a = p.parse_args(argv)
    code, out = asyncio.run(run(a))
    if code:
        print("refusing to run: %s" % out.get("refused"), file=sys.stderr)
        return code
    path = save(out, a.out_dir)
    print(json.dumps(dict(out["summary"], result=os.path.relpath(path, ROOT))), flush=True)
    if out["summary"].get("refused") and (out.get("wake") or {}).get("hint"):
        print("hint: %s" % out["wake"]["hint"], file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
