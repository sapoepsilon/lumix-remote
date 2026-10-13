"""Chaos / fuzz harness: random concurrent UI traffic against the server + emulator.

One event loop runs the camera emulator (wedge realism on) and the real server
app on its own ephemeral port with its own config dir (the ``Rig`` from
test_timelapse_emu). Several "browser tabs" fire random sequences of what the web
UI can send -- shutter, video start/stop, settings, live view viewers (MJPEG) on
and off, live-view quality, frame polls, file browsing and imports, time-lapse
start/pause/resume/stop (server and in-camera engine), reconnect, WebSockets,
requests aborted half-way -- while a director restarts the server now and then.

Afterwards it checks the invariants that protect the real S5:

* the emulator never wedged (err_critical / err_busy for good);
* mode switches (recmode/playmode/pictmode) reaching the camera respect the
  transport cap (``MODE_SWITCH_MIN_GAP_S`` apart, ``MODE_SWITCH_MAX`` per
  ``MODE_SWITCH_WINDOW_S``), across server restarts too;
* no ``stopstream`` without a stream (``err_critical`` on the S5);
* the server gets back to ``ready`` once the traffic stops;
* jobs survive: no time-lapse or import job ends ``failed``, and a job that
  existed before a server restart is still there after it.

The sequence of actions is a pure function of the seed; the timing between
concurrent actors is not, so a failure is minimized by hand into a deterministic
test in tests/test_chaos.py. Long runs: ``LUMIX_CHAOS_SEEDS=1-50`` (see
test_chaos.ChaosLongRun) or ``.venv/bin/python tools/chaos.py --seeds 1-50``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest import mock

import aiohttp

from test_timelapse_emu import FAST_CAMERA, Rig, emu_cfg

MODE_SWITCHES = ("recmode", "playmode", "pictmode")


def chaos_emu_cfg(**kw):
    # The camera's own timing scaled like FAST_CAMERA scales the server's
    # (real: limiter 1 per 10 s / 3 per 60 s, churn wedge = 4th recmode in 20 s,
    # settle 5-22 s; x0.05 here).
    base = dict(settle_min=0.25, settle_max=1.1, churn_window_s=1.0, interval_scale=0.05,
                capture_busy_s=0.3, video_busy_s=0.5)
    base.update(kw)
    return emu_cfg(**base)


class ChaosRig(Rig):
    """Rig with a longer wait for "ready" (real timing: the settle alone is 5-22 s)."""
    ready_s = 20.0

    async def start_server(self):
        orig = self.wait

        async def wait(pred, timeout, what):
            return await orig(pred, max(timeout, self.ready_s), what)
        self.wait = wait
        try:
            await super().start_server()
        finally:
            del self.wait


@dataclass
class ChaosResult:
    seed: int
    trace: List[str] = field(default_factory=list)
    violations: List[str] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.violations

    def report(self, tail: int = 60) -> str:
        lines = ["seed %d: %s" % (self.seed, "OK" if self.ok else "%d violation(s)" % len(self.violations))]
        lines += ["  ! " + v for v in self.violations]
        if not self.ok:
            lines.append("  trace (last %d):" % tail)
            lines += ["    " + t for t in self.trace[-tail:]]
        return "\n".join(lines)


ACTIONS = {
    # name: weight
    "photo": 6, "video": 4, "setting": 6, "viewer": 6, "lv_quality": 3, "frame": 4,
    "files_browse": 4, "files_done": 3, "import": 3, "job_ctl": 2,
    "tl_start": 3, "tl_pause": 2, "tl_resume": 2, "tl_stop": 2,
    "reconnect": 2, "ws": 4, "abort": 5, "poll": 6, "touch": 2, "af": 2,
}


class Chaos:
    def __init__(self, seed: int, duration: float = 20.0, tabs: int = 3, restarts: int = 1,
                 actions: Optional[Dict[str, int]] = None, emu_kw: Optional[Dict[str, Any]] = None,
                 recover_s: float = 45.0):
        self.seed = seed
        self.duration = duration
        self.tabs = tabs
        self.restarts = restarts
        self.actions = dict(actions or ACTIONS)
        self.emu_kw = emu_kw or {}
        self.recover_s = recover_s
        self.res = ChaosResult(seed)
        self.t0 = 0.0
        self.up = asyncio.Event()
        self.stop = False
        self.switches: List[float] = []      # wall times of mode switches the camera got
        self.bad_stopstreams: List[str] = []
        self.wedges: List[str] = []
        self.stream_open_at_stop: List[bool] = []
        self.loop_errors: List[str] = []
        self.jobs_before_restart: Dict[str, Any] = {}

    # ------------------------------------------------------------ plumbing
    def note(self, who: str, what: str) -> None:
        self.res.trace.append("%6.2f %-8s %s" % (time.monotonic() - self.t0, who, what))

    def _hook_emulator(self, rig: Rig) -> None:
        cam = rig.emu
        orig = cam.record

        def record(kind, what, result, ip=""):
            if kind == "cgi" and what.startswith("mode=camcmd"):
                v = what.split("value=", 1)[-1].split("&")[0]
                if v in MODE_SWITCHES:
                    self.switches.append(time.time())
                    self.note("camera", "%s -> %s" % (v, result))
            if kind == "cgi" and "mode=stopstream" in what:
                if result != "ok":
                    self.bad_stopstreams.append("%.2f stopstream -> %s" % (time.monotonic() - self.t0, result))
            if kind == "wedge":
                self.wedges.append("%.2f %s" % (time.monotonic() - self.t0, what))
                self.note("camera", "WEDGE " + what)
            return orig(kind, what, result, ip)
        cam.record = record

    async def call(self, rig: Rig, who: str, method: str, path: str, body=None,
                   timeout: float = 30.0) -> Any:
        if rig.client is None:
            return None
        try:
            st, d = await asyncio.wait_for(rig.call(method, path, body), timeout)
        except asyncio.TimeoutError:
            self.note(who, "%s %s TIMEOUT" % (method, path))
            self.res.stats["timeouts"] = self.res.stats.get("timeouts", 0) + 1
            if not self.stop:
                self.res.violations.append("%s %s hung > %.0f s" % (method, path, timeout))
            return None
        except (aiohttp.ClientError, RuntimeError, AttributeError, ConnectionError) as e:
            self.note(who, "%s %s %s" % (method, path, type(e).__name__))
            return None
        short = d
        if isinstance(d, dict):
            short = {k: d[k] for k in ("result", "error", "state", "left", "id") if k in d}
            if "job" in d and isinstance(d["job"], dict):
                short["job"] = d["job"].get("state")
        self.note(who, "%s %s -> %s %s" % (method, path, st, json.dumps(short, default=str)[:120]))
        if st >= 500 and not (st == 503 and "liveview" in path):   # 503 = no frame (yet)
            self.res.violations.append("%s %s -> %s %s" % (method, path, st, json.dumps(d, default=str)[:200]))
        return st, d

    # ------------------------------------------------------------ actions
    async def act(self, rig: Rig, who: str, rnd: random.Random, name: str) -> None:
        c = lambda m, p, b=None, **kw: self.call(rig, who, m, p, b, **kw)  # noqa: E731
        if name == "photo":
            await c("POST", "/api/capture")
        elif name == "video":
            await c("POST", "/api/video/start")
            await asyncio.sleep(rnd.uniform(0.1, 2.0))
            await c("POST", "/api/video/stop")
        elif name == "setting":
            r = await c("GET", "/api/quick")
            items = (r[1] or {}).get("items", []) if r else []
            items = [i for i in items if i["setting"]["options"]]
            if items:
                it = rnd.choice(items)
                opts = [o for o in it["setting"]["options"] if o.get("value") is not None]
                if opts:
                    o = rnd.choice(opts)
                    await c("POST", "/api/settings/%s" % it["setting"]["id"], {"option": o["id"]})
        elif name == "viewer":
            await self.viewer(rig, who, rnd.uniform(0.2, 3.0))
        elif name == "lv_quality":
            await c("POST", "/api/liveview/quality", {"liveview_quality": rnd.choice(["vga", "qvga"])})
        elif name == "frame":
            await c("GET", "/api/liveview.jpg?after=%d" % rnd.randint(0, 5), timeout=10)
        elif name == "files_browse":
            r = await c("GET", "/api/files?limit=%d" % rnd.choice([5, 20, 50]))
            if r and r[0] == 200 and r[1] and r[1].get("items") and rnd.random() < 0.5:
                await c("GET", "/api/files/%s/thumb" % rnd.choice(r[1]["items"])["id"])
        elif name == "files_done":
            await c("POST", "/api/files/done")
        elif name == "import":
            r = await c("GET", "/api/files?limit=3&kind=photo")
            if r and r[0] == 200 and r[1] and r[1].get("items"):
                ids = [i["id"] for i in r[1]["items"]][:rnd.randint(1, 2)]
                await c("POST", "/api/files/import", {"ids": ids, "dest": self.dest, "include_raw": False})
        elif name == "job_ctl":
            r = await c("GET", "/api/jobs")
            jobs = (r[1] or {}).get("jobs", []) if r and isinstance(r[1], dict) else []
            if jobs:
                j = rnd.choice(jobs)
                await c("POST", "/api/jobs/%s/%s" % (j["id"], rnd.choice(["pause", "resume", "cancel"])))
        elif name == "tl_start":
            body = {"interval": rnd.choice([2, 3]), "count": rnd.randint(2, 5),
                    "native": rnd.random() < 0.4, "name": "c%d" % rnd.randint(0, 99)}
            await c("POST", "/api/timelapse/start", body)
        elif name in ("tl_pause", "tl_resume", "tl_stop"):
            await c("POST", "/api/timelapse/" + name[3:])
        elif name == "reconnect":
            await c("POST", "/api/connection/reconnect", {})
        elif name == "ws":
            await self.websocket(rig, who, rnd.uniform(0.3, 4.0))
        elif name == "abort":
            path, body = rnd.choice([
                ("/api/capture", {}), ("/api/video/start", {}), ("/api/video/stop", {}),
                ("/api/liveview/quality", {"liveview_quality": rnd.choice(["vga", "qvga"])}),
                ("/api/files/done", {}), ("/api/timelapse/stop", {}),
                ("/api/timelapse/start", {"interval": 2, "count": 3}),
                ("/api/connection/reconnect", {}),
            ])
            await self.abort(rig, who, path, body, rnd.uniform(0.01, 0.4))
        elif name == "poll":
            await c("GET", rnd.choice(["/api/status", "/api/timelapse", "/api/jobs", "/api/files/status",
                                       "/api/connection", "/api/power/plan", "/api/liveview/quality"]))
        elif name == "touch":
            await c("POST", "/api/touch", {"x": rnd.randint(0, 1000), "y": rnd.randint(0, 1000)})
        elif name == "af":
            await c("POST", "/api/af")

    async def viewer(self, rig: Rig, who: str, hold: float) -> None:
        if rig.client is None:
            return
        url = str(rig.client.make_url("/api/liveview.mjpg"))
        n = 0
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=hold + 5)) as r:
                    end = time.monotonic() + hold
                    while time.monotonic() < end:
                        try:
                            chunk = await asyncio.wait_for(r.content.readany(), max(0.05, end - time.monotonic()))
                        except asyncio.TimeoutError:
                            break
                        if not chunk:
                            break
                        n += chunk.count(b"--lumixframe")
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError):
            pass
        self.note(who, "viewer %.1fs: %d frames" % (hold, n))

    async def websocket(self, rig: Rig, who: str, hold: float) -> None:
        if rig.client is None:
            return
        url = str(rig.client.make_url("/api/ws"))
        n = 0
        try:
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(url) as w:
                    end = time.monotonic() + hold
                    while time.monotonic() < end:
                        try:
                            m = await asyncio.wait_for(w.receive(), max(0.05, end - time.monotonic()))
                        except asyncio.TimeoutError:
                            break
                        if m.type != aiohttp.WSMsgType.TEXT:
                            break
                        n += 1
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError):
            pass
        self.note(who, "ws %.1fs: %d msgs" % (hold, n))

    async def abort(self, rig: Rig, who: str, path: str, body: Any, after: float) -> None:
        if rig.client is None:
            return
        url = str(rig.client.make_url(path))
        try:
            async with aiohttp.ClientSession() as s:
                await asyncio.wait_for(s.post(url, json=body), after)
                self.note(who, "abort %s: finished before %.2fs" % (path, after))
                return
        except asyncio.TimeoutError:
            pass
        except (aiohttp.ClientError, ConnectionError):
            pass
        self.note(who, "abort %s after %.2fs" % (path, after))

    # ------------------------------------------------------------ actors
    async def tab(self, rig: Rig, i: int) -> None:
        rnd = random.Random("%d-tab-%d" % (self.seed, i))
        names = list(self.actions)
        weights = [self.actions[n] for n in names]
        who = "tab%d" % i
        while not self.stop:
            await self.up.wait()
            name = rnd.choices(names, weights)[0]
            try:
                await self.act(rig, who, rnd, name)
            except Exception as e:  # harness bug or unexpected client error: record, go on
                self.note(who, "%s raised %s: %s" % (name, type(e).__name__, e))
                self.res.violations.append("action %s raised %s: %s" % (name, type(e).__name__, e))
            self.res.stats[name] = self.res.stats.get(name, 0) + 1
            await asyncio.sleep(rnd.uniform(0.0, 0.6))

    async def director(self, rig: Rig) -> None:
        rnd = random.Random("%d-director" % self.seed)
        times = sorted(rnd.uniform(0.25, 0.85) * self.duration for _ in range(self.restarts))
        for t in times:
            await asyncio.sleep(max(0.0, t - (time.monotonic() - self.t0)))
            if self.stop:
                return
            self.up.clear()
            before = rig.job()
            self.note("director", "server restart (job %s)" % (before.state if before else None))
            await self.restart_stop(rig)
            await asyncio.sleep(rnd.uniform(0.0, 0.5))
            try:
                await rig.start_server()
            except AssertionError as e:
                self.res.violations.append("server not ready after restart: %s" % str(e).splitlines()[0])
            after = rig.job() if rig.mgr else None
            if before is not None and before.state not in ("done", "stopped", "failed"):
                if after is None or after.id != before.id:
                    self.res.violations.append("time-lapse job %s (%s) lost in a server restart"
                                               % (before.id, before.state))
            self.res.stats["restarts"] = self.res.stats.get("restarts", 0) + 1
            self.note("director", "server back (job %s)" % (after.state if after else None))
            self.up.set()

    async def restart_stop(self, rig: Rig) -> None:
        """Stop the server like SIGTERM does: graceful shutdown, then (as
        web.run_app does before the process exits) cancel whatever request
        handlers are still running, so nothing of the old server keeps talking
        to the camera next to the new one."""
        from server.app import DETACHED
        app = rig.client.server.app if rig.client is not None else None
        try:
            await asyncio.wait_for(rig.stop_server(), 20)
        except asyncio.TimeoutError:
            self.res.violations.append("server stop hung > 20 s")
        if app is not None:
            left = [t for t in app[DETACHED] if not t.done()]
            for t in left:
                t.cancel()
            if left:
                self.note("director", "cancelled %d request(s) still running at exit" % len(left))
                await asyncio.wait(left, timeout=5)

    # ------------------------------------------------------------ run
    async def run_async(self, rig: Rig) -> None:
        self.t0 = time.monotonic()
        self._hook_emulator(rig)
        loop = asyncio.get_event_loop()

        def on_error(loop_, ctx):
            msg = ctx.get("message", "")
            exc = ctx.get("exception")
            self.loop_errors.append("%s %r" % (msg, exc))
        loop.set_exception_handler(on_error)
        self.up.set()
        tasks = [asyncio.ensure_future(self.tab(rig, i)) for i in range(self.tabs)]
        tasks.append(asyncio.ensure_future(self.director(rig)))
        await asyncio.sleep(self.duration)
        self.stop = True
        self.up.set()
        done, pending = await asyncio.wait(tasks, timeout=40)
        for t in pending:
            t.cancel()
            self.res.violations.append("an actor did not finish within 40 s after the stop")
        await self.recover(rig)

    async def recover(self, rig: Rig) -> None:
        """Traffic stopped: like a user closing Files and waiting, the server must
        get back to ready by itself."""
        if rig.client is None:
            await rig.start_server()
        self.note("check", "recovery")
        await self.call(rig, "check", "POST", "/api/files/done")
        end = time.monotonic() + self.recover_s
        cam = rig.sess.cam
        while time.monotonic() < end and cam.status != "ready":
            await asyncio.sleep(0.2)
        if cam.status != "ready":
            self.res.violations.append("server not ready %.0f s after the traffic stopped: %s (%s)"
                                       % (self.recover_s, cam.status, cam.status_msg))
        # give a running time-lapse a moment, then its state must not be failed
        # a run the shutter started just now: give the session's watcher time to adopt it
        end = time.monotonic() + 6.0
        while time.monotonic() < end and rig.emu.interval.get("state") in ("rec", "pause") \
                and not (rig.job() is not None and rig.job().state in ("running", "paused")):
            await asyncio.sleep(0.2)
        j = rig.job()
        run = rig.emu.interval.get("state")
        if run in ("rec", "pause") and not (j is not None and j.state in ("running", "paused")
                                            and j.engine == "native"):
            self.res.violations.append("in-camera interval run (%s) the server does not own: the UI "
                                       "cannot stop it" % run)
        if j is not None and j.state == "failed":
            self.res.violations.append("time-lapse job failed: %s" % j.message)
        for jid, fj in rig.sess.files.jobs.items():
            if fj.state == "failed":
                self.res.violations.append("import job %s failed: %s" % (jid, fj.errors[-1:] if fj.errors else ""))

    def check(self, rig: Rig) -> None:
        emu = rig.emu
        for w in self.wedges:
            self.res.violations.append("emulator wedged: " + w)
        if (emu.wedged or emu.hard_wedged or emu.settings_wedged) and not self.wedges:
            self.res.violations.append("emulator wedged: %s" % emu.wedge_reason)
        for s in self.bad_stopstreams:
            self.res.violations.append("stopstream without a stream: " + s)
        from lumix.camera import Camera
        gap, n, win = Camera.MODE_SWITCH_MIN_GAP_S, Camera.MODE_SWITCH_MAX, Camera.MODE_SWITCH_WINDOW_S
        tol = 0.05
        sw = self.switches
        for i in range(1, len(sw)):
            if sw[i] - sw[i - 1] < gap - tol:
                self.res.violations.append("mode switches %.2f s apart (cap %.2f s) at t=%.2f"
                                           % (sw[i] - sw[i - 1], gap, sw[i] - self.wall0))
            if i >= n and sw[i] - sw[i - n] < win - tol:
                self.res.violations.append("%d mode switches within %.2f s (cap %d per %.2f s) at t=%.2f"
                                           % (n + 1, sw[i] - sw[i - n], n, win, sw[i] - self.wall0))
        for e in self.loop_errors:
            if "Unclosed" in e:
                continue
            self.res.violations.append("event loop: " + e[:200])
        self.res.stats["mode_switches"] = len(sw)


def run_seed(seed: int, duration: float = 20.0, tabs: int = 3, restarts: int = 1,
             actions: Optional[Dict[str, int]] = None, emu_kw: Optional[Dict[str, Any]] = None,
             quiet: bool = True, timing: str = "fast") -> ChaosResult:
    """One chaos run in a fresh loop, emulator, server and config dir.

    timing "fast": server and camera timing x0.05 (FAST_CAMERA, chaos_emu_cfg);
    "real": the server's real mode-switch cap and settle rules against the
    camera's real churn/settle windows (slow: use a long duration)."""
    from lumix.camera import Camera
    if quiet:
        logging.disable(logging.WARNING)
    if timing == "real":
        emu_kw = dict(dict(settle_min=5.0, settle_max=22.0, churn_window_s=20.0, interval_scale=0.2,
                           capture_busy_s=1.2, video_busy_s=2.0), **(emu_kw or {}))
    p = mock.patch.multiple(Camera, create=True, **(FAST_CAMERA if timing == "fast" else {"STABLE_S": 2.0}))
    p.start()
    tmp = tempfile.TemporaryDirectory(prefix="lumix-chaos-")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ch = Chaos(seed, duration, tabs, restarts, actions, emu_kw, recover_s=45.0 if timing == "fast" else 120.0)
    ch.dest = os.path.join(tmp.name, "Pictures")
    ch.wall0 = time.time()
    rig = ChaosRig(loop, os.path.join(tmp.name, "home"), chaos_emu_cfg(seed=seed, **(emu_kw or {})))
    rig.ready_s = 20.0 if timing == "fast" else 60.0
    try:
        rig.run(rig.start_emu())
        rig.run(rig.start_server())
        rig.run(ch.run_async(rig), timeout=duration + 200)
        ch.check(rig)
    except Exception as e:
        ch.res.violations.append("harness: %s: %s" % (type(e).__name__, str(e).splitlines()[0] if str(e) else ""))
    finally:
        try:
            rig.run(rig.stop_all(), timeout=30)
        except Exception:
            pass
        loop.run_until_complete(asyncio.sleep(0.1))
        loop.close()
        asyncio.set_event_loop(None)
        tmp.cleanup()
        p.stop()
        if quiet:
            logging.disable(logging.NOTSET)
    return ch.res


def parse_seeds(spec: str) -> List[int]:
    out: List[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out
