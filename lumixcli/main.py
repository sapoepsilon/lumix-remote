"""`lumix` command line: drives the camera through the Lumix Remote server API.

The server holds the camera's single controller session, so every command here
is an HTTP/WebSocket call to it; nothing opens its own camera connection.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional

from . import resolve, servermgr
from .client import CameraError, CliError, Client, ServerUnreachable, check
from .resolve import ResolveError, short_id

EXIT_USAGE = 2


# ------------------------------------------------------------------ output
def out(args: argparse.Namespace, data: Any, human: Callable[[Any], Optional[str]]) -> None:
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        text = human(data)
        if text:
            print(text)


def _fmt_secs(v: Any) -> str:
    try:
        s = int(v)
    except (TypeError, ValueError):
        return str(v)
    return "%d s (%d:%02d:%02d)" % (s, s // 3600, s % 3600 // 60, s % 60)


def _yn(v: Any) -> str:
    return "yes" if v else "no"


def fmt_status(st: Dict[str, Any]) -> str:
    cam = st.get("camera") or {}
    sd = st.get("sd") or {}
    lv = st.get("liveview") or {}
    name = cam.get("friendlyName") or "camera"
    head = "%s (%s fw %s): %s%s" % (name, cam.get("modelNumber") or "?", cam.get("X_FirmVersion") or "?",
                                   st.get("status"), "  " + st["ip"] if st.get("ip") else "")
    rows = [head]
    if st.get("message"):
        rows.append("  message    %s" % st["message"])
    batt = st.get("battery")
    if st.get("battery_grip") and not str(st.get("battery_grip")).startswith("-1"):
        batt = "%s (grip %s)" % (batt, st["battery_grip"])
    rows.append("  battery    %s" % batt)
    cards = []
    for k in ("sd1", "sd2"):
        if sd.get(k):
            full = sd.get(k + "_full") == "true"
            cards.append("%s %s%s%s" % (k, sd[k], " FULL" if full else "",
                                        " (active)" if sd.get("current") == k else ""))
    rows.append("  card       %s%s" % (", ".join(cards) or "?",
                                      "  [writing]" if sd.get("access") == "on" else ""))
    rec = "recording %s" % (_fmt_secs(st.get("rec_progress")) if st.get("recording") else "no")
    rows.append("  mode       %s, %s" % (st.get("cammode"), rec))
    rows.append("  remaining  %s photos, video %s" % (st.get("remain_photos"), _fmt_secs(st.get("remain_video_s"))))
    rows.append("  lens       %s" % st.get("lens"))
    if st.get("temperature"):
        rows.append("  temp       %s" % st["temperature"])
    rows.append("  liveview   %s fps, frame age %ss" % (lv.get("fps"), lv.get("age")))
    if st.get("op_owner"):
        rows.append("  busy with  %s" % st["op_owner"])
    if st.get("powered_off"):
        rows.append("  camera was powered off over Wi-Fi; switch it on by hand")
    return "\n".join(rows)


def fmt_setting_line(s: Dict[str, Any], options: bool = True) -> str:
    toks = resolve.option_tokens(s)
    cur = s.get("current_option")
    cur_tok = next((t for t, o in zip(toks, s["options"]) if o["id"] == cur), None)
    shown = cur_tok or (s.get("current") if s.get("current") is not None else "-")
    line = "%-34s %-22s %s" % (short_id(s["id"]) + ("" if s.get("enabled", True) else " (off)"),
                               shown, s.get("title", ""))
    if s.get("current_title") and s.get("current_title") != shown:
        line += " = %s" % s["current_title"]
    if options and toks:
        allowed = [t + ("" if o.get("enabled", True) else "*") for t, o in zip(toks, s["options"])]
        line += "\n    " + " ".join(allowed)
    return line


# ---------------------------------------------------------------- commands
def cmd_status(c: Client, a: argparse.Namespace) -> int:
    out(a, c.get("/api/status"), fmt_status)
    return 0


def cmd_info(c: Client, a: argparse.Namespace) -> int:
    def human(d: Dict[str, Any]) -> str:
        ddd, lens, cap = d.get("ddd") or {}, d.get("lens") or {}, d.get("capability") or {}
        rows = ["camera     %s %s fw %s" % (ddd.get("friendlyName"), ddd.get("modelNumber"), ddd.get("X_FirmVersion")),
                "lens       %s" % lens.get("name"),
                "touch_type %s" % d.get("touch_type")]
        e = d.get("exposure") or {}
        rows.append("aperture   %s (%s)" % (e.get("focal_title"), e.get("focal")))
        rows.append("shutter    %s (%s)" % (e.get("shtrspeed_title"), e.get("shtrspeed")))
        for k, v in sorted(cap.items()):
            rows.append("%-10s %s" % (k, v))
        return "\n".join(rows)
    out(a, c.get("/api/info"), human)
    return 0


def cmd_state(c: Client, a: argparse.Namespace) -> int:
    out(a, c.get("/api/state"), lambda d: "\n".join("%-24s %s" % kv for kv in sorted(d.items())))
    return 0


def _settings(c: Client) -> List[Dict[str, Any]]:
    d = c.get("/api/settings")
    if not d.get("ready"):
        raise CliError("camera menu not loaded yet (camera not connected?)")
    return d["settings"]


def cmd_get(c: Client, a: argparse.Namespace) -> int:
    if a.raw:
        out(a, c.get("/api/getsetting/" + urllib.parse.quote(a.raw, safe="")),
            lambda d: "\n".join("%s = %s" % kv for kv in d.get("values", {}).items()) or "(no value)")
        return 0
    if a.setting is None:
        q = c.get("/api/quick")
        e = q.get("exposure") or {}

        def human(d: Dict[str, Any]) -> str:
            rows = ["%-14s %-24s %s" % ("aperture", e.get("focal_title") or "-", "(aperture)"),
                    "%-14s %-24s %s" % ("shutter", e.get("shtrspeed_title") or "-", "(shutter)")]
            for it in d.get("items", []):
                s = it["setting"]
                rows.append("%-14s %-24s (%s)" % (it["key"], s.get("current_title") or "-", short_id(s["id"])))
            return "\n".join(rows)
        out(a, q, human)
        return 0
    kind = resolve.EXPOSURE.get(a.setting.lower())
    if kind:
        e = c.get("/api/info").get("exposure") or {}
        d = {"setting": kind, "value": e.get(kind), "title": e.get(kind + "_title"),
             "options": e.get("aperture_options" if kind == "focal" else "shutter_options")}
        out(a, d, lambda d: "%s = %s (%s)\n    %s" % (
            "aperture" if kind == "focal" else "shutter", d["title"], d["value"],
            " ".join(o["title"] for o in d["options"] or [])))
        return 0
    s = resolve.find_setting(_settings(c), a.setting)
    out(a, s, lambda s: fmt_setting_line(s))
    return 0


def cmd_set(c: Client, a: argparse.Namespace) -> int:
    if a.raw:
        if len(a.args) not in (2, 3):
            raise ResolveError("usage: lumix set --raw TYPE VALUE [VALUE2]")
        body: Dict[str, Any] = {"type": a.args[0], "value": a.args[1]}
        if len(a.args) == 3:
            body["value2"] = a.args[2]
        r = c.post("/api/setting", body)
        out(a, r, lambda r: "%s = %s: %s" % (a.args[0], "/".join(a.args[1:]), r["result"]))
        return 0
    if len(a.args) != 2:
        raise ResolveError("usage: lumix set SETTING VALUE (see `lumix settings list`)")
    name, value = a.args
    kind = resolve.EXPOSURE.get(name.lower())
    if kind:
        e = c.get("/api/info").get("exposure") or {}
        opts = e.get("aperture_options" if kind == "focal" else "shutter_options") or []
        # Unknown lens limits: let a raw code through rather than refusing.
        o = resolve.find_exposure(opts, value, kind) if opts or "/" not in value else {"value": value, "title": value}
        r = c.post("/api/setting", {"type": kind, "value": o["value"]})
        out(a, r, lambda r: "%s: %s -> %s (%s)" % ("aperture" if kind == "focal" else "shutter",
                                                  e.get(kind + "_title"), o["title"], r["result"]))
        return 0
    s = resolve.find_setting(_settings(c), name)
    o = resolve.find_option(s, value)
    r = c.post("/api/settings/" + urllib.parse.quote(s["id"], safe=""), {"option": o["id"]})
    new = r.get("setting") or {}
    out(a, r, lambda r: "%s: %s -> %s (%s)" % (short_id(s["id"]), s.get("current_title"),
                                              new.get("current_title", o["title"]), r["result"]))
    return 0


def cmd_settings_list(c: Client, a: argparse.Namespace) -> int:
    items = _settings(c)
    if a.filter:
        f = resolve.norm(a.filter)
        items = [s for s in items if f in resolve.norm(s["id"]) or f in resolve.norm(s.get("title", ""))
                 or any(f in resolve.norm(t) for t in s.get("cmd_types", []))
                 or f in resolve.norm(s.get("section", ""))]
    if a.enabled:
        items = [s for s in items if s.get("enabled", True)]
    if a.names:
        names = [short_id(s["id"]) for s in items]
        if not a.filter:
            names += ["aperture", "shutter"]
        if a.json:
            print(json.dumps(names))
        else:
            print("\n".join(names))
        return 0
    out(a, items, lambda items: "\n".join(fmt_setting_line(s, not a.brief) for s in items)
        + "\n(%d settings; '(off)' = not available in the current mode, '*' = option disabled)" % len(items))
    return 0


def cmd_settings_options(c: Client, a: argparse.Namespace) -> int:
    kind = resolve.EXPOSURE.get(a.setting.lower())
    if kind:
        e = c.get("/api/info").get("exposure") or {}
        opts = e.get("aperture_options" if kind == "focal" else "shutter_options") or []
        rows = [{"token": o["title"], "title": o["title"], "value": o["value"],
                 "current": o["value"] == e.get(kind), "enabled": True} for o in opts]
    else:
        s = resolve.find_setting(_settings(c), a.setting)
        rows = [{"token": t, "title": o.get("title"), "value": o.get("value"), "id": o["id"],
                 "current": o["id"] == s.get("current_option"), "enabled": o.get("enabled", True)}
                for t, o in zip(resolve.option_tokens(s), s["options"])]
    if a.names:
        print(json.dumps([r["token"] for r in rows]) if a.json else "\n".join(r["token"] for r in rows))
        return 0
    out(a, rows, lambda rows: "\n".join("%s %-28s %-24s %s%s" % (
        "*" if r["current"] else " ", r["token"], r["title"], r["value"],
        "" if r["enabled"] else "  (disabled)") for r in rows))
    return 0


def cmd_shoot(c: Client, a: argparse.Namespace) -> int:
    r = c.post("/api/capture", timeout=max(c.timeout, 30))

    def human(r: Dict[str, Any]) -> str:
        msg = "shot: %s, remaining %s -> %s" % (r["result"], r.get("remain_before"), r.get("remain_after"))
        if not r.get("count_changed"):
            msg += " (count unchanged; camera may still be writing)"
        return msg
    out(a, r, human)
    return 0


def cmd_video(c: Client, a: argparse.Namespace) -> int:
    action = a.action
    if action == "toggle":
        action = "stop" if c.get("/api/status").get("recording") else "start"
    r = c.post("/api/video/" + action)
    out(a, r, lambda r: "video %s: %s (recording: %s)" % (action, r["result"], _yn(r.get("recording"))))
    return 0


def cmd_af(c: Client, a: argparse.Namespace) -> int:
    r = c.post("/api/af")
    out(a, r, lambda r: "af: %s" % r["result"])
    return 0


def cmd_focus(c: Client, a: argparse.Namespace) -> int:
    x, y = resolve.parse_coord(a.x), resolve.parse_coord(a.y)
    r = c.post("/api/touch", {"x": x, "y": y})
    out(a, r, lambda r: "touch focus %d/%d: %s (release %s)" % (x, y, r["result"], r.get("release")))
    return 0


def cmd_zoom(c: Client, a: argparse.Namespace) -> int:
    action = "zoomstop" if a.direction == "stop" else "%s-%s" % (a.direction, "fast" if a.fast else "normal")
    r = c.post("/api/zoom", {"action": action})
    out(a, r, lambda r: "zoom %s: %s" % (action, r["result"]))
    return 0


def cmd_raw(c: Client, a: argparse.Namespace) -> int:
    r = c.post("/api/camcmd", {"value": a.value})
    out(a, r, lambda r: "camcmd %s: %s%s" % (a.value, r["result"], ("\n" + r["text"]) if r.get("text") else ""))
    return 0


def cmd_poweroff(c: Client, a: argparse.Namespace) -> int:
    if not a.yes:
        sys.stderr.write("lumix: poweroff needs --yes (the camera cannot be switched back on over Wi-Fi)\n")
        return EXIT_USAGE
    r = c.post("/api/power/off", {"confirm": True})
    out(a, r, lambda r: "poweroff: %s (switch the camera on by hand)" % r["result"])
    return 0


def cmd_snapshot(c: Client, a: argparse.Namespace) -> int:
    data = c.get_bytes("/api/liveview.jpg" + ("?source=" + urllib.parse.quote(a.source) if a.source else ""))
    if a.output == "-":
        sys.stdout.buffer.write(data)
        sys.stdout.flush()
        return 0
    path = a.output or "lumix-%s.jpg" % time.strftime("%Y%m%d-%H%M%S")
    with open(path, "wb") as f:
        f.write(data)
    out(a, {"path": os.path.abspath(path), "bytes": len(data)},
        lambda d: "saved %s (%d bytes)" % (d["path"], d["bytes"]))
    return 0


# ------------------------------------------------------------------ events
def fmt_event(ev: Dict[str, Any]) -> str:
    ts = ev.get("ts")
    t = time.strftime("%H:%M:%S", time.localtime(ts)) if isinstance(ts, (int, float)) else "--:--:--"
    return "%s %-7s %s" % (t, ev.get("kind", "?"), ev.get("text", ""))


STATUS_KEYS = ("status", "message", "cammode", "recording", "battery", "remain_photos", "op_owner", "powered_off")


class EventPrinter:
    """Turns WebSocket messages into lines; status ticks only when something changed."""

    def __init__(self, as_json: bool, show_all: bool):
        self.as_json, self.show_all = as_json, show_all
        self.last_status: Optional[Dict[str, Any]] = None
        self.printed = 0

    def handle(self, msg: Dict[str, Any]) -> List[str]:
        t, d = msg.get("type"), msg.get("data")
        lines: List[str] = []
        if t == "status" and isinstance(d, dict):
            key = {k: d.get(k) for k in STATUS_KEYS}
            first = self.last_status is None
            changed = not first and key != self.last_status
            if self.show_all or changed:
                if self.as_json:
                    lines.append(json.dumps(msg, default=str))
                else:
                    diff = ", ".join("%s=%s" % (k, key[k]) for k in STATUS_KEYS
                                     if first or key[k] != self.last_status.get(k))
                    lines.append("%s status  %s" % (time.strftime("%H:%M:%S"), diff))
            self.last_status = key
        elif t == "event" and isinstance(d, dict):
            lines.append(json.dumps(d, default=str) if self.as_json else fmt_event(d))
        elif t == "events" and isinstance(d, list):
            if self.show_all:  # history replay on connect
                lines.extend(json.dumps(e, default=str) if self.as_json else fmt_event(e) for e in d)
        elif t == "settings":
            lines.append(json.dumps(msg, default=str) if self.as_json else
                         "%s settings changed: %s" % (time.strftime("%H:%M:%S"),
                                                      ", ".join(short_id(str(x)) for x in (d or []))))
        elif t == "exposure" and isinstance(d, dict):
            if self.show_all:
                lines.append(json.dumps(msg, default=str) if self.as_json else
                             "%s exposure %s %s" % (time.strftime("%H:%M:%S"), d.get("focal_title"),
                                                     d.get("shtrspeed_title")))
        elif self.show_all:
            lines.append(json.dumps(msg, default=str))
        return lines


def _emit(lines: List[str], pr: EventPrinter, limit: Optional[int]) -> bool:
    """Print lines; True when --count is reached."""
    for ln in lines:
        print(ln, flush=True)
        pr.printed += 1
        if limit and pr.printed >= limit:
            return True
    return False


def follow_ws(c: Client, a: argparse.Namespace) -> int:
    import asyncio

    import aiohttp

    pr = EventPrinter(a.json, a.all)

    async def run() -> int:
        delay = 1.0
        while True:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.ws_connect(c.ws_url(), heartbeat=20) as ws:
                        delay = 1.0
                        async for m in ws:
                            if m.type != aiohttp.WSMsgType.TEXT:
                                break
                            try:
                                msg = json.loads(m.data)
                            except ValueError:
                                continue
                            if _emit(pr.handle(msg), pr, a.count):
                                return 0
            except aiohttp.ClientError as e:
                if pr.printed == 0 and a.no_reconnect:
                    raise CliError("cannot open %s: %s" % (c.ws_url(), e)) from None
                sys.stderr.write("lumix: event stream: %s\n" % e)
            if a.no_reconnect:
                return 3
            sys.stderr.write("lumix: reconnecting in %.0fs\n" % delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(run())
    finally:
        loop.close()


def follow_poll(c: Client, a: argparse.Namespace) -> int:
    pr = EventPrinter(a.json, a.all)
    last = time.time() if not a.all else 0.0
    while True:
        evs = c.get("/api/events")
        new = [e for e in evs if isinstance(e.get("ts"), (int, float)) and e["ts"] > last]
        if new:
            last = new[-1]["ts"]
        if _emit([json.dumps(e) if a.json else fmt_event(e) for e in new], pr, a.count):
            return 0
        time.sleep(1)


def cmd_events(c: Client, a: argparse.Namespace) -> int:
    if not a.follow:
        evs = c.get("/api/events")
        if a.lines:
            evs = evs[-a.lines:]
        out(a, evs, lambda evs: "\n".join(fmt_event(e) for e in evs) or "(no events)")
        return 0
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        sys.stderr.write("lumix: aiohttp not installed, polling /api/events instead of the WebSocket\n")
        return follow_poll(c, a)
    try:
        return follow_ws(c, a)
    except KeyboardInterrupt:
        return 130


# ------------------------------------------------------------------ server
def cmd_server(c: Client, a: argparse.Namespace) -> int:
    if a.action == "status":
        st = servermgr.status(c)
        up = st["reachable"]

        def human(st: Dict[str, Any]) -> str:
            who = ("pid %d (%s)" % (st["pid"], st["pidfile"])) if st["pid"] else \
                ("not started by lumix" if up else "")
            return "server %s at %s%s\n  log %s" % ("running" if up else "not running", st["url"],
                                                     ("  " + who) if who else "", st["log"])
        if up:
            try:
                cam = c.get("/api/status")
                st["camera_status"] = cam.get("status")
            except CliError:
                pass
        out(a, st, lambda st: human(st) + ("\n  camera %s" % st["camera_status"] if st.get("camera_status") else ""))
        return 0 if up else 1
    try:
        if a.action == "start":
            st = servermgr.start(c, a.extra)
            out(a, st, lambda st: st["message"] + "\n  log %s" % st["log"])
            return 0 if st["reachable"] else 1
        st = servermgr.stop(c)
        out(a, st, lambda st: st["message"])
        return 0
    except RuntimeError as e:
        raise CliError(str(e))


# ----------------------------------------------------------------- cameras
def fmt_cameras(d: Dict[str, Any]) -> str:
    cams = d.get("cameras") or []
    if not cams:
        return "no cameras set up (lumix cameras add NAME, or open the web UI)"
    rows = []
    for cam in cams:
        info = cam.get("camera") or {}
        prof = cam.get("profile") or {}
        model = info.get("modelNumber") or prof.get("name") or cam.get("model")
        rows.append("%s %-14s %-16s %-14s %-12s %s%s" % (
            "*" if cam.get("active") else " ", cam["id"], cam.get("nickname") or "", model or "?",
            cam.get("status") or "-", cam.get("ip") or "",
            "  [ble]" if (cam.get("ble") or {}).get("registered") else ""))
    if d.get("offline"):
        rows.append("(server not running: read from %s)" % d.get("path"))
    return "\n".join(rows)


def _registry():
    from lumix import registry
    reg = registry.Registry()
    registry.migrate(reg)
    return registry, reg


def _offline_list() -> Dict[str, Any]:
    registry, reg = _registry()
    d = reg.load()
    cams = [{"id": e["id"], "nickname": e.get("nickname"), "active": e["id"] == d["active"],
             "model": e.get("model"), "profile": {"name": e.get("model")},
             "camera": {"modelNumber": e.get("model_number"), "friendlyName": e.get("friendly_name")},
             "status": "offline", "ip": e.get("fixed_ip") or e.get("last_ip"),
             "ble": {"registered": bool((e.get("ble") or {}).get("registered_at"))}}
            for e in d["cameras"]]
    return {"active": d["active"], "cameras": cams, "offline": True, "path": reg.path}


def _cam_path(cam_id: str, tail: str = "") -> str:
    return "/api/cameras/%s%s" % (urllib.parse.quote(cam_id, safe=""), tail)


def cmd_cameras(c: Client, a: argparse.Namespace) -> int:
    """Registry commands. Through the server when it runs (sessions start/stop
    right away), else straight on ~/.config/lumix-remote/cameras.json."""
    action = a.cameras_cmd
    try:
        if action == "list":
            out(a, c.get("/api/cameras"), fmt_cameras)
        elif action == "add":
            body: Dict[str, Any] = {"nickname": a.name}
            if a.model:
                body["model"] = a.model
            if a.ip:
                body["ip"] = a.ip
            ports = {k: v for k, v in (("http", a.http_port), ("upnp", a.upnp_port),
                                       ("files", a.files_port)) if v}
            if ports:
                body["ports"] = ports
            if a.use:
                body["activate"] = True
            r = c.post("/api/cameras", body)
            out(a, r, lambda r: "added %s (%s)%s" % (r["id"], r.get("nickname"),
                                                    ", now active" if r.get("active") else ""))
        elif action == "remove":
            r = c.delete(_cam_path(a.id))
            out(a, r, lambda r: "removed %s%s" % (r["removed"], "; active: %s" % r["active"] if r.get("active") else ""))
        elif action == "use":
            r = c.post(_cam_path(a.id, "/activate"))
            out(a, r, lambda r: "active camera: %s (%s)" % (r["id"], r.get("nickname")))
        elif action == "rename":
            r = c.patch(_cam_path(a.id), {"nickname": a.name})
            out(a, r, lambda r: "%s is now %r" % (r["id"], r.get("nickname")))
        return 0
    except ServerUnreachable:
        pass
    registry, reg = _registry()
    try:
        if action == "list":
            out(a, _offline_list(), fmt_cameras)
            return 0
        if action == "add":
            ports = {k: v for k, v in (("http", a.http_port), ("upnp", a.upnp_port),
                                       ("files", a.files_port)) if v}
            fields: Dict[str, Any] = {"model": a.model or "auto"}
            if a.ip:
                fields["fixed_ip"] = a.ip
            if ports:
                fields["ports"] = ports
            e = reg.add(a.name, activate=a.use, **fields)
            out(a, e, lambda e: "added %s (%s) to %s" % (e["id"], e["nickname"], reg.path))
            return 0
        e = reg.find(a.id)
        if e is None:
            raise registry.RegistryError("no camera %r (see lumix cameras list)" % a.id)
        if action == "remove":
            reg.remove(e["id"])
            out(a, {"removed": e["id"]}, lambda d: "removed %s" % d["removed"])
        elif action == "use":
            reg.set_active(e["id"])
            out(a, {"active": e["id"]}, lambda d: "active camera: %s" % d["active"])
        elif action == "rename":
            e = reg.rename(e["id"], a.name)
            out(a, e, lambda e: "%s is now %r" % (e["id"], e["nickname"]))
    except registry.RegistryError as e:
        raise ResolveError(str(e))
    return 0


def cmd_coming_soon(c: Client, a: argparse.Namespace) -> int:
    sys.stderr.write("lumix %s: coming soon (the server exposes no %s API yet)\n" % (a.feature, a.feature))
    return 1


COMPLETION = r'''# lumix shell completion. bash: eval "$(lumix completion bash)"
# zsh: autoload -U +X bashcompinit && bashcompinit && eval "$(lumix completion zsh)"
_lumix() {
  local cur prev words cword
  cur="${COMP_WORDS[COMP_CWORD]}"; prev="${COMP_WORDS[COMP_CWORD-1]}"
  local cmds="status info state get set settings shoot video af focus zoom cmd snapshot events poweroff cameras server timelapse files completion"
  local i sub="" n=0
  for ((i=1; i<COMP_CWORD; i++)); do
    case "${COMP_WORDS[i]}" in --server|--timeout|--camera) ((i++));; -*) ;; *) [ -z "$sub" ] && sub="${COMP_WORDS[i]}"; ((n++));; esac
  done
  case "$sub" in
    "") COMPREPLY=($(compgen -W "$cmds" -- "$cur"));;
    get) [ $n -eq 1 ] && COMPREPLY=($(compgen -W "$(lumix settings list --names 2>/dev/null)" -- "$cur"));;
    set) if [ $n -eq 1 ]; then COMPREPLY=($(compgen -W "$(lumix settings list --names 2>/dev/null)" -- "$cur"));
         elif [ $n -eq 2 ]; then COMPREPLY=($(compgen -W "$(lumix settings options "$prev" --names 2>/dev/null)" -- "$cur")); fi;;
    settings) if [ $n -eq 1 ]; then COMPREPLY=($(compgen -W "list options" -- "$cur"));
              elif [ "$prev" = options ]; then COMPREPLY=($(compgen -W "$(lumix settings list --names 2>/dev/null)" -- "$cur")); fi;;
    video) COMPREPLY=($(compgen -W "start stop toggle" -- "$cur"));;
    zoom) COMPREPLY=($(compgen -W "tele wide stop --fast" -- "$cur"));;
    server) COMPREPLY=($(compgen -W "start stop status" -- "$cur"));;
    cameras) if [ $n -eq 1 ]; then COMPREPLY=($(compgen -W "list add remove use rename" -- "$cur"));
             else COMPREPLY=($(compgen -W "$(lumix cameras list --json 2>/dev/null | python3 -c 'import json,sys; print(" ".join(c["id"] for c in json.load(sys.stdin)["cameras"]))' 2>/dev/null)" -- "$cur")); fi;;
    completion) COMPREPLY=($(compgen -W "bash zsh" -- "$cur"));;
  esac
}
complete -F _lumix lumix
'''


def cmd_completion(c: Client, a: argparse.Namespace) -> int:
    sys.stdout.write(COMPLETION)
    return 0


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--server", default=argparse.SUPPRESS, metavar="URL",
                        help="server URL (env LUMIX_SERVER, default 127.0.0.1:8770)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable JSON output")
    common.add_argument("--timeout", type=float, default=argparse.SUPPRESS, help="HTTP timeout in seconds")
    common.add_argument("--camera", "-c", default=argparse.SUPPRESS, metavar="ID",
                        help="camera id or nickname (env LUMIX_CAMERA, default: the server's active camera)")

    p = argparse.ArgumentParser(
        prog="lumix", description="Control a Lumix camera through the Lumix Remote server.",
        epilog="Exit codes: 0 ok, 1 camera/server error (e.g. err_busy), 2 usage, 3 server unreachable.")
    p.add_argument("--server", default=None, metavar="URL",
                   help="server URL (env LUMIX_SERVER, default 127.0.0.1:8770)")
    p.add_argument("--json", action="store_true", default=False, help="machine-readable JSON output")
    p.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds (default 30)")
    p.add_argument("--camera", "-c", default=None, metavar="ID",
                   help="camera id or nickname (env LUMIX_CAMERA, default: the server's active camera)")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    def add(name: str, fn: Callable, help: str, **kw: Any) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, parents=[common], help=help, description=help, **kw)
        sp.set_defaults(fn=fn)
        return sp

    add("status", cmd_status, "battery, card, mode, remaining shots/video, connection")
    add("info", cmd_info, "device, lens, capabilities, aperture/shutter")
    add("state", cmd_state, "raw getstate fields")

    sp = add("get", cmd_get, "show quick controls, or one setting's current value and options")
    sp.add_argument("setting", nargs="?", help="setting name (iso, sensitivity, whitebalance, aperture, ...)")
    sp.add_argument("--raw", metavar="TYPE", help="raw camera getsetting TYPE (focal, shtrspeed, iso, ...)")

    sp = add("set", cmd_set, "change a setting: lumix set iso 400 / set aperture 4 / set --raw TYPE VALUE")
    sp.add_argument("args", nargs="+", metavar="ARG", help="SETTING VALUE, or with --raw: TYPE VALUE [VALUE2]")
    sp.add_argument("--raw", action="store_true", help="send a raw setsetting TYPE VALUE [VALUE2]")

    sp = add("settings", None, "list settings and their allowed values")
    ss = sp.add_subparsers(dest="settings_cmd", metavar="list|options")
    ss.required = True
    lp = ss.add_parser("list", parents=[common], help="every setting: name, current value, allowed values")
    lp.add_argument("--filter", "-f", help="substring of name/title/command type/section")
    lp.add_argument("--names", action="store_true", help="names only, one per line (for completion)")
    lp.add_argument("--brief", "-b", action="store_true", help="hide allowed values")
    lp.add_argument("--enabled", action="store_true", help="only settings available in the current mode")
    lp.set_defaults(fn=cmd_settings_list)
    op = ss.add_parser("options", parents=[common], help="allowed values of one setting ('*' = current)")
    op.add_argument("setting")
    op.add_argument("--names", action="store_true", help="tokens only, one per line (for completion)")
    op.set_defaults(fn=cmd_settings_options)

    add("shoot", cmd_shoot, "take a photo")
    sp = add("video", cmd_video, "video recording")
    sp.add_argument("action", choices=["start", "stop", "toggle"])
    add("af", cmd_af, "one-shot autofocus")
    sp = add("focus", cmd_focus, "touch focus at X Y (0..1000, 0.0..1.0 or N%%)")
    sp.add_argument("x")
    sp.add_argument("y")
    sp = add("zoom", cmd_zoom, "power zoom (power-zoom lenses only)")
    sp.add_argument("direction", choices=["tele", "wide", "stop"])
    sp.add_argument("--fast", action="store_true")
    sp = add("cmd", cmd_raw, "raw camcmd passthrough, e.g. lumix cmd recmode")
    sp.add_argument("value", help="camcmd value (poweroff is refused; use `lumix poweroff --yes`)")
    sp = add("snapshot", cmd_snapshot, "save the latest live view frame as JPEG")
    sp.add_argument("-o", "--output", help="file (default lumix-YYYYmmdd-HHMMSS.jpg, '-' = stdout)")
    sp.add_argument("--source", help="frame source (default wifi)")
    sp = add("events", cmd_events, "recent camera/server events; -f follows the WebSocket feed")
    sp.add_argument("-f", "--follow", action="store_true", help="stream new events (Ctrl-C to stop)")
    sp.add_argument("-n", "--lines", type=int, default=0, help="only the last N events")
    sp.add_argument("--all", action="store_true", help="with -f: include history, every status tick, exposure")
    sp.add_argument("--count", type=int, default=0, help="with -f: exit after N lines")
    sp.add_argument("--no-reconnect", action="store_true", help="with -f: exit when the stream drops")
    sp = add("poweroff", cmd_poweroff, "power the camera off (cannot be switched back on over Wi-Fi)")
    sp.add_argument("--yes", action="store_true", help="really power off")

    sp = add("cameras", None, "list / add / remove / use / rename cameras (works without the server too)")
    cs = sp.add_subparsers(dest="cameras_cmd", metavar="list|add|remove|use|rename")
    cs.required = True
    cp = cs.add_parser("list", parents=[common], help="registered cameras ('*' = active)")
    cp.set_defaults(fn=cmd_cameras)
    cp = cs.add_parser("add", parents=[common], help="register a camera (the server finds it on the LAN)")
    cp.add_argument("name", help="nickname, e.g. \"S5 B-cam\" (the id is derived from it)")
    cp.add_argument("--model", help="model profile id (default auto-detect; see GET /api/models)")
    cp.add_argument("--ip", help="fixed camera IP (default: discover)")
    cp.add_argument("--http-port", type=int, help="camera cam.cgi port (emulator)")
    cp.add_argument("--upnp-port", type=int, help="camera :60606 port (emulator)")
    cp.add_argument("--files-port", type=int, help="camera :50001 port (emulator)")
    cp.add_argument("--use", action="store_true", help="make it the active camera")
    cp.set_defaults(fn=cmd_cameras)
    cp = cs.add_parser("remove", parents=[common], help="forget a camera")
    cp.add_argument("id")
    cp.set_defaults(fn=cmd_cameras)
    cp = cs.add_parser("use", parents=[common], help="make a camera the active one")
    cp.add_argument("id")
    cp.set_defaults(fn=cmd_cameras)
    cp = cs.add_parser("rename", parents=[common], help="change a camera's nickname")
    cp.add_argument("id")
    cp.add_argument("name")
    cp.set_defaults(fn=cmd_cameras)

    sp = add("server", cmd_server, "start/stop/status of the background web server (./run.sh)")
    sp.add_argument("action", choices=["start", "stop", "status"])
    sp.add_argument("extra", nargs=argparse.REMAINDER,
                    help="for start: extra run.sh args after --, e.g. -- --camera-ip 192.168.1.50")

    for feat, help in (("timelapse", "interval shooting (coming soon)"), ("files", "browse/download card files (coming soon)")):
        sp = add(feat, cmd_coming_soon, help)
        sp.add_argument("rest", nargs=argparse.REMAINDER)
        sp.set_defaults(feature=feat)

    sp = add("completion", cmd_completion, "print a bash/zsh completion script")
    sp.add_argument("shell", choices=["bash", "zsh"])
    return p


def main(argv: Optional[List[str]] = None) -> int:
    p = build_parser()
    a, extra = p.parse_known_args(argv)
    if extra:
        # `lumix set exposure3 -1/3`: argparse takes "-1/3" for an option; keep it as a value.
        if a.command == "set" and not any(x.startswith("--") for x in extra):
            a.args += extra
        else:
            p.error("unrecognized arguments: %s" % " ".join(extra))
    if getattr(a, "extra", None) and a.extra[0] == "--":
        a.extra = a.extra[1:]
    c = Client(a.server, timeout=a.timeout, camera=a.camera)
    try:
        return a.fn(c, a)
    except ResolveError as e:
        sys.stderr.write("lumix: %s\n" % e)
        return EXIT_USAGE
    except CliError as e:
        if a.json and e.payload is not None:
            print(json.dumps(e.payload, indent=2, default=str))
        sys.stderr.write("lumix: %s\n" % e)
        return e.exit_code
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


__all__ = ["main", "build_parser", "check", "CameraError"]
