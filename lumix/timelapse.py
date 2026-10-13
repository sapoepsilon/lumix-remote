"""Time-lapse per camera: two engines, one job model.

``server`` engine
    The server takes one photo per interval (``camcmd capture`` under the camera
    lock, one shot = one short lock hold, so UI commands interleave). Shots sit on
    a fixed grid (``anchor_t + k * interval``); a slot that cannot be shot is
    skipped, never queued, so a busy or reconnecting camera never gets a burst
    of captures afterwards:

    * camera reports ``busy`` (someone uses the body): wait up to half an interval
      without sending anything, then skip the slot;
    * ``err_busy`` (card write still running): at most two quick retries (1 s
      apart) inside the slot, then skip; never counted as the wedge;
    * camera reconnecting: the job waits, missed slots are counted, shooting
      resumes on the grid when the camera is back (AE lock re-applied);
    * camera left rec mode on its own (``Camera.auto_rec``): slots are skipped
      while it is given time to come back by itself (reads only), then one
      recmode, at most two per 5 min; when that does not hold the job PAUSES
      (``paused_by="camera"``, ``job.message`` says why) instead of fighting
      the camera, which wedged the real S5 on 2026-10-12 16:52;
    * a capture the camera answers ok but then reports as failed
      (``X_Panasonic_Cam_PRec`` err_rec, no photo written) is not counted as a
      shot; two in a row pause the job the same way.

    ``count`` is the number of photos taken (skipped slots extend the run);
    ``duration`` caps the run on the slot grid (pauses do not count).

``native`` engine
    The camera's own interval shooting: drive mode ``stopmotion_interval``
    (value2 ``interval``), ``interval_shoot_interval`` / ``interval_shoot_num`` /
    ``interval_start_time``, then the shutter (``camcmd capture``) starts the run;
    ``camctrl interval pause|restart|stop``. LUMIX Sync also sends ``camctrl
    interval set`` before the shutter (docs/lumix-sync-wifi-flow.md section 10),
    but a DC-S5 (fw 2.03) answers ``err_reject`` to it whenever getstate already
    says ``interval_status=enable`` (verified 2026-10-12: set -> err_reject, then
    capture -> ok started a 3-shot run), so it is only sent when the camera is not
    ready yet. getstate ``interval_status``: ``enable`` (armed, idle), ``rec``
    (running; other models may say ``run``), ``pause``, ``off`` (just finished,
    warn_disp ``interval_finish``), then ``enable`` again; ``interval_remain_num``
    counts down. When the run cannot be armed the job falls back to the server
    engine instead of failing (``job.message`` says why). Only for profiles with
    ``capabilities["interval_native"]``. While it runs the body answers
    ``err_busy`` to capture/mode switches, so the UI's shutter and file browsing
    are refused (``Camera.interval_active``).

    The run does not need the server. A server stop / restart leaves it shooting
    (no ``camctrl interval pause``), and the next start ADOPTS it: getstate
    ``interval_status`` / ``interval_remain_num`` re-attach the persisted job
    (or a new "camera run" job when there is none) without sending anything but
    reads; a run paused on the body stays paused until the user resumes. Only
    an explicit Pause pauses the camera. A run the camera no longer has (power
    cycle, cancelled on the body) is never re-armed behind the user's back: the
    job is marked done when its time is up, else paused with Resume re-arming
    the remaining shots. (2026-10-12 11:55: pause on shutdown + playmode on
    reconnect + re-arm setsetting wedged the real S5.)

Job state (``cameras/<id>/timelapse.json``) is written on every change, so a
server restart resumes a running job; a job paused by power-off / server stop
resumes when the camera is back (``resume_on_reconnect``). Frames can be
imported to ``~/Pictures/Lumix/<date>/timelapse-<name>/`` (after every shot when
the interval leaves room for the play-mode round trip, else at the end) and
assembled into an MP4 with ffmpeg when it is installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from .protocol import CamError

log = logging.getLogger("lumix.timelapse")

ACTIVE = ("scheduled", "running", "paused")
FINISHING = ("stopping", "importing", "assembling")
TERMINAL = ("done", "stopped", "failed")
# Import root; LUMIX_IMPORT_DEST overrides it (QA, other disks).
DEFAULT_DEST = os.environ.get("LUMIX_IMPORT_DEST") or "~/Pictures/Lumix"
IMPORT_EACH_MIN_S = 30.0       # play-mode round trip per shot needs about this much room
MIN_INTERVAL_S = 1.0
MAX_INTERVAL_S = 24 * 3600.0
MAX_COUNT = 99999
REC_RETRY_S = 60.0             # cameras without Camera.auto_rec: one live-view restart per minute
# Photo recording errors (X_Panasonic_Cam_PRec err_*) in a row that pause the job
PHOTO_ERROR_PAUSE = 2
# Quiesce reasons whose pauses resume when the camera is back (resume_on_reconnect)
RECONNECT_RESUMABLE = ("poweroff", "server-stop", "reconnect")


# getstate interval_status while an in-camera run is going (S5: rec / pause)
INTERVAL_RUNNING = ("run", "rec", "pause")
INTERVAL_SHOOTING = ("run", "rec")


class TimelapseError(ValueError):
    pass


class RunInProgress(TimelapseError):
    """The camera is already running an interval run: adopt it, never arm into it."""


def _drive_value(r: Any) -> Optional[List[Optional[str]]]:
    """getsetting drivemode -> [value, value2]: the S5 answers
    ``<settingvalue drivemode="stopmotion_interval">interval</settingvalue>``."""
    if not getattr(r, "ok", False) or getattr(r, "xml", None) is None:
        return None
    el = r.xml.find("settingvalue")
    if el is None or el.get("drivemode") is None:
        return None
    return [el.get("drivemode"), (el.text or "").strip() or None]


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text or "").strip("-.")[:48]
    return s or time.strftime("%Y%m%d-%H%M")


def parse_start_at(v: Any, now: Optional[float] = None) -> Optional[float]:
    """Epoch seconds, ISO 8601 ("2026-10-12T06:30", local time unless an offset
    is given), "HH:MM" (today, or tomorrow when already past) or "+SECONDS"."""
    if v in (None, "", 0):
        return None
    now = time.time() if now is None else now
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s.startswith("+"):
        return now + float(s[1:])
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if m:
        base = dt.datetime.fromtimestamp(now)
        t = base.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                         second=int(m.group(3) or 0), microsecond=0)
        if t.timestamp() <= now:
            t += dt.timedelta(days=1)
        return t.timestamp()
    try:
        return float(s)
    except ValueError:
        pass
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise TimelapseError("start_at: use epoch seconds, ISO time, HH:MM or +SECONDS")
    return d.timestamp()


class TimelapseJob:
    FIELDS = ("id", "name", "engine", "interval_s", "count", "duration_s", "start_at", "ae_lock",
              "auto_import", "import_mode", "video", "fps", "dest", "folder",
              "state", "paused_by", "paused_at", "waiting", "message",
              "created", "started_at", "finished_at",
              "slot", "anchor_t", "anchor_slot", "shots", "skipped", "missed", "failed",
              "last_shot_at", "next_shot_at", "last_result", "frames", "frames_imported",
              "import_error", "video_path", "video_error", "fixed", "native", "skips", "errors",
              "has_frame", "frame_at")

    def __init__(self, **kw: Any):
        self.id = uuid.uuid4().hex[:12]
        self.name = ""
        self.engine = "server"
        self.interval_s = 10.0
        self.count: Optional[int] = None
        self.duration_s: Optional[float] = None
        self.start_at: Optional[float] = None
        self.ae_lock = False
        self.auto_import = False
        self.import_mode = "end"        # "each" | "end"
        self.video = False
        self.fps = 24
        self.dest = os.path.abspath(os.path.expanduser(DEFAULT_DEST))
        self.folder = ""
        self.state = "scheduled"
        self.paused_by: Optional[str] = None
        self.paused_at: Optional[float] = None
        self.waiting: Optional[str] = None
        self.message: Optional[str] = None
        self.created = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.slot = 0                   # slots passed (shot, skipped or missed)
        self.anchor_t: Optional[float] = None
        self.anchor_slot = 0
        self.shots = 0
        self.skipped = 0                # slots skipped while connected (busy, ...)
        self.missed = 0                 # slots missed while the camera was away
        self.failed = 0
        self.last_shot_at: Optional[float] = None
        self.next_shot_at: Optional[float] = None
        self.last_result: Optional[str] = None
        self.frames: List[str] = []     # imported files, in shooting order
        self.frames_imported = 0
        self.import_error: Optional[str] = None
        self.video_path: Optional[str] = None
        self.video_error: Optional[str] = None
        self.fixed: Dict[str, Optional[str]] = {}
        self.native: Dict[str, Any] = {}
        self.skips: List[Dict[str, Any]] = []
        self.errors: List[str] = []
        self.has_frame = False
        self.frame_at: Optional[float] = None
        for k, v in kw.items():
            if k in self.FIELDS:
                setattr(self, k, v)

    # ------------------------------------------------------------- schedule
    @property
    def slot_limit(self) -> Optional[int]:
        """Slots that fit into the duration (first shot at 0 s)."""
        if not self.duration_s:
            return None
        return int(self.duration_s // self.interval_s) + 1

    @property
    def target(self) -> Optional[int]:
        """Photos the run is expected to take."""
        lim = self.slot_limit
        if self.count and lim:
            return min(self.count, lim)
        return self.count or lim

    def slot_time(self, k: Optional[int] = None) -> float:
        k = self.slot if k is None else k
        assert self.anchor_t is not None
        return self.anchor_t + (k - self.anchor_slot) * self.interval_s

    def reanchor(self, t: Optional[float] = None) -> None:
        self.anchor_t, self.anchor_slot = (time.time() if t is None else t), self.slot

    def finished_shooting(self) -> bool:
        if self.count and self.shots >= self.count:
            return True
        lim = self.slot_limit
        return bool(lim and self.slot >= lim)

    def remaining(self) -> Optional[int]:
        out = []
        if self.count:
            out.append(max(0, self.count - self.shots))
        if self.slot_limit:
            out.append(max(0, self.slot_limit - self.slot))
        return min(out) if out else None

    def eta_s(self, now: Optional[float] = None) -> Optional[float]:
        now = time.time() if now is None else now
        rem = self.remaining()
        if rem is None or self.state not in ACTIVE:
            return None
        if rem == 0:
            return 0.0
        if self.state == "scheduled" and self.start_at:
            return max(0.0, self.start_at - now) + (rem - 1) * self.interval_s
        nxt = self.next_shot_at if self.next_shot_at and self.state == "running" else now
        return max(0.0, nxt - now) + (rem - 1) * self.interval_s

    def progress(self) -> float:
        tgt = self.target
        if not tgt:
            return 0.0
        if self.count:
            return round(min(1.0, self.shots / tgt), 4)
        return round(min(1.0, self.slot / tgt), 4)

    # --------------------------------------------------------------- export
    def to_dict(self) -> Dict[str, Any]:
        d = {k: getattr(self, k) for k in self.FIELDS}
        d["frames"] = self.frames[-5:]
        d["frames_total"] = len(self.frames)
        d["skips"] = self.skips[-10:]
        d["errors"] = self.errors[-10:]
        d.update(target=self.target, remaining=self.remaining(), eta_s=self.eta_s(),
                 progress=self.progress(), slot_limit=self.slot_limit, now=time.time())
        return d

    def save_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.FIELDS}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TimelapseJob":
        return cls(**{k: v for k, v in d.items() if k in cls.FIELDS})

    def summary(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in ("id", "name", "engine", "state", "interval_s", "count",
                                             "shots", "skipped", "missed", "created", "finished_at",
                                             "folder", "video_path")}


class TimelapseEngine:
    """One per camera session. ``cam`` is a lumix.camera.Camera, ``files`` its
    FileBrowser; ``dir`` is the camera's config directory."""

    def __init__(self, cam: Any, files: Any, dir: str,
                 settings: Optional[Callable[[], Dict[str, Any]]] = None):
        self.cam = cam
        self.files = files
        self.dir = dir
        self.path = os.path.join(dir, "timelapse.json")
        self.frame_path = os.path.join(dir, "timelapse-last.jpg")
        self.settings = settings or (lambda: {})
        self.job: Optional[TimelapseJob] = None
        self.history: List[Dict[str, Any]] = []
        self._task: Optional[asyncio.Task] = None
        self._shot_lock_: Optional[asyncio.Lock] = None
        self._epoch: Optional[float] = None       # cam.connected_at the job prepared for
        self._last_rec_try = 0.0
        self._photo_err_streak = 0
        self._last_pub = 0.0
        self.load()

    @property
    def _shot_lock(self) -> asyncio.Lock:
        # created lazily inside the running loop (Python 3.9 binds locks to a loop)
        if self._shot_lock_ is None:
            self._shot_lock_ = asyncio.Lock()
        return self._shot_lock_

    # ------------------------------------------------------------ persistence
    def load(self) -> None:
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if data.get("job"):
            try:
                self.job = TimelapseJob.from_dict(data["job"])
            except (TypeError, ValueError) as e:
                log.warning("cannot load timelapse job: %s", e)
        self.history = list(data.get("history") or [])[-20:]

    def save(self) -> None:
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"version": 1, "job": self.job.save_dict() if self.job else None,
                           "history": self.history[-20:]}, f, indent=1, default=str)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("cannot save timelapse state: %s", e)

    def publish(self, force: bool = True) -> None:
        self._last_pub = time.time()
        with contextlib.suppress(Exception):
            self.cam.bus.publish("timelapse", self.status())

    def _changed(self) -> None:
        self.save()
        self.publish()

    # ------------------------------------------------------------------ info
    def capabilities(self) -> Dict[str, Any]:
        caps = getattr(getattr(self.cam, "profile", None), "capabilities", {}) or {}
        return {"native": bool(caps.get("interval_native")),
                "native_max_count": caps.get("interval_max_count"),
                "native_min_interval_s": caps.get("interval_min_s"),
                "ffmpeg": shutil.which("ffmpeg") is not None,
                "import_each_min_s": IMPORT_EACH_MIN_S,
                "default_dest": os.path.abspath(os.path.expanduser(DEFAULT_DEST))}

    def status(self) -> Dict[str, Any]:
        j = self.job
        return {"job": j.to_dict() if j else None, "active": bool(j and j.state in ACTIVE + FINISHING),
                "history": self.history[-10:], "capabilities": self.capabilities(),
                "frame_url": "/api/timelapse/frame.jpg" if j and j.has_frame else None}

    def is_active(self) -> bool:
        return bool(self.job and self.job.state in ACTIVE + FINISHING)

    def is_running(self) -> bool:
        return bool(self.job and self.job.state in ("scheduled", "running"))

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Session start: pick up a persisted job (server restart / crash)."""
        j = self.job
        if j is None:
            return
        if j.state in ("scheduled", "running"):
            if j.state == "running" and j.engine == "server":
                j.message = "resumed after a server restart"
            self._spawn()
        elif j.state in FINISHING:
            self._spawn(self._finish(j, "stopped" if j.state == "stopping" else "done"))

    async def shutdown(self) -> None:
        """Session stop: cancel the task; the persisted state stays as it is."""
        t, self._task = self._task, None
        if t is not None and not t.done():
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if self.cam.interval_owner == "timelapse":
            self.cam.interval_owner = None

    def _spawn(self, coro: Any = None) -> None:
        if self._task is not None and not self._task.done():
            if coro is not None:
                coro.close()
            return
        self._task = asyncio.ensure_future(coro if coro is not None else self._run())

    async def _cancel_task(self, timeout: float = 10.0) -> None:
        """Stop the runner after the shot in flight (if any) is done."""
        t = self._task
        if t is None or t.done():
            return
        if t is asyncio.current_task():
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._shot_lock.acquire(), timeout)
            self._shot_lock.release()
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
            await asyncio.wait_for(asyncio.shield(t), 5)
        self._task = None

    # ---------------------------------------------------------------- control
    def new_job(self, p: Dict[str, Any]) -> TimelapseJob:
        """Validate start parameters into a job (TimelapseError on bad input)."""
        native = bool(p.get("native")) or p.get("engine") == "native"
        try:
            interval = float(p.get("interval", p.get("interval_s", 0)))
        except (TypeError, ValueError):
            raise TimelapseError("interval must be a number of seconds")
        if not MIN_INTERVAL_S <= interval <= MAX_INTERVAL_S:
            raise TimelapseError("interval must be %g..%g s" % (MIN_INTERVAL_S, MAX_INTERVAL_S))
        count = p.get("count")
        duration = p.get("duration", p.get("duration_s"))
        try:
            count = int(count) if count not in (None, "", 0) else None
            duration = float(duration) if duration not in (None, "", 0) else None
        except (TypeError, ValueError):
            raise TimelapseError("count must be an integer, duration a number of seconds")
        if count is not None and not 1 <= count <= MAX_COUNT:
            raise TimelapseError("count must be 1..%d" % MAX_COUNT)
        if duration is not None and duration <= 0:
            raise TimelapseError("duration must be positive")
        if count is None and duration is None:
            raise TimelapseError("give a count, a duration or both")
        start_at = parse_start_at(p.get("start_at"))
        if start_at is not None and start_at > time.time() + 7 * 86400:
            raise TimelapseError("start_at is more than a week away")
        caps = self.capabilities()
        if native:
            if not caps["native"]:
                raise TimelapseError("this camera model has no in-camera interval shooting "
                                     "(profile capability interval_native); use the server engine")
            if interval != int(interval):
                raise TimelapseError("in-camera interval takes whole seconds")
            if count is None:
                count = int((duration or 0) // interval) + 1
                duration = None
            mx = caps.get("native_max_count")
            if mx and count > mx:
                raise TimelapseError("the camera takes at most %d shots per interval run" % mx)
        video = bool(p.get("video"))
        auto_import = bool(p.get("import", p.get("auto_import"))) or video
        mode = str(p.get("import_mode") or "auto")
        if mode not in ("auto", "each", "end"):
            raise TimelapseError("import_mode must be auto, each or end")
        if mode == "auto":
            mode = "each" if (not native and interval >= IMPORT_EACH_MIN_S) else "end"
        if native:
            mode = "end"
        try:
            fps = max(1, min(120, int(p.get("fps") or 24)))
        except (TypeError, ValueError):
            raise TimelapseError("fps must be an integer")
        name = _slug(str(p.get("name") or time.strftime("%Y%m%d-%H%M")))
        dest = os.path.abspath(os.path.expanduser(str(p.get("dest") or DEFAULT_DEST)))
        day = dt.date.fromtimestamp(start_at or time.time()).isoformat()
        folder = os.path.join(dest, day, "timelapse-%s" % name)
        return TimelapseJob(name=name, engine="native" if native else "server", interval_s=interval,
                            count=count, duration_s=duration, start_at=start_at,
                            ae_lock=bool(p.get("ae_lock")), auto_import=auto_import,
                            import_mode=mode, video=video, fps=fps, dest=dest, folder=folder,
                            state="scheduled")

    async def start_job(self, p: Dict[str, Any]) -> Dict[str, Any]:
        if self.is_active():
            raise TimelapseError("a timelapse is already %s on this camera (stop it first)"
                                 % self.job.state)  # type: ignore[union-attr]
        job = self.new_job(p)
        if self.cam.status != "ready":
            raise TimelapseError("camera not connected (%s)" % self.cam.status)
        if getattr(self.files, "active", False):
            # files -> shooting: running imports are checkpointed, camera back to rec
            await self.files.leave(force=True, pause_jobs=True)
            if self.files.active:
                raise TimelapseError("could not leave file browsing (camera stays in play mode)")
        if self.cam.status != "ready":
            raise TimelapseError("camera not connected (%s)" % self.cam.status)
        mid = getattr(self.cam, "mode_switch_blocked", None)
        why = await mid() if mid is not None else None
        if why:
            raise TimelapseError("camera busy: %s; a new timelapse would have to change its "
                                 "mode/settings now" % why)
        if self.job is not None:
            self.history.append(self.job.summary())
        self.job = job
        with contextlib.suppress(OSError):
            os.remove(self.frame_path)
        self.cam.log_event("timelapse", "timelapse %s (%s engine): every %gs, %s%s" % (
            job.name, job.engine, job.interval_s,
            "%d shots" % job.count if job.count else "%gs" % job.duration_s,
            ", starts %s" % time.strftime("%H:%M:%S", time.localtime(job.start_at))
            if job.start_at else ""))
        self._changed()
        self._spawn()
        return self.status()

    async def pause(self, by: str = "user", timeout: float = 10.0) -> Dict[str, Any]:
        j = self.job
        if j is None or j.state not in ("scheduled", "running"):
            if by == "user":
                raise TimelapseError("no running timelapse to pause")
            return self.status()
        await self._cancel_task(timeout)
        j.state, j.paused_by, j.paused_at, j.waiting = "paused", by, time.time(), None
        j.next_shot_at = None
        if j.engine == "native":
            await self._native_ctrl(j, "pause")
        self.cam.log_event("timelapse", "timelapse %s paused (%s) after %d shot(s)" % (j.name, by, j.shots))
        self._changed()
        return self.status()

    async def resume(self, by: Optional[str] = None) -> Dict[str, Any]:
        j = self.job
        if j is None or j.state != "paused":
            if by is None:
                raise TimelapseError("no paused timelapse to resume")
            return self.status()
        was = j.paused_by
        reset = getattr(self.cam, "reset_auto_rec", None)
        if reset is not None and was == "camera":
            reset()             # the user checked the camera: a fresh recmode budget
        self._photo_err_streak = 0
        if j.engine == "native":
            # an explicit resume (user, or the power-off resume): restart a run paused
            # on the camera / re-arm a lost one; a server restart only adopts
            j.native["resume_requested"] = True
        j.state = "running" if j.started_at else "scheduled"
        j.paused_by = j.paused_at = None
        j.anchor_t = None                   # re-anchored on the first slot after the pause
        j.message = "resumed (%s)" % (by or "user") if was != "user" or by else None
        self.cam.log_event("timelapse", "timelapse %s resumed (%s; was paused by %s)"
                           % (j.name, by or "user", was))
        self._changed()
        self._spawn()
        return self.status()

    async def auto_resume(self, trigger: str) -> bool:
        """Resume a job paused by a quiesce: ``trigger`` "reconnect" resumes
        power-off / server-stop pauses, "files" a pause for file browsing."""
        j = self.job
        if j is None or j.state != "paused" or not j.paused_by:
            return False
        if trigger == "files" and j.paused_by != "files":
            return False
        if trigger == "reconnect":
            if j.paused_by not in RECONNECT_RESUMABLE:
                return False
            # only a connection made after the pause counts (a late hook from the
            # connect before a power-off must not resume it)
            if (self.cam.status != "ready" or self.cam.powered_off
                    or (self.cam.connected_at or 0) < (j.paused_at or 0)):
                return False
        await self.resume(by=trigger)
        return True

    async def stop_job(self) -> Dict[str, Any]:
        """Stop early; frames taken so far are still imported/assembled."""
        j = self.job
        if j is None or j.state not in ACTIVE:
            raise TimelapseError("no active timelapse to stop")
        await self._cancel_task()
        j.state, j.waiting, j.next_shot_at = "stopping", None, None
        self._changed()
        self._spawn(self._finish(j, "stopped"))
        return self.status()

    # ----------------------------------------------------------------- runner
    async def _run(self) -> None:
        j = self.job
        if j is None:
            return
        try:
            if j.engine == "native":
                await self._run_native(j)
            else:
                await self._run_server(j)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never leave a job "running" without a task
            log.exception("timelapse runner failed")
            j.errors.append("%s: %s" % (type(e).__name__, e))
            j.state, j.finished_at, j.waiting = "failed", time.time(), None
            if self.cam.interval_owner == "timelapse":
                self.cam.interval_owner = None
            self.cam.log_event("warn", "timelapse %s failed: %s" % (j.name, e))
            self._changed()

    async def _wait_start(self, j: TimelapseJob) -> None:
        while j.start_at and time.time() < j.start_at:
            j.next_shot_at = j.start_at
            await asyncio.sleep(min(1.0, j.start_at - time.time()))
        if j.state == "scheduled":
            j.state, j.started_at = "running", time.time()
            self._changed()

    async def _wait_ready(self, j: TimelapseJob) -> bool:
        """Wait while the camera reconnects; True when it was away."""
        cam = self.cam
        if cam.status == "ready":
            return False
        j.waiting = "camera %s" % cam.status
        self._changed()
        while cam.status != "ready":
            await asyncio.sleep(0.5)
            w = "camera %s" % cam.status
            if w != j.waiting:
                j.waiting = w
                self.publish()
        j.waiting = None
        self.cam.log_event("timelapse", "timelapse %s: camera back, resuming on the grid" % j.name)
        self._changed()
        return True

    def _skip(self, j: TimelapseJob, reason: str, missed: int = 0) -> None:
        if missed:
            j.missed += missed
        else:
            j.skipped += 1
        j.skips.append({"t": time.time(), "slot": j.slot, "n": missed or 1, "reason": reason})
        j.skips = j.skips[-30:]
        self.cam.log_event("timelapse", "timelapse %s: %s %s (%s)" % (
            j.name, "missed" if missed else "skipped",
            "%d slot(s)" % missed if missed else "slot %d" % (j.slot + 1), reason))

    async def _run_server(self, j: TimelapseJob) -> None:
        await self._wait_start(j)
        await self._wait_ready(j)
        with contextlib.suppress(CamError, OSError, asyncio.TimeoutError):
            await self._prepare(j)
        if j.anchor_t is None:
            j.reanchor()
        while not j.finished_shooting():
            t_slot = j.slot_time()
            j.next_shot_at = t_slot
            self.publish()
            while True:
                d = t_slot - time.time()
                if d <= 0:
                    break
                await asyncio.sleep(min(d, 0.5))
            if await self._wait_ready(j) or self.cam.connected_at != self._epoch:
                with contextlib.suppress(CamError, OSError, asyncio.TimeoutError):
                    await self._prepare(j)
            late = time.time() - t_slot
            tol = self._tolerance(j)
            if late > tol:
                n = int(late // j.interval_s) + 1
                self._skip(j, "camera unavailable", missed=n)
                j.slot += n
                self._changed()
                continue
            try:
                await self._shoot(j, tol)
            except (CamError, OSError, asyncio.TimeoutError) as e:
                # the camera dropped off mid-shot: skip; the supervisor reconnects
                self._skip(j, "camera not reachable (%s)" % type(e).__name__)
            j.slot += 1
            self._changed()
            if j.state != "running":
                return          # paused by _camera_pause (camera trouble)
        j.next_shot_at = None
        j.state = "importing" if j.auto_import else "assembling" if j.video else j.state
        await self._finish(j, "done")

    @staticmethod
    def _tolerance(j: TimelapseJob) -> float:
        return max(1.5, min(j.interval_s * 0.5, 10.0))

    async def _prepare(self, j: TimelapseJob) -> None:
        """(Re)connect setup: rec mode, AE/AF lock and the fixed exposure."""
        cam = self.cam
        self._epoch = cam.connected_at
        if cam.status != "ready":
            return
        if j.engine == "server":
            await self._single_drive(j)
        if j.ae_lock and j.engine == "server":
            from .camera import Busy
            try:
                async with cam.exclusive("timelapse", timeout=10) as t:
                    if not j.fixed:
                        for k in ("focal", "shtrspeed", "iso"):
                            r = await t.cgi("getsetting", k, timeout=4)
                            j.fixed[k] = r.setting_value(k) if r.ok else None
                    else:
                        # after a reconnect / power cycle: put the run's exposure back
                        for k, v in j.fixed.items():
                            if v:
                                await t.cgi("setsetting", k, v, timeout=4)
                    r = await t.cgi("camctrl", "af_ae_lock", "on", timeout=4)
                    cam.log_event("timelapse", "timelapse %s: AE/AF lock -> %s, fixed %s"
                                  % (j.name, r.result, j.fixed))
            except Busy as e:
                j.errors.append("AE lock: %s" % e)
        self._changed()

    async def _single_drive(self, j: TimelapseJob) -> None:
        """Server engine: one capture must be one photo. In Time Lapse drive the
        shutter starts the camera's own interval run instead (burst / self-timer
        are wrong too), so the run uses single drive and the user's drive mode is
        put back when it ends (``_restore_drive``).

        Some S5 sessions answer ``setsetting drivemode=normal`` with ok and keep
        Time Lapse (2026-10-12, curmenu drivemode enable="no"). Then every shot is
        a one-shot in-camera run: ``interval_shoot_num=1``, capture only while
        ``interval_status`` is ``enable`` (the camera needs ~5 s after each shot),
        and the user's image count is restored at the end."""
        from .camera import Busy
        cam = self.cam
        if j.native.get("oneshot"):
            return
        try:
            async with cam.exclusive("timelapse", timeout=10) as t:
                r = await t.cgi("getsetting", "drivemode", timeout=4)
                cur = _drive_value(r)
                if cur is None or cur[0] == "normal":
                    return
                j.native.setdefault("drive_saved", cur)
                r = await t.cgi("setsetting", "drivemode", "normal", timeout=6)
                await asyncio.sleep(0.5)
                now = _drive_value(await t.cgi("getsetting", "drivemode", timeout=4))
                cam.log_event("timelapse", "timelapse %s: drive mode %s -> single for the server "
                                           "engine: %s (now %s)" % (
                                               j.name, "/".join(v for v in cur if v), r.result,
                                               "/".join(v for v in now if v) if now else "?"))
                if now is None or now[0] == "normal":
                    return
                j.native.pop("drive_saved", None)       # unchanged: nothing to put back
                if now[0] != "stopmotion_interval" or now[1] not in ("interval", None):
                    return
                if cam.state.get("interval_status") == "enable":
                    j.native.setdefault("shoot_num_saved", cam.state.get("interval_remain_num"))
                for typ, v in (("interval_shoot_num", "1"), ("interval_start_time", "-1")):
                    r = await t.cgi("setsetting", typ, v, timeout=6)
                    if not r.ok:
                        raise TimelapseError("camera refused %s=%s (%s)" % (typ, v, r.result))
                j.native["oneshot"] = True
                cam.log_event("timelapse", "timelapse %s: the camera keeps Time Lapse drive; every "
                                           "shot is a one-shot in-camera run" % j.name)
        except Busy as e:
            j.errors.append("drive mode: %s" % e)

    def _camera_pause(self, j: TimelapseJob, why: str) -> None:
        """Pause from inside the runner because the camera itself is in trouble.
        Not resumed on reconnect (``paused_by="camera"``): the user checks the
        camera and presses Resume."""
        j.state, j.paused_by, j.paused_at, j.waiting = "paused", "camera", time.time(), None
        j.next_shot_at = None
        j.message = "paused to protect the camera: %s. Check the camera (its screen, card, lens, " \
                    "battery), then Resume" % why
        j.errors.append("paused: %s" % why)
        self.cam.log_event("warn", "timelapse %s paused after %d shot(s): %s" % (j.name, j.shots, why))
        self._changed()

    async def _rec_ready(self, j: TimelapseJob) -> "tuple[str, str]":
        """("ready" | "wait" | "give_up", reason): Camera.auto_rec's policy (never
        fight a camera that left rec on its own), else the old live-view restart."""
        auto = getattr(self.cam, "auto_rec", None)
        if auto is not None:
            return await auto("timelapse %s" % j.name)
        ok = await self._ensure_rec(j)
        return ("ready", "") if ok else ("wait", "camera not in rec mode (%s)" % self.cam.state.get("cammode"))

    async def _ensure_rec(self, j: TimelapseJob) -> bool:
        cam = self.cam
        if cam.state.get("cammode") == "rec":
            return True
        if time.time() - self._last_rec_try < REC_RETRY_S:
            return False
        self._last_rec_try = time.time()
        from .camera import Busy
        try:
            async with cam.exclusive("timelapse", timeout=5):
                cam.liveview_wanted = True
                await cam.start_liveview("timelapse")
        except Busy:
            return False
        return cam.state.get("cammode") == "rec"

    async def _shoot(self, j: TimelapseJob, tol: float) -> None:
        from .camera import Busy
        cam = self.cam
        reason = None
        if cam.wedged:
            reason = "camera stuck answering err_busy (power-cycle it)"
        elif getattr(self.files, "active", False):
            reason = "file browsing"
        elif cam.state.get("rec") == "on":
            reason = "video recording in progress"
        else:
            deadline = time.monotonic() + tol
            while cam.body_busy and time.monotonic() < deadline:
                await asyncio.sleep(0.25)       # nothing is sent while the body is in use
            if cam.body_busy:
                reason = "camera busy (in use on the body)"
        if reason is None:
            st, why = await self._rec_ready(j)
            if st == "give_up":
                self._skip(j, why)
                self._camera_pause(j, why)
                return
            if st != "ready":
                reason = why
        if reason is None and j.native.get("oneshot"):
            # a capture now would land on a run that is still finishing
            deadline = time.monotonic() + tol
            while cam.state.get("interval_status") != "enable" and time.monotonic() < deadline:
                await asyncio.sleep(0.25)
            if cam.state.get("interval_status") != "enable":
                reason = "camera still finishing the last shot (interval_status %s)" % (
                    cam.state.get("interval_status"))
        if reason:
            self._skip(j, reason)
            return
        tries = 1 + max(0, min(2, int(tol) - 1))
        ok = False
        try:
            async with self._shot_lock:
                async with cam.exclusive("timelapse", timeout=tol):
                    before = cam.state.get("remaincapacity")
                    t_cap = time.monotonic()
                    r = await cam.cmd("camcmd", value="capture", timeout=15, tries=tries,
                                      judge_wedge=False)
                    j.last_result = r.result
                    if r.ok:
                        # let the card write settle (bounded, so the grid holds); the
                        # real S5 reported err_rec ~3.7 s after a capture that wrote nothing
                        end = time.monotonic() + min(5.0, j.interval_s * 0.6)
                        written = False
                        while time.monotonic() < end:
                            await asyncio.sleep(0.4)
                            with contextlib.suppress(Exception):
                                await cam._refresh_state()
                            if cam.state.get("remaincapacity") not in (before, None):
                                written = True
                                break
                            if self._photo_error_since(t_cap):
                                break
                        perr = None if written else self._photo_error_since(t_cap)
                        if perr:
                            j.failed += 1
                            j.last_result = perr
                            j.errors.append("shot %d: camera reported %s (no photo written)"
                                            % (j.slot + 1, perr))
                            self._photo_err_streak += 1
                        else:
                            ok = True
                            self._photo_err_streak = 0
                            j.shots += 1
                            j.last_shot_at = time.time()
                    elif r.result == "err_busy":
                        self._skip(j, "camera answered err_busy %dx" % tries)
                    else:
                        j.failed += 1
                        j.errors.append("shot %d: capture -> %s" % (j.slot + 1, r.result))
                        cam.log_event("warn", "timelapse %s: capture -> %s" % (j.name, r.result))
        except Busy as e:
            self._skip(j, str(e))
            return
        if self._photo_err_streak >= PHOTO_ERROR_PAUSE:
            self._photo_err_streak = 0
            self._camera_pause(j, "the camera failed to record %d photos in a row (%s) and dropped "
                                  "to playback" % (PHOTO_ERROR_PAUSE, j.last_result))
            return
        if ok:
            self._grab_frame(j)
            if j.auto_import and j.import_mode == "each":
                await self._import_frames(j, back_to_rec=True)

    def _photo_error_since(self, t: float) -> Optional[str]:
        pe = getattr(self.cam, "photo_error", None)
        return pe[1] if pe and pe[0] >= t else None

    def _grab_frame(self, j: TimelapseJob) -> None:
        frame = getattr(getattr(self.cam, "liveview", None), "frame", None)
        if not frame:
            return
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self.frame_path + ".tmp", "wb") as f:
                f.write(frame)
            os.replace(self.frame_path + ".tmp", self.frame_path)
            j.has_frame, j.frame_at = True, time.time()
        except OSError:
            pass

    # ----------------------------------------------------------- native engine
    async def _native_arm(self, j: TimelapseJob, count: int) -> None:
        """Arm and start an in-camera run of ``count`` shots (TimelapseError when
        the camera does not start it)."""
        from .camera import Busy
        cam = self.cam
        try:
            async with cam.exclusive("timelapse", timeout=15) as t:
                with contextlib.suppress(CamError, OSError, asyncio.TimeoutError):
                    await cam._refresh_state()
                if cam.state.get("interval_status") in INTERVAL_RUNNING:
                    # never arm into a run: setsetting/recmode then wedge the S5 (11:55)
                    raise RunInProgress("the camera is already running an interval run "
                                        "(interval_status %s)" % cam.state.get("interval_status"))
                cam.interval_owner = "timelapse"
                if cam.state.get("cammode") != "rec":
                    await cam.ensure_rec()
                r = await t.cgi("getsetting", "drivemode", timeout=4)
                cur = _drive_value(r)
                if "drive_saved" not in j.native and "drivemode_saved" not in j.native:
                    j.native["drive_saved"] = cur
                steps = [] if cur == ["stopmotion_interval", "interval"] else \
                    [("drivemode", "stopmotion_interval", "interval")]
                steps += [("interval_shoot_interval", str(int(j.interval_s)), None),
                          ("interval_shoot_num", str(count), None),
                          ("interval_start_time", "-1", None)]
                for typ, v, v2 in steps:
                    r = await t.cgi("setsetting", typ, v, v2, timeout=6)
                    cam.log_event("timelapse", "native: setsetting %s=%s -> %s" % (typ, v, r.result))
                    if not r.ok:
                        raise TimelapseError("camera refused %s=%s (%s)" % (typ, v, r.result))
                await asyncio.sleep(0.3)
                with contextlib.suppress(CamError, OSError, asyncio.TimeoutError):
                    await cam._refresh_state()
                st = cam.state.get("interval_status")
                for _ in range(20):
                    if st != "off":
                        break
                    await asyncio.sleep(0.5)            # last run just finished (~5 s)
                    with contextlib.suppress(CamError, OSError, asyncio.TimeoutError):
                        await cam._refresh_state()
                    st = cam.state.get("interval_status")
                if st != "enable":
                    # not armed yet (LUMIX Sync's step; the S5 rejects it once armed)
                    for _ in range(10):
                        r = await t.cgi("camctrl", "interval", "set", timeout=6)
                        if not r.result.startswith("ok_under_research"):
                            break
                        await asyncio.sleep(1.0)
                    cam.log_event("timelapse", "native: camctrl interval set (status %s) -> %s"
                                  % (st, r.result))
                    if r.result != "ok":
                        raise TimelapseError("camctrl interval set -> %s (interval_status %s)"
                                             % (r.result, st))
                r = await cam.cmd("camcmd", value="capture", timeout=10, tries=3, judge_wedge=False)
                cam.log_event("timelapse", "native: shutter (starts the run) -> %s" % r.result)
                if not r.ok:
                    raise TimelapseError("starting the interval run: capture -> %s" % r.result)
                # the run shows up in getstate within a second or two
                started = False
                for _ in range(10):
                    await asyncio.sleep(0.5)
                    with contextlib.suppress(CamError, OSError, asyncio.TimeoutError):
                        await cam._refresh_state()
                    st = cam.state.get("interval_status")
                    if st in INTERVAL_RUNNING or (st == "off" and count <= 1):
                        started = True
                        break
                if not started:
                    raise TimelapseError("the camera took the shutter but did not start an interval "
                                         "run (interval_status %s)" % st)
        except Busy as e:
            cam.interval_owner = None
            raise TimelapseError(str(e))
        except Exception:
            cam.interval_owner = None
            raise
        j.native.update(armed=True, armed_count=count, base=j.shots, armed_at=time.time(),
                        seen_run=False, remain=count)
        self._changed()

    async def _native_ctrl(self, j: TimelapseJob, op: str) -> Optional[str]:
        """camctrl interval pause|restart|stop (best effort, short lock)."""
        if self.cam.status != "ready" or self.cam.transport is None:
            return None
        from .camera import Busy
        try:
            async with self.cam.exclusive("timelapse", timeout=8) as t:
                r = await t.cgi("camctrl", "interval", op, timeout=6)
        except (Busy, Exception) as e:
            self.cam.log_event("warn", "native: camctrl interval %s failed: %s" % (op, e))
            return None
        self.cam.log_event("timelapse", "native: camctrl interval %s -> %s" % (op, r.result))
        return r.result

    async def _native_restore(self, j: TimelapseJob) -> None:
        self.cam.interval_owner = None
        await self._restore_drive(j)

    async def _restore_drive(self, j: TimelapseJob) -> None:
        """Put back the drive mode the run started from (native: the user's mode if
        it was not Time Lapse; server: Time Lapse etc. after single drive) and the
        image count a one-shot server run replaced."""
        num = j.native.get("shoot_num_saved")
        if num and self.cam.status == "ready":
            from .camera import Busy
            with contextlib.suppress(Busy, Exception):
                deadline = time.monotonic() + 10
                while (self.cam.state.get("interval_status") in INTERVAL_RUNNING + ("off",)
                       and time.monotonic() < deadline):
                    await asyncio.sleep(0.5)            # let the last one-shot run end
                async with self.cam.exclusive("timelapse", timeout=8) as t:
                    r = await t.cgi("setsetting", "interval_shoot_num", str(num), timeout=6)
                    self.cam.log_event("timelapse", "timelapse %s: image count back to %s -> %s"
                                       % (j.name, num, r.result))
                    if r.ok:
                        j.native.pop("shoot_num_saved", None)
        saved = j.native.get("drive_saved")
        if saved is None and j.native.get("drivemode_saved"):     # jobs saved before 2026-10-12
            saved = [j.native["drivemode_saved"], None]
        if not saved or not saved[0] or self.cam.status != "ready":
            return
        if j.engine == "native" and saved[0] == "stopmotion_interval":
            return                                  # the run left it there
        from .camera import Busy
        with contextlib.suppress(Busy, Exception):
            async with self.cam.exclusive("timelapse", timeout=8) as t:
                r = await t.cgi("setsetting", "drivemode", saved[0], saved[1], timeout=6)
                self.cam.log_event("timelapse", "timelapse %s: drive mode back to %s -> %s"
                                   % (j.name, "/".join(v for v in saved if v), r.result))
                if r.ok:
                    j.native.pop("drive_saved", None)
                    j.native.pop("drivemode_saved", None)

    async def _fallback_to_server(self, j: TimelapseJob, why: str) -> None:
        """The in-camera run could not be started: shoot from the server instead."""
        self.cam.interval_owner = None
        j.errors.append("in-camera: %s" % why)
        j.engine = "server"
        j.native["fallback"] = why
        j.message = ("In-camera interval could not be started (%s); the server is taking "
                     "the shots instead" % why)
        self.cam.log_event("warn", "timelapse %s: %s" % (j.name, j.message))
        if j.anchor_t is None:
            j.reanchor()
        self._changed()
        await self._run_server(j)

    def _adopt(self, j: TimelapseJob, why: Optional[str]) -> None:
        """Re-attach ``j`` to the run the camera is doing right now (reads only)."""
        cam, nat = self.cam, j.native
        st = cam.state.get("interval_status")
        try:
            remain = int(cam.state.get("interval_remain_num") or -1)
        except ValueError:
            remain = -1
        if not nat.get("armed_count") or remain > nat["armed_count"]:
            # no record of the armed count (job lost / run started on the body)
            nat.update(armed_count=max(remain, 0) + j.shots, base=0)
        nat.update(armed=True, seen_run=True, remain=remain)
        nat.setdefault("armed_at", time.time())
        if 0 <= remain <= nat["armed_count"]:
            j.shots = nat["base"] + nat["armed_count"] - remain
        cam.interval_owner = "timelapse"
        j.waiting = "interval run paused on the camera" if st == "pause" else None
        if why:
            j.message = "%s: the camera kept shooting on its own (%s shots left)" % (
                why, remain if remain >= 0 else "?")
            cam.log_event("timelapse", "timelapse %s: adopted the camera's interval run (%s, %s left, "
                                       "%d shot(s) so far); nothing sent" % (j.name, st, remain, j.shots))
        self._changed()

    async def _run_lost(self, j: TimelapseJob, why: str) -> bool:
        """The camera has no interval run for an armed job. Never re-arm on our
        own (setsetting + shutter into a camera we did not see finish). Returns
        True when the job is over (marked done), False when it was paused."""
        nat = j.native
        nat["armed"] = False
        warn = self.cam.state.get("warn_disp") or ""
        expected_end = (nat.get("armed_at") or 0) + max(0, (nat.get("armed_count") or 0) - 1) * j.interval_s
        if "interval_finish" in warn or nat.get("remain") == 0 or time.time() >= expected_end:
            j.shots = max(j.shots, (nat.get("base") or 0) + (nat.get("armed_count") or 0))
            j.message = "the camera finished the interval run (%s)" % why
            return True
        left = (j.count or 0) - j.shots
        j.state, j.paused_by, j.paused_at, j.waiting = "paused", "camera", time.time(), None
        j.next_shot_at = None
        j.message = ("The camera is no longer running the interval run (%s; switched off or "
                     "cancelled on the body?). Resume re-arms the remaining %d shot(s)." % (why, left))
        self.cam.interval_owner = None
        self.cam.log_event("warn", "timelapse %s: %s" % (j.name, j.message))
        self._changed()
        return False

    async def _run_native(self, j: TimelapseJob) -> None:
        cam = self.cam
        await self._wait_start(j)
        await self._wait_ready(j)
        nat = j.native
        resume = bool(nat.pop("resume_requested", False))
        with contextlib.suppress(CamError, OSError, asyncio.TimeoutError):
            await cam._refresh_state()              # the 1 Hz poll may be stale
        st = cam.state.get("interval_status")
        if st in INTERVAL_RUNNING and (nat.get("armed") or not resume):
            # the camera is shooting (or holds a paused run): adopt, send nothing;
            # only an explicit resume restarts a run paused on the camera
            self._adopt(j, None if resume else "server restarted")
            if st == "pause" and resume:
                await self._native_ctrl(j, "restart")
                j.waiting = None
        elif nat.get("armed"):
            if not resume:
                if await self._run_lost(j, "after a server restart"):
                    await self._native_done(j)
                return
            nat["armed"] = False                    # explicit resume: re-arm below
        if not nat.get("armed"):
            left = (j.count or 0) - j.shots
            if left <= 0:
                await self._finish(j, "done")
                return
            try:
                await self._native_arm(j, left)
            except asyncio.CancelledError:
                raise
            except RunInProgress:
                self._adopt(j, "already running")
            except Exception as e:      # TimelapseError, CamError, OSError, timeouts
                log.warning("native arm failed, falling back to the server engine: %s", e)
                if cam.state.get("interval_status") in INTERVAL_RUNNING:
                    # the camera started a run after all: never put server shots on top
                    self._adopt(j, "arm reported %s" % e)
                elif cam.wedged or "err_critical" in str(e) or "err_busy" in str(e):
                    # a camera refusing settings is not one the server can shoot with
                    cam.interval_owner = None
                    j.errors.append("in-camera: %s" % e)
                    j.state, j.paused_by, j.paused_at = "paused", "camera", time.time()
                    j.message = "The camera refused the interval settings (%s); paused" % e
                    cam.log_event("warn", "timelapse %s: %s" % (j.name, j.message))
                    self._changed()
                    return
                else:
                    await self._fallback_to_server(j, str(e) or type(e).__name__)
                    return
        cam.interval_owner = "timelapse"
        last_remain = nat.get("remain")
        while True:
            await asyncio.sleep(1.0)
            if await self._wait_ready(j):
                with contextlib.suppress(CamError, OSError, asyncio.TimeoutError):
                    await cam._refresh_state()
                if cam.state.get("interval_status") not in INTERVAL_RUNNING:
                    if not await self._run_lost(j, "after a reconnect"):
                        return
                    break
                self._adopt(j, "camera back")
                last_remain = nat.get("remain")
                continue
            st = cam.state.get("interval_status")
            try:
                remain = int(cam.state.get("interval_remain_num") or -1)
            except ValueError:
                remain = -1
            if st in INTERVAL_RUNNING:
                nat["seen_run"] = True
                w = "interval run paused on the camera" if st == "pause" else None
                if w != j.waiting:
                    j.waiting = w
                    self._changed()
                if 0 <= remain <= nat["armed_count"]:
                    shots = nat["base"] + nat["armed_count"] - remain
                    if shots != j.shots:
                        j.shots = shots
                        j.last_shot_at = time.time()
                        j.next_shot_at = time.time() + j.interval_s
                        self._grab_frame(j)
                        self._changed()
                    last_remain = remain
                    nat["remain"] = remain
            elif nat.get("seen_run") or time.time() - nat.get("armed_at", 0) > 10:
                warn = cam.state.get("warn_disp") or ""
                if "interval_finish" in warn or last_remain in (0, None) or j.shots >= (j.count or 0):
                    j.shots = nat["base"] + nat["armed_count"]
                elif "interval_cancel" in warn:
                    j.message = "the interval run was cancelled on the camera"
                break
        await self._native_done(j)

    async def _native_done(self, j: TimelapseJob) -> None:
        j.next_shot_at = None
        j.native["armed"] = False
        await self._native_restore(j)
        j.state = "importing" if j.auto_import else "assembling" if j.video else j.state
        await self._finish(j, "done")

    def adopt_camera_run(self) -> bool:
        """Camera ready with an interval run going and no active job here (run
        started on the body, or the job file is gone): create a native job that
        follows it from getstate so the UI shows the progress and offers
        Pause/Stop. Nothing is sent to the camera."""
        cam = self.cam
        if self.is_active() or cam.status != "ready" or not getattr(cam, "interval_running", False):
            return False
        interval = 1.0
        s = cam.menu.by_cmd_type("interval_shoot_interval") if getattr(cam, "menu", None) else None
        with contextlib.suppress(TypeError, ValueError, AttributeError):
            interval = max(MIN_INTERVAL_S, float(s.current))
        try:
            remain = int(cam.state.get("interval_remain_num") or 0)
        except ValueError:
            remain = 0
        if self.job is not None:
            self.history.append(self.job.summary())
        name = "camera-run-%s" % time.strftime("%Y%m%d-%H%M")
        self.job = TimelapseJob(name=name, engine="native", interval_s=interval, count=max(1, remain),
                                state="running", started_at=time.time(),
                                folder=os.path.join(os.path.abspath(os.path.expanduser(DEFAULT_DEST)),
                                                    dt.date.today().isoformat(), "timelapse-%s" % name))
        self.job.native.update(adopted=True, armed=True, armed_count=max(1, remain), base=0,
                               armed_at=time.time(), remain=remain, seen_run=True)
        self._adopt(self.job, "found running on the camera")
        self._spawn()
        return True

    # ------------------------------------------------------------- finishing
    async def _finish(self, j: TimelapseJob, final: str) -> None:
        cam = self.cam
        if j.engine == "native" and final == "stopped" and not j.native.get("armed"):
            # stopped while the arm was still confirming the start: the camera may
            # already be shooting; an explicit Stop stops it
            with contextlib.suppress(CamError, OSError, asyncio.TimeoutError):
                await cam._refresh_state()
            if getattr(cam, "interval_running", False):
                j.native["armed"] = True
        if j.engine == "native" and j.native.get("armed"):
            await self._native_ctrl(j, "stop")
            j.native["armed"] = False
            await self._native_restore(j)
        elif j.engine == "server" and (j.native.get("drive_saved") or j.native.get("shoot_num_saved")):
            await self._restore_drive(j)
        if self.cam.interval_owner == "timelapse":
            self.cam.interval_owner = None
        if j.auto_import and j.shots > j.frames_imported:
            j.state = "importing"
            self._changed()
            if cam.status != "ready":
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wait_ready(j), 60)
            await self._import_frames(j, back_to_rec=True)
        if j.video:
            j.state = "assembling"
            self._changed()
            await self._assemble(j)
        j.state, j.finished_at, j.waiting = final, time.time(), None
        cam.log_event("timelapse", "timelapse %s %s: %d shot(s), %d skipped, %d missed%s" % (
            j.name, final, j.shots, j.skipped, j.missed,
            ", video %s" % j.video_path if j.video_path else ""))
        self._changed()

    async def _import_frames(self, j: TimelapseJob, back_to_rec: bool = True) -> None:
        """Import the shots not imported yet: the newest N photos on the card
        (a native run's frames are the newest Interval group). Play-mode round
        trip without quiesce hooks; back to rec + live view afterwards."""
        n = j.shots - j.frames_imported
        if n <= 0 or self.files is None:
            return
        fb = self.files
        try:
            async with fb.quiet():
                try:
                    res = await fb.list(0, n, kind="photo", refresh=True)
                    items = list(reversed(res["items"][:n]))     # oldest first
                    ids = [i["id"] for i in items]
                    if ids:
                        ijob = fb.new_job(ids, dest=j.dest, include_raw=False, folder=j.folder,
                                          label="timelapse %s" % j.name)
                        await fb.run_job(ijob)
                        for i in items:
                            p = os.path.join(j.folder, i["name"])
                            if os.path.exists(p) and p not in j.frames:
                                j.frames.append(p)
                        j.frames_imported += len(ids) - ijob.files_failed
                        if ijob.errors:
                            j.import_error = ijob.errors[-1].get("error")
                finally:
                    if back_to_rec:
                        await fb.leave(force=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            j.import_error = "%s: %s" % (type(e).__name__, e)
            self.cam.log_event("warn", "timelapse %s: import failed: %s" % (j.name, j.import_error))
        self._changed()

    async def _assemble(self, j: TimelapseJob) -> None:
        ffmpeg = shutil.which("ffmpeg")
        frames = [f for f in j.frames if os.path.exists(f) and f.lower().endswith((".jpg", ".jpeg"))]
        if not ffmpeg:
            j.video_error = "ffmpeg not installed (brew install ffmpeg); frames are in %s" % j.folder
            return
        if len(frames) < 2:
            j.video_error = "need at least 2 imported JPEG frames (have %d)" % len(frames)
            return
        out = os.path.join(j.folder, "timelapse-%s.mp4" % j.name)
        with tempfile.TemporaryDirectory(prefix="lumix-tl-") as tmp:
            for i, f in enumerate(frames):
                os.symlink(f, os.path.join(tmp, "f%06d.jpg" % i))
            cmd = [ffmpeg, "-y", "-loglevel", "error", "-framerate", str(j.fps),
                   "-i", os.path.join(tmp, "f%06d.jpg"),
                   "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264",
                   "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                _, err = await asyncio.wait_for(proc.communicate(), 600)
            except (OSError, asyncio.TimeoutError) as e:
                j.video_error = "ffmpeg: %s" % e
                return
            if proc.returncode != 0 or not os.path.exists(out):
                j.video_error = "ffmpeg exit %s: %s" % (proc.returncode, err.decode(errors="replace")[-300:])
                return
        j.video_path, j.video_error = out, None
        self.cam.log_event("timelapse", "timelapse %s: video %s (%d frames at %d fps)"
                           % (j.name, out, len(frames), j.fps))
