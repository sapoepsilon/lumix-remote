"""lumix-ble: command-line Bluetooth helper. Run via bin/lumix-ble.

  scan                 list advertising Lumix bodies
  register [--wait 300]  first-time registration (camera on Bluetooth > SET > Pairing):
                       BLE UUID+name, join the camera SoftAP briefly, accctrl, rejoin home Wi-Fi
  pair [--wait 300]    BLE-only part of registration (camera stays "Pairing failed" without Wi-Fi step)
  status               connect, read the info characteristics
  dump                 read every readable characteristic (JSON)
  wake / wifi-on       log in and ask the camera to start Wi-Fi (Via Network);
                       powers the body up from OFF when Remote Wakeup is ON
  power-cycle          Wi-Fi poweroff -> wait -> BLE wake -> wait for :60606

Every command prints one JSON object on stdout (progress goes to stderr), so
the server can run it as a subprocess.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any, Dict

from lumix import ble


def _err(s: str) -> None:
    print(s, file=sys.stderr, flush=True)


def _out(d: Dict[str, Any]) -> None:
    print(json.dumps(d, default=str), flush=True)


async def cmd_scan(a) -> Dict[str, Any]:
    res = await ble.scan(a.timeout, a.name)
    for r in res:
        r.pop("_dev", None)
    return {"ok": bool(res), "devices": res}


async def cmd_pair(a) -> Dict[str, Any]:
    return await ble.pair(a.name, wait=a.wait, log=_err)


async def cmd_register(a) -> Dict[str, Any]:
    return await ble.first_register(a.name, wait=a.wait, wifi_if=a.wifi_if, log=_err)


async def cmd_status(a) -> Dict[str, Any]:
    return await ble.status(a.name, log=_err)


async def cmd_dump(a) -> Dict[str, Any]:
    async with ble.LumixBle(a.name, on_event=_err) as c:
        await c.register()
        return {"ok": True, "values": await c.read_all()}


async def cmd_wake(a) -> Dict[str, Any]:
    return await ble.wake(a.name, ssid=a.ssid, wifi_wait=a.wifi_wait, camera_ip=a.camera_ip, log=_err)


async def cmd_cycle(a) -> Dict[str, Any]:
    return await ble.power_cycle(a.name, camera_ip=a.camera_ip, server=a.server, off_wait=a.off_wait,
                                 wifi_wait=a.wifi_wait, ssid=a.ssid, log=_err)


def main() -> None:
    p = argparse.ArgumentParser(prog="lumix-ble", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", default=None, help="advertised name, e.g. S5-XXXXXX (default: any Lumix)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("--timeout", type=float, default=8.0); s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("pair"); s.add_argument("--wait", type=float, default=300.0); s.set_defaults(fn=cmd_pair)
    s = sub.add_parser("register"); s.add_argument("--wait", type=float, default=300.0)
    s.add_argument("--wifi-if", default="en0"); s.set_defaults(fn=cmd_register)
    s = sub.add_parser("status"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("dump"); s.set_defaults(fn=cmd_dump)
    for nm in ("wake", "wifi-on", "power-cycle"):
        s = sub.add_parser(nm)
        s.add_argument("--ssid", default=None, help="network to join (default: the camera's saved network, then LUMIX_HOME_SSID / ble.json)")
        s.add_argument("--camera-ip", default=None, help="expected camera IP (default: last known)")
        s.add_argument("--wifi-wait", type=float, default=90.0, help="seconds to wait for :60606")
        if nm == "power-cycle":
            s.add_argument("--server", default="http://127.0.0.1:8770",
                           help="Lumix Remote server for the Wi-Fi poweroff ('' = talk to the camera directly)")
            s.add_argument("--off-wait", type=float, default=8.0, help="seconds between poweroff and wake")
            s.set_defaults(fn=cmd_cycle)
        else:
            s.set_defaults(fn=cmd_wake)
    a = p.parse_args()
    t0 = time.time()
    try:
        res = asyncio.run(a.fn(a))
    except KeyboardInterrupt:
        res = {"ok": False, "error": "interrupted"}
    except Exception as e:
        res = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    res.setdefault("elapsed_s", round(time.time() - t0, 1))
    _out(res)
    sys.exit(0 if res.get("ok") else 1)


if __name__ == "__main__":
    main()
